"""Tests for the Praise timesheet parser."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from praison.models import DayType, WorkplaceType
from praison.parser import (
    build_location_categories,
    parse_summary,
    parse_timesheet,
    parse_timezone,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "timesheet.json").read_text())
JST = ZoneInfo("Asia/Tokyo")

LOCATIONS = {"loc-office": "on_site", "loc-remote": "remote"}

# Fixed "now": 2026-06-03 16:00 JST (open session clocked in 09:00 JST)
NOW = datetime(2026, 6, 3, 16, 0, tzinfo=JST)


def _records():
    return {
        r.date.isoformat(): r
        for r in parse_timesheet(FIXTURE, location_categories=LOCATIONS, now=NOW)
    }


def test_timezone_and_summary() -> None:
    assert str(parse_timezone(FIXTURE)) == "Asia/Tokyo"
    summary = parse_summary(FIXTURE)
    assert summary.required_minutes == 9840
    assert summary.remote_budget_minutes == 1980
    assert summary.on_site_minutes == 1080
    assert summary.has_remote_allowance is True
    assert summary.leave_days_count == 1.5


def test_closed_office_day() -> None:
    record = _records()["2026-06-01"]
    assert record.day_type == DayType.WORKING_DAY
    assert record.office_minutes == 480
    assert record.remote_minutes == 0
    entry = record.entries[0]
    assert entry.workplace == WorkplaceType.OFFICE
    assert str(entry.clock_in) == "09:00"  # 00:00Z -> 09:00 JST
    assert str(entry.clock_out) == "18:00"


def test_split_office_and_remote_sessions() -> None:
    record = _records()["2026-06-02"]
    assert record.office_minutes == 360
    assert record.remote_minutes == 30


def test_open_session_live_minutes_with_mandatory_break() -> None:
    record = _records()["2026-06-03"]
    # Open since 09:00 JST, now 16:00 -> gross 420 >= 360 threshold -> -60 break
    assert record.remote_minutes == 360  # unknown location, name heuristic "Work From Home"
    assert record.office_minutes == 0


def test_full_day_paid_leave() -> None:
    record = _records()["2026-06-04"]
    assert record.day_type == DayType.PAID_LEAVE
    assert record.total_minutes == 0
    assert record.memo == "Annual Paid Leave"


def test_half_day_leave_with_afternoon_work() -> None:
    record = _records()["2026-06-05"]
    assert record.day_type == DayType.HALF_DAY_PAID_LEAVE
    assert record.office_minutes == 240
    assert record.expected_minutes == 240


def test_rest_day_maps_to_weekend() -> None:
    record = _records()["2026-06-06"]  # Saturday
    assert record.day_type == DayType.WEEKEND


def test_unpaid_leave() -> None:
    record = _records()["2026-06-08"]
    assert record.day_type == DayType.UNPAID_LEAVE
    assert record.expected_minutes == 480


def test_build_location_categories() -> None:
    locations = [
        {"id": "a", "name": "Office", "category": "on_site"},
        {"id": "b", "name": "Home", "category": "remote"},
        {"id": "c", "name": "NoCategory"},
    ]
    categories = build_location_categories(locations)
    assert categories == {"a": "on_site", "b": "remote", "c": "on_site"}
