"""Sendspin bridge: a stalled station must not grow memory or keep Music Assistant playing into a dead stream."""

from __future__ import annotations

import asyncio
import shutil
from types import SimpleNamespace

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from quasar_adapter import aapi
from quasar_adapter.sendspin import SendspinPlayer, _Encoder
from quasar_adapter.server import HUB_KEY

from .conftest import DEVICE_ID

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
CHUNK = bytes(19_200)  # 100 ms of 48 kHz stereo s16


async def test_encoder_drops_old_audio_when_nobody_reads():
    encoder = _Encoder(FFMPEG, 48000, 2, max_queued=200_000)
    await encoder.start()
    try:
        for _ in range(200):  # 20 s of audio, nobody reads the MP3
            encoder.feed(CHUNK)
            await asyncio.sleep(0)
        await asyncio.sleep(0.3)
        assert encoder._queued <= 200_000 + len(CHUNK)
        assert encoder.dropped > 0
    finally:
        await encoder.stop()


async def test_a_new_request_takes_the_stream_over(tmp_path):
    hub = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)))
    player = SendspinPlayer(hub, SimpleNamespace(device_id=DEVICE_ID), port=0)  # type: ignore[arg-type]
    player._encoder = _Encoder(FFMPEG, 48000, 2)
    await player._encoder.start()
    player._token = "t"

    async def feed():
        while True:
            player._encoder.feed(CHUNK)
            await asyncio.sleep(0.05)

    async def handle(request: web.Request) -> web.StreamResponse:
        return await player.serve(request, "t")

    app = web.Application()
    app.router.add_get("/s", handle)
    server = TestServer(app)
    await server.start_server()
    feeder = asyncio.create_task(feed())
    try:
        async with aiohttp.ClientSession() as http:
            first = await http.get(server.make_url("/s"))
            assert await first.content.read(1000)
            # the station asks again while its first request still hangs: the second one gets the audio
            second = await http.get(server.make_url("/s"))
            assert len(await asyncio.wait_for(second.content.readexactly(4000), 5)) == 4000
            assert player._serving is not None
            rest = await asyncio.wait_for(first.content.read(), 5)  # the first one is closed
            assert len(rest) < 200_000
            second.close()
            first.close()
    finally:
        feeder.cancel()
        await player._encoder.stop()
        await server.close()


async def test_station_stopping_the_stream_by_itself_pauses_music_assistant(adapter, station, ha):
    server, _ = adapter
    hub = server.app[HUB_KEY]
    commands: list[str] = []

    async def group_command(name: str) -> None:
        commands.append(name)

    async def stop() -> None:
        return None

    hub.sendspin[DEVICE_ID] = SimpleNamespace(group_command=group_command, on_station_state=lambda: None, stop=stop)
    player = hub.stations[DEVICE_ID].state.player
    player.state, player.source, player.stream_id = "playing", "sendspin", "s1"

    state = aapi.TDeviceState()
    state.AudioPlayer.PlayerState = state.AudioPlayer.DESCRIPTOR.fields_by_name["PlayerState"].enum_type \
        .values_by_name["Idle"].number
    state.AudioPlayer.CurrentlyPlaying.StreamId = "s1"
    msg = station.text_input_server_action("m1", "some_action", {})
    msg.Event.TextInput.Request.DeviceStateRaw = state.SerializeToString()
    await station.send(msg)

    for _ in range(50):
        if commands:
            break
        await asyncio.sleep(0.05)
    assert commands == ["pause"]
    assert player.state == "paused"
