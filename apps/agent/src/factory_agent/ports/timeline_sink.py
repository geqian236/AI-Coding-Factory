"""仅发布已提交 timeline 记录的 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class TimelineSink(Protocol):
    """只接受已提交记录；事务前发布和 transport 策略不属于本接口。"""

    def publish_committed(self, record: Mapping[str, object]) -> None: ...


__all__ = ["TimelineSink"]
