"""Event entity: a native station alarm rang."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import YandexQuasarConfigEntry
from .client import AdapterClient
from .entity import YandexQuasarEntity, setup_station_entities

PARALLEL_UPDATES = 0

EVENT_ALARM_FIRED = "alarm_fired"


async def async_setup_entry(
    hass: HomeAssistant, entry: YandexQuasarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_station_entities(entry, async_add_entities, lambda client, device_id: [
        YandexQuasarAlarmEvent(client, device_id)
    ])


class YandexQuasarAlarmEvent(YandexQuasarEntity, EventEntity):
    """Fires when a station alarm rings: the adapter keeps time, the station itself tells nobody."""

    _attr_translation_key = "alarm"
    _attr_event_types = [EVENT_ALARM_FIRED]

    def __init__(self, client: AdapterClient, device_id: str) -> None:
        super().__init__(client, device_id, "alarm")

    @property
    def available(self) -> bool:
        return self._client.connected

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._client.subscribe_events(self._device_id, self._on_event))

    @callback
    def _on_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind != "alarm.fired":
            return
        alarm = data.get("alarm") or {}
        self._trigger_event(EVENT_ALARM_FIRED, {
            "alarm_id": alarm.get("alarm_id"),
            "time": alarm.get("time"),
            "title": alarm.get("title") or "",
        })
        self.async_write_ha_state()
