"""One uniproxy (AAPI) connection of a station: voice turns, server-initiated turns, media control.

Protocol facts this code relies on are documented in the project Notion («Заметки об устройствах», «Протокол станции»).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import contextlib
import itertools
import logging
import time
import uuid
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from google.protobuf.json_format import MessageToDict

from . import aapi
from .audio import MicDecoder, OpusTranscoder, SpotterTailTrimmer, apply_gain, pcm_level_db
from .media import resolve_media_url
from .station import WAKE_WORDS

if TYPE_CHECKING:
    from .hub import Hub
    from .station import Station

_LOGGER = logging.getLogger(__name__)

MEDIA_CALLBACK_PREFIX = "qa_media_"
ANNOUNCE_TOKEN_PREFIX = "qa-turn:"
BOOMERANG_TIMEOUT = 5.0
PARTIAL_PLACEHOLDER = "…"

_turn_seq = itertools.count(1)


class CommandError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class Turn:
    """A dialog turn: started by the station (voice) or by us (announce)."""

    turn_id: str
    kind: str  # "voice" | "announce"
    seq: int = field(default_factory=lambda: next(_turn_seq))
    message_id: str = ""  # MessageId of the station event we answer (RefMessageId of our replies)
    request_id: str = ""
    sequence_number: int = 0
    mic_stream_id: int | None = None
    phase: str = "listening"  # listening | recognized | speaking | done
    decoder: MicDecoder | None = None
    preroll: list[bytes] = field(default_factory=list)
    forwarding: bool = False
    speech_seen: bool = False
    tail_trimmer: SpotterTailTrimmer | None = None
    asr_sent: bool = False
    debug_pcm: bytearray | None = None
    tasks: set[asyncio.Task] = field(default_factory=set)
    playback_done: asyncio.Future | None = None
    # announce-only
    media_urls: list[str] = field(default_factory=list)
    text: str = ""
    listen: bool = False


# Connections a station may hold at once. It opens a new uniproxy WS while the previous one is still alive (seen
# mid-turn, right after VoiceInput); older ones beyond this are closed.
MAX_SOCKETS = 4
ORIGIN_MEMORY = 256  # message/stream ids remembered to route replies


class StationSession:
    """One station, possibly over several uniproxy connections.

    Replies go to the connection the request came on (by RefMessageId / stream id), server-initiated messages to
    the newest one. The session, and the station's "online" state, lasts until its last connection closes.
    """

    def __init__(self, hub: Hub, station: Station, ws: web.WebSocketResponse) -> None:
        self.hub = hub
        self.station = station
        self._sockets: list[web.WebSocketResponse] = [ws]
        self._origin: OrderedDict[str, web.WebSocketResponse] = OrderedDict()
        self.cfg = hub.config
        self._send_lock = asyncio.Lock()
        self._next_stream = 2
        self._turns: dict[str, Turn] = {}
        self._voice: Turn | None = None
        self._announce_by_token: dict[str, Turn] = {}
        self._media_is_stream = False
        self._closed = False

    # ---- plumbing -------------------------------------------------------------------------------------------

    @property
    def ws(self) -> web.WebSocketResponse:
        """The newest connection: target of server-initiated messages."""
        return self._sockets[-1]

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def socket_count(self) -> int:
        return len(self._sockets)

    def attach(self, ws: web.WebSocketResponse) -> None:
        """Another connection of the same station: the session and its turns stay, new messages go here."""
        self._sockets.append(ws)
        while len(self._sockets) > MAX_SOCKETS:
            stale = self._sockets.pop(0)
            self.hub.track(asyncio.create_task(stale.close()))

    def detach(self, ws: web.WebSocketResponse) -> bool:
        """Forget a closed connection; True when it was the last one."""
        if ws in self._sockets:
            self._sockets.remove(ws)
        return not self._sockets

    async def run(self) -> None:
        await self.read(self.ws)

    async def read(self, ws: web.WebSocketResponse) -> None:
        async for msg in ws:
            if msg.type.name != "BINARY":
                continue
            data: bytes = msg.data
            try:
                if aapi.is_aapi(data):
                    message = aapi.parse_client(data)
                    self._remember_origin(message, ws)
                    await self._on_message(message)
                else:
                    stream_id, payload = aapi.split_stream_frame(data)
                    await self._on_audio(stream_id, payload)
            except Exception:
                _LOGGER.exception("[%s] failed to handle frame", self.station.device_id)

    def _remember(self, key: str, ws: web.WebSocketResponse) -> None:
        if not key:
            return
        self._origin[key] = ws
        self._origin.move_to_end(key)
        while len(self._origin) > ORIGIN_MEMORY:
            self._origin.popitem(last=False)

    def _remember_origin(self, msg: Any, ws: web.WebSocketResponse) -> None:
        if msg.WhichOneof("Message") == "Event":
            self._remember(msg.Event.Header.MessageId, ws)
            if msg.Event.Header.StreamId:
                self._remember(f"stream:{msg.Event.Header.StreamId}", ws)

    def _target(self, msg: Any) -> web.WebSocketResponse:
        """Where a message to the station goes: the connection of the request it answers, else the newest."""
        target = None
        if msg.WhichOneof("Message") == "Event":
            header = msg.Event.Header
            target = self._origin.get(header.RefMessageId)
            if target is not None and header.StreamId:  # our TTS stream follows its reply
                self._remember(f"stream:{header.StreamId}", target)
        elif msg.WhichOneof("Message") == "StreamControl":
            target = self._origin.get(f"stream:{msg.StreamControl.StreamId}")
        if target is None or target.closed or target not in self._sockets:
            return self.ws
        return target

    async def close(self) -> None:
        self._closed = True
        for turn in list(self._turns.values()):
            self._finish(turn, emit_stop="disconnected")

    async def _send(self, msg: Any) -> None:
        await self._send_raw(aapi.encode(msg), self._target(msg))

    async def _send_stream(self, stream_id: int, chunk: bytes) -> None:
        target = self._origin.get(f"stream:{stream_id}")
        if target is None or target.closed or target not in self._sockets:
            target = self.ws
        await self._send_raw(aapi.make_stream_frame(stream_id, chunk), target)

    async def _send_raw(self, data: bytes, ws: web.WebSocketResponse | None = None) -> None:
        ws = ws or self.ws
        if self._closed or ws.closed:
            return
        async with self._send_lock:
            try:
                await ws.send_bytes(data)
            except ConnectionResetError:
                # That connection is going away (network glitch, maind restart); others may still be open.
                _LOGGER.debug("[%s] send on closing connection dropped", self.station.device_id)

    def _spawn(self, turn: Turn | None, coro: Awaitable, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        if turn is not None:
            turn.tasks.add(task)
            task.add_done_callback(turn.tasks.discard)
        else:
            self.hub.track(task)
        return task

    def _alloc_stream(self) -> int:
        sid = self._next_stream
        self._next_stream += 2
        return sid

    # ---- station → adapter ----------------------------------------------------------------------------------

    async def _on_message(self, msg: Any) -> None:
        if msg.WhichOneof("Message") == "StreamControl":
            await self._on_stream_control(msg.StreamControl)
            return
        event = msg.Event
        kind = event.WhichOneof("Event")
        ref = event.Header.MessageId
        if kind == "SynchronizeStateRequest":
            await self._send(aapi.synchronize_state_response(ref, str(uuid.uuid4())))
        elif kind == "LogSpotter":
            await self._send(aapi.log_ack(ref))
        elif kind == "RequestStat":
            await self._send(aapi.request_stat_ack(ref))
            self._on_request_stat(event.RequestStat)
        elif kind == "VoiceInput":
            await self._on_voice_input(event)
        elif kind == "TextInput":
            await self._on_text_input(event)
        elif kind == "CancelRequest":
            self._on_cancel(event.CancelRequest.RequestMessageId)
        else:
            _LOGGER.debug("[%s] ignored event %s", self.station.device_id, kind)

    def _update_device_state(self, request: Any) -> None:
        state = aapi.parse_device_state(request.DeviceStateRaw)
        if state is not None and self.station.apply_device_state(state):
            self.hub.emit_state(self.station)

    async def _on_voice_input(self, event: Any) -> None:
        vi = event.VoiceInput
        self._update_device_state(vi.Request)
        if self._voice is not None:
            self._finish(self._voice, emit_stop="cancel")
        turn = Turn(
            turn_id=str(uuid.uuid4()), kind="voice", message_id=event.Header.MessageId,
            request_id=vi.Header.RequestId, sequence_number=vi.Header.SequenceNumber,
            mic_stream_id=event.Header.StreamId,
        )
        if self.cfg.debug_audio_dir:
            turn.debug_pcm = bytearray()
        self._turns[turn.turn_id] = turn
        self._voice = turn
        await self._send(aapi.input_start_ack(turn.message_id))
        wake_word = None
        if vi.Request.ActivationType == "spotter":
            # Some activations come with an empty SpotterPhrase; HA then saw "" and rejected repeats as
            # "Duplicate wake-up detected for ". Fall back to the configured wake word.
            phrase = (vi.SpotterPhrase or "").strip() or WAKE_WORDS.get(self.station.wake_word, "")
            wake_word = phrase.capitalize() or None
        _LOGGER.info(
            "[%s] voice turn %s (activation=%s)", self.station.device_id, turn.turn_id,
            vi.Request.ActivationType or "?",
        )
        if not self.hub.has_ha_clients():
            _LOGGER.warning("[%s] no Home Assistant connected, dropping voice request", self.station.device_id)
            await self._respond_empty(turn)
            return
        self._set_alice_state("listening")
        self.hub.emit(self.station, "voice.start", {
            "turn_id": turn.turn_id, "turn_seq": turn.seq, "wake_word": wake_word,
            "reason": "wake_word" if wake_word else "follow_up",
        })
        self._spawn(turn, self._preroll_timeout(turn), "preroll")
        self._spawn(turn, self._partials(turn), "partials")
        self._spawn(turn, self._utterance_timeout(turn), "utterance-timeout")

    async def _on_audio(self, stream_id: int, payload: bytes) -> None:
        turn = self._voice
        if turn is None or turn.mic_stream_id != stream_id or turn.phase != "listening":
            return
        if not turn.forwarding:
            turn.preroll.append(payload)
            return
        self._forward(turn, payload)

    def _forward(self, turn: Turn, payload: bytes) -> None:
        assert turn.decoder is not None
        raw = turn.decoder.feed(payload)
        if not raw:
            return
        # Detectors (speech level, wake-word tail) always look at the boosted audio: their thresholds are absolute.
        boosted = apply_gain(raw, self.cfg.mic_gain_db)
        pcm = boosted if self.station.mic_preamp else raw
        if turn.tail_trimmer is not None:
            pcm = turn.tail_trimmer.feed(boosted, pcm)
            if turn.tail_trimmer.cut_bytes is not None:
                if turn.tail_trimmer.cut_bytes:
                    _LOGGER.debug("[%s] cut %d ms of wake-word tail", self.station.device_id,
                                  turn.tail_trimmer.cut_bytes // 32)
                turn.tail_trimmer = None
        if pcm:
            # Same span as forwarded (after the tail cut), at the detector's gain.
            level_pcm = pcm if self.station.mic_preamp else apply_gain(pcm, self.cfg.mic_gain_db)
            if not turn.speech_seen and pcm_level_db(level_pcm) > self.cfg.speech_threshold_db:
                turn.speech_seen = True
            if turn.debug_pcm is not None:
                turn.debug_pcm += pcm
            self.hub.emit_audio(turn.seq, pcm)

    async def _preroll_timeout(self, turn: Turn) -> None:
        # Wake-word activations push ~2 s of buffered audio together with SpotterEnd. Without SpotterEnd
        # (follow-up listening) everything is the command, so start forwarding after a short wait.
        await asyncio.sleep(self.cfg.preroll_timeout)
        if not turn.forwarding and turn.phase == "listening":
            self._start_forwarding(turn, keep_preroll=True)

    def _start_forwarding(self, turn: Turn, keep_preroll: bool) -> None:
        turn.forwarding = True
        turn.decoder = MicDecoder()
        buffered, turn.preroll = turn.preroll, []
        if keep_preroll:
            for chunk in buffered:
                self._forward(turn, chunk)

    async def _partials(self, turn: Turn) -> None:
        # The station closes the mic with NO_SPEECH ~5 s after SpotterEnd unless it sees a NON-EMPTY partial,
        # so once our level detector hears speech we report a placeholder partial until HA decides.
        while turn.phase == "listening":
            await asyncio.sleep(self.cfg.partial_interval)
            if turn.phase == "listening":
                text = PARTIAL_PLACEHOLDER if turn.speech_seen else ""
                await self._send(aapi.asr_result(turn.message_id, text, end_of_utt=False))

    async def _utterance_timeout(self, turn: Turn) -> None:
        await asyncio.sleep(self.cfg.max_utterance)
        if turn.phase == "listening":
            _LOGGER.info("[%s] turn %s: utterance timeout", self.station.device_id, turn.turn_id)
            self.hub.emit(self.station, "voice.stop", {"turn_id": turn.turn_id, "reason": "timeout"})
            await self._respond_empty(turn)

    async def _on_stream_control(self, ctrl: Any) -> None:
        turn = self._voice
        if turn is None or turn.mic_stream_id != ctrl.StreamId:
            return
        action = ctrl.WhichOneof("Action")
        if action == "SpotterEnd":
            if turn.phase == "listening" and not turn.forwarding:
                # The pre-roll starts ~80 ms before the end of the wake word (its "-са" tail) and holds whatever was
                # said while the spotter was confirming it. With include_wake_word the stream starts earlier and the
                # whole word is wanted, so it is neither dropped nor trimmed.
                station = self.station
                if station.trim_wake_word and not station.include_wake_word:
                    turn.tail_trimmer = SpotterTailTrimmer(self.cfg.spotter_tail_ms, self.cfg.speech_threshold_db)
                self._start_forwarding(turn, keep_preroll=station.include_wake_word or not station.drop_preroll)
        elif action == "Close" and turn.phase == "listening":
            reason = ctrl.Close.Reason
            _LOGGER.info("[%s] mic closed by station, reason=%s", self.station.device_id, reason)
            if reason == aapi.CLOSE_CANCEL:
                self.hub.emit(self.station, "voice.stop", {"turn_id": turn.turn_id, "reason": "cancel"})
                self._finish(turn)
            elif reason == aapi.CLOSE_NO_SPEECH:
                self.hub.emit(self.station, "voice.stop", {"turn_id": turn.turn_id, "reason": "no_speech"})
                await self._respond_empty(turn)
            else:
                # Station stopped recording; HA finishes STT with what it has and answers as usual.
                turn.phase = "recognized"
                self.hub.emit(self.station, "voice.stop", {"turn_id": turn.turn_id, "reason": "end"})

    def _on_cancel(self, request_message_id: str) -> None:
        for turn in list(self._turns.values()):
            if turn.message_id == request_message_id:
                _LOGGER.info("[%s] station cancelled turn %s", self.station.device_id, turn.turn_id)
                self._finish(turn, emit_stop="cancel")

    def _on_request_stat(self, stat: Any) -> None:
        for turn in self._turns.values():
            if turn.message_id == stat.RequestMessageId and turn.phase == "speaking":
                if turn.playback_done and not turn.playback_done.done():
                    turn.playback_done.set_result(stat.Timestamps.OnSoundPlayerEndTime)

    async def _on_text_input(self, event: Any) -> None:
        ti = event.TextInput
        req_event = ti.Request.Event
        self._update_device_state(ti.Request)
        payload = MessageToDict(req_event.Payload) if req_event.HasField("Payload") else {}
        name = req_event.Name
        ref = event.Header.MessageId

        token = payload.get("typed_semantic_frame", {}).get("tts_semantic_frame", {}).get("text", "")
        turn = self._announce_by_token.pop(token, None) if token.startswith(ANNOUNCE_TOKEN_PREFIX) else None
        if turn is not None:
            turn.message_id = ref
            turn.request_id = ti.Header.RequestId
            turn.sequence_number = ti.Header.SequenceNumber
            self._spawn(turn, self._speak(turn, turn.text, turn.media_urls, turn.listen), "announce")
            return

        if name.startswith(MEDIA_CALLBACK_PREFIX):
            _LOGGER.info("[%s] media callback %s", self.station.device_id, name)
            self._on_media_callback(name.removeprefix(MEDIA_CALLBACK_PREFIX), payload)
        else:
            _LOGGER.debug("[%s] server_action %s", self.station.device_id, name)
        # Answer every server_action, otherwise the station retries/timeouts after 15 s.
        await self._send(aapi.alice_response(ref, ti.Header.RequestId, ti.Header.SequenceNumber))

    # ---- turn completion ------------------------------------------------------------------------------------

    def _finish(self, turn: Turn, emit_stop: str | None = None) -> None:
        if turn.phase == "done":
            return
        if emit_stop:
            self.hub.emit(self.station, "voice.stop", {"turn_id": turn.turn_id, "reason": emit_stop})
        turn.phase = "done"
        current = asyncio.current_task()
        for task in list(turn.tasks):
            if task is not current:
                task.cancel()
        if turn.playback_done and not turn.playback_done.done():
            turn.playback_done.cancel()
        self._turns.pop(turn.turn_id, None)
        if self._voice is turn:
            self._voice = None
        if not self._turns:
            self._set_alice_state("idle")

    def _dump_debug_audio(self, turn: Turn) -> None:
        if not turn.debug_pcm:
            return
        path = Path(self.cfg.debug_audio_dir) / f"{turn.turn_id}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(turn.debug_pcm))
        turn.debug_pcm = None
        _LOGGER.info("[%s] turn audio saved to %s", self.station.device_id, path)

    async def _end_recognition(self, turn: Turn, text: str) -> None:
        if turn.asr_sent or turn.mic_stream_id is None:
            return
        turn.asr_sent = True
        self._dump_debug_audio(turn)
        if turn.phase == "listening":
            turn.phase = "recognized"
        await self._send(aapi.asr_result(turn.message_id, text, end_of_utt=True))
        await self._send(aapi.stream_close(turn.mic_stream_id))
        self._set_alice_state("thinking")
        self._spawn(turn, self._response_timeout(turn), "response-timeout")

    async def _response_timeout(self, turn: Turn) -> None:
        # The station waits vinsRequestTimeout (raised via get_sync_info) for an answer; close the turn just before it
        # so the user hears our short error sound instead of the station's "something went wrong" phrase. The HA
        # pipeline keeps running, so a slow action (e.g. an LLM starting music) still happens.
        await asyncio.sleep(self.cfg.ha_response_timeout)
        if turn.phase == "recognized":
            _LOGGER.warning("[%s] turn %s: no answer from Home Assistant in %.0f s", self.station.device_id,
                            turn.turn_id, self.cfg.ha_response_timeout)
            await self._respond_empty(turn, self._error_sound())

    async def _respond_empty(self, turn: Turn, directives: list[dict] | None = None) -> None:
        await self._end_recognition(turn, "")
        await self._send(aapi.alice_response(
            turn.message_id, turn.request_id, turn.sequence_number, directives=directives,
        ))
        self._finish(turn)

    def _error_sound(self) -> list[dict]:
        if not self.cfg.error_sound:
            return []
        try:
            return [aapi.local_sound(self.cfg.error_sound)]
        except ValueError:
            _LOGGER.warning("Unknown error_sound %r, see EStoredSound", self.cfg.error_sound)
            return []

    async def _speak(self, turn: Turn, text: str, urls: list[str], listen: bool) -> None:
        """AliceResponse + TtsSpeak + Ogg/Opus stream, then wait for the station to finish playing."""
        await self._end_recognition(turn, "")
        has_voice = bool(urls)
        await self._send(aapi.alice_response(
            turn.message_id, turn.request_id, turn.sequence_number,
            text=text, has_voice=has_voice, should_listen=listen,
        ))
        if not has_voice:
            self._playback_finished(turn)
            return
        turn.phase = "speaking"
        turn.playback_done = asyncio.get_running_loop().create_future()
        self._set_alice_state("speaking")
        stream_id = self._alloc_stream()
        transcoder = OpusTranscoder(urls, ffmpeg=self.cfg.ffmpeg)
        started = time.monotonic()
        error = None
        try:
            await self._send(aapi.tts_speak(turn.message_id, stream_id))
            async for chunk in transcoder.chunks():
                await self._send_stream(stream_id, chunk)
        except Exception as err:  # noqa: BLE001 - report to HA, never break the session
            _LOGGER.warning("[%s] TTS streaming failed: %s", self.station.device_id, err)
            error = str(err)
        finally:
            await self._send(aapi.stream_close(stream_id))
        if error:
            self._playback_finished(turn, error=error)
            return
        # Station reports the end via RequestStat; fall back to audio duration if it never arrives.
        remaining = max(0.0, transcoder.duration - (time.monotonic() - started)) + self.cfg.playback_grace
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(turn.playback_done), timeout=remaining)
        self._playback_finished(turn)

    def _playback_finished(self, turn: Turn, error: str | None = None) -> None:
        data: dict[str, Any] = {"turn_id": turn.turn_id}
        if error:
            data["error"] = error
        self.hub.emit(self.station, "voice.playback_finished", data)
        self._finish(turn)

    def _set_alice_state(self, value: str) -> None:
        if self.station.state.alice_state != value:
            self.station.state.alice_state = value
            self.hub.emit_state(self.station)

    # ---- HA commands ----------------------------------------------------------------------------------------

    async def command(self, type_: str, data: dict) -> dict:
        handler: Callable[[dict], Awaitable[dict]] | None = getattr(
            self, "_cmd_" + type_.replace(".", "_"), None
        )
        if handler is None:
            raise CommandError("unknown_command", type_)
        return await handler(data)

    def _turn(self, data: dict) -> Turn:
        turn = self._turns.get(data.get("turn_id", ""))
        if turn is None:
            raise CommandError("turn_closed")
        return turn

    async def _cmd_voice_stt_end(self, data: dict) -> dict:
        await self._end_recognition(self._turn(data), data.get("text", ""))
        return {}

    async def _cmd_voice_respond(self, data: dict) -> dict:
        turn = self._turn(data)
        urls = [data["tts_url"]] if data.get("tts_url") else []
        self._spawn(turn, self._speak(turn, data.get("text", ""), urls, bool(data.get("listen"))), "speak")
        return {}

    async def _cmd_voice_error(self, data: dict) -> dict:
        turn = self._turn(data)
        _LOGGER.info("[%s] HA pipeline error: %s", self.station.device_id, data.get("message"))
        await self._respond_empty(turn, self._error_sound())
        return {}

    async def _cmd_voice_abort(self, data: dict) -> dict:
        turn = self._turn(data)
        await self._respond_empty(turn)
        return {}

    async def _cmd_voice_announce(self, data: dict) -> dict:
        urls = [u for u in (data.get("preannounce_url"), data.get("media_url")) if u]
        if not urls:
            raise CommandError("invalid", "media_url is required")
        turn = Turn(
            turn_id=data.get("turn_id") or str(uuid.uuid4()), kind="announce", phase="recognized",
            media_urls=urls, text=data.get("text", ""), listen=bool(data.get("listen")),
        )
        token = ANNOUNCE_TOKEN_PREFIX + turn.turn_id
        self._turns[turn.turn_id] = turn
        self._announce_by_token[token] = turn
        await self._send(aapi.push([aapi.directive(
            "@@mm_semantic_frame",
            {"typed_semantic_frame": {"tts_semantic_frame": {"text": token}}},
            type_="server_action", led_silent=True,
        )]))
        self._spawn(turn, self._boomerang_timeout(turn, token), "boomerang-timeout")
        return {"turn_id": turn.turn_id}

    async def _boomerang_timeout(self, turn: Turn, token: str) -> None:
        await asyncio.sleep(BOOMERANG_TIMEOUT)
        if self._announce_by_token.pop(token, None) is not None:
            _LOGGER.warning("[%s] announce %s: station did not answer", self.station.device_id, turn.turn_id)
            self._playback_finished(turn, error="station_timeout")

    async def _push(self, *directives: dict) -> None:
        await self._send(aapi.push(list(directives)))

    async def play_stream(self, url: str, metadata: dict, source: str) -> None:
        """Play an adapter-served live stream (Sendspin bridge); our own URL needs no redirect probing."""
        await self._cmd_media_play({"url": url, "metadata": metadata, "resolve": False, "source": source})

    async def stop_stream(self) -> None:
        await self._push(aapi.directive("player_pause", {"smooth": True}))
        player = self.station.state.player
        player.state = "idle"
        player.stream_id = None
        self.hub.emit_state(self.station)

    async def _route_to_sendspin(self, name: str) -> bool:
        """While Music Assistant streams over Sendspin, playback control belongs to MA's queue."""
        sendspin = self.hub.sendspin.get(self.station.device_id)
        if sendspin is None or self.station.state.player.source != "sendspin":
            return False
        try:
            await sendspin.group_command(name)
        except (RuntimeError, ValueError) as err:
            raise CommandError("sendspin", str(err)) from err
        return True

    async def _cmd_media_play(self, data: dict) -> dict:
        if data.get("resolve", True):
            url, is_stream = await resolve_media_url(self.hub.http, data["url"])
        else:
            url, is_stream = data["url"], True
        meta = data.get("metadata") or {}
        offset = float(data.get("offset") or 0)
        stream_id = str(uuid.uuid4())
        callbacks = {
            # Same shape as the phase 0 experiment that is confirmed to come back (Notion, «Заметки об устройствах»).
            f"on_{event}": {
                "name": MEDIA_CALLBACK_PREFIX + event, "type": "server_action", "ignore_answer": True,
                "payload": {"stream_id": stream_id},
            }
            for event in ("started", "stopped", "finished", "failed")
        }
        await self._push(aapi.directive("audio_play", {
            "background_mode": "Ducking",
            "screen_type": "Music",
            "stream": {
                "id": stream_id, "type": "Track", "url": url, "format": "MP3",
                "offset_ms": int(offset * 1000),
            },
            "metadata": {
                "title": meta.get("title") or "", "subtitle": meta.get("artist") or "",
                "art_image_url": meta.get("image") or "",
            },
            "callbacks": callbacks,
        }))
        player = self.station.state.player
        player.state = "buffering"
        player.url = data["url"]
        player.source = data.get("source")
        player.stream_id = stream_id
        player.title = meta.get("title")
        player.artist = meta.get("artist")
        player.image = meta.get("image")
        player.duration = meta.get("duration")
        player.position = offset
        player.position_ts = time.time()
        self._media_is_stream = is_stream
        self.hub.emit_state(self.station)
        return {"stream_id": stream_id}

    def _on_media_callback(self, event: str, payload: dict) -> None:
        player = self.station.state.player
        if payload.get("stream_id") != player.stream_id:
            return  # callback of a replaced track
        if event == "started":
            player.state = "playing"
        elif event == "stopped" and player.source == "sendspin":
            # Stops we initiate clear stream_id first, so this is the station itself (local "пауза" fast command):
            # pause Music Assistant's queue too, otherwise MA keeps "playing" into a dead stream.
            player.state = "paused"
            self._spawn(None, self._pause_sendspin_queue(), "sendspin-pause")
        elif event in ("stopped", "finished") and player.state != "paused":
            player.state = "idle"
        elif event == "failed":
            player.state = "idle"
        self.hub.emit(self.station, "media.event", {"event": event, "url": player.url})
        self.hub.emit_state(self.station)

    async def _pause_sendspin_queue(self) -> None:
        try:
            await self._route_to_sendspin("pause")
        except CommandError as err:
            _LOGGER.warning("[%s] could not pause Music Assistant: %s", self.station.device_id, err)

    def _current_position(self) -> float:
        player = self.station.state.player
        if player.state == "playing":
            return player.position + (time.time() - player.position_ts)
        return player.position

    async def _cmd_media_pause(self, data: dict) -> dict:
        if await self._route_to_sendspin("pause"):
            return {}
        player = self.station.state.player
        position = self._current_position()
        await self._push(aapi.directive("player_pause", {"smooth": True}))
        player.state = "paused"
        player.position = position
        player.position_ts = time.time()
        self.hub.emit_state(self.station)
        return {}

    async def _cmd_media_resume(self, data: dict) -> dict:
        # player_continue does not work for audio_play on this firmware: restart the URL at the saved position.
        if await self._route_to_sendspin("resume"):
            return {}
        player = self.station.state.player
        if not player.url:
            raise CommandError("nothing_to_resume")
        return await self._cmd_media_play({
            "url": player.url,
            "offset": 0 if self._media_is_stream else player.position,
            "metadata": {"title": player.title, "artist": player.artist, "image": player.image,
                         "duration": player.duration},
        })

    async def _cmd_media_stop(self, data: dict) -> dict:
        if await self._route_to_sendspin("stop"):
            return {}
        await self._push(aapi.directive("player_pause", {"smooth": False}))
        player = self.station.state.player
        player.state = "idle"
        player.url = None
        player.source = None
        player.stream_id = None
        player.position = 0.0
        self.hub.emit_state(self.station)
        return {}

    async def _cmd_media_next(self, data: dict) -> dict:
        if await self._route_to_sendspin("next"):
            return {}
        raise CommandError("not_supported", "next track needs a queue (Music Assistant over Sendspin)")

    async def _cmd_media_previous(self, data: dict) -> dict:
        if await self._route_to_sendspin("previous"):
            return {}
        raise CommandError("not_supported", "previous track needs a queue (Music Assistant over Sendspin)")

    async def _cmd_media_seek(self, data: dict) -> dict:
        player = self.station.state.player
        if not player.url or self._media_is_stream:
            raise CommandError("not_seekable")
        return await self._cmd_media_play({
            "url": player.url, "offset": float(data["position"]),
            "metadata": {"title": player.title, "artist": player.artist, "image": player.image,
                         "duration": player.duration},
        })

    async def _cmd_volume_set(self, data: dict) -> dict:
        level = max(0.0, min(1.0, float(data["level"])))
        new_level = round(level * 10)
        await self._push(aapi.directive("sound_set_level", {"new_level": new_level}))
        self.station.state.volume = new_level / 10
        self.hub.emit_state(self.station)
        return {}

    async def _cmd_volume_step(self, data: dict) -> dict:
        name = "sound_louder" if data.get("direction") == "up" else "sound_quiter"
        await self._push(aapi.directive(name))
        volume = self.station.state.volume
        if volume is not None:
            delta = 0.1 if data.get("direction") == "up" else -0.1
            self.station.state.volume = round(max(0.0, min(1.0, volume + delta)), 2)
            self.hub.emit_state(self.station)
        return {}

    async def _cmd_volume_mute(self, data: dict) -> dict:
        muted = bool(data["muted"])
        await self._push(aapi.directive("sound_mute" if muted else "sound_unmute"))
        self.station.state.muted = muted
        self.hub.emit_state(self.station)
        return {}

    async def _cmd_station_get(self, data: dict) -> dict:
        return self.station.info()

    def _set_flag(self, attr: str, data: dict) -> dict:
        if not isinstance(data.get("enabled"), bool):
            raise CommandError("invalid", "enabled must be true or false")
        setattr(self.station, attr, data["enabled"])
        self.hub.save_registry()
        _LOGGER.info("[%s] %s %s", self.station.device_id, attr, "on" if data["enabled"] else "off")
        return self.station.info()

    async def _cmd_config_mic_preamp(self, data: dict) -> dict:
        return self._set_flag("mic_preamp", data)

    async def _cmd_config_trim_wake_word(self, data: dict) -> dict:
        return self._set_flag("trim_wake_word", data)

    async def _cmd_config_drop_preroll(self, data: dict) -> dict:
        return self._set_flag("drop_preroll", data)

    async def _cmd_config_include_wake_word(self, data: dict) -> dict:
        info = self._set_flag("include_wake_word", data)
        await self._push(aapi.directive("push_update_config"))  # the stream start point comes from get_sync_info
        return info

    async def _cmd_config_wake_word(self, data: dict) -> dict:
        """Switch the on-device spotter: served as account_config.spotter, applied via push_update_config."""
        wake_word = data["wake_word"]
        if wake_word not in WAKE_WORDS:
            raise CommandError("invalid", f"unsupported wake word {wake_word!r}")
        self.station.wake_word = wake_word
        self.hub.save_registry()
        await self._push(aapi.directive("push_update_config"))
        _LOGGER.info("[%s] wake word set to %s", self.station.device_id, wake_word)
        return self.station.info()

    # Native station timers: the station shows the countdown and rings by itself; it reports nothing back.
    async def _cmd_timer_set(self, data: dict) -> dict:
        try:
            spec = aapi.timer_set(str(data["timer_id"]), int(data["duration"]), title=data.get("title") or "")
        except (KeyError, TypeError, ValueError) as err:
            raise CommandError("invalid", f"bad timer: {err}") from err
        await self._push(spec)
        return {}

    async def _timer_action(self, action: str, data: dict) -> dict:
        if not data.get("timer_id"):
            raise CommandError("invalid", "timer_id is required")
        await self._push(aapi.timer_action(action, str(data["timer_id"])))
        return {}

    async def _cmd_timer_cancel(self, data: dict) -> dict:
        return await self._timer_action("cancel", data)

    async def _cmd_timer_pause(self, data: dict) -> dict:
        return await self._timer_action("pause", data)

    async def _cmd_timer_resume(self, data: dict) -> dict:
        return await self._timer_action("resume", data)

    async def _cmd_timer_stop(self, data: dict) -> dict:
        return await self._timer_action("stop", data)

    async def _cmd_debug_sound(self, data: dict) -> dict:
        """Experiments: play a stored station sound by EStoredSound name."""
        try:
            await self._push(aapi.local_sound(data["sound"]))
        except ValueError as err:
            raise CommandError("invalid", f"unknown sound {data['sound']!r}") from err
        return {}

    async def _cmd_debug_push(self, data: dict) -> dict:
        """Experiments: push raw directives [{name, payload, type?}] without restarting the adapter."""
        await self._push(*(
            aapi.directive(d["name"], d.get("payload"), type_=d.get("type", "client_action"))
            for d in data["directives"]
        ))
        return {}
