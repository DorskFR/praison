"""Duration handling utilities."""

from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


class Duration:
    """Represents a duration in minutes with convenient operators."""

    @classmethod
    def parse(cls, duration: str) -> "Duration":
        """Parse a duration string like '09:30' into a Duration object."""
        if duration == "":
            return cls(0)
        hours, minutes = duration.split(":")
        total_minutes = 60 * int(hours) + int(minutes)
        return cls(total_minutes)

    @classmethod
    def now(cls, tz: ZoneInfo = JST) -> "Duration":
        """Get the current time of day as a Duration."""
        return cls.parse(datetime.now(tz).strftime("%H:%M"))

    def __init__(self, minutes: int = 0) -> None:
        self.minutes: int = minutes

    def __repr__(self) -> str:
        sign = "-" if self.minutes < 0 else ""
        abs_minutes = abs(self.minutes)
        return f"{sign}{abs_minutes // 60:02}:{abs_minutes % 60:02}"

    __str__ = __repr__

    def __hash__(self) -> int:
        return hash(self.minutes)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Duration):
            return NotImplemented
        return self.minutes == other.minutes

    def __ne__(self, other: object) -> bool:
        if not isinstance(other, Duration):
            return NotImplemented
        return self.minutes != other.minutes

    def __add__(self, other: "Duration") -> "Duration":
        return Duration(self.minutes + other.minutes)

    def __sub__(self, other: "Duration") -> "Duration":
        return Duration(self.minutes - other.minutes)

    def __neg__(self) -> "Duration":
        return Duration(-self.minutes)

    def __mul__(self, other: int) -> "Duration":
        return Duration(other * self.minutes)

    def __lt__(self, other: "Duration") -> bool:
        return self.minutes < other.minutes

    def __gt__(self, other: "Duration") -> bool:
        return self.minutes > other.minutes

    def __le__(self, other: "Duration") -> bool:
        return self.minutes <= other.minutes

    def __ge__(self, other: "Duration") -> bool:
        return self.minutes >= other.minutes

    def __abs__(self) -> "Duration":
        return Duration(abs(self.minutes))

    def __bool__(self) -> bool:
        return self.minutes > 0
