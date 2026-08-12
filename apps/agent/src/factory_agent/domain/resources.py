"""Resource lease 与 target guard 的 Task 1 只读 selector。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from factory_agent.errors import FactoryError


class TargetGuardState(StrEnum):
    OPEN = "OPEN"
    INTERVENTION_REQUIRED = "INTERVENTION_REQUIRED"


class ResourceContractError(FactoryError):
    """Resource/guard exact-set 或闭集枚举非法时抛出。"""

    error_code = "INVALID_RESOURCE_RECORD"


def _values(record: Mapping[str, Any], record_type: type[Any]) -> dict[str, Any]:
    """复制 exact-set selector，不提前实现 lease 接管或 guard clear policy。"""
    expected = frozenset(record_type.__dataclass_fields__)
    if not isinstance(record, Mapping) or frozenset(record) != expected:
        raise ResourceContractError("resource record fields do not match the frozen contract")
    return dict(record)


@dataclass(frozen=True, slots=True)
class ResourceLease:
    """资源租约 selector 快照；调度、续租和接管属于后续 Task 5。"""

    resource_key: str
    owner_executor_id: str
    fencing_token: int
    control_epoch: int
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    state_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ResourceLease:
        """从 resource_leases 精确 selector 构建不可变值对象。"""
        return cls(**_values(record, cls))


@dataclass(frozen=True, slots=True)
class TargetGuard:
    """资源指纹 guard selector 快照；本阶段不猜测清除授权规则。"""

    resource_fingerprint: str
    guard_state: TargetGuardState
    reason_code: str | None
    evidence_digest: str | None
    created_by_release_id: str | None
    created_at: str
    cleared_by: str | None
    cleared_at: str | None
    clear_receipt_digest: str | None
    state_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> TargetGuard:
        """从 target_guards exact-set selector 构建不可变值对象。"""
        values = _values(record, cls)
        try:
            values["guard_state"] = TargetGuardState(values["guard_state"])
        except (TypeError, ValueError) as exc:
            raise ResourceContractError("target guard state is invalid") from exc
        return cls(**values)


__all__ = ["ResourceContractError", "ResourceLease", "TargetGuard", "TargetGuardState"]
