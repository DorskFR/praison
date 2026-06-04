"""Planning storage: Postgres when DB_HOST is set, SQLite otherwise."""

import os
import sqlite3
from datetime import date
from pathlib import Path
from typing import Protocol

from praison.config import DEFAULT_DB_PATH
from praison.models import PlannedDay

_COLUMNS = "date, office_minutes, remote_minutes, is_paid_leave, is_half_day_leave, note"


def _row_to_planned(row: tuple) -> PlannedDay:
    raw_date = row[0]
    return PlannedDay(
        date=raw_date if isinstance(raw_date, date) else date.fromisoformat(raw_date),
        office_minutes=row[1],
        remote_minutes=row[2],
        is_paid_leave=bool(row[3]),
        is_half_day_leave=bool(row[4]),
        note=row[5] or "",
    )


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


class PlanningStore(Protocol):
    """Storage interface for planned days."""

    def save_planned_day(self, planned: PlannedDay) -> None: ...

    def get_planned_day(self, target_date: date) -> PlannedDay | None: ...

    def get_planned_days_for_month(self, year: int, month: int) -> list[PlannedDay]: ...

    def delete_planned_day(self, target_date: date) -> None: ...

    def clear_all(self) -> None: ...


def create_database() -> PlanningStore:
    """Postgres when DB_HOST is set in the environment, SQLite otherwise."""
    if os.environ.get("DB_HOST"):
        return PostgresPlanningDatabase(
            host=os.environ["DB_HOST"],
            dbname=os.environ.get("DB_NAME", "praison"),
            user=os.environ.get("DB_USER", "postgres"),
            password=os.environ.get("DB_PASS", "postgres"),
        )
    return PlanningDatabase()


class PlanningDatabase:
    """SQLite-backed store (local/standalone use)."""

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
                f"INSERT OR REPLACE INTO planned_days ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: S608
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

    def get_planned_day(self, target_date: date) -> PlannedDay | None:
        """Get a planned day by date."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_COLUMNS} FROM planned_days WHERE date = ?",  # noqa: S608
                (target_date.isoformat(),),
            )
            row = cursor.fetchone()
            return _row_to_planned(row) if row else None

    def get_planned_days_for_month(self, year: int, month: int) -> list[PlannedDay]:
        """Get all planned days for a specific month."""
        start_date, end_date = _month_bounds(year, month)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE date >= ? AND date < ? ORDER BY date",
                (start_date.isoformat(), end_date.isoformat()),
            )
            return [_row_to_planned(row) for row in cursor]

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


class PostgresPlanningDatabase:
    """Postgres-backed store (deployed use)."""

    def __init__(self, host: str, dbname: str, user: str, password: str) -> None:
        import psycopg2

        self._connect = lambda: psycopg2.connect(
            host=host, dbname=dbname, user=user, password=password
        )
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS planned_days (
                    date DATE PRIMARY KEY,
                    office_minutes INTEGER NOT NULL,
                    remote_minutes INTEGER NOT NULL,
                    is_paid_leave BOOLEAN NOT NULL,
                    is_half_day_leave BOOLEAN NOT NULL DEFAULT FALSE,
                    note TEXT
                )
            """)
            conn.commit()

    def save_planned_day(self, planned: PlannedDay) -> None:
        """Save or update a planned day."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO planned_days ({_COLUMNS}) "  # noqa: S608
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (date) DO UPDATE SET "
                "office_minutes = EXCLUDED.office_minutes, "
                "remote_minutes = EXCLUDED.remote_minutes, "
                "is_paid_leave = EXCLUDED.is_paid_leave, "
                "is_half_day_leave = EXCLUDED.is_half_day_leave, "
                "note = EXCLUDED.note",
                (
                    planned.date,
                    planned.office_minutes,
                    planned.remote_minutes,
                    planned.is_paid_leave,
                    planned.is_half_day_leave,
                    planned.note,
                ),
            )
            conn.commit()

    def get_planned_day(self, target_date: date) -> PlannedDay | None:
        """Get a planned day by date."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM planned_days WHERE date = %s",  # noqa: S608
                (target_date,),
            )
            row = cur.fetchone()
            return _row_to_planned(row) if row else None

    def get_planned_days_for_month(self, year: int, month: int) -> list[PlannedDay]:
        """Get all planned days for a specific month."""
        start_date, end_date = _month_bounds(year, month)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE date >= %s AND date < %s ORDER BY date",
                (start_date, end_date),
            )
            return [_row_to_planned(row) for row in cur.fetchall()]

    def delete_planned_day(self, target_date: date) -> None:
        """Delete a planned day."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM planned_days WHERE date = %s", (target_date,))
            conn.commit()

    def clear_all(self) -> None:
        """Clear all planned days (for testing)."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM planned_days")
            conn.commit()
