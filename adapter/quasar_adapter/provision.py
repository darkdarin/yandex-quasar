"""Point a rooted station at this adapter over SSH or ADB, and back to the Yandex cloud.

Everything happens with stock BusyBox tools on the station: the adapter reads ``quasar.cfg`` and the cloud seed
files, edits the JSON itself, uploads the result and restarts quasar. Backups stay on the station's ``/data``:
``quasar.cfg.orig`` (factory, never overwritten), ``quasar.cfg.prev`` (before the last change, used for the
automatic rollback) and ``quasar-cloud-backup.tar`` (cloud authorization).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .hub import Hub

_LOGGER = logging.getLogger(__name__)

QUASAR_DIR = "/system/vendor/quasar"
CFG_PATH = f"{QUASAR_DIR}/quasar.cfg"
BACKUP_ORIG = "/data/quasar.cfg.orig"
BACKUP_PREV = "/data/quasar.cfg.prev"
CFG_UPLOAD = "/data/quasar.cfg.new"
CLOUD_BACKUP = "/data/quasar-cloud-backup.tar"
CLOUD_FILES = "account_storage.dat devices.json registered initialized data/user_config.dat"
DEVICES_JSON = "/data/quasar/devices.json"
USER_CONFIG = "/data/quasar/data/user_config.dat"
SEED_TMP = "/tmp/quasar_adapter_seed"

# Models whose quasar.cfg layout has been checked on a real device (deviceType from quasar.cfg).
SUPPORTED_PLATFORMS = {"cucumber": "Яндекс Станция Миди", "plum": "Яндекс Станция Лайт 2"}

CONNECT_TIMEOUT = 15
RECONNECT_TIMEOUT = 240  # the station re-dials uniproxy with a backoff of up to ~2 min
RECONNECT_SETTLE = 3  # let the old connection drop before looking for a new one
LAUNCHER_PID = "pidof quasar_launcher2"
RESTART_CHECK = 30  # q restart: ~5 s to stop, ~1 s to start; a new launcher pid means it worked
RESTART_POLL = 2
REBOOT_CALL_TIMEOUT = 10


class ProvisionError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


# ---- transports -----------------------------------------------------------------------------------------------


class StationShell(Protocol):
    async def run(self, command: str) -> tuple[int, str]: ...
    async def read(self, path: str) -> bytes: ...
    async def write(self, path: str, data: bytes) -> None: ...
    async def close(self) -> None: ...


class SshShell:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    @classmethod
    async def connect(cls, host: str, port: int, username: str, password: str | None,
                      private_key: str | None) -> SshShell:
        import asyncssh

        keys = [asyncssh.import_private_key(private_key)] if private_key else []
        try:
            conn = await asyncio.wait_for(asyncssh.connect(
                host, port=port, username=username or "root", password=password or "",
                client_keys=keys, known_hosts=None, agent_path=None, preferred_auth=_ssh_auth_order(keys),
            ), CONNECT_TIMEOUT)
        except asyncssh.PermissionDenied as err:
            raise ProvisionError("auth_failed", "SSH: login rejected") from err
        except (OSError, TimeoutError, asyncssh.Error) as err:
            raise ProvisionError("cannot_connect", f"SSH {host}:{port}: {err}") from err
        return cls(conn)

    async def run(self, command: str) -> tuple[int, str]:
        result = await self._conn.run(command, check=False)
        return result.exit_status or 0, str(result.stdout or "") + str(result.stderr or "")

    async def read(self, path: str) -> bytes:
        result = await self._conn.run(f"cat {shlex.quote(path)}", check=False, encoding=None)
        if result.exit_status:
            raise ProvisionError("station_error", f"cannot read {path}")
        return bytes(result.stdout)

    async def write(self, path: str, data: bytes) -> None:
        result = await self._conn.run(f"cat > {shlex.quote(path)}", input=data, check=False, encoding=None)
        if result.exit_status:
            raise ProvisionError("station_error", f"cannot write {path}")

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


def _ssh_auth_order(keys: list) -> str:
    # Rooted stations often run dropbear with an empty root password (-B): "none"/"password" must be tried too.
    return ",".join((["publickey"] if keys else []) + ["none", "password", "keyboard-interactive"])


class AdbShell:
    _RC_MARK = "__qa_rc="

    def __init__(self, device: Any) -> None:
        self._device = device

    @classmethod
    async def connect(cls, host: str, port: int, key_path: Path) -> AdbShell:
        from adb_shell.adb_device_async import AdbDeviceTcpAsync
        from adb_shell.auth.keygen import keygen
        from adb_shell.auth.sign_cryptography import CryptographySigner
        from adb_shell.exceptions import AdbConnectionError, DeviceAuthError

        if not key_path.exists():
            # Our own ADB identity; stations rooted the usual way don't ask for it, others need it accepted once.
            await asyncio.to_thread(keygen, str(key_path))
        signer = await asyncio.to_thread(CryptographySigner, str(key_path))
        device = AdbDeviceTcpAsync(host, port, default_transport_timeout_s=CONNECT_TIMEOUT)
        try:
            await device.connect(rsa_keys=[signer], auth_timeout_s=10)
        except DeviceAuthError as err:
            raise ProvisionError("auth_failed", "ADB: the station did not accept our key") from err
        except (OSError, TimeoutError, AdbConnectionError) as err:
            raise ProvisionError("cannot_connect", f"ADB {host}:{port}: {err}") from err
        return cls(device)

    async def run(self, command: str) -> tuple[int, str]:
        out = await self._device.shell(f"{command}; echo {self._RC_MARK}$?", read_timeout_s=60)
        body, _, rc = out.rpartition(self._RC_MARK)
        return (int(rc.strip()) if rc.strip().isdigit() else 1), body

    async def read(self, path: str) -> bytes:
        buf = io.BytesIO()
        try:
            await self._device.pull(path, buf)
        except Exception as err:  # noqa: BLE001 - adb-shell raises various errors for a missing file
            raise ProvisionError("station_error", f"cannot read {path}: {err}") from err
        return buf.getvalue()

    async def write(self, path: str, data: bytes) -> None:
        await self._device.push(io.BytesIO(data), path, st_mode=0o100644)

    async def close(self) -> None:
        await self._device.close()


# ---- quasar.cfg -----------------------------------------------------------------------------------------------


def redirect_config(cfg: dict, host: str, port: int, full: bool) -> tuple[dict, list[str]]:
    """Return a copy of quasar.cfg pointed at the adapter and the list of keys that were changed."""
    new = json.loads(json.dumps(cfg))
    base = f"http://{host}:{port}"
    changed: list[str] = []

    def put(path: str, value: Any) -> None:
        section, _, key = path.rpartition(".")
        node = new
        for part in section.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict) or key not in node:
            return  # this firmware has no such key: nothing to redirect
        if node[key] != value:
            node[key] = value
            changed.append(path)

    def keep_path(url: str, prefix: str) -> str:
        parts = urlsplit(url if "://" in url else f"https://{url}")
        return f"{base}/{prefix}{parts.path or ''}"

    put("aliced.uniProxyUrl", f"ws://{host}:{port}/uni.ws")
    if not full:
        return new, changed
    put("common.backendUrl", f"{base}/quasar")
    put("authd.passportUrl", f"{base}/oauth")
    put("authd.codeUrl", f"{base}/passport")
    put("authd.loginUrl", f"{base}/login")
    put("arpc_client.server_url", f"{base}/rpc")
    put("teleme3d.appMetricaStartupHost", f"{base}/startup")
    put("teleme3d.appmetricaFallbackHosts", [f"{base}/appmetrica"])
    endpoint = new.get("teleme3d", {}).get("clickdaemon", {}).get("endpointUri")
    if isinstance(endpoint, str) and not endpoint.startswith(base):
        put("teleme3d.clickdaemon.endpointUri", keep_path(endpoint, "clck"))
    time_url = new.get("ntpd", {}).get("timeOverHttpUrl")
    if isinstance(time_url, str) and not time_url.startswith(base):
        put("ntpd.timeOverHttpUrl", keep_path(time_url, "scbh"))
    put("fluent-bitd.fluent-bit.enabled", False)
    return new, changed


def extract_user_config(raw: bytes) -> dict:
    """user_config.dat is a few header bytes followed by the cloud config JSON (and some trailing bytes)."""
    text = raw.decode("utf-8", "replace")
    start = text.find("{")
    if start < 0:
        raise ProvisionError("station_error", "user_config.dat has no JSON")
    config, _ = json.JSONDecoder().raw_decode(text[start:])
    return config


# ---- provisioning ---------------------------------------------------------------------------------------------


class Provisioner:
    """One provisioning job; progress goes to the log and to ``on_step`` (shown in HA)."""

    def __init__(self, hub: Hub, data: dict) -> None:
        self.hub = hub
        self.method = data.get("method", "ssh")
        if self.method not in ("ssh", "adb"):
            raise ProvisionError("invalid", f"unknown method {self.method!r}")
        self.host = str(data.get("host") or "").strip()
        if not self.host:
            raise ProvisionError("invalid", "host is required")
        self.port = int(data.get("port") or (5555 if self.method == "adb" else 22))
        self.username = data.get("username") or "root"
        self.password = data.get("password")
        self.private_key = data.get("private_key") or None
        self.full = data.get("mode", "full") == "full"
        self.allow_untested = bool(data.get("allow_untested"))
        self.steps: list[str] = []

    def _step(self, text: str) -> None:
        self.steps.append(text)
        _LOGGER.info("[provision %s] %s", self.host, text)

    async def _open(self) -> StationShell:
        if self.method == "adb":
            return await AdbShell.connect(self.host, self.port, self.hub.config.data_dir / "adbkey")
        return await SshShell.connect(self.host, self.port, self.username, self.password, self.private_key)

    async def _probe(self, shell: StationShell) -> dict:
        rc, out = await shell.run(f"{QUASAR_DIR}/q device-id 2>/dev/null | sed -n 's/^Device ID: *//p'")
        device_id = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
        cfg_raw = await shell.read(CFG_PATH)
        try:
            cfg = json.loads(cfg_raw)
        except ValueError as err:
            raise ProvisionError("station_error", f"{CFG_PATH} is not valid JSON") from err
        _rc, firmware = await shell.run(f"cat {QUASAR_DIR}/version_full 2>/dev/null")
        platform = str(cfg.get("common", {}).get("deviceType") or cfg.get("deviceType") or "")
        if not device_id:
            raise ProvisionError("station_error", "cannot read the station device id")
        return {"device_id": device_id, "platform": platform, "firmware": firmware.strip(), "cfg": cfg,
                "cfg_raw": cfg_raw}

    async def _install(self, shell: StationShell, data: bytes) -> None:
        await shell.write(CFG_UPLOAD, data)
        script = (
            f"cmp -s {CFG_UPLOAD} {CFG_PATH} && exit 0; "
            f"mount -o remount,rw / && cp {CFG_UPLOAD} {CFG_PATH}; rc=$?; sync; mount -o remount,ro /; "
            f"[ $rc -eq 0 ] && cmp -s {CFG_UPLOAD} {CFG_PATH}"
        )
        rc, out = await shell.run(script)
        if rc:
            raise ProvisionError("station_error", f"cannot install quasar.cfg: {out.strip()[:200]}")

    async def _restart_quasar(self, shell: StationShell) -> None:
        """Restart quasar so it re-reads quasar.cfg; reboot the station if quasar did not come back."""
        _rc, before = await shell.run(LAUNCHER_PID)
        # The restart takes several seconds and must outlive our shell. adbd kills everything left in its session
        # when the shell closes, nohup and setsid included; start-stop-daemon -b escapes it. A plain background job
        # is the fallback for stations without it: that one survives only over SSH.
        restart = f"sleep 1; {QUASAR_DIR}/q restart"
        await shell.run(
            f"if command -v start-stop-daemon >/dev/null 2>&1; "
            f"then start-stop-daemon -S -b -x /bin/sh -- -c {shlex.quote(restart)}; "
            f"else ({restart}) >/dev/null 2>&1 & fi"
        )
        deadline = time.monotonic() + RESTART_CHECK
        while time.monotonic() < deadline:
            await asyncio.sleep(RESTART_POLL)
            _rc, now = await shell.run(LAUNCHER_PID)
            if now.strip() and now.strip() != before.strip():
                self._step("quasar перезапущен")
                return
        self._step("quasar не перезапустился, станция перезагружается")
        try:
            # reboot takes adbd/dropbear down with it: the call may hang or fail, and that is fine.
            await asyncio.wait_for(shell.run("reboot"), REBOOT_CALL_TIMEOUT)
        except Exception as err:  # noqa: BLE001 - the transport breaks while the station goes down
            _LOGGER.debug("[provision %s] reboot call ended with %r", self.host, err)

    def _save_seeds(self, devices_raw: bytes, user_config_raw: bytes) -> None:
        backend = self.hub.backend
        if backend is None:
            return
        devices = json.loads(devices_raw)
        existing = backend.read_seed("devices.json") or {}
        known = {d.get("id"): d for d in existing.get("devices", []) if isinstance(d, dict)}
        for device in devices.get("devices", []):
            known[device.get("id")] = device
        backend.write_seed("devices.json", {**existing, **devices, "devices": list(known.values())})
        config = extract_user_config(user_config_raw)
        current = backend.read_seed("sync_info_base.json")
        # Keep the richer one: a hand-made seed or an earlier capture may hold more than this station has now.
        if current is None or len(json.dumps(config)) > len(json.dumps(current)):
            backend.write_seed("sync_info_base.json", config)
        else:
            self._step("sync_info_base.json уже есть и не беднее, оставлен без изменений")

    async def _cloud_user_config(self, shell: StationShell) -> bytes:
        """The cloud config as it was before unbinding: once unbound, user_config.dat holds what we served."""
        rc, _ = await shell.run(
            f"rm -rf {SEED_TMP} && mkdir -p {SEED_TMP} && tar -xf {CLOUD_BACKUP} -C {SEED_TMP} data/user_config.dat"
        )
        try:
            return await shell.read(f"{SEED_TMP}/data/user_config.dat") if rc == 0 else await shell.read(USER_CONFIG)
        finally:
            await shell.run(f"rm -rf {SEED_TMP}")

    async def provision(self) -> dict:
        host = self.hub.config.advertise_host or self.hub.local_ip
        port = self.hub.config.port
        shell = await self._open()
        try:
            info = await self._probe(shell)
            device_id, platform = info["device_id"], info["platform"]
            self._step(f"станция {platform} {info['firmware']}")
            if platform not in SUPPORTED_PLATFORMS and not self.allow_untested:
                raise ProvisionError("unsupported", f"модель {platform or '?'} не проверена")
            new_cfg, changed = redirect_config(info["cfg"], host, port, self.full)
            await shell.run(f"[ -f {BACKUP_ORIG} ] || cp {CFG_PATH} {BACKUP_ORIG}")
            if self.full:
                await shell.run(f"[ -f {CLOUD_BACKUP} ] || (cd /data/quasar && tar -cf {CLOUD_BACKUP} {CLOUD_FILES})")
                self._save_seeds(await shell.read(DEVICES_JSON), await self._cloud_user_config(shell))
                self._step("данные станции сохранены в адаптере")
            if not changed:
                self._step("станция уже настроена на этот адаптер")
                previous = self.hub.sessions.get(device_id)
                if previous is not None:
                    return {"device_id": device_id, "platform": platform, "changed": [], "steps": self.steps}
            else:
                await shell.write(BACKUP_PREV, info["cfg_raw"])
                await self._install(shell, (json.dumps(new_cfg, ensure_ascii=False, indent=4) + "\n").encode())
                self._step(f"quasar.cfg обновлён: {', '.join(changed)}")
            previous = self.hub.sessions.get(device_id)
            await self._restart_quasar(shell)
            self._step("quasar перезапускается")
        finally:
            await shell.close()

        if await self._wait_reconnect(device_id, previous):
            self._step("станция подключилась к адаптеру")
            return {"device_id": device_id, "platform": platform, "changed": changed, "steps": self.steps}
        if changed:
            await self._rollback()
        raise ProvisionError("timeout", "станция не подключилась к адаптеру, настройки возвращены")

    async def _wait_reconnect(self, device_id: str, previous: Any) -> bool:
        deadline = time.monotonic() + RECONNECT_TIMEOUT
        await asyncio.sleep(RECONNECT_SETTLE)
        while time.monotonic() < deadline:
            session = self.hub.sessions.get(device_id)
            if session is not None and session is not previous:
                return True
            await asyncio.sleep(1)
        return False

    async def _rollback(self) -> None:
        self._step("откат quasar.cfg")
        shell = await self._open()
        try:
            await self._install(shell, await shell.read(BACKUP_PREV))
            await self._restart_quasar(shell)
        finally:
            await shell.close()

    async def restore(self) -> dict:
        """Put the factory quasar.cfg back: the station returns to the Yandex cloud."""
        shell = await self._open()
        try:
            info = await self._probe(shell)
            rc, _ = await shell.run(f"[ -f {BACKUP_ORIG} ]")
            if rc:
                raise ProvisionError("no_backup", f"на станции нет {BACKUP_ORIG}")
            await self._install(shell, await shell.read(BACKUP_ORIG))
            await self._restart_quasar(shell)
            self._step("заводской quasar.cfg восстановлен, quasar перезапускается")
        finally:
            await shell.close()
        return {"device_id": info["device_id"], "steps": self.steps}
