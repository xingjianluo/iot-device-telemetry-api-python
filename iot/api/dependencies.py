"""API 共享依赖。"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Header, Request

from ..clock import Clock, SystemClock
from ..service import DeviceService, TelemetryService
from ..storage import DeviceStore


@dataclass
class AppState:
    store: DeviceStore
    clock: Clock
    devices: DeviceService
    telemetry: TelemetryService


def build_state(clock: Clock | None = None) -> AppState:
    clock = clock or SystemClock()
    store = DeviceStore()
    return AppState(
        store=store,
        clock=clock,
        devices=DeviceService(store, clock),
        telemetry=TelemetryService(store, clock),
    )


def get_state(request: Request) -> AppState:
    return request.app.state.state  # type: ignore[no-any-return]


def device_secret_header(
    x_device_secret: str | None = Header(default=None),
) -> str | None:
    """设备凭证请求头，缺失时为 None（由服务层给出可区分原因）。"""
    return x_device_secret
