"""Alarm validation and schedule (alarms.py)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quasar_adapter import alarms

CET = timezone(timedelta(hours=1))
NOW = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)  # Thursday, 11:00 CET


def test_alarm_without_days_rings_once_at_the_next_such_time():
    later = alarms.normalize({"time": "11:30", "offset_seconds": 3600}, NOW)
    assert later["date"] == "2026-11-26" and later["weekdays"] == []
    earlier = alarms.normalize({"time": "10:30", "offset_seconds": 3600}, NOW)
    assert earlier["date"] == "2026-11-27"
    assert alarms.next_ring(earlier, NOW) == datetime(2026, 11, 27, 10, 30, tzinfo=CET)


def test_repeating_alarm_rings_on_its_weekdays():
    alarm = alarms.normalize({"time": "07:00", "weekdays": ["monday", "thu"], "offset_seconds": 3600}, NOW)
    assert alarm["weekdays"] == ["mon", "thu"] and alarm["date"] is None
    assert alarms.next_ring(alarm, NOW) == datetime(2026, 11, 30, 7, 0, tzinfo=CET)  # Thursday 7:00 is past
    today = alarms.normalize({"time": "12:00", "weekdays": ["thu"], "offset_seconds": 3600}, NOW)
    assert alarms.next_ring(today, NOW) == datetime(2026, 11, 26, 12, 0, tzinfo=CET)


def test_prune_drops_only_rung_one_off_alarms():
    once = alarms.normalize({"time": "11:30", "offset_seconds": 3600}, NOW)
    weekly = alarms.normalize({"time": "11:30", "weekdays": ["thu"], "offset_seconds": 3600}, NOW)
    after = NOW + timedelta(hours=1)
    assert alarms.prune([once, weekly], after) == [weekly]


@pytest.mark.parametrize("data", [
    {"time": "24:00"},
    {"time": "7"},
    {"time": "07:00", "weekdays": ["someday"]},
    {"time": "07:00", "date": "2026-02-30"},
    {"time": "07:00", "date": "2026-12-01", "weekdays": ["mon"]},
])
def test_bad_alarms_are_rejected(data):
    with pytest.raises(ValueError):
        alarms.normalize(data, NOW)


def test_time_zone_name_follows_daylight_saving():
    weekly = alarms.normalize({"time": "07:00", "weekdays": ["mon"], "timezone": "Europe/Madrid",
                               "offset_seconds": 3600}, NOW)
    summer = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    assert alarms.next_ring(weekly, summer) == datetime(2026, 6, 1, 5, 0, tzinfo=timezone.utc)  # 7:00 CEST
