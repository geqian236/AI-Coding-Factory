"""Task 2 工作流复合 CAS，复用 Task 1 UoW 的状态事件配对登记。"""

from __future__ import annotations

from factory_agent.domain.workflow import (
    AchievedStage,
    Attempt,
    DrainState,
    PhaseBarrier,
    Run,
    RunDesiredState,
    RunObservedState,
    RunPhase,
    Task,
    TaskLifecycle,
)
from factory_agent.errors import FactoryError
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
        """只在 Run 已收敛 QUEUED 时插入 fresh Attempt，禁止复用旧 identity。"""
        run_id_row = self._connection.execute(
            "SELECT s.run_id FROM steps AS s WHERE s.step_id=?",
            (previous.step_id,),
        ).fetchone()
        if run_id_row is None:
            raise WorkflowRepositoryError("attempt step lineage is missing")
        run = self.get_run(str(run_id_row[0]))
        if run is None:
            raise WorkflowRepositoryError("attempt run lineage is missing")
        require_new_attempt_dispatch(
            observed_state=run.observed_state,
            new_attempt_id=candidate.attempt_id,
            previous_attempt_id=previous.attempt_id,
        )
        self._task1.insert_attempt(
            {
                field: getattr(candidate, field).value
                if hasattr(getattr(candidate, field), "value")
                else getattr(candidate, field)
                for field in Attempt.__dataclass_fields__
            }
        )


__all__ = ["SqliteWorkflowRepository", "WorkflowRepositoryError"]
