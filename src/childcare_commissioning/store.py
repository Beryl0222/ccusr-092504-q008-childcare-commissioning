"""事件存储：聚合流版本、并发基线、事件幂等与 JSONL 持久化。

并发审批采用调用方读取时的聚合版本作为基线（expected_version）：
两个审批人基于同一基线先后提交时，第二个提交收到 ConcurrentModificationError，
必须重读后再决定，避免静默覆盖签署结论。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .clock import Clock, SystemClock


class ConcurrentModificationError(RuntimeError):
    """提交基线与当前聚合版本不一致。"""

    def __init__(self, aggregate_id: str, expected: int, actual: int) -> None:
        super().__init__(
            f"聚合 {aggregate_id} 基线版本 {expected} 已过期，当前版本 {actual}"
        )
        self.aggregate_id = aggregate_id
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    occurred_at: str
    version: int
    summary: str
    payload: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _isoformat(moment: Any) -> str:
    return moment.isoformat()


class EventStore:
    """内存事件日志，按聚合维护单调版本。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._events: list[StoredEvent] = []
        self._versions: dict[str, int] = {}
        self._by_id: dict[str, StoredEvent] = {}
        self._lock = threading.RLock()

    @property
    def clock(self) -> Clock:
        return self._clock

    def version(self, aggregate_id: str) -> int:
        with self._lock:
            return self._versions.get(aggregate_id, 0)

    def events(self, aggregate_id: str | None = None) -> list[StoredEvent]:
        with self._lock:
            if aggregate_id is None:
                return list(self._events)
            return [e for e in self._events if e.aggregate_id == aggregate_id]

    def append(
        self,
        *,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        summary: str,
        payload: Mapping[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> StoredEvent:
        """追加事件。

        expected_version 为 None 表示新建流（当前版本必须为 0）；
        传入整数时必须与当前版本相等。event_id 重复时幂等返回原事件，
        用于“重复材料只收一次”和重启后的提醒/复核去重。
        """
        with self._lock:
            existing = self._by_id.get(event_id)
            if existing is not None:
                return existing

            current = self._versions.get(aggregate_id, 0)
            if expected_version is None:
                if current != 0:
                    raise ConcurrentModificationError(aggregate_id, 0, current)
            elif expected_version != current:
                raise ConcurrentModificationError(aggregate_id, expected_version, current)

            version = current + 1
            data: dict[str, Any] = dict(payload or {})
            data.update(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "occurred_at": _isoformat(self._clock.now()),
                    "version": version,
                    "summary": summary,
                }
            )
            event = StoredEvent(
                event_id=event_id,
                event_type=event_type,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                occurred_at=data["occurred_at"],
                version=version,
                summary=summary,
                payload=data,
            )
            self._events.append(event)
            self._versions[aggregate_id] = version
            self._by_id[event_id] = event
            self._after_append(event)
            return event

    def _after_append(self, event: StoredEvent) -> None:
        """持久化子类的挂载点。"""

    def _restore(self, event: StoredEvent) -> None:
        self._events.append(event)
        self._versions[event.aggregate_id] = event.version
        self._by_id[event.event_id] = event


class JsonEventStore(EventStore):
    """JSONL 事件存储；重启后回放继续未完成的复核与到期提醒。"""

    def __init__(self, path: str | Path, clock: Clock | None = None) -> None:
        super().__init__(clock=clock)
        self.path = Path(path)
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                event = StoredEvent(
                    event_id=data["event_id"],
                    event_type=data["event_type"],
                    aggregate_type=data["aggregate_type"],
                    aggregate_id=data["aggregate_id"],
                    occurred_at=data["occurred_at"],
                    version=data["version"],
                    summary=data["summary"],
                    payload=data,
                )
                self._restore(event)

    def _after_append(self, event: StoredEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.payload, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
            handle.flush()
