"""aiohttp application: station uniproxy endpoint, HA API, zeroconf announcement."""

from __future__ import annotations

import logging
import socket
import uuid

from aiohttp import web
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from . import __version__
from .config import Config
from .backend import Backend
from .ha_api import HAApi
from .hub import Hub
from .session import StationSession

_LOGGER = logging.getLogger(__name__)

ZEROCONF_TYPE = "_quasar-adapter._tcp.local."
CONFIG_KEY = web.AppKey("config", Config)
HUB_KEY = web.AppKey("hub", Hub)


def _adapter_id(cfg: Config) -> str:
    path = cfg.data_dir / "adapter_id"
    if path.exists():
        return path.read_text().strip()
    value = uuid.uuid4().hex[:12]
    path.write_text(value)
    return value


async def handle_uniproxy(request: web.Request) -> web.StreamResponse:
    hub: Hub = request.app[HUB_KEY]
    device_id = request.headers.get("X-UPRX-DEVICE-ID")
    if not device_id:
        raise web.HTTPBadRequest(text="X-UPRX-DEVICE-ID required")
    ws = web.WebSocketResponse(compress=False, max_msg_size=0, heartbeat=30)
    await ws.prepare(request)
    platform = request.headers.get("X-UPRX-DEVICE-MODEL", "")
    firmware = request.headers.get("X-UPRX-FIRMWARE", "")
    priority = request.headers.get("X-UPRX-CONNECTION-PRIORITY", "?")
    session = hub.sessions.get(device_id)
    if session is not None and not session.closed:
        # The station opens another connection while the old one is alive (also mid-turn): keep the session.
        session.attach(ws)
        hub.station_reconnected(session, platform, firmware, request.remote)
        _LOGGER.info("Station %s opened connection #%d from %s (priority %s)", device_id, session.socket_count,
                     request.remote, priority)
    else:
        session = StationSession(hub, hub.get_station(device_id), ws)
        hub.station_connected(session, platform=platform, firmware=firmware, ip=request.remote)
        _LOGGER.info("Station %s connected from %s (priority %s)", device_id, request.remote, priority)
    try:
        await session.read(ws)
    finally:
        if session.detach(ws):
            await session.close()
            hub.station_disconnected(session)
            _LOGGER.info("Station %s disconnected", device_id)
        else:
            _LOGGER.info("Station %s closed a connection, %d left", device_id, session.socket_count)
    return ws




async def handle_sendspin_stream(request: web.Request) -> web.StreamResponse:
    player = request.app[HUB_KEY].sendspin.get(request.match_info["device_id"])
    if player is None:
        raise web.HTTPNotFound()
    return await player.serve(request, request.match_info["token"])


async def _zeroconf_ctx(app: web.Application):
    cfg: Config = app[CONFIG_KEY]
    hub: Hub = app[HUB_KEY]
    host = cfg.advertise_host or hub.local_ip
    info = AsyncServiceInfo(
        ZEROCONF_TYPE,
        f"{cfg.adapter_name} {hub.adapter_id}.{ZEROCONF_TYPE}",
        addresses=[socket.inet_aton(host)],
        port=cfg.port,
        properties={"id": hub.adapter_id, "version": __version__, "api": "1", "name": cfg.adapter_name},
        server=f"quasar-adapter-{hub.adapter_id}.local.",
    )
    zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        await zc.async_register_service(info)
        _LOGGER.info("Announced %s on %s:%d", ZEROCONF_TYPE, host, cfg.port)
    except Exception as err:  # noqa: BLE001 - discovery is optional
        _LOGGER.warning("Zeroconf announcement failed: %s", err)
    yield
    await zc.async_unregister_all_services()
    await zc.async_close()


async def _hub_ctx(app: web.Application):
    hub: Hub = app[HUB_KEY]
    await hub.start()
    yield
    await hub.stop()


def build_app(cfg: Config, *, zeroconf: bool = True) -> web.Application:
    hub = Hub(cfg, _adapter_id(cfg))
    app = web.Application()
    app[CONFIG_KEY] = cfg
    app[HUB_KEY] = hub
    app.router.add_get("/uni.ws", handle_uniproxy)
    app.router.add_get("/{host}/uni.ws", handle_uniproxy)
    app.router.add_get("/ha", HAApi(hub).handle)
    app.router.add_get("/sendspin/{device_id}/{token}.mp3", handle_sendspin_stream)
    # Everything else is the Yandex cloud HTTP API the station expects (quasar backend, oauth, metrics).
    hub.backend = Backend(hub)
    app.router.add_route("*", "/{tail:.*}", hub.backend.handle)
    app.cleanup_ctx.append(_hub_ctx)
    if zeroconf:
        app.cleanup_ctx.append(_zeroconf_ctx)
    return app


def run(cfg: Config) -> None:
    _LOGGER.info("Quasar Adapter %s, HA API token in %s", __version__, cfg.data_dir / "api_token")
    if cfg.token_generated:
        # Printed once, when created: in the HA app the log is the only place a user can read it.
        _LOGGER.warning("Generated new HA API token: %s", cfg.api_token)
    web.run_app(build_app(cfg), host=cfg.host, port=cfg.port, print=None, access_log=None)
