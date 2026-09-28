"""Credential issuance, rotation and token-based authentication.

Compatibility rule
------------------
On rotation the previous active credential is *not* killed instantly: it keeps
working for a configurable grace window (``DEFAULT_GRACE_SECONDS``) with status
``grace``, so devices can roll over without dropping in-flight telemetry. After
the window it becomes ``expired``. Rotating again before that window ends
invalidates the old credential immediately as ``revoked`` — hence there is at
most one active and at most one still-usable retired credential per device,
never two simultaneously active credentials.

Refused access always carries a distinguishable reason:

* ``missing_credential``  no token presented;
* ``invalid_credential``  token unknown;
* ``credential_expired``  token matched a credential past its grace window;
* ``credential_revoked``  token matched a credential superseded twice within
                          its grace window.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from app.errors import AuthenticationError
from app.store import CredentialRecord, DeviceRecord, InMemoryStore

DEFAULT_GRACE_SECONDS = 3600

TOKEN_PREFIX = "iotdev_"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(token: str) -> str:
    """One-way token hash (local only; SHA-256, no external KMS)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    """Generate a high-entropy opaque access token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def issue_credential(store: InMemoryStore, device: DeviceRecord,
                     now: datetime | None = None) -> tuple[CredentialRecord, str]:
    """Create and activate a brand-new credential for ``device``.

    The plaintext token is returned once; only its hash is stored.
    """
    now = now or utc_now()
    token = generate_token()
    token_hash = hash_token(token)
    cred = CredentialRecord(
        credential_id="cred_" + secrets.token_hex(8),
        device_id=device.device_id,
        token_hash=token_hash,
        issued_at=now,
        expires_at=None,
        status="active",
    )
    with device.lock:
        device.credentials[cred.credential_id] = cred
        device.active_credential_id = cred.credential_id
        store.index_token(token_hash, device.device_id, cred.credential_id)
    return cred, token


def retire_credential(device: DeviceRecord,
                      grace_seconds: int, now: datetime | None = None) -> CredentialRecord | None:
    """Move the device's active credential into its post-rotation state.

    * If there is no active credential, nothing happens.
    * The active credential becomes ``grace`` with an absolute expiry.
    * Any credential already in ``grace`` is invalidated immediately as
      ``revoked``. The token stays indexed on purpose, so presenting it later
      yields the distinguishable ``credential_revoked`` rejection rather than
      the vaguer ``invalid_credential``.
    """
    now = now or utc_now()
    with device.lock:
        previous_id = device.active_credential_id
        previous = (
            device.credentials.get(previous_id) if previous_id is not None else None
        )

        # Step 1: the currently active credential moves into grace with a
        # fresh window.
        retired: CredentialRecord | None = None
        if previous is not None and previous.status == "active":
            previous.status = "grace"
            previous.expires_at = now + timedelta(seconds=grace_seconds)
            retired = previous

        # Step 2: every *other* credential still in grace is force-invalidated
        # by this rotation: expired if its own window already elapsed, revoked
        # otherwise. Revoked tokens stay indexed so presenting one yields the
        # distinguishable credential_revoked code.
        for cred in list(device.credentials.values()):
            if cred is previous or cred.status != "grace":
                continue
            if cred.expires_at is not None and now >= cred.expires_at:
                cred.status = "expired"
            else:
                cred.status = "revoked"
        return retired


def _effective_status(cred: CredentialRecord, now: datetime) -> str:
    """Lazily turn an overdue grace credential into an expired one."""
    if cred.status == "grace" and cred.expires_at is not None and now >= cred.expires_at:
        cred.status = "expired"
    return cred.status


def authenticate(store: InMemoryStore, token: str | None,
                 now: datetime | None = None) -> DeviceRecord:
    """Authenticate a presented bearer token and return its device.

    Raises :class:`AuthenticationError` with a distinct code on every failure.
    """
    now = now or utc_now()
    if not token:
        raise AuthenticationError(
            "no credential presented", code="missing_credential", status_code=401
        )
    resolved = store.resolve_token(hash_token(token))
    if resolved is None:
        raise AuthenticationError(
            "credential is not recognized", code="invalid_credential", status_code=401
        )
    device, cred = resolved
    with device.lock:
        status = _effective_status(cred, now)
        if status == "active" or status == "grace":
            return device
        if status == "expired":
            raise AuthenticationError(
                "credential has passed its post-rotation grace window",
                code="credential_expired",
                status_code=401,
            )
        if status == "revoked":
            raise AuthenticationError(
                "credential was revoked by a subsequent rotation",
                code="credential_revoked",
                status_code=401,
            )
    # Indexed tokens should never reach an unknown status; fail closed.
    raise AuthenticationError(
        "credential is unusable", code="invalid_credential", status_code=401
    )
