"""Sendspin player bridge: the station becomes a native Music Assistant player.

MA connects to the adapter (server-initiated, mDNS ``_sendspin._tcp``) and streams PCM. The adapter re-encodes it to an
endless MP3 HTTP stream that the station plays via ``audio_play``. Playback control and volume flow both ways.

The station plays through its own buffered HTTP player, so exact multi-room sync is not possible; MA's static playback
delay setting can roughly compensate.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiosendspin.client import ClientListener, PairingSupport, SendspinClient
from aiosendspin.models.core import DeviceInfo, ServerCommandPayload, ServerStatePayload, StreamStartMessage
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, MediaCommand, PlayerCommand, Roles
from aiosendspin.noise import Identity
from aiosendspin.noise.trust_store import FileClientPairingStore

if TYPE_CHECKING:
    from .hub import Hub
    from .station import Station

_LOGGER = logging.getLogger(__name__)

PCM_FORMATS = [
    SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=48000, bit_depth=16),
    SupportedAudioFormat(codec=AudioCodec.PCM, channels=2, sample_rate=44100, bit_depth=16),
]
# ~20 s of 48 kHz stereo PCM: MA sends ahead at most this much.
BUFFER_CAPACITY = 4_000_000
# PCM waiting for ffmpeg while the station does not read the stream (a stalled HTTP connection): beyond this the
# oldest audio is dropped, so a station that stopped listening cannot grow the adapter's memory until it is killed.
MAX_QUEUED = BUFFER_CAPACITY
# A station that takes no audio for this long has stalled: its HTTP request stops holding the stream.
WRITE_TIMEOUT_S = 15.0
MEDIA_COMMANDS = {
    "pause": MediaCommand.PAUSE, "resume": MediaCommand.PLAY, "stop": MediaCommand.STOP,
    "next": MediaCommand.NEXT, "previous": MediaCommand.PREVIOUS,
}


class _Encoder:
    """PCM in, endless MP3 out (ffmpeg). One HTTP reader at a time; PCM waiting for ffmpeg is bounded."""

    def __init__(self, ffmpeg: str, sample_rate: int, channels: int, max_queued: int = MAX_QUEUED) -> None:
        self._args = [
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", str(channels), "-i", "pipe:0",
            "-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3", "-flush_packets", "1", "pipe:1",
        ]
        self._proc: asyncio.subprocess.Process | None = None
        self._chunks: collections.deque[bytes] = collections.deque()
        self._queued = 0
        self._max_queued = max_queued
        self._ready = asyncio.Event()
        self._closed = False
        self._writer: asyncio.Task | None = None
        self.dropped = 0  # bytes of PCM dropped because nobody read the stream

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._writer = asyncio.create_task(self._write_loop())

    def feed(self, pcm: bytes) -> None:
        self._chunks.append(pcm)
        self._queued += len(pcm)
        while self._queued > self._max_queued and len(self._chunks) > 1:
            old = self._chunks.popleft()  # keep the newest audio: the station is not listening anyway
            self._queued -= len(old)
            if not self.dropped:
                _LOGGER.warning("Sendspin stream is not read: dropping audio")
            self.dropped += len(old)
        self._ready.set()

    async def _write_loop(self) -> None:
        assert self._proc and self._proc.stdin
        try:
            while True:
                while not self._chunks:
                    if self._closed:
                        return
                    self._ready.clear()
                    await self._ready.wait()
                chunk = self._chunks.popleft()
                self._queued -= len(chunk)
                self._proc.stdin.write(chunk)
                await self._proc.stdin.drain()  # backpressure: the station reads in real time
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._proc.stdin.close()

    async def read(self, size: int = 8192) -> bytes:
        assert self._proc and self._proc.stdout
        return await self._proc.stdout.read(size)

    async def stop(self) -> None:
        self._closed = True
        self._ready.set()
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
            await self._proc.wait()
        if self._writer:
            self._writer.cancel()


class SendspinPlayer:
    def __init__(self, hub: Hub, station: Station, port: int) -> None:
        self.hub = hub
        self.station = station
        self.port = port
        self._dir = Path(hub.config.data_dir) / "sendspin" / station.device_id
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._encoder: _Encoder | None = None
        self._format: tuple[int, int] = (48000, 2)
        self._token: str | None = None
        self._metadata: dict[str, Any] = {}
        self._reported: tuple[int, bool] | None = None
        self._serving: asyncio.Task | None = None  # the HTTP request the station reads the stream with
        self.active = False  # MA is streaming to this station right now

    # ---- lifecycle ------------------------------------------------------------------------------------------

    def _identity(self) -> Identity:
        path = self._dir / "identity.json"
        if path.exists():
            raw = json.loads(path.read_text())["private_key"]
            return Identity.from_private_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        identity = Identity.generate()
        path.touch(mode=0o600)
        path.write_text(json.dumps({"private_key": identity.private_b64u}))
        return identity

    def _settings(self) -> dict:
        path = self._dir / "settings.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _save_settings(self, **values: Any) -> None:
        path = self._dir / "settings.json"
        path.write_text(json.dumps({**self._settings(), **values}))

    async def start(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        identity = self._identity()
        store = await FileClientPairingStore.open(self._dir / "pairing.json")
        volume = self.station.state.volume
        client = SendspinClient(
            identity=identity,
            client_name=self.station.display_name,
            roles=[Roles.PLAYER, Roles.METADATA, Roles.CONTROLLER],
            pairing_store=store,
            pairing_support=PairingSupport(pairing_code_display=self._show_pairing_code, offer_static_pairing_code=True),
            device_info=DeviceInfo(product_name=self.station.display_name, manufacturer="Yandex",
                                   software_version=self.station.firmware or None),
            player_support=ClientHelloPlayerSupport(supported_formats=PCM_FORMATS, buffer_capacity=BUFFER_CAPACITY),
            output_delay_ms=float(self._settings().get("output_delay_ms", 0.0)),
            state_supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE, PlayerCommand.SET_OUTPUT_DELAY],
            initial_volume=int(round((volume if volume is not None else 0.5) * 100)),
            initial_muted=self.station.state.muted,
        )
        client.add_stream_start_listener(self._on_stream_start)
        client.add_audio_chunk_listener(self._on_audio_chunk)
        client.add_stream_clear_listener(self._on_stream_clear)
        client.add_stream_end_listener(self._on_stream_end)
        client.add_metadata_listener(self._on_metadata)
        client.add_server_command_listener(self._on_server_command)
        client.add_output_delay_listener(lambda delay_ms: self._save_settings(output_delay_ms=delay_ms))
        client.add_disconnect_listener(lambda: self._spawn(self._stop_stream(pause_station=True)))
        self._client = client
        self._listener = ClientListener(
            client_id=identity.peer_id, on_connection=self._on_connection, port=self.port,
            client_name=self.station.display_name,
        )
        await self._listener.start()
        _LOGGER.info("[%s] Sendspin player listening on port %d (mDNS _sendspin._tcp)", self.station.device_id,
                     self.port)

    async def stop(self) -> None:
        await self._stop_stream(pause_station=False)
        if self._client:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        if self._listener:
            await self._listener.stop()

    async def _show_pairing_code(self, pairing_code: str | None, *, grouped: str | None) -> None:
        # A new code per pairing attempt; None when the attempt ends. HA shows it as a notification.
        if grouped:
            _LOGGER.warning("[%s] Sendspin pairing required: enter code %s in Music Assistant",
                            self.station.device_id, grouped)
        self.hub.emit(self.station, "sendspin.pairing_code", {"code": grouped})

    async def _on_connection(self, ws: web.WebSocketResponse) -> None:
        assert self._client is not None
        _LOGGER.info("[%s] Music Assistant connected over Sendspin", self.station.device_id)
        try:
            await self._client.attach_websocket(ws)
        except Exception as err:  # noqa: BLE001 - handshake/admission failures are expected on retries
            _LOGGER.info("[%s] Sendspin connection ended: %s", self.station.device_id, err)

    def _spawn(self, coro) -> None:
        self.hub.track(asyncio.get_running_loop().create_task(coro))

    # ---- audio ----------------------------------------------------------------------------------------------

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        player = message.payload.player
        if player is None:
            return
        self._format = (player.sample_rate, player.channels)
        self._spawn(self._start_stream())

    async def _start_stream(self) -> None:
        if self.active and (session := self.hub.sessions.get(self.station.device_id)):
            # A new track or a seek replaces the stream: stopping the old one makes the station report
            # it stopped before the new one plays, which must not read as the person's pause.
            session.forget_stream()
        await self._stop_stream(pause_station=False)
        self._encoder = _Encoder(self.hub.config.ffmpeg, *self._format)
        await self._encoder.start()
        self._token = uuid.uuid4().hex
        self.active = True
        session = self.hub.sessions.get(self.station.device_id)
        if session is None:
            return
        url = f"http://{self.hub.config.advertise_host or self.hub.local_ip}:{self.hub.config.port}" \
              f"/sendspin/{self.station.device_id}/{self._token}.mp3"
        await session.play_stream(url, self._metadata, source="sendspin")

    def _on_audio_chunk(self, server_ts: int, data: bytes, audio_format: Any, send_ahead: int) -> None:
        if self._encoder is not None:
            self._encoder.feed(data)

    def _on_stream_clear(self, roles: list[str] | None) -> None:
        if self.active and (roles is None or "player" in roles):
            # Seek / skip: the station must drop its buffer, so restart the HTTP stream.
            self._spawn(self._start_stream())

    def _on_stream_end(self, roles: list[str] | None) -> None:
        if roles is None or "player" in roles:
            self._spawn(self._stop_stream(pause_station=True))

    async def _stop_stream(self, *, pause_station: bool) -> None:
        was_active = self.active
        self.active = False
        self._token = None
        if self._encoder is not None:
            await self._encoder.stop()
            self._encoder = None
        if pause_station and was_active and (session := self.hub.sessions.get(self.station.device_id)):
            await session.stop_stream()

    async def serve(self, request: web.Request, token: str) -> web.StreamResponse:
        if token != self._token or self._encoder is None:
            raise web.HTTPNotFound()
        # The station asks again after a network hiccup while its old request may still hang: the new request
        # takes the stream over (the encoder has one output, two readers cannot share it).
        task = asyncio.current_task()
        previous, self._serving = self._serving, task
        if previous is not None and previous is not task and not previous.done():
            _LOGGER.info("[%s] the station asked for the stream again: dropping its old request",
                         self.station.device_id)
            previous.cancel()
            await asyncio.wait({previous}, timeout=2)
        encoder = self._encoder
        response = web.StreamResponse(headers={"Content-Type": "audio/mpeg", "Cache-Control": "no-cache"})
        await response.prepare(request)
        try:
            while encoder is self._encoder and self._serving is task and (data := await encoder.read()):
                await asyncio.wait_for(response.write(data), WRITE_TIMEOUT_S)
        except TimeoutError:
            _LOGGER.warning("[%s] the station stopped reading the stream", self.station.device_id)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            if self._serving is task:
                self._serving = None
        return response

    # ---- metadata, commands, volume -------------------------------------------------------------------------

    def _on_metadata(self, state: ServerStatePayload | None) -> None:
        metadata = getattr(state, "metadata", None) if state is not None else None
        if metadata is None or not hasattr(metadata, "title"):
            return
        self._metadata = {
            "title": metadata.title, "artist": metadata.artist, "image": metadata.artwork_url,
            "duration": (metadata.progress.track_duration / 1000)
            if getattr(metadata, "progress", None) and getattr(metadata.progress, "track_duration", None) else None,
        }
        if self.active:
            player = self.station.state.player
            player.title, player.artist, player.image = (self._metadata["title"], self._metadata["artist"],
                                                         self._metadata["image"])
            player.duration = self._metadata["duration"]
            self.hub.emit_state(self.station)

    def _on_server_command(self, payload: ServerCommandPayload) -> None:
        command = payload.player
        session = self.hub.sessions.get(self.station.device_id)
        if command is None or session is None:
            return
        if command.command == PlayerCommand.VOLUME and command.volume is not None:
            self._reported = (command.volume, self.station.state.muted)
            self._spawn(session.command("volume.set", {"level": command.volume / 100}))
        elif command.command == PlayerCommand.MUTE and command.mute is not None:
            self._spawn(session.command("volume.mute", {"muted": command.mute}))

    def on_station_state(self) -> None:
        """Report volume changes made on the station (buttons, voice, HA) back to Music Assistant."""
        if self._client is None or not self._client.connected or self.station.state.volume is None:
            return
        state = (int(round(self.station.state.volume * 100)), self.station.state.muted)
        if state == self._reported:
            return
        self._reported = state
        self._spawn(self._client.send_player_state(available=True, volume=state[0], muted=state[1]))

    async def group_command(self, name: str) -> None:
        if self._client is None or not self._client.connected:
            raise RuntimeError("Music Assistant is not connected")
        await self._client.send_group_command(MEDIA_COMMANDS[name])
