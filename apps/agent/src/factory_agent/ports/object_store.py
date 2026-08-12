"""内容寻址对象的验证与只读打开 primitive。"""

from __future__ import annotations

from typing import BinaryIO, Protocol


class ObjectStore(Protocol):
    """对象存储边界只暴露 identity 验证和 verified stream。"""

    def verify(self, digest: str) -> bool: ...
    def open_verified(self, digest: str) -> BinaryIO: ...


__all__ = ["ObjectStore"]
