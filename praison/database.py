"""Storage: Postgres when DB_HOST is set, SQLite otherwise.

Multi-tenant: every user is a row in ``users`` (keyed on a Praise server + email
pair) and every planned day is scoped to a ``user_id``. The Praise password is
never stored here -- it lives only in the user's session cookie -- so the user
row exists purely for ownership and per-user settings. Legacy single-tenant rows
(no ``user_id``) are migrated to a ``'legacy'`` placeholder and claimed by the
seeded user on startup.
"""

import os
import sqlite3
import uuid
from datetime import date
from pathlib import Path
from typing import Protocol

from praison.config import DEFAULT_DB_PATH
from praison.models import PlannedDay, User

_COLUMNS = "date, office_minutes, remote_minutes, is_paid_leave, is_half_day_leave, note"
_USER_COLUMNS = "id, praise_url, praise_email, hours_per_day, wfh_hours_per_business_day"
_LEGACY_USER_ID = "legacy"


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


def _row_to_user(row: tuple) -> User:
    return User(
        id=row[0],
        praise_url=row[1],
        praise_email=row[2],
        hours_per_day=row[3],
        wfh_hours_per_business_day=row[4],
    )


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _new_user_id() -> str:
    return uuid.uuid4().hex


class Store(Protocol):
    """Storage interface for users and their planned days."""

    def create_user(
        self,
        praise_url: str,
        praise_email: str,
        hours_per_day: int,
        wfh_hours_per_business_day: float,
    ) -> User: ...

    def get_user_by_identity(self, praise_url: str, praise_email: str) -> User | None: ...

    def get_user_by_id(self, user_id: str) -> User | None: ...

    def update_login(self, user_id: str) -> None: ...

    def update_settings(
        self, user_id: str, hours_per_day: int, wfh_hours_per_business_day: float
    ) -> None: ...

    def claim_legacy_plans(self, user_id: str) -> None: ...

    def save_planned_day(self, user_id: str, planned: PlannedDay) -> None: ...

    def get_planned_day(self, user_id: str, target_date: date) -> PlannedDay | None: ...

    def get_planned_days_for_month(
        self, user_id: str, year: int, month: int
    ) -> list[PlannedDay]: ...

    def delete_planned_day(self, user_id: str, target_date: date) -> None: ...


def create_database() -> Store:
    """Postgres when DB_HOST is set in the environment, SQLite otherwise."""
    if os.environ.get("DB_HOST"):
        return PostgresStore(
            host=os.environ["DB_HOST"],
            dbname=os.environ.get("DB_NAME", "praison"),
            user=os.environ.get("DB_USER", "postgres"),
            password=os.environ.get("DB_PASS", "postgres"),
        )
    return SqliteStore()


_USERS_SQLITE = """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        praise_url TEXT NOT NULL,
        praise_email TEXT NOT NULL,
        hours_per_day INTEGER NOT NULL DEFAULT 8,
        wfh_hours_per_business_day REAL NOT NULL DEFAULT 1.5,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        last_login_at TEXT,
        UNIQUE (praise_url, praise_email)
    )
"""

_PLANNED_SQLITE = """
    CREATE TABLE IF NOT EXISTS planned_days (
        user_id TEXT NOT NULL,
        date TEXT NOT NULL,
        office_minutes INTEGER NOT NULL,
        remote_minutes INTEGER NOT NULL,
        is_paid_leave INTEGER NOT NULL,
        is_half_day_leave INTEGER NOT NULL DEFAULT 0,
        note TEXT,
        PRIMARY KEY (user_id, date)
    )
"""


class SqliteStore:
    """SQLite-backed store (local/standalone use)."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_USERS_SQLITE)
            # Drop the now-unused password column: the Praise password lives in
            # the session cookie, never at rest. (SQLite >= 3.35 supports this.)
            user_cols = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
            if "encrypted_password" in user_cols:
                conn.execute("ALTER TABLE users DROP COLUMN encrypted_password")
            cols = [row[1] for row in conn.execute("PRAGMA table_info(planned_days)")]
            if not cols:
                conn.execute(_PLANNED_SQLITE)
            elif "user_id" not in cols:
                # Legacy single-tenant table keyed on date alone: rebuild with a
                # composite key, tagging existing rows as 'legacy' for later claim.
                conn.execute("ALTER TABLE planned_days RENAME TO planned_days_legacy")
                conn.execute(_PLANNED_SQLITE)
                conn.execute(
                    f"INSERT INTO planned_days (user_id, {_COLUMNS}) "  # noqa: S608
                    f"SELECT '{_LEGACY_USER_ID}', {_COLUMNS} FROM planned_days_legacy"
                )
                conn.execute("DROP TABLE planned_days_legacy")
            conn.commit()

    def create_user(
        self,
        praise_url: str,
        praise_email: str,
        hours_per_day: int,
        wfh_hours_per_business_day: float,
    ) -> User:
        user = User(
            id=_new_user_id(),
            praise_url=praise_url,
            praise_email=praise_email,
            hours_per_day=hours_per_day,
            wfh_hours_per_business_day=wfh_hours_per_business_day,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT INTO users ({_USER_COLUMNS}) VALUES (?, ?, ?, ?, ?)",  # noqa: S608
                (
                    user.id,
                    user.praise_url,
                    user.praise_email,
                    user.hours_per_day,
                    user.wfh_hours_per_business_day,
                ),
            )
            conn.commit()
        return user

    def get_user_by_identity(self, praise_url: str, praise_email: str) -> User | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users "  # noqa: S608
                "WHERE praise_url = ? AND praise_email = ?",
                (praise_url, praise_email),
            )
            row = cursor.fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> User | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",  # noqa: S608
                (user_id,),
            )
            row = cursor.fetchone()
            return _row_to_user(row) if row else None

    def update_login(self, user_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
                (user_id,),
            )
            conn.commit()

    def update_settings(
        self, user_id: str, hours_per_day: int, wfh_hours_per_business_day: float
    ) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE users SET hours_per_day = ?, wfh_hours_per_business_day = ? WHERE id = ?",
                (hours_per_day, wfh_hours_per_business_day, user_id),
            )
            conn.commit()

    def claim_legacy_plans(self, user_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE planned_days SET user_id = ? WHERE user_id = ?",
                (user_id, _LEGACY_USER_ID),
            )
            conn.commit()

    def save_planned_day(self, user_id: str, planned: PlannedDay) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO planned_days (user_id, {_COLUMNS}) "  # noqa: S608
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    planned.date.isoformat(),
                    planned.office_minutes,
                    planned.remote_minutes,
                    1 if planned.is_paid_leave else 0,
                    1 if planned.is_half_day_leave else 0,
                    planned.note,
                ),
            )
            conn.commit()

    def get_planned_day(self, user_id: str, target_date: date) -> PlannedDay | None:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE user_id = ? AND date = ?",
                (user_id, target_date.isoformat()),
            )
            row = cursor.fetchone()
            return _row_to_planned(row) if row else None

    def get_planned_days_for_month(self, user_id: str, year: int, month: int) -> list[PlannedDay]:
        start_date, end_date = _month_bounds(year, month)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE user_id = ? AND date >= ? AND date < ? ORDER BY date",
                (user_id, start_date.isoformat(), end_date.isoformat()),
            )
            return [_row_to_planned(row) for row in cursor]

    def delete_planned_day(self, user_id: str, target_date: date) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM planned_days WHERE user_id = ? AND date = ?",
                (user_id, target_date.isoformat()),
            )
            conn.commit()


_USERS_PG = """
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        praise_url TEXT NOT NULL,
        praise_email TEXT NOT NULL,
        hours_per_day INTEGER NOT NULL DEFAULT 8,
        wfh_hours_per_business_day DOUBLE PRECISION NOT NULL DEFAULT 1.5,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        last_login_at TIMESTAMPTZ,
        UNIQUE (praise_url, praise_email)
    )
"""

_PLANNED_PG = """
    CREATE TABLE IF NOT EXISTS planned_days (
        user_id TEXT NOT NULL,
        date DATE NOT NULL,
        office_minutes INTEGER NOT NULL,
        remote_minutes INTEGER NOT NULL,
        is_paid_leave BOOLEAN NOT NULL,
        is_half_day_leave BOOLEAN NOT NULL DEFAULT FALSE,
        note TEXT,
        PRIMARY KEY (user_id, date)
    )
"""


class PostgresStore:
    """Postgres-backed store (deployed use)."""

    def __init__(self, host: str, dbname: str, user: str, password: str) -> None:
        import psycopg2

        self._connect = lambda: psycopg2.connect(
            host=host, dbname=dbname, user=user, password=password
        )
        self._init_db()

    def _init_db(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(_USERS_PG)
            # Drop the now-unused password column: the Praise password lives in
            # the session cookie, never at rest.
            cur.execute("ALTER TABLE users DROP COLUMN IF EXISTS encrypted_password")
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'planned_days'"
            )
            cols = {row[0] for row in cur.fetchall()}
            if not cols:
                cur.execute(_PLANNED_PG)
            elif "user_id" not in cols:
                # Legacy single-tenant table keyed on date: add user_id, retag the
                # primary key as (user_id, date), tag existing rows 'legacy'.
                cur.execute(
                    "ALTER TABLE planned_days ADD COLUMN user_id TEXT NOT NULL "
                    f"DEFAULT '{_LEGACY_USER_ID}'"
                )
                cur.execute("ALTER TABLE planned_days DROP CONSTRAINT IF EXISTS planned_days_pkey")
                cur.execute("ALTER TABLE planned_days ADD PRIMARY KEY (user_id, date)")
                cur.execute("ALTER TABLE planned_days ALTER COLUMN user_id DROP DEFAULT")
            conn.commit()

    def create_user(
        self,
        praise_url: str,
        praise_email: str,
        hours_per_day: int,
        wfh_hours_per_business_day: float,
    ) -> User:
        user = User(
            id=_new_user_id(),
            praise_url=praise_url,
            praise_email=praise_email,
            hours_per_day=hours_per_day,
            wfh_hours_per_business_day=wfh_hours_per_business_day,
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO users ({_USER_COLUMNS}) VALUES (%s, %s, %s, %s, %s)",  # noqa: S608
                (
                    user.id,
                    user.praise_url,
                    user.praise_email,
                    user.hours_per_day,
                    user.wfh_hours_per_business_day,
                ),
            )
            conn.commit()
        return user

    def get_user_by_identity(self, praise_url: str, praise_email: str) -> User | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM users "  # noqa: S608
                "WHERE praise_url = %s AND praise_email = %s",
                (praise_url, praise_email),
            )
            row = cur.fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: str) -> User | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_USER_COLUMNS} FROM users WHERE id = %s",  # noqa: S608
                (user_id,),
            )
            row = cur.fetchone()
            return _row_to_user(row) if row else None

    def update_login(self, user_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET last_login_at = now() WHERE id = %s",
                (user_id,),
            )
            conn.commit()

    def update_settings(
        self, user_id: str, hours_per_day: int, wfh_hours_per_business_day: float
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET hours_per_day = %s, wfh_hours_per_business_day = %s "
                "WHERE id = %s",
                (hours_per_day, wfh_hours_per_business_day, user_id),
            )
            conn.commit()

    def claim_legacy_plans(self, user_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE planned_days SET user_id = %s WHERE user_id = %s",
                (user_id, _LEGACY_USER_ID),
            )
            conn.commit()

    def save_planned_day(self, user_id: str, planned: PlannedDay) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO planned_days (user_id, {_COLUMNS}) "  # noqa: S608
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (user_id, date) DO UPDATE SET "
                "office_minutes = EXCLUDED.office_minutes, "
                "remote_minutes = EXCLUDED.remote_minutes, "
                "is_paid_leave = EXCLUDED.is_paid_leave, "
                "is_half_day_leave = EXCLUDED.is_half_day_leave, "
                "note = EXCLUDED.note",
                (
                    user_id,
                    planned.date,
                    planned.office_minutes,
                    planned.remote_minutes,
                    planned.is_paid_leave,
                    planned.is_half_day_leave,
                    planned.note,
                ),
            )
            conn.commit()

    def get_planned_day(self, user_id: str, target_date: date) -> PlannedDay | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE user_id = %s AND date = %s",
                (user_id, target_date),
            )
            row = cur.fetchone()
            return _row_to_planned(row) if row else None

    def get_planned_days_for_month(self, user_id: str, year: int, month: int) -> list[PlannedDay]:
        start_date, end_date = _month_bounds(year, month)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {_COLUMNS} FROM planned_days "  # noqa: S608
                "WHERE user_id = %s AND date >= %s AND date < %s ORDER BY date",
                (user_id, start_date, end_date),
            )
            return [_row_to_planned(row) for row in cur.fetchall()]

    def delete_planned_day(self, user_id: str, target_date: date) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM planned_days WHERE user_id = %s AND date = %s",
                (user_id, target_date),
            )
            conn.commit()
