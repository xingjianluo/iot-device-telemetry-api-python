"""凭证签发、轮换与宽限期测试：拒绝原因必须可区分。"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from .conftest import auth, log_exchange


def _rotate(client: TestClient, device_id: str, secret: str, grace: int | None, caplog):
    payload = {} if grace is None else {"grace_seconds": grace}
    resp = client.post(
        f"/devices/{device_id}/credentials/rotate",
        headers=auth(secret),
        json=payload,
    )
    log_exchange(caplog, "rotate", {"grace": grace}, resp)
    return resp


def test_register_issues_active_credential(client, registered, caplog):
    device_id, secret = registered
    resp = client.get(f"/devices/{device_id}/credentials", headers=auth(secret))
    log_exchange(caplog, "list_credentials", device_id, resp)
    assert resp.status_code == 200
    creds = resp.json()["credentials"]
    assert len(creds) == 1
    assert creds[0]["status"] == "active"
    assert creds[0]["secret_prefix"] == secret[:8]


def test_duplicate_device_id_rejected(client, registered, caplog):
    resp = client.post("/devices", json={"device_id": "dev-1"})
    log_exchange(caplog, "duplicate_register", {"device_id": "dev-1"}, resp)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "device_already_exists"


def test_rotate_keeps_old_secret_usable_during_grace(client, registered, fake_clock, caplog):
    device_id, old_secret = registered
    resp = _rotate(client, device_id, old_secret, 100, caplog)
    assert resp.status_code == 200
    new_secret = resp.json()["secret"]
    assert resp.json()["grace_expires_at"] == fake_clock.now() + 100

    # 宽限期内：新旧凭证都可访问
    for sec, via in [(old_secret, "grace"), (new_secret, "active")]:
        r = client.get(f"/devices/{device_id}/credentials", headers=auth(sec))
        log_exchange(caplog, f"access_{via}", {"prefix": sec[:8]}, r)
        assert r.status_code == 200

    listing = client.get(
        f"/devices/{device_id}/credentials", headers=auth(new_secret)
    ).json()["credentials"]
    by_prefix = {c["secret_prefix"]: c["status"] for c in listing}
    assert by_prefix[old_secret[:8]] == "grace"
    assert by_prefix[new_secret[:8]] == "active"
    # 任何时刻只有一个 active
    assert sum(1 for s in by_prefix.values() if s == "active") == 1


def test_expired_credential_is_rejected_with_distinct_reason(
    client, registered, fake_clock, caplog
):
    device_id, old_secret = registered
    new_secret = _rotate(client, device_id, old_secret, 100, caplog).json()["secret"]

    # 宽限期刚过：旧凭证被拒绝，且原因是 credential_expired
    fake_clock.advance(100)
    r = client.post(
        f"/devices/{device_id}/telemetry",
        headers=auth(old_secret),
        json={"items": []},
    )
    log_exchange(caplog, "expired_access", {"prefix": old_secret[:8]}, r)
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "credential_expired"
    assert body["error"]["details"]["grace_expires_at"] == fake_clock.now()

    # 新凭证不受影响
    r2 = client.get(f"/devices/{device_id}/credentials", headers=auth(new_secret))
    assert r2.status_code == 200


def test_bad_and_missing_credential_have_distinct_reasons(client, registered, caplog):
    device_id, _ = registered

    r_missing = client.get(f"/devices/{device_id}/credentials")
    log_exchange(caplog, "no_header", {}, r_missing)
    assert r_missing.status_code == 401
    assert r_missing.json()["error"]["code"] == "missing_credential"

    r_bad = client.get(
        f"/devices/{device_id}/credentials", headers=auth("deadbeef" * 8)
    )
    log_exchange(caplog, "bad_secret", {}, r_bad)
    assert r_bad.status_code == 401
    assert r_bad.json()["error"]["code"] == "bad_credential"

    assert r_missing.json()["error"]["code"] != r_bad.json()["error"]["code"]


def test_second_rotation_revokes_pending_grace_credential(
    client, registered, fake_clock, caplog
):
    device_id, secret1 = registered
    secret2 = _rotate(client, device_id, secret1, 1000, caplog).json()["secret"]
    # 紧接着第二次轮换：第一次留下的 grace 凭证必须立即失效（revoked）
    secret3 = _rotate(client, device_id, secret2, 1000, caplog).json()["secret"]

    listing = client.get(
        f"/devices/{device_id}/credentials", headers=auth(secret3)
    ).json()["credentials"]
    by_prefix = {c["secret_prefix"]: c for c in listing}
    statuses = [c["status"] for c in listing]
    log_exchange(caplog, "after_second_rotation", statuses, listing)

    assert by_prefix[secret1[:8]]["status"] == "revoked"
    assert by_prefix[secret2[:8]]["status"] == "grace"
    assert by_prefix[secret3[:8]]["status"] == "active"
    assert statuses.count("active") == 1
    assert statuses.count("grace") <= 1

    # 被撤销的凭证访问给出独立原因
    r = client.get(f"/devices/{device_id}/credentials", headers=auth(secret1))
    log_exchange(caplog, "revoked_access", {}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "credential_revoked"


def test_grace_zero_expires_old_immediately(client, registered, fake_clock, caplog):
    device_id, old_secret = registered
    new_secret = _rotate(client, device_id, old_secret, 0, caplog).json()["secret"]
    r = client.get(f"/devices/{device_id}/credentials", headers=auth(old_secret))
    log_exchange(caplog, "grace_zero_old", {}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "credential_expired"
    assert client.get(
        f"/devices/{device_id}/credentials", headers=auth(new_secret)
    ).status_code == 200


def test_rotate_requires_valid_credential(client, registered, caplog):
    device_id, secret = registered
    r = client.post(
        f"/devices/{device_id}/credentials/rotate", json={"grace_seconds": 10}
    )
    log_exchange(caplog, "rotate_without_credential", {}, r)
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credential"


def test_unknown_device_returns_not_found(client, caplog):
    r = client.get("/devices/ghost/state", headers=auth("x" * 32))
    log_exchange(caplog, "unknown_device", {}, r)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "device_not_found"
