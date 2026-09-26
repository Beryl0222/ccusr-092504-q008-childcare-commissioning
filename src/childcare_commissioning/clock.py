"""可注入时钟：资金节点与竣工后投运期限均按此时钟判定。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    """真实墙钟，统一带 UTC 时区。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """测试/重放用固定时钟，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        self._moment = self._ensure_tz(moment)

    @staticmethod
    def _ensure_tz(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment.replace(tzinfo=timezone.utc)
        return moment

    def now(self) -> datetime:
        return self._moment

    def advance(self, **delta) -> None:
        from datetime import timedelta

        self._moment = self._moment + timedelta(**delta)

    def set(self, moment: datetime) -> None:
        self._moment = self._ensure_tz(moment)


class ManualClock:
    """由回调提供当前时间，便于在场景脚本里逐拍驱动。"""

    def __init__(self, supplier: Callable[[], datetime]) -> None:
        self._supplier = supplier

    def now(self) -> datetime:
        value = self._supplier()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
