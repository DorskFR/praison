"""Custom exceptions."""


class PraisonError(Exception):
    """Base exception for praison."""


class InvalidPraiseLoginError(PraisonError):
    """Raised when Praise rejects the login credentials."""


class PraiseApiError(PraisonError):
    """Raised when a Praise API call fails."""
