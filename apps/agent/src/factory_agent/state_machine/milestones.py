"""里程碑单调推进与目标 Task 成功证据判定。"""

from __future__ import annotations

from dataclasses import dataclass

from factory_agent.domain.workflow import AchievedStage, TargetStage
from factory_agent.errors import FactoryError
from factory_agent.state_machine.transitions import require_achieved_stage_transition


class MilestoneEvidenceError(FactoryError):
    """里程碑证据不完整或仍存在活动/未知事实时抛出。"""

    error_code = "MILESTONE_EVIDENCE_INCOMPLETE"


@dataclass(frozen=True, slots=True)
class MilestoneEvidence:
    """推进里程碑所需的五项原子证据摘要。"""

    barrier_passed: bool
    active_attempt_count: int
    unknown_remote_state: bool
    required_artifacts_committed: bool
    blocking_finding_count: int

    def __post_init__(self) -> None:
        """拒绝负计数与 bool/int 混淆，避免宽松输入绕过 gate。"""
        if (
            type(self.barrier_passed) is not bool
            or type(self.unknown_remote_state) is not bool
            or type(self.required_artifacts_committed) is not bool
            or type(self.active_attempt_count) is not int
            or self.active_attempt_count < 0
            or type(self.blocking_finding_count) is not int
            or self.blocking_finding_count < 0
        ):
            raise MilestoneEvidenceError("milestone evidence shape is invalid")


@dataclass(frozen=True, slots=True)
class MilestoneDecision:
    """事务层可据此更新 achieved_stage 与最终 lifecycle。"""

    achieved_stage: AchievedStage
    target_reached: bool
    lifecycle_should_succeed: bool


def evaluate_milestone(
    *,
    current: AchievedStage,
    candidate: AchievedStage,
    target: TargetStage,
    evidence: MilestoneEvidence,
) -> MilestoneDecision:
    """在完整证据下推进一个里程碑；达到所选 target 才建议 Task SUCCEEDED。"""
    require_achieved_stage_transition(current, candidate)
    if not (
        evidence.barrier_passed
        and evidence.active_attempt_count == 0
        and not evidence.unknown_remote_state
        and evidence.required_artifacts_committed
        and evidence.blocking_finding_count == 0
    ):
        raise MilestoneEvidenceError("milestone success evidence is incomplete")
    candidate_stage = AchievedStage(candidate)
    target_reached = candidate_stage.value == TargetStage(target).value
    return MilestoneDecision(
        achieved_stage=candidate_stage,
        target_reached=target_reached,
        lifecycle_should_succeed=target_reached,
    )


__all__ = [
    "MilestoneDecision",
    "MilestoneEvidence",
    "MilestoneEvidenceError",
    "evaluate_milestone",
]
