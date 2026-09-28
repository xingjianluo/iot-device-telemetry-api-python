"""Telemetry ingestion: rejection reasons, dedup/ordering and query rules."""

from __future__ import annotations

import pytest

from app import services


def _register() -> tuple[str, str]:
    info, cred = services.register_device("sensor", {})
    return info.device_id, cred.token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _ts(minute: int, second: int = 0) -> str:
    return f"2026-09-28T10:{minute:02d}:{second:02d}Z"


# --------------------------------------------------------------------------
# Rejection reasons must be distinguishable and bad rows never stored
# --------------------------------------------------------------------------

def test_all_four_rejection_reasons_are_distinguishable(client):
    device_id, token = _register()

    # Seed one accepted point so regression/duplicate have something to hit.
    seed = client.post(
        f"/devices/{device_id}/telemetry",
        json={"entries": [{"ts": _ts(1), "metrics": {"temp": 20.0}}]},
        headers=_auth(token),
    )
    assert seed.json()["accepted"] == 1

    resp = client.post(
        f"/devices/{device_id}/telemetry",
        json={"entries": [
            {"metrics": {"temp": 1}},                       # 0 missing ts
            42,                                              # 1 not an object
            {"ts": _ts(3), "metrics": {}},                  # 2 empty metrics
            {"ts": 1700000000, "metrics": {"temp": 1}},     # 3 wrong ts type
            {"ts": "2026-09-28T10:04", "metrics": {"x": 1}},# 4 naive timestamp
            {"ts": _ts(4), "metrics": {"temp": "hot"}},     # 5 non-numeric
            {"ts": _ts(4), "metrics": {"temp": True}},      # 6 bool rejected
            {"ts": _ts(1), "metrics": {"temp": 9}},         # 7 duplicate stored
            {"ts": _ts(6), "metrics": {"a": 1.0}},          # 8 accepted
            {"ts": _ts(6), "metrics": {"a": 2.0}},          # 9 dup in batch
            {"ts": _ts(0), "metrics": {"a": 1.0}},          # 10 regression
        ]},
        headers=_auth(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["accepted"] == 1
    reasons = {item["index"]: item["reason"] for item in body["rejected_items"]}
    assert reasons[0] == "missing_field"
    assert reasons[1] == "missing_field"
    assert reasons[2] == "missing_field"
    assert reasons[3] == "type_mismatch"
    assert reasons[4] == "type_mismatch"
    assert reasons[5] == "type_mismatch"
    assert reasons[6] == "type_mismatch"
    assert reasons[7] == "duplicate"
    assert reasons[9] == "duplicate"
    assert reasons[10] == "timestamp_regression"

    # rejected rows never enter results: still 2 points, latest is t6
    latest = client.get(f"/devices/{device_id}/state/latest", headers=_auth(token)).json()
    assert latest["latest_ts"] == "2026-09-28T10:06:00Z"
    assert latest["count"] == 2


def test_duplicate_timestamps_within_batch_first_wins(client):
    device_id, token = _register()
    resp = client.post(
        f"/devices/{device_id}/telemetry",
        json={"entries": [
            {"ts": _ts(1), "metrics": {"temp": 1.0}},
            {"ts": _ts(1), "metrics": {"temp": 99.0}},
            {"ts": _ts(1), "metrics": {"temp": 50.0}},
        ]},
        headers=_auth(token),
    )
    body = resp.json()
    assert body["accepted"] == 1 and body["rejected"] == 2
    latest = client.get(f"/devices/{device_id}/state/latest", headers=_auth(token)).json()
    assert latest["metrics"] == {"temp": 1.0}  # first occurrence wins
    assert all(i["reason"] == "duplicate" for i in body["rejected_items"])


def test_shuffled_batch_converges_to_same_series(client):
    """[t2,t1] and [t1,t2] (both on fresh devices) converge to the same series."""
    payload_a = [
        {"ts": _ts(1), "metrics": {"temp": 1.0}},
        {"ts": _ts(2), "metrics": {"temp": 2.0}},
    ]
    payload_b = list(reversed(payload_a))

    results = []
    for payload in (payload_a, payload_b):
        device_id, token = _register()
        client.post(f"/devices/{device_id}/telemetry",
                    json={"entries": payload}, headers=_auth(token))
        history = client.get(f"/devices/{device_id}/state/history",
                             headers=_auth(token)).json()
        results.append([(p["ts"], p["metrics"]) for p in history["points"]])

    assert results[0] == results[1]
    assert [ts for ts, _ in results[0]] == [_ts(1), _ts(2)]


def test_timestamp_regression_after_commit_is_rejected(client):
    device_id, token = _register()
    client.post(f"/devices/{device_id}/telemetry",
                json={"entries": [{"ts": _ts(5), "metrics": {"temp": 5}}]},
                headers=_auth(token))
    # A later batch carrying a timestamp older than durable latest is a clock
    # regression and must be rejected (not silently stored, not silently lost).
    resp = client.post(f"/devices/{device_id}/telemetry",
                       json={"entries": [{"ts": _ts(4), "metrics": {"temp": 4}}]},
                       headers=_auth(token)).json()
    assert resp["accepted"] == 0
    assert resp["rejected_items"][0]["reason"] == "timestamp_regression"
    # rejected row must not change latest/history
    latest = client.get(f"/devices/{device_id}/state/latest", headers=_auth(token)).json()
    assert latest["metrics"] == {"temp": 5} and latest["count"] == 1


def test_empty_batch_is_rejected_at_request_validation(client):
    device_id, token = _register()
    resp = client.post(f"/devices/{device_id}/telemetry",
                       json={"entries": []}, headers=_auth(token))
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def test_history_range_is_inclusive_and_time_ordered(client):
    device_id, token = _register()
    client.post(f"/devices/{device_id}/telemetry", json={"entries": [
        {"ts": _ts(1), "metrics": {"v": 1}},
        {"ts": _ts(2), "metrics": {"v": 2}},
        {"ts": _ts(3), "metrics": {"v": 3}},
    ]}, headers=_auth(token))

    body = client.get(
        f"/devices/{device_id}/state/history",
        params={"start": _ts(2), "end": _ts(2)},
        headers=_auth(token),
    ).json()
    assert [p["ts"] for p in body["points"]] == [_ts(2)]

    body = client.get(
        f"/devices/{device_id}/state/history",
        params={"start": _ts(2)},
        headers=_auth(token),
    ).json()
    assert [p["ts"] for p in body["points"]] == [_ts(2), _ts(3)]


def test_latest_state_for_device_without_data(client):
    device_id, token = _register()
    body = client.get(f"/devices/{device_id}/state/latest", headers=_auth(token)).json()
    assert body["latest_ts"] is None and body["metrics"] == {} and body["count"] == 0


def test_history_requires_authentication(client):
    device_id, _ = _register()
    assert client.get(f"/devices/{device_id}/state/history").status_code == 401
