"""Intent/Execution authorization append/get primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class AuthorizationStore(Protocol):
    """持久授权合同；签发、求交、消费和撤销策略属于后续任务。"""

    def append_intent_authorization(self, record: Mapping[str, object]) -> None: ...
    def get_intent_authorization(self, authorization_id: str) -> Mapping[str, object] | None: ...
    def append_execution_authorization(self, record: Mapping[str, object]) -> None: ...
    def get_execution_authorization(self, authorization_id: str) -> Mapping[str, object] | None: ...


__all__ = ["AuthorizationStore"]
