"""Observed state 转换、目标谓词与 authoritative event 的应用服务。"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from factory_agent.domain.events import payload_digest
from factory_agent.domain.workflow import (
    AchievedStage,
    PhaseBarrier,
    Run,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    Task,
    TaskLifecycle,
)
from factory_agent.errors import FactoryError
from factory_agent.observability.logging import get_logger
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.state_machine.barriers import BarrierStepFacts, evaluate_barrier
from factory_agent.state_machine.milestones import MilestoneEvidence, evaluate_milestone
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork, StaleStateVersionError
from factory_agent.storage.sqlite.workflow_repository import (
    BarrierAuthorityFacts,
    ObservedStateAuthorityFacts,
    SqliteWorkflowRepository,
    WorkflowRepositoryError,
    _PlanBarrierAuthority,
)

LOGGER = get_logger(__name__)
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")


class TransitionPredicateError(FactoryError):
    """合法状态边的目标事实仍不完整时抛出。"""

    error_code = "STATE_PREDICATE_REJECTED"


class StateScenarioEvidenceError(FactoryError):
    """STATE-001 局部场景缺项、额外项或任一断言失败时抛出。"""

    error_code = "STATE_SCENARIO_EVIDENCE_INCOMPLETE"


class BarrierMilestoneCommitError(FactoryError):
    """barrier lineage、gate 或下一个 phase selector 不一致时抛出。"""

    error_code = "BARRIER_MILESTONE_COMMIT_REJECTED"


_STATE_001_PARTIAL_CHECKS = (
    "five_dimensions",
    "observed_legal_illegal",
    "barrier_settled_passed",
    "unknown_settle_timeout",
    "milestone_evidence",
    "fast_pause_resume",
    "cas_competition",
)


class TransactionCoordinator(Protocol):
    """应用服务所需的单写事务提交表面。"""

    async def execute[ResultT](
        self,
        *,
        operation: str,
        context: Mapping[str, object],
        command: Callable[[SqliteUnitOfWork], ResultT],
    ) -> ResultT: ...


@dataclass(frozen=True, slots=True)
class ObservedStateEvidence:
    """五维 observed 目标状态所需的权威执行事实摘要。"""

    active_attempt_count: int
    lease_active: bool
    authorization_active: bool
    control_sequence_current: bool
    heartbeat_valid: bool
    unknown_remote_state: bool
    unsettled_started_receipt: bool
    dispatch_state_valid: bool = False
    blocking_fact: bool = False
    block_reason_code: str | None = None
    pausing_fact: bool = False
    stopping_fact: bool = False
    interrupted_fact: bool = False
    reconciling_fact: bool = False
    terminated_fact: bool = False

    def __post_init__(self) -> None:
        """阻断负计数和 bool/int 混淆。"""
        booleans = (
            self.lease_active,
            self.authorization_active,
            self.control_sequence_current,
            self.heartbeat_valid,
            self.unknown_remote_state,
            self.unsettled_started_receipt,
            self.dispatch_state_valid,
            self.blocking_fact,
            self.pausing_fact,
            self.stopping_fact,
            self.interrupted_fact,
            self.reconciling_fact,
            self.terminated_fact,
        )
        if (
            type(self.active_attempt_count) is not int
            or self.active_attempt_count < 0
            or any(type(value) is not bool for value in booleans)
            or (
                self.block_reason_code is not None
                and (not isinstance(self.block_reason_code, str) or not self.block_reason_code)
            )
        ):
            raise TransitionPredicateError("observed state evidence is invalid")


@dataclass(frozen=True, slots=True)
class BarrierMilestoneRequest:
    """barrier 通过后一次事务推进 Run/Task 所需的闭集 selector 与证据摘要。"""

    request_id: str
    task_id: str
    run_id: str
    barrier_id: str
    expected_task_state_version: int
    expected_run_state_version: int
    expected_barrier_state_version: int
    next_phase: RunPhase
    next_barrier_id: str | None
    candidate_achieved_stage: AchievedStage
    gate_digest: str
    now: datetime
    steps: tuple[BarrierStepFacts, ...]
    milestone_evidence: MilestoneEvidence

    def __post_init__(self) -> None:
        """进入事务和日志前拒绝空 identity、旧式摘要与宽松数值。"""
        try:
            object.__setattr__(self, "next_phase", RunPhase(self.next_phase))
            object.__setattr__(self, "candidate_achieved_stage", AchievedStage(self.candidate_achieved_stage))
        except (TypeError, ValueError) as exc:
            raise BarrierMilestoneCommitError("barrier milestone enum is invalid") from exc
        versions = (
            self.expected_task_state_version,
            self.expected_run_state_version,
            self.expected_barrier_state_version,
        )
        if (
            any(
                not isinstance(value, str) or not value
                for value in (self.request_id, self.task_id, self.run_id, self.barrier_id)
            )
            or (
                self.next_barrier_id is not None
                and (not isinstance(self.next_barrier_id, str) or not self.next_barrier_id)
            )
            or any(type(value) is not int or value < 0 for value in versions)
            or not isinstance(self.gate_digest, str)
            or _SHA256.fullmatch(self.gate_digest) is None
            or not isinstance(self.now, datetime)
            or self.now.tzinfo is None
            or not isinstance(self.steps, tuple)
            or not all(isinstance(step, BarrierStepFacts) for step in self.steps)
            or not isinstance(self.milestone_evidence, MilestoneEvidence)
        ):
            raise BarrierMilestoneCommitError("barrier milestone request is invalid")


@dataclass(frozen=True, slots=True)
class BarrierMilestoneResult:
    """同一事务提交后的三个权威投影。"""

    barrier: PhaseBarrier
    run: Run
    task: Task


def require_observed_target_predicates(
    run: Run,
    candidate: RunObservedState,
    evidence: ObservedStateEvidence,
) -> None:
    """在合法边之外核对五维 observed 的权威事实与阻断原因。"""
    try:
        target = RunObservedState(candidate)
    except (TypeError, ValueError) as exc:
        raise TransitionPredicateError("observed state target enum is invalid") from exc
    valid = True
    if target is RunObservedState.RUNNING:
        valid = (
            run.desired_state is RunDesiredState.RUNNING
            and evidence.active_attempt_count == 1
            and evidence.lease_active
            and evidence.authorization_active
            and evidence.control_sequence_current
            and evidence.heartbeat_valid
            and evidence.dispatch_state_valid
            and not evidence.unknown_remote_state
        )
    elif target is RunObservedState.PAUSED:
        valid = (
            run.desired_state is RunDesiredState.PAUSED
            and evidence.active_attempt_count == 0
            and not evidence.lease_active
            and not evidence.heartbeat_valid
            and not evidence.authorization_active
            and not evidence.unknown_remote_state
            and not evidence.unsettled_started_receipt
        )
    elif target is RunObservedState.QUEUED:
        valid = (
            run.desired_state is RunDesiredState.RUNNING
            and evidence.active_attempt_count == 0
            and not evidence.lease_active
            and not evidence.authorization_active
            and not evidence.heartbeat_valid
            and not evidence.unknown_remote_state
            and not evidence.unsettled_started_receipt
        )
    elif target is RunObservedState.PAUSING:
        valid = (
            run.desired_state is RunDesiredState.PAUSED
            and evidence.active_attempt_count == 1
            and evidence.pausing_fact
            and not evidence.unknown_remote_state
        )
    elif target is RunObservedState.STOPPING:
        valid = (
            run.desired_state is RunDesiredState.PAUSED
            and evidence.active_attempt_count == 1
            and evidence.stopping_fact
            and not evidence.unknown_remote_state
        )
    elif target is RunObservedState.INTERRUPTED:
        valid = (
            run.desired_state is RunDesiredState.PAUSED
            and evidence.active_attempt_count == 0
            and evidence.interrupted_fact
            and not evidence.lease_active
            and not evidence.authorization_active
            and not evidence.heartbeat_valid
            and not evidence.unknown_remote_state
            and not evidence.unsettled_started_receipt
        )
    elif target is RunObservedState.RECONCILING:
        valid = evidence.reconciling_fact or evidence.unsettled_started_receipt or evidence.unknown_remote_state
    elif target is RunObservedState.BLOCKED:
        valid = evidence.blocking_fact and evidence.block_reason_code is not None
    elif target is RunObservedState.TERMINATED:
        valid = (
            evidence.terminated_fact
            and evidence.active_attempt_count == 0
            and not evidence.lease_active
            and not evidence.authorization_active
            and not evidence.heartbeat_valid
            and not evidence.unknown_remote_state
            and not evidence.unsettled_started_receipt
            and not evidence.blocking_fact
        )
    if not valid:
        raise TransitionPredicateError("observed state target facts are incomplete")


def append_authoritative_state_event(
    unit_of_work: SqliteUnitOfWork,
    *,
    state_event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    task_id: str,
    run_id: str | None,
    step_id: str | None,
    attempt_id: str | None,
    previous_state_version: int,
    state_version: int,
    payload: Mapping[str, object],
) -> None:
    """把状态 CAS 的脱敏摘要登记到 Task 1 同事务事件配对队列。"""
    scope = {"TASK": "TASK", "RUN": "RUN", "PHASE_BARRIER": "RUN", "STEP": "STEP", "ATTEMPT": "ATTEMPT"}[aggregate_type]
    event_payload = dict(payload)
    unit_of_work.events.append_authoritative_state_event(
        {
            "schemaVersion": 1,
            "stateEventId": state_event_id,
            "eventType": "state.changed",
            "durabilityClass": "authoritative_state",
            "taskId": task_id,
            "scope": scope,
            "aggregateType": aggregate_type,
            "aggregateId": aggregate_id,
            "runId": run_id,
            "stepId": step_id,
            "attemptId": attempt_id,
            "previousStateVersion": previous_state_version,
            "stateVersion": state_version,
            "payload": event_payload,
            "payloadDigest": payload_digest(event_payload),
        }
    )


def build_state_001_partial_receipt(checks: Mapping[str, bool]) -> dict[str, object]:
    """在七项本阶段证据全真时生成唯一 PARTIAL subcheck，绝不签发 FINAL。"""
    if tuple(checks) != _STATE_001_PARTIAL_CHECKS or any(
        type(value) is not bool or not value for value in checks.values()
    ):
        raise StateScenarioEvidenceError("STATE-001 partial evidence is incomplete")
    return {
        "subcheckId": "phase1-task2-state-control-v1",
        "contributesTo": "STATE-001",
        "qualification": "PARTIAL",
        "status": "PASS",
        "passedChecks": _STATE_001_PARTIAL_CHECKS,
    }


def _authoritative_gate_digest(
    authority: BarrierAuthorityFacts,
    *,
    settled: bool,
    passed: bool,
    timed_out: bool,
    block_reason_code: str | None,
) -> str:
    """只对事务内重算的 gate/证据摘要做 digest，不把请求 payload 当作真源。"""
    material = {
        "settled": settled,
        "passed": passed,
        "timedOut": timed_out,
        "blockReasonCode": block_reason_code,
        "activeAttemptCount": authority.active_attempt_count,
        "unknownRemoteState": authority.unknown_remote_state,
        "requiredArtifactsCommitted": authority.required_artifacts_committed,
        "blockingFindingCount": authority.blocking_finding_count,
        "requiredNodeIds": list(authority.required_node_ids),
        "requiredNodeSetDigest": authority.required_node_set_digest,
        "steps": [
            {
                "stepId": step.step_id,
                "required": step.required,
                "phase": step.phase.value,
                "outcome": step.outcome.value,
                "activeAttempt": step.active_attempt,
                "unsettledStartedReceipt": step.unsettled_started_receipt,
                "successPredicatePassed": step.success_predicate_passed,
                "requiredArtifactsCommitted": step.required_artifacts_committed,
                "blockingFindingOpen": step.blocking_finding_open,
            }
            for step in authority.steps
        ],
    }
    return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()


class TransitionService:
    """经唯一 coordinator 执行 observed state CAS 与事件原子提交。"""

    def __init__(
        self,
        *,
        coordinator: TransactionCoordinator,
        state_event_id_factory: Callable[[], str],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._state_event_id_factory = state_event_id_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def transition_observed(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate: RunObservedState,
        evidence: ObservedStateEvidence | None = None,
        request_id: str,
    ) -> Run:
        """只使用同一事务查询的权威事实，再写 projection 和 state.changed。"""
        started = time.monotonic()

        def command(unit_of_work: SqliteUnitOfWork) -> Run:
            repository = SqliteWorkflowRepository(unit_of_work)
            current = repository.get_run(run_id)
            if current is None:
                raise WorkflowRepositoryError("run does not exist")
            authority: ObservedStateAuthorityFacts = repository.load_observed_state_authority(
                run_id=run_id,
                now=self._now(),
            )
            # 保留旧参数仅为兼容调用面；任何调用方摘要都不进入状态判定。
            require_observed_target_predicates(
                current,
                candidate,
                ObservedStateEvidence(
                    active_attempt_count=authority.active_attempt_count,
                    lease_active=authority.lease_active,
                    authorization_active=authority.authorization_active,
                    control_sequence_current=authority.control_sequence_current,
                    heartbeat_valid=authority.heartbeat_valid,
                    unknown_remote_state=authority.unknown_remote_state,
                    unsettled_started_receipt=authority.unsettled_started_receipt,
                    dispatch_state_valid=authority.dispatch_state_valid,
                    blocking_fact=authority.blocking_fact,
                    block_reason_code=authority.block_reason_code,
                    pausing_fact=authority.pausing_fact,
                    stopping_fact=authority.stopping_fact,
                    interrupted_fact=authority.interrupted_fact,
                    reconciling_fact=authority.reconciling_fact,
                    terminated_fact=authority.terminated_fact,
                ),
            )
            updated = repository.transition_observed(
                run_id=run_id,
                expected_state_version=expected_state_version,
                candidate=candidate,
            )
            append_authoritative_state_event(
                unit_of_work,
                state_event_id=self._state_event_id_factory(),
                aggregate_type="RUN",
                aggregate_id=run_id,
                task_id=updated.task_id,
                run_id=run_id,
                step_id=None,
                attempt_id=None,
                previous_state_version=expected_state_version,
                state_version=updated.state_version,
                payload={"observedState": updated.observed_state.value},
            )
            return updated

        try:
            updated = await self._coordinator.execute(
                operation="transition_observed_state",
                context={"request_id": request_id, "run_id": run_id},
                command=command,
            )
        except Exception as exc:
            LOGGER.warning(
                "observed_state_transition_rejected",
                request_id=request_id,
                run_id=run_id,
                operation="transition_observed_state",
                status="rejected",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=getattr(exc, "error_code", type(exc).__name__),
            )
            raise
        LOGGER.info(
            "observed_state_transition_committed",
            request_id=request_id,
            run_id=run_id,
            operation="transition_observed_state",
            status="committed",
            duration_ms=round((time.monotonic() - started) * 1000, 3),
            state_version=updated.state_version,
        )
        return updated

    def _commit_passed_barrier(
        self,
        unit_of_work: SqliteUnitOfWork,
        request: BarrierMilestoneRequest,
    ) -> BarrierMilestoneResult:
        """在 owner transaction 内核对 lineage/gate，并登记三组 CAS↔event。"""
        repository = SqliteWorkflowRepository(unit_of_work)
        current_task = repository.get_task(request.task_id)
        current_run = repository.get_run(request.run_id)
        current_barrier = repository.get_phase_barrier(request.barrier_id)
        if current_task is None or current_run is None or current_barrier is None:
            raise BarrierMilestoneCommitError("barrier milestone lineage is missing")
        if (
            current_run.task_id != current_task.task_id
            or current_task.lifecycle is not TaskLifecycle.ACTIVE
            or current_task.active_run_id != current_run.run_id
            or current_task.active_plan_revision_id != current_barrier.plan_revision_id
            or current_barrier.run_id != current_run.run_id
            or current_run.active_barrier_id != current_barrier.barrier_id
            or current_barrier.business_phase != current_run.phase.value
            or current_barrier.settled
            or current_barrier.passed
        ):
            raise BarrierMilestoneCommitError("barrier milestone lineage is inconsistent")
        if (
            current_task.state_version != request.expected_task_state_version
            or current_barrier.state_version != request.expected_barrier_state_version
            or current_run.state_version != request.expected_run_state_version
        ):
            # 版本竞争优先于请求 selector 解析，保证失败事务不会泄漏任何 lineage 判断副作用。
            raise StaleStateVersionError("stale barrier milestone aggregate version")
        try:
            plan_authority: _PlanBarrierAuthority = repository.load_barrier_plan_authority(
                barrier_id=current_barrier.barrier_id,
                run_id=current_run.run_id,
                task_id=current_task.task_id,
            )
        except WorkflowRepositoryError as exc:
            raise BarrierMilestoneCommitError("active PlanRevision authority is incomplete") from exc
        barrier_specs = plan_authority.barriers
        current_spec_indexes = [
            index
            for index, spec in enumerate(barrier_specs)
            if spec.get("businessPhase") == current_barrier.business_phase
            and spec.get("barrierOrdinal") == current_barrier.barrier_ordinal
        ]
        if len(current_spec_indexes) != 1:
            raise BarrierMilestoneCommitError("current barrier is not a unique frozen successor")
        current_spec_index = current_spec_indexes[0]
        expected_next_spec = (
            None if current_spec_index + 1 >= len(barrier_specs) else barrier_specs[current_spec_index + 1]
        )
        next_barrier = (
            None if request.next_barrier_id is None else repository.get_phase_barrier(request.next_barrier_id)
        )
        next_authority: _PlanBarrierAuthority | None = None
        if expected_next_spec is not None and request.next_barrier_id is not None:
            try:
                # 后继仍从同一 validated PlanRevision 重验五字段与派生 identity，不能只信请求 selector。
                next_authority = repository.load_barrier_plan_authority(
                    barrier_id=request.next_barrier_id,
                    run_id=current_run.run_id,
                    task_id=current_task.task_id,
                    require_active_barrier=False,
                )
            except WorkflowRepositoryError:
                next_authority = None
        expected_next_node_ids = expected_next_spec.get("requiredNodeIds") if expected_next_spec is not None else None
        expected_next_digest = (
            None
            if not isinstance(expected_next_node_ids, (tuple, list))
            else "sha256:" + hashlib.sha256(canonicalize(list(expected_next_node_ids))).hexdigest()
        )
        next_selector_valid = (
            expected_next_spec is None and request.next_barrier_id is None and request.next_phase is current_run.phase
        ) or (
            expected_next_spec is not None
            and request.next_barrier_id is not None
            and next_barrier is not None
            and next_authority is not None
            and next_barrier.run_id == current_run.run_id
            and next_barrier.plan_revision_id == current_barrier.plan_revision_id
            and next_barrier.business_phase == expected_next_spec.get("businessPhase")
            and next_barrier.barrier_ordinal == expected_next_spec.get("barrierOrdinal")
            and next_authority.business_phase == expected_next_spec.get("businessPhase")
            and next_authority.barrier_ordinal == expected_next_spec.get("barrierOrdinal")
            and next_authority.pass_predicate_id == expected_next_spec.get("passPredicateId")
            and next_authority.required_node_set_digest == expected_next_digest
            and next_authority.settle_timeout_ms == expected_next_spec.get("settleTimeoutMs")
            and request.next_phase.value == expected_next_spec.get("businessPhase")
        )
        # steps、milestone_evidence、gate_digest 仍保留在请求合同中兼容旧调用方，但不作为证据真源。
        try:
            settle_deadline = (
                None
                if current_barrier.settle_deadline_at is None
                else datetime.fromisoformat(current_barrier.settle_deadline_at)
            )
            # steps/attempts/receipts/artifacts/findings 必须在唯一写事务内重算。
            authority = repository.load_barrier_authority(
                barrier_id=current_barrier.barrier_id,
                run_id=current_run.run_id,
                task_id=current_task.task_id,
            )
            barrier_decision = evaluate_barrier(
                authority.steps,
                # 超时只能由服务端注入时钟决定，request.now 仅为兼容字段，绝不参与授权。
                now=self._now(),
                settle_deadline_at=settle_deadline,
            )
        except WorkflowRepositoryError as exc:
            raise BarrierMilestoneCommitError("barrier authority facts are incomplete") from exc
        except (TypeError, ValueError) as exc:
            raise BarrierMilestoneCommitError("barrier deadline is invalid") from exc
        if (
            not barrier_decision.settled
            and barrier_decision.requires_user_action
            and barrier_decision.block_reason_code == "UNKNOWN_REMOTE_STATE_TIMEOUT"
        ):
            # 未知远端状态不能伪造成功；超时在本事务落成 BLOCKED，并保留可追踪行动事件。
            if (
                current_task.state_version != request.expected_task_state_version
                or current_barrier.state_version != request.expected_barrier_state_version
                or current_run.state_version != request.expected_run_state_version
            ):
                # action-required 也是 barrier milestone 的一次闭集提交，三路版本必须同时匹配。
                raise StaleStateVersionError("stale barrier milestone aggregate version")
            updated_run = repository.block_run_for_action_required(
                run_id=current_run.run_id,
                expected_state_version=request.expected_run_state_version,
                reason_code=barrier_decision.block_reason_code,
            )
            append_authoritative_state_event(
                unit_of_work,
                state_event_id=self._state_event_id_factory(),
                aggregate_type="RUN",
                aggregate_id=updated_run.run_id,
                task_id=current_task.task_id,
                run_id=updated_run.run_id,
                step_id=None,
                attempt_id=None,
                previous_state_version=request.expected_run_state_version,
                state_version=updated_run.state_version,
                payload={
                    "observedState": updated_run.observed_state.value,
                    "requiresUserAction": updated_run.requires_user_action,
                    "blockReasonCode": updated_run.block_reason_code,
                    "actionRequired": True,
                },
            )
            return BarrierMilestoneResult(barrier=current_barrier, run=updated_run, task=current_task)
        try:
            # 通过前先确认当前是首个未通过 barrier，且后继没有被预置成 settled/passed。
            repository.validate_barrier_sequence(
                run_id=current_run.run_id,
                plan_revision_id=current_barrier.plan_revision_id,
                plan_revision_digest=plan_authority.plan_revision_digest,
                barrier_specs=barrier_specs,
                current_business_phase=current_barrier.business_phase,
                current_barrier_ordinal=current_barrier.barrier_ordinal,
            )
        except WorkflowRepositoryError as exc:
            raise BarrierMilestoneCommitError("barrier sequence is not the first unpassed projection") from exc
        passed_barrier_rows = repository._connection.execute(
            "SELECT business_phase,barrier_ordinal FROM phase_barriers "
            "WHERE run_id=? AND plan_revision_id=? AND passed=1",
            (current_run.run_id, current_barrier.plan_revision_id),
        ).fetchall()
        passed_keys = {(str(phase), int(ordinal)) for phase, ordinal in passed_barrier_rows}
        passed_keys.add((current_barrier.business_phase, current_barrier.barrier_ordinal))
        passed_node_ids: set[object] = set()
        for spec in barrier_specs:
            if (spec.get("businessPhase"), spec.get("barrierOrdinal")) not in passed_keys:
                continue
            required_node_ids = spec.get("requiredNodeIds", ())
            if not isinstance(required_node_ids, (tuple, list, set, frozenset)):
                raise BarrierMilestoneCommitError("PlanRevision barrier node selector is invalid")
            passed_node_ids.update(required_node_ids)
        candidate_stage_nodes = plan_authority.stage_maps.get(request.candidate_achieved_stage.value)
        if candidate_stage_nodes is None or not set(candidate_stage_nodes).issubset(passed_node_ids):
            raise BarrierMilestoneCommitError("candidate milestone is not derived from passed PlanRevision barriers")
        if not barrier_decision.settled or not barrier_decision.passed:
            raise BarrierMilestoneCommitError("barrier gate has not passed")
        gate_digest = _authoritative_gate_digest(
            authority,
            settled=barrier_decision.settled,
            passed=barrier_decision.passed,
            timed_out=barrier_decision.timed_out,
            block_reason_code=barrier_decision.block_reason_code,
        )
        milestone_evidence = MilestoneEvidence(
            barrier_passed=barrier_decision.passed,
            active_attempt_count=authority.active_attempt_count,
            unknown_remote_state=authority.unknown_remote_state,
            required_artifacts_committed=authority.required_artifacts_committed,
            blocking_finding_count=authority.blocking_finding_count,
        )
        milestone = evaluate_milestone(
            current=current_task.achieved_stage,
            candidate=request.candidate_achieved_stage,
            target=current_task.target_stage,
            evidence=milestone_evidence,
        )
        target_lifecycle = TaskLifecycle.SUCCEEDED if milestone.lifecycle_should_succeed else current_task.lifecycle
        if milestone.lifecycle_should_succeed:
            if request.next_barrier_id is not None or request.next_phase is not current_run.phase:
                # Task 目标已达成时，Run 必须在本事务直接收口，不能选择另一个后继继续执行。
                raise BarrierMilestoneCommitError("target milestone cannot select a successor")
        elif not next_selector_valid:
            raise BarrierMilestoneCommitError("next barrier selector is inconsistent")

        updated_barrier = repository.pass_phase_barrier(
            barrier_id=request.barrier_id,
            expected_state_version=request.expected_barrier_state_version,
            gate_digest=gate_digest,
        )
        append_authoritative_state_event(
            unit_of_work,
            state_event_id=self._state_event_id_factory(),
            aggregate_type="PHASE_BARRIER",
            aggregate_id=updated_barrier.barrier_id,
            task_id=current_task.task_id,
            run_id=current_run.run_id,
            step_id=None,
            attempt_id=None,
            previous_state_version=request.expected_barrier_state_version,
            state_version=updated_barrier.state_version,
            payload={"settled": True, "passed": True, "gateDigest": gate_digest},
        )
        updated_run = (
            repository.complete_run(
                run_id=request.run_id,
                expected_state_version=request.expected_run_state_version,
            )
            if milestone.lifecycle_should_succeed
            else repository.advance_run_phase(
                run_id=request.run_id,
                expected_state_version=request.expected_run_state_version,
                candidate_phase=request.next_phase,
                next_barrier_id=request.next_barrier_id,
            )
        )
        append_authoritative_state_event(
            unit_of_work,
            state_event_id=self._state_event_id_factory(),
            aggregate_type="RUN",
            aggregate_id=updated_run.run_id,
            task_id=current_task.task_id,
            run_id=updated_run.run_id,
            step_id=None,
            attempt_id=None,
            previous_state_version=request.expected_run_state_version,
            state_version=updated_run.state_version,
            payload={
                "phase": updated_run.phase.value,
                "activeBarrierId": updated_run.active_barrier_id,
                "observedState": updated_run.observed_state.value,
            },
        )
        updated_task = repository.advance_task_milestone(
            task_id=request.task_id,
            expected_state_version=request.expected_task_state_version,
            candidate_stage=milestone.achieved_stage,
            lifecycle=target_lifecycle,
            outcome_version=current_task.outcome_version + 1,
        )
        append_authoritative_state_event(
            unit_of_work,
            state_event_id=self._state_event_id_factory(),
            aggregate_type="TASK",
            aggregate_id=updated_task.task_id,
            task_id=updated_task.task_id,
            run_id=None,
            step_id=None,
            attempt_id=None,
            previous_state_version=request.expected_task_state_version,
            state_version=updated_task.state_version,
            payload={
                "achievedStage": updated_task.achieved_stage.value,
                "lifecycle": updated_task.lifecycle.value,
                "outcomeVersion": updated_task.outcome_version,
            },
        )
        return BarrierMilestoneResult(barrier=updated_barrier, run=updated_run, task=updated_task)

    async def commit_passed_barrier(self, request: BarrierMilestoneRequest) -> BarrierMilestoneResult:
        """原子提交 passed barrier、下个 phase/active barrier 与 Task 里程碑。"""
        started = time.monotonic()
        try:
            result = await self._coordinator.execute(
                operation="commit_passed_barrier",
                context={"request_id": request.request_id, "run_id": request.run_id},
                command=lambda unit_of_work: self._commit_passed_barrier(unit_of_work, request),
            )
        except Exception as exc:
            LOGGER.warning(
                "barrier_milestone_commit_rejected",
                request_id=request.request_id,
                run_id=request.run_id,
                operation="commit_passed_barrier",
                status="rejected",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                error_code=getattr(exc, "error_code", type(exc).__name__),
            )
            raise
        if result.run.requires_user_action:
            LOGGER.warning(
                "barrier_milestone_action_required",
                request_id=request.request_id,
                run_id=request.run_id,
                operation="commit_passed_barrier",
                status="action_required",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                block_reason_code=result.run.block_reason_code,
                run_state_version=result.run.state_version,
            )
        else:
            LOGGER.info(
                "barrier_milestone_committed",
                request_id=request.request_id,
                run_id=request.run_id,
                operation="commit_passed_barrier",
                status="committed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                task_state_version=result.task.state_version,
                run_state_version=result.run.state_version,
                barrier_state_version=result.barrier.state_version,
            )
        return result


__all__ = [
    "BarrierMilestoneCommitError",
    "BarrierMilestoneRequest",
    "BarrierMilestoneResult",
    "ObservedStateEvidence",
    "StateScenarioEvidenceError",
    "TransactionCoordinator",
    "TransitionPredicateError",
    "TransitionService",
    "append_authoritative_state_event",
    "build_state_001_partial_receipt",
    "require_observed_target_predicates",
]
