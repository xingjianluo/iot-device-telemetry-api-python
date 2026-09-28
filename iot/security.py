"""设备接入凭证的签发、轮换与状态判定。

兼容规则：
- 同一设备任何时刻最多只有一个 active 凭证；
- 轮换后旧 active 凭证进入 grace 状态，宽限窗口内仍可访问；
- 再次轮换会让上一个 grace 凭证立即 revoked（旧 secret 立刻失效）；
- grace 到期后变为 expired，访问被拒绝且拒绝原因可区分。
"""
from __future__ import annotations

import hmac
import secrets

from .clock import Clock, SystemClock
from .models import Credential, DeviceState

DEFAULT_GRACE_SECONDS = 600


def generate_secret() -> str:
    """生成一个随机接入密钥（hex 编码 32 字节）。"""
    return secrets.token_hex(32)


def secret_prefix(secret: str) -> str:
    return secret[:8]


class CredentialManager:
    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or SystemClock()

    # -- 签发 -------------------------------------------------------------

    def issue(self, device: DeviceState) -> Credential:
        """设备注册时签发首个凭证。"""
        assert not device.credentials, "device already has a credential"
        cred = Credential(secret=generate_secret(), issued_at=self.clock.now())
        device.credentials[cred.secret] = cred
        return cred

    # -- 轮换 -------------------------------------------------------------

    def rotate(
        self,
        device: DeviceState,
        grace_seconds: int | None = None,
    ) -> tuple[Credential, Credential | None, int]:
        """轮换凭证。

        - 当前 active 凭证在 grace_seconds 窗口内进入 grace（仍可访问）；
        - 若设备已有 grace 凭证（上一轮换留下），它会被立即 revoked，
          从而保证任何时刻最多只有一个 active、至多一个 grace；
        - grace_seconds=0 时旧凭证立即 expired。

        Returns:
            (新 active 凭证, 进入宽限期的旧凭证或 None, 实际宽限秒数)
        """
        now = self.clock.now()
        grace = DEFAULT_GRACE_SECONDS if grace_seconds is None else grace_seconds

        old_active: Credential | None = None
        for cred in device.credentials.values():
            if cred.status == "active":
                old_active = cred
            elif cred.status == "grace":
                # 上一次轮换的旧凭证：新轮换一旦发生立即作废
                cred.status = "revoked"
                cred.grace_expires_at = None

        if old_active is not None:
            if grace > 0:
                old_active.status = "grace"
                old_active.rotated_at = now
                old_active.grace_expires_at = now + grace
                grace_cred: Credential | None = old_active
            else:
                old_active.status = "expired"
                old_active.rotated_at = now
                old_active.grace_expires_at = now
                grace_cred = None

        new_cred = Credential(secret=generate_secret(), issued_at=now)
        device.credentials[new_cred.secret] = new_cred
        return new_cred, grace_cred, grace

    # -- 状态刷新与查找 ----------------------------------------------------

    def refresh_status(self, device: DeviceState) -> None:
        """根据当前时间把到期的 grace 凭证标记为 expired。"""
        now = self.clock.now()
        for cred in device.credentials.values():
            if (
                cred.status == "grace"
                and cred.grace_expires_at is not None
                and now >= cred.grace_expires_at
            ):
                cred.status = "expired"

    def find_active(self, device: DeviceState) -> Credential | None:
        self.refresh_status(device)
        for cred in device.credentials.values():
            if cred.status == "active":
                return cred
        return None

    def find_by_secret(
        self, device: DeviceState, provided_secret: str
    ) -> Credential | None:
        """用常量时间比较按 secret 查找，避免时序侧信道。"""
        match: Credential | None = None
        for cred in device.credentials.values():
            if hmac.compare_digest(cred.secret, provided_secret):
                match = cred
        if match is not None:
            self.refresh_status(device)
        return match
