"""Concurrent reads/writes against the same device.

The store serializes per-device mutations behind an RLock and classifies +
commits each batch atomically. These tests assert the observable guarantees
independent of thread scheduling:

* no half-updated state: concurrent readers always see a sorted, self
  consistent snapshot (latest == history tail);
* every committed timestamp appears exactly once -- concurrent writes never
  lose or duplicate a point;
* only accepted rows are counted; duplicate/regression rows are rejected and
  never enter query results.

Timestamps are handed out by a shared monotonic allocator so that *whether* a
row is acceptable never depends on scheduling: allocation order == durable
time order.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app import services
from app.store import get_store
from app.telemetry import process_batch

BASE = datetime(2026, 9, 28, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _register() -> str:
    info, _ = services.register_device("sensor", {})
    return info.device_id


class MonotonicClock:
    """Hands out strictly increasing timestamps across all threads."""

    def __init__(self) -> None:
        self._next_id = 0
        self._lock = threading.Lock()

    def take(self, n: int) -> list[datetime]:
        with self._lock:
            start = self._next_id
            self._next_id += n  # contiguous reservation, no gaps/overlaps
        return [BASE + timedelta(seconds=start + i) for i in range(n)]


def test_concurrent_writers_never_lose_or_duplicate_points():
    device_id = _register()
    device = get_store().require_device(device_id)
    clock = MonotonicClock()
    errors: list[BaseException] = []
    writer_count = 12
    writes_per_writer = 50

    def worker(_tid: int) -> None:
        for _ in range(writes_per_writer):
            # Allocate the timestamp *inside* the device lock so allocation
            # order == commit order regardless of scheduling: the point is
            # then guaranteed newer than durable state and must be accepted.
            with get_store().with_device_lock(device_id) as locked:
                ts = clock.take(1)[0]
                try:
                    accepted, rejected = process_batch(
                        locked, [{"ts": _iso(ts), "metrics": {"v": ts.second}}]
                    )
                    assert len(accepted) == 1
                    assert rejected == []
                except BaseException as exc:  # noqa: BLE001 - surfaced below
                    errors.append(exc)

    with ThreadPoolExecutor(max_workers=writer_count) as pool:
        list(pool.map(worker, range(writer_count)))

    assert errors == []
    record = get_store().require_device(device_id)
    with record.lock:
        timestamps = sorted(record.points)
    expected = writer_count * writes_per_writer
    assert len(timestamps) == expected
    assert len(set(timestamps)) == expected  # no duplicates / torn writes
    latest = services.get_latest(device_id)
    assert latest.latest_ts == timestamps[-1]
    assert latest.count == expected


def test_concurrent_readers_always_see_consistent_snapshots():
    device_id = _register()
    device = get_store().require_device(device_id)
    clock = MonotonicClock()
    stop = threading.Event()
    violations: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            # Single-lock atomic snapshot: a reader can never observe a mix
            # of a new latest with an old history (no half-updated state).
            latest, points = get_store().snapshot(device_id)
            tss = [p.ts for p in points]
            if tss != sorted(tss):
                violations.append("history not sorted")
                return
            if points and latest is not None and latest.ts != tss[-1]:
                violations.append("latest disagrees with history tail")
                return
            if latest is not None and any(ts > latest.ts for ts in tss):
                violations.append("point newer than latest")
                return

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    def writer() -> None:
        # Timestamp allocation and commit share one critical section.
        with get_store().with_device_lock(device_id) as locked:
            ts = clock.take(1)[0]
            process_batch(locked, [{"ts": _iso(ts), "metrics": {"v": ts.second}}])

    total_writes = 16 * 25
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(writer) for _ in range(total_writes)]
        for f in futures:
            f.result()
    stop.set()
    for t in readers:
        t.join()

    assert violations == [], violations
    assert services.get_latest(device_id).count == total_writes


def test_concurrent_duplicate_and_regression_rows_are_never_committed():
    device_id = _register()
    device = get_store().require_device(device_id)
    clock = MonotonicClock()
    accepted_lock = threading.Lock()
    total_accepted = 0

    # Seed durable state first so later duplicate/regression rows have a target.
    seed_ts = clock.take(100)
    process_batch(device, [
        {"ts": _iso(ts), "metrics": {"v": 1.0}} for ts in seed_ts
    ])
    seed_max = seed_ts[-1]

    def good_writer() -> None:
        nonlocal total_accepted
        # Allocation + commit in one critical section => schedule-proof.
        with get_store().with_device_lock(device_id) as locked:
            ts = clock.take(1)[0]
            accepted, rejected = process_batch(
                locked, [{"ts": _iso(ts), "metrics": {"v": 2.0}}]
            )
            assert len(accepted) == 1
            assert rejected == []
            with accepted_lock:
                total_accepted += len(accepted)

    def bad_writer() -> None:
        # Mix valid-shape bad rows: duplicate (a seeded ts) and regression
        # (fixed old timestamp). None may be committed.
        accepted, rejected = process_batch(device, [
            {"ts": _iso(seed_ts[0]), "metrics": {"v": 9.0}},               # duplicate
            {"ts": _iso(BASE - timedelta(seconds=1)), "metrics": {"v": 1}},  # regression
            {"ts": "not-a-time", "metrics": {"v": 1}},                    # type
            {"metrics": {"v": 1}},                                        # missing
        ])
        assert accepted == []
        reasons = {r["reason"] for r in rejected}
        assert reasons == {"duplicate", "timestamp_regression", "type_mismatch", "missing_field"}

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = []
        for _ in range(80):
            futures.append(pool.submit(good_writer))
        for _ in range(8):
            futures.append(pool.submit(bad_writer))
        for f in futures:
            f.result()

    record = get_store().require_device(device_id)
    with record.lock:
        committed = dict(record.points)
    # 100 seeded + 80 fresh single-point writes; nothing else.
    assert total_accepted == 80
    assert len(committed) == 100 + total_accepted
    assert BASE - timedelta(seconds=1) not in committed
    # Seeded values must not be overwritten by duplicate rows.
    assert committed[seed_ts[0]]["v"] == 1.0
    assert committed[seed_max]["v"] == 1.0
