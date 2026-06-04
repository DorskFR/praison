"""Japanese holiday calendar integration."""

from datetime import date

import jpholiday


def is_japanese_holiday(target_date: date) -> bool:
    """Check if a date is a Japanese public holiday."""
    return jpholiday.is_holiday(target_date)


def get_holiday_name(target_date: date) -> str | None:
    """Get the name of a Japanese holiday, or None if not a holiday."""
    return jpholiday.is_holiday_name(target_date)


def is_working_day(target_date: date) -> bool:
    """
    Check if a date is a working day.

    A working day is:
    - Not a weekend (Saturday/Sunday)
    - Not a Japanese public holiday
    """
    # 5 = Saturday, 6 = Sunday
    if target_date.weekday() in (5, 6):
        return False
    return not is_japanese_holiday(target_date)
