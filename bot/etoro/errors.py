"""
M13.2 — eToro adapter exception hierarchy.

Concrete typed exceptions so callers can branch on cause without
parsing error strings.
"""


class EtoroError(Exception):
    """Base class for all eToro adapter errors."""


class EtoroAuthError(EtoroError):
    """401/403 — credentials invalid OR endpoint not entitled for this key.
    Never retried by the client."""


class EtoroRouteError(EtoroError):
    """404 — endpoint path is wrong (bug in our code). Never retried."""


class EtoroValidationError(EtoroError):
    """4xx other than 401/403/404/429 — request body or query is wrong.
    Never retried."""


class EtoroRateLimitError(EtoroError):
    """429 Too Many Requests. Carries retry_after (seconds) if the
    response provided a Retry-After header."""

    def __init__(self, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.retry_after = retry_after


class EtoroTransientError(EtoroError):
    """5xx or network/timeout error. Retryable with backoff."""
