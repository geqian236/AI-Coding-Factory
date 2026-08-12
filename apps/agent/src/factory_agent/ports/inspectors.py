"""确定性 inspector 的单一调用 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class Inspector(Protocol):
    """检查一个显式输入并返回 primitive mapping，不依赖调度或存储实现。"""

    def inspect(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


__all__ = ["Inspector"]
