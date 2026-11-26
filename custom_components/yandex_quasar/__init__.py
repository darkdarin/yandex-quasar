"""Yandex Station via Quasar Adapter."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .client import AdapterAuthError, AdapterClient, AdapterConnectionError
from .const import CONF_TOKEN, DOMAIN
from .services import async_setup_services

PLATFORMS = [
    Platform.ASSIST_SATELLITE, Platform.EVENT, Platform.MEDIA_PLAYER, Platform.SELECT, Platform.SENSOR, Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class YandexQuasarData:
    client: AdapterClient


type YandexQuasarConfigEntry = ConfigEntry[YandexQuasarData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: YandexQuasarConfigEntry) -> bool:
    client = AdapterClient(
        hass, async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN]
    )
    try:
        await client.connect()
    except AdapterAuthError as err:
        raise ConfigEntryAuthFailed("Adapter rejected the token") from err
    except AdapterConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Quasar Adapter: {err}") from err

    entry.runtime_data = YandexQuasarData(client)
    client.start(lambda coro, name: entry.async_create_background_task(hass, coro, name))
    entry.async_on_unload(client.subscribe_all_events(_pairing_notifier(hass, client)))
    entry.async_on_unload(client.close)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _pairing_notifier(hass: HomeAssistant, client: AdapterClient):
    """Show the Sendspin pairing code Music Assistant asks for (a new one per attempt)."""

    @callback
    def handle(station: str, kind: str, data: dict) -> None:
        if kind != "sendspin.pairing_code":
            return
        notification_id = f"{DOMAIN}_sendspin_pairing_{station}"
        if code := data.get("code"):
            name = client.stations.get(station, {}).get("name", station)
            persistent_notification.async_create(
                hass,
                f"Music Assistant запрашивает PIN для плеера **{name}**.\n\nВведите код: **{code}**",
                title="Сопряжение Sendspin",
                notification_id=notification_id,
            )
        else:
            persistent_notification.async_dismiss(hass, notification_id)

    return handle


async def async_unload_entry(hass: HomeAssistant, entry: YandexQuasarConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
