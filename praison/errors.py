"""Custom exceptions."""


class PraisonError(Exception):
    """Base exception for praison."""


class PraiseApiError(PraisonError):
    """Raised when a Praise API call fails."""


class PraiseTokenExpiredError(PraisonError):
    """Raised when Praise rejects the stored bearer token (401).

    Signals that the user must re-authorize praison via the device flow; there is
    no silent recovery because re-authorization needs a human approval in Praise.
    """


class PraiseCliLoginError(PraisonError):
    """Raised when the CLI device-authorization flow terminally fails.

    Covers a denied, expired, or otherwise invalid login request. The message is
    user-facing.
    """


class PraiseUrlNotAllowedError(PraisonError):
    """Raised when a submitted Praise URL is not in the configured allowlist."""
