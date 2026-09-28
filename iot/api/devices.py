"""设备注册与凭证管理路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models import (
    CredentialIssuedResponse,
    CredentialListResponse,
    RegisterDeviceRequest,
    RotateCredentialRequest,
)
from .dependencies import device_secret_header, get_state

router = APIRouter(prefix="/devices", tags=["devices"])


@router.post("", response_model=CredentialIssuedResponse, status_code=201)
def register_device(
    payload: RegisterDeviceRequest,
    state=Depends(get_state),
) -> dict:
    """注册设备并签发首个接入凭证。"""
    return state.devices.register(payload.device_id)


@router.post("/{device_id}/credentials/rotate", response_model=CredentialIssuedResponse)
def rotate_credential(
    device_id: str,
    payload: RotateCredentialRequest,
    secret: str | None = Depends(device_secret_header),
    state=Depends(get_state),
) -> dict:
    """轮换设备凭证：必须持有当前有效凭证，旧凭证进入宽限窗口。"""
    state.devices.authenticate(device_id, secret)
    return state.devices.rotate_credential(device_id, payload.grace_seconds)


@router.get("/{device_id}/credentials", response_model=CredentialListResponse)
def list_credentials(
    device_id: str,
    secret: str | None = Depends(device_secret_header),
    state=Depends(get_state),
) -> dict:
    """查看设备凭证列表（只返回 secret 前 8 位与状态，不返回完整 secret）。"""
    state.devices.authenticate(device_id, secret)
    return state.devices.list_credentials(device_id)
