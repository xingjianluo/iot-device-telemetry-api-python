"""错误类型与错误码定义。"""
from __future__ import annotations

import enum


class ErrorCode(str, enum.Enum):
    """所有可区分的拒绝原因。"""

    DEVICE_NOT_FOUND = "device_not_found"
    DEVICE_ALREADY_EXISTS = "device_already_exists"
    MISSING_CREDENTIAL = "missing_credential"
    BAD_CREDENTIAL = "bad_credential"
    CREDENTIAL_EXPIRED = "credential_expired"
    CREDENTIAL_REVOKED = "credential_revoked"
    MISSING_FIELD = "missing_field"
    TYPE_MISMATCH = "type_mismatch"
    INVALID_TIMESTAMP = "invalid_timestamp"
    TIMESTAMP_REGRESSION = "timestamp_regression"
    DUPLICATE_CONFLICT = "duplicate_conflict"


class AppError(Exception):
    """业务异常基类，携带可区分的错误码。"""

    status_code: int = 400

    def __init__(self, code: ErrorCode, message: str, details: dict | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


class AuthError(AppError):
    """接入认证类错误（401）。"""

    status_code = 401


class TelemetryValidationError(AppError):
    """遥测数据规则类错误（400，按条目返回）。"""

    status_code = 400


class NotFoundError(AppError):
    """资源不存在（404）。"""

    status_code = 404


class ConflictError(AppError):
    """冲突（409）。"""

    status_code = 409
