"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DeviceRegisterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DeviceInfo(BaseModel):
    device_id: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class CredentialView(BaseModel):
    """A credential as returned to the device owner.

    ``token`` is only ever shown at issue/rotation time.
    """

    credential_id: str
    device_id: str
    # Plaintext token is present only for a freshly issued credential; views
    # of retired credentials carry ``None`` (tokens are never recoverable).
    token: str | None = None
    issued_at: datetime
    # None while active; set to the end of the grace window once retired.
    expires_at: datetime | None = None
    status: str  # active | grace | expired | revoked | none


class RotateResponse(BaseModel):
    device_id: str
    new: CredentialView
    retired: CredentialView
    grace_seconds: int


class TelemetryEntry(BaseModel):
    """Normalized telemetry point produced after per-entry validation."""

    ts: datetime
    metrics: dict[str, float]


class BatchRejectedItem(BaseModel):
    index: int
    reason: str  # missing_field | type_mismatch | timestamp_regression | duplicate
    message: str


class TelemetryBatchRequest(BaseModel):
    # Fully untyped on purpose: each entry is validated individually so a
    # single bad row (wrong type, missing field, ...) is rejected with a
    # categorized reason instead of making the whole batch fail with an
    # undifferentiated 422.
    entries: list[Any] = Field(min_length=1)


class TelemetryBatchResponse(BaseModel):
    device_id: str
    accepted: int
    rejected: int
    rejected_items: list[BatchRejectedItem]
    latest_ts: datetime | None = None


class LatestStateResponse(BaseModel):
    device_id: str
    latest_ts: datetime | None = None
    metrics: dict[str, float]
    count: int


class HistoryPoint(BaseModel):
    ts: datetime
    metrics: dict[str, float]


class HistoryResponse(BaseModel):
    device_id: str
    start: datetime | None = None
    end: datetime | None = None
    points: list[HistoryPoint]
