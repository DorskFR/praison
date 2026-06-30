"""Tests for the Praise timesheet parser."""

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from praison.models import DayType, WorkplaceType
from praison.parser import (
    build_location_categories,
    parse_open_session,
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


def test_day_level_auto_break_deducted() -> None:
    # Real Praise shape: session actualWorkMinutes is gross (617, no recorded
    # break) while the mandatory 1h auto-break only appears in the day-level
    # breakMinutes. Local total must match Praise's day actualWorkMinutes (557).
    record = _records()["2026-06-09"]
    assert record.office_minutes == 557
    assert record.remote_minutes == 0


def test_build_location_categories() -> None:
    locations = [
        {"id": "a", "name": "Office", "category": "on_site"},
        {"id": "b", "name": "Home", "category": "remote"},
        {"id": "c", "name": "NoCategory"},
    ]
    categories = build_location_categories(locations)
    assert categories == {"a": "on_site", "b": "remote", "c": "on_site"}


def test_parse_open_session_on_site() -> None:
    categories = {"loc-1": "on_site", "loc-2": "remote"}
    status = {
        "isClockedIn": True,
        "lastEvent": {
            "type": "clock_in",
            "timestamp": "2026-06-29T22:18:01.779Z",
            "locationId": "loc-1",
        },
    }
    result = parse_open_session(status, categories)
    assert result is not None
    category, started_at = result
    assert category == "on_site"
    assert started_at == datetime(2026, 6, 29, 22, 18, 1, 779000, tzinfo=ZoneInfo("UTC"))


def test_parse_open_session_unknown_location_defaults_on_site() -> None:
    status = {
        "isClockedIn": True,
        "lastEvent": {"type": "clock_in", "timestamp": "2026-06-29T22:18:01Z", "locationId": "x"},
    }
    result = parse_open_session(status, {})
    assert result is not None
    assert result[0] == "on_site"


def test_parse_open_session_none_when_not_clocked_in() -> None:
    assert parse_open_session({"isClockedIn": False}, {}) is None
    assert parse_open_session(None, {}) is None
    on_break = {"isClockedIn": True, "lastEvent": {"type": "break_start"}}
    assert parse_open_session(on_break, {}) is None
