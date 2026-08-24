"""Budget clock 追加事件的最小闭集和值对象；不计算时间或预算。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from factory_agent.errors import FactoryError


class BudgetClockTransition(StrEnum):
    RUNNING_STARTED = "RUNNING_STARTED"
    SUSPENSION_STARTED = "SUSPENSION_STARTED"


class BudgetSuspensionReason(StrEnum):
    USER_PAUSED = "USER_PAUSED"
    REQUIRES_USER_ACTION = "REQUIRES_USER_ACTION"


class BudgetContractError(FactoryError):
    """Budget clock exact-set、闭集或 transition/reason 配对非法时抛出。"""

    error_code = "INVALID_BUDGET_CLOCK_EVENT"


@dataclass(frozen=True, slots=True)
class BudgetClockEvent:
    """追加型 budget clock event；时间差、异常与阈值算法留给 Task 3。"""

    run_id: str
    clock_seq: int
    transition: BudgetClockTransition
    suspension_reason: BudgetSuspensionReason | None
    boot_id: str
    monotonic_ns: int
    wall_time: str
    state_event_id: str
    previous_clock_digest: str | None
    clock_digest: str

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> BudgetClockEvent:
        """构造不可变 event，并仅冻结 RUNNING/暂停原因的合法配对。"""
        if not isinstance(record, Mapping) or frozenset(record) != frozenset(cls.__dataclass_fields__):
            raise BudgetContractError("budget event fields do not match the frozen contract")
        values = dict(record)
        try:
            transition = BudgetClockTransition(values["transition"])
            reason_value = values["suspension_reason"]
            reason = None if reason_value is None else BudgetSuspensionReason(reason_value)
        except (TypeError, ValueError) as exc:
            raise BudgetContractError("budget event enum is invalid") from exc
        if (transition is BudgetClockTransition.RUNNING_STARTED and reason is not None) or (
            transition is BudgetClockTransition.SUSPENSION_STARTED and reason is None
        ):
            raise BudgetContractError("budget transition and suspension reason do not form a valid pair")
        values["transition"] = transition
        values["suspension_reason"] = reason
        return cls(**values)


__all__ = [
    "BudgetClockEvent",
    "BudgetClockTransition",
    "BudgetContractError",
    "BudgetSuspensionReason",
]
