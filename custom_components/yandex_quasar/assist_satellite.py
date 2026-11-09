"""Assist satellite: the station's wake word + mic feed HA pipelines, answers are played by the station."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
import logging
from typing import Any
import uuid

from homeassistant.components import tts
from homeassistant.components.assist_pipeline import PipelineEvent, PipelineEventType, PipelineStage
from homeassistant.components.assist_satellite import (
    AssistSatelliteAnnouncement,
    AssistSatelliteConfiguration,
    AssistSatelliteEntity,
    AssistSatelliteEntityDescription,
    AssistSatelliteEntityFeature,
    AssistSatelliteWakeWord,
)
from homeassistant.components.intent import TimerEventType, TimerInfo, async_register_timer_handler
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import YandexQuasarConfigEntry
from .client import AdapterClient, AdapterError
from .const import DOMAIN
from .entity import YandexQuasarEntity, setup_station_entities

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
ANNOUNCE_TIMEOUT = 300
AUDIO_CHUNK_TIMEOUT = 3


async def async_setup_entry(
    hass: HomeAssistant, entry: YandexQuasarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_station_entities(entry, async_add_entities, lambda client, device_id: [
        YandexQuasarSatellite(entry, client, device_id)
    ])


class YandexQuasarSatellite(YandexQuasarEntity, AssistSatelliteEntity):
    entity_description = AssistSatelliteEntityDescription(key="assist_satellite")
    _attr_name = None
    _attr_supported_features = (
        AssistSatelliteEntityFeature.ANNOUNCE | AssistSatelliteEntityFeature.START_CONVERSATION
    )
    # The adapter fetches the URL and transcodes it to Ogg/Opus for the station.
    _attr_tts_options = {tts.ATTR_PREFERRED_FORMAT: "mp3"}

    def __init__(self, entry: YandexQuasarConfigEntry, client: AdapterClient, device_id: str) -> None:
        super().__init__(client, device_id, "assist_satellite")
        self._entry = entry
        self._turn_id: str | None = None
        self._turn_seq: int | None = None
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._responded = False
        self._response_text = ""
        self._continue_conversation = False
        self._announcements: dict[str, asyncio.Future[dict]] = {}
        self._native_timers: set[str] = set()  # HA timer ids mirrored on the station

    # ---- select entities ------------------------------------------------------------------------------------

    def _select_entity_id(self, key: str) -> str | None:
        return er.async_get(self.hass).async_get_entity_id("select", DOMAIN, f"{self._device_id}-{key}")

    @property
    def pipeline_entity_id(self) -> str | None:
        return self._select_entity_id("pipeline")

    @property
    def vad_sensitivity_entity_id(self) -> str | None:
        return self._select_entity_id("vad_sensitivity")

    # ---- configuration --------------------------------------------------------------------------------------

    @callback
    def async_get_configuration(self) -> AssistSatelliteConfiguration:
        words = self.station.get("wake_words") or [{"id": "alisa", "name": "Алиса"}]
        return AssistSatelliteConfiguration(
            available_wake_words=[AssistSatelliteWakeWord(w["id"], w["name"], ["ru"]) for w in words],
            active_wake_words=[self.station.get("active_wake_word") or words[0]["id"]],
            max_active_wake_words=1,
        )

    async def async_set_configuration(self, config: AssistSatelliteConfiguration) -> None:
        if not config.active_wake_words:
            raise HomeAssistantError("The station always needs one active wake word")
        try:
            info = await self._client.request(
                "config.wake_word", self._device_id, {"wake_word": config.active_wake_words[0]}
            )
        except AdapterError as err:
            raise HomeAssistantError(f"Could not change the wake word: {err}") from err
        self._client.stations[self._device_id] = {**self.station, **info}
        self.async_write_ha_state()

    # ---- lifecycle ------------------------------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._client.subscribe_events(self._device_id, self._on_adapter_event))
        if self.registry_entry and self.registry_entry.device_id:
            # Without a handler HA's timer intents refuse to run on this device ("can't set timers").
            self.async_on_remove(
                async_register_timer_handler(self.hass, self.registry_entry.device_id, self._handle_timer)
            )

    @callback
    def _handle_timer(self, event_type: TimerEventType, timer: TimerInfo) -> None:
        self._entry.async_create_background_task(
            self.hass, self._sync_timer(event_type, timer), f"{self.entity_id} timer {event_type.value}"
        )

    async def _sync_timer(self, event_type: TimerEventType, timer: TimerInfo) -> None:
        """Mirror HA timers to native station timers: the station shows the countdown and rings by itself."""
        native = timer.id in self._native_timers
        try:
            if event_type is TimerEventType.STARTED:
                await self._set_native_timer(timer)
            elif event_type is TimerEventType.UPDATED and native:
                # Time added/removed or paused/resumed: re-create with the remaining time, then pause if needed.
                await self._client.request("timer.cancel", self._device_id, {"timer_id": timer.id})
                await self._set_native_timer(timer)
                if not timer.is_active:
                    await self._client.request("timer.pause", self._device_id, {"timer_id": timer.id})
            elif event_type is TimerEventType.CANCELLED and native:
                self._native_timers.discard(timer.id)
                await self._client.request("timer.cancel", self._device_id, {"timer_id": timer.id})
            elif event_type is TimerEventType.FINISHED:
                self._native_timers.discard(timer.id)
                if not native:
                    await self._announce_timer(timer)
        except AdapterError as err:
            _LOGGER.warning("Could not sync timer %s (%s) to the station: %s", timer.id, event_type.value, err)

    async def _set_native_timer(self, timer: TimerInfo) -> None:
        self._native_timers.discard(timer.id)
        await self._client.request("timer.set", self._device_id, {
            "timer_id": timer.id, "duration": max(1, timer.seconds_left), "title": timer.name or "",
        })
        self._native_timers.add(timer.id)

    async def _announce_timer(self, timer: TimerInfo) -> None:
        """Fallback for timers the station did not get (offline when the timer started)."""
        message = f"Таймер {timer.name} закончился" if timer.name else "Таймер закончился"
        try:
            await self.async_internal_announce(message=message)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not announce finished timer: %s", err)

    async def async_will_remove_from_hass(self) -> None:
        self._end_audio()
        await super().async_will_remove_from_hass()

    # ---- adapter → HA ---------------------------------------------------------------------------------------

    @callback
    def _on_adapter_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "voice.start":
            self._start_turn(data)
        elif kind == "voice.stop" and data.get("turn_id") == self._turn_id:
            _LOGGER.debug("voice.stop %s", data)
            self._end_audio()
            if data.get("reason") != "end":
                # Station gave up (cancel/no speech/timeout): the adapter already answered it.
                self._turn_id = None
                self._entry.async_create_background_task(
                    self.hass, self._cancel_running_pipeline(), "yandex_quasar cancel pipeline"
                )
        elif kind == "voice.playback_finished":
            turn_id = data.get("turn_id")
            fut = self._announcements.pop(turn_id, None)
            if fut is not None:
                if not fut.done():
                    fut.set_result(data)
            elif turn_id == self._turn_id or self._turn_id is None:
                self._turn_id = None
                self.tts_response_finished()

    @callback
    def _start_turn(self, data: dict[str, Any]) -> None:
        self._end_audio()
        self._turn_id = data["turn_id"]
        self._turn_seq = data["turn_seq"]
        self._responded = False
        self._response_text = ""
        self._continue_conversation = False
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._audio_queue = queue
        self._client.set_audio_handler(self._turn_seq, queue.put_nowait)
        self._entry.async_create_background_task(
            self.hass,
            self.async_accept_pipeline_from_satellite(
                audio_stream=self._stream(queue),
                start_stage=PipelineStage.STT,
                end_stage=PipelineStage.TTS,
                wake_word_phrase=data.get("wake_word") or None,
            ),
            f"{self.entity_id} pipeline",
        )

    @callback
    def _end_audio(self) -> None:
        if self._turn_seq is not None:
            self._client.set_audio_handler(self._turn_seq, None)
            self._turn_seq = None
        self._audio_queue.put_nowait(None)

    async def _stream(self, queue: asyncio.Queue[bytes | None]) -> AsyncIterable[bytes]:
        while True:
            try:
                async with asyncio.timeout(AUDIO_CHUNK_TIMEOUT):
                    chunk = await queue.get()
            except TimeoutError:
                _LOGGER.debug("Audio stream stalled, ending STT")
                return
            if chunk is None:
                return
            yield chunk

    # ---- HA pipeline → adapter ------------------------------------------------------------------------------

    def _send(self, type_: str, data: dict[str, Any]) -> None:
        async def _do() -> None:
            try:
                await self._client.request(type_, self._device_id, data)
            except AdapterError as err:
                _LOGGER.debug("%s failed: %s", type_, err)

        self._entry.async_create_background_task(self.hass, _do(), f"yandex_quasar {type_}")

    def on_pipeline_event(self, event: PipelineEvent) -> None:
        turn_id = self._turn_id
        if turn_id is None:
            return
        data = event.data or {}
        if event.type is PipelineEventType.STT_END:
            self._end_audio()
            self._send("voice.stt_end", {"turn_id": turn_id, "text": data.get("stt_output", {}).get("text", "")})
        elif event.type is PipelineEventType.INTENT_END:
            output = data.get("intent_output") or {}
            self._continue_conversation = bool(output.get("continue_conversation"))
            speech = (output.get("response") or {}).get("speech") or {}
            self._response_text = (speech.get("plain") or {}).get("speech", "")
        elif event.type is PipelineEventType.TTS_START:
            self._response_text = data.get("tts_input") or self._response_text
        elif event.type is PipelineEventType.TTS_END:
            tts_output = data.get("tts_output") or {}
            url = tts_output.get("url")
            self._responded = True
            self._send("voice.respond", {
                "turn_id": turn_id,
                "text": self._response_text,
                "tts_url": async_process_play_media_url(self.hass, url) if url else None,
                "listen": self._continue_conversation,
            })
        elif event.type is PipelineEventType.ERROR:
            self._end_audio()
            self._responded = True
            self._send("voice.error", {"turn_id": turn_id, "code": data.get("code"), "message": data.get("message")})
            self._turn_id = None
        elif event.type is PipelineEventType.RUN_END and not self._responded:
            self._send("voice.respond", {"turn_id": turn_id, "text": self._response_text, "tts_url": None})
            self._turn_id = None

    # ---- announcements --------------------------------------------------------------------------------------

    async def async_announce(self, announcement: AssistSatelliteAnnouncement) -> None:
        await self._play_announcement(announcement, listen=False)

    async def async_start_conversation(self, start_announcement: AssistSatelliteAnnouncement) -> None:
        # The station opens the mic by itself after playback (should_listen) and sends a new voice.start.
        await self._play_announcement(start_announcement, listen=True)

    async def _play_announcement(self, announcement: AssistSatelliteAnnouncement, listen: bool) -> None:
        turn_id = str(uuid.uuid4())
        fut: asyncio.Future[dict] = self.hass.loop.create_future()
        self._announcements[turn_id] = fut
        preannounce = announcement.preannounce_media_id
        try:
            await self._client.request("voice.announce", self._device_id, {
                "turn_id": turn_id,
                "media_url": async_process_play_media_url(self.hass, announcement.media_id),
                "preannounce_url": async_process_play_media_url(self.hass, preannounce) if preannounce else None,
                "text": announcement.message,
                "listen": listen,
            })
            async with asyncio.timeout(ANNOUNCE_TIMEOUT):
                result = await fut
        except AdapterError as err:
            raise HomeAssistantError(f"Announcement failed: {err}") from err
        finally:
            self._announcements.pop(turn_id, None)
        if result.get("error"):
            raise HomeAssistantError(f"Announcement failed: {result['error']}")
