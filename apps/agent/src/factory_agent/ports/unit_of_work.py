"""一次 SQLite writer 事务可见的四个仓储引用。"""

from __future__ import annotations

from typing import Protocol


class UnitOfWork(Protocol):
    """只暴露四个事务内仓储；begin/commit/rollback 由 coordinator 管理。"""

    workflow: object
    events: object
    authorization: object
    resources: object


__all__ = ["UnitOfWork"]
