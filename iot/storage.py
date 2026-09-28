"""本地内存存储。

不依赖任何外部服务：
- 一把全局注册表锁保护设备注册表的增删查；
- 每台设备一把 RLock，保护该设备的凭证与遥测状态，
  保证并发读写同一设备时不会出现"半更新"状态。
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from .errors import ErrorCode, NotFoundError
from .models import DeviceState


class DeviceStore:
    def __init__(self) -> None:
        self._registry_lock = threading.RLock()
        self._devices: dict[str, DeviceState] = {}
        self._device_locks: dict[str, threading.RLock] = {}

    # -- 设备注册表 -------------------------------------------------------

    def create_device(self, device_id: str, created_at: float) -> DeviceState:
        """在注册表锁内原子地创建设备；已存在返回 None。"""
        with self._registry_lock:
            if device_id in self._devices:
                return None  # type: ignore[return-value]
            state = DeviceState(device_id=device_id, created_at=created_at)
            self._devices[device_id] = state
            self._device_locks[device_id] = threading.RLock()
            return state

    def exists(self, device_id: str) -> bool:
        with self._registry_lock:
            return device_id in self._devices

    @contextmanager
    def device(self, device_id: str) -> Iterator[DeviceState]:
        """获取设备状态（不存在抛 404），并持有该设备的锁直到退出。"""
        with self._registry_lock:
            state = self._devices.get(device_id)
            lock = self._device_locks.get(device_id)
        if state is None or lock is None:
            raise NotFoundError(
                ErrorCode.DEVICE_NOT_FOUND, f"device '{device_id}' not found"
            )
        with lock:
            yield state
