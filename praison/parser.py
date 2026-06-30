"""Parse Praise timesheet JSON into DayRecord objects."""

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from praison.calculator import MANDATORY_BREAK_DURATION, MANDATORY_BREAK_THRESHOLD
from praison.duration import Duration
from praison.models import DayRecord, DayType, ServerSummary, WorkEntry, WorkplaceType

# Praise rest-day types map to our WEEKEND/HOLIDAY buckets
_REST_DAY_TYPES = {"scheduled_rest_day", "statutory_rest_day"}


def parse_timezone(data: dict[str, Any]) -> ZoneInfo:
    """Timezone the timesheet datetimes should be displayed in."""
    return ZoneInfo(data.get("timezone") or "UTC")


def parse_summary(data: dict[str, Any]) -> ServerSummary:
    """Extract Praise's server-computed monthly summary."""
    summary = data.get("summary") or {}
    return ServerSummary(
        required_minutes=int(summary.get("requiredMinutes") or 0),
        required_on_site_minutes=int(summary.get("requiredOnSiteMinutes") or 0),
        remote_budget_minutes=int(summary.get("remoteBudgetMinutes") or 0),
        on_site_minutes=int(summary.get("onSiteMinutes") or 0),
        remote_minutes=int(summary.get("remoteMinutes") or 0),
        has_remote_allowance=bool(summary.get("hasRemoteAllowance")),
        leave_days_count=float(summary.get("leaveDaysCount") or 0),
    )


def parse_timesheet(
    data: dict[str, Any],
    location_categories: dict[str, str] | None = None,
    now: datetime | None = None,
    hours_per_day: int = 8,
) -> list[DayRecord]:
    """Parse the `days` array of a Praise timesheet into DayRecords.

    Praise keeps `dayType == "working_day"` for leave days and signals leave via
    `leaveUnit`/`leaveCategory`; we map those onto our leave day types.

    On-site vs remote is decided by each session's location category
    (`location_categories`: locationId -> "on_site"|"remote"), falling back to a
    name heuristic when the id is unknown.
    """
    tz = parse_timezone(data)
    if now is None:
        now = datetime.now(tz)
    location_categories = location_categories or {}

    records = []
    for day in data.get("days") or []:
        target_date = date.fromisoformat(day["date"])
        day_type = _parse_day_type(day, target_date)
        entries = _parse_entries(day, tz, now, location_categories)
        memo = day.get("leaveType") or ""
        records.append(
            DayRecord(
                date=target_date,
                day_type=day_type,
                entries=entries,
                memo=memo,
                hours_per_day=hours_per_day,
            )
        )
    return records


def _parse_day_type(day: dict[str, Any], target_date: date) -> DayType:
    praise_type = day.get("dayType")
    if praise_type == "holiday":
        return DayType.HOLIDAY
    if praise_type in _REST_DAY_TYPES:
        # Praise rest days that aren't weekend days are still non-working
        return DayType.WEEKEND if target_date.weekday() in (5, 6) else DayType.HOLIDAY

    leave_unit = day.get("leaveUnit")
    leave_category = day.get("leaveCategory")
    if leave_unit == "full_day":
        return DayType.UNPAID_LEAVE if leave_category == "unpaid" else DayType.PAID_LEAVE
    if leave_unit in ("half_day_am", "half_day_pm"):
        return DayType.HALF_DAY_PAID_LEAVE
    return DayType.WORKING_DAY


def _parse_entries(
    day: dict[str, Any],
    tz: ZoneInfo,
    now: datetime,
    location_categories: dict[str, str],
) -> list[WorkEntry]:
    entries = []
    sessions = day.get("sessions") or []
    for session in sessions:
        workplace = _parse_workplace(session, location_categories)
        clock_in_dt = _parse_iso(session.get("clockIn"), tz)
        clock_out_dt = _parse_iso(session.get("clockOut"), tz)
        minutes = _session_work_minutes(session, clock_in_dt, clock_out_dt, now)
        entries.append(
            WorkEntry(
                workplace=workplace,
                clock_in=_to_time_of_day(clock_in_dt),
                clock_out=_to_time_of_day(clock_out_dt),
                duration=Duration(minutes),
                category=str(session.get("locationName") or workplace.value),
            )
        )

    # Praise nets only *recorded* breaks out of session actualWorkMinutes; the
    # mandatory auto-break (1h once the day reaches 6h) shows up solely in the
    # day-level breakMinutes. Deduct the remainder so totals match the server.
    if entries and all(s.get("clockOut") for s in sessions):
        deficit = _day_auto_break_deficit(day, sessions)
        if deficit > 0:
            longest = max(entries, key=lambda e: e.duration.minutes)
            longest.duration = Duration(max(0, longest.duration.minutes - deficit))

    # Day with recorded work but no session detail: fall back to one office entry
    if not entries and day.get("actualWorkMinutes"):
        entries.append(
            WorkEntry(
                workplace=WorkplaceType.OFFICE,
                clock_in=None,
                clock_out=None,
                duration=Duration(int(day["actualWorkMinutes"])),
                category="Work",
            )
        )
    return entries


def _parse_workplace(session: dict[str, Any], location_categories: dict[str, str]) -> WorkplaceType:
    category = location_categories.get(str(session.get("locationId")))
    if category == "remote":
        return WorkplaceType.WFH
    if category == "on_site":
        return WorkplaceType.OFFICE
    # Unknown id: heuristic on location name
    name = str(session.get("locationName") or "").lower()
    if any(token in name for token in ("remote", "wfh", "home")):
        return WorkplaceType.WFH
    return WorkplaceType.OFFICE


def _day_auto_break_deficit(day: dict[str, Any], sessions: list[dict[str, Any]]) -> int:
    """Auto-break minutes Praise deducts at the day level only.

    The day's `breakMinutes` includes the mandatory auto-break
    (`autoBreakApplied`), while each session's `actualWorkMinutes` nets out
    just its recorded breaks. The difference must still be deducted locally.
    """
    day_break = day.get("breakMinutes")
    if day_break is None:
        return 0
    recorded = sum(_session_recorded_break_minutes(s) for s in sessions)
    return max(0, int(day_break) - recorded)


def _session_recorded_break_minutes(session: dict[str, Any]) -> int:
    """Break minutes Praise has recorded against a session.

    Closed sessions carry a computed `breakMinutes`; an open session leaves it
    null but still reports `breakPeriods`, so fall back to summing those.
    """
    recorded = session.get("breakMinutes")
    if recorded is not None:
        return int(recorded)
    return sum(int(bp.get("minutes") or 0) for bp in session.get("breakPeriods") or [])


def _session_work_minutes(
    session: dict[str, Any],
    clock_in: datetime | None,
    clock_out: datetime | None,
    now: datetime,
) -> int:
    """Net work minutes for a session, live for the open session.

    Mirrors Praise's per-session break handling: a recorded break is subtracted
    and suppresses the auto-break; otherwise the mandatory 1h break is deducted
    once this session's gross reaches the 6h threshold.
    """
    if clock_out is not None:
        actual = session.get("actualWorkMinutes")
        if actual is not None:
            return int(actual)
        if clock_in is None:
            return 0
        gross = max(0, int((clock_out - clock_in).total_seconds() / 60))
        return max(0, gross - _session_recorded_break_minutes(session))

    # Open session: measure up to now
    if clock_in is None:
        return 0
    gross = max(0, int((now - clock_in).total_seconds() / 60))
    recorded_break = _session_recorded_break_minutes(session)
    if recorded_break > 0:
        deduction = recorded_break
    elif gross >= MANDATORY_BREAK_THRESHOLD:
        deduction = MANDATORY_BREAK_DURATION
    else:
        deduction = 0
    return max(0, gross - deduction)


def _parse_iso(iso_str: str | None, tz: ZoneInfo) -> datetime | None:
    if not iso_str:
        return None
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone(tz)


def _to_time_of_day(dt: datetime | None) -> Duration | None:
    if dt is None:
        return None
    return Duration(dt.hour * 60 + dt.minute)


def build_location_categories(locations: list[dict[str, Any]]) -> dict[str, str]:
    """Map locationId -> category ("on_site"|"remote") from /api/time/clock/locations."""
    return {
        str(loc["id"]): str(loc.get("category") or "on_site")
        for loc in locations
        if loc.get("id") is not None
    }


def parse_open_session(
    clock_status: dict[str, Any] | None,
    location_categories: dict[str, str],
) -> tuple[str, datetime] | None:
    """The currently-open clock-in session, as (category, started_at), or None.

    Praise's ``/api/time/my-timesheet`` summary counts only *closed* sessions, so a
    session you are actively clocked into is missing from ``onSiteMinutes`` /
    ``remoteMinutes`` until you clock out — while Praise's own dashboard live-counts
    it. We read ``/api/time/clock/status`` so callers can credit that elapsed time,
    matching the dashboard (and reality).
    """
    if not clock_status or not clock_status.get("isClockedIn"):
        return None
    event = clock_status.get("lastEvent") or {}
    if event.get("type") != "clock_in":
        return None
    timestamp = event.get("timestamp")
    if not timestamp:
        return None
    try:
        started_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    category = location_categories.get(str(event.get("locationId")), "on_site")
    return (category, started_at)
