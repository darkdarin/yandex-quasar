"""Central registry: stations, their uniproxy sessions and connected Home Assistant clients."""

from __future__ import annotations

import asyncio
import json
import functools
import logging
import socket
import struct
from typing import TYPE_CHECKING, Any

import aiohttp

from .config import Config
from .glagol import GlagolClient
from .sendspin import SendspinPlayer
from .station import Station

if TYPE_CHECKING:
    from .backend import Backend
    from .ha_api import HAClient
    from .session import StationSession

_LOGGER = logging.getLogger(__name__)

AUDIO_KIND_MIC_PCM = 0x01
AUDIO_DEFAULTS = 2  # bump when a per-station audio default changes meaning


class Hub:
    def __init__(self, config: Config, adapter_id: str) -> None:
        self.config = config
        self.adapter_id = adapter_id
        self.stations: dict[str, Station] = {}
        self.sessions: dict[str, StationSession] = {}
        self.ha_clients: set[HAClient] = set()
        self.http: aiohttp.ClientSession | None = None
        self.backend: Backend | None = None  # set by build_app; signs glagol tokens
        self.glagol: dict[str, GlagolClient] = {}
        self.sendspin: dict[str, SendspinPlayer] = {}
        self._tasks: set[asyncio.Task] = set()
        self._registry_path = config.data_dir / "stations.json"
        self._load_registry()

    @functools.cached_property
    def local_ip(self) -> str:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 9))  # no packets are sent for UDP connect
            return sock.getsockname()[0]

    async def start(self) -> None:
        self.http = aiohttp.ClientSession()

    async def stop(self) -> None:
        for player in list(self.sendspin.values()):
            await player.stop()
        for client in list(self.glagol.values()):
            await client.stop()
        for session in list(self.sessions.values()):
            await session.close()
        if self.http:
            await self.http.close()

    def track(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- stations -------------------------------------------------------------------------------------------

    def _load_registry(self) -> None:
        if not self._registry_path.exists():
            return
        for item in json.loads(self._registry_path.read_text(encoding="utf-8")):
            self.stations[item["device_id"]] = Station(
                device_id=item["device_id"], platform=item.get("platform", ""),
                firmware=item.get("firmware", ""), name=item.get("name", ""),
                wake_word=item.get("wake_word", "alisa"), mic_preamp=item.get("mic_preamp", True),
                trim_wake_word=item.get("trim_wake_word", True),
                # 0.2.14 dropped the pre-roll by default and saved that; it turned out to be the start of the command,
                # so registries written before AUDIO_DEFAULTS reset it to "keep".
                drop_preroll=item.get("drop_preroll", False) if item.get("audio_defaults") == AUDIO_DEFAULTS else False,
                include_wake_word=item.get("include_wake_word", False),
            )

    def save_registry(self) -> None:
        data = [
            {"device_id": s.device_id, "platform": s.platform, "firmware": s.firmware, "name": s.name,
             "wake_word": s.wake_word, "mic_preamp": s.mic_preamp,
             "trim_wake_word": s.trim_wake_word, "drop_preroll": s.drop_preroll,
             "include_wake_word": s.include_wake_word, "audio_defaults": AUDIO_DEFAULTS}
            for s in self.stations.values()
        ]
        self._registry_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def station_connected(self, session: StationSession, platform: str, firmware: str, ip: str | None) -> Station:
        station = session.station
        is_new = station.device_id not in self.stations
        station.platform = platform or station.platform
        station.firmware = firmware or station.firmware
        override = self.config.stations.get(station.device_id)
        if override and override.name:
            station.name = override.name
        station.state.connection = "online"
        station.state.ip = ip
        self.stations[station.device_id] = station
        old = self.sessions.get(station.device_id)
        self.sessions[station.device_id] = session
        if old is not None and old is not session:
            self.track(asyncio.create_task(old.ws.close()))
        self.save_registry()
        if is_new:
            self.emit(station, "station.added", station.info())
        self.emit_state(station)
        if self.config.glagol_client and self.backend is not None and station.device_id not in self.glagol:
            # Live state (volume buttons) that uniproxy never pushes; follows station.state.ip on reconnects.
            self.glagol[station.device_id] = GlagolClient(self, station)
            self.glagol[station.device_id].start()
        if self.config.sendspin_enabled and station.device_id not in self.sendspin:
            port = self.config.sendspin_port + sorted(self.stations).index(station.device_id)
            player = SendspinPlayer(self, station, port)
            self.sendspin[station.device_id] = player
            self.track(asyncio.create_task(self._start_sendspin(player)))
        return station

    def station_reconnected(self, session: StationSession, platform: str, firmware: str, ip: str | None) -> None:
        """Another connection of an already connected station (StationSession.attach)."""
        station = session.station
        station.platform = platform or station.platform
        station.firmware = firmware or station.firmware
        if ip and ip != station.state.ip:
            station.state.ip = ip
            self.emit_state(station)

    async def _start_sendspin(self, player: SendspinPlayer) -> None:
        try:
            await player.start()
        except Exception:
            _LOGGER.exception("[%s] Sendspin player failed to start", player.station.device_id)
            self.sendspin.pop(player.station.device_id, None)

    def station_disconnected(self, session: StationSession) -> None:
        station = session.station
        if self.sessions.get(station.device_id) is not session:
            return  # replaced by a newer connection
        del self.sessions[station.device_id]
        station.state.connection = "offline"
        station.state.alice_state = "idle"
        self.emit_state(station)

    def get_station(self, device_id: str) -> Station:
        station = self.stations.get(device_id)
        if station is None:
            station = Station(device_id=device_id)
        return station

    # ---- HA side --------------------------------------------------------------------------------------------

    def has_ha_clients(self) -> bool:
        return bool(self.ha_clients)

    def emit(self, station: Station, type_: str, data: dict[str, Any]) -> None:
        message = {"type": type_, "station": station.device_id, "data": data}
        for client in list(self.ha_clients):
            client.send_json_nowait(message)

    def emit_state(self, station: Station) -> None:
        self.emit(station, "station.state", station.state.as_dict())
        if (player := self.sendspin.get(station.device_id)) is not None:
            player.on_station_state()

    def emit_audio(self, turn_seq: int, pcm: bytes) -> None:
        frame = struct.pack(">BI", AUDIO_KIND_MIC_PCM, turn_seq) + pcm
        for client in list(self.ha_clients):
            client.send_bytes_nowait(frame)
