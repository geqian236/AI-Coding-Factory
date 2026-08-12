"""Control command 的内部闭集与 v1 wire PAUSE 兼容适配边界。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from factory_agent.errors import FactoryError


class ControlCommandType(StrEnum):
    SOFT_PAUSE = "SOFT_PAUSE"
    IMMEDIATE_STOP = "IMMEDIATE_STOP"
    RESUME = "RESUME"
    CANCEL = "CANCEL"


class ControlCommandReceiptPhase(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ControlContractError(FactoryError):
    """Control selector 或 wire compatibility 输入非法时抛出。"""

    error_code = "INVALID_CONTROL_RECORD"


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """持久化 control command selector；外部 PAUSE 不得渗入本对象。"""

    command_id: str
    run_id: str
    command_seq: int
    request_id: str
    command_type: ControlCommandType
    actor_id: str
    expected_state_version: int
    accepted_state_version: int
    issued_at: str
    acknowledged_attempt_id: str | None
    reason_digest: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ControlCommand:
        """从 exact-set 持久 selector 构造内部命令，拒绝 v1 wire 的 PAUSE 字面量。"""
        if not isinstance(record, Mapping) or frozenset(record) != frozenset(cls.__dataclass_fields__):
            raise ControlContractError("control command fields do not match the frozen contract")
        values = dict(record)
        try:
            values["command_type"] = ControlCommandType(values["command_type"])
        except (TypeError, ValueError) as exc:
            raise ControlContractError("control command type is invalid") from exc
        return cls(**values)


def from_v1_wire_command_type(value: str) -> ControlCommandType:
    """仅在 v1 adapter 边界把冻结 PAUSE 映射为内部 SOFT_PAUSE。"""
    return ControlCommandType.SOFT_PAUSE if value == "PAUSE" else ControlCommandType(value)


def to_v1_wire_command_type(value: ControlCommandType) -> str:
    """把内部 SOFT_PAUSE 映回 v1 PAUSE，其余闭集命令保持同名。"""
    command_type = ControlCommandType(value)
    return "PAUSE" if command_type is ControlCommandType.SOFT_PAUSE else command_type.value


__all__ = [
    "ControlCommand",
    "ControlCommandReceiptPhase",
    "ControlCommandType",
    "ControlContractError",
    "from_v1_wire_command_type",
    "to_v1_wire_command_type",
]
