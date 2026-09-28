"""Thread-safe in-process state store (local memory only).

No database, message broker or cloud service is used, and no real
credentials are needed anywhere.

Concurrency model
-----------------
Every device owns a re-entrant lock. A telemetry batch is validated *and*
committed while holding that lock, and every read takes it too, so concurrent
clients can never observe a half-updated state. Two different devices use
different locks and therefore do not contend.

Invariants
----------
* at most one credential per device has status ``active``;
* a token hash resolves to at most one (device, credential);
* telemetry is stored as one metrics dict per timestamp (UTC), sorted on read;
* rejected rows never enter the store.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.errors import DeviceNotFoundError


@dataclass
class CredentialRecord:
    credential_id: str
    device_id: str
    token_hash: str
    issued_at: datetime
    # None while active; set to the end of the compatibility window once retired.
    expires_at: datetime | None
    status: str  # active | grace | expired | revoked


@dataclass
class TelemetryPoint:
    ts: datetime
    metrics: dict[str, float]


@dataclass
class DeviceRecord:
    device_id: str
    name: str
    metadata: dict
    created_at: datetime
    active_credential_id: str | None = None
    credentials: dict[str, CredentialRecord] = field(default_factory=dict)
    points: dict[datetime, dict[str, float]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


class InMemoryStore:
    def __init__(self) -> None:
        self._devices: dict[str, DeviceRecord] = {}
        self._token_index: dict[str, tuple[str, str]] = {}
        self._registry_lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        with self._registry_lock:
            self._devices.clear()
            self._token_index.clear()

    # -- devices -----------------------------------------------------------

    def create_device(self, device_id: str, name: str, metadata: dict,
                      created_at: datetime) -> DeviceRecord:
        record = DeviceRecord(device_id=device_id, name=name,
                              metadata=dict(metadata), created_at=created_at)
        with self._registry_lock:
            self._devices[device_id] = record
        return record

    def get_device(self, device_id: str) -> DeviceRecord | None:
        return self._devices.get(device_id)

    def require_device(self, device_id: str) -> DeviceRecord:
        record = self.get_device(device_id)
        if record is None:
            raise DeviceNotFoundError(f"device '{device_id}' does not exist")
        return record

    # -- credentials -------------------------------------------------------

    def index_token(self, token_hash: str, device_id: str, credential_id: str) -> None:
        self._token_index[token_hash] = (device_id, credential_id)

    def unindex_token(self, token_hash: str) -> None:
        self._token_index.pop(token_hash, None)

    def resolve_token(self, token_hash: str):
        indexed = self._token_index.get(token_hash)
        if indexed is None:
            return None
        device_id, credential_id = indexed
        device = self._devices.get(device_id)
        if device is None:
            return None
        cred = device.credentials.get(credential_id)
        if cred is None or cred.token_hash != token_hash:
            return None
        return device, cred

    # -- telemetry ---------------------------------------------------------

    def append_points(self, device: DeviceRecord, points: list[TelemetryPoint]) -> None:
        """Commit already-validated points (caller holds/uses device lock)."""
        with device.lock:
            for point in points:
                device.points[point.ts] = dict(point.metrics)

    def latest(self, device_id: str) -> TelemetryPoint | None:
        device = self.require_device(device_id)
        with device.lock:
            if not device.points:
                return None
            ts = max(device.points)
            return TelemetryPoint(ts=ts, metrics=dict(device.points[ts]))

    def history(self, device_id: str, start: datetime | None,
                end: datetime | None) -> list[TelemetryPoint]:
        device = self.require_device(device_id)
        with device.lock:
            return self._history_locked(device, start, end)

    @staticmethod
    def _history_locked(device: DeviceRecord, start: datetime | None,
                        end: datetime | None) -> list[TelemetryPoint]:
        result = []
        for ts in sorted(device.points):
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            result.append(TelemetryPoint(ts=ts, metrics=dict(device.points[ts])))
        return result

    def snapshot(self, device_id: str) -> tuple[TelemetryPoint | None, list[TelemetryPoint]]:
        """Atomically read (latest, full sorted history) under one lock."""
        device = self.require_device(device_id)
        with device.lock:
            ordered = self._history_locked(device, None, None)
            latest = ordered[-1] if ordered else None
            return latest, ordered

    def with_device_lock(self, device_id: str) -> "_DeviceLock":
        """Hold the device lock across timestamp allocation + commit."""
        return _DeviceLock(self.require_device(device_id))


class _DeviceLock:
    def __init__(self, device: DeviceRecord) -> None:
        self._device = device

    def __enter__(self) -> DeviceRecord:
        self._device.lock.acquire()
        return self._device

    def __exit__(self, exc_type, exc, tb) -> None:
        self._device.lock.release()


_store: InMemoryStore | None = None
_store_lock = threading.Lock()


def get_store() -> InMemoryStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = InMemoryStore()
    return _store
