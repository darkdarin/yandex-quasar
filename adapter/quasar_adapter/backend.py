"""Emulation of the Yandex cloud HTTP backends the station talks to (quasar.yandex.net, oauth/passport, metrics, rpc).

Endpoint list comes from the paths compiled into maind 0.369 (see «Протокол станции» in the project Notion). Routing is by path suffix,
so the station may use any base URL prefix (``http://adapter:8080/quasar``, ``.../oauth`` ...).

Token endpoints echo the station's own access token for the x-token grant, so the real Yandex tokens stored on the
station stay valid and the station can be returned to the cloud by restoring quasar.cfg.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import logging
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from aiohttp import web

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

SYNC_INFO_SEED = "sync_info_base.json"  # {account_config, device_config, system_config} from user_config.dat
DEVICES_SEED = "devices.json"  # station's /data/quasar/devices.json (device list cache with glagol certs)
TOKEN_TTL = 365 * 24 * 3600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Backend:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.cfg = hub.config
        self._dir = Path(self.cfg.data_dir) / "backend"
        self._dir.mkdir(parents=True, exist_ok=True)
        key_file = self._dir / "glagol_jwt.key"
        if not key_file.exists():
            key_file.write_bytes(secrets.token_bytes(32))
            key_file.chmod(0o600)
        self._jwt_key = key_file.read_bytes()
        self._unhandled: set[str] = set()
        self._seen: set[str] = set()
        self._routes: list[tuple[str, Any]] = [
            # quasar.yandex.net
            ("/register", self.register),
            ("/check_updates", self.check_updates),
            ("/confirm_update", self.ok),
            ("/allow_update", self.allow_update),
            ("/get_sync_info", self.get_sync_info),
            ("/update_device_state", self.update_device_state),
            ("/set_device_state", self.ok),
            ("/set_device_capability", self.ok),
            ("/glagol/token", self.glagol_token),
            ("/glagol/check_token", self.glagol_check_token),
            ("/glagol/v2.0/check_token", self.glagol_check_token),
            ("/check_token", self.glagol_check_token),
            ("/glagol/device_list", self.device_list),
            ("/glagol/family_device_list", self.device_list),
            ("/push_subscribe", self.push_subscribe),
            ("/get_timezone", self.ok),
            ("/get_user_details", self.ok),
            # oauth.yandex.ru / mobileproxy.passport.yandex.net / login.yandex.ru
            ("/1/token", self.token),
            ("/token", self.token),
            ("/revoke_token", self.ok),
            ("/1/bundle/auth/oauth/code_for_am", self.code_for_am),
            # appmetrica / clck / scbh / rpc.alice.yandex.net
            ("/analytics/startup", self.startup),
            ("/report", self.report),
            ("/quasar_metrics/write/batch", self.clck_batch),
            ("/quasar_metrics/write/json/batch", self.clck_json_batch),
            ("/quasar/write/json/batch", self.clck_json_batch),
            ("/generate_204", self.generate_204),
        ]

    # ---- dispatch -------------------------------------------------------------------------------------------

    async def handle(self, request: web.Request) -> web.StreamResponse:
        path = request.path.rstrip("/") or "/"
        if self.cfg.backend_capture_dir and ("/gproxy/" in path or path.endswith("/set_device_capability")):
            await self._capture(request, path)
        for suffix, handler in self._routes:
            if path == suffix or path.endswith(suffix):
                if path not in self._seen:
                    self._seen.add(path)
                    _LOGGER.info("Backend: first %s %s", request.method, path)
                return await handler(request)
        if path.startswith("/gproxy/") or "/gproxy/" in path:
            return web.Response(status=200, content_type="application/x-protobuf")
        if path not in self._unhandled:
            self._unhandled.add(path)
            _LOGGER.warning("Unhandled backend request %s %s%s (answered status ok)", request.method, request.host,
                            request.path_qs.split("?")[0])
        return web.json_response({"status": "ok"})

    async def _capture(self, request: web.Request, path: str) -> None:
        """Debug: save raw bodies of capability/event reports to find button, mute and volume events."""
        body = await request.read()
        directory = Path(self.cfg.backend_capture_dir)
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{path.strip('/').replace('/', '_')}"
        (directory / f"{name}.bin").write_bytes(body)
        summary = describe_events(body) if "push_events" in path else f"{len(body)} bytes"
        _LOGGER.info("Captured %s %s (%s): %s", request.method, path, request.content_type, summary)

    @staticmethod
    async def _form(request: web.Request) -> dict[str, str]:
        body = (await request.read()).decode(errors="replace")
        values = {k: v[0] for k, v in parse_qs(body).items()}
        values.update(request.query)
        return values

    def _seed(self, name: str) -> Any | None:
        for directory in (self.cfg.seed_dir, self._dir):
            if directory and (path := Path(directory) / name).exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except ValueError as err:
                    _LOGGER.error("Bad seed file %s: %s", path, err)
        return None

    def read_seed(self, name: str) -> Any | None:
        return self._seed(name)

    def write_seed(self, name: str, data: Any) -> None:
        """Seeds captured by provisioning go where a user would put them by hand (the app's config dir)."""
        path = Path(self.cfg.seed_dir or self._dir) / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        path.chmod(0o600)  # devices.json carries the station's glagol keys

    # ---- quasar.yandex.net ----------------------------------------------------------------------------------

    async def ok(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"status": "ok"})

    async def register(self, request: web.Request) -> web.StreamResponse:
        q = request.query
        _LOGGER.info("Station %s registered (platform=%s, firmware=%s)", q.get("device_id"), q.get("platform"),
                     q.get("firmware_version") or q.get("firmware"))
        return web.json_response({"status": "ok", "hasCriticalUpdate": False})

    async def check_updates(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"hasUpdate": False, "critical": False})

    async def allow_update(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"status": "ok", "is_allowed": False})

    async def get_sync_info(self, request: web.Request) -> web.StreamResponse:
        device_id = request.query.get("device_id", "")
        base = self._seed(SYNC_INFO_SEED) or _default_config()
        config = copy.deepcopy(base.get("config", base))
        if station := self.hub.stations.get(device_id):
            config.setdefault("account_config", {})["spotter"] = station.wake_word
        system = config.setdefault("system_config", {})
        # This URL overrides uniProxyUrl from quasar.cfg once the station uses our backend.
        system["uniProxyUrl"] = self.uniproxy_url(request)
        system["onlineSpotterEnabled"] = False
        if station is not None and station.include_wake_word:
            # Start the voice stream this far before the end of the wake word (firmware default: -80 ms).
            recognizer = system.setdefault("voiceDialogSettings", {}).setdefault("recognizer", {})
            recognizer["spotterSubscribePointShiftMs"] = -self.cfg.wake_word_lead_ms
        # LLM agents with tools (e.g. Music Assistant search) often need >15 s, the firmware default.
        system["vinsRequestTimeout"] = self.cfg.vins_request_timeout_ms
        system.setdefault("voiceDialogSettings", {})["vinsRequestTimeout"] = self.cfg.vins_request_timeout_ms
        updater = system.setdefault("com.yandex.launcher.updaterapp", {}).setdefault("settings", {})
        updater["allowFirmwareUpdate"] = {"ota": False}
        updater["allowFirmwareUpdateV2"] = {"ota": False}
        body = {
            "status": "ok",
            "config": config,
            "subscription": {"mode": "transaction"},
            "tags": [],
        }
        if security := self.glagol_security(device_id):
            body["glagol"] = {"security": security}
        return web.json_response(body)

    async def update_device_state(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        return web.json_response({"status": "ok"})

    async def push_subscribe(self, request: web.Request) -> web.StreamResponse:
        return web.json_response({"status": "ok", "sign": secrets.token_hex(16), "ts": int(time.time())})

    def uniproxy_url(self, request: web.Request | None = None) -> str:
        if not self.cfg.advertise_host and request is not None and request.host:
            # The station has just reached us at this host:port, so uniproxy is reachable there too, even when our
            # own idea of the LAN address is wrong (several interfaces, Docker port mapping).
            return f"ws://{request.host}/uni.ws"
        host = self.hub.advertised_host(request.remote if request is not None else None)
        return f"ws://{host}:{self.cfg.port}/uni.ws"

    # ---- glagol ---------------------------------------------------------------------------------------------

    def _devices(self) -> list[dict]:
        seed = self._seed(DEVICES_SEED) or {}
        return seed.get("devices", []) if isinstance(seed, dict) else []

    def glagol_security(self, device_id: str) -> dict | None:
        for device in self._devices():
            if device.get("id") == device_id and device.get("glagol", {}).get("security"):
                return device["glagol"]["security"]
        if not device_id:
            return None
        path = self._dir / f"glagol_{device_id}.json"
        if not path.exists():
            path.write_text(json.dumps(_self_signed_localhost()))
            path.chmod(0o600)
            _LOGGER.info("Generated glagol certificate for %s", device_id)
        return json.loads(path.read_text())

    def mint_glagol_token(self, device_id: str, platform: str) -> str:
        now = int(time.time())
        claims = {"plt": platform, "sub": device_id, "iss": "quasar-backend", "aud": "glagol",
                  "iat": now, "exp": now + 86400}
        head = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        payload = _b64url(json.dumps(claims).encode())
        sig = _b64url(hmac.new(self._jwt_key, f"{head}.{payload}".encode(), hashlib.sha256).digest())
        return f"{head}.{payload}.{sig}"

    async def glagol_token(self, request: web.Request) -> web.StreamResponse:
        token = self.mint_glagol_token(request.query.get("device_id", ""), request.query.get("platform", ""))
        return web.json_response({"status": "ok", "token": token})

    async def glagol_check_token(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        # Tokens of other local clients (e.g. the yandex_station integration) are signed by Yandex and cannot be
        # verified here; accepting them keeps those clients working. Restrict with glagol_accept_any = false.
        valid = self.cfg.glagol_accept_any
        return web.json_response({"status": "ok", "valid": valid, "owner": valid, "guest": False})

    async def device_list(self, request: web.Request) -> web.StreamResponse:
        devices = copy.deepcopy(self._devices())
        known = {d.get("id") for d in devices}
        for station in self.hub.stations.values():
            if station.device_id not in known:
                devices.append({
                    "id": station.device_id, "platform": station.platform, "name": station.display_name,
                    "glagol": {"security": self.glagol_security(station.device_id)},
                    "networkInfo": {}, "tags": [], "guestMode": False, "group": None,
                })
        return web.json_response({"status": "ok", "devices": devices})

    # ---- oauth / passport -----------------------------------------------------------------------------------

    async def token(self, request: web.Request) -> web.StreamResponse:
        form = await self._form(request)
        grant = form.get("grant_type", "")
        _LOGGER.info("OAuth grant %s from %s", grant or "?", request.remote)
        if grant == "x-token" and form.get("access_token"):
            # Echo the station's own token: real credentials on the station stay untouched.
            token = form["access_token"]
        else:
            token = "y0_" + hashlib.sha256((grant + form.get("code", "") + form.get("refresh_token", "")
                                            + form.get("device_id", "")).encode()).hexdigest()
        body: dict[str, Any] = {"access_token": token, "token_type": "bearer", "expires_in": TOKEN_TTL}
        if grant == "authorization_code":
            body["refresh_token"] = "1:" + hashlib.sha256(("refresh" + token).encode()).hexdigest()
        if self.cfg.account_uid:
            body["uid"] = int(self.cfg.account_uid)
        return web.json_response(body)

    async def code_for_am(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        return web.json_response({"status": "ok", "code": secrets.token_hex(8), "expires_in": 600})

    # ---- metrics / connectivity -----------------------------------------------------------------------------

    async def startup(self, request: web.Request) -> web.StreamResponse:
        seed = request.query.get("deviceid", "") + request.query.get("api_key_128", "")
        uid = hashlib.md5(seed.encode()).hexdigest()  # noqa: S324 - not security relevant
        return web.json_response({"device_id": {"hash": str(int(uid[:15], 16))}, "uuid": {"value": uid},
                                  "query_hosts": {"list": {}}})

    async def report(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        return web.json_response({"status": "accepted"})

    async def clck_batch(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        return web.Response(body=b"\x0a\x02OK", content_type="application/x-protobuf")

    async def clck_json_batch(self, request: web.Request) -> web.StreamResponse:
        await request.read()
        return web.json_response([{"ResponseStatus": "OK"}])

    async def generate_204(self, request: web.Request) -> web.StreamResponse:
        return web.Response(status=204)


def describe_events(body: bytes) -> str:
    """Human-readable summary of a TIoTEventsBatchRequest (capability events of the station's endpoints)."""
    from google.protobuf.json_format import MessageToDict

    from . import aapi  # noqa: F401 - puts generated protobuf modules on sys.path
    from alice.protos.api.rpc.iot.events_batch import events_batch_pb2

    batch = events_batch_pb2.TIoTEventsBatchRequest()
    try:
        batch.ParseFromString(body)
    except Exception as err:  # noqa: BLE001
        return f"unparsed ({err})"
    parts = []
    for endpoint in batch.Batch:
        for event in endpoint.CapabilityEvents:
            data = MessageToDict(event, preserving_proto_field_name=True).get("Event", {})
            parts.append(f"{endpoint.EndpointId}:{json.dumps(data, ensure_ascii=False)[:300]}")
    return "; ".join(parts) or "empty batch"


def _default_config() -> dict:
    return {
        "account_config": {"spotter": "alisa", "jingle": False, "smartActivation": True},
        "device_config": {"locale": "ru-RU", "dndMode": {"enabled": False}},
        "system_config": {},
    }


def _self_signed_localhost() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        key, crt = Path(tmp) / "key.pem", Path(tmp) / "crt.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(crt),
             "-days", "3650", "-subj", "/CN=localhost"],
            check=True, capture_output=True,
        )
        return {"server_certificate": crt.read_text(), "server_private_key": key.read_text()}
