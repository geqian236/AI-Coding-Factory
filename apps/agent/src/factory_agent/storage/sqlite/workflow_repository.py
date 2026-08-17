"""Task 2 工作流复合 CAS，复用 Task 1 UoW 的状态事件配对登记。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

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
from factory_agent.state_machine.barriers import BarrierStepFacts
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


@dataclass(frozen=True, slots=True)
class BarrierAuthorityFacts:
    """同一事务由 steps/attempts/receipts/artifacts/findings 重算的 gate 事实。"""

    steps: tuple[BarrierStepFacts, ...]
    active_attempt_count: int
    unknown_remote_state: bool
    required_artifacts_committed: bool
    blocking_finding_count: int


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
        if active_attempt is not None:
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
        return ObservedStateAuthorityFacts(
            active_attempt_count=len(active_rows),
            lease_active=lease_active,
            authorization_active=authorization_active,
            control_sequence_current=control_sequence_current,
            heartbeat_valid=heartbeat_valid,
            unknown_remote_state=unknown_remote_state,
            unsettled_started_receipt=unsettled_started_receipt,
        )

    def load_barrier_authority(
        self,
        *,
        barrier_id: str,
        run_id: str,
        task_id: str,
    ) -> BarrierAuthorityFacts:
        """从权威表重算 barrier，拒绝空步骤和调用方自报的成功摘要。"""
        rows_cursor = self._connection.execute(
            "SELECT * FROM steps WHERE run_id=? AND barrier_id=? ORDER BY step_id",
            (run_id, barrier_id),
        )
        rows = rows_cursor.fetchall()
        if not rows:
            raise WorkflowRepositoryError("barrier has no authoritative steps")
        columns = tuple(item[0] for item in rows_cursor.description)
        facts: list[BarrierStepFacts] = []
        for values in rows:
            record = dict(zip(columns, values, strict=True))
            try:
                phase = StepPhase(record["phase"])
                outcome = StepOutcome(record["outcome"])
                required_value = record["required"]
                if type(required_value) is not int or required_value not in (0, 1):
                    raise ValueError("step required flag is invalid")
                required = required_value == 1
            except (TypeError, ValueError, KeyError) as exc:
                raise WorkflowRepositoryError("barrier step selector is invalid") from exc
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
            required_artifacts_committed = (
                self._connection.execute(
                    "SELECT 1 FROM artifacts AS ar JOIN attempts AS at "
                    "ON at.attempt_id=ar.producer_attempt_id "
                    "WHERE at.step_id=? AND ar.commit_state='COMMITTED' AND ar.digest=? LIMIT 1",
                    (record["step_id"], record["required_artifacts_digest"]),
                ).fetchone()
                is not None
            )
            facts.append(
                BarrierStepFacts(
                    step_id=str(record["step_id"]),
                    required=required,
                    phase=phase,
                    outcome=outcome,
                    active_attempt=active_attempt,
                    unsettled_started_receipt=unsettled_started_receipt,
                    # Predicate result is the persisted terminal Step outcome; no caller boolean is trusted.
                    success_predicate_passed=phase is StepPhase.TERMINAL and outcome is StepOutcome.SUCCEEDED,
                    required_artifacts_committed=required_artifacts_committed,
                    blocking_finding_open=False,
                )
            )
        blocking_cursor = self._connection.execute(
            "SELECT COUNT(*) FROM review_findings "
            "WHERE task_id=? AND UPPER(severity)='BLOCKING' "
            "AND UPPER(status) NOT IN ('CLOSED','RESOLVED','SUPERSEDED','DISMISSED')",
            (task_id,),
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
