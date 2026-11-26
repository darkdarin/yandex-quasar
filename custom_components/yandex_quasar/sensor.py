"""Connection status and next alarm sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time

from . import YandexQuasarConfigEntry, alarms
from .client import AdapterClient
from .entity import YandexQuasarEntity, setup_station_entities

PARALLEL_UPDATES = 0

STATUS_ONLINE = "online"
STATUS_STATION_OFFLINE = "station_offline"
STATUS_ADAPTER_OFFLINE = "adapter_offline"


async def async_setup_entry(
    hass: HomeAssistant, entry: YandexQuasarConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    setup_station_entities(entry, async_add_entities, lambda client, device_id: [
        YandexQuasarStatusSensor(client, device_id),
        YandexQuasarNextAlarmSensor(client, device_id),
    ])


class YandexQuasarStatusSensor(YandexQuasarEntity, SensorEntity):
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_ONLINE, STATUS_STATION_OFFLINE, STATUS_ADAPTER_OFFLINE]
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: AdapterClient, device_id: str) -> None:
        super().__init__(client, device_id, "status")

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        if not self._client.connected:
            return STATUS_ADAPTER_OFFLINE
        if self.station_state.get("connection") == "online":
            return STATUS_ONLINE
        return STATUS_STATION_OFFLINE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "ip": self.station_state.get("ip"),
            "firmware": self.station.get("firmware"),
            "protocol": self.station.get("protocol"),
            "alice_state": self.station_state.get("alice_state"),
            "adapter_version": self._client.version,
        }


class YandexQuasarNextAlarmSensor(YandexQuasarEntity, SensorEntity):
    """When the station's alarm rings next; every alarm in the attributes."""

    _attr_translation_key = "next_alarm"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, client: AdapterClient, device_id: str) -> None:
        super().__init__(client, device_id, "next_alarm")
        self._unsub_ring: CALLBACK_TYPE | None = None

    @property
    def available(self) -> bool:
        return self._client.connected  # alarms ring on the station even while it is not connected

    @property
    def _upcoming(self) -> list[dict[str, Any]]:
        return alarms.upcoming(self.station_state.get("alarms") or [])

    @property
    def native_value(self) -> datetime | None:
        upcoming = self._upcoming
        return datetime.fromisoformat(upcoming[0]["next_ring"]) if upcoming else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"alarms": self._upcoming}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._cancel_ring)
        self._schedule_ring()

    @callback
    def _handle_update(self) -> None:
        self._schedule_ring()
        super()._handle_update()

    @callback
    def _cancel_ring(self) -> None:
        if self._unsub_ring is not None:
            self._unsub_ring()
            self._unsub_ring = None

    @callback
    def _schedule_ring(self) -> None:
        """Move on to the following alarm once this one has rung."""
        self._cancel_ring()
        if (ring := self.native_value) is not None:
            self._unsub_ring = async_track_point_in_utc_time(self.hass, self._rang, ring)

    @callback
    def _rang(self, _now: datetime) -> None:
        self._unsub_ring = None
        self._schedule_ring()
        self.async_write_ha_state()
