"""AAPI (protobuf uniproxy) framing, parsing and message builders.

Generated protobuf modules live in ``quasar_adapter/_proto`` (built by ``tools/gen_protos.sh`` from quasar-protos,
not committed). They use absolute imports such as ``alice.protos...`` so the directory is added to ``sys.path``.
"""

from __future__ import annotations

import struct
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from google.protobuf import struct_pb2
from google.protobuf.json_format import MessageToDict

_PROTO_DIR = Path(__file__).parent / "_proto"
if str(_PROTO_DIR) not in sys.path:
    sys.path.insert(0, str(_PROTO_DIR))

from alice.protos.api.alicekit.common.device_state import device_state_pb2  # noqa: E402
from alice.protos.api.alicekit.protocol.client import client_message_pb2  # noqa: E402
from alice.protos.api.alicekit.protocol.server import server_message_pb2  # noqa: E402
from alice.protos.endpoint.capabilities.alarm import capability_pb2 as alarm_capability_pb2  # noqa: E402
from alice.protos.endpoint.capabilities.audio_file_player import (  # noqa: E402
    capability_pb2 as audio_file_player_pb2,
)

AAPI_MAGIC = b"AAPI"

TClientMessage = client_message_pb2.TClientMessage
TServerMessage = server_message_pb2.TServerMessage
TDeviceState = device_state_pb2.TDeviceState

# TStreamControl.TActionClose.EReason
CLOSE_OK = 0
CLOSE_INTERNAL_ERROR = 1
CLOSE_STOP_RECORDING = 2
CLOSE_AUDIO_SOURCE_STOPPED = 3
CLOSE_CANCEL = 4
CLOSE_NO_SPEECH = 5


def new_id() -> str:
    return str(uuid.uuid4())


def is_aapi(frame: bytes) -> bool:
    return frame.startswith(AAPI_MAGIC)


def parse_client(frame: bytes) -> Any:
    msg = TClientMessage()
    msg.ParseFromString(frame[len(AAPI_MAGIC):])
    return msg


def parse_server(frame: bytes) -> Any:
    msg = TServerMessage()
    msg.ParseFromString(frame[len(AAPI_MAGIC):])
    return msg


def encode(msg: Any) -> bytes:
    return AAPI_MAGIC + msg.SerializeToString()


def split_stream_frame(frame: bytes) -> tuple[int, bytes]:
    return struct.unpack(">I", frame[:4])[0], frame[4:]


def make_stream_frame(stream_id: int, payload: bytes) -> bytes:
    return struct.pack(">I", stream_id) + payload


def to_dict(msg: Any) -> dict[str, Any]:
    return MessageToDict(msg, preserving_proto_field_name=True)


def parse_device_state(raw: bytes) -> Any | None:
    if not raw:
        return None
    state = TDeviceState()
    try:
        state.ParseFromString(raw)
    except Exception:  # noqa: BLE001 - corrupted/unknown payloads must not kill the session
        return None
    return state


# ---- builders -------------------------------------------------------------------------------------------------


def _event(ref: str | None = None, stream_id: int | None = None) -> Any:
    msg = TServerMessage()
    msg.Event.Header.MessageId = new_id()
    if ref:
        msg.Event.Header.RefMessageId = ref
    if stream_id is not None:
        msg.Event.Header.StreamId = stream_id
    return msg


def synchronize_state_response(ref: str, session_id: str) -> Any:
    msg = _event(ref)
    msg.Event.SynchronizeStateResponse.SessionId = session_id
    return msg


def log_ack(ref: str) -> Any:
    msg = _event(ref)
    msg.Event.LogAck.SetInParent()
    return msg


def request_stat_ack(ref: str) -> Any:
    msg = _event(ref)
    msg.Event.RequestStatAck.SetInParent()
    return msg


def input_start_ack(ref: str) -> Any:
    msg = _event(ref)
    msg.Event.InputStartAck.RequestStartTime = int(time.time() * 1_000_000)
    return msg


def asr_result(ref: str, text: str, end_of_utt: bool) -> Any:
    msg = _event(ref)
    result = msg.Event.AsrResult
    result.EndOfUtt = end_of_utt
    result.ResponseCode = 0
    if text or end_of_utt:
        rec = result.Recognition.add()
        rec.Normalized = text
        rec.Confidence = 1.0
        for word in text.split():
            w = rec.Words.add()
            w.Value = word
            w.Confidence = 1.0
    return msg


def stream_close(stream_id: int, reason: int = CLOSE_OK) -> Any:
    msg = TServerMessage()
    msg.StreamControl.StreamId = stream_id
    msg.StreamControl.MessageId = new_id()
    msg.StreamControl.Close.Reason = reason
    return msg


def directive(
    name: str,
    payload: dict | None = None,
    *,
    type_: str = "client_action",
    led_silent: bool = False,
    directive_type: int | None = None,
    payload_raw: bytes | None = None,
) -> dict:
    """Directive description used by ``fill_directive``; plain dict so callers stay protobuf-free."""
    return {
        "name": name, "payload": payload, "type": type_, "led_silent": led_silent,
        "directive_type": directive_type, "payload_raw": payload_raw,
    }


def fill_directive(target: Any, spec: dict) -> None:
    target.Type = spec["type"]
    target.Name = spec["name"]
    target.IsLedSilent = spec["led_silent"]
    if spec.get("directive_type") is not None:
        target.DirectiveType = spec["directive_type"]
        target.PayloadRaw = spec.get("payload_raw") or b""
    else:
        payload = struct_pb2.Struct()
        payload.update(spec.get("payload") or {})
        target.Payload.CopyFrom(payload)


TTS_PLACEHOLDER = directive("tts_play_placeholder", {"channel": "Dialog"}, led_silent=True)
# Stops what audio_play started, at once. player_pause does not stop it on this firmware (tested on a Midi,
# 2026-11-12): the station plays on until its buffer runs out. player_pause is for the station's own music.
AUDIO_STOP = directive("audio_stop", {"smooth": False})
PLAYER_PAUSE = directive("player_pause", {"smooth": True})

# ---- native timers and alarms (typed directives, NAlice.TAlarmCapability) ----------------------------------------

_Alarm = alarm_capability_pb2.TAlarmCapability
# NAlice.TCapability.EDirectiveType
TIMER_DIRECTIVE_TYPES = {"set": 112, "cancel": 113, "pause": 114, "resume": 115, "stop": 276}
ALARM_DIRECTIVE_TYPES = {"add": 32, "update": 34, "snooze": 116, "stop": 118, "cancel": 132}
WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


def _typed(name: str, directive_type: int, payload: Any) -> dict:
    payload.Name = name
    return directive(name, directive_type=directive_type, payload_raw=payload.SerializeToString())


def timer_set(timer_id: str, duration: int, *, title: str = "", start_ts: int | None = None) -> dict:
    payload = _Alarm.TTimerSetDirective()
    timer = payload.Timer
    timer.TimerId = timer_id
    timer.StartTimestamp = start_ts if start_ts is not None else int(time.time())
    timer.Duration = duration
    timer.Remaining = duration
    timer.Title = title
    return _typed("timer_set_directive", TIMER_DIRECTIVE_TYPES["set"], payload)


def timer_action(action: str, timer_id: str) -> dict:
    """cancel | pause | resume | stop (stop = silence a ringing timer)."""
    message = {"cancel": _Alarm.TTimerCancelDirective, "pause": _Alarm.TTimerPauseDirective,
               "resume": _Alarm.TTimerResumeDirective, "stop": _Alarm.TTimerStopDirective}[action]()
    message.TimerId = timer_id
    return _typed(f"timer_{action}_directive", TIMER_DIRECTIVE_TYPES[action], message)


def alarm_add(alarm_id: str, hours: int, minutes: int, *, weekdays: list[str] | None = None,
              date: tuple[int, int, int] | None = None, timezone: str = "", offset_seconds: int = 0,
              title: str = "") -> dict:
    payload = _Alarm.TAlarmAddDirective()
    alarm = payload.Alarm
    alarm.AlarmId = alarm_id
    alarm.LocalTime.Hours = hours
    alarm.LocalTime.Minutes = minutes
    if timezone:
        alarm.Timezone.TimezoneName = timezone
        alarm.Timezone.OffsetSeconds = offset_seconds
    for day in weekdays or []:
        alarm.ActivationDays.add().Weekday = WEEKDAYS.index(day) + 1
    if date is not None:
        d = alarm.ActivationDays.add().Date
        d.Year, d.Month, d.Day = date
    alarm.Title = title
    alarm.EditTimestamp = int(time.time() * 1000)
    return _typed("alarm_add_directive", ALARM_DIRECTIVE_TYPES["add"], payload)


def alarm_cancel(alarm_id: str) -> dict:
    payload = _Alarm.TAlarmCancelDirective()
    payload.AlarmId = alarm_id
    return _typed("alarm_cancel_directive", ALARM_DIRECTIVE_TYPES["cancel"], payload)


def alarm_stop() -> dict:
    return _typed("alarm_stop_directive", ALARM_DIRECTIVE_TYPES["stop"], _Alarm.TAlarmStopDirective())

LOCAL_AUDIO_FILE_PLAY_TYPE = 47  # NAlice.TCapability.EDirectiveType.LocalAudioFilePlayDirectiveType
CHANNEL_DIALOG = 1  # NAlice.EDirectiveChannel.Dialog
_AudioFilePlayer = audio_file_player_pb2.TAudioFilePlayerCapability


def local_sound(sound: str) -> dict:
    """Play a sound stored on the station (EStoredSound name, e.g. "VinsError", "SpotterCancel").

    maind is built without protobuf reflection, so this directive only works as a typed one (PayloadRaw).
    """
    payload = _AudioFilePlayer.TLocalAudioFilePlayDirective()
    payload.SoundToPlay = _AudioFilePlayer.EStoredSound.Value(sound)
    payload.Channel = CHANNEL_DIALOG
    payload.PlayTimes = 1
    return directive(
        "local_audio_file_play", directive_type=LOCAL_AUDIO_FILE_PLAY_TYPE,
        payload_raw=payload.SerializeToString(),
    )


def alice_response(
    ref: str,
    request_id: str,
    sequence_number: int,
    *,
    text: str | None = None,
    has_voice: bool = False,
    should_listen: bool = False,
    directives: list[dict] | None = None,
) -> Any:
    msg = _event(ref)
    resp = msg.Event.AliceResponse
    resp.Header.RequestId = request_id
    resp.Header.ResponseId = new_id()
    resp.Header.SequenceNumber = sequence_number
    resp.Response.SetInParent()
    if text:
        resp.VoiceResponse.OutputSpeech.Text = text
    resp.VoiceResponse.HasVoiceResponse = has_voice
    resp.VoiceResponse.ShouldListen = should_listen
    all_directives = list(directives or [])
    if has_voice:
        all_directives.insert(0, TTS_PLACEHOLDER)
    for spec in all_directives:
        fill_directive(resp.Response.Directives.add(), spec)
    return msg


def tts_speak(ref: str, stream_id: int) -> Any:
    msg = _event(ref, stream_id)
    msg.Event.TtsSpeak.Format = "audio/opus"
    return msg


def push(directives: list[dict]) -> Any:
    msg = TServerMessage()
    msg.Event.Header.MessageId = new_id()
    msg.Event.Header.Ack = int(time.time() * 1000)
    p = msg.Event.Push
    p.PushIds.append(new_id())
    p.DeduplicationPushId = new_id()
    p.AnalyticsMetaInfo.SetInParent()
    for spec in directives:
        fill_directive(p.Directives.add(), spec)
    return msg
