"""Device registration and credential rotation endpoints."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import services
from app.security import DEFAULT_GRACE_SECONDS

router = APIRouter(prefix="/devices", tags=["devices"])


class RegisterBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    metadata: dict = Field(default_factory=dict)


class RotateBody(BaseModel):
    # Compatibility window for the credential being retired.
    grace_seconds: int = Field(default=DEFAULT_GRACE_SECONDS, ge=0, le=24 * 3600)


@router.post("", status_code=201)
def register(body: RegisterBody) -> dict:
    """Register a device and receive its first credential (token shown once)."""
    info, credential = services.register_device(body.name, body.metadata)
    return {"device": info.model_dump(mode="json"), "credential": credential.model_dump(mode="json")}


@router.post("/{device_id}/credentials/rotate")
def rotate(device_id: str, body: RotateBody | None = None) -> dict:
    """Issue a new credential; the previous one enters the grace window.

    The retired credential is returned without a token (callers already hold
    it). Rotating again before the window ends revokes the intermediate
    credential immediately, so two credentials are never active together.
    """
    grace = body.grace_seconds if body is not None else DEFAULT_GRACE_SECONDS
    result = services.rotate_credential(device_id, grace)
    return result.model_dump(mode="json")
