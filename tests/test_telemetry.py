"""遥测批量上报测试：拒绝原因可区分、重复/乱序落库一致。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import auth, log_exchange


def _ingest(client: TestClient, device_id: str, secret: str, items, caplog, name):
    resp = client.post(
        f"/devices/{device_id}/telemetry",
        headers=auth(secret),
        json={"items": items},
    )
    log_exchange(caplog, name, items, resp)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _results_by_index(body):
    return {r["index"]: r for r in body["results"]}


def test_bad_data_reasons_are_distinguishable(client, registered, caplog):
    device_id, secret = registered
    body = _ingest(
        client,
        device_id,
        secret,
        [
            {"metric": "temp", "value": 1.0},               # 缺 timestamp
            {"timestamp": 1, "value": 1.0},                # 缺 metric
            {"timestamp": 1, "metric": "temp"},            # 缺 value
            {"timestamp": "1", "metric": "temp", "value": 1.0},  # 类型错
            {"timestamp": True, "metric": "temp", "value": 1.0}, # bool 不接受
            {"timestamp": 1, "metric": "", "value": 1.0},  # metric 非法
            {"timestamp": 1, "metric": "t", "value": None},  # value 类型错
        ],
        caplog,
        "bad_data",
    )
    assert body["accepted_count"] == 0
    assert body["rejected_count"] == 7
    by_index = _results_by_index(body)
    assert by_index[0]["reason"] == "missing_field:timestamp"
    assert by_index[1]["reason"] == "missing_field:metric"
    assert by_index[2]["reason"] == "missing_field:value"
    assert by_index[3]["reason"] == "type_mismatch:timestamp"
    assert by_index[4]["reason"] == "type_mismatch:timestamp"
    assert by_index[5]["reason"] == "type_mismatch:metric"
    assert by_index[6]["reason"] == "type_mismatch:value"
    # 拒绝原因彼此可区分
    reasons = {by_index[i]["reason"] for i in range(7)}
    assert len(reasons) == 6  # index3/4 同为 timestamp 类型错误

    # NaN 能被 Python JSON 编出但不是合法有限数值 -> invalid_timestamp
    raw = '{"items":[{"timestamp": NaN, "metric": "t", "value": 1}]}'
    resp = client.post(
        f"/devices/{device_id}/telemetry",
        headers={**auth(secret), "Content-Type": "application/json"},
        content=raw,
    )
    log_exchange(caplog, "nan_timestamp", raw, resp)
    assert resp.status_code == 200
    rbody = resp.json()
    assert rbody["rejected_count"] == 1
    assert rbody["results"][0]["reason"] == "invalid_timestamp"

    # 被拒绝的数据不能算进任何查询结果
    state = client.get(
        f"/devices/{device_id}/state", headers=auth(secret)
    ).json()
    assert state["latest_ts"] is None and state["state"] == {}


def test_out_of_order_batch_produces_consistent_state(client, registered, caplog):
    device_id, secret = registered
    items = [
        {"timestamp": 30, "metric": "temp", "value": 30.0},
        {"timestamp": 10, "metric": "temp", "value": 10.0},
        {"timestamp": 20, "metric": "temp", "value": 20.0},
    ]
    body_forward = _ingest(client, device_id, secret, items, caplog, "forward")
    assert body_forward["accepted_count"] == 3

    state = client.get(f"/devices/{device_id}/state", headers=auth(secret)).json()
    assert state["latest_ts"] == 30.0
    assert state["state"]["temp"]["value"] == 30.0

    hist = client.get(
        f"/devices/{device_id}/history?start=0&end=100", headers=auth(secret)
    ).json()["points"]
    assert [(p["timestamp"], p["value"]) for p in hist] == [
        (10.0, 10.0),
        (20.0, 20.0),
        (30.0, 30.0),
    ]


def test_same_batch_out_of_order_equals_sorted_order(client, fake_clock, caplog):
    """同一批数据乱序到达与排序后到达，最终落库状态必须一致。"""
    def fresh_device():
        r = client.post("/devices", json={})
        assert r.status_code == 201
        return r.json()["device_id"], r.json()["secret"]

    def snapshot(device_id, secret):
        hist = client.get(
            f"/devices/{device_id}/history?start=0&end=9999", headers=auth(secret)
        ).json()["points"]
        return [(p["timestamp"], p["metric"], p["value"]) for p in hist]

    items = [
        {"timestamp": t, "metric": "temp", "value": float(t)}
        for t in (40, 10, 30, 20)
    ]
    id_a, sec_a = fresh_device()
    _ingest(client, id_a, sec_a, items, caplog, "shuffled")
    id_b, sec_b = fresh_device()
    _ingest(client, id_b, sec_b, list(reversed(items)), caplog, "reversed")
    assert snapshot(id_a, sec_a) == snapshot(id_b, sec_b) == [
        (10.0, "temp", 10.0),
        (20.0, "temp", 20.0),
        (30.0, "temp", 30.0),
        (40.0, "temp", 40.0),
    ]


def test_idempotent_duplicate_in_batch(client, registered, caplog):
    device_id, secret = registered
    body = _ingest(
        client,
        device_id,
        secret,
        [
            {"timestamp": 10, "metric": "temp", "value": 1.0},
            {"timestamp": 10, "metric": "temp", "value": 1.0},
            {"timestamp": 10, "metric": "temp", "value": 1.0},
        ],
        caplog,
        "dup_in_batch",
    )
    assert body["accepted_count"] == 1
    assert body["duplicate_count"] == 2
    assert all(r["reason"] == "duplicate_same_value"
               for i, r in _results_by_index(body).items() if i > 0)


def test_conflicting_duplicate_in_batch_rejects_all(client, registered, caplog):
    device_id, secret = registered
    # 同一键两个不同值：两条都拒绝，且与到达顺序无关
    body = _ingest(
        client,
        device_id,
        secret,
        [
            {"timestamp": 10, "metric": "temp", "value": 1.0},
            {"timestamp": 10, "metric": "temp", "value": 2.0},
        ],
        caplog,
        "dup_conflict",
    )
    assert body["accepted_count"] == 0
    assert body["rejected_count"] == 2
    assert all(r["reason"] == "duplicate_conflict" for r in body["results"])

    state = client.get(f"/devices/{device_id}/state", headers=auth(secret)).json()
    assert state["latest_ts"] is None


def test_repeated_upload_same_value_is_idempotent(client, registered, caplog):
    device_id, secret = registered
    first = _ingest(
        client, device_id, secret,
        [{"timestamp": 10, "metric": "temp", "value": 1.0}], caplog, "first",
    )
    again = _ingest(
        client, device_id, secret,
        [{"timestamp": 10, "metric": "temp", "value": 1.0}], caplog, "again",
    )
    assert first["accepted_count"] == 1
    assert again["accepted_count"] == 0 and again["duplicate_count"] == 1
    assert again["results"][0]["reason"] == "duplicate_same_value"


def test_conflicting_value_for_existing_point_rejected(client, registered, caplog):
    device_id, secret = registered
    _ingest(
        client, device_id, secret,
        [{"timestamp": 10, "metric": "temp", "value": 1.0}], caplog, "store",
    )
    body = _ingest(
        client, device_id, secret,
        [{"timestamp": 10, "metric": "temp", "value": 9.0}], caplog, "conflict",
    )
    assert body["rejected_count"] == 1
    assert body["results"][0]["reason"] == "duplicate_conflict"
    # 原值保持不变
    hist = client.get(
        f"/devices/{device_id}/history?start=0&end=100", headers=auth(secret)
    ).json()["points"]
    assert hist[0]["value"] == 1.0


def test_timestamp_regression_is_rejected_and_flagged(client, registered, caplog):
    device_id, secret = registered
    _ingest(
        client, device_id, secret,
        [{"timestamp": 100, "metric": "temp", "value": 1.0}], caplog, "t100",
    )
    body = _ingest(
        client, device_id, secret,
        [
            {"timestamp": 99, "metric": "temp", "value": 2.0},
            {"timestamp": 101, "metric": "temp", "value": 3.0},
        ],
        caplog,
        "regression_mixed",
    )
    by_index = _results_by_index(body)
    assert by_index[0]["outcome"] == "rejected"
    assert by_index[0]["reason"] == "timestamp_regression"
    assert by_index[1]["outcome"] == "accepted"
    # 被拒数据不落库
    hist = client.get(
        f"/devices/{device_id}/history?start=0&end=200", headers=auth(secret)
    ).json()["points"]
    assert [p["timestamp"] for p in hist] == [100.0, 101.0]


def test_history_range_inclusive_and_sorted(client, registered, caplog):
    device_id, secret = registered
    _ingest(
        client, device_id, secret,
        [
            {"timestamp": 10, "metric": "a", "value": 1.0},
            {"timestamp": 10, "metric": "b", "value": 2.0},
            {"timestamp": 20, "metric": "a", "value": 3.0},
        ],
        caplog,
        "seed",
    )
    r = client.get(
        f"/devices/{device_id}/history?start=10&end=10", headers=auth(secret)
    )
    points = r.json()["points"]
    assert [(p["timestamp"], p["metric"]) for p in points] == [(10.0, "a"), (10.0, "b")]
