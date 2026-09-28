"""Telemetry ingestion and state query endpoints (device-authenticated)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import services
from app.dependencies import get_authenticated_device
from app.errors import AuthenticationError
from app.models import TelemetryBatchRequest
from app.store import DeviceRecord

router = APIRouter(prefix="/devices", tags=["telemetry"])


def _authorize_for_path(device: DeviceRecord, device_id: str) -> None:
    """A credential only grants access to the device it belongs to."""
    if device.device_id != device_id:
        raise AuthenticationError(
            f"credential belongs to device '{device.device_id}', not '{device_id}'",
            code="invalid_credential",
            status_code=403,
        )


def _parse_query_ts(raw: str | None, field_name: str) -> datetime | None:
    if raw is None:
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        from app.errors import AppError

        raise AppError(
            f"query parameter '{field_name}' must be ISO-8601 with timezone",
            code="invalid_query", status_code=400,
        ) from exc
    if dt.tzinfo is None:
        from app.errors import AppError

        raise AppError(
            f"query parameter '{field_name}' must carry a timezone",
            code="invalid_query", status_code=400,
        )
    return dt


@router.post("/{device_id}/telemetry")
def ingest(device_id: str, body: TelemetryBatchRequest,
           device: DeviceRecord = Depends(get_authenticated_device)) -> dict:
    """Batch-report telemetry.

    Per-row validation: bad rows are reported with a distinct reason and are
    never stored; the response summarizes accepted/rejected rows.
    """
    _authorize_for_path(device, device_id)
    result = services.ingest_batch(device_id, body.entries)
    return result.model_dump(mode="json")


@router.get("/{device_id}/state/latest")
def latest(device_id: str,
           device: DeviceRecord = Depends(get_authenticated_device)) -> dict:
    _authorize_for_path(device, device_id)
    return services.get_latest(device_id).model_dump(mode="json")


@router.get("/{device_id}/state/history")
def history(device_id: str, start: str | None = None, end: str | None = None,
            device: DeviceRecord = Depends(get_authenticated_device)) -> dict:
    """Return stored points in ``[start, end]`` (inclusive), time ordered."""
    _authorize_for_path(device, device_id)
    start_dt = _parse_query_ts(start, "start")
    end_dt = _parse_query_ts(end, "end")
    if start_dt and end_dt and start_dt > end_dt:
        from app.errors import AppError

        raise AppError("'start' must not be after 'end'",
                       code="invalid_query", status_code=400)
    result = services.get_history(device_id, start_dt, end_dt)
    return {
        "device_id": result["device_id"],
        "start": result["start"].isoformat() if result["start"] else None,
        "end": result["end"].isoformat() if result["end"] else None,
        "points": [p.model_dump(mode="json") for p in result["points"]],
    }
