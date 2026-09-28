"""设备与遥测业务逻辑。

所有会改变设备状态的操作都在该设备的锁内完成（store.device(...)），
因此并发读写同一设备时，读到的要么是更新前、要么是更新后的完整状态。
"""
from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass

from .clock import Clock, SystemClock
from .errors import AuthError, ConflictError, ErrorCode
from .models import (
    TelemetryBatchRequest,
    TelemetryPoint,
    TelemetryPointResponse,
    is_finite_number,
)
from .security import CredentialManager
from .storage import DeviceStore

logger = logging.getLogger("iot.service")

# 校验顺序固定，保证拒绝原因稳定、可区分
FIELD_ORDER = ("timestamp", "metric", "value")


@dataclass
class ItemVerdict:
    index: int
    outcome: str  # accepted | duplicate | rejected
    reason: str
    timestamp: float | None = None
    metric: str | None = None


class DeviceService:
    def __init__(
        self,
        store: DeviceStore,
        clock: Clock | None = None,
        credentials: CredentialManager | None = None,
    ) -> None:
        self.clock = clock or SystemClock()
        self.store = store
        self.credentials = credentials or CredentialManager(self.clock)

    # -- 注册 -------------------------------------------------------------

    def register(self, device_id: str | None) -> dict:
        new_id = device_id or f"dev-{secrets.token_hex(8)}"
        state = self.store.create_device(new_id, self.clock.now())
        if state is None:
            raise ConflictError(
                ErrorCode.DEVICE_ALREADY_EXISTS,
                f"device '{new_id}' already exists",
            )
        # create_device 之后只有本调用能拿到这台新设备，锁内签发即可
        with self.store.device(new_id) as device:
            cred = self.credentials.issue(device)
        logger.info("register device_id=%s secret_prefix=%s", new_id, cred.secret[:8])
        return {
            "device_id": new_id,
            "secret": cred.secret,
            "status": cred.status,
            "issued_at": cred.issued_at,
            "grace_expires_at": None,
            "grace_seconds": None,
        }

    # -- 鉴权 -------------------------------------------------------------

    def authenticate(self, device_id: str, provided_secret: str | None) -> None:
        """校验设备凭证；失败抛出原因可区分的 AuthError。"""
        with self.store.device(device_id) as device:
            if provided_secret is None or provided_secret == "":
                logger.info("auth deny device_id=%s reason=missing_credential", device_id)
                raise AuthError(
                    ErrorCode.MISSING_CREDENTIAL,
                    "missing device credential header 'X-Device-Secret'",
                )
            cred = self.credentials.find_by_secret(device, provided_secret)
            if cred is None:
                logger.info("auth deny device_id=%s reason=bad_credential", device_id)
                raise AuthError(
                    ErrorCode.BAD_CREDENTIAL,
                    "invalid device secret",
                )
            if cred.status == "expired":
                logger.info(
                    "auth deny device_id=%s reason=credential_expired rotated_at=%s",
                    device_id, cred.rotated_at,
                )
                raise AuthError(
                    ErrorCode.CREDENTIAL_EXPIRED,
                    "credential grace window has expired; rotate required",
                    details={"grace_expires_at": cred.grace_expires_at},
                )
            if cred.status == "revoked":
                logger.info("auth deny device_id=%s reason=credential_revoked", device_id)
                raise AuthError(
                    ErrorCode.CREDENTIAL_REVOKED,
                    "credential was revoked by a newer rotation",
                )
            logger.info(
                "auth allow device_id=%s via=%s secret_prefix=%s",
                device_id, cred.status, cred.secret[:8],
            )

    # -- 凭证轮换 / 查询 --------------------------------------------------

    def rotate_credential(self, device_id: str, grace_seconds: int | None) -> dict:
        with self.store.device(device_id) as device:
            if self.credentials.find_active(device) is None:
                # 当前设计不会发生：注册必有 active，写防御性分支
                raise ConflictError(
                    ErrorCode.CREDENTIAL_REVOKED,
                    "device has no active credential",
                )
            new_cred, grace_cred, grace = self.credentials.rotate(
                device, grace_seconds
            )
            logger.info(
                "rotate device_id=%s grace_seconds=%s new_prefix=%s old_status=%s "
                "old_expires=%s",
                device_id,
                grace,
                new_cred.secret[:8],
                grace_cred.status if grace_cred else "expired",
                grace_cred.grace_expires_at if grace_cred else self.clock.now(),
            )
            return {
                "device_id": device_id,
                "secret": new_cred.secret,
                "status": new_cred.status,
                "issued_at": new_cred.issued_at,
                "grace_expires_at": (
                    grace_cred.grace_expires_at if grace_cred else new_cred.issued_at
                ),
                "grace_seconds": grace,
            }

    def list_credentials(self, device_id: str) -> dict:
        with self.store.device(device_id) as device:
            self.credentials.refresh_status(device)
            infos = [
                {
                    "secret_prefix": cred.secret[:8],
                    "status": cred.status,
                    "issued_at": cred.issued_at,
                    "rotated_at": cred.rotated_at,
                    "grace_expires_at": cred.grace_expires_at,
                }
                for cred in sorted(device.credentials.values(), key=lambda c: c.issued_at)
            ]
        return {"device_id": device_id, "credentials": infos}


class TelemetryService:
    def __init__(self, store: DeviceStore, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or SystemClock()

    # -- 批量上报 ---------------------------------------------------------

    def ingest(self, device_id: str, batch: TelemetryBatchRequest) -> dict:
        raw_items = batch.items
        logger.info(
            "ingest start device_id=%s batch_size=%d items=%s",
            device_id, len(raw_items), raw_items,
        )

        # 阶段一（锁外）：逐条字段校验。拒绝原因与条目顺序无关，固定检查顺序。
        verdicts: list[ItemVerdict] = []
        valid: list[tuple[int, float, str, float]] = []
        for index, item in enumerate(raw_items):
            verdict = self._validate_item(index, item)
            if verdict is not None:
                verdicts.append(verdict)
            else:
                ts = float(item["timestamp"])
                metric = str(item["metric"])
                value = float(item["value"])
                valid.append((index, ts, metric, value))

        # 阶段二（锁外）：按 (timestamp, metric) 分组，完成批内重复判定。
        # 同键值全部一致 -> 幂等重复，只保留一条参与后续判定；
        # 同键出现不同值 -> 冲突，该键所有条目一律拒绝。
        # 整段逻辑只依赖数据本身，与条目到达顺序无关。
        groups: dict[tuple[float, str], list[tuple[int, float]]] = {}
        for index, ts, metric, value in valid:
            groups.setdefault((ts, metric), []).append((index, value))

        candidates: list[tuple[int, float, str, float]] = []
        for (ts, metric), occurrences in groups.items():
            values = {value for _, value in occurrences}
            if len(occurrences) > 1:
                if len(values) == 1:
                    # 保留最小 index 的一条作为代表参与后续判定，其余判幂等重复
                    keep_index = min(i for i, _ in occurrences)
                    keep_value = dict(occurrences)[keep_index]
                    for index, _ in occurrences:
                        if index != keep_index:
                            verdicts.append(
                                ItemVerdict(
                                    index, "duplicate", "duplicate_same_value",
                                    ts, metric,
                                )
                            )
                    candidates.append((keep_index, ts, metric, keep_value))
                else:
                    for index, _ in occurrences:
                        verdicts.append(
                            ItemVerdict(
                                index,
                                "rejected",
                                ErrorCode.DUPLICATE_CONFLICT.value,
                                ts,
                                metric,
                            )
                        )
            else:
                index, value = occurrences[0]
                candidates.append((index, ts, metric, value))

        # 先按时间排序：批内乱序到达时，应用顺序只取决于数据，保证最终状态一致。
        candidates.sort(key=lambda x: (x[1], x[2]))

        accepted_indexes: set[int] = set()
        # 阶段三（锁内）：与已落库状态比较并一次性应用。
        with self.store.device(device_id) as device:
            now = self.clock.now()
            for index, ts, metric, value in candidates:
                key = (ts, metric)
                existing = device.points.get(key)
                if existing is not None:
                    if existing.value == value:
                        verdicts.append(
                            ItemVerdict(
                                index, "duplicate", "duplicate_same_value", ts, metric
                            )
                        )
                    else:
                        verdicts.append(
                            ItemVerdict(
                                index,
                                "rejected",
                                ErrorCode.DUPLICATE_CONFLICT.value,
                                ts,
                                metric,
                            )
                        )
                    continue
                # 水位比较针对"此前批次已落库"的最新时间戳；
                # 本批候选已按时间排序，批内乱序不会互相触发倒退。
                if device.latest_ts is not None and ts < device.latest_ts:
                    verdicts.append(
                        ItemVerdict(
                            index,
                            "rejected",
                            ErrorCode.TIMESTAMP_REGRESSION.value,
                            ts,
                            metric,
                        )
                    )
                    continue
                device.points[key] = TelemetryPoint(
                    timestamp=ts, metric=metric, value=value, received_at=now
                )
                device.latest_ts = (
                    ts if device.latest_ts is None else max(device.latest_ts, ts)
                )
                accepted_indexes.add(index)

        for index, ts, metric, value in candidates:
            if index in accepted_indexes:
                verdicts.append(
                    ItemVerdict(index, "accepted", "accepted", ts, metric)
                )

        verdicts.sort(key=lambda v: v.index)
        accepted = sum(1 for v in verdicts if v.outcome == "accepted")
        duplicate = sum(1 for v in verdicts if v.outcome == "duplicate")
        rejected = sum(1 for v in verdicts if v.outcome == "rejected")
        results = [
            {
                "index": v.index,
                "outcome": v.outcome,
                "reason": v.reason,
                "timestamp": v.timestamp,
                "metric": v.metric,
            }
            for v in verdicts
        ]
        logger.info(
            "ingest done device_id=%s accepted=%d duplicate=%d rejected=%d results=%s",
            device_id, accepted, duplicate, rejected, results,
        )
        return {
            "device_id": device_id,
            "accepted_count": accepted,
            "duplicate_count": duplicate,
            "rejected_count": rejected,
            "results": results,
        }

    def _validate_item(self, index: int, item: object) -> ItemVerdict | None:
        """返回非 None 表示该条目被拒绝；None 表示字段校验通过。"""
        if not isinstance(item, dict):
            return ItemVerdict(index, "rejected", ErrorCode.TYPE_MISMATCH.value)
        for field_name in FIELD_ORDER:
            if field_name not in item:
                return ItemVerdict(
                    index, "rejected",
                    f"{ErrorCode.MISSING_FIELD.value}:{field_name}",
                )
        ts = item["timestamp"]
        if not is_finite_number(ts):
            return ItemVerdict(
                index, "rejected", ErrorCode.INVALID_TIMESTAMP.value
                if not isinstance(ts, (str, bool))
                else f"{ErrorCode.TYPE_MISMATCH.value}:timestamp",
            )
        metric = item["metric"]
        if not isinstance(metric, str) or not metric:
            return ItemVerdict(
                index, "rejected", f"{ErrorCode.TYPE_MISMATCH.value}:metric"
            )
        value = item["value"]
        if not is_finite_number(value):
            return ItemVerdict(
                index, "rejected", f"{ErrorCode.TYPE_MISMATCH.value}:value"
            )
        return None

    # -- 查询 -------------------------------------------------------------

    def latest(self, device_id: str) -> dict:
        with self.store.device(device_id) as device:
            if device.latest_ts is None:
                return {"device_id": device_id, "latest_ts": None, "state": {}}
            state: dict[str, TelemetryPointResponse] = {}
            for point in device.points.values():
                if point.timestamp != device.latest_ts:
                    continue
                current = state.get(point.metric)
                if current is None or point.received_at > current.received_at:
                    state[point.metric] = TelemetryPointResponse(
                        timestamp=point.timestamp,
                        metric=point.metric,
                        value=point.value,
                        received_at=point.received_at,
                    )
            return {
                "device_id": device_id,
                "latest_ts": device.latest_ts,
                "state": state,
            }

    def history(self, device_id: str, start: float, end: float) -> dict:
        with self.store.device(device_id) as device:
            points = [
                TelemetryPointResponse(
                    timestamp=p.timestamp,
                    metric=p.metric,
                    value=p.value,
                    received_at=p.received_at,
                )
                for p in device.points.values()
                if start <= p.timestamp <= end
            ]
        points.sort(key=lambda p: (p.timestamp, p.metric))
        return {"device_id": device_id, "points": points}
