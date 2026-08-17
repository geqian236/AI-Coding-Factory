"""STATE-001 barrier、UNKNOWN timeout 与里程碑证据测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from factory_agent.domain.workflow import AchievedStage, StepOutcome, StepPhase, TargetStage
from factory_agent.state_machine.barriers import BarrierStepFacts, evaluate_barrier
from factory_agent.state_machine.milestones import (
    MilestoneEvidence,
    MilestoneEvidenceError,
    evaluate_milestone,
)

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)


def _step(**overrides: object) -> BarrierStepFacts:
    values: dict[str, object] = {
        "step_id": "step-1",
        "required": True,
        "phase": StepPhase.TERMINAL,
        "outcome": StepOutcome.SUCCEEDED,
        "active_attempt": False,
        "unsettled_started_receipt": False,
        "success_predicate_passed": True,
        "required_artifacts_committed": True,
        "blocking_finding_open": False,
    }
    values.update(overrides)
    return BarrierStepFacts(**values)  # type: ignore[arg-type]


def test_barrier_is_settled_and_passed_only_when_all_frozen_predicates_hold() -> None:
    """known terminal、无活动 Attempt/未知副作用且 required 证据齐全才可 passed。"""
    decision = evaluate_barrier(
        (_step(step_id="required"), _step(step_id="optional", required=False)),
        now=NOW,
        settle_deadline_at=NOW + timedelta(seconds=10),
    )
    assert decision.settled is True
    assert decision.passed is True
    assert decision.requires_user_action is False
    assert decision.block_reason_code is None


@pytest.mark.parametrize(
    ("mutation", "expected_settled", "expected_code"),
    [
        ({"phase": StepPhase.RUNNING}, False, "BARRIER_NOT_SETTLED"),
        ({"active_attempt": True}, False, "ACTIVE_ATTEMPT"),
        ({"unsettled_started_receipt": True}, False, "UNSETTLED_SIDE_EFFECT"),
        ({"outcome": StepOutcome.UNKNOWN_REMOTE_STATE}, False, "UNKNOWN_REMOTE_STATE"),
        ({"required_artifacts_committed": False}, True, "REQUIRED_ARTIFACT_MISSING"),
        ({"success_predicate_passed": False}, True, "SUCCESS_PREDICATE_FAILED"),
        ({"blocking_finding_open": True}, True, "BLOCKING_FINDING_OPEN"),
    ],
)
def test_barrier_rejects_each_unsettled_or_unpassed_fact(
    mutation: dict[str, object],
    expected_settled: bool,
    expected_code: str,
) -> None:
    """settled 与 passed 分离：缺 Artifact 是已收敛失败，UNKNOWN 则永不 settled。"""
    decision = evaluate_barrier(
        (_step(**mutation),),
        now=NOW,
        settle_deadline_at=NOW + timedelta(seconds=10),
    )
    assert decision.settled is expected_settled
    assert decision.passed is False
    assert decision.block_reason_code == expected_code


def test_unknown_remote_state_timeout_stays_unknown_and_requires_action() -> None:
    """settle timeout 只能 BLOCKED/ACTION_REQUIRED，不能把未知洗成失败终态。"""
    decision = evaluate_barrier(
        (_step(outcome=StepOutcome.UNKNOWN_REMOTE_STATE),),
        now=NOW,
        settle_deadline_at=NOW - timedelta(milliseconds=1),
    )
    assert decision.settled is False
    assert decision.passed is False
    assert decision.timed_out is True
    assert decision.requires_user_action is True
    assert decision.block_reason_code == "UNKNOWN_REMOTE_STATE_TIMEOUT"


def test_optional_step_missing_artifact_does_not_block_passed_barrier() -> None:
    """requiredArtifacts 只对 required Step 构成里程碑 gate。"""
    decision = evaluate_barrier(
        (_step(required=False, required_artifacts_committed=False),),
        now=NOW,
        settle_deadline_at=None,
    )
    assert decision.settled is True
    assert decision.passed is True


def test_milestone_advances_and_completes_only_with_atomic_success_evidence() -> None:
    """目标 barrier 与全部收口证据齐全时才可推进里程碑并结束目标 Task。"""
    evidence = MilestoneEvidence(
        barrier_passed=True,
        active_attempt_count=0,
        unknown_remote_state=False,
        required_artifacts_committed=True,
        blocking_finding_count=0,
    )
    decision = evaluate_milestone(
        current=AchievedStage.CODEX_APPROVED,
        candidate=AchievedStage.PR_READY,
        target=TargetStage.PR_READY,
        evidence=evidence,
    )
    assert decision.achieved_stage is AchievedStage.PR_READY
    assert decision.target_reached is True
    assert decision.lifecycle_should_succeed is True


@pytest.mark.parametrize(
    "evidence",
    [
        MilestoneEvidence(False, 0, False, True, 0),
        MilestoneEvidence(True, 1, False, True, 0),
        MilestoneEvidence(True, 0, True, True, 0),
        MilestoneEvidence(True, 0, False, False, 0),
        MilestoneEvidence(True, 0, False, True, 1),
    ],
)
def test_milestone_rejects_missing_or_unsettled_evidence(evidence: MilestoneEvidence) -> None:
    """缺 barrier、活动 Attempt、UNKNOWN、缺 Artifact 或 blocking finding 均阻断里程碑。"""
    with pytest.raises(MilestoneEvidenceError):
        evaluate_milestone(
            current=AchievedStage.CODEX_APPROVED,
            candidate=AchievedStage.PR_READY,
            target=TargetStage.PRODUCTION_ACCEPTED,
            evidence=evidence,
        )
