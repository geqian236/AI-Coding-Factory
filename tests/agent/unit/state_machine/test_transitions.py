"""STATE-001 五维状态转换的冻结参数化测试。"""

from __future__ import annotations

from itertools import product

import pytest
from factory_agent.application.transition_service import (
    StateScenarioEvidenceError,
    build_state_001_partial_receipt,
)
from factory_agent.domain.workflow import (
    AchievedStage,
    AttemptOutcome,
    AttemptPhase,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    StepOutcome,
    StepPhase,
    TaskLifecycle,
)
from factory_agent.state_machine import transitions as transition_rules
from factory_agent.state_machine.transitions import (
    PhaseTransitionReason,
    StateTransitionError,
    require_achieved_stage_transition,
    require_attempt_phase_transition,
    require_attempt_termination_transition,
    require_desired_transition,
    require_observed_transition,
    require_phase_transition,
    require_step_phase_transition,
    require_task_lifecycle_transition,
)

OBSERVED_TRANSITIONS: dict[RunObservedState, frozenset[RunObservedState]] = {
    RunObservedState.QUEUED: frozenset(
        {
            RunObservedState.RUNNING,
            RunObservedState.PAUSED,
            RunObservedState.RECONCILING,
            RunObservedState.BLOCKED,
            RunObservedState.TERMINATED,
        }
    ),
    RunObservedState.RUNNING: frozenset(
        {
            RunObservedState.QUEUED,
            RunObservedState.PAUSING,
            RunObservedState.STOPPING,
            RunObservedState.RECONCILING,
            RunObservedState.BLOCKED,
            RunObservedState.TERMINATED,
        }
    ),
    RunObservedState.PAUSING: frozenset(
        {
            RunObservedState.PAUSED,
            RunObservedState.STOPPING,
            RunObservedState.RECONCILING,
            RunObservedState.BLOCKED,
        }
    ),
    RunObservedState.PAUSED: frozenset(
        {RunObservedState.QUEUED, RunObservedState.RECONCILING, RunObservedState.TERMINATED}
    ),
    RunObservedState.STOPPING: frozenset(
        {RunObservedState.INTERRUPTED, RunObservedState.RECONCILING, RunObservedState.TERMINATED}
    ),
    RunObservedState.INTERRUPTED: frozenset(
        {
            RunObservedState.RECONCILING,
            RunObservedState.PAUSED,
            RunObservedState.QUEUED,
            RunObservedState.TERMINATED,
        }
    ),
    RunObservedState.RECONCILING: frozenset(
        {
            RunObservedState.QUEUED,
            RunObservedState.PAUSED,
            RunObservedState.STOPPING,
            RunObservedState.BLOCKED,
            RunObservedState.TERMINATED,
        }
    ),
    RunObservedState.BLOCKED: frozenset(
        {
            RunObservedState.QUEUED,
            RunObservedState.PAUSED,
            RunObservedState.RECONCILING,
            RunObservedState.TERMINATED,
        }
    ),
    RunObservedState.TERMINATED: frozenset(),
}


LEGAL_OBSERVED = tuple(
    (current, candidate) for current, candidates in OBSERVED_TRANSITIONS.items() for candidate in candidates
)
ILLEGAL_OBSERVED = tuple(
    (current, candidate)
    for current, candidate in product(RunObservedState, repeat=2)
    if candidate not in OBSERVED_TRANSITIONS[current]
)


@pytest.mark.parametrize(("current", "candidate"), LEGAL_OBSERVED)
def test_each_frozen_observed_transition_is_accepted(
    current: RunObservedState,
    candidate: RunObservedState,
) -> None:
    """§7.3 表中的每一条边都必须机械接受。"""
    require_observed_transition(current, candidate)


@pytest.mark.parametrize(("current", "candidate"), ILLEGAL_OBSERVED)
def test_every_other_observed_transition_is_rejected(
    current: RunObservedState,
    candidate: RunObservedState,
) -> None:
    """§7.3 表之外的边（含自环和 RECONCILING→RUNNING）必须 fail closed。"""
    with pytest.raises(StateTransitionError) as caught:
        require_observed_transition(current, candidate)
    assert caught.value.error_code == "INVALID_STATE_TRANSITION"


@pytest.mark.parametrize(
    ("current", "candidate"),
    [
        (RunDesiredState.RUNNING, RunDesiredState.PAUSED),
        (RunDesiredState.PAUSED, RunDesiredState.RUNNING),
        (RunDesiredState.RUNNING, RunDesiredState.CANCELLED),
        (RunDesiredState.PAUSED, RunDesiredState.CANCELLED),
    ],
)
def test_desired_state_accepts_only_frozen_edges(
    current: RunDesiredState,
    candidate: RunDesiredState,
) -> None:
    """用户意图只允许 RUNNING/PAUSED 互转及进入不可逆 CANCELLED。"""
    require_desired_transition(current, candidate)


@pytest.mark.parametrize(
    ("current", "candidate"),
    [
        (RunDesiredState.RUNNING, RunDesiredState.RUNNING),
        (RunDesiredState.PAUSED, RunDesiredState.PAUSED),
        (RunDesiredState.CANCELLED, RunDesiredState.CANCELLED),
        (RunDesiredState.CANCELLED, RunDesiredState.RUNNING),
        (RunDesiredState.CANCELLED, RunDesiredState.PAUSED),
    ],
)
def test_desired_state_rejects_self_loops_and_cancel_reversal(
    current: RunDesiredState,
    candidate: RunDesiredState,
) -> None:
    """幂等命令由 requestId 处理，不得伪装成新的状态转换。"""
    with pytest.raises(StateTransitionError):
        require_desired_transition(current, candidate)


@pytest.mark.parametrize("terminal", tuple(TaskLifecycle)[1:])
def test_task_lifecycle_has_only_active_to_terminal_edges(terminal: TaskLifecycle) -> None:
    """任务终局只能从 ACTIVE 进入，任何终局都不可再次转换。"""
    require_task_lifecycle_transition(TaskLifecycle.ACTIVE, terminal)
    for candidate in TaskLifecycle:
        with pytest.raises(StateTransitionError):
            require_task_lifecycle_transition(terminal, candidate)


def test_run_phase_accepts_normal_progress_and_only_three_reasoned_exceptions() -> None:
    """业务 phase 只允许顺序推进、修复回环、重规划与发布回滚。"""
    phases = tuple(RunPhase)
    for current, candidate in zip(phases, phases[1:]):
        require_phase_transition(current, candidate)
    require_phase_transition(
        RunPhase.CODE_REVIEWING,
        RunPhase.IMPLEMENTING,
        reason=PhaseTransitionReason.REPAIR,
    )
    require_phase_transition(
        RunPhase.VERIFYING,
        RunPhase.PLANNING,
        reason=PhaseTransitionReason.REPLAN,
    )
    require_phase_transition(
        RunPhase.DEPLOYING_PRODUCTION,
        RunPhase.ROLLING_BACK,
        reason=PhaseTransitionReason.ROLLBACK,
    )


@pytest.mark.parametrize(
    ("current", "candidate", "reason"),
    [
        (RunPhase.FINALIZING, RunPhase.IMPLEMENTING, None),
        (RunPhase.CODE_REVIEWING, RunPhase.IMPLEMENTING, None),
        (RunPhase.VERIFYING, RunPhase.PLANNING, PhaseTransitionReason.REPAIR),
        (RunPhase.PLANNING, RunPhase.ROLLING_BACK, PhaseTransitionReason.ROLLBACK),
    ],
)
def test_run_phase_rejects_unreasoned_or_wrong_reason_jumps(
    current: RunPhase,
    candidate: RunPhase,
    reason: PhaseTransitionReason | None,
) -> None:
    """非顺序跳转必须携带匹配的冻结原因，不能用任意 reason 绕过。"""
    with pytest.raises(StateTransitionError):
        require_phase_transition(current, candidate, reason=reason)


def test_achieved_stage_is_monotonic_and_allows_idempotent_projection() -> None:
    """里程碑投影可重复写同值，但绝不倒退。"""
    stages = tuple(AchievedStage)
    for current, candidate in zip(stages, stages[1:]):
        require_achieved_stage_transition(current, candidate)
        require_achieved_stage_transition(current, current)
    with pytest.raises(StateTransitionError):
        require_achieved_stage_transition(AchievedStage.PR_READY, AchievedStage.CODEX_APPROVED)


def test_step_phase_outcome_validator_freezes_the_complete_projection_matrix() -> None:
    """Step phase/outcome 组合必须与冻结语义逐项一致，UNKNOWN 只属于 RECONCILING。"""
    accepted = {
        *((phase, StepOutcome.NONE) for phase in tuple(StepPhase)[:4]),
        (StepPhase.RECONCILING, StepOutcome.NONE),
        (StepPhase.RECONCILING, StepOutcome.UNKNOWN_REMOTE_STATE),
        *((StepPhase.TERMINAL, outcome) for outcome in tuple(StepOutcome)[1:-1]),
    }
    for phase, outcome in product(StepPhase, StepOutcome):
        if (phase, outcome) in accepted:
            transition_rules.require_step_phase_outcome(phase, outcome)
        else:
            with pytest.raises(StateTransitionError):
                transition_rules.require_step_phase_outcome(phase, outcome)


def test_attempt_phase_outcome_validator_freezes_the_complete_projection_matrix() -> None:
    """Attempt 非终态只能是 NONE；TERMINATED 可保留 UNKNOWN 事实但不得是 NONE。"""
    for phase, outcome in product(AttemptPhase, AttemptOutcome):
        accepted = (phase is not AttemptPhase.TERMINATED and outcome is AttemptOutcome.NONE) or (
            phase is AttemptPhase.TERMINATED and outcome is not AttemptOutcome.NONE
        )
        if accepted:
            transition_rules.require_attempt_phase_outcome(phase, outcome)
        else:
            with pytest.raises(StateTransitionError):
                transition_rules.require_attempt_phase_outcome(phase, outcome)


def test_executor_phase_only_transitions_accept_only_frozen_reconciling_edges() -> None:
    """executor 普通写只开放 RUNNING→RECONCILING，终止必须走独立语义入口。"""
    require_step_phase_transition(
        StepPhase.RUNNING,
        StepPhase.RECONCILING,
        current_outcome=StepOutcome.NONE,
    )
    require_attempt_phase_transition(
        AttemptPhase.RUNNING,
        AttemptPhase.RECONCILING,
        current_outcome=AttemptOutcome.NONE,
    )


@pytest.mark.parametrize(
    ("current", "candidate"),
    [
        (StepPhase.RUNNING, StepPhase.PENDING),
        (StepPhase.RUNNING, StepPhase.RUNNING),
        (StepPhase.RUNNING, StepPhase.TERMINAL),
        (StepPhase.TERMINAL, StepPhase.RECONCILING),
    ],
)
def test_step_phase_only_transition_rejects_backward_self_terminal_and_terminal_rewrite(
    current: StepPhase,
    candidate: StepPhase,
) -> None:
    """Step phase-only CAS 不得倒退、自环、伪造 TERMINAL 或改写既有终态。"""
    with pytest.raises(StateTransitionError):
        require_step_phase_transition(current, candidate, current_outcome=StepOutcome.NONE)


@pytest.mark.parametrize(
    ("current", "candidate"),
    [
        (AttemptPhase.RUNNING, AttemptPhase.CREATED),
        (AttemptPhase.RUNNING, AttemptPhase.RUNNING),
        (AttemptPhase.RUNNING, AttemptPhase.TERMINATED),
        (AttemptPhase.TERMINATED, AttemptPhase.RECONCILING),
    ],
)
def test_attempt_phase_only_transition_rejects_backward_self_terminal_and_terminal_rewrite(
    current: AttemptPhase,
    candidate: AttemptPhase,
) -> None:
    """Attempt phase-only CAS 不得绕过 termination outcome/drain/time 的原子语义。"""
    with pytest.raises(StateTransitionError):
        require_attempt_phase_transition(current, candidate, current_outcome=AttemptOutcome.NONE)


def test_phase_only_transition_requires_none_outcome() -> None:
    """非终态 projection 已携带 outcome 时属于损坏事实，不能继续普通状态推进。"""
    with pytest.raises(StateTransitionError):
        require_step_phase_transition(
            StepPhase.RUNNING,
            StepPhase.RECONCILING,
            current_outcome=StepOutcome.SUCCEEDED,
        )
    with pytest.raises(StateTransitionError):
        require_attempt_phase_transition(
            AttemptPhase.RUNNING,
            AttemptPhase.RECONCILING,
            current_outcome=AttemptOutcome.SUCCEEDED,
        )


@pytest.mark.parametrize("current", tuple(AttemptPhase)[:-1])
def test_attempt_termination_accepts_nonterminal_none_to_non_none(current: AttemptPhase) -> None:
    """独立 termination 允许任一合法非终态进入 TERMINATED，并要求非 NONE 结果。"""
    require_attempt_termination_transition(
        current,
        current_outcome=AttemptOutcome.NONE,
        terminal_outcome=AttemptOutcome.UNKNOWN_REMOTE_STATE,
    )


@pytest.mark.parametrize(
    ("current", "current_outcome", "terminal_outcome"),
    [
        (AttemptPhase.TERMINATED, AttemptOutcome.SUCCEEDED, AttemptOutcome.FAILED),
        (AttemptPhase.RUNNING, AttemptOutcome.SUCCEEDED, AttemptOutcome.FAILED),
        (AttemptPhase.RUNNING, AttemptOutcome.NONE, AttemptOutcome.NONE),
    ],
)
def test_attempt_termination_rejects_terminal_rewrite_or_invalid_outcome(
    current: AttemptPhase,
    current_outcome: AttemptOutcome,
    terminal_outcome: AttemptOutcome,
) -> None:
    """既有终态、非终态异常 outcome 与 NONE 终局结果一律拒绝。"""
    with pytest.raises(StateTransitionError):
        require_attempt_termination_transition(
            current,
            current_outcome=current_outcome,
            terminal_outcome=terminal_outcome,
        )


def test_state_001_contribution_can_only_emit_unique_partial_subcheck() -> None:
    """Task 2 只生成 Phase 1 PARTIAL，不得冒充正式 STATE-001 FINAL receipt。"""
    checks = {
        "five_dimensions": True,
        "observed_legal_illegal": True,
        "barrier_settled_passed": True,
        "unknown_settle_timeout": True,
        "milestone_evidence": True,
        "fast_pause_resume": True,
        "cas_competition": True,
    }
    receipt = build_state_001_partial_receipt(checks)
    assert receipt == {
        "subcheckId": "phase1-task2-state-control-v1",
        "contributesTo": "STATE-001",
        "qualification": "PARTIAL",
        "status": "PASS",
        "passedChecks": tuple(checks),
    }
    assert "testId" not in receipt
    assert "finalPassOwner" not in receipt
    with pytest.raises(StateScenarioEvidenceError):
        build_state_001_partial_receipt({**checks, "cas_competition": False})
