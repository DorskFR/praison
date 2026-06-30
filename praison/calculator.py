"""Business logic calculator for workplace rules."""

from calendar import monthrange
from datetime import date, datetime, timedelta

from praison.duration import JST, Duration
from praison.holidays import is_working_day
from praison.models import (
    DayRecord,
    DayType,
    MonthStats,
    PlannedDay,
    ServerSummary,
    WorkEntry,
    WorkplaceType,
)

DEFAULT_HOURS_PER_DAY = 8
DEFAULT_WFH_HOURS_PER_DAY = 1.5  # seniors / team leaders allowance
MANDATORY_BREAK_THRESHOLD = 6 * 60  # minutes
MANDATORY_BREAK_DURATION = 60  # minutes

_COUNTABLE_DAY_TYPES = (
    DayType.WORKING_DAY,
    DayType.UNPAID_LEAVE,
    DayType.PAID_LEAVE,
    DayType.HALF_DAY_PAID_LEAVE,
)


def calculate_month_stats(
    year: int,
    month: int,
    merged_records: list[DayRecord],
    today: date | None = None,
    hours_per_day: int = DEFAULT_HOURS_PER_DAY,
    wfh_hours_per_day: float = DEFAULT_WFH_HOURS_PER_DAY,
    server_summary: ServerSummary | None = None,
    planned_days: list[PlannedDay] | None = None,
) -> tuple[MonthStats, dict[date, int]]:
    """
    Calculate monthly statistics from merged records (actual + planned + auto-defaults).

    Single source of truth for ALL calculations.

    Rules:
    - `hours_per_day` required per working day
    - `wfh_hours_per_day` WFH quota per business day (calendar-based,
      NOT reduced by leave — matches Praise's remote budget computation)
    - Mandatory in-office = total required - WFH quota
    - Paid leave reduces required hours (not a working day)
    - Unpaid leave is a working day (hours still due)

    If `server_summary` is provided, local results are compared against it and
    discrepancies are flagged in the returned stats.

    Args:
        today: If provided, balance will be calculated only for past/today records
    """
    if today is None:
        today = datetime.now(JST).date()

    wfh_quota_per_day_minutes = int(wfh_hours_per_day * 60)

    # When planned_days is provided, today's contribution to the *projected* totals
    # (office/WFH "Planned" and the end-of-month surplus/deficit) is the greater of
    # what has been clocked so far and what is still planned/required for today — so
    # mid-day the projection assumes today's plan will be met rather than freezing at
    # the partial clock. The running balance and "leave at" deliberately keep using
    # the actual clocked-so-far, so they answer "where am I right now".
    planned_by_date = {p.date: p for p in (planned_days or [])}

    working_days = 0
    paid_leave_days: float = 0
    total_office_minutes = 0
    total_wfh_minutes = 0

    # Track daily balances (cumulative, WITH WFH capping)
    daily_balances: dict[date, int] = {}
    running_balance_minutes = 0
    current_balance_minutes = 0

    # WFH quota is based on total business days in the month (calendar-based,
    # not reduced by leave) — count first to know the total quota.
    total_wfh_quota_minutes = sum(
        wfh_quota_per_day_minutes for r in merged_records if r.day_type in _COUNTABLE_DAY_TYPES
    )
    remaining_wfh_quota_minutes = total_wfh_quota_minutes

    for record in merged_records:
        is_past_or_today = record.date <= today

        if record.day_type in _COUNTABLE_DAY_TYPES:
            working_days += 1

        if record.day_type == DayType.PAID_LEAVE:
            paid_leave_days += 1
        elif record.day_type == DayType.HALF_DAY_PAID_LEAVE:
            paid_leave_days += 0.5

        # Aggregate hours (uncapped, for display purposes). For today, project to the
        # plan/default if it exceeds what's been clocked so far (see note above).
        projected_office_minutes = record.office_minutes
        projected_wfh_minutes = record.remote_minutes
        if (
            planned_days is not None
            and record.date == today
            and record.day_type == DayType.WORKING_DAY
        ):
            plan = planned_by_date.get(today)
            if plan is not None and (plan.office_minutes or plan.remote_minutes):
                plan_office, plan_wfh = plan.office_minutes, plan.remote_minutes
            else:
                plan_office, plan_wfh = hours_per_day * 60, 0
            projected_office_minutes = max(projected_office_minutes, plan_office)
            projected_wfh_minutes = max(projected_wfh_minutes, plan_wfh)
        total_office_minutes += projected_office_minutes
        total_wfh_minutes += projected_wfh_minutes

        # Daily balance with WFH capping: only count WFH up to remaining quota
        capped_wfh_minutes = min(record.remote_minutes, remaining_wfh_quota_minutes)
        remaining_wfh_quota_minutes = max(0, remaining_wfh_quota_minutes - record.remote_minutes)

        worked_minutes = record.office_minutes + capped_wfh_minutes
        daily_balance = worked_minutes - record.expected_minutes
        running_balance_minutes += daily_balance
        daily_balances[record.date] = running_balance_minutes

        if is_past_or_today:
            current_balance_minutes = running_balance_minutes

    # Requirements
    actual_working_days = working_days - paid_leave_days
    total_required_hours = actual_working_days * hours_per_day
    wfh_quota_hours = working_days * wfh_hours_per_day
    office_required_hours = max(0.0, total_required_hours - wfh_quota_hours)

    suggested_clockout_time = _suggested_clockout(
        merged_records,
        daily_balances,
        today,
        hours_per_day,
        total_wfh_quota_minutes,
        office_required_minutes=int(office_required_hours * 60),
        server_summary=server_summary,
    )

    discrepancies = []
    if server_summary is not None:
        discrepancies = _find_discrepancies(
            server_summary,
            local_wfh_quota_minutes=total_wfh_quota_minutes,
            local_required_minutes=int(total_required_hours * 60),
        )

    stats = MonthStats(
        year=year,
        month=month,
        working_days=working_days,
        total_required_hours=total_required_hours,
        wfh_quota_hours=wfh_quota_hours,
        office_required_hours=office_required_hours,
        actual_office_hours=total_office_minutes / 60,
        actual_wfh_hours=total_wfh_minutes / 60,
        paid_leave_days=paid_leave_days,
        balance_minutes=current_balance_minutes,
        suggested_clockout_time=suggested_clockout_time,
        discrepancies=discrepancies,
    )

    return stats, daily_balances


def _find_discrepancies(
    summary: ServerSummary,
    local_wfh_quota_minutes: int,
    local_required_minutes: int,
) -> list[str]:
    """Compare local computation against Praise's server summary.

    Always recompute locally; flag anything the server disagrees with so config
    drift (e.g. WFH rate changed in Praise) is visible instead of silent.
    """
    issues = []
    if summary.remote_budget_minutes and summary.remote_budget_minutes != local_wfh_quota_minutes:
        issues.append(
            f"WFH quota mismatch: praise says {Duration(summary.remote_budget_minutes)}, "
            f"local config computes {Duration(local_wfh_quota_minutes)}"
        )
    if summary.required_minutes and summary.required_minutes != local_required_minutes:
        issues.append(
            f"Required hours mismatch: praise says {Duration(summary.required_minutes)}, "
            f"local computes {Duration(local_required_minutes)}"
        )
    return issues


def _suggested_clockout(
    merged_records: list[DayRecord],
    daily_balances: dict[date, int],
    today: date,
    hours_per_day: int,
    total_wfh_quota_minutes: int,
    office_required_minutes: int,
    server_summary: ServerSummary | None = None,
) -> str | None:
    """Clock-out time today that satisfies BOTH the running balance and the office floor.

    The running balance lets WFH (capped to quota) substitute for office on a daily
    basis, so it can read neutral/positive while the *monthly* office requirement is
    still unmet. Office is a hard floor that WFH can never cover, so the leave-at time
    is the later of two constraints: the balance-neutral time, and the time needed to
    clock enough office to reach the office floor (given office already done plus office
    still planned on future days). Only when both are satisfied is it "Done".

    The office floor is measured against Praise's authoritative server summary
    (`on_site_minutes` vs `required_on_site_minutes`) when available — those are the
    numbers shown to the user and the source of truth for actuals; the local
    reconstruction can diverge from them. Only future planned office (which the summary,
    being actuals-only, doesn't include) is added from the local merge.
    """
    today_record = next((r for r in merged_records if r.date == today), None)
    if not today_record or today_record.day_type != DayType.WORKING_DAY:
        return None

    yesterday_balance = daily_balances.get(today - timedelta(days=1), 0)
    minutes_needed_today = hours_per_day * 60 - yesterday_balance

    # Recompute remaining WFH quota as of this morning
    remaining_quota_before_today = total_wfh_quota_minutes
    for record in merged_records:
        if record.date >= today:
            break
        remaining_quota_before_today = max(0, remaining_quota_before_today - record.remote_minutes)

    capped_today_wfh = min(today_record.remote_minutes, remaining_quota_before_today)
    today_worked = today_record.office_minutes + capped_today_wfh
    balance_remaining = minutes_needed_today - today_worked

    # Office-floor constraint: WFH (over quota or not) can never count toward the office
    # requirement, so any office shortfall not covered by future planned office must be
    # clocked in office today.
    if server_summary is not None and server_summary.required_on_site_minutes:
        # Authoritative: office already done (incl. today) per Praise's summary, plus
        # office still planned on future days (the summary is actuals-only).
        office_done = server_summary.on_site_minutes
        future_planned_office = sum(r.office_minutes for r in merged_records if r.date > today)
        office_required = server_summary.required_on_site_minutes
    else:
        office_done = sum(r.office_minutes for r in merged_records if r.date <= today)
        future_planned_office = sum(r.office_minutes for r in merged_records if r.date > today)
        office_required = office_required_minutes
    office_floor_remaining = office_required - office_done - future_planned_office

    remaining_minutes = max(balance_remaining, office_floor_remaining)

    if remaining_minutes <= 0:
        return "Done ✓"
    clockout_time = datetime.now(JST) + timedelta(minutes=remaining_minutes)
    return clockout_time.strftime("%H:%M")


def generate_month_calendar(
    year: int, month: int, hours_per_day: int = DEFAULT_HOURS_PER_DAY
) -> list[DayRecord]:
    """Generate a calendar for the entire month with empty DayRecords."""
    _, days_in_month = monthrange(year, month)
    calendar = []

    for day in range(1, days_in_month + 1):
        target_date = date(year, month, day)

        if is_working_day(target_date):
            day_type = DayType.WORKING_DAY
        elif target_date.weekday() in (5, 6):
            day_type = DayType.WEEKEND
        else:
            day_type = DayType.HOLIDAY

        calendar.append(
            DayRecord(
                date=target_date,
                day_type=day_type,
                entries=[],
                memo="",
                hours_per_day=hours_per_day,
            )
        )

    return calendar


def _planned_entries(planned: PlannedDay) -> list[WorkEntry]:
    entries = []
    if planned.office_minutes > 0:
        entries.append(
            WorkEntry(
                workplace=WorkplaceType.OFFICE,
                clock_in=None,
                clock_out=None,
                duration=Duration(planned.office_minutes),
                category="Planned",
            )
        )
    if planned.remote_minutes > 0:
        entries.append(
            WorkEntry(
                workplace=WorkplaceType.WFH,
                clock_in=None,
                clock_out=None,
                duration=Duration(planned.remote_minutes),
                category="Planned",
            )
        )
    return entries


def _planned_day_type(planned: PlannedDay, default: DayType) -> DayType:
    if planned.is_half_day_leave:
        return DayType.HALF_DAY_PAID_LEAVE
    if planned.is_paid_leave:
        return DayType.PAID_LEAVE
    if planned.is_unpaid_leave:
        return DayType.UNPAID_LEAVE
    return default


def _default_working_entries(hours_per_day: int) -> list[WorkEntry]:
    return [
        WorkEntry(
            workplace=WorkplaceType.OFFICE,
            clock_in=None,
            clock_out=None,
            duration=Duration(hours_per_day * 60),
            category="Planned",
        )
    ]


def merge_actual_and_planned(
    actual_records: list[DayRecord],
    planned_days: list[PlannedDay],
    year: int,
    month: int,
    today: date | None = None,
    hours_per_day: int = DEFAULT_HOURS_PER_DAY,
) -> list[DayRecord]:
    """
    Merge actual records with planned days for display.

    For past dates, use actual records.
    For future dates, create records from planned data or auto-generate defaults.
    Future working days without plans default to `hours_per_day` office work.
    """
    if today is None:
        today = datetime.now(JST).date()

    actual_by_date = {record.date: record for record in actual_records}
    planned_by_date = {planned.date: planned for planned in planned_days}

    result = []
    for day_record in generate_month_calendar(year, month, hours_per_day):
        target_date = day_record.date
        actual = actual_by_date.get(target_date)
        planned = planned_by_date.get(target_date)

        # For past/today: always use actual data if available
        if actual is not None and target_date <= today:
            result.append(actual)
        elif actual is not None:
            # Future date with actual data: use it only if meaningful
            has_meaningful_entries = any(entry.duration.minutes > 0 for entry in actual.entries)
            if has_meaningful_entries or actual.day_type != DayType.WORKING_DAY:
                result.append(actual)
            elif planned is not None:
                result.append(
                    DayRecord(
                        date=target_date,
                        day_type=_planned_day_type(planned, day_record.day_type),
                        entries=_planned_entries(planned),
                        memo=planned.note,
                        hours_per_day=hours_per_day,
                    )
                )
            elif day_record.day_type == DayType.WORKING_DAY:
                result.append(
                    DayRecord(
                        date=target_date,
                        day_type=day_record.day_type,
                        entries=_default_working_entries(hours_per_day),
                        memo="",
                        hours_per_day=hours_per_day,
                    )
                )
            else:
                result.append(actual)
        elif planned is not None:
            result.append(
                DayRecord(
                    date=target_date,
                    day_type=_planned_day_type(planned, day_record.day_type),
                    entries=_planned_entries(planned),
                    memo=planned.note,
                    hours_per_day=hours_per_day,
                )
            )
        elif target_date > today and day_record.day_type == DayType.WORKING_DAY:
            result.append(
                DayRecord(
                    date=target_date,
                    day_type=day_record.day_type,
                    entries=_default_working_entries(hours_per_day),
                    memo="",
                    hours_per_day=hours_per_day,
                )
            )
        else:
            result.append(day_record)

    return result
