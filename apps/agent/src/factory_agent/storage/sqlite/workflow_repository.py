"""Task 2 工作流复合 CAS，复用 Task 1 UoW 的状态事件配对登记。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from factory_agent.domain.artifacts import Artifact
from factory_agent.domain.plans import PlanRevisionBundle
from factory_agent.domain.workflow import (
    AchievedStage,
    Attempt,
    AttemptOutcome,
    AttemptPhase,
    DrainState,
    PhaseBarrier,
    Run,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    StepOutcome,
    StepPhase,
    Task,
    TaskLifecycle,
)
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import barrier_id as derive_barrier_id
from factory_agent.state_machine.barriers import BarrierStepFacts
from factory_agent.state_machine.predicates import KNOWN_TERMINAL_OUTCOMES
from factory_agent.state_machine.transitions import (
    require_achieved_stage_transition,
    require_observed_transition,
    require_phase_transition,
    require_task_lifecycle_transition,
)
from factory_agent.state_machine.write_guards import require_new_attempt_dispatch
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork


class WorkflowRepositoryError(FactoryError):
    """工作流 lineage 不完整或出现多个活动 Attempt 时抛出。"""

    error_code = "WORKFLOW_REPOSITORY_INVARIANT"


@dataclass(frozen=True, slots=True)
class ObservedStateAuthorityFacts:
    """同一 owner transaction 从 Attempt/Lease/Auth/receipt 表投影的不可伪造事实。"""

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


@dataclass(frozen=True, slots=True)
class BarrierAuthorityFacts:
    """同一事务由 steps/attempts/receipts/artifacts/findings 重算的 gate 事实。"""

    steps: tuple[BarrierStepFacts, ...]
    active_attempt_count: int
    unknown_remote_state: bool
    required_artifacts_committed: bool
    blocking_finding_count: int
    required_node_ids: tuple[str, ...] = ()
    required_node_set_digest: str = ""


@dataclass(frozen=True, slots=True)
class _PlanBarrierAuthority:
    """已通过领域 hydrator 校验的当前 barrier 合同投影。"""

    plan_revision_id: str
    business_phase: str
    barrier_ordinal: int
    pass_predicate_id: str
    required_node_ids: tuple[str, ...]
    required_node_set_digest: str
    settle_timeout_ms: int
    nodes: Mapping[str, Mapping[str, object]]
    barriers: tuple[Mapping[str, object], ...]
    stage_maps: Mapping[str, tuple[str, ...]]


class SqliteWorkflowRepository:
    """在现有 UoW 内扩展多列 CAS，不创建第二连接或隐藏事务。"""

    def __init__(self, unit_of_work: SqliteUnitOfWork) -> None:
        self._unit_of_work = unit_of_work
        self._connection = unit_of_work._connection
        self._task1 = unit_of_work.workflow

    def get_run(self, run_id: str) -> Run | None:
        """读取并按 Task 1 精确 selector hydrate Run。"""
        row = self._task1.get_run(run_id)
        if row is None:
            return None
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def get_task(self, task_id: str) -> Task | None:
        """读取 Task 的权威 selector，供里程碑事务核对 target 与 outcomeVersion。"""
        row = self._task1.get_task(task_id)
        if row is None:
            return None
        return Task.from_record({key: row[key] for key in Task.__dataclass_fields__})

    @staticmethod
    def _hydrate_barrier(row: dict[str, object]) -> PhaseBarrier:
        """把 STRICT SQLite 的 0/1 显式收敛为 bool，避免把整数泄漏进领域层。"""
        record = {
            key: bool(row[key]) if key in {"settled", "passed"} else row[key]
            for key in PhaseBarrier.__dataclass_fields__
            if not key.startswith("_")
        }
        return PhaseBarrier.from_record(record)

    def get_phase_barrier(self, barrier_id: str) -> PhaseBarrier | None:
        """读取运行时 barrier 的权威 selector。"""
        row = self._task1.get_phase_barrier(barrier_id)
        return None if row is None else self._hydrate_barrier(row)

    def get_attempt(self, attempt_id: str) -> Attempt | None:
        """读取并按 Task 1 精确 selector hydrate Attempt。"""
        row = self._task1.get_attempt(attempt_id)
        if row is None:
            return None
        return Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})

    def get_active_attempt_for_run(self, run_id: str) -> Attempt | None:
        """返回唯一未终止且未 DRAINED Attempt；多个活动执行事实必须 fail closed。"""
        cursor = self._connection.execute(
            "SELECT a.* FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
            "WHERE s.run_id=? AND a.phase<>'TERMINATED' AND a.drain_state<>'DRAINED' "
            "ORDER BY a.attempt_id",
            (run_id,),
        )
        rows = cursor.fetchall()
        if len(rows) > 1:
            raise WorkflowRepositoryError("multiple active attempts exist for one run")
        if not rows:
            return None
        record = dict(zip((item[0] for item in cursor.description), rows[0], strict=True))
        return Attempt.from_record({key: record[key] for key in Attempt.__dataclass_fields__})

    def load_observed_state_authority(
        self,
        *,
        run_id: str,
        now: datetime,
    ) -> ObservedStateAuthorityFacts:
        """在状态 CAS 前从同一 SQLite 连接重算执行事实，调用方布尔摘要完全不参与授权。"""
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise WorkflowRepositoryError("authority fact clock is invalid")
        run = self.get_run(run_id)
        if run is None:
            raise WorkflowRepositoryError("run does not exist")
        active_cursor = self._connection.execute(
            "SELECT a.* FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
            "WHERE s.run_id=? AND a.phase<>'TERMINATED' AND a.drain_state<>'DRAINED' "
            "ORDER BY a.attempt_id",
            (run_id,),
        )
        active_rows = active_cursor.fetchall()
        if len(active_rows) > 1:
            raise WorkflowRepositoryError("multiple active attempts exist for one run")
        active_attempt = None
        if active_rows:
            record = dict(zip((item[0] for item in active_cursor.description), active_rows[0], strict=True))
            active_attempt = Attempt.from_record({key: record[key] for key in Attempt.__dataclass_fields__})
        now_text = now.astimezone(UTC).isoformat().replace("+00:00", "Z")
        lease_active = False
        heartbeat_valid = False
        authorization_active = False
        control_sequence_current = False
        dispatch_state_valid = False
        if active_attempt is not None:
            step_phase_row = self._connection.execute(
                "SELECT phase FROM steps WHERE step_id=? AND run_id=?",
                (active_attempt.step_id, run_id),
            ).fetchone()
            # 只有 Step 与 Attempt 都进入 RUNNING，观察态才可越过 QUEUED。
            dispatch_state_valid = (
                active_attempt.phase is AttemptPhase.RUNNING
                and active_attempt.drain_state is DrainState.NONE
                and step_phase_row is not None
                and step_phase_row[0] == StepPhase.RUNNING.value
            )
            lease_row = self._connection.execute(
                "SELECT 1 FROM resource_leases "
                "WHERE owner_executor_id=? AND fencing_token=? AND control_epoch=? "
                "AND heartbeat_at<=? AND expires_at>? LIMIT 1",
                (
                    active_attempt.executor_id,
                    active_attempt.fencing_token,
                    active_attempt.control_epoch,
                    now_text,
                    now_text,
                ),
            ).fetchone()
            lease_active = lease_row is not None
            heartbeat_valid = lease_active
            control_sequence_current = active_attempt.accepted_control_command_seq == run.control_command_seq
            authorization_active = (
                self._connection.execute(
                    "SELECT 1 FROM execution_authorizations "
                    "WHERE run_id=? AND step_id=? AND attempt_id=? AND executor_id=? "
                    "AND fencing_token=? AND control_epoch=? AND accepted_control_command_seq=? "
                    "AND consumption_state='AVAILABLE' AND revoked_at IS NULL "
                    "AND issued_at<=? AND expires_at>? LIMIT 1",
                    (
                        run_id,
                        active_attempt.step_id,
                        active_attempt.attempt_id,
                        active_attempt.executor_id,
                        active_attempt.fencing_token,
                        active_attempt.control_epoch,
                        active_attempt.accepted_control_command_seq,
                        now_text,
                        now_text,
                    ),
                ).fetchone()
                is not None
            )
        else:
            # Attempt 已收口时，Run 仍不能带着未撤销授权或其 lease 假装 PAUSED/QUEUED。
            authorization_active = (
                self._connection.execute(
                    "SELECT 1 FROM execution_authorizations "
                    "WHERE run_id=? AND consumption_state='AVAILABLE' AND revoked_at IS NULL "
                    "AND issued_at<=? AND expires_at>? LIMIT 1",
                    (run_id, now_text, now_text),
                ).fetchone()
                is not None
            )
            lease_active = (
                self._connection.execute(
                    "SELECT 1 FROM resource_leases AS l "
                    "JOIN execution_authorizations AS ea "
                    "ON ea.executor_id=l.owner_executor_id "
                    "AND ea.fencing_token=l.fencing_token "
                    "AND ea.control_epoch=l.control_epoch "
                    "WHERE ea.run_id=? AND ea.consumption_state='AVAILABLE' "
                    "AND ea.revoked_at IS NULL AND ea.issued_at<=? AND ea.expires_at>? "
                    "AND l.heartbeat_at<=? AND l.expires_at>? LIMIT 1",
                    (run_id, now_text, now_text, now_text, now_text),
                ).fetchone()
                is not None
            )
            heartbeat_valid = lease_active
        unknown_remote_state = (
            self._connection.execute(
                "SELECT 1 FROM steps AS s WHERE s.run_id=? AND s.outcome='UNKNOWN_REMOTE_STATE' "
                "UNION ALL SELECT 1 FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
                "WHERE s.run_id=? AND a.outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                (run_id, run_id),
            ).fetchone()
            is not None
        )
        attempt_fact_rows = self._connection.execute(
            "SELECT a.phase,a.outcome,a.drain_state FROM attempts AS a "
            "JOIN steps AS s ON s.step_id=a.step_id WHERE s.run_id=?",
            (run_id,),
        ).fetchall()
        step_fact_rows = self._connection.execute(
            "SELECT phase,outcome FROM steps WHERE run_id=?",
            (run_id,),
        ).fetchall()
        blocking_finding_row = self._connection.execute(
            "SELECT COUNT(*) FROM review_findings WHERE task_id=? "
            "AND UPPER(severity)='BLOCKING' "
            "AND UPPER(status) NOT IN ('CLOSED','RESOLVED','SUPERSEDED','DISMISSED')",
            (run.task_id,),
        ).fetchone()
        blocking_finding = bool(blocking_finding_row and int(blocking_finding_row[0]) > 0)
        blocking_fact = blocking_finding or unknown_remote_state or bool(run.requires_user_action)
        block_reason_code = run.block_reason_code
        if block_reason_code is None:
            if blocking_finding:
                block_reason_code = "BLOCKING_FINDING_OPEN"
            elif unknown_remote_state:
                block_reason_code = "UNKNOWN_REMOTE_STATE"
        pausing_fact = active_attempt is not None and active_attempt.drain_state is DrainState.DRAINING
        stopping_fact = active_attempt is not None and (
            active_attempt.phase is AttemptPhase.INTERRUPTING or active_attempt.drain_state is DrainState.DRAINING
        )
        interrupted_fact = active_attempt is None and any(
            outcome in {"INTERRUPTED", "KILLED", "LOST"} for _phase, outcome, _drain_state in attempt_fact_rows
        )
        reconciling_fact = (
            unknown_remote_state
            or any(
                phase == StepPhase.RECONCILING.value or phase == AttemptPhase.RECONCILING.value
                for phase, _outcome, _drain_state in attempt_fact_rows
            )
            or any(phase == StepPhase.RECONCILING.value for phase, _outcome in step_fact_rows)
        )
        unsettled_started_receipt = (
            self._connection.execute(
                "SELECT 1 FROM action_receipt_events AS r "
                "JOIN actions AS a ON a.action_id=r.action_id "
                "JOIN attempts AS at ON at.attempt_id=r.attempt_id "
                "JOIN steps AS s ON s.step_id=at.step_id "
                "WHERE s.run_id=? AND r.phase='STARTED' "
                "AND NOT EXISTS ("
                "SELECT 1 FROM action_receipt_events AS later "
                "WHERE later.action_id=r.action_id AND later.receipt_seq>r.receipt_seq "
                "AND later.phase IN ('COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')) LIMIT 1",
                (run_id,),
            ).fetchone()
            is not None
        )
        terminated_fact = (
            active_attempt is None
            and not lease_active
            and not authorization_active
            and not unsettled_started_receipt
            and bool(step_fact_rows)
            and all(
                phase == StepPhase.TERMINAL.value and outcome in {item.value for item in KNOWN_TERMINAL_OUTCOMES}
                for phase, outcome in step_fact_rows
            )
            and all(
                phase == AttemptPhase.TERMINATED.value
                and outcome
                in {
                    AttemptOutcome.SUCCEEDED.value,
                    AttemptOutcome.FAILED.value,
                    AttemptOutcome.INTERRUPTED.value,
                    AttemptOutcome.KILLED.value,
                    AttemptOutcome.LOST.value,
                }
                and drain_state in {DrainState.NONE.value, DrainState.DRAINED.value}
                for phase, outcome, drain_state in attempt_fact_rows
            )
            and not unknown_remote_state
            and not blocking_fact
        )
        return ObservedStateAuthorityFacts(
            active_attempt_count=len(active_rows),
            lease_active=lease_active,
            authorization_active=authorization_active,
            control_sequence_current=control_sequence_current,
            heartbeat_valid=heartbeat_valid,
            unknown_remote_state=unknown_remote_state,
            unsettled_started_receipt=unsettled_started_receipt,
            dispatch_state_valid=dispatch_state_valid,
            blocking_fact=blocking_fact,
            block_reason_code=block_reason_code,
            pausing_fact=pausing_fact,
            stopping_fact=stopping_fact,
            interrupted_fact=interrupted_fact,
            reconciling_fact=reconciling_fact,
            terminated_fact=terminated_fact,
        )

    def _load_required_node_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
        require_active_barrier: bool = True,
    ) -> _PlanBarrierAuthority:
        """从已验证 PlanRevisionBundle 派生 barrier 合同，并可校验后继投影。"""
        barrier_row = self._connection.execute(
            "SELECT plan_revision_id,business_phase,barrier_ordinal,required_node_set_digest,"
            "pass_predicate_id,settle_timeout_ms "
            "FROM phase_barriers WHERE barrier_id=? AND run_id=?",
            (barrier_id, run_id),
        ).fetchone()
        if barrier_row is None:
            raise WorkflowRepositoryError("barrier declaration is missing")
        if (
            type(barrier_row[0]) is not str
            or type(barrier_row[1]) is not str
            or type(barrier_row[2]) is not int
            or barrier_row[2] < 0
        ):
            raise WorkflowRepositoryError("barrier selector is invalid")
        run_row = self._connection.execute(
            "SELECT task_id,phase,active_barrier_id,dag_version FROM runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if run_row is None or run_row[0] != task_id:
            raise WorkflowRepositoryError("barrier run lineage is inconsistent")
        if require_active_barrier and (run_row[1] != barrier_row[1] or run_row[2] != barrier_id):
            raise WorkflowRepositoryError("barrier run lineage is inconsistent")
        task_row = self._connection.execute(
            "SELECT active_plan_revision_id,target_stage FROM tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task_row is None or task_row[0] != barrier_row[0]:
            raise WorkflowRepositoryError("barrier is not the current plan revision")
        plan_record = self._task1.get_plan_revision(str(barrier_row[0]))
        if plan_record is None or plan_record.get("task_id") != task_id:
            raise WorkflowRepositoryError("barrier plan lineage is incomplete")
        stored_digest = barrier_row[3]
        stored_pass_predicate_id = barrier_row[4]
        stored_settle_timeout_ms = barrier_row[5]
        if (
            type(stored_digest) is not str
            or type(stored_pass_predicate_id) is not str
            or type(stored_settle_timeout_ms) is not int
            or stored_settle_timeout_ms <= 0
        ):
            raise WorkflowRepositoryError("barrier node set digest is invalid")
        try:
            bundle = PlanRevisionBundle.from_persistence_record(plan_record)
            parsed_revision = bundle.plan_revision
        except (FactoryError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("validated plan revision is unavailable") from exc
        plan_revision_digest = parsed_revision.get("planRevisionDigest")
        if type(plan_revision_digest) is not str or plan_revision_digest != plan_record.get("plan_revision_digest"):
            raise WorkflowRepositoryError("plan revision digest is inconsistent")
        try:
            expected_barrier_id = derive_barrier_id(
                run_id,
                plan_revision_digest,
                str(barrier_row[1]),
                int(barrier_row[2]),
            )
        except (FactoryError, TypeError, ValueError) as exc:
            raise WorkflowRepositoryError("barrier identity cannot be derived") from exc
        if expected_barrier_id != barrier_id:
            raise WorkflowRepositoryError("barrier identity is not derived from plan authority")
        declared_dag_version = parsed_revision.get("dagVersion")
        run_spec = bundle.run_spec
        run_spec_work_plan = run_spec.get("workPlan")
        run_spec_dag_version = run_spec_work_plan.get("dagVersion") if isinstance(run_spec_work_plan, Mapping) else None
        if (
            type(run_row[3]) is not int
            or type(declared_dag_version) is not int
            or run_row[3] != declared_dag_version
            or run_row[3] != run_spec_dag_version
            or type(run_spec.get("targetStage")) is not str
            or run_spec.get("targetStage") != task_row[1]
        ):
            raise WorkflowRepositoryError("run plan selector is inconsistent")
        raw_barriers = parsed_revision.get("barriers")
        if not isinstance(raw_barriers, Sequence) or isinstance(raw_barriers, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("plan revision barrier declaration is missing")
        matching_barriers = [
            barrier
            for barrier in raw_barriers
            if isinstance(barrier, Mapping)
            and barrier.get("businessPhase") == barrier_row[1]
            and barrier.get("barrierOrdinal") == barrier_row[2]
        ]
        if len(matching_barriers) != 1:
            raise WorkflowRepositoryError("plan revision barrier selector is ambiguous")
        declared_barrier = matching_barriers[0]
        declared_timeout_ms = declared_barrier.get("settleTimeoutMs")
        if (
            declared_barrier.get("passPredicateId") != stored_pass_predicate_id
            or type(declared_timeout_ms) is not int
            or declared_timeout_ms <= 0
            or declared_timeout_ms != stored_settle_timeout_ms
        ):
            raise WorkflowRepositoryError("barrier predicate is not the plan predicate")
        raw_node_ids = declared_barrier.get("requiredNodeIds")
        if not isinstance(raw_node_ids, Sequence) or isinstance(raw_node_ids, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("required node declaration is empty")
        required_node_ids: list[str] = []
        for node_id in raw_node_ids:
            if type(node_id) is not str or not node_id:
                raise WorkflowRepositoryError("required node declaration is invalid")
            required_node_ids.append(node_id)
        if len(set(required_node_ids)) != len(required_node_ids):
            raise WorkflowRepositoryError("required node declaration is duplicated")
        expected_digest = "sha256:" + hashlib.sha256(canonicalize(required_node_ids)).hexdigest()
        if expected_digest != stored_digest:
            raise WorkflowRepositoryError("required node set digest is inconsistent")
        raw_nodes = parsed_revision.get("nodes")
        if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes, bytearray)):
            raise WorkflowRepositoryError("plan revision node registry is missing")
        nodes: dict[str, Mapping[str, object]] = {}
        for node in raw_nodes:
            if not isinstance(node, Mapping):
                raise WorkflowRepositoryError("plan revision node registry is invalid")
            node_id = node.get("logicalNodeId")
            if type(node_id) is not str or not node_id or node_id in nodes:
                raise WorkflowRepositoryError("plan revision node identity is invalid")
            required_artifacts = node.get("requiredArtifacts")
            if (
                not isinstance(required_artifacts, Sequence)
                or isinstance(required_artifacts, (str, bytes, bytearray))
                or any(type(artifact) is not str or not artifact for artifact in required_artifacts)
            ):
                raise WorkflowRepositoryError("plan revision artifact registry is invalid")
            if len(set(required_artifacts)) != len(required_artifacts):
                raise WorkflowRepositoryError("plan revision artifact registry is duplicated")
            depends_on = node.get("dependsOn")
            if (
                not isinstance(depends_on, Sequence)
                or isinstance(depends_on, (str, bytes, bytearray))
                or any(type(dependency) is not str or not dependency for dependency in depends_on)
                or len(set(depends_on)) != len(depends_on)
            ):
                raise WorkflowRepositoryError("plan revision dependency registry is invalid")
            if (
                any(
                    type(node.get(field)) is not str or not node.get(field)
                    for field in (
                        "businessPhase",
                        "nodeType",
                        "sideEffectClass",
                        "successPredicateId",
                        "retryPolicyId",
                    )
                )
                or type(node.get("barrierOrdinal")) is not int
                or type(node.get("required")) is not bool
            ):
                raise WorkflowRepositoryError("plan revision node role is invalid")
            nodes[node_id] = node
        for node in nodes.values():
            dependencies = node.get("dependsOn")
            if not isinstance(dependencies, Sequence) or isinstance(dependencies, (str, bytes, bytearray)):
                raise WorkflowRepositoryError("plan revision dependency selector is invalid")
            if any(dependency_id not in nodes for dependency_id in dependencies):
                raise WorkflowRepositoryError("plan revision dependency selector is invalid")
        for node_id in required_node_ids:
            node = nodes.get(node_id)
            if (
                node is None
                or node.get("businessPhase") != barrier_row[1]
                or node.get("barrierOrdinal") != barrier_row[2]
            ):
                raise WorkflowRepositoryError("required node is outside the current barrier")
            if node.get("required") is not True:
                raise WorkflowRepositoryError("required node role is inconsistent")
        plan_required_node_ids = {
            node_id
            for node_id, node in nodes.items()
            if node.get("businessPhase") == barrier_row[1]
            and node.get("barrierOrdinal") == barrier_row[2]
            and node.get("required") is True
        }
        if plan_required_node_ids != set(required_node_ids):
            raise WorkflowRepositoryError("plan required node selector is incomplete")
        raw_stage_maps = parsed_revision.get("stageMaps")
        if not isinstance(raw_stage_maps, Mapping):
            raise WorkflowRepositoryError("plan stage map registry is missing")
        stage_maps: dict[str, tuple[str, ...]] = {}
        for stage, stage_node_ids in raw_stage_maps.items():
            if (
                type(stage) is not str
                or not stage
                or not isinstance(stage_node_ids, Sequence)
                or isinstance(stage_node_ids, (str, bytes, bytearray))
            ):
                raise WorkflowRepositoryError("plan stage map registry is invalid")
            normalized_stage_nodes = tuple(stage_node_ids)
            if any(
                type(node_id) is not str or not node_id or node_id not in nodes for node_id in normalized_stage_nodes
            ):
                raise WorkflowRepositoryError("plan stage map node selector is invalid")
            if len(set(normalized_stage_nodes)) != len(normalized_stage_nodes):
                raise WorkflowRepositoryError("plan stage map node selector is duplicated")
            stage_maps[stage] = normalized_stage_nodes
        return _PlanBarrierAuthority(
            plan_revision_id=str(barrier_row[0]),
            business_phase=str(barrier_row[1]),
            barrier_ordinal=int(barrier_row[2]),
            pass_predicate_id=stored_pass_predicate_id,
            required_node_ids=tuple(required_node_ids),
            required_node_set_digest=stored_digest,
            settle_timeout_ms=stored_settle_timeout_ms,
            nodes=nodes,
            barriers=tuple(item for item in raw_barriers if isinstance(item, Mapping)),
            stage_maps=stage_maps,
        )

    def load_barrier_plan_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
        require_active_barrier: bool = True,
    ) -> _PlanBarrierAuthority:
        """暴露已验证 PlanRevision 投影；后继校验可跳过 active selector 但不跳过谱系。"""
        return self._load_required_node_authority(
            barrier_id=barrier_id,
            run_id=run_id,
            task_id=task_id,
            require_active_barrier=require_active_barrier,
        )

    def load_barrier_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
    ) -> BarrierAuthorityFacts:
        """从权威表重算 barrier，拒绝空步骤和调用方自报的成功摘要。"""
        plan_authority = self._load_required_node_authority(
            barrier_id=barrier_id,
            run_id=run_id,
            task_id=task_id,
        )
        required_node_ids = plan_authority.required_node_ids
        barrier_revision_id = plan_authority.plan_revision_id
        barrier_phase = plan_authority.business_phase
        required_node_set_digest = self._connection.execute(
            "SELECT required_node_set_digest FROM phase_barriers WHERE barrier_id=?",
            (barrier_id,),
        ).fetchone()[0]
        rows_cursor = self._connection.execute(
            "SELECT * FROM steps WHERE run_id=? AND barrier_id=? ORDER BY step_id",
            (run_id, barrier_id),
        )
        rows = rows_cursor.fetchall()
        if not rows:
            raise WorkflowRepositoryError("barrier has no authoritative steps")
        columns = tuple(item[0] for item in rows_cursor.description)
        facts: list[BarrierStepFacts] = []
        actual_node_ids: list[str] = []
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            if record["plan_revision_id"] != barrier_revision_id or record["business_phase"] != barrier_phase:
                raise WorkflowRepositoryError("barrier step lineage is inconsistent")
            logical_node_id = record["logical_node_id"]
            if type(logical_node_id) is not str or not logical_node_id:
                raise WorkflowRepositoryError("barrier logical node selector is invalid")
            plan_node = plan_authority.nodes.get(logical_node_id)
            if plan_node is None:
                raise WorkflowRepositoryError("barrier step is not declared by the plan")
            actual_node_ids.append(logical_node_id)
            try:
                phase = StepPhase(record["phase"])
                outcome = StepOutcome(record["outcome"])
                required_value = record["required"]
                if type(required_value) is not int or required_value not in (0, 1):
                    raise ValueError("step required flag is invalid")
                persisted_required = required_value == 1
            except (TypeError, ValueError, KeyError) as exc:
                raise WorkflowRepositoryError("barrier step selector is invalid") from exc
            declared_required = logical_node_id in required_node_ids
            expected_role = {
                "business_phase": plan_node.get("businessPhase"),
                "node_type": plan_node.get("nodeType"),
                "required": plan_node.get("required"),
                "side_effect_class": plan_node.get("sideEffectClass"),
                "success_predicate_id": plan_node.get("successPredicateId"),
                "timeout_ms": plan_node.get("timeoutMs"),
                "retry_policy_id": plan_node.get("retryPolicyId"),
            }
            if (
                record["business_phase"] != expected_role["business_phase"]
                or record["node_type"] != expected_role["node_type"]
                or record["side_effect_class"] != expected_role["side_effect_class"]
                or record["success_predicate_id"] != expected_role["success_predicate_id"]
                or record["timeout_ms"] != expected_role["timeout_ms"]
                or record["retry_policy_id"] != expected_role["retry_policy_id"]
            ):
                raise WorkflowRepositoryError("barrier step role or predicate is inconsistent")
            if type(expected_role["required"]) is not bool or expected_role["required"] != declared_required:
                raise WorkflowRepositoryError("plan node required role is inconsistent")
            if persisted_required != declared_required:
                # required 身份由不可变 PlanRevision 派生，数据库漂移不能削弱 Artifact/成功谓词门禁。
                raise WorkflowRepositoryError("barrier step required flag is inconsistent")
            plan_dependencies = plan_node.get("dependsOn")
            plan_required_artifacts = plan_node.get("requiredArtifacts")
            if (
                not isinstance(plan_dependencies, Sequence)
                or isinstance(plan_dependencies, (str, bytes, bytearray))
                or not isinstance(plan_required_artifacts, Sequence)
                or isinstance(plan_required_artifacts, (str, bytes, bytearray))
            ):
                raise WorkflowRepositoryError("barrier step selector registry is invalid")
            expected_dependency_digest = "sha256:" + hashlib.sha256(canonicalize(list(plan_dependencies))).hexdigest()
            expected_required_artifacts_digest = (
                "sha256:" + hashlib.sha256(canonicalize(list(plan_required_artifacts))).hexdigest()
            )
            if (
                record["dependency_hash"] != expected_dependency_digest
                or record["required_artifacts_digest"] != expected_required_artifacts_digest
            ):
                # Step 的 selector 必须由冻结 node role 派生，任意摘要不能伪造 gate 输入。
                raise WorkflowRepositoryError("barrier step selector digest is inconsistent")
            required = declared_required
            active_attempt = (
                self._connection.execute(
                    "SELECT 1 FROM attempts WHERE step_id=? AND phase<>'TERMINATED' AND drain_state<>'DRAINED' LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            unsettled_started_receipt = (
                self._connection.execute(
                    "SELECT 1 FROM action_receipt_events AS r "
                    "JOIN actions AS a ON a.action_id=r.action_id "
                    "JOIN attempts AS at ON at.attempt_id=r.attempt_id "
                    "WHERE at.step_id=? AND r.phase='STARTED' "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM action_receipt_events AS later "
                    "WHERE later.action_id=r.action_id AND later.receipt_seq>r.receipt_seq "
                    "AND later.phase IN ('COMPLETED','ABSENT_CONFIRMED','FAILED_RECONCILED')) LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            artifact_cursor = self._connection.execute(
                "SELECT ar.* FROM artifacts AS ar JOIN attempts AS at "
                "ON at.attempt_id=ar.producer_attempt_id "
                "WHERE at.step_id=? ORDER BY ar.artifact_id",
                (record["step_id"],),
            )
            artifact_rows = artifact_cursor.fetchall()
            artifact_columns = tuple(item[0] for item in artifact_cursor.description)
            committed_artifact_ids: set[str] = set()
            for artifact_values in artifact_rows:
                artifact_record = dict(zip(artifact_columns, artifact_values, strict=True))
                try:
                    artifact = Artifact.from_record(artifact_record)
                except FactoryError as exc:
                    raise WorkflowRepositoryError("barrier artifact identity is invalid") from exc
                if artifact.is_gate_eligible:
                    committed_artifact_ids.add(artifact.artifact_id)
            raw_required_artifact_ids = plan_node.get("requiredArtifacts", ())
            if not isinstance(raw_required_artifact_ids, Sequence) or isinstance(
                raw_required_artifact_ids, (str, bytes)
            ):
                raise WorkflowRepositoryError("plan artifact requirement is invalid")
            required_artifact_ids: tuple[object, ...] = tuple(raw_required_artifact_ids)
            if any(type(item) is not str or not item for item in required_artifact_ids):
                raise WorkflowRepositoryError("plan artifact requirement is invalid")
            required_artifacts_committed = committed_artifact_ids == set(required_artifact_ids)
            attempt_unknown = (
                self._connection.execute(
                    "SELECT 1 FROM attempts WHERE step_id=? AND outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                    (record["step_id"],),
                ).fetchone()
                is not None
            )
            if attempt_unknown:
                # Attempt-only UNKNOWN 也必须进入 barrier 的未知闭环，不能被终止 Step 的成功投影覆盖。
                outcome = StepOutcome.UNKNOWN_REMOTE_STATE
            facts.append(
                BarrierStepFacts(
                    step_id=str(record["step_id"]),
                    required=required,
                    phase=phase,
                    outcome=outcome,
                    active_attempt=active_attempt,
                    unsettled_started_receipt=unsettled_started_receipt,
                    # 谓词结果只能来自持久化终态 Step outcome，绝不采信调用方布尔值。
                    success_predicate_passed=phase is StepPhase.TERMINAL and outcome is StepOutcome.SUCCEEDED,
                    required_artifacts_committed=required_artifacts_committed,
                    blocking_finding_open=False,
                )
            )
        if len(set(actual_node_ids)) != len(actual_node_ids):
            raise WorkflowRepositoryError("barrier step node set is duplicated")
        if not set(required_node_ids).issubset(actual_node_ids):
            raise WorkflowRepositoryError("barrier required node set is incomplete")
        blocking_cursor = self._connection.execute(
            "SELECT COUNT(*) FROM review_findings "
            "WHERE task_id=? AND plan_revision_id=? AND UPPER(severity)='BLOCKING' "
            "AND UPPER(status) NOT IN ('CLOSED','RESOLVED','SUPERSEDED','DISMISSED')",
            (task_id, barrier_revision_id),
        )
        blocking_finding_count = int(blocking_cursor.fetchone()[0])
        if blocking_finding_count:
            facts = [
                BarrierStepFacts(
                    step_id=step.step_id,
                    required=step.required,
                    phase=step.phase,
                    outcome=step.outcome,
                    active_attempt=step.active_attempt,
                    unsettled_started_receipt=step.unsettled_started_receipt,
                    success_predicate_passed=step.success_predicate_passed,
                    required_artifacts_committed=step.required_artifacts_committed,
                    blocking_finding_open=True,
                )
                for step in facts
            ]
        active_attempt_count = int(
            self._connection.execute(
                "SELECT COUNT(*) FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
                "WHERE s.run_id=? AND a.phase<>'TERMINATED' AND a.drain_state<>'DRAINED'",
                (run_id,),
            ).fetchone()[0]
        )
        unknown_remote_state = (
            self._connection.execute(
                "SELECT 1 FROM steps AS s WHERE s.run_id=? AND s.outcome='UNKNOWN_REMOTE_STATE' "
                "UNION ALL SELECT 1 FROM attempts AS a JOIN steps AS s ON s.step_id=a.step_id "
                "WHERE s.run_id=? AND a.outcome='UNKNOWN_REMOTE_STATE' LIMIT 1",
                (run_id, run_id),
            ).fetchone()
            is not None
        )
        required_artifacts_committed = all(not step.required or step.required_artifacts_committed for step in facts)
        return BarrierAuthorityFacts(
            steps=tuple(facts),
            active_attempt_count=active_attempt_count,
            unknown_remote_state=unknown_remote_state,
            required_artifacts_committed=required_artifacts_committed,
            blocking_finding_count=blocking_finding_count,
            required_node_ids=required_node_ids,
            required_node_set_digest=required_node_set_digest,
        )

    def update_run_control(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        desired_state: RunDesiredState,
        control_command_seq: int,
    ) -> Run:
        """同一次 Run CAS 更新 desired 与严格单调 control sequence。"""
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "desired_state": RunDesiredState(desired_state).value,
                "control_command_seq": control_command_seq,
            },
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def update_attempt_drain(
        self,
        *,
        attempt_id: str,
        expected_state_version: int,
        command_id: str,
    ) -> Attempt:
        """阻断命令原子锁定 Attempt 并置 DRAINING；RESUME 不调用本方法。"""
        row = self._task1._cas(
            table="attempts",
            identity_column="attempt_id",
            identity=attempt_id,
            expected_state_version=expected_state_version,
            changes={"interrupt_command_id": command_id, "drain_state": DrainState.DRAINING.value},
            aggregate_type="ATTEMPT",
        )
        return Attempt.from_record({key: row[key] for key in Attempt.__dataclass_fields__})

    def pass_phase_barrier(
        self,
        *,
        barrier_id: str,
        expected_state_version: int,
        gate_digest: str,
    ) -> PhaseBarrier:
        """用单次 CAS 固化 settled/passed 与其脱敏 gate 摘要。"""
        row = self._task1._cas(
            table="phase_barriers",
            identity_column="barrier_id",
            identity=barrier_id,
            expected_state_version=expected_state_version,
            changes={"settled": 1, "passed": 1, "gate_digest": gate_digest},
            aggregate_type="PHASE_BARRIER",
        )
        return self._hydrate_barrier(row)

    def block_run_for_action_required(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        reason_code: str,
    ) -> Run:
        """UNKNOWN 超时在同一 owner transaction 内落成 BLOCKED/ACTION_REQUIRED。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        # 通过统一转换图校验 BLOCKED 边，CAS 失败时由 coordinator 整体回滚。
        require_observed_transition(current.observed_state, RunObservedState.BLOCKED)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "observed_state": RunObservedState.BLOCKED.value,
                "requires_user_action": 1,
                "block_reason_code": reason_code,
            },
            aggregate_type="RUN",
        )
        record = {key: row[key] for key in Run.__dataclass_fields__}
        record["requires_user_action"] = bool(record["requires_user_action"])
        return Run.from_record(record)

    def advance_run_phase(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate_phase: RunPhase,
        next_barrier_id: str | None,
    ) -> Run:
        """校验 phase 边后原子推进 Run 当前 barrier 投影。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        if RunPhase(candidate_phase) is current.phase:
            current_barrier = self.get_phase_barrier(current.active_barrier_id) if current.active_barrier_id else None
            if current_barrier is None:
                raise WorkflowRepositoryError("same-phase progress has no current barrier")
            if next_barrier_id is None:
                future_row = self._connection.execute(
                    "SELECT 1 FROM phase_barriers WHERE run_id=? AND plan_revision_id=? AND barrier_ordinal>? LIMIT 1",
                    (run_id, current_barrier.plan_revision_id, current_barrier.barrier_ordinal),
                ).fetchone()
                if future_row is not None:
                    raise WorkflowRepositoryError("same-phase successor is required")
            else:
                next_barrier = self.get_phase_barrier(next_barrier_id)
                if (
                    next_barrier is None
                    or next_barrier.run_id != run_id
                    or next_barrier.plan_revision_id != current_barrier.plan_revision_id
                    or next_barrier.business_phase != current.phase.value
                    or next_barrier.barrier_ordinal <= current_barrier.barrier_ordinal
                ):
                    raise WorkflowRepositoryError("same-phase successor is invalid")
        else:
            require_phase_transition(current.phase, candidate_phase)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={
                "phase": RunPhase(candidate_phase).value,
                "active_barrier_id": next_barrier_id,
            },
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def advance_task_milestone(
        self,
        *,
        task_id: str,
        expected_state_version: int,
        candidate_stage: AchievedStage,
        lifecycle: TaskLifecycle,
        outcome_version: int,
    ) -> Task:
        """校验里程碑与可选终局后，在同一 Task CAS 增加 outcomeVersion。"""
        current = self.get_task(task_id)
        if current is None:
            raise WorkflowRepositoryError("task does not exist")
        require_achieved_stage_transition(current.achieved_stage, candidate_stage)
        target_lifecycle = TaskLifecycle(lifecycle)
        if target_lifecycle is not current.lifecycle:
            require_task_lifecycle_transition(current.lifecycle, target_lifecycle)
        row = self._task1._cas(
            table="tasks",
            identity_column="task_id",
            identity=task_id,
            expected_state_version=expected_state_version,
            changes={
                "achieved_stage": AchievedStage(candidate_stage).value,
                "lifecycle": target_lifecycle.value,
                "outcome_version": outcome_version,
            },
            aggregate_type="TASK",
        )
        return Task.from_record({key: row[key] for key in Task.__dataclass_fields__})

    def transition_observed(
        self,
        *,
        run_id: str,
        expected_state_version: int,
        candidate: RunObservedState,
    ) -> Run:
        """在发送 SQL 前校验 §7.3，再用 Task 1 五路 CAS 登记同事务事件。"""
        current = self.get_run(run_id)
        if current is None:
            raise WorkflowRepositoryError("run does not exist")
        require_observed_transition(current.observed_state, candidate)
        row = self._task1._cas(
            table="runs",
            identity_column="run_id",
            identity=run_id,
            expected_state_version=expected_state_version,
            changes={"observed_state": RunObservedState(candidate).value},
            aggregate_type="RUN",
        )
        return Run.from_record({key: row[key] for key in Run.__dataclass_fields__})

    def append_superseding_attempt(self, previous: Attempt, candidate: Attempt) -> None:
        """只用权威 Run/历史 Attempt 构造 QUEUED 后继，禁止调用方伪造启动事实。"""
        if not isinstance(previous, Attempt) or not isinstance(candidate, Attempt):
            raise WorkflowRepositoryError("attempt dispatch selector is invalid")
        stored_previous = self.get_attempt(previous.attempt_id)
        if stored_previous is None or stored_previous != previous:
            raise WorkflowRepositoryError("previous attempt is not the authoritative selector")
        run_id_row = self._connection.execute(
            "SELECT s.run_id FROM steps AS s WHERE s.step_id=?",
            (previous.step_id,),
        ).fetchone()
        if run_id_row is None:
            raise WorkflowRepositoryError("attempt step lineage is missing")
        run = self.get_run(str(run_id_row[0]))
        if run is None:
            raise WorkflowRepositoryError("attempt run lineage is missing")
        try:
            require_new_attempt_dispatch(
                observed_state=run.observed_state,
                new_attempt_id=candidate.attempt_id,
                previous_attempt_id=previous.attempt_id,
            )
        except FactoryError as exc:
            raise WorkflowRepositoryError("new attempt dispatch state is invalid") from exc
        if run.desired_state is not RunDesiredState.RUNNING:
            raise WorkflowRepositoryError("new attempt dispatch requires desired RUNNING")
        if previous.phase is not AttemptPhase.TERMINATED or previous.drain_state is not DrainState.DRAINED:
            raise WorkflowRepositoryError("previous attempt is not fully drained")
        if candidate.supersedes_attempt_id != previous.attempt_id:
            raise WorkflowRepositoryError("candidate must supersede the authoritative previous attempt")
        if candidate.accepted_control_command_seq != run.control_command_seq:
            raise WorkflowRepositoryError("candidate control sequence is not the authoritative Run sequence")
        if candidate.accepted_control_command_seq <= previous.accepted_control_command_seq:
            raise WorkflowRepositoryError("candidate control sequence is not fresh")
        if candidate.control_epoch <= previous.control_epoch:
            raise WorkflowRepositoryError("candidate control epoch is not fresh")
        try:
            expected = previous.supersede(
                new_attempt_id=candidate.attempt_id,
                fencing_token=candidate.fencing_token,
                control_epoch=candidate.control_epoch,
                accepted_control_command_seq=candidate.accepted_control_command_seq,
            )
        except FactoryError as exc:
            raise WorkflowRepositoryError("candidate dispatch facts are invalid") from exc
        if (
            candidate != expected
            or candidate.phase is not AttemptPhase.CREATED
            or candidate.outcome is not AttemptOutcome.NONE
        ):
            raise WorkflowRepositoryError("candidate must be a fresh CREATED Attempt")
        prior_authorization = self._connection.execute(
            "SELECT MAX(fencing_token), MAX(control_epoch), MAX(accepted_control_command_seq) "
            "FROM execution_authorizations WHERE run_id=? AND step_id=?",
            (run.run_id, candidate.step_id),
        ).fetchone()
        if prior_authorization is not None:
            max_fencing, max_control_epoch, max_control_seq = prior_authorization
            if max_fencing is not None and candidate.fencing_token <= int(max_fencing):
                raise WorkflowRepositoryError("candidate fencing token is not fresh relative to authorization")
            if max_control_epoch is not None and candidate.control_epoch <= int(max_control_epoch):
                raise WorkflowRepositoryError("candidate control epoch is not fresh relative to authorization")
            if max_control_seq is not None and candidate.accepted_control_command_seq <= int(max_control_seq):
                raise WorkflowRepositoryError("candidate control sequence is not fresh relative to authorization")
        self._task1.insert_attempt(
            {
                field: getattr(candidate, field).value
                if hasattr(getattr(candidate, field), "value")
                else getattr(candidate, field)
                for field in Attempt.__dataclass_fields__
            }
        )


__all__ = [
    "BarrierAuthorityFacts",
    "ObservedStateAuthorityFacts",
    "SqliteWorkflowRepository",
    "WorkflowRepositoryError",
]
