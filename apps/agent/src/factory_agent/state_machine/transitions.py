"""五维权威状态的确定性转换表与 fail-closed 校验。"""

from __future__ import annotations

from enum import StrEnum
from typing import NoReturn

from factory_agent.domain.workflow import (
    AchievedStage,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    TaskLifecycle,
)
from factory_agent.errors import FactoryError


class StateTransitionError(FactoryError):
    """状态边不在冻结转换图时抛出，错误消息不携带业务 payload。"""

    error_code = "INVALID_STATE_TRANSITION"


class PhaseTransitionReason(StrEnum):
    """业务阶段非顺序跳转的闭集原因。"""

    REPAIR = "REPAIR"
    REPLAN = "REPLAN"
    ROLLBACK = "ROLLBACK"


_OBSERVED_TRANSITIONS: dict[RunObservedState, frozenset[RunObservedState]] = {
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

_DESIRED_TRANSITIONS: dict[RunDesiredState, frozenset[RunDesiredState]] = {
    RunDesiredState.RUNNING: frozenset({RunDesiredState.PAUSED, RunDesiredState.CANCELLED}),
    RunDesiredState.PAUSED: frozenset({RunDesiredState.RUNNING, RunDesiredState.CANCELLED}),
    RunDesiredState.CANCELLED: frozenset(),
}

_PUBLISH_OR_ACCEPT_PHASES = frozenset(
    {
        RunPhase.PUBLISHING_PR,
        RunPhase.MERGING,
        RunPhase.BUILDING_ARTIFACT,
        RunPhase.DEPLOYING_STAGING,
        RunPhase.ACCEPTING_STAGING,
        RunPhase.DEPLOYING_PRODUCTION,
        RunPhase.ACCEPTING_PRODUCTION,
    }
)
_REPLANNABLE_PHASES = frozenset(
    {
        RunPhase.DESIGN_REVIEWING,
        RunPhase.PREPARING_WORKSPACE,
        RunPhase.IMPLEMENTING,
        RunPhase.VERIFYING,
        RunPhase.CODE_REVIEWING,
    }
)


def _reject() -> NoReturn:
    """统一产生稳定、脱敏的状态机拒绝分类。"""
    raise StateTransitionError("state edge is outside the frozen transition graph")


def require_observed_transition(current: RunObservedState, candidate: RunObservedState) -> None:
    """校验 §7.3 observed_state 的精确有向边，自环也不算新转换。"""
    current_state = RunObservedState(current)
    candidate_state = RunObservedState(candidate)
    if candidate_state not in _OBSERVED_TRANSITIONS[current_state]:
        _reject()


def require_desired_transition(current: RunDesiredState, candidate: RunDesiredState) -> None:
    """校验用户意图转换；CANCELLED 进入后不可逆。"""
    current_state = RunDesiredState(current)
    candidate_state = RunDesiredState(candidate)
    if candidate_state not in _DESIRED_TRANSITIONS[current_state]:
        _reject()


def require_task_lifecycle_transition(current: TaskLifecycle, candidate: TaskLifecycle) -> None:
    """任务终局只允许从 ACTIVE 进入一次，终态不再产生状态边。"""
    current_state = TaskLifecycle(current)
    candidate_state = TaskLifecycle(candidate)
    if current_state is not TaskLifecycle.ACTIVE or candidate_state is TaskLifecycle.ACTIVE:
        _reject()


def require_phase_transition(
    current: RunPhase,
    candidate: RunPhase,
    *,
    reason: PhaseTransitionReason | None = None,
) -> None:
    """校验正常顺序与修复、重规划、回滚三种有原因的非顺序边。"""
    current_phase = RunPhase(current)
    candidate_phase = RunPhase(candidate)
    phases = tuple(RunPhase)
    current_index = phases.index(current_phase)
    if reason is None:
        if current_index + 1 < len(phases) and phases[current_index + 1] is candidate_phase:
            return
        _reject()
    transition_reason = PhaseTransitionReason(reason)
    if (
        transition_reason is PhaseTransitionReason.REPAIR
        and current_phase is RunPhase.CODE_REVIEWING
        and candidate_phase is RunPhase.IMPLEMENTING
    ):
        return
    if (
        transition_reason is PhaseTransitionReason.REPLAN
        and current_phase in _REPLANNABLE_PHASES
        and candidate_phase is RunPhase.PLANNING
    ):
        return
    if (
        transition_reason is PhaseTransitionReason.ROLLBACK
        and current_phase in _PUBLISH_OR_ACCEPT_PHASES
        and candidate_phase is RunPhase.ROLLING_BACK
    ):
        return
    _reject()


def require_achieved_stage_transition(current: AchievedStage, candidate: AchievedStage) -> None:
    """校验里程碑单调投影；同值允许幂等重放，跨级与倒退均拒绝。"""
    current_stage = AchievedStage(current)
    candidate_stage = AchievedStage(candidate)
    stages = tuple(AchievedStage)
    current_index = stages.index(current_stage)
    candidate_index = stages.index(candidate_stage)
    if candidate_index not in {current_index, current_index + 1}:
        _reject()


__all__ = [
    "PhaseTransitionReason",
    "StateTransitionError",
    "require_achieved_stage_transition",
    "require_desired_transition",
    "require_observed_transition",
    "require_phase_transition",
    "require_task_lifecycle_transition",
]
