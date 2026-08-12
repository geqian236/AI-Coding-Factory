"""单条 authoritative state event lane 的 Task 1 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class EventStore(Protocol):
    """只追加/读取权威 state event；不包含 Task 7 batch/segment API。"""

    def append_authoritative_state_event(self, record: Mapping[str, object]) -> None: ...
    def get_authoritative_state_event(self, event_id: str) -> Mapping[str, object] | None: ...


__all__ = ["EventStore"]
