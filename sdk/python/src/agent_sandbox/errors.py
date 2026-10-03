"""Exceptions raised by the SDK. All inherit from SandboxError."""
from __future__ import annotations

from typing import Optional


class SandboxError(Exception):
    """Base class. `status` is the HTTP status when the server answered, else None."""

    def __init__(self, message: str, status: Optional[int] = None, detail: object = None):
        super().__init__(message)
        self.status = status
        self.detail = detail if detail is not None else message


class APIConnectionError(SandboxError):
    """The server could not be reached (DNS, TLS, refused connection, timeout)."""


class AuthenticationError(SandboxError):
    """401: missing or invalid API key."""


class PermissionDeniedError(SandboxError, PermissionError):
    """403: path outside the workspace, egress outside the tenant policy, or another tenant's session."""


class NotFoundError(SandboxError):
    """404: the session doesn't exist or has expired."""


class QuotaExceededError(SandboxError):
    """413: the write would take the workspace past its disk quota."""


class ValidationError(SandboxError):
    """400/422: the request was rejected as invalid (e.g. unknown template, timeout out of range)."""


class RateLimitError(SandboxError):
    """429: a per-tenant limit was hit (sessions, requests per minute, or concurrent commands).

    `retry_after` is the server's suggested wait in seconds, when it sent one.
    """

    def __init__(self, message: str, status: int = 429, detail: object = None, retry_after: Optional[int] = None):
        super().__init__(message, status, detail)
        self.retry_after = retry_after


class CapacityError(SandboxError):
    """503: every workspace on the server is in use."""


class CommandError(SandboxError):
    """Raised by CommandResult.check() when a command failed, timed out or was killed."""

    def __init__(self, message: str, result: "object"):
        super().__init__(message)
        self.result = result


STATUS_ERRORS = {
    400: ValidationError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    413: QuotaExceededError,
    422: ValidationError,
    429: RateLimitError,
    503: CapacityError,
}
