"""Credential issuance, rotation grace window and distinguishable refusals."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import security, services
from app.errors import AuthenticationError
from app.store import get_store


def _register(name: str = "sensor") -> tuple[str, str]:
    info, cred = services.register_device(name, {"zone": "lab"})
    return info.device_id, cred.token


# --------------------------------------------------------------------------
# HTTP-level: registration and rotation shapes
# --------------------------------------------------------------------------

def test_register_returns_single_active_credential(client):
    resp = client.post("/devices", json={"name": "sensor-a", "metadata": {"loc": 1}})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["device"]["name"] == "sensor-a"
    assert body["credential"]["token"].startswith("iotdev_")
    assert body["credential"]["status"] == "active"
    assert body["credential"]["expires_at"] is None

    dev = get_store().require_device(body["device"]["device_id"])
    assert [c.status for c in dev.credentials.values()].count("active") == 1


def test_rotation_keeps_exactly_one_active_and_exposes_grace_window(client):
    device_id, token = _register()
    resp = client.post(
        f"/devices/{device_id}/credentials/rotate",
        json={"grace_seconds": 600},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["new"]["status"] == "active"
    assert body["retired"]["status"] == "grace"
    assert body["retired"]["expires_at"] is not None
    assert body["retired"]["token"] is None  # old token is never re-disclosed
    assert body["grace_seconds"] == 600

    dev = get_store().require_device(device_id)
    statuses = [c.status for c in dev.credentials.values()]
    assert statuses.count("active") == 1
    assert statuses.count("grace") == 1


# --------------------------------------------------------------------------
# Authentication failure reasons must be distinguishable
# --------------------------------------------------------------------------

def test_missing_vs_invalid_credential_reasons(client):
    device_id, _ = _register()

    missing = client.get(f"/devices/{device_id}/state/latest")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "missing_credential"

    invalid = client.get(
        f"/devices/{device_id}/state/latest",
        headers={"Authorization": "Bearer iotdev_not-a-real-token"},
    )
    assert invalid.status_code == 401
    assert invalid.json()["error"]["code"] == "invalid_credential"


def test_old_credential_works_within_grace_then_is_expired():
    """Deterministic clock: inside window -> OK; past window -> expired."""
    store = get_store()
    device_id, old_token = _register()

    result = services.rotate_credential(device_id, 3600)
    expires_at = result.retired.expires_at
    assert expires_at is not None

    # one second before the window ends the old token still authenticates
    before = security.authenticate(store, old_token, now=expires_at - timedelta(seconds=1))
    assert before.device_id == device_id

    # exactly at the window end -> distinguishable expiry, stays expired
    with pytest.raises(AuthenticationError) as exc:
        security.authenticate(store, old_token, now=expires_at)
    assert exc.value.code == "credential_expired"

    with pytest.raises(AuthenticationError) as exc2:
        security.authenticate(store, old_token, now=expires_at + timedelta(days=1))
    assert exc2.value.code == "credential_expired"


def test_second_rotation_within_window_revokes_intermediate_credential():
    store = get_store()
    device_id, t1 = _register()

    r1 = services.rotate_credential(device_id, 3600)
    t2 = r1.new.token
    r2 = services.rotate_credential(device_id, 3600)
    t3 = r2.new.token
    # After r2: t1 (still within its identical window) -> revoked,
    # t2 -> grace, t3 -> active. Only one grace credential exists.
    cred_t1 = store.resolve_token(security.hash_token(t1))[1]
    assert cred_t1.status == "revoked"
    assert security.authenticate(store, t2).device_id == device_id

    r3 = services.rotate_credential(device_id, 3600)
    t4 = r3.new.token

    # t2's window had not elapsed when r3 superseded it -> revoked
    with pytest.raises(AuthenticationError) as exc:
        security.authenticate(store, t2)
    assert exc.value.code == "credential_revoked"
    # t3 has just entered grace with a fresh window -> still accepted
    assert security.authenticate(store, t3).device_id == device_id

    # A fourth rotation revokes t3 (still inside its window) as well
    r4 = services.rotate_credential(device_id, 3600)
    t5 = r4.new.token
    with pytest.raises(AuthenticationError) as exc3:
        security.authenticate(store, t3)
    assert exc3.value.code == "credential_revoked"

    # newest credential keeps working and only one credential stays active
    assert security.authenticate(store, t5).device_id == device_id
    statuses = [c.status for c in get_store().require_device(device_id).credentials.values()]
    assert statuses.count("active") == 1
    assert statuses.count("grace") == 1


def test_grace_zero_means_old_credential_is_expired_immediately():
    store = get_store()
    device_id, old_token = _register()

    result = services.rotate_credential(device_id, 0)
    assert result.retired.expires_at is not None

    # any check at or after the zero-length window is refused as expired
    now = security.utc_now()
    with pytest.raises(AuthenticationError) as exc:
        security.authenticate(store, old_token, now=now)
    assert exc.value.code == "credential_expired"


def test_credential_of_one_device_cannot_access_another(client):
    d1, t1 = _register(name="d1")
    d2, _ = _register(name="d2")
    resp = client.get(
        f"/devices/{d2}/state/latest",
        headers={"Authorization": f"Bearer {t1}"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "invalid_credential"


def test_rotate_unknown_device_is_404(client):
    resp = client.post("/devices/does-not-exist/credentials/rotate", json={"grace_seconds": 10})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "device_not_found"
