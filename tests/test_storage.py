"""Tests for multi-tenant storage: encryption, user accounts, plan scoping."""

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from praison import crypto
from praison.database import SqliteStore
from praison.models import PlannedDay


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(db_path=tmp_path / "planning.db")


def test_crypto_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAISON_SECRET_KEY", raising=False)
    monkeypatch.setattr(crypto, "DEFAULT_KEY_PATH", tmp_path / "secret.key")
    crypto._fernet.cache_clear()  # noqa: SLF001 - reset memoized key for the test
    secret = "hunter2-correct-horse"  # noqa: S105
    token = crypto.encrypt(secret)
    assert token != secret
    assert crypto.decrypt(token) == secret


def test_create_and_fetch_user(store: SqliteStore) -> None:
    user = store.create_user("https://praise.example.com", "a@b.com", "enc", 7, 2.0)
    assert user.id
    assert store.get_user_by_id(user.id) == user
    assert store.get_user_by_identity("https://praise.example.com", "a@b.com") == user
    assert store.get_user_by_identity("https://other.example.com", "a@b.com") is None


def test_update_login_refreshes_password(store: SqliteStore) -> None:
    user = store.create_user("https://p.example.com", "a@b.com", "old", 8, 1.5)
    store.update_login(user.id, "new")
    refreshed = store.get_user_by_id(user.id)
    assert refreshed is not None
    assert refreshed.encrypted_password == "new"  # noqa: S105


def test_plans_are_scoped_per_user(store: SqliteStore) -> None:
    alice = store.create_user("https://p.example.com", "alice@b.com", "e", 8, 1.5)
    bob = store.create_user("https://p.example.com", "bob@b.com", "e", 8, 1.5)
    day = date(2026, 6, 8)
    store.save_planned_day(alice.id, PlannedDay(date=day, office_minutes=480, remote_minutes=0))
    store.save_planned_day(bob.id, PlannedDay(date=day, office_minutes=0, remote_minutes=120))

    alice_plan = store.get_planned_day(alice.id, day)
    bob_plan = store.get_planned_day(bob.id, day)
    assert alice_plan is not None
    assert alice_plan.office_minutes == 480
    assert bob_plan is not None
    assert bob_plan.remote_minutes == 120
    assert len(store.get_planned_days_for_month(alice.id, 2026, 6)) == 1

    store.delete_planned_day(alice.id, day)
    assert store.get_planned_day(alice.id, day) is None
    assert store.get_planned_day(bob.id, day) is not None  # bob untouched


def test_legacy_table_is_migrated_and_claimed(tmp_path: Path) -> None:
    # Simulate the old single-tenant schema (date-only primary key, no user_id).
    db_path = tmp_path / "planning.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE planned_days (date TEXT PRIMARY KEY, office_minutes INTEGER NOT NULL, "
            "remote_minutes INTEGER NOT NULL, is_paid_leave INTEGER NOT NULL, "
            "is_half_day_leave INTEGER NOT NULL DEFAULT 0, note TEXT)"
        )
        conn.execute("INSERT INTO planned_days VALUES ('2026-06-08', 480, 0, 0, 0, 'legacy plan')")
        conn.commit()

    store = SqliteStore(db_path=db_path)
    cols = [row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(planned_days)")]
    assert "user_id" in cols

    user = store.create_user("https://p.example.com", "a@b.com", "e", 8, 1.5)
    store.claim_legacy_plans(user.id)
    claimed = store.get_planned_day(user.id, date(2026, 6, 8))
    assert claimed is not None
    assert claimed.note == "legacy plan"
