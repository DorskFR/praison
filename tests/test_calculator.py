"""Tests for the workplace rules calculator."""

from datetime import date, timedelta

from praison.calculator import (
    _suggested_clockout,
    calculate_month_stats,
    generate_month_calendar,
    merge_actual_and_planned,
)
from praison.duration import Duration
from praison.models import (
    DayRecord,
    DayType,
    PlannedDay,
    ServerSummary,
    WorkEntry,
    WorkplaceType,
)

YEAR, MONTH = 2026, 6  # June 2026: 22 working days, no JP holiday


def _work_day(day: int, office_minutes: int = 0, remote_minutes: int = 0) -> DayRecord:
    entries = []
    if office_minutes:
        entries.append(
            WorkEntry(WorkplaceType.OFFICE, None, None, Duration(office_minutes), "Work")
        )
    if remote_minutes:
        entries.append(WorkEntry(WorkplaceType.WFH, None, None, Duration(remote_minutes), "Work"))
    return DayRecord(date=date(YEAR, MONTH, day), day_type=DayType.WORKING_DAY, entries=entries)


def test_generate_month_calendar() -> None:
    calendar = generate_month_calendar(YEAR, MONTH)
    assert len(calendar) == 30
    working = [r for r in calendar if r.day_type == DayType.WORKING_DAY]
    assert len(working) == 22
    assert calendar[5].day_type == DayType.WEEKEND  # June 6 2026 is a Saturday


def test_basic_stats_with_default_wfh_rate() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=date(YEAR, MONTH, 1))
    assert stats.working_days == 22
    assert stats.total_required_hours == 22 * 8
    # 1.5h per business day
    assert stats.wfh_quota_hours == 22 * 1.5
    assert stats.office_required_hours == 22 * 8 - 22 * 1.5


def test_wfh_rate_is_configurable() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    stats, _ = calculate_month_stats(
        YEAR, MONTH, records, today=date(YEAR, MONTH, 1), wfh_hours_per_day=1.0
    )
    assert stats.wfh_quota_hours == 22.0
    assert stats.office_required_hours == 22 * 8 - 22


def test_balance_caps_wfh_at_quota() -> None:
    # Quota 22 * 90min = 1980 min. WFH 8h/day burns through it.
    records = generate_month_calendar(YEAR, MONTH)
    wfh_days = [d for d in records if d.day_type == DayType.WORKING_DAY][:5]
    for record in wfh_days:
        record.entries.append(WorkEntry(WorkplaceType.WFH, None, None, Duration(480), "Work"))
    stats, balances = calculate_month_stats(YEAR, MONTH, records, today=date(YEAR, MONTH, 30))
    # 5 days x 8h WFH = 2400 min, capped at 1980 quota
    last_day = max(balances)
    # expected: total required = 22*480; worked counted = min(2400, 1980)
    assert balances[last_day] == 1980 - 22 * 480
    assert stats.actual_wfh_hours == 40.0
    assert stats.wfh_over_quota == 40.0 - 33.0


def test_paid_leave_reduces_required_but_not_quota() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    working = [r for r in records if r.day_type == DayType.WORKING_DAY]
    working[0].day_type = DayType.PAID_LEAVE
    working[1].day_type = DayType.HALF_DAY_PAID_LEAVE
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=date(YEAR, MONTH, 1))
    assert stats.paid_leave_days == 1.5
    assert stats.total_required_hours == (22 - 1.5) * 8
    # quota stays calendar-based
    assert stats.wfh_quota_hours == 22 * 1.5


def test_unpaid_leave_keeps_hours_due() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    working = [r for r in records if r.day_type == DayType.WORKING_DAY]
    working[0].day_type = DayType.UNPAID_LEAVE
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=date(YEAR, MONTH, 1))
    assert stats.paid_leave_days == 0
    assert stats.total_required_hours == 22 * 8


def test_half_day_leave_expects_half_day_work() -> None:
    record = _work_day(1, office_minutes=240)
    record.day_type = DayType.HALF_DAY_PAID_LEAVE
    _, balances = calculate_month_stats(YEAR, MONTH, [record], today=date(YEAR, MONTH, 1))
    assert balances[date(YEAR, MONTH, 1)] == 0  # 4h worked vs 4h expected


def test_merge_planned_overrides_default() -> None:
    today = date(YEAR, MONTH, 5)
    planned = [
        PlannedDay(date=date(YEAR, MONTH, 8), office_minutes=0, remote_minutes=480),
        PlannedDay(
            date=date(YEAR, MONTH, 9), office_minutes=0, remote_minutes=0, is_paid_leave=True
        ),
        PlannedDay(
            date=date(YEAR, MONTH, 10),
            office_minutes=240,
            remote_minutes=0,
            is_paid_leave=True,
            is_half_day_leave=True,
        ),
    ]
    merged = merge_actual_and_planned([], planned, YEAR, MONTH, today=today)
    by_date = {r.date: r for r in merged}
    assert by_date[date(YEAR, MONTH, 8)].remote_minutes == 480
    assert by_date[date(YEAR, MONTH, 9)].day_type == DayType.PAID_LEAVE
    assert by_date[date(YEAR, MONTH, 10)].day_type == DayType.HALF_DAY_PAID_LEAVE
    # Unplanned future working day defaults to 8h office
    assert by_date[date(YEAR, MONTH, 11)].office_minutes == 480
    # Weekend stays empty
    assert by_date[date(YEAR, MONTH, 6)].total_minutes == 0


def test_merge_uses_actual_for_past() -> None:
    today = date(YEAR, MONTH, 5)
    actual = [_work_day(1, office_minutes=480), _work_day(2, remote_minutes=300)]
    merged = merge_actual_and_planned(actual, [], YEAR, MONTH, today=today)
    by_date = {r.date: r for r in merged}
    assert by_date[date(YEAR, MONTH, 1)].office_minutes == 480
    assert by_date[date(YEAR, MONTH, 2)].remote_minutes == 300


def test_full_month_plan_reaches_neutral_eom() -> None:
    # Plan every working day: 6.5h office + 1.5h WFH = 8h -> EoM balance 0
    today = date(YEAR, MONTH, 1)
    planned = [
        PlannedDay(date=r.date, office_minutes=390, remote_minutes=90)
        for r in generate_month_calendar(YEAR, MONTH)
        if r.day_type == DayType.WORKING_DAY
    ]
    merged = merge_actual_and_planned([], planned, YEAR, MONTH, today=date(YEAR, 5, 31))
    stats, balances = calculate_month_stats(YEAR, MONTH, merged, today=today)
    assert balances[max(balances)] == 0
    assert stats.total_deficit == 0
    assert stats.wfh_over_quota == 0


def test_today_projects_to_default_for_planned_totals() -> None:
    # Today: clocked only 3h office so far, no explicit plan -> projected totals
    # assume the 8h working day, but the running balance reflects actual 3h.
    today = date(YEAR, MONTH, 1)
    records = [_work_day(1, office_minutes=180)]
    stats, balances = calculate_month_stats(YEAR, MONTH, records, today=today, planned_days=[])
    # Projected "Planned" office total = 8h, not the 3h clocked
    assert stats.total_office_hours == 8.0
    # Balance to date stays on actual: 3h worked - 8h expected = -5h
    assert balances[today] == -300


def test_today_projects_to_plan_split() -> None:
    # Today clocked 1h office; an explicit plan splits 6.5h office + 1.5h WFH.
    today = date(YEAR, MONTH, 1)
    records = [_work_day(1, office_minutes=60)]
    planned = [PlannedDay(date=today, office_minutes=390, remote_minutes=90)]
    stats, balances = calculate_month_stats(YEAR, MONTH, records, today=today, planned_days=planned)
    assert stats.total_office_hours == 6.5
    assert stats.total_wfh_hours == 1.5
    # Balance still on actual clocked: 1h - 8h = -7h
    assert balances[today] == -420


def test_today_projection_keeps_actual_when_already_over_plan() -> None:
    today = date(YEAR, MONTH, 1)
    records = [_work_day(1, office_minutes=600)]  # 10h, already past the 8h default
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=today, planned_days=[])
    assert stats.total_office_hours == 10.0


def test_over_quota_wfh_excluded_from_eom_surplus() -> None:
    # All-office month meeting required, plus WFH way over quota -> the excess WFH
    # must NOT inflate the end-of-month surplus.
    records = generate_month_calendar(YEAR, MONTH)
    working = [r for r in records if r.day_type == DayType.WORKING_DAY]
    for r in working:
        r.entries.append(WorkEntry(WorkplaceType.OFFICE, None, None, Duration(480), "Work"))
        r.entries.append(WorkEntry(WorkplaceType.WFH, None, None, Duration(120), "Work"))
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=date(YEAR, MONTH, 30))
    # Office alone (22*8h) already meets the 176h required; WFH (22*2h=44h) is all
    # above the office requirement and only counts up to the 33h quota.
    assert stats.actual_wfh_hours == 44.0
    assert stats.wfh_quota_hours == 33.0
    # Surplus = office(176) + min(wfh 44, quota 33) - required 176 = 33h, NOT 44h.
    assert stats.total_deficit == -33.0


def test_leave_at_ignores_over_quota_wfh() -> None:
    # Quota fully burned on earlier days; today's WFH is entirely over quota and
    # must not count toward the suggested clock-out.
    records = generate_month_calendar(YEAR, MONTH)
    working = [r for r in records if r.day_type == DayType.WORKING_DAY]
    for r in working[:22]:  # burn the whole 33h quota with WFH before today
        r.entries.append(WorkEntry(WorkplaceType.WFH, None, None, Duration(120), "Work"))
    today = working[-1].date
    working[-1].entries.append(WorkEntry(WorkplaceType.WFH, None, None, Duration(480), "Work"))
    stats, _ = calculate_month_stats(YEAR, MONTH, records, today=today)
    # Today's 8h WFH is over quota -> contributes nothing, so a full day is still owed.
    assert stats.suggested_clockout_time not in (None, "Done ✓")


def test_leave_at_not_done_while_office_below_floor() -> None:
    # Even when the running balance has banked enough to read "Done", the monthly
    # office floor is a hard constraint WFH can never cover. With office still 1h02m
    # below the 143h floor and no future office planned, leave-at must NOT be Done.
    today = date(YEAR, MONTH, 30)
    yesterday = today - timedelta(days=1)
    # One synthetic prior day carrying all office done before today: 139h13m.
    prior = DayRecord(
        date=yesterday,
        day_type=DayType.WORKING_DAY,
        entries=[WorkEntry(WorkplaceType.OFFICE, None, None, Duration(8353), "Work")],
    )
    today_record = _work_day(30, office_minutes=165)  # today's 2h45m office -> 141h58m total
    # Balance term alone would say "Done" (banked a full day ahead).
    daily_balances = {yesterday: 480}
    office_required_minutes = 143 * 60  # 8580
    result = _suggested_clockout(
        [prior, today_record],
        daily_balances,
        today,
        hours_per_day=8,
        total_wfh_quota_minutes=33 * 60,
        office_required_minutes=office_required_minutes,
    )
    assert result not in (None, "Done ✓")  # ~1h02m of office still owed today


def test_leave_at_uses_server_summary_for_office_floor() -> None:
    # The local reconstruction can diverge from Praise's authoritative summary. Here the
    # local office is well over the floor (would say Done locally) but the server summary
    # says office is still 1h02m below the required on-site floor -> NOT Done.
    today = date(YEAR, MONTH, 30)
    yesterday = today - timedelta(days=1)
    prior = DayRecord(
        date=yesterday,
        day_type=DayType.WORKING_DAY,
        entries=[WorkEntry(WorkplaceType.OFFICE, None, None, Duration(8600), "Work")],
    )
    today_record = _work_day(30, remote_minutes=165)
    summary = ServerSummary(required_on_site_minutes=8580, on_site_minutes=8518)
    result = _suggested_clockout(
        [prior, today_record],
        {yesterday: 480},
        today,
        hours_per_day=8,
        total_wfh_quota_minutes=33 * 60,
        office_required_minutes=8580,
        server_summary=summary,
    )
    assert result not in (None, "Done ✓")
    # Once the server summary shows the floor met, it is genuinely Done.
    summary_met = ServerSummary(required_on_site_minutes=8580, on_site_minutes=8600)
    assert (
        _suggested_clockout(
            [prior, today_record],
            {yesterday: 480},
            today,
            hours_per_day=8,
            total_wfh_quota_minutes=33 * 60,
            office_required_minutes=8580,
            server_summary=summary_met,
        )
        == "Done ✓"
    )


def test_leave_at_done_when_office_floor_met() -> None:
    # Same banked balance, but office already at the floor -> genuinely Done.
    today = date(YEAR, MONTH, 30)
    yesterday = today - timedelta(days=1)
    prior = DayRecord(
        date=yesterday,
        day_type=DayType.WORKING_DAY,
        entries=[WorkEntry(WorkplaceType.OFFICE, None, None, Duration(8580), "Work")],
    )
    today_record = _work_day(30, office_minutes=0)
    result = _suggested_clockout(
        [prior, today_record],
        {yesterday: 480},
        today,
        hours_per_day=8,
        total_wfh_quota_minutes=33 * 60,
        office_required_minutes=143 * 60,
    )
    assert result == "Done ✓"


def test_server_summary_discrepancy_flagged() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    # Server thinks the budget is 1h/day, local config says 1.5h/day
    summary = ServerSummary(
        required_minutes=22 * 480,
        remote_budget_minutes=22 * 60,
    )
    stats, _ = calculate_month_stats(
        YEAR, MONTH, records, today=date(YEAR, MONTH, 1), server_summary=summary
    )
    assert len(stats.discrepancies) == 1
    assert "WFH quota mismatch" in stats.discrepancies[0]


def test_server_summary_agreement_no_flags() -> None:
    records = generate_month_calendar(YEAR, MONTH)
    summary = ServerSummary(
        required_minutes=22 * 480,
        remote_budget_minutes=(22 * 90),
    )
    stats, _ = calculate_month_stats(
        YEAR, MONTH, records, today=date(YEAR, MONTH, 1), server_summary=summary
    )
    assert stats.discrepancies == []
