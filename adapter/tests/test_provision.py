"""Station provisioning over a fake shell: config redirect, seeds, backups, rollback."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quasar_adapter import provision
from quasar_adapter.provision import CFG_PATH, Provisioner, ProvisionError, extract_user_config, redirect_config

FACTORY_CFG = {
    "common": {"deviceType": "cucumber", "backendUrl": "https://quasar.yandex.net"},
    "aliced": {"uniProxyUrl": "wss://uniproxy.alice.yandex.net/uni.ws"},
    "authd": {"passportUrl": "https://oauth.yandex.ru", "codeUrl": "https://mobileproxy.passport.yandex.net",
              "loginUrl": "https://login.yandex.ru"},
    "teleme3d": {"clickdaemon": {"endpointUri": "https://clck.yandex.net/quasar_metrics/write/batch"}},
    "ntpd": {"timeOverHttpUrl": "http://scbh.yandex.net/generate_204"},
    "fluent-bitd": {"fluent-bit": {"enabled": True}},
}
DEVICES = {"devices": [{"id": "DEV1", "glagol": {"security": {"server_certificate": "x"}}}]}
USER_CONFIG = b"\x00\x01hdr" + json.dumps({"account_config": {}, "system_config": {}}).encode() + b"\x00tail"


class FakeShell:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.commands: list[str] = []
        self.on_restart = lambda: None
        self.restart_works = True  # False: the restart dies with the shell, as it did over ADB before
        self.launcher_pid = 100

    async def run(self, command: str) -> tuple[int, str]:
        self.commands.append(command)
        if "q device-id" in command:
            return 0, "DEV1\n"
        if "version_full" in command:
            return 0, "0.369.1.50\n"
        if "q restart" in command and self.restart_works:
            self.launcher_pid += 1
            self.on_restart()
        if command == provision.LAUNCHER_PID:
            return 0, f"{self.launcher_pid}\n"
        if command == "reboot":
            self.launcher_pid += 1
            self.on_restart()
        if command.startswith("cmp -s"):  # install script: emulate cp
            self.files[CFG_PATH] = self.files[provision.CFG_UPLOAD]
        if "tar -xf" in command:  # cloud backup holds the original user_config.dat
            if provision.CLOUD_BACKUP not in self.files:
                return 1, "no backup"
            self.files[f"{provision.SEED_TMP}/data/user_config.dat"] = self.files[provision.CLOUD_BACKUP]
        if command.startswith("[ -f") and "|| " not in command:
            return (0 if command.split()[2] in self.files else 1), ""
        return 0, ""

    async def read(self, path: str) -> bytes:
        if path not in self.files:
            raise ProvisionError("station_error", path)
        return self.files[path]

    async def write(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def close(self) -> None:
        pass


class FakeBackend:
    def __init__(self) -> None:
        self.seeds: dict[str, object] = {}

    def read_seed(self, name: str):
        return self.seeds.get(name)

    def write_seed(self, name: str, data) -> None:
        self.seeds[name] = data


@pytest.fixture
def hub(tmp_path: Path):
    return SimpleNamespace(
        config=SimpleNamespace(advertise_host="", port=8080, data_dir=tmp_path),
        local_ip="192.168.0.50", sessions={}, backend=FakeBackend(),
    )


@pytest.fixture
def shell(monkeypatch):
    files = {
        CFG_PATH: json.dumps(FACTORY_CFG).encode(),
        provision.DEVICES_JSON: json.dumps(DEVICES).encode(),
        provision.USER_CONFIG: USER_CONFIG,
    }
    fake = FakeShell(files)

    async def open_(self):
        return fake

    monkeypatch.setattr(Provisioner, "_open", open_)
    monkeypatch.setattr(provision, "RECONNECT_SETTLE", 0)
    monkeypatch.setattr(provision, "RECONNECT_TIMEOUT", 0.5)
    monkeypatch.setattr(provision, "RESTART_CHECK", 0.05)
    monkeypatch.setattr(provision, "RESTART_POLL", 0.01)
    return fake


def test_redirect_full_and_voice_only():
    new, changed = redirect_config(FACTORY_CFG, "10.0.0.2", 8080, full=True)
    assert new["aliced"]["uniProxyUrl"] == "ws://10.0.0.2:8080/uni.ws"
    assert new["common"]["backendUrl"] == "http://10.0.0.2:8080/quasar"
    assert new["teleme3d"]["clickdaemon"]["endpointUri"] == "http://10.0.0.2:8080/clck/quasar_metrics/write/batch"
    assert new["ntpd"]["timeOverHttpUrl"] == "http://10.0.0.2:8080/scbh/generate_204"
    assert new["fluent-bitd"]["fluent-bit"]["enabled"] is False
    assert "arpc_client.server_url" not in changed  # key absent in this firmware: left alone
    assert redirect_config(new, "10.0.0.2", 8080, full=True)[1] == []  # idempotent
    voice, changed = redirect_config(FACTORY_CFG, "10.0.0.2", 8080, full=False)
    assert changed == ["aliced.uniProxyUrl"] and voice["common"] == FACTORY_CFG["common"]


def test_extract_user_config():
    assert extract_user_config(USER_CONFIG) == {"account_config": {}, "system_config": {}}


async def test_provision_full(hub, shell):
    shell.on_restart = lambda: hub.sessions.__setitem__("DEV1", object())
    result = await Provisioner(hub, {"method": "ssh", "host": "10.0.0.9", "mode": "full"}).provision()
    assert result["device_id"] == "DEV1" and "common.backendUrl" in result["changed"]
    installed = json.loads(shell.files[CFG_PATH])
    assert installed["common"]["backendUrl"] == "http://192.168.0.50:8080/quasar"
    assert json.loads(shell.files[provision.BACKUP_PREV]) == FACTORY_CFG
    assert any("quasar.cfg.orig" in c for c in shell.commands)
    assert any(provision.CLOUD_BACKUP in c for c in shell.commands)
    assert hub.backend.seeds["devices.json"]["devices"][0]["id"] == "DEV1"
    assert hub.backend.seeds["sync_info_base.json"] == {"account_config": {}, "system_config": {}}


async def test_provision_rolls_back_when_station_does_not_return(hub, shell):
    with pytest.raises(ProvisionError) as err:
        await Provisioner(hub, {"method": "adb", "host": "10.0.0.9", "mode": "voice"}).provision()
    assert err.value.code == "timeout"
    assert json.loads(shell.files[CFG_PATH]) == FACTORY_CFG
    assert sum("q restart" in c for c in shell.commands) == 2
    assert all("start-stop-daemon -S -b" in c for c in shell.commands if "q restart" in c)
    assert "reboot" not in shell.commands  # quasar itself restarted both times


async def test_station_reboots_when_quasar_does_not_restart(hub, shell):
    shell.restart_works = False
    shell.on_restart = lambda: hub.sessions.__setitem__("DEV1", object())
    result = await Provisioner(hub, {"method": "adb", "host": "10.0.0.9", "mode": "voice"}).provision()
    assert shell.commands.count("reboot") == 1
    assert "quasar не перезапустился, станция перезагружается" in result["steps"]


async def test_unsupported_model_is_refused(hub, shell):
    cfg = dict(FACTORY_CFG, common={"deviceType": "plutonium"})
    shell.files[CFG_PATH] = json.dumps(cfg).encode()
    with pytest.raises(ProvisionError) as err:
        await Provisioner(hub, {"method": "ssh", "host": "10.0.0.9"}).provision()
    assert err.value.code == "unsupported"
    assert json.loads(shell.files[CFG_PATH]) == cfg


async def test_restore_puts_factory_config_back(hub, shell):
    shell.files[provision.BACKUP_ORIG] = json.dumps(FACTORY_CFG).encode()
    shell.files[CFG_PATH] = json.dumps(redirect_config(FACTORY_CFG, "x", 1, True)[0]).encode()
    await Provisioner(hub, {"method": "ssh", "host": "10.0.0.9"}).restore()
    assert json.loads(shell.files[CFG_PATH]) == FACTORY_CFG


async def test_seed_prefers_cloud_backup_and_richer_config(hub, shell):
    rich = b"hdr" + json.dumps({"account_config": {"a": 1}, "system_config": {"big": "x" * 100}}).encode()
    shell.files[provision.CLOUD_BACKUP] = rich  # stands in for the tar member
    shell.files[provision.USER_CONFIG] = b"hdr" + json.dumps({"system_config": {}}).encode()  # what we served
    shell.on_restart = lambda: hub.sessions.__setitem__("DEV1", object())
    await Provisioner(hub, {"method": "ssh", "host": "10.0.0.9"}).provision()
    assert hub.backend.seeds["sync_info_base.json"]["system_config"]["big"] == "x" * 100
    hub.backend.seeds["sync_info_base.json"] = {"system_config": {"big": "y" * 500}}
    hub.sessions.clear()
    await Provisioner(hub, {"method": "ssh", "host": "10.0.0.9"}).provision()
    assert hub.backend.seeds["sync_info_base.json"]["system_config"]["big"] == "y" * 500  # richer kept
