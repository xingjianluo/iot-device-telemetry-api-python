"""Edge-to-edge API behavior: validation, graceful auth during rotation."""

from __future__ import annotations

from app import services


def _register(client, name="sensor"):
    body = client.post("/devices", json={"name": name}).json()
    return body["device"]["device_id"], body["credential"]["token"]


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_rejects_blank_name(client):
    assert client.post("/devices", json={"name": ""}).status_code == 422
    assert client.post("/devices", json={}).status_code == 422


def test_old_token_can_ingest_during_grace_window_then_401(client):
    device_id, old = _register(client)
    rotate = client.post(f"/devices/{device_id}/credentials/rotate",
                         json={"grace_seconds": 2}, headers=_h(old)).json()
    new = rotate["new"]["token"]

    # old token still allowed to report during the compatibility window
    ok = client.post(f"/devices/{device_id}/telemetry",
                     json={"entries": [{"ts": "2026-09-28T00:00:00Z",
                                        "metrics": {"temp": 1}}]},
                     headers=_h(old))
    assert ok.status_code == 200 and ok.json()["accepted"] == 1
    assert client.post(f"/devices/{device_id}/telemetry",
                       json={"entries": [{"ts": "2026-09-28T00:01:00Z",
                                          "metrics": {"temp": 2}}]},
                       headers=_h(new)).status_code == 200

    import time
    time.sleep(2.1)
    expired = client.post(f"/devices/{device_id}/telemetry",
                          json={"entries": [{"ts": "2026-09-28T00:02:00Z",
                                             "metrics": {"temp": 3}}]},
                          headers=_h(old))
    assert expired.status_code == 401
    assert expired.json()["error"]["code"] == "credential_expired"


def test_history_query_param_validation(client):
    device_id, token = _register(client)
    bad = client.get(f"/devices/{device_id}/state/history?start=not-a-time",
                     headers=_h(token))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_query"

    order = client.get(
        f"/devices/{device_id}/state/history",
        params={"start": "2026-09-28T02:00:00Z", "end": "2026-09-28T01:00:00Z"},
        headers=_h(token),
    )
    assert order.status_code == 400 and order.json()["error"]["code"] == "invalid_query"


def test_unknown_device_access_rules(client):
    _, token = _register(client)
    # A valid credential for another device is refused before existence is
    # revealed (avoids leaking which device ids exist).
    assert client.get("/devices/nope/state/latest", headers=_h(token)).status_code == 403
    # Unauthenticated callers are rejected at the auth layer regardless.
    assert client.get("/devices/nope/state/latest").status_code == 401
