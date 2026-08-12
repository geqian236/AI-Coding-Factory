"""Resource lease 与 target guard 的 Task 1 持久化 primitive。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from factory_agent.domain.resources import ResourceLease, TargetGuard


class ResourceStore(Protocol):
    """冻结 lease/guard get/insert/CAS，不包含接管或 clear policy。"""

    def get_resource_lease(self, resource_key: str) -> Mapping[str, object] | None: ...
    def insert_resource_lease(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_resource_lease(
        self,
        resource_key: str,
        expected_state_version: int,
        changes: Mapping[str, object],
    ) -> ResourceLease: ...
    def get_target_guard(self, resource_fingerprint: str) -> Mapping[str, object] | None: ...
    def insert_target_guard(self, record: Mapping[str, object]) -> None: ...
    def compare_and_set_target_guard(
        self,
        resource_fingerprint: str,
        expected_state_version: int,
        changes: Mapping[str, object],
    ) -> TargetGuard: ...


__all__ = ["ResourceStore"]
