"""Server-side guardrails for user-supplied input.

The Praise URL is attacker-controllable (the login form posts it and the server
then makes requests to it), so an operator can pin which Praise hosts are
acceptable via ``PRAISON_ALLOWED_PRAISE_URLS``. When that variable is unset or
empty, every host is accepted (the historical behaviour).
"""

import os
from urllib.parse import urlsplit

from praison.errors import PraiseUrlNotAllowedError
from praison.praise.session import normalize_url

_ALLOWED_URLS_ENV = "PRAISON_ALLOWED_PRAISE_URLS"


def _host(url: str) -> str:
    """Lower-cased hostname of a URL, tolerating a bare host with no scheme."""
    return (urlsplit(normalize_url(url)).hostname or "").lower()


def allowed_praise_hosts() -> set[str]:
    """Hostnames permitted by ``PRAISON_ALLOWED_PRAISE_URLS`` (empty = allow all).

    Entries may be full URLs (``https://praise.example.com``) or bare hosts
    (``praise.example.com``); only the hostname is compared.
    """
    raw = os.environ.get(_ALLOWED_URLS_ENV, "")
    return {_host(entry) for entry in raw.split(",") if entry.strip()}


def assert_praise_url_allowed(url: str) -> None:
    """Raise :class:`PraiseUrlNotAllowedError` if ``url``'s host is not allowed.

    A no-op when no allowlist is configured.
    """
    allowed = allowed_praise_hosts()
    if not allowed:
        return
    if _host(url) not in allowed:
        raise PraiseUrlNotAllowedError(url)
