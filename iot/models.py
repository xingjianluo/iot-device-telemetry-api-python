"""领域模型与 API 请求/响应数据结构。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 内部领域模型
# ---------------------------------------------------------------------------

CredentialStatus = Literal["active", "grace", "expired", "revoked"]


@dataclass
class Credential:
    """设备接入凭证。

    状态流转：active（唯一生效） -> 轮换后 grace（宽限期内仍可用）
    -> expired（宽限期结束，拒绝）；再次轮换会使上一个 grace 凭证立即 revoked。
    """

    secret: str
    issued_at: float
    status: CredentialStatus = "active"
    rotated_at: float | None = None
    grace_expires_at: float | None = None

    def public_view(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issued_at": self.issued_at,
            "rotated_at": self.rotated_at,
            "grace_expires_at": self.grace_expires_at,
        }


@dataclass
class TelemetryPoint:
    """一条已落库的遥测数据，按 (timestamp, metric) 唯一。"""

    timestamp: float
    metric: str
    value: float
    received_at: float


@dataclass
class DeviceState:
    """一台设备的全部本地状态，所有字段都在设备锁保护下访问。"""

    device_id: str
    created_at: float
    credentials: dict[str, Credential] = field(default_factory=dict)
    points: dict[tuple[float, str], TelemetryPoint] = field(default_factory=dict)
    latest_ts: float | None = None


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class RegisterDeviceRequest(BaseModel):
    # 不传则由服务端生成
    device_id: str | None = Field(default=None, min_length=1, max_length=128)


class RotateCredentialRequest(BaseModel):
    # 旧凭证宽限窗口（秒），None 时使用服务端默认值；0 表示立即失效
    grace_seconds: int | None = Field(default=None, ge=0, le=365 * 24 * 3600)


class TelemetryBatchRequest(BaseModel):
    # 故意用宽松类型接收，逐条手工校验以便精确区分拒绝原因
    items: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class CredentialIssuedResponse(BaseModel):
    device_id: str
    secret: str
    status: CredentialStatus
    issued_at: float
    grace_expires_at: float | None = None
    grace_seconds: int | None = None


class CredentialInfo(BaseModel):
    secret_prefix: str
    status: CredentialStatus
    issued_at: float
    rotated_at: float | None = None
    grace_expires_at: float | None = None


class CredentialListResponse(BaseModel):
    device_id: str
    credentials: list[CredentialInfo]


class TelemetryItemResult(BaseModel):
    index: int
    # accepted=落库；duplicate=幂等重复未重复落库；rejected=被规则拒绝
    outcome: Literal["accepted", "duplicate", "rejected"]
    reason: str
    timestamp: float | None = None
    metric: str | None = None


class TelemetryBatchResponse(BaseModel):
    device_id: str
    accepted_count: int
    duplicate_count: int
    rejected_count: int
    results: list[TelemetryItemResult]


class TelemetryPointResponse(BaseModel):
    timestamp: float
    metric: str
    value: float
    received_at: float


class LatestStateResponse(BaseModel):
    device_id: str
    latest_ts: float | None
    state: dict[str, TelemetryPointResponse]


class HistoryResponse(BaseModel):
    device_id: str
    points: list[TelemetryPointResponse]


def is_finite_number(value: Any) -> bool:
    """严格的有限数值判断：bool 不算数值，NaN/Inf 不算合法。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))
