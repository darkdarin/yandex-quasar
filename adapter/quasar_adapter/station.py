"""Station identity and observable state (what HA sees)."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

PLATFORM_NAMES = {
    "cucumber": "Яндекс Станция Миди",
    "yandexstation_2": "Яндекс Станция 2",
    "yandexmini_2": "Яндекс Станция Мини 2",
    "yandexmicro": "Яндекс Станция Лайт",
    "chiron": "Яндекс Станция Макс",
    "bergamot": "Яндекс Станция Мини 3",
    "plum": "Яндекс Станция Лайт 2",
    "orion": "Яндекс Станция 3",
}

# Spotter models shipped in /system/vendor/quasar/spotter_models/activation/ (yasmina is the Arabic one).
WAKE_WORDS = {"alisa": "Алиса", "yandex": "Яндекс"}

PLAYER_STATES = {
    "Idle": "idle", "Playing": "playing", "Paused": "paused", "Buffering": "buffering",
    "Stopped": "idle", "Finished": "idle", "Unknown": "idle",
}


@dataclass
class PlayerState:
    state: str = "idle"  # idle | playing | paused | buffering
    url: str | None = None
    source: str | None = None  # "sendspin" while Music Assistant streams to the station
    stream_id: str | None = None
    title: str | None = None
    artist: str | None = None
    image: str | None = None
    duration: float | None = None
    position: float = 0.0
    position_ts: float = field(default_factory=time.time)


@dataclass
class StationState:
    connection: str = "offline"  # online | offline
    ip: str | None = None
    alice_state: str = "idle"  # idle | listening | thinking | speaking
    volume: float | None = None  # 0..1
    muted: bool = False
    mic_muted: bool = False
    player: PlayerState = field(default_factory=PlayerState)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Station:
    device_id: str
    platform: str = ""
    firmware: str = ""
    name: str = ""
    protocol: str = "aapi"
    wake_word: str = "alisa"
    # Adapter gain (mic_gain_db) on the audio sent to HA; off = raw station audio for processing in HA.
    mic_preamp: bool = True
    # Cut the end of the wake word ("-са") from the start of the command (SpotterTailTrimmer, spotter_tail_ms).
    trim_wake_word: bool = True
    # The burst sent with SpotterEnd: audio from just before the end of the wake word up to the moment the spotter
    # confirmed it, i.e. the start of a quickly spoken command. Dropping it loses that start.
    drop_preroll: bool = False
    # Start the stream well before the end of the wake word (spotterSubscribePointShiftMs) so HA gets it whole.
    include_wake_word: bool = False
    state: StationState = field(default_factory=StationState)
    # streams the adapter stopped itself (to replace or end them): what the station reports about them
    # afterwards is the stop we made, not the person's pause
    stopped_streams: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.name or PLATFORM_NAMES.get(self.platform, f"Яндекс Станция {self.device_id[-4:]}")

    def capabilities(self) -> list[str]:
        return ["voice", "media"]

    def info(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "platform": self.platform,
            "name": self.display_name,
            "firmware": self.firmware,
            "protocol": self.protocol,
            "capabilities": self.capabilities(),
            "wake_words": [{"id": key, "name": name} for key, name in WAKE_WORDS.items()],
            "active_wake_word": self.wake_word,
            "mic_preamp": self.mic_preamp,
            "trim_wake_word": self.trim_wake_word,
            "drop_preroll": self.drop_preroll,
            "include_wake_word": self.include_wake_word,
            "state": self.state.as_dict(),
        }

    def forget_stream(self) -> None:
        """The adapter stops the current stream itself: what comes about it later is not news."""
        player = self.state.player
        if player.stream_id:
            self.stopped_streams = [*self.stopped_streams, player.stream_id][-8:]
        player.stream_id = None

    def apply_device_state(self, ds: Any) -> bool:
        """Update from a TDeviceState protobuf; return True when something HA cares about changed."""
        before = self.state.as_dict()
        if ds.SoundMaxLevel:
            self.state.volume = round(ds.SoundLevel / ds.SoundMaxLevel, 2)
        self.state.muted = ds.SoundMuted
        self.state.mic_muted = ds.MicsMuted
        stream_id = ds.AudioPlayer.CurrentlyPlaying.StreamId if ds.HasField("AudioPlayer") else ""
        # Callbacks of a replaced track carry the old track's state: never apply it to the current one,
        # nor that of a stream we stopped (its "Paused" is our stop, and its id must not come back).
        replaced = self.state.player.stream_id and stream_id != self.state.player.stream_id
        if stream_id and (replaced or stream_id in self.stopped_streams):
            return before != self.state.as_dict()
        if ds.HasField("AudioPlayer"):
            ap = ds.AudioPlayer
            state_name = ap.DESCRIPTOR.fields_by_name["PlayerState"].enum_type.values_by_number[
                ap.PlayerState
            ].name
            player = self.state.player
            new_state = PLAYER_STATES.get(state_name, "idle")
            # While we keep a pause the station may auto-stop the pipeline; keep reporting "paused".
            if not (player.state == "paused" and new_state == "idle"):
                player.state = new_state
            if ap.CurrentlyPlaying.StreamId:
                player.stream_id = ap.CurrentlyPlaying.StreamId
            player.position = ap.OffsetMs / 1000
            player.position_ts = time.time()
            if ap.DurationMs:
                player.duration = ap.DurationMs / 1000
        return before != self.state.as_dict()
