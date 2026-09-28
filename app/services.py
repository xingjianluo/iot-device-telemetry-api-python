"""Service layer: transactional use cases on top of the local store.

Every state-changing operation performs its checks and commit while holding
the relevant device lock (inside store/security/telemetry helpers), so no
half-updated state is ever observable. Each operation logs the request input
and the decision basis at INFO level -- that is the "request input +
rationale" log surfaced by the test suite.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from app import security
from app.models import (
    CredentialView,
    DeviceInfo,
    HistoryPoint,
    LatestStateResponse,
    RotateResponse,
    TelemetryBatchResponse,
)
from app.store import CredentialRecord, InMemoryStore, TelemetryPoint, get_store
from app.telemetry import process_batch

logger = logging.getLogger("iot.services")


# -- views ------------------------------------------------------------------

def _credential_view(cred: CredentialRecord | None,
                     token: str | None = None,
                     device_id: str | None = None) -> CredentialView:
    if cred is None:
        # Only used when a device rotates before ever having a credential,
        # which registration makes impossible; kept for defensiveness.
        return CredentialView(
            credential_id="", device_id=device_id or "", token=None,
            issued_at=security.utc_now(), expires_at=None, status="none",
        )
    return CredentialView(
        credential_id=cred.credential_id,
        device_id=cred.device_id,
        token=token,
        issued_at=cred.issued_at,
        expires_at=cred.expires_at,
        status=cred.status,
    )


def _point_view(point: TelemetryPoint | None) -> HistoryPoint | None:
    if point is None:
        return None
    return HistoryPoint(ts=point.ts, metrics=point.metrics)


# -- use cases --------------------------------------------------------------

def register_device(name: str, metadata: dict,
                    store: InMemoryStore | None = None
                    ) -> tuple[DeviceInfo, CredentialView]:
    store = store or get_store()
    now = security.utc_now()
    device_id = "dev_" + secrets.token_hex(8)
    logger.info("register_device input: name=%r metadata=%r -> new device_id=%s",
                name, metadata, device_id)
    device = store.create_device(device_id, name, metadata, now)
    cred, token = security.issue_credential(store, device, now=now)
    logger.info(
        "register_device decision: device_id=%s credential_id=%s status=active "
        "(first credential, no rotation needed)", device_id, cred.credential_id,
    )
    info = DeviceInfo(
        device_id=device.device_id, name=device.name,
        metadata=device.metadata, created_at=device.created_at,
    )
    return info, _credential_view(cred, token)


def rotate_credential(device_id: str, grace_seconds: int,
                      store: InMemoryStore | None = None) -> RotateResponse:
    store = store or get_store()
    if grace_seconds < 0:
        # 0 is allowed (no compatibility window); negatives are invalid.
        raise ValueError("grace_seconds must be non-negative")
    logger.info("rotate_credential input: device_id=%s grace_seconds=%s",
                device_id, grace_seconds)
    device = store.require_device(device_id)
    now = security.utc_now()
    with device.lock:
        retired = security.retire_credential(device, grace_seconds, now=now)
        new_cred, new_token = security.issue_credential(store, device, now=now)
        statuses = {c.status for c in device.credentials.values()}
        active_count = sum(c.status == "active" for c in device.credentials.values())
        logger.info(
            "rotate_credential decision: device_id=%s new_credential_id=%s retired_id=%s "
            "retired_status=%s retired_expires_at=%s active_count=%d statuses=%s",
            device_id, new_cred.credential_id,
            retired.credential_id if retired else None,
            retired.status if retired else None,
            retired.expires_at.isoformat() if retired and retired.expires_at else None,
            active_count, sorted(statuses),
        )
        retired_view = _credential_view(retired)
        new_view = _credential_view(new_cred, new_token)
    return RotateResponse(
        device_id=device_id, new=new_view, retired=retired_view,
        grace_seconds=grace_seconds,
    )


def ingest_batch(device_id: str, raw_entries: list,
                 store: InMemoryStore | None = None) -> TelemetryBatchResponse:
    store = store or get_store()
    logger.info("ingest_batch input: device_id=%s batch_size=%d payload=%r",
                device_id, len(raw_entries), raw_entries)
    device = store.require_device(device_id)
    accepted, rejected = process_batch(device, raw_entries)
    logger.info(
        "ingest_batch decision: device_id=%s accepted=%d rejected=%d reasons=%r "
        "(rejected rows are not committed)",
        device_id, len(accepted), len(rejected),
        {r["reason"] for r in rejected},
    )
    for item in rejected:
        logger.info("ingest_batch rejected row: device_id=%s index=%d reason=%s message=%s",
                    device_id, item["index"], item["reason"], item["message"])
    latest = store.latest(device_id)
    return TelemetryBatchResponse(
        device_id=device_id,
        accepted=len(accepted),
        rejected=len(rejected),
        rejected_items=rejected,
        latest_ts=latest.ts if latest else None,
    )


def get_latest(device_id: str, store: InMemoryStore | None = None) -> LatestStateResponse:
    store = store or get_store()
    device = store.require_device(device_id)
    point = store.latest(device_id)
    logger.info("get_latest input: device_id=%s -> latest_ts=%s",
                device_id, point.ts.isoformat() if point else None)
    return LatestStateResponse(
        device_id=device_id,
        latest_ts=point.ts if point else None,
        metrics=point.metrics if point else {},
        count=len(device.points),
    )


def get_history(device_id: str, start: datetime | None, end: datetime | None,
                store: InMemoryStore | None = None):
    store = store or get_store()
    logger.info("get_history input: device_id=%s start=%s end=%s",
                device_id,
                start.isoformat() if start else None,
                end.isoformat() if end else None)
    points = store.history(device_id, start, end)
    logger.info("get_history decision: device_id=%s returned_points=%d",
                device_id, len(points))
    return {
        "device_id": device_id,
        "start": start,
        "end": end,
        "points": [_point_view(p) for p in points],
    }
