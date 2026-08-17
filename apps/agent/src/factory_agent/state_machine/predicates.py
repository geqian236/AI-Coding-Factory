"""Barrier settled/passed 的无副作用机械谓词。"""

from __future__ import annotations

from typing import Protocol

from factory_agent.domain.workflow import StepOutcome, StepPhase

KNOWN_TERMINAL_OUTCOMES = frozenset(
    {
        StepOutcome.SUCCEEDED,
        StepOutcome.FAILED,
        StepOutcome.INTERRUPTED,
        StepOutcome.SKIPPED,
        StepOutcome.CANCELLED,
    }
)


class BarrierStepView(Protocol):
    """Barrier 判定所需的最小只读 Step 事实。"""

    @property
    def required(self) -> bool: ...

    @property
    def phase(self) -> StepPhase: ...

    @property
    def outcome(self) -> StepOutcome: ...

    @property
    def active_attempt(self) -> bool: ...

    @property
    def unsettled_started_receipt(self) -> bool: ...

    @property
    def success_predicate_passed(self) -> bool: ...

    @property
    def required_artifacts_committed(self) -> bool: ...

    @property
    def blocking_finding_open(self) -> bool: ...


def step_is_settled(step: BarrierStepView) -> bool:
    """Step 只有 known terminal 且无活动执行/未核对副作用时才 settled。"""
    return (
        step.phase is StepPhase.TERMINAL
        and step.outcome in KNOWN_TERMINAL_OUTCOMES
        and not step.active_attempt
        and not step.unsettled_started_receipt
    )


def required_step_passed(step: BarrierStepView) -> bool:
    """Optional Step 不参与 gate；required Step 必须满足谓词、Artifact 和 finding。"""
    return not step.required or (
        step_is_settled(step)
        and step.success_predicate_passed
        and step.required_artifacts_committed
        and not step.blocking_finding_open
    )


__all__ = ["BarrierStepView", "KNOWN_TERMINAL_OUTCOMES", "required_step_passed", "step_is_settled"]
