"""可注入的时钟，便于测试凭证宽限期等时间相关逻辑。"""
from __future__ import annotations

import time


class Clock:
    """时钟接口：业务层只依赖它，方便测试里替换成可控时间。"""

    def now(self) -> float:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        raise NotImplementedError


class SystemClock(Clock):
    """真实系统时钟。"""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock(Clock):
    """测试用时钟：时间只在显式 advance 时前进。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += float(seconds)

    def advance(self, seconds: float) -> None:
        self._now += float(seconds)
