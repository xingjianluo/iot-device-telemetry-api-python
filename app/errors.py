"""Domain error types and their stable machine-readable codes.

Authentication rejection reasons are intentionally distinct so callers can
tell why a credential was refused:

* ``missing_credential``  - no Authorization header / token presented
* ``invalid_credential``  - token does not match any credential on record
* ``credential_expired``  - token matched an old credential whose grace window
                            (the post-rotation compatibility window) has lapsed
* ``credential_revoked``  - token matched a credential invalidated early by a
                            later rotation before its grace window ended
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for errors that map to a specific HTTP response."""

    status_code: int = 400
    code: str = "app_error"

    def __init__(self, message: str, *, code: str | None = None,
                 status_code: int | None = None, detail: object | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.detail = detail


class AuthenticationError(AppError):
    """Raised when device authentication fails. ``code`` distinguishes why."""

    status_code = 401


class DeviceNotFoundError(AppError):
    status_code = 404
    code = "device_not_found"
