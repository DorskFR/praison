"""Tests for the Praise-URL allowlist and session-secret separation."""

import pytest

from praison.errors import PraiseUrlNotAllowedError
from praison.security import assert_praise_url_allowed


def test_no_allowlist_accepts_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRAISON_ALLOWED_PRAISE_URLS", raising=False)
    assert_praise_url_allowed("https://anything.example.com")  # no raise


def test_empty_allowlist_accepts_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAISON_ALLOWED_PRAISE_URLS", "  , ")
    assert_praise_url_allowed("https://anything.example.com")  # no raise


def test_allowlist_permits_listed_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAISON_ALLOWED_PRAISE_URLS", "praise.example.com, other.example.org")
    # Bare host, scheme, trailing slash and case all normalize to the same host.
    assert_praise_url_allowed("PRAISE.example.com")
    assert_praise_url_allowed("https://praise.example.com/")
    assert_praise_url_allowed("https://other.example.org")


def test_allowlist_rejects_unlisted_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAISON_ALLOWED_PRAISE_URLS", "praise.example.com")
    with pytest.raises(PraiseUrlNotAllowedError):
        assert_praise_url_allowed("https://evil.example.net")


def test_allowlist_accepts_full_url_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRAISON_ALLOWED_PRAISE_URLS", "https://praise.example.com/")
    assert_praise_url_allowed("praise.example.com")
    with pytest.raises(PraiseUrlNotAllowedError):
        assert_praise_url_allowed("notpraise.example.com")


def test_session_secret_separate_from_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet

    from praison import crypto

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("PRAISON_SECRET_KEY", key)
    monkeypatch.delenv("PRAISON_SESSION_SECRET", raising=False)

    # The cookie-signing secret is derived, not the raw Fernet key.
    assert crypto.session_secret() != key

    # An explicit override wins.
    monkeypatch.setenv("PRAISON_SESSION_SECRET", "explicit-session-secret")
    assert crypto.session_secret() == "explicit-session-secret"
