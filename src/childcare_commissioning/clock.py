"""可注入时钟：资金节点与投运期限的计算不依赖系统时间。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """返回带时区的当前时刻。"""

    def now(self) -> datetime: ...


class SystemClock:
    """生产环境使用的真实时钟。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FixedClock:
    """测试与复核场景使用的固定时钟，可显式推进。"""

    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current = self.current + delta
