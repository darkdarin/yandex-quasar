"""Yandex cloud backend emulation."""

from __future__ import annotations

import json

import aiohttp
from aiohttp.test_utils import TestServer

from quasar_adapter.config import Config
from quasar_adapter.server import build_app

SEED_SYNC = {
    "account_config": {"spotter": "alisa"},
    "device_config": {"name": "Миди"},
    "system_config": {"uniProxyUrl": "wss://uniproxy.alice.yandex.net/uni.ws", "onlineSpotterEnabled": True},
}
SEED_DEVICES = {"devices": [{"id": "DEV1", "platform": "cucumber",
                             "glagol": {"security": {"server_certificate": "CERT", "server_private_key": "KEY"}}}]}


async def _server(tmp_path, advertise_host="10.0.0.5"):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "sync_info_base.json").write_text(json.dumps(SEED_SYNC))
    (seed / "devices.json").write_text(json.dumps(SEED_DEVICES))
    cfg = Config(data_dir=tmp_path / "data", api_token="t", seed_dir=str(seed), advertise_host=advertise_host,
                 port=8080, account_uid="42")
    cfg.data_dir.mkdir()
    server = TestServer(build_app(cfg, zeroconf=False))
    await server.start_server()
    return server


async def test_sync_info_points_to_adapter_and_blocks_ota(tmp_path):
    server = await _server(tmp_path)
    async with aiohttp.ClientSession() as http:
        async with http.get(server.make_url("/quasar/get_sync_info?device_id=DEV1&platform=cucumber")) as r:
            body = await r.json()
        system = body["config"]["system_config"]
        assert body["status"] == "ok"
        assert system["uniProxyUrl"] == "ws://10.0.0.5:8080/uni.ws"
        assert system["onlineSpotterEnabled"] is False
        assert system["com.yandex.launcher.updaterapp"]["settings"]["allowFirmwareUpdate"] == {"ota": False}
        assert body["config"]["device_config"]["name"] == "Миди"
        assert body["glagol"]["security"]["server_certificate"] == "CERT"

        async with http.get(server.make_url("/check_updates?device_id=DEV1")) as r:
            assert (await r.json())["hasUpdate"] is False
        async with http.post(server.make_url("/register?device_id=DEV1&platform=cucumber")) as r:
            assert (await r.json())["hasCriticalUpdate"] is False
    await server.close()


async def test_sync_info_uniproxy_follows_the_address_the_station_used(tmp_path):
    # No advertise_host: our default-route address may be a WAN or Docker bridge one, so the station is sent to
    # uniproxy at the host:port it has just reached the backend on (e.g. a Docker port mapping 8081 -> 8080).
    server = await _server(tmp_path, advertise_host="")
    async with aiohttp.ClientSession() as http:
        url = server.make_url("/quasar/get_sync_info?device_id=DEV1")
        async with http.get(url, headers={"Host": "192.168.50.1:8081"}) as r:
            body = await r.json()
    assert body["config"]["system_config"]["uniProxyUrl"] == "ws://192.168.50.1:8081/uni.ws"
    await server.close()


async def test_xtoken_grant_echoes_station_token(tmp_path):
    server = await _server(tmp_path)
    async with aiohttp.ClientSession() as http:
        async with http.post(server.make_url("/passport/1/token"),
                             data={"grant_type": "x-token", "access_token": "y0_REAL"}) as r:
            body = await r.json()
        assert body["access_token"] == "y0_REAL" and body["uid"] == 42
        async with http.post(server.make_url("/oauth/token"),
                             data={"grant_type": "authorization_code", "code": "c", "device_id": "DEV1"}) as r:
            body = await r.json()
        assert body["access_token"].startswith("y0_") and body["refresh_token"]
    await server.close()


async def test_glagol_and_misc(tmp_path):
    server = await _server(tmp_path)
    async with aiohttp.ClientSession() as http:
        async with http.get(server.make_url("/quasar/glagol/token?device_id=DEV1&platform=cucumber")) as r:
            token = (await r.json())["token"]
        assert token.count(".") == 2
        async with http.post(server.make_url("/quasar/glagol/check_token"), data=b"anything") as r:
            assert (await r.json())["valid"] is True
        async with http.get(server.make_url("/quasar/glagol/device_list")) as r:
            assert [d["id"] for d in (await r.json())["devices"]] == ["DEV1"]
        async with http.get(server.make_url("/scbh/generate_204")) as r:
            assert r.status == 204
        async with http.post(server.make_url("/rpc/gproxy/push_events_batch"), data=b"\x00") as r:
            assert r.status == 200
        async with http.post(server.make_url("/clck/quasar_metrics/write/batch"), data=b"\x00") as r:
            assert await r.read() == b"\x0a\x02OK"
        async with http.get(server.make_url("/some/unknown")) as r:
            assert (await r.json()) == {"status": "ok"}
    await server.close()


def test_registry_resets_preroll_dropped_by_0_2_14(tmp_path):
    from quasar_adapter.hub import Hub

    (tmp_path / "stations.json").write_text(json.dumps([
        {"device_id": "OLD", "drop_preroll": True},  # 0.2.14 saved its default
        {"device_id": "NEW", "drop_preroll": True, "audio_defaults": 2},  # chosen by the user since
    ]))
    hub = Hub(Config(data_dir=tmp_path, api_token="x"), "adapter")
    assert hub.stations["OLD"].drop_preroll is False
    assert hub.stations["NEW"].drop_preroll is True
