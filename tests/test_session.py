"""Tests for Praise session cookie reuse.

Praise mints a new session on every login and evicts the oldest once a user
exceeds its active-session cap, so the multi-tenant server must log in once per
user and replay the cookie. These tests pin that behaviour.
"""

from praison.praise.session import PraiseSession, SessionState


class _FakePraise(PraiseSession):
    """PraiseSession with the network stubbed: login just sets a cookie."""

    login_calls = 0

    def _fetch_build_version(self) -> None:
        self.session.headers["X-Build-Version"] = "v1"

    def _login(self) -> None:
        type(self).login_calls += 1
        self.session.cookies.set("session_id", "tok")


def test_session_state_reuse_skips_relogin() -> None:
    _FakePraise.login_calls = 0
    state = SessionState()

    # First use: no cookie yet -> one login, cookie + build version persisted.
    with _FakePraise("praise.example", "e", "p", session_path=None, state=state):
        pass
    assert _FakePraise.login_calls == 1
    assert len(state.cookies) == 1
    assert state.build_version == "v1"

    # Subsequent uses: cookie restored from state -> no further logins.
    for _ in range(3):
        with _FakePraise("praise.example", "e", "p", session_path=None, state=state):
            pass
    assert _FakePraise.login_calls == 1


def test_without_state_logs_in_every_time() -> None:
    _FakePraise.login_calls = 0
    for _ in range(3):
        with _FakePraise("praise.example", "e", "p", session_path=None):
            pass
    assert _FakePraise.login_calls == 3
