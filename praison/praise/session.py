"""HTTP client against the Praise API using a CLI device-flow bearer token.

Praise forces 2FA (TOTP) and reCAPTCHA on ``/api/auth/login``, which a headless
app cannot satisfy. Instead praison authenticates each user via Praise's CLI
device-authorization flow:

1. :func:`start_cli_login` opens a login request and returns a short user code
   plus a verification URL.
2. The user approves the request in their own Praise browser, where 2FA and the
   captcha are handled -- praison never sees either.
3. :func:`poll_cli_token` polls until Praise hands back a ``prs_cli_`` bearer
   token (or the request is denied/expires).

The caller stores that token encrypted, per user, and replays it via
:class:`PraiseSession` on every fetch. Every request carries
``X-Praise-CLI-Version`` (exactly as the real CLI does), which routes the call
past Praise's strict web build-version check -- so there is no version handshake.
"""

import logging
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import requests

from praison.errors import PraiseApiError, PraiseCliLoginError, PraiseTokenExpiredError

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds

CLI_TOKEN_PREFIX = "prs_cli_"  # noqa: S105 - token format marker, not a secret

# Sent as ``X-Praise-CLI-Version`` on every request. Its presence makes Praise's
# versionCheck middleware skip the exact web build-version match; cliVersionCheck
# then only rejects it if it is *below* the server's CLI_MIN_VERSION. Keep this at
# a recent CalVer; bump it if Praise raises its minimum supported CLI version.
_CLI_VERSION = "2026.6.30"
_BASE_HEADERS = {"X-Praise-CLI-Version": _CLI_VERSION}


def normalize_url(base_url: str) -> str:
    """Canonical Praise base URL: default to https, drop trailing slashes.

    Used both for connecting and as part of the per-user identity key, so the
    same server typed two ways resolves to one account.
    """
    base_url = base_url.strip()
    if not base_url.startswith(("http://", "https://")):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/")


@dataclass
class CliLoginStart:
    """A pending device-authorization request the user must approve in Praise."""

    base_url: str
    device_code: str
    user_code: str
    verification_url: str
    expires_at: str
    interval_seconds: int


def _body(response: requests.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _error_code(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _unwrap(response: requests.Response) -> Any:
    """Return ``data`` from a Praise ``APISuccessResponse``; raise otherwise.

    A 401 means the bearer token was rejected and the user must re-authorize.
    """
    if response.status_code == 401:
        raise PraiseTokenExpiredError("Praise rejected the stored token")
    body = _body(response)
    if not body.get("success"):
        code = _error_code(body) or f"http_{response.status_code}"
        raise PraiseApiError(f"API error: {code}")
    return body["data"]


def start_cli_login(base_url: str, label: str | None = "praison") -> CliLoginStart:
    """Open a CLI device-authorization request against Praise."""
    base_url = normalize_url(base_url)
    payload: dict[str, Any] = {}
    if label:
        payload["label"] = label
    response = requests.post(
        f"{base_url}/api/auth/cli/start", json=payload, headers=_BASE_HEADERS, timeout=_TIMEOUT
    )
    data = _unwrap(response)
    return CliLoginStart(
        base_url=base_url,
        device_code=data["deviceCode"],
        user_code=data["userCode"],
        verification_url=data["verificationUrl"],
        expires_at=data["expiresAt"],
        interval_seconds=int(data.get("intervalSeconds", 5)),
    )


def poll_cli_token(base_url: str, device_code: str) -> str | None:
    """Poll once for the device-flow token.

    Returns the ``prs_cli_`` token once the request is approved, or ``None`` while
    it is still pending. Raises :class:`PraiseCliLoginError` if the request was
    denied, expired, or is otherwise invalid.
    """
    base_url = normalize_url(base_url)
    response = requests.post(
        f"{base_url}/api/auth/cli/token",
        json={"deviceCode": device_code},
        headers=_BASE_HEADERS,
        timeout=_TIMEOUT,
    )
    body = _body(response)
    if body.get("success"):
        return body["data"]["token"]

    code = _error_code(body)
    if code == "apiError.cliLoginPending":
        return None
    terminal = {
        "apiError.cliLoginRejected": "You denied the authorization request in Praise.",
        "apiError.cliLoginExpired": "The authorization request expired. Please start again.",
        "apiError.cliLoginCodeInvalid": "That authorization request is no longer valid.",
    }
    if code in terminal:
        raise PraiseCliLoginError(terminal[code])
    raise PraiseCliLoginError(f"Unexpected Praise response ({code or response.status_code}).")


def fetch_me(base_url: str, token: str) -> dict[str, Any]:
    """Fetch the authenticated user (id, email, org) and validate the token."""
    base_url = normalize_url(base_url)
    response = requests.get(
        f"{base_url}/api/auth/me",
        headers={**_BASE_HEADERS, "Authorization": f"Bearer {token}"},
        timeout=_TIMEOUT,
    )
    return _unwrap(response)


def logout_token(base_url: str, token: str) -> None:
    """Best-effort revoke of a bearer token on the Praise side."""
    try:
        requests.post(
            f"{base_url}/api/auth/logout",
            headers={**_BASE_HEADERS, "Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as exc:  # best-effort; the token will expire anyway
        logger.info("praise token revoke failed (ignored): %s", exc)


class PraiseSession:
    """Authenticated, context-managed session against Praise using a bearer token."""

    def __init__(self, base_url: str, token: str) -> None:
        self._base_url = normalize_url(base_url)
        self._token = token
        self._session: requests.Session | None = None

    def __enter__(self) -> Self:
        self._session = requests.Session()
        self._session.headers.update(_BASE_HEADERS)
        self._session.headers["Authorization"] = f"Bearer {self._token}"
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
        kwargs.setdefault("timeout", _TIMEOUT)
        response = self.session.get(url, **kwargs)
        return _unwrap(response)
