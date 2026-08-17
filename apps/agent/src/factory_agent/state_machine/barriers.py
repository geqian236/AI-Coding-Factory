"""Barrier settled/passed、UNKNOWN timeout 与阻断原因投影。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from factory_agent.domain.workflow import StepOutcome, StepPhase
from factory_agent.errors import FactoryError
from factory_agent.state_machine.predicates import required_step_passed, step_is_settled


class BarrierContractError(FactoryError):
    """Barrier 输入为空、时间类型错误或事实字段非法时抛出。"""

    error_code = "INVALID_BARRIER_FACTS"


@dataclass(frozen=True, slots=True)
class BarrierStepFacts:
    """Barrier 决策所需的脱敏事实，不携带 Artifact 或 receipt 原文。"""

    step_id: str
    required: bool
    phase: StepPhase
    outcome: StepOutcome
    active_attempt: bool
    unsettled_started_receipt: bool
    success_predicate_passed: bool
    required_artifacts_committed: bool
    blocking_finding_open: bool

    def __post_init__(self) -> None:
        """收敛枚举并拒绝可伪装成 bool 的整数。"""
        if not isinstance(self.step_id, str) or not self.step_id:
            raise BarrierContractError("barrier step identity is invalid")
        for name in (
            "required",
            "active_attempt",
            "unsettled_started_receipt",
            "success_predicate_passed",
            "required_artifacts_committed",
            "blocking_finding_open",
        ):
            if type(getattr(self, name)) is not bool:
                raise BarrierContractError("barrier boolean fact is invalid")
        object.__setattr__(self, "phase", StepPhase(self.phase))
        object.__setattr__(self, "outcome", StepOutcome(self.outcome))


@dataclass(frozen=True, slots=True)
class BarrierDecision:
    """Barrier 的可持久化决策摘要。"""

    settled: bool
    passed: bool
    timed_out: bool
    requires_user_action: bool
    block_reason_code: str | None


def _unsettled_reason(steps: tuple[BarrierStepFacts, ...]) -> str | None:
    """按安全优先级返回首个未收敛原因。"""
    if any(step.outcome is StepOutcome.UNKNOWN_REMOTE_STATE for step in steps):
        return "UNKNOWN_REMOTE_STATE"
    if any(step.active_attempt for step in steps):
        return "ACTIVE_ATTEMPT"
    if any(step.unsettled_started_receipt for step in steps):
        return "UNSETTLED_SIDE_EFFECT"
    if any(not step_is_settled(step) for step in steps):
        return "BARRIER_NOT_SETTLED"
    return None


def _unpassed_reason(steps: tuple[BarrierStepFacts, ...]) -> str | None:
    """settled 后按证据优先级返回 gate 未通过原因。"""
    required = tuple(step for step in steps if step.required)
    if any(not step.required_artifacts_committed for step in required):
        return "REQUIRED_ARTIFACT_MISSING"
    if any(not step.success_predicate_passed for step in required):
        return "SUCCESS_PREDICATE_FAILED"
    if any(step.blocking_finding_open for step in required):
        return "BLOCKING_FINDING_OPEN"
    if any(not required_step_passed(step) for step in required):
        return "SUCCESS_PREDICATE_FAILED"
    return None


def evaluate_barrier(
    steps: tuple[BarrierStepFacts, ...],
    *,
    now: datetime,
    settle_deadline_at: datetime | None,
) -> BarrierDecision:
    """机械判定 settled/passed；UNKNOWN 超时保持未知并要求人工处理。"""
    if (
        not steps
        or not isinstance(now, datetime)
        or (settle_deadline_at is not None and not isinstance(settle_deadline_at, datetime))
    ):
        raise BarrierContractError("barrier evaluation input is invalid")
    unsettled_reason = _unsettled_reason(steps)
    if unsettled_reason is not None:
        timed_out = settle_deadline_at is not None and now >= settle_deadline_at
        unknown_timed_out = timed_out and unsettled_reason == "UNKNOWN_REMOTE_STATE"
        return BarrierDecision(
            settled=False,
            passed=False,
            timed_out=timed_out,
            requires_user_action=unknown_timed_out,
            block_reason_code="UNKNOWN_REMOTE_STATE_TIMEOUT" if unknown_timed_out else unsettled_reason,
        )
    unpassed_reason = _unpassed_reason(steps)
    return BarrierDecision(
        settled=True,
        passed=unpassed_reason is None,
        timed_out=False,
        requires_user_action=False,
        block_reason_code=unpassed_reason,
    )


__all__ = ["BarrierContractError", "BarrierDecision", "BarrierStepFacts", "evaluate_barrier"]
