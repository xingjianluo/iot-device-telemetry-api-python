"""并发读写同一设备的一致性测试。

使用同步 TestClient 从多线程发起请求：同步端点在线程池中真实并行执行，
因此能实际竞争每设备锁，验证不会出现半更新状态。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi.testclient import TestClient

from .conftest import auth, log_exchange


def _post_telemetry(client: TestClient, device_id: str, secret: str, ts: int):
    return client.post(
        f"/devices/{device_id}/telemetry",
        headers=auth(secret),
        json={"items": [{"timestamp": ts, "metric": "temp", "value": float(ts)}]},
    )


def test_concurrent_writes_all_or_nothing_and_no_half_state(
    client, registered, caplog
):
    device_id, secret = registered
    n = 30

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(_post_telemetry, client, device_id, secret, ts)
            for ts in range(1, n + 1)
        ]
        responses = [f.result() for f in as_completed(futures)]

    assert all(r.status_code == 200 for r in responses)
    accepted = [r.json()["accepted_count"] for r in responses]
    # 每个时间戳只有一个线程能落库，其余按时间戳倒退被拒；
    # 由于提交顺序基本递增，至少 1 条且至多 n 条 accepted，且总和可验证
    assert sum(accepted) >= 1
    log_exchange(
        caplog, "concurrent_writes",
        {"threads": 8, "items": n},
        {"accepted_total": sum(accepted)},
    )

    # 最终状态必须完整且自洽：水位等于最大成功时间戳，该点值与之一致
    state = client.get(
        f"/devices/{device_id}/state", headers=auth(secret)
    ).json()
    latest_ts = state["latest_ts"]
    assert state["state"]["temp"]["value"] == float(latest_ts)
    assert state["state"]["temp"]["timestamp"] == latest_ts

    hist = client.get(
        f"/devices/{device_id}/history?start=0&end=99999", headers=auth(secret)
    ).json()["points"]
    stored_ts = sorted(p["timestamp"] for p in hist)
    # 水位必须是被接受过的最大时间戳；所有已存点 <= 水位，且无重复键
    assert stored_ts[-1] == latest_ts
    assert stored_ts == sorted(set(stored_ts))
    assert all(t <= latest_ts for t in stored_ts)
    # 值与时间戳严格对应（没有被并发写撕裂成别的值）
    assert all(p["value"] == p["timestamp"] for p in hist)


def test_concurrent_reads_never_observe_half_updated_state(
    client, registered, caplog
):
    device_id, secret = registered
    # 预置一批数据
    for ts in (1, 2, 3):
        r = _post_telemetry(client, device_id, secret, ts)
        assert r.status_code == 200

    stop = threading.Event()
    errors: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            state = client.get(
                f"/devices/{device_id}/state", headers=auth(secret)
            ).json()
            latest = state["latest_ts"]
            if latest is None:
                continue
            temp = state["state"].get("temp")
            # 不变量：有水位就一定有 temp 点，且 value == timestamp（无半更新）
            if temp is None or temp["value"] != float(temp["timestamp"]):
                errors.append(f"torn state: {state}")
            if temp is not None and temp["timestamp"] != latest:
                errors.append(f"latest_ts disagrees with state: {state}")

    def writer(start: int) -> None:
        for ts in range(start, start + 20):
            _post_telemetry(client, device_id, secret, ts)

    with ThreadPoolExecutor(max_workers=6) as pool:
        readers = [pool.submit(reader) for _ in range(4)]
        writers = [pool.submit(writer, s) for s in (4, 24, 44)]
        for f in writers:
            f.result()
        stop.set()
        for f in readers:
            f.result()

    log_exchange(
        caplog, "concurrent_read_write",
        {"readers": 4, "writers": 3},
        {"errors": errors},
    )
    assert errors == []

    hist = client.get(
        f"/devices/{device_id}/history?start=0&end=99999", headers=auth(secret)
    ).json()["points"]
    assert all(p["value"] == p["timestamp"] for p in hist)


def test_concurrent_writes_with_duplicate_conflict_never_partial(
    client, registered, caplog
):
    """多个线程同时对同一 (timestamp, metric) 写不同值：

    只能有一个值落库（不能两个值都出现），其余全部被拒绝。"""
    device_id, secret = registered
    ts = 100

    def write(value: float):
        return client.post(
            f"/devices/{device_id}/telemetry",
            headers=auth(secret),
            json={"items": [
                {"timestamp": ts, "metric": "temp", "value": value}
            ]},
        )

    barrier = threading.Barrier(6)

    def go(value: float):
        barrier.wait()  # 尽量同时打进去
        return write(value)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(go, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))

    outcomes = [r.json()["results"][0] for r in responses]
    accepted = [o for o in outcomes if o["outcome"] == "accepted"]
    rejected = [o for o in outcomes if o["reason"] == "duplicate_conflict"]
    log_exchange(
        caplog, "same_key_conflict", {"writers": 6, "timestamp": ts},
        {"accepted": len(accepted), "conflict": len(rejected)},
    )
    assert len(accepted) == 1
    assert len(rejected) == 5

    hist = client.get(
        f"/devices/{device_id}/history?start=ts&end=100".replace("ts", "0"),
        headers=auth(secret),
    ).json()["points"]
    at_ts = [p for p in hist if p["timestamp"] == float(ts)]
    assert len(at_ts) == 1
    assert at_ts[0]["value"] in {1.0, 2.0, 3.0, 4.0, 5.0, 6.0}
