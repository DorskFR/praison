"""SQLite database for storing future planning data."""

import sqlite3
from datetime import date
from pathlib import Path

from praison.config import DEFAULT_DB_PATH
from praison.models import PlannedDay


class PlanningDatabase:
    """Database for storing and retrieving planned days."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS planned_days (
                    date TEXT PRIMARY KEY,
                    office_minutes INTEGER NOT NULL,
                    remote_minutes INTEGER NOT NULL,
                    is_paid_leave INTEGER NOT NULL,
                    is_half_day_leave INTEGER NOT NULL DEFAULT 0,
                    note TEXT
                )
            """)
            conn.commit()

    def save_planned_day(self, planned: PlannedDay) -> None:
        """Save or update a planned day."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO planned_days
                (date, office_minutes, remote_minutes, is_paid_leave, is_half_day_leave, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    planned.date.isoformat(),
                    planned.office_minutes,
                    planned.remote_minutes,
                    1 if planned.is_paid_leave else 0,
                    1 if planned.is_half_day_leave else 0,
                    planned.note,
                ),
            )
            conn.commit()

    @staticmethod
    def _row_to_planned(row: tuple) -> PlannedDay:
        return PlannedDay(
            date=date.fromisoformat(row[0]),
            office_minutes=row[1],
            remote_minutes=row[2],
            is_paid_leave=bool(row[3]),
            is_half_day_leave=bool(row[4]),
            note=row[5] or "",
        )

    _SELECT = (
        "SELECT date, office_minutes, remote_minutes, is_paid_leave, is_half_day_leave, note "
        "FROM planned_days"
    )

    def get_planned_day(self, target_date: date) -> PlannedDay | None:
        """Get a planned day by date."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(f"{self._SELECT} WHERE date = ?", (target_date.isoformat(),))
            row = cursor.fetchone()
            return self._row_to_planned(row) if row else None

    def get_planned_days_for_month(self, year: int, month: int) -> list[PlannedDay]:
        """Get all planned days for a specific month."""
        start_date = date(year, month, 1)
        end_date = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"{self._SELECT} WHERE date >= ? AND date < ? ORDER BY date",
                (start_date.isoformat(), end_date.isoformat()),
            )
            return [self._row_to_planned(row) for row in cursor]

    def delete_planned_day(self, target_date: date) -> None:
        """Delete a planned day."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM planned_days WHERE date = ?", (target_date.isoformat(),))
            conn.commit()

    def clear_all(self) -> None:
        """Clear all planned days (for testing)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM planned_days")
            conn.commit()
