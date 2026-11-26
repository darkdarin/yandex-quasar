"""Native station alarms: the adapter's list of them, kept per station and mirrored to the station.

The station rings and shows an alarm by itself and keeps its list in /data/quasar/alarms.dat (across reboots; a rung
one-off alarm stays there disabled), but reports neither the list nor a ring: the adapter's list (stations.json) is
what HA sees, and the adapter keeps time to tell HA that an alarm rang.

An alarm is a dict: ``alarm_id``, ``time`` "HH:MM" (the alarm's own local time), ``weekdays`` (``mon`` … ``sun``,
repeats) or ``date`` "YYYY-MM-DD" (once), ``title``, ``timezone`` (IANA name) and ``offset_seconds`` (its UTC offset).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .aapi import WEEKDAYS

_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(:[0-5]\d)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize(data: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Validate an alarm from HA; without weekdays and date it rings once, at the next such time."""
    match = _TIME.match(str(data.get("time", "")).strip())
    if not match:
        raise ValueError(f"bad time {data.get('time')!r}, expected HH:MM")
    alarm_time = f"{int(match[1]):02d}:{match[2]}"
    weekdays = [str(day).lower()[:3] for day in data.get("weekdays") or []]
    if bad := [day for day in weekdays if day not in WEEKDAYS]:
        raise ValueError(f"bad weekdays {bad}")
    weekdays = sorted(set(weekdays), key=WEEKDAYS.index)
    offset = int(data.get("offset_seconds") or 0)
    if not -14 * 3600 <= offset <= 14 * 3600:
        raise ValueError(f"bad offset_seconds {offset}")
    date = data.get("date") or None
    if date is not None:
        if weekdays:
            raise ValueError("an alarm has weekdays or a date, not both")
        if not _DATE.match(str(date)):
            raise ValueError(f"bad date {date!r}, expected YYYY-MM-DD")
        datetime.strptime(date, "%Y-%m-%d")
    elif not weekdays:
        zone = _zone({"timezone": data.get("timezone"), "offset_seconds": offset})
        local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
        hours, minutes = (int(part) for part in alarm_time.split(":"))
        ring = local_now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if ring <= local_now:
            ring += timedelta(days=1)
        date = ring.date().isoformat()
    return {
        "alarm_id": str(data.get("alarm_id") or uuid.uuid4()),
        "time": alarm_time,
        "weekdays": weekdays,
        "date": date,
        "title": str(data.get("title") or ""),
        "timezone": str(data.get("timezone") or ""),
        "offset_seconds": offset,
    }


def _zone(alarm: dict[str, Any]) -> tzinfo:
    """The alarm's time zone: by name, so a daylight saving change keeps 7:00 at 7:00; else its fixed offset."""
    if alarm.get("timezone"):
        try:
            return ZoneInfo(alarm["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return timezone(timedelta(seconds=int(alarm.get("offset_seconds") or 0)))


def next_ring(alarm: dict[str, Any], now: datetime | None = None) -> datetime | None:
    """When the alarm rings next (aware datetime), None when a one-off alarm is already past."""
    now = now or datetime.now(timezone.utc)
    zone = _zone(alarm)
    hours, minutes = (int(part) for part in alarm["time"].split(":"))
    if alarm.get("date"):
        ring = datetime.strptime(alarm["date"], "%Y-%m-%d").replace(hour=hours, minute=minutes, tzinfo=zone)
        return ring if ring > now else None
    local_now = now.astimezone(zone)
    for days in range(8):
        ring = (local_now + timedelta(days=days)).replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if ring > now and WEEKDAYS[ring.weekday()] in alarm["weekdays"]:
            return ring
    return None


def prune(alarms: list[dict[str, Any]], now: datetime | None = None) -> list[dict[str, Any]]:
    """Drop one-off alarms that have rung; the station forgets them too."""
    return [alarm for alarm in alarms if next_ring(alarm, now) is not None]
