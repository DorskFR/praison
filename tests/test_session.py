"""Tests for the Praise CLI device-flow client and bearer-token fetches."""

import pytest

import praison.praise.session as session_mod
from praison.errors import PraiseCliLoginError, PraiseTokenExpiredError
from praison.praise.session import PraiseSession, fetch_me, poll_cli_token, start_cli_login


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload


def _ok(data: dict) -> _Resp:
    return _Resp({"success": True, "data": data})


def _err(code: str, status: int = 400) -> _Resp:
    return _Resp({"success": False, "error": {"code": code}}, status)


class _FakeSession:
    """Stand-in for requests.Session used by PraiseSession."""

    response: _Resp = _ok({"days": []})

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.closed = False

    def get(self, url: str, **kwargs: object) -> _Resp:  # noqa: ARG002
        return type(self).response

    def close(self) -> None:
        self.closed = True


def test_start_cli_login_posts_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_post(url: str, json: dict, headers: dict, timeout: int) -> _Resp:  # noqa: ARG001
        captured["url"] = url
        captured["headers"] = headers
        return _ok(
            {
                "deviceCode": "dc",
                "userCode": "ABCD1234",
                "verificationUrl": "https://praise.example/cli/authorize",
                "expiresAt": "2026-06-30T00:00:00Z",
                "intervalSeconds": 5,
            }
        )

    monkeypatch.setattr(session_mod.requests, "post", fake_post)
    start = start_cli_login("praise.example")

    assert start.base_url == "https://praise.example"
    assert start.device_code == "dc"
    assert start.user_code == "ABCD1234"
    assert str(captured["url"]).endswith("/api/auth/cli/start")
    assert "X-Praise-CLI-Version" in captured["headers"]  # type: ignore[operator]


def test_poll_pending_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_mod.requests, "post", lambda *_a, **_k: _err("apiError.cliLoginPending")
    )
    assert poll_cli_token("praise.example", "dc") is None


def test_poll_success_returns_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_mod.requests, "post", lambda *_a, **_k: _ok({"token": "prs_cli_abc"})
    )
    assert poll_cli_token("praise.example", "dc") == "prs_cli_abc"


def test_poll_rejected_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_mod.requests, "post", lambda *_a, **_k: _err("apiError.cliLoginRejected")
    )
    with pytest.raises(PraiseCliLoginError):
        poll_cli_token("praise.example", "dc")


def test_fetch_me_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_get(url: str, headers: dict, timeout: int) -> _Resp:  # noqa: ARG001
        captured["headers"] = headers
        return _ok({"id": "u1", "email": "a@b.com", "orgName": "Org"})

    monkeypatch.setattr(session_mod.requests, "get", fake_get)
    me = fetch_me("praise.example", "prs_cli_x")

    assert me["email"] == "a@b.com"
    assert captured["headers"]["Authorization"] == "Bearer prs_cli_x"  # type: ignore[index]


def test_fetch_me_401_raises_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_mod.requests, "get", lambda *_a, **_k: _Resp({}, 401))
    with pytest.raises(PraiseTokenExpiredError):
        fetch_me("praise.example", "bad")


def test_praise_session_get_timesheet_uses_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSession.response = _ok({"days": [], "summary": {}})
    fake = _FakeSession()
    monkeypatch.setattr(session_mod.requests, "Session", lambda: fake)

    with PraiseSession("praise.example", "prs_cli_x") as praise:
        data = praise.get_timesheet(2026, 6)

    assert data == {"days": [], "summary": {}}
    assert fake.headers["Authorization"] == "Bearer prs_cli_x"
    assert "X-Praise-CLI-Version" in fake.headers
    assert fake.closed


def test_praise_session_401_raises_token_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeSession.response = _Resp({}, 401)
    monkeypatch.setattr(session_mod.requests, "Session", _FakeSession)

    with PraiseSession("praise.example", "tok") as praise, pytest.raises(PraiseTokenExpiredError):
        praise.get_clock_status()
