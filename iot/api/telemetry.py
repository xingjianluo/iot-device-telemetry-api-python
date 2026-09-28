"""遥测上报与查询路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..models import (
    HistoryResponse,
    LatestStateResponse,
    TelemetryBatchRequest,
    TelemetryBatchResponse,
)
from .dependencies import device_secret_header, get_state

router = APIRouter(prefix="/devices", tags=["telemetry"])


@router.post("/{device_id}/telemetry", response_model=TelemetryBatchResponse)
def ingest_telemetry(
    device_id: str,
    payload: TelemetryBatchRequest,
    secret: str | None = Depends(device_secret_header),
    state=Depends(get_state),
) -> dict:
    """批量上报遥测。

    逐条返回 accepted / duplicate / rejected 判定及拒绝原因，
    被拒绝的数据不会落库，也不会出现在任何查询结果中。
    """
    state.devices.authenticate(device_id, secret)
    return state.telemetry.ingest(device_id, payload)


@router.get("/{device_id}/state", response_model=LatestStateResponse)
def latest_state(
    device_id: str,
    secret: str | None = Depends(device_secret_header),
    state=Depends(get_state),
) -> dict:
    """查询设备最新时间戳下各指标的最新值。"""
    state.devices.authenticate(device_id, secret)
    return state.telemetry.latest(device_id)


@router.get("/{device_id}/history", response_model=HistoryResponse)
def history(
    device_id: str,
    start: float = Query(..., description="起始时间戳（秒，含）"),
    end: float = Query(..., description="结束时间戳（秒，含）"),
    secret: str | None = Depends(device_secret_header),
    state=Depends(get_state),
) -> dict:
    """按时间区间查询历史遥测点（按 timestamp, metric 排序）。"""
    state.devices.authenticate(device_id, secret)
    return state.telemetry.history(device_id, start, end)
