"""Task/Run/Barrier/Step/Attempt 的纯领域值对象与最小冻结不变量。

本节点只提供 Task 1 所需的不可变 selector、阶段单调性和 Attempt 新身份规则；
完整状态机、barrier gate 与恢复编排属于后续任务，不在这里猜测实现。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum, StrEnum
from typing import Any

from factory_agent.errors import FactoryError
from factory_agent.policy.plan_hash import barrier_id as derive_barrier_id


class TaskLifecycle(StrEnum):
    ACTIVE = "ACTIVE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED_NEEDS_INTERVENTION = "FAILED_NEEDS_INTERVENTION"


class TargetStage(StrEnum):
    DESIGN_APPROVED = "DESIGN_APPROVED"
    CODEX_APPROVED = "CODEX_APPROVED"
    PR_READY = "PR_READY"
    MERGED = "MERGED"
    STAGING_ACCEPTED = "STAGING_ACCEPTED"
    PRODUCTION_ACCEPTED = "PRODUCTION_ACCEPTED"


class AchievedStage(StrEnum):
    NONE = "NONE"
    DESIGN_APPROVED = "DESIGN_APPROVED"
    CODEX_APPROVED = "CODEX_APPROVED"
    PR_READY = "PR_READY"
    MERGED = "MERGED"
    STAGING_ACCEPTED = "STAGING_ACCEPTED"
    PRODUCTION_ACCEPTED = "PRODUCTION_ACCEPTED"


class RunDesiredState(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"


class RunObservedState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    INTERRUPTED = "INTERRUPTED"
    RECONCILING = "RECONCILING"
    BLOCKED = "BLOCKED"
    TERMINATED = "TERMINATED"


class RunPhase(StrEnum):
    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    BOOTSTRAPPING_REPOSITORY = "BOOTSTRAPPING_REPOSITORY"
    PLANNING = "PLANNING"
    DESIGN_REVIEWING = "DESIGN_REVIEWING"
    PREPARING_WORKSPACE = "PREPARING_WORKSPACE"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    CODE_REVIEWING = "CODE_REVIEWING"
    PUBLISHING_PR = "PUBLISHING_PR"
    MERGING = "MERGING"
    BUILDING_ARTIFACT = "BUILDING_ARTIFACT"
    DEPLOYING_STAGING = "DEPLOYING_STAGING"
    ACCEPTING_STAGING = "ACCEPTING_STAGING"
    DEPLOYING_PRODUCTION = "DEPLOYING_PRODUCTION"
    ACCEPTING_PRODUCTION = "ACCEPTING_PRODUCTION"
    ROLLING_BACK = "ROLLING_BACK"
    FINALIZING = "FINALIZING"


class StepPhase(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    RECONCILING = "RECONCILING"
    TERMINAL = "TERMINAL"


class StepOutcome(StrEnum):
    NONE = "NONE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    UNKNOWN_REMOTE_STATE = "UNKNOWN_REMOTE_STATE"


class AttemptPhase(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    INTERRUPTING = "INTERRUPTING"
    RECONCILING = "RECONCILING"
    TERMINATED = "TERMINATED"


class AttemptOutcome(StrEnum):
    NONE = "NONE"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    KILLED = "KILLED"
    LOST = "LOST"
    UNKNOWN_REMOTE_STATE = "UNKNOWN_REMOTE_STATE"


class DrainState(StrEnum):
    NONE = "NONE"
    DRAINING = "DRAINING"
    DRAINED = "DRAINED"


class WorkflowContractError(FactoryError):
    """工作流 selector 形状或枚举非法时抛出。"""

    error_code = "INVALID_WORKFLOW_RECORD"


class StageRegressionError(FactoryError):
    """achieved stage 尝试后退时抛出。"""

    error_code = "ACHIEVED_STAGE_REGRESSION"


class AttemptReuseError(FactoryError):
    """恢复或重派尝试复用旧 Attempt identity 时抛出。"""

    error_code = "ATTEMPT_ID_REUSE"


class AttemptDispatchError(FactoryError):
    """重派控制事实类型、范围或 fencing 单调性非法时抛出。"""

    error_code = "INVALID_ATTEMPT_DISPATCH"


def _exact_record(record: Mapping[str, Any], expected: tuple[str, ...]) -> dict[str, Any]:
    """复制 exact-set selector；缺字段和未知字段均 fail closed。"""
    if not isinstance(record, Mapping) or frozenset(record) != frozenset(expected):
        raise WorkflowContractError("workflow record fields do not match the frozen contract")
    return dict(record)


def _enum[EnumT: Enum](enum_type: type[EnumT], value: object) -> EnumT:
    """在领域边界把 wire 字符串收敛为闭集枚举。"""
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise WorkflowContractError("workflow enum is outside the frozen closed set") from exc


@dataclass(frozen=True, slots=True)
class Task:
    """任务 selector；只实现 achieved stage 单调前进这一 Task 1 不变量。"""

    task_id: str
    project_id: str
    lifecycle: TaskLifecycle
    target_stage: TargetStage
    achieved_stage: AchievedStage
    active_plan_revision_id: str | None
    active_run_id: str | None
    state_version: int
    outcome_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Task:
        """从 tasks 精确 selector 构建不可变实体，不套用旧 ID 格式约束。"""
        values = _exact_record(record, tuple(cls.__dataclass_fields__))
        values["lifecycle"] = _enum(TaskLifecycle, values["lifecycle"])
        values["target_stage"] = _enum(TargetStage, values["target_stage"])
        values["achieved_stage"] = _enum(AchievedStage, values["achieved_stage"])
        return cls(**values)

    def advance_achieved_stage(self, new_stage: AchievedStage) -> Task:
        """只允许 achieved stage 单调前进；目标阶段不是已达事实的错误上限。"""
        candidate = _enum(AchievedStage, new_stage)
        order = tuple(AchievedStage)
        if order.index(candidate) < order.index(self.achieved_stage):
            raise StageRegressionError("achieved stage cannot move backward")
        if candidate is self.achieved_stage:
            return self
        return replace(self, achieved_stage=candidate, state_version=self.state_version + 1)


@dataclass(frozen=True, slots=True)
class Run:
    """运行 selector 的不可变快照；本阶段不实现完整转移状态机。"""

    run_id: str
    task_id: str
    desired_state: RunDesiredState
    observed_state: RunObservedState
    phase: RunPhase
    active_barrier_id: str | None
    dag_version: int
    run_cursor: str | None
    durable_cursor: str | None
    control_command_seq: int
    repair_loop_used: int
    auto_replan_used: int
    requires_user_action: bool
    block_reason_code: str | None
    budget_accumulated_ms: int
    budget_clock_state: str | None
    budget_clock_boot_id: str | None
    budget_clock_monotonic_ns: int | None
    budget_clock_wall_time: str | None
    budget_suspension_reason: str | None
    executor_id: str | None
    host_id: str | None
    runtime: str | None
    protocol_version: str
    recovery_target_phase: str | None
    state_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Run:
        """从 runs 精确 selector 构建不可变运行快照。"""
        values = _exact_record(record, tuple(cls.__dataclass_fields__))
        values["desired_state"] = _enum(RunDesiredState, values["desired_state"])
        values["observed_state"] = _enum(RunObservedState, values["observed_state"])
        values["phase"] = _enum(RunPhase, values["phase"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class PhaseBarrier:
    """按真实 run 派生 identity 的运行时 barrier；plan 中绝不保存 barrier_id。"""

    barrier_id: str
    run_id: str
    plan_revision_id: str
    business_phase: str
    barrier_ordinal: int
    required_node_set_digest: str
    settle_timeout_ms: int
    settle_deadline_at: str | None
    pass_predicate_id: str
    settled: bool
    passed: bool
    gate_digest: str | None
    state_version: int
    _required_node_ids: tuple[str, ...] = field(default=(), repr=False, compare=False)

    @property
    def required_node_ids(self) -> tuple[str, ...]:
        """返回 plan barrier 的只读 required node 顺序；持久行只保存其摘要。"""
        return self._required_node_ids

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> PhaseBarrier:
        """从 phase_barriers 精确 selector 构建不可变运行时 barrier。"""
        public_fields = tuple(name for name in cls.__dataclass_fields__ if not name.startswith("_"))
        return cls(**_exact_record(record, public_fields))

    @classmethod
    def from_plan_barrier(
        cls,
        *,
        run_id: str,
        plan_revision_id: str,
        plan_revision_digest: str,
        plan_barrier: Mapping[str, Any],
    ) -> PhaseBarrier:
        """从五字段 plan spec 与真实 run/revision 派生运行时 barrier identity。"""
        expected = (
            "businessPhase",
            "barrierOrdinal",
            "requiredNodeIds",
            "settleTimeoutMs",
            "passPredicateId",
        )
        spec = _exact_record(plan_barrier, expected)
        required_node_ids = tuple(spec["requiredNodeIds"])
        import hashlib

        from factory_agent.policy.canonical_json import canonicalize

        required_digest = "sha256:" + hashlib.sha256(canonicalize(list(required_node_ids))).hexdigest()
        return cls(
            barrier_id=derive_barrier_id(
                run_id,
                plan_revision_digest,
                spec["businessPhase"],
                spec["barrierOrdinal"],
            ),
            run_id=run_id,
            plan_revision_id=plan_revision_id,
            business_phase=spec["businessPhase"],
            barrier_ordinal=spec["barrierOrdinal"],
            required_node_set_digest=required_digest,
            settle_timeout_ms=spec["settleTimeoutMs"],
            settle_deadline_at=None,
            pass_predicate_id=spec["passPredicateId"],
            settled=False,
            passed=False,
            gate_digest=None,
            state_version=0,
            _required_node_ids=required_node_ids,
        )


@dataclass(frozen=True, slots=True)
class Step:
    """步骤 selector 的不可变快照；调度与 gate 判定不属于本节点。"""

    step_id: str
    run_id: str
    plan_revision_id: str
    barrier_id: str
    logical_node_id: str
    business_phase: str
    node_type: str
    required: bool
    side_effect_class: str
    phase: StepPhase
    outcome: StepOutcome
    dependency_hash: str
    required_artifacts_digest: str
    success_predicate_id: str
    timeout_ms: int
    retry_policy_id: str
    idempotency_key: str
    state_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Step:
        """从 steps 精确 selector 构建不可变步骤快照。"""
        values = _exact_record(record, tuple(cls.__dataclass_fields__))
        values["phase"] = _enum(StepPhase, values["phase"])
        values["outcome"] = _enum(StepOutcome, values["outcome"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class Attempt:
    """Attempt 选择器与自身 CAS state_version；旧 identity 永不复用。"""

    attempt_id: str
    step_id: str
    supersedes_attempt_id: str | None
    phase: AttemptPhase
    outcome: AttemptOutcome
    executor_id: str | None
    process_session_id: str | None
    pid: int | None
    process_start_time: str | None
    job_object_id: str | None
    wsl_distro: str | None
    container_id: str | None
    image_digest: str | None
    exit_code: int | None
    termination_reason: str | None
    fencing_token: int
    control_epoch: int
    accepted_control_command_seq: int
    interrupt_command_id: str | None
    drain_state: DrainState
    started_at: str | None
    ended_at: str | None
    state_version: int

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> Attempt:
        """从 attempts 精确 selector 构建不可变 Attempt。"""
        values = _exact_record(record, tuple(cls.__dataclass_fields__))
        values["phase"] = _enum(AttemptPhase, values["phase"])
        values["outcome"] = _enum(AttemptOutcome, values["outcome"])
        values["drain_state"] = _enum(DrainState, values["drain_state"])
        return cls(**values)

    def supersede(
        self,
        *,
        new_attempt_id: str,
        fencing_token: int,
        control_epoch: int,
        accepted_control_command_seq: int,
    ) -> Attempt:
        """按新派发 fencing/control 事实创建干净后继，绝不复制旧进程或终态证据。"""
        if type(new_attempt_id) is not str or not new_attempt_id or new_attempt_id == self.attempt_id:
            raise AttemptReuseError("superseding attempt requires a fresh identity")
        max_safe_integer = 2**53 - 1
        dispatch_values = (fencing_token, control_epoch, accepted_control_command_seq)
        if (
            any(type(value) is not int or not 0 <= value <= max_safe_integer for value in dispatch_values)
            or type(self.fencing_token) is not int
            or not 0 <= self.fencing_token <= max_safe_integer
            or fencing_token <= self.fencing_token
        ):
            # 仅暴露稳定分类；禁止把 fencing/control 原值写入异常或日志。
            raise AttemptDispatchError("invalid attempt dispatch facts")
        # 重派是新执行边界：仅保留 step/supersession 链和调用方显式提供的新控制事实；
        # executor、进程、容器、终止、drain 与时间事实均属于旧 Attempt，必须清空。
        return Attempt(
            attempt_id=new_attempt_id,
            step_id=self.step_id,
            supersedes_attempt_id=self.attempt_id,
            phase=AttemptPhase.CREATED,
            outcome=AttemptOutcome.NONE,
            executor_id=None,
            process_session_id=None,
            pid=None,
            process_start_time=None,
            job_object_id=None,
            wsl_distro=None,
            container_id=None,
            image_digest=None,
            exit_code=None,
            termination_reason=None,
            fencing_token=fencing_token,
            control_epoch=control_epoch,
            accepted_control_command_seq=accepted_control_command_seq,
            interrupt_command_id=None,
            drain_state=DrainState.NONE,
            started_at=None,
            ended_at=None,
            state_version=0,
        )


__all__ = [
    "AchievedStage",
    "Attempt",
    "AttemptDispatchError",
    "AttemptOutcome",
    "AttemptPhase",
    "AttemptReuseError",
    "DrainState",
    "PhaseBarrier",
    "Run",
    "RunDesiredState",
    "RunObservedState",
    "RunPhase",
    "StageRegressionError",
    "Step",
    "StepOutcome",
    "StepPhase",
    "TargetStage",
    "Task",
    "TaskLifecycle",
    "WorkflowContractError",
]
