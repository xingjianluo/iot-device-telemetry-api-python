"""FastAPI dependencies: bearer-token device authentication."""

from __future__ import annotations

from fastapi import Header

from app import security
from app.errors import AuthenticationError
from app.store import DeviceRecord, get_store

_BEARER_PREFIX = "Bearer "


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if authorization.startswith(_BEARER_PREFIX):
        token = authorization[len(_BEARER_PREFIX):].strip()
        return token or None
    # A header without the Bearer scheme is malformed rather than secret: we
    # treat the raw value as a token candidate; an unknown value then fails as
    # invalid_credential, and an empty header as missing_credential.
    return authorization.strip() or None


def get_authenticated_device(
    authorization: str | None = Header(default=None),
) -> DeviceRecord:
    """Resolve the caller to a device or raise with a distinct error code."""
    token = _extract_token(authorization)
    return security.authenticate(get_store(), token)
