"""可替换时钟采样 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class Clock(Protocol):
    """返回 boot/monotonic/wall-time primitive，便于后续预算算法确定性测试。"""

    def sample(self) -> Mapping[str, object]: ...


__all__ = ["Clock"]
