"""Data models for attendance and planning."""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from praison.duration import Duration


class WorkplaceType(StrEnum):
    """Type of workplace."""

    OFFICE = "on_site"
    WFH = "remote"


class DayType(StrEnum):
    """Type of day."""

    WORKING_DAY = "working_day"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"
    PAID_LEAVE = "paid_leave"
    HALF_DAY_PAID_LEAVE = "half_day_paid_leave"
    UNPAID_LEAVE = "unpaid_leave"


@dataclass
class WorkEntry:
    """A single work entry (clock-in/clock-out pair)."""

    workplace: WorkplaceType
    clock_in: Duration | None
    clock_out: Duration | None
    duration: Duration
    category: str


@dataclass
class DayRecord:
    """Record for a single day."""

    date: date
    day_type: DayType
    entries: list[WorkEntry]
    memo: str = ""
    hours_per_day: int = 8

    @property
    def office_minutes(self) -> int:
        """Total office minutes for this day (excluding leave)."""
        return sum(
            entry.duration.minutes
            for entry in self.entries
            if entry.workplace == WorkplaceType.OFFICE and not self._is_leave_entry(entry)
        )

    @property
    def remote_minutes(self) -> int:
        """Total remote (WFH) minutes for this day (excluding leave)."""
        return sum(
            entry.duration.minutes
            for entry in self.entries
            if entry.workplace == WorkplaceType.WFH and not self._is_leave_entry(entry)
        )

    def _is_leave_entry(self, entry: WorkEntry) -> bool:
        """Check if entry is a leave entry."""
        return "leave" in entry.category.lower() or "holiday" in entry.category.lower()

    @property
    def total_minutes(self) -> int:
        """Total work minutes for this day."""
        return sum(entry.duration.minutes for entry in self.entries)

    @property
    def expected_minutes(self) -> int:
        """Expected work minutes for this day based on day type."""
        if self.day_type == DayType.WORKING_DAY:
            return self.hours_per_day * 60
        if self.day_type == DayType.UNPAID_LEAVE:
            return self.hours_per_day * 60  # Unpaid leave: hours still due
        if self.day_type == DayType.PAID_LEAVE:
            return 0  # Paid leave: no hours due
        if self.day_type == DayType.HALF_DAY_PAID_LEAVE:
            return self.hours_per_day * 30  # Half-day paid leave: half the hours still due
        return 0  # Weekend/holiday


@dataclass
class PlannedDay:
    """A planned future day."""

    date: date
    office_minutes: int
    remote_minutes: int
    is_paid_leave: bool = False
    is_half_day_leave: bool = False
    note: str = ""


@dataclass
class ServerSummary:
    """Praise's server-computed monthly summary (ground truth for actuals)."""

    required_minutes: int = 0
    required_on_site_minutes: int = 0
    remote_budget_minutes: int = 0
    on_site_minutes: int = 0
    remote_minutes: int = 0
    has_remote_allowance: bool = False
    leave_days_count: float = 0


@dataclass
class MonthStats:
    """Statistics for a month."""

    year: int
    month: int
    working_days: int
    total_required_hours: float
    wfh_quota_hours: float
    office_required_hours: float
    actual_office_hours: float
    actual_wfh_hours: float
    paid_leave_days: float
    balance_minutes: int
    suggested_clockout_time: str | None = None  # e.g., "17:30" or "Done ✓"
    discrepancies: list[str] = field(default_factory=list)

    @property
    def total_office_hours(self) -> float:
        return self.actual_office_hours

    @property
    def total_wfh_hours(self) -> float:
        return self.actual_wfh_hours

    @property
    def wfh_over_quota(self) -> float:
        """How much WFH hours are over quota (negative if under)."""
        return self.total_wfh_hours - self.wfh_quota_hours

    @property
    def wfh_remaining(self) -> float:
        """Remaining WFH allowance in hours (0 if exhausted)."""
        return max(0.0, self.wfh_quota_hours - self.total_wfh_hours)

    @property
    def office_deficit(self) -> float:
        """How much office hours are under required (negative if over)."""
        return self.office_required_hours - self.total_office_hours

    @property
    def total_deficit(self) -> float:
        """
        Total hours deficit at end of month.

        WFH only contributes up to quota; any extra WFH doesn't count.
        """
        wfh_contribution = min(self.total_wfh_hours, self.wfh_quota_hours)
        total_worked = self.total_office_hours + wfh_contribution
        return self.total_required_hours - total_worked
