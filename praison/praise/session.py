"""HTTP session against the Praise API.

Inspired by the praiselul package: cookie-based session persisted to disk,
X-Build-Version header from /api/health, transparent recovery from stale
build version (426) and rejected session (401).
"""

from dataclasses import dataclass, field
from http.cookiejar import LoadError, LWPCookieJar
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import requests
from requests.cookies import RequestsCookieJar

from praison.config import DEFAULT_SESSION_PATH
from praison.errors import InvalidPraiseLoginError, PraiseApiError

_TIMEOUT = 30  # seconds


@dataclass
class SessionState:
    """In-memory cookie + build-version store, reused across PraiseSession
    instances.

    The multi-tenant web server keeps one of these per user so it logs in to
    Praise once and replays the cookie on every fetch, instead of logging in
    afresh each time. A fresh login mints a new Praise session, and Praise caps
    active sessions per user and evicts the oldest once exceeded -- so repeated
    logins silently sign the user out of their real browser sessions.
    """

    cookies: RequestsCookieJar = field(default_factory=RequestsCookieJar)
    build_version: str | None = None


def normalize_url(base_url: str) -> str:
    """Canonical Praise base URL: default to https, drop trailing slashes.

    Used both for connecting and as part of the per-user identity key, so the
    same server typed two ways resolves to one account.
    """
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/")


class PraiseSession:
    """Authenticated context-managed session against praise."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        session_path: Path | None = DEFAULT_SESSION_PATH,
        state: SessionState | None = None,
    ) -> None:
        self._base_url = normalize_url(base_url)
        self._email = email
        self._password = password
        # session_path persists cookies to disk (CLI). state holds them in
        # memory across instances (multi-tenant web server, see SessionState).
        # When state is given it takes precedence and nothing touches disk.
        self._session_path = session_path
        self._state = state
        self._session: requests.Session | None = None

    @property
    def _meta_path(self) -> Path | None:
        """File holding the cached build version, alongside the cookie file."""
        return self._session_path.with_suffix(".meta") if self._session_path else None

    def __enter__(self) -> Self:
        self._session = requests.Session()
        if not self._load_session():
            self._fetch_build_version()
            self._login()
            self._save_session()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._session:
            self._session.close()
        self._session = None

    @property
    def session(self) -> requests.Session:
        assert self._session, "PraiseSession should be used as a context manager"
        return self._session

    def get_timesheet(self, year: int, month: int) -> dict[str, Any]:
        """Fetch the monthly timesheet (days + summary)."""
        return self._get_data(
            f"{self._base_url}/api/time/my-timesheet",
            params={"year": year, "month": month, "locale": "en"},
        )

    def get_clock_status(self) -> dict[str, Any]:
        """Fetch the current clock-in/out status."""
        return self._get_data(
            f"{self._base_url}/api/time/clock/status",
            params={"locale": "en"},
        )

    def get_locations(self) -> list[dict[str, Any]]:
        """Fetch clock-in locations (id, name, category on_site/remote)."""
        data = self._get_data(
            f"{self._base_url}/api/time/clock/locations",
            params={"locale": "en"},
        )
        if isinstance(data, list):
            return data
        return data.get("locations", [])

    def _get_data(self, url: str, **kwargs: Any) -> Any:
        response = self._get(url, **kwargs)
        response.raise_for_status()
        data = response.json()
        if not data.get("success"):
            code = data.get("error", {}).get("code", "unknown")
            raise PraiseApiError(f"API error: {code}")
        return data["data"]

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        """GET that transparently recovers once from a stale build version (426)
        or a rejected session (401) by refreshing the relevant state and retrying."""
        kwargs.setdefault("timeout", _TIMEOUT)
        response = self.session.get(url, **kwargs)
        if response.status_code == 426:
            self._fetch_build_version()
            self._save_build_version()
            response = self.session.get(url, **kwargs)
        if response.status_code == 401:
            self._login()
            self._save_session()
            response = self.session.get(url, **kwargs)
        return response

    def _fetch_build_version(self) -> None:
        """Fetch the server's build version from /api/health (no version check
        on that route) and set it as a default header for subsequent requests."""
        response = self.session.get(f"{self._base_url}/api/health", timeout=_TIMEOUT)
        response.raise_for_status()
        version = response.json().get("version")
        if version:
            self.session.headers["X-Build-Version"] = version

    def _login(self) -> None:
        response = self.session.post(
            f"{self._base_url}/api/auth/login",
            json={"email": self._email, "password": self._password},
            timeout=_TIMEOUT,
        )
        if response.status_code == 401:
            raise InvalidPraiseLoginError("Praise rejected the credentials")
        response.raise_for_status()
        if not response.json().get("success"):
            raise InvalidPraiseLoginError("Praise rejected the credentials")

    def _load_session(self) -> bool:
        """Load persisted cookies and build version. Returns True if a usable
        session was restored. Missing/corrupt files mean a fresh login."""
        if self._state is not None:
            if len(self._state.cookies) == 0:
                return False
            self.session.cookies.update(self._state.cookies)
            if self._state.build_version:
                self.session.headers["X-Build-Version"] = self._state.build_version
            else:
                self._fetch_build_version()
            return True
        if self._session_path is None or not self._session_path.is_file():
            return False
        jar = LWPCookieJar(str(self._session_path))
        try:
            jar.load(ignore_discard=True)
        except (OSError, LoadError):
            return False
        if not len(jar):
            return False
        self.session.cookies.update(jar)

        build_version = self._load_build_version()
        if build_version:
            self.session.headers["X-Build-Version"] = build_version
        else:
            self._fetch_build_version()
        return True

    def _save_session(self) -> None:
        """Persist the current cookies (0600) and build version."""
        if self._state is not None:
            self._state.cookies.clear()
            self._state.cookies.update(self.session.cookies)
            version = self.session.headers.get("X-Build-Version")
            self._state.build_version = str(version) if version else None
            return
        if self._session_path is None:
            return
        self._session_path.parent.mkdir(parents=True, exist_ok=True)
        jar = LWPCookieJar(str(self._session_path))
        for cookie in self.session.cookies:
            jar.set_cookie(cookie)
        jar.save(ignore_discard=True)
        self._session_path.chmod(0o600)
        self._save_build_version()

    def _load_build_version(self) -> str | None:
        if self._meta_path is None:
            return None
        try:
            return self._meta_path.read_text().strip() or None
        except OSError:
            return None

    def _save_build_version(self) -> None:
        if self._meta_path is None:
            return
        version = self.session.headers.get("X-Build-Version")
        if not version:
            return
        self._meta_path.write_text(str(version))
        self._meta_path.chmod(0o600)


def verify_credentials(base_url: str, email: str, password: str) -> None:
    """Validate credentials against Praise without persisting anything.

    Raises ``InvalidPraiseLoginError`` if Praise rejects them, ``PraiseApiError``
    or a network error if the server is unreachable. Used at login time to decide
    whether to admit (and, on first login, register) a praison account.
    """
    with PraiseSession(base_url, email, password, session_path=None):
        pass
