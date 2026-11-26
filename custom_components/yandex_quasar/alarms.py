"""Native station alarms: lookup of the station and the adapter calls shared by services, entities and LLM tools.

The adapter keeps each station's alarm list (``alarms`` in the station state) and mirrors it to the station, which
rings and shows the alarm by itself. An alarm is a dict: ``alarm_id``, ``time`` "HH:MM", ``weekdays`` (``mon`` …
``sun``, repeats) or ``date`` "YYYY-MM-DD" (once), ``title``, ``timezone`` and ``offset_seconds``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .client import AdapterClient, AdapterCommandError, AdapterError
from .const import DOMAIN

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def station_clients(hass: HomeAssistant) -> dict[str, AdapterClient]:
    """Every known station id and the adapter client that serves it."""
    found: dict[str, AdapterClient] = {}
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        client: AdapterClient = entry.runtime_data.client
        for station_id in client.stations:
            found[station_id] = client
    return found


def station_of_device(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    return next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)


def resolve_station(hass: HomeAssistant, device_id: str) -> tuple[AdapterClient, str]:
    station_id = station_of_device(hass, device_id)
    client = station_clients(hass).get(station_id or "")
    if station_id is None or client is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_a_station", translation_placeholders={"device": device_id}
        )
    return client, station_id


async def call(client: AdapterClient, station_id: str, command: str, data: dict[str, Any] | None = None) -> dict:
    try:
        return await client.request(command, station_id, data or {})
    except AdapterCommandError as err:
        if err.code == "invalid":
            raise ServiceValidationError(str(err)) from err
        raise HomeAssistantError(f"Станция не выполнила {command}: {err}") from err
    except AdapterError as err:
        raise HomeAssistantError(f"Нет связи с Quasar Adapter: {err}") from err


def alarm_request(hass: HomeAssistant, time: str, weekdays: list[str] | None = None, date: str | None = None,
                  title: str | None = None, alarm_id: str | None = None) -> dict[str, Any]:
    """Adapter alarm.set data in HA's time zone (the adapter picks the next such time when there is no day)."""
    zone = dt_util.get_default_time_zone()
    offset = dt_util.now(zone).utcoffset() or timedelta()
    data: dict[str, Any] = {
        "time": time[:5],
        "weekdays": [day.lower()[:3] for day in weekdays or []],
        "title": title or "",
        "timezone": str(hass.config.time_zone),
        "offset_seconds": int(offset.total_seconds()),
    }
    if date:
        data["date"] = str(date)
    if alarm_id:
        data["alarm_id"] = alarm_id
    return data


def _zone(alarm: dict[str, Any]) -> tzinfo:
    if alarm.get("timezone") and (zone := dt_util.get_time_zone(alarm["timezone"])) is not None:
        return zone
    return timezone(timedelta(seconds=int(alarm.get("offset_seconds") or 0)))


def next_ring(alarm: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """When the alarm rings next, None when a one-off alarm is already past (same rule as the adapter)."""
    now = now or dt_util.utcnow()
    zone = _zone(alarm)
    hours, minutes = (int(part) for part in alarm["time"].split(":")[:2])
    if alarm.get("date"):
        ring = datetime.strptime(alarm["date"], "%Y-%m-%d").replace(hour=hours, minute=minutes, tzinfo=zone)
        return ring if ring > now else None
    local_now = now.astimezone(zone)
    for days in range(8):
        ring = (local_now + timedelta(days=days)).replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if ring > now and WEEKDAYS[ring.weekday()] in alarm.get("weekdays", []):
            return ring
    return None


def describe(alarm: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """An alarm as HA shows it: what the adapter keeps plus when it rings next."""
    ring = next_ring(alarm, now)
    return {
        "alarm_id": alarm["alarm_id"],
        "time": alarm["time"],
        "weekdays": alarm.get("weekdays") or [],
        "date": alarm.get("date"),
        "title": alarm.get("title") or "",
        "next_ring": dt_util.as_local(ring).isoformat() if ring else None,
    }


def upcoming(alarms: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Alarms that still ring, soonest first."""
    rings = [(ring, alarm) for alarm in alarms if (ring := next_ring(alarm, now)) is not None]
    return [describe(alarm, now) for _ring, alarm in sorted(rings, key=lambda pair: pair[0])]
