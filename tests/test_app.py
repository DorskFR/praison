"""End-to-end tests for the device-flow login endpoints (FastAPI TestClient)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import praison.app as app_mod
from praison.database import SqliteStore
from praison.praise.session import CliLoginStart


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAISON_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("PRAISON_HTTPS_ONLY", "false")
    monkeypatch.delenv("PRAISON_ALLOWED_PRAISE_URLS", raising=False)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(db_path=tmp_path / "planning.db")


@pytest.fixture
def client(env: None, store: SqliteStore) -> TestClient:  # noqa: ARG001
    return TestClient(app_mod.create_app(db=store))


def _stub_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        app_mod,
        "start_cli_login",
        lambda _url, *_a, **_k: CliLoginStart(
            base_url="https://praise.example",
            device_code="dc",
            user_code="ABCD1234",
            verification_url="https://praise.example/cli/authorize",
            expires_at="2099-01-01T00:00:00Z",
            interval_seconds=5,
        ),
    )


def test_login_form_renders(client: TestClient) -> None:
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "Praise URL" in resp.text


def test_unauthenticated_redirects_to_login(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_login_start_shows_code(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_start(monkeypatch)
    resp = client.post("/login/start", data={"praise_url": "praise.example"})
    assert resp.status_code == 200
    assert "ABCD-1234" in resp.text  # user code formatted XXXX-XXXX
    assert "praise.example/cli/authorize" in resp.text


def test_login_poll_pending_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_start(monkeypatch)
    client.post("/login/start", data={"praise_url": "praise.example"})
    monkeypatch.setattr(app_mod, "poll_cli_token", lambda _base, _dc: None)
    resp = client.get("/login/poll")
    assert resp.status_code == 204
    assert "hx-redirect" not in {k.lower() for k in resp.headers}


def test_login_poll_completes_and_stores_token(
    client: TestClient, store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_start(monkeypatch)
    client.post("/login/start", data={"praise_url": "praise.example"})
    monkeypatch.setattr(app_mod, "poll_cli_token", lambda _base, _dc: "prs_cli_token")
    monkeypatch.setattr(
        app_mod,
        "fetch_me",
        lambda _base, _tok: {"id": "x", "email": "user@example.com", "orgName": "Org"},
    )

    resp = client.get("/login/poll")
    assert resp.status_code == 204
    assert resp.headers["hx-redirect"] == "/"

    user = store.get_user_by_identity("https://praise.example", "user@example.com")
    assert user is not None
    assert store.get_praise_token(user.id) is not None  # token persisted, encrypted
