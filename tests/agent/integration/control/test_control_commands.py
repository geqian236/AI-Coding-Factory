"""控制命令 requestId、CAS、追加 receipt 与 RESUME 语义集成测试。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from factory_agent.application.control_service import (
    ControlCommandRequest,
    ControlRequestConflictError,
    ControlService,
)
from factory_agent.application.transition_service import (
    BarrierMilestoneRequest,
    TransitionService,
)
from factory_agent.domain.control import ControlCommandReceiptPhase, ControlCommandType
from factory_agent.domain.workflow import AchievedStage, RunPhase, StepOutcome, StepPhase
from factory_agent.state_machine.barriers import BarrierStepFacts
from factory_agent.state_machine.milestones import MilestoneEvidence
from factory_agent.state_machine.transitions import StateTransitionError
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork, StaleStateVersionError

SHA_A = "sha256:" + "a" * 64
MIGRATION_ROOT = (
    Path(__file__).resolve().parents[4]
    / "apps"
    / "agent"
    / "src"
    / "factory_agent"
    / "storage"
    / "sqlite"
    / "migrations"
)


class _SqliteCoordinator:
    """在同一真实 SQLite 连接上复现 coordinator 的事务协议。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    async def execute[ResultT](
        self,
        *,
        operation: str,
        context: Mapping[str, object],
        command: Callable[[Any], ResultT],
    ) -> ResultT:
        assert operation
        assert "request_id" in context
        unit_of_work = SqliteUnitOfWork(self.connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            result = command(unit_of_work)
            unit_of_work.precommit()
            unit_of_work.commit()
            return result
        except BaseException:
            unit_of_work.rollback()
            raise


def _ids(prefix: str) -> Callable[[], str]:
    """返回稳定递增 identity 工厂，避免测试依赖随机数。"""
    values: Iterator[int] = iter(range(1, 100))
    return lambda: f"{prefix}-{next(values)}"


def _insert(connection: sqlite3.Connection, table: str, values: Mapping[str, object]) -> None:
    """测试 setup 只使用冻结表名，所有业务值仍通过参数绑定写入。"""
    columns = tuple(values)
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',  # noqa: S608
        tuple(values[column] for column in columns),
    )


def _connection(*, desired_state: str = "RUNNING", observed_state: str = "QUEUED") -> sqlite3.Connection:
    """加载真实四个 migration，并插入控制测试所需的最小 lineage。"""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=OFF")
    for path in sorted(MIGRATION_ROOT.glob("*.sql")):
        connection.executescript(path.read_text(encoding="utf-8"))
    _insert(connection, "projects", {"project_id": "project-control-1"})
    _insert(
        connection,
        "tasks",
        {
            "task_id": "task-control-1",
            "project_id": "project-control-1",
            "lifecycle": "ACTIVE",
            "target_stage": "CODEX_APPROVED",
            "achieved_stage": "NONE",
            "active_plan_revision_id": None,
            "active_run_id": "run-control-1",
            "state_version": 0,
            "outcome_version": 0,
            "intake_schema_id": "task-intake.v1",
            "intake_schema_version": 1,
            "canonical_intake": b"{}",
            "intake_digest": SHA_A,
        },
    )
    _insert(
        connection,
        "runs",
        {
            "run_id": "run-control-1",
            "task_id": "task-control-1",
            "desired_state": desired_state,
            "observed_state": observed_state,
            "phase": "PLANNING",
            "active_barrier_id": None,
            "dag_version": 1,
            "run_cursor": None,
            "durable_cursor": None,
            "control_command_seq": 0,
            "repair_loop_used": 0,
            "auto_replan_used": 0,
            "requires_user_action": 0,
            "block_reason_code": None,
            "budget_accumulated_ms": 0,
            "budget_clock_state": None,
            "budget_clock_boot_id": None,
            "budget_clock_monotonic_ns": None,
            "budget_clock_wall_time": None,
            "budget_suspension_reason": None,
            "executor_id": None,
            "host_id": None,
            "runtime": None,
            "protocol_version": "control-plane.v1",
            "recovery_target_phase": None,
            "state_version": 0,
        },
    )
    return connection


def _service(connection: sqlite3.Connection) -> ControlService:
    """构造注入稳定时钟和 identity 的真实服务。"""
    return ControlService(
        coordinator=_SqliteCoordinator(connection),
        now=lambda: datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        command_id_factory=_ids("command"),
        receipt_id_factory=_ids("receipt"),
        state_event_id_factory=_ids("state-event"),
    )


def _request(
    *,
    request_id: str = "request-pause-1",
    command_type: ControlCommandType = ControlCommandType.SOFT_PAUSE,
    expected_state_version: int = 0,
) -> ControlCommandRequest:
    """构造只含摘要、不含用户原始 reason 的命令边界。"""
    return ControlCommandRequest(
        request_id=request_id,
        run_id="run-control-1",
        command_type=command_type,
        actor_id="actor-control-1",
        expected_state_version=expected_state_version,
        reason_digest=SHA_A,
    )


@pytest.mark.asyncio
async def test_request_id_is_idempotent_and_receipts_are_append_only() -> None:
    """相同 requestId 只接受一次，后续完成事实只能追加 receipt event。"""
    connection = _connection()
    try:
        service = _service(connection)
        first = await service.submit(_request())
        replay = await service.submit(_request())
        completed = await service.append_receipt(
            command_id=first.command.command_id,
            phase=ControlCommandReceiptPhase.COMPLETED,
            attempt_id=None,
            evidence_digest=SHA_A,
        )

        assert replay.idempotent_replay is True
        assert replay.command == first.command
        assert completed.receipt_seq == 1
        run = connection.execute(
            "SELECT desired_state,observed_state,control_command_seq,state_version FROM runs"
        ).fetchone()
        assert run == ("PAUSED", "QUEUED", 1, 1)
        assert connection.execute("SELECT count(*) FROM control_commands").fetchone() == (1,)
        assert connection.execute(
            "SELECT phase FROM control_command_receipt_events ORDER BY receipt_seq"
        ).fetchall() == [("ACKNOWLEDGED",), ("COMPLETED",)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE control_command_receipt_events SET phase='FAILED' WHERE receipt_seq=0")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_resume_changes_only_desired_state_and_never_forges_running() -> None:
    """RESUME 接受事务不得直接把 observed_state 改成 RUNNING。"""
    connection = _connection(desired_state="PAUSED", observed_state="PAUSED")
    try:
        service = _service(connection)
        accepted = await service.submit(_request(request_id="request-resume-1", command_type=ControlCommandType.RESUME))
        assert accepted.command.command_type is ControlCommandType.RESUME
        assert connection.execute(
            "SELECT desired_state,observed_state,control_command_seq,state_version FROM runs"
        ).fetchone() == ("RUNNING", "PAUSED", 1, 1)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_immediate_stop_on_fully_paused_run_appends_completed_noop_receipt() -> None:
    """已满足 PAUSED 谓词且无活动 Attempt 时，立即停止直接追加完成事实。"""
    connection = _connection(desired_state="PAUSED", observed_state="PAUSED")
    try:
        service = _service(connection)
        accepted = await service.submit(
            _request(request_id="request-stop-paused", command_type=ControlCommandType.IMMEDIATE_STOP)
        )
        assert accepted.command.acknowledged_attempt_id is None
        assert connection.execute("SELECT desired_state,observed_state,control_command_seq FROM runs").fetchone() == (
            "PAUSED",
            "PAUSED",
            1,
        )
        assert connection.execute(
            "SELECT phase FROM control_command_receipt_events ORDER BY receipt_seq"
        ).fetchall() == [("ACKNOWLEDGED",), ("COMPLETED",)]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_immediate_stop_does_not_treat_reconciling_run_as_paused_noop() -> None:
    """只有 observed 也为 PAUSED 才能完成 no-op，RECONCILING 必须继续 fail closed。"""
    connection = _connection(desired_state="PAUSED", observed_state="RECONCILING")
    try:
        service = _service(connection)
        with pytest.raises(StateTransitionError):
            await service.submit(
                _request(request_id="request-stop-reconciling", command_type=ControlCommandType.IMMEDIATE_STOP)
            )
        assert connection.execute("SELECT count(*) FROM control_commands").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_reusing_request_id_with_different_semantics_is_rejected() -> None:
    """requestId 只能重放同一命令，不能被另一类型或版本占用。"""
    connection = _connection()
    try:
        service = _service(connection)
        await service.submit(_request())
        with pytest.raises(ControlRequestConflictError) as caught:
            await service.submit(_request(command_type=ControlCommandType.CANCEL, expected_state_version=1))
        assert caught.value.error_code == "CONTROL_REQUEST_CONFLICT"
        assert connection.execute("SELECT count(*) FROM control_commands").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_competing_expected_state_version_allows_exactly_one_command() -> None:
    """两个旧版本命令竞争时只有一方 CAS 成功，失败命令与 receipt 全部回滚。"""
    connection = _connection()
    try:
        service = _service(connection)
        await service.submit(_request(request_id="request-race-winner"))
        with pytest.raises(StaleStateVersionError):
            await service.submit(
                _request(
                    request_id="request-race-loser",
                    command_type=ControlCommandType.CANCEL,
                    expected_state_version=0,
                )
            )
        assert connection.execute("SELECT count(*) FROM control_commands").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM control_command_receipt_events").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_phase_and_milestone_commit_atomically() -> None:
    """barrier、Run phase 与 Task 里程碑必须和三条 state event 同事务落库。"""
    connection = _connection()
    try:
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-planning",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": SHA_A,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": "2026-08-14T08:01:00+00:00",
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-design",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "DESIGN_REVIEWING",
                "barrier_ordinal": 1,
                "required_node_set_digest": SHA_A,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "design-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-planning'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-barrier"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-barrier-1",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-planning",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id="barrier-design",
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
            steps=(
                BarrierStepFacts(
                    step_id="step-planning",
                    required=True,
                    phase=StepPhase.TERMINAL,
                    outcome=StepOutcome.SUCCEEDED,
                    active_attempt=False,
                    unsettled_started_receipt=False,
                    success_predicate_passed=True,
                    required_artifacts_committed=True,
                    blocking_finding_open=False,
                ),
            ),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )

        result = await service.commit_passed_barrier(request)

        assert result.barrier.passed is True
        assert connection.execute(
            "SELECT settled,passed,gate_digest,state_version FROM phase_barriers WHERE barrier_id='barrier-planning'"
        ).fetchone() == (1, 1, SHA_A, 1)
        assert connection.execute(
            "SELECT phase,active_barrier_id,state_version FROM runs WHERE run_id='run-control-1'"
        ).fetchone() == ("DESIGN_REVIEWING", "barrier-design", 1)
        assert connection.execute(
            "SELECT achieved_stage,lifecycle,outcome_version,state_version FROM tasks WHERE task_id='task-control-1'"
        ).fetchone() == ("DESIGN_APPROVED", "ACTIVE", 1, 1)
        assert connection.execute(
            "SELECT aggregate_type FROM authoritative_state_events ORDER BY aggregate_type"
        ).fetchall() == [("PHASE_BARRIER",), ("RUN",), ("TASK",)]

        stale = replace(
            request,
            request_id="request-barrier-stale",
            barrier_id="barrier-design",
            expected_task_state_version=1,
            expected_run_state_version=0,
            next_phase=RunPhase.PREPARING_WORKSPACE,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.CODEX_APPROVED,
        )
        with pytest.raises(StaleStateVersionError):
            await service.commit_passed_barrier(stale)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id='barrier-design'"
        ).fetchone() == (0, 0, 0)
    finally:
        connection.close()
