"""可注入的时间源，方便测试时控制时间。"""

from __future__ import annotations

import time
from typing import Callable


class Clock:
    def __init__(self, fn: Callable[[], float] = time.time):
        self._fn = fn

    def now(self) -> float:
        return self._fn()


class MutableClock(Clock):
    """可手动设置当前时间的时钟，引擎与行情共享同一实例。"""

    def __init__(self, start: float = 0.0):
        super().__init__(lambda: self._t)
        self._t = start

    def set(self, t: float) -> None:
        self._t = t

    def advance(self, seconds: float) -> None:
        self._t += seconds
