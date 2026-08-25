"""Phase 1 Task 3 预算时钟 RED/GREEN 集成测试。"""

from __future__ import annotations

import importlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from factory_agent.domain.events import payload_digest
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork

MIGRATIONS_DIR = Path(__file__).resolve().parents[4] / "apps/agent/src/factory_agent/storage/sqlite/migrations"


class _InjectedFailure(RuntimeError):
    """测试 UoW 故障点必须走 coordinator rollback。"""


def _budget_module() -> ModuleType:
    """延迟导入待实现服务模块，让缺失实现保持为单项 RED。"""
    return importlib.import_module("factory_agent.application.budget_service")


def _new_connection() -> sqlite3.Connection:
    """使用内存 SQLite 避免测试在 C 盘留下数据库或临时文件。"""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    for name in ("0001_workflow.sql", "0002_event_store.sql", "0003_auth_resources.sql", "0004_control_budget.sql"):
        connection.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
    return connection


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """测试 fixture 使用固定表名，业务值始终参数绑定。"""
    columns = tuple(values)
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    connection.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',  # noqa: S608 - table 来自本测试固定字面量
        tuple(values[column] for column in columns),
    )


def _insert_run(connection: sqlite3.Connection, **overrides: object) -> None:
    """构造最小真实 Run 投影，覆盖预算服务的 CAS/事件 FK。"""
    _insert(connection, "projects", {"project_id": "project-budget-1"})
    _insert(
        connection,
        "tasks",
        {
            "task_id": "task-budget-1",
            "project_id": "project-budget-1",
            "lifecycle": "ACTIVE",
            "target_stage": "CODEX_APPROVED",
            "achieved_stage": "NONE",
            "active_plan_revision_id": None,
            # Run 的 FK 必须在两条记录完成插入后再回填，避免 fixture 先写悬空引用。
            "active_run_id": None,
            "state_version": 0,
            "outcome_version": 0,
            "intake_schema_id": "task-intake.v1",
            "intake_schema_version": 1,
            "canonical_intake": b"{}",
            "intake_digest": "sha256:" + "a" * 64,
        },
    )
    record: dict[str, object] = {
        "run_id": "run-budget-1",
        "task_id": "task-budget-1",
        "desired_state": "RUNNING",
        "observed_state": "RUNNING",
        "phase": "IMPLEMENTING",
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
        "executor_id": "executor-budget-1",
        "host_id": "host-budget-1",
        "runtime": "local-windows",
        "protocol_version": "control-plane.v1",
        "recovery_target_phase": None,
        "state_version": 0,
    }
    record.update(overrides)
    _insert(connection, "runs", record)
    connection.execute("UPDATE tasks SET active_run_id=? WHERE task_id=?", ("run-budget-1", "task-budget-1"))


class _Clock:
    """可重放的 boot/monotonic/wall 样本源。"""

    def __init__(self, *samples: dict[str, object]) -> None:
        self.samples = list(samples)

    def sample(self) -> dict[str, object]:
        assert self.samples
        return self.samples.pop(0)


class _Coordinator:
    """在真实 UoW 连接上复现唯一 writer 的事务协议。"""

    def __init__(self, connection: sqlite3.Connection, *, failing_points: set[str] | None = None) -> None:
        self.connection = connection
        self.failing_points = failing_points or set()

    async def execute(
        self,
        *,
        operation: str,
        context: dict[str, object],
        command: Callable[[SqliteUnitOfWork], object],
    ) -> object:
        assert operation
        assert context["run_id"] == "run-budget-1"

        def probe(point: str) -> None:
            if point in self.failing_points:
                raise _InjectedFailure(point)

        unit_of_work = SqliteUnitOfWork(self.connection, failure_probe=probe)
        unit_of_work.begin_immediate()
        try:
            result = command(unit_of_work)
            unit_of_work.precommit()
            unit_of_work.commit()
            return result
        except BaseException:
            unit_of_work.rollback()
            raise


def _sample(boot_id: str, monotonic_ns: int, wall_time: str) -> dict[str, object]:
    """构造 Clock port 的稳定 primitive。"""
    return {"boot_id": boot_id, "monotonic_ns": monotonic_ns, "wall_time": wall_time}


@pytest.mark.asyncio
async def test_same_boot_sleep_is_billed_from_monotonic_clock() -> None:
    """同 boot 睡眠包含在 monotonic 增量内，不能因 wall 时间近似而赠送预算。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 61_000_000_000, "2026-08-24T00:01:01Z"),
            ),
            state_event_id_factory=iter(("state-budget-1", "state-budget-2")).__next__,
        )

        first = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        second = await service.observe(
            run_id="run-budget-1",
            budget_limit_ms=120_000,
            request_id="request-budget-1",
            trace_id="trace-budget-1",
        )

        assert first.budget_accumulated_ms == 0
        assert second.budget_accumulated_ms == 60_000
        assert first.request_id.startswith("budget-request-")
        assert first.trace_id.startswith("budget-trace-")
        assert second.request_id == "request-budget-1"
        assert second.trace_id == "trace-budget-1"
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (2,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_wall_rollback_blocks_without_gifting_budget() -> None:
    """同 boot wall 回拨超过阈值必须 fail closed 且保持累计预算不变。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-a", 2_000_000_000, "2026-08-24T00:00:00Z"),
            ),
            state_event_id_factory=iter(("state-budget-3", "state-budget-4")).__next__,
            max_clock_rollback_ms=1_000,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert result.clock_anomaly is True
        assert result.budget_accumulated_ms == 0
        assert result.observed_state == "BLOCKED"
        assert connection.execute("SELECT budget_accumulated_ms,block_reason_code FROM runs").fetchone() == (
            0,
            "CLOCK_ANOMALY",
        )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_cross_boot_shutdown_is_conservatively_billed() -> None:
    """跨 boot 没有连续 monotonic 证据时按非负 wall anchor 保守计费。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-b", 7_000_000_000, "2026-08-24T01:00:00Z"),
            ),
            state_event_id_factory=iter(("state-budget-5", "state-budget-6")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert result.budget_accumulated_ms == 3_600_000
        assert result.observed_state == "BLOCKED"
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_user_pause_stops_clock_but_requires_action_is_only_other_pause() -> None:
    """只有 USER_PAUSED/REQUIRES_USER_ACTION 能冻结计时，重启不清零累计值。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-b", 1_000_000_000, "2026-08-24T00:10:00Z"),
            ),
            state_event_id_factory=iter(("state-budget-7", "state-budget-8", "state-budget-9")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='PAUSED' WHERE run_id='run-budget-1'")
        paused = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='RUNNING' WHERE run_id='run-budget-1'")
        resumed = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert paused.budget_accumulated_ms == 10_000
        assert paused.suspension_reason == "USER_PAUSED"
        assert resumed.budget_accumulated_ms == 10_000
        assert resumed.clock_state == "RUNNING"
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "clock_event",
    [
        {"run_id": "other-run", "state_event_id": "state-lineage-1"},
        {"run_id": "run-budget-1", "state_event_id": "other-state-event"},
    ],
)
async def test_budget_persist_rejects_clock_event_lineage_mismatch(clock_event: dict[str, str]) -> None:
    """clock event 必须在 CAS 前绑定当前 Run 与同一 state event，错配不得落库。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(BudgetRepositoryError, match="clock event lineage"):
                BudgetRepository(unit_of_work).persist_observation(
                    run_id="run-budget-1",
                    expected_state_version=0,
                    changes={
                        "observed_state": "RUNNING",
                        "requires_user_action": 0,
                        "block_reason_code": None,
                        "budget_accumulated_ms": 0,
                        "budget_clock_state": "RUNNING",
                        "budget_clock_boot_id": "boot-a",
                        "budget_clock_monotonic_ns": 1_000_000_000,
                        "budget_clock_wall_time": "2026-08-24T00:00:00Z",
                        "budget_suspension_reason": None,
                    },
                    state_event_id="state-lineage-1",
                    state_payload={
                        "kind": "budget.clock.observed",
                        "runId": "run-budget-1",
                        "budgetAccumulatedMs": 0,
                        "clockState": "RUNNING",
                        "suspensionReason": None,
                        "elapsedMs": 0,
                        "clockAnomaly": False,
                        "budgetExhausted": False,
                    },
                    clock_event=clock_event,
                )
        finally:
            unit_of_work.rollback()
        assert connection.execute("SELECT state_version,budget_accumulated_ms FROM runs").fetchone() == (0, 0)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_requires_user_action_freezes_clock_across_service_restart() -> None:
    """REQUIRES_USER_ACTION 与 USER_PAUSED 一样冻结计时，重启服务不清零累计值。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-b", 1_000_000_000, "2026-08-24T01:00:00Z"),
            ),
            state_event_id_factory=iter(
                ("state-budget-action-1", "state-budget-action-2", "state-budget-action-3")
            ).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET requires_user_action=1 WHERE run_id='run-budget-1'")
        paused = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET requires_user_action=0,desired_state='RUNNING' WHERE run_id='run-budget-1'")
        resumed = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert paused.suspension_reason == "REQUIRES_USER_ACTION"
        assert resumed.budget_accumulated_ms == 10_000
        assert resumed.clock_state == "RUNNING"
        assert resumed.qualification == "PARTIAL"
        assert resumed.final_receipt is False
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_exhaustion_blocks_and_preserves_append_only_clock_chain() -> None:
    """预算耗尽停止新派发，Run 进入 BLOCKED 且时钟链不允许原位修改。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-budget-10", "state-budget-11")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)

        assert result.budget_exhausted is True
        assert result.observed_state == "BLOCKED"
        assert result.clock_state == "SUSPENDED"
        assert result.suspension_reason == "REQUIRES_USER_ACTION"
        assert result.requires_user_action is True
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (2,)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM budget_clock_events")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_same_boot_wall_forward_jump_blocks_without_gifting_budget() -> None:
    """同 boot wall 前跳与 monotonic 偏差超过冻结阈值时必须 fail closed。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 2_000_000_000, "2026-08-24T01:00:00Z"),
            ),
            state_event_id_factory=iter(("state-forward-1", "state-forward-2")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert result.clock_anomaly is True
        assert result.budget_accumulated_ms == 0
        assert result.observed_state == "BLOCKED"
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (1,)
        from factory_agent.storage.sqlite.budget_repository import BudgetRepository

        projection = BudgetRepository(
            SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        ).reconstruct_projection("run-budget-1")
        assert projection["clock_seq"] == 1
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_spoofed_clock_anomaly_exception() -> None:
    """普通事件链不能只靠可变 block reason 冒充 CLOCK_ANOMALY anchor。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-anomaly-spoof-1", "state-anomaly-spoof-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute(
            "UPDATE runs SET budget_clock_state='BLOCKED',block_reason_code='CLOCK_ANOMALY' WHERE run_id='run-budget-1'"
        )

        with pytest.raises(BudgetRepositoryError, match="budget clock state does not match"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_clock_anomaly_state_version_jump() -> None:
    """异常 state event 必须从最后 clock-linked 版本连续衔接，不能只满足单行自洽。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-anomaly-jump-1", "state-anomaly-jump-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        payload = {
            "kind": "budget.clock.observed",
            "runId": "run-budget-1",
            "budgetAccumulatedMs": 10_000,
            "clockState": "BLOCKED",
            "suspensionReason": None,
            "elapsedMs": 0,
            "clockAnomaly": True,
            "budgetExhausted": False,
        }
        state_event = {
            "schemaVersion": 1,
            "stateEventId": "state-anomaly-jump-100",
            "eventType": "state.changed",
            "durabilityClass": "authoritative_state",
            "taskId": "task-budget-1",
            "scope": "RUN",
            "aggregateType": "RUN",
            "aggregateId": "run-budget-1",
            "runId": "run-budget-1",
            "stepId": None,
            "attemptId": None,
            "previousStateVersion": 99,
            "stateVersion": 100,
            "payload": payload,
            "payloadDigest": payload_digest(payload),
        }
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_delete")
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        _insert(
            connection,
            "authoritative_state_events",
            {
                "state_event_id": state_event["stateEventId"],
                "schema_version": state_event["schemaVersion"],
                "task_id": state_event["taskId"],
                "scope": state_event["scope"],
                "aggregate_type": state_event["aggregateType"],
                "aggregate_id": state_event["aggregateId"],
                "run_id": state_event["runId"],
                "step_id": state_event["stepId"],
                "attempt_id": state_event["attemptId"],
                "previous_state_version": state_event["previousStateVersion"],
                "state_version": state_event["stateVersion"],
                "payload_digest": state_event["payloadDigest"],
                "canonical_state_event": canonicalize(state_event),
            },
        )
        connection.execute(
            "UPDATE runs SET state_version=100,budget_clock_state='BLOCKED',block_reason_code='CLOCK_ANOMALY' "
            "WHERE run_id='run-budget-1'"
        )

        with pytest.raises(BudgetRepositoryError, match="anomaly state event lineage"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("taskId", "task-other"),
        ("eventType", "state.changed.invalid"),
        ("durabilityClass", "derived"),
        ("stepId", "step-other"),
        ("attemptId", "attempt-other"),
    ],
)
async def test_budget_rebuild_rejects_normal_state_event_canonical_identity_drift(
    field: str,
    value: str,
) -> None:
    """普通 clock-linked state event 的完整 canonical identity 不能漂移。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-canonical-identity-1", "state-canonical-identity-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        row = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events WHERE state_event_id=?",
            ("state-canonical-identity-2",),
        ).fetchone()
        assert row is not None
        state_event = json.loads(bytes(row[1]).decode("utf-8"))
        state_event[field] = value
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        connection.execute(
            "UPDATE authoritative_state_events SET canonical_state_event=? WHERE state_event_id=?",
            (canonicalize(state_event), row[0]),
        )

        with pytest.raises(BudgetRepositoryError, match="canonical identity"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["task_id", "step_id", "attempt_id"])
async def test_budget_rebuild_rejects_normal_state_event_lineage_column_drift(column: str) -> None:
    """普通 state event 的持久 lineage 列必须匹配 Run 的 task/执行身份。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-lineage-column-1", "state-lineage-column-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        replacement = "task-other" if column == "task_id" else f"{column}-other"
        connection.execute(
            f"UPDATE authoritative_state_events SET {column}=? WHERE state_event_id=?",  # noqa: S608 - column is parametrized
            (replacement, "state-lineage-column-2"),
        )

        with pytest.raises(BudgetRepositoryError, match="state event lineage"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_synchronized_task_lineage_migration_without_digest_change() -> None:
    """Run 与权威 state event 同步迁移到合法 Task 时，clock 摘要仍须绑定原任务身份。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        _insert(
            connection,
            "tasks",
            {
                "task_id": "task-budget-2",
                "project_id": "project-budget-1",
                "lifecycle": "ACTIVE",
                "target_stage": "CODEX_APPROVED",
                "achieved_stage": "NONE",
                "active_plan_revision_id": None,
                "active_run_id": None,
                "state_version": 0,
                "outcome_version": 0,
                "intake_schema_id": "task-intake.v1",
                "intake_schema_version": 1,
                "canonical_intake": b"{}",
                "intake_digest": "sha256:" + "b" * 64,
            },
        )
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-task-digest-1", "state-task-digest-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        before_digests = connection.execute(
            "SELECT clock_seq,clock_digest FROM budget_clock_events ORDER BY clock_seq"
        ).fetchall()
        rows = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events "
            "WHERE run_id=? ORDER BY state_version",
            ("run-budget-1",),
        ).fetchall()
        assert len(rows) == 2
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        for state_event_id, canonical_blob in rows:
            state_event = json.loads(bytes(canonical_blob).decode("utf-8"))
            assert isinstance(state_event, dict)
            state_event["taskId"] = "task-budget-2"
            connection.execute(
                "UPDATE authoritative_state_events SET task_id=?,canonical_state_event=? WHERE state_event_id=?",
                ("task-budget-2", canonicalize(state_event), state_event_id),
            )
        connection.execute("UPDATE runs SET task_id=? WHERE run_id=?", ("task-budget-2", "run-budget-1"))
        connection.execute("UPDATE tasks SET active_run_id=NULL WHERE task_id=?", ("task-budget-1",))
        connection.execute(
            "UPDATE tasks SET active_run_id=? WHERE task_id=?",
            ("run-budget-1", "task-budget-2"),
        )

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT task_id FROM runs WHERE run_id=?", ("run-budget-1",)).fetchone() == (
            "task-budget-2",
        )
        assert connection.execute("SELECT active_run_id FROM tasks WHERE task_id=?", ("task-budget-1",)).fetchone() == (
            None,
        )
        assert connection.execute("SELECT active_run_id FROM tasks WHERE task_id=?", ("task-budget-2",)).fetchone() == (
            "run-budget-1",
        )
        assert connection.execute(
            "SELECT DISTINCT task_id FROM authoritative_state_events WHERE run_id=?", ("run-budget-1",)
        ).fetchall() == [("task-budget-2",)]
        after_digests = connection.execute(
            "SELECT clock_seq,clock_digest FROM budget_clock_events ORDER BY clock_seq"
        ).fetchall()
        assert after_digests == before_digests

        with pytest.raises(BudgetRepositoryError, match="budget clock digest is invalid"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("taskId", "task-other"),
        ("eventType", "state.changed.invalid"),
        ("durabilityClass", "derived"),
        ("stepId", "step-other"),
        ("attemptId", "attempt-other"),
    ],
)
async def test_budget_rebuild_rejects_clock_anomaly_state_event_canonical_identity_drift(
    field: str,
    value: str,
) -> None:
    """CLOCK_ANOMALY 例外只能由完整 canonical identity 的权威 state event 证明。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 2_000_000_000, "2026-08-24T00:01:00Z"),
            ),
            state_event_id_factory=iter(("state-anomaly-identity-1", "state-anomaly-identity-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        row = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events WHERE state_event_id=?",
            ("state-anomaly-identity-2",),
        ).fetchone()
        assert row is not None
        state_event = json.loads(bytes(row[1]).decode("utf-8"))
        state_event[field] = value
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        connection.execute(
            "UPDATE authoritative_state_events SET canonical_state_event=? WHERE state_event_id=?",
            (canonicalize(state_event), row[0]),
        )

        with pytest.raises(BudgetRepositoryError, match="anomaly state event"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["task_id", "step_id", "attempt_id"])
async def test_budget_rebuild_rejects_clock_anomaly_state_event_lineage_column_drift(column: str) -> None:
    """CLOCK_ANOMALY 例外不能绕过权威 state event 持久 lineage 校验。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 2_000_000_000, "2026-08-24T00:01:00Z"),
            ),
            state_event_id_factory=iter(("state-anomaly-lineage-1", "state-anomaly-lineage-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        replacement = "task-other" if column == "task_id" else f"{column}-other"
        connection.execute(
            f"UPDATE authoritative_state_events SET {column}=? WHERE state_event_id=?",  # noqa: S608 - column is parametrized
            (replacement, "state-anomaly-lineage-2"),
        )

        with pytest.raises(BudgetRepositoryError, match="anomaly state event lineage"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_paused_anchor_rollback_is_still_rejected() -> None:
    """暂停只冻结计时，不绕过 wall 回拨和 monotonic 锚点完整性校验。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-a", 2_000_000_000, "2026-08-24T00:00:00Z"),
            ),
            state_event_id_factory=iter(("state-pause-rollback-1", "state-pause-rollback-2")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='PAUSED' WHERE run_id='run-budget-1'")
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert result.clock_anomaly is True
        assert result.observed_state == "BLOCKED"
        assert result.budget_accumulated_ms == 0
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_clock_genesis_and_projection_rebuild_are_reconstructable() -> None:
    """从 genesis 逐段重算累计值、摘要链和当前锚点，暂停区间不计费。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-a", 21_000_000_000, "2026-08-24T00:00:20Z"),
                _sample("boot-a", 26_000_000_000, "2026-08-24T00:00:25Z"),
                _sample("boot-a", 31_000_000_000, "2026-08-24T00:00:30Z"),
            ),
            state_event_id_factory=iter(
                (
                    "state-rebuild-1",
                    "state-rebuild-2",
                    "state-rebuild-3",
                    "state-rebuild-4",
                    "state-rebuild-5",
                )
            ).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='PAUSED' WHERE run_id='run-budget-1'")
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='RUNNING' WHERE run_id='run-budget-1'")
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        projection = BudgetRepository(unit_of_work).reconstruct_projection("run-budget-1")
        assert projection["clock_seq"] == 5
        assert projection["budget_accumulated_ms"] == 20_000
        assert projection["clock_state"] == "RUNNING"
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (5,)
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("budget_accumulated_ms", 1),
        ("budget_clock_state", "RUNNING"),
    ],
)
async def test_budget_rebuild_rejects_tampered_empty_genesis(column: str, value: object) -> None:
    """没有 clock event 的 Run 只能保持完整零值 genesis，不能伪造计时投影。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        if column == "budget_accumulated_ms":
            connection.execute(
                "UPDATE runs SET budget_accumulated_ms=? WHERE run_id=?",
                (value, "run-budget-1"),
            )
        else:
            connection.execute(
                "UPDATE runs SET budget_clock_state=? WHERE run_id=?",
                (value, "run-budget-1"),
            )

        with pytest.raises(BudgetRepositoryError, match="budget clock genesis is missing"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_run_state_version_behind_clock_chain() -> None:
    """Run 投影版本回拨到 clock 链尾之前时，恢复必须拒绝旧投影。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-version-floor-1", "state-version-floor-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        assert connection.execute(
            "SELECT state_version FROM runs WHERE run_id=?",
            ("run-budget-1",),
        ).fetchone() == (2,)

        connection.execute(
            "UPDATE runs SET state_version=? WHERE run_id=?",
            (1, "run-budget-1"),
        )

        with pytest.raises(BudgetRepositoryError, match="state version"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_clock_rebuild_rejects_tampered_digest_chain() -> None:
    """即使测试暂时移除 SQLite 触发器，恢复器也必须拒绝篡改摘要。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-tamper-1", "state-tamper-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        connection.execute("DROP TRIGGER trg_budget_clock_events__reject_update")
        connection.execute(
            "UPDATE budget_clock_events SET clock_digest=? WHERE run_id=? AND clock_seq=2",
            ("sha256:" + "0" * 64, "run-budget-1"),
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        with pytest.raises(BudgetRepositoryError, match="digest"):
            BudgetRepository(unit_of_work).reconstruct_projection("run-budget-1")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_repeated_budget_block_observations_keep_clock_chain_reconstructable() -> None:
    """预算耗尽后的连续 BLOCKED 观察冻结计费，但继续追加暂停 anchor。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
                _sample("boot-a", 21_000_000_000, "2026-08-24T00:00:20Z"),
            ),
            state_event_id_factory=iter(("state-block-1", "state-block-2", "state-block-3")).__next__,
        )

        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        result = await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)

        assert result.observed_state == "BLOCKED"
        assert result.clock_state == "SUSPENDED"
        assert result.suspension_reason == "REQUIRES_USER_ACTION"
        assert result.budget_accumulated_ms == 10_000
        projection = BudgetRepository(
            SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        ).reconstruct_projection("run-budget-1")
        assert projection["clock_seq"] == 3
        assert projection["budget_accumulated_ms"] == 10_000
        assert projection["clock_state"] == "SUSPENDED"
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_canonical_suspension_reason_drift() -> None:
    """即使同步重算 payload digest，暂停原因漂移也不能脱离 clock event 摘要。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-payload-drift-1", "state-payload-drift-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='PAUSED' WHERE run_id='run-budget-1'")
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        row = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events WHERE state_event_id=?",
            ("state-payload-drift-2",),
        ).fetchone()
        assert row is not None
        state_event = json.loads(bytes(row[1]).decode("utf-8"))
        assert isinstance(state_event, dict)
        payload = state_event["payload"]
        assert isinstance(payload, dict)
        payload["suspensionReason"] = "REQUIRES_USER_ACTION"
        state_event["payloadDigest"] = payload_digest(payload)
        canonical_blob = canonicalize(state_event)
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        connection.execute(
            "UPDATE authoritative_state_events SET canonical_state_event=?,payload_digest=? WHERE state_event_id=?",
            (canonical_blob, state_event["payloadDigest"], row[0]),
        )

        with pytest.raises(BudgetRepositoryError, match="digest"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_budget_exhausted_true_to_false_drift() -> None:
    """预算耗尽事实改为 false 并同步 payload 摘要时，clock 摘要仍必须拒绝。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-exhausted-drift-1", "state-exhausted-drift-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)

        row = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events WHERE state_event_id=?",
            ("state-exhausted-drift-2",),
        ).fetchone()
        assert row is not None
        state_event = json.loads(bytes(row[1]).decode("utf-8"))
        assert isinstance(state_event, dict)
        payload = state_event["payload"]
        assert isinstance(payload, dict)
        assert payload["budgetExhausted"] is True
        payload["budgetExhausted"] = False
        state_event["payloadDigest"] = payload_digest(payload)
        canonical_blob = canonicalize(state_event)
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        connection.execute(
            "UPDATE authoritative_state_events SET canonical_state_event=?,payload_digest=? WHERE state_event_id=?",
            (canonical_blob, state_event["payloadDigest"], row[0]),
        )

        with pytest.raises(BudgetRepositoryError, match="digest"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_budget_exhausted_false_to_true_drift() -> None:
    """非耗尽暂停事实改为 true 并同步 payload 摘要时，clock 摘要仍必须拒绝。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-not-exhausted-drift-1", "state-not-exhausted-drift-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)
        connection.execute("UPDATE runs SET desired_state='PAUSED' WHERE run_id='run-budget-1'")
        await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        row = connection.execute(
            "SELECT state_event_id,canonical_state_event FROM authoritative_state_events WHERE state_event_id=?",
            ("state-not-exhausted-drift-2",),
        ).fetchone()
        assert row is not None
        state_event = json.loads(bytes(row[1]).decode("utf-8"))
        assert isinstance(state_event, dict)
        payload = state_event["payload"]
        assert isinstance(payload, dict)
        assert payload["budgetExhausted"] is False
        payload["suspensionReason"] = "REQUIRES_USER_ACTION"
        payload["budgetExhausted"] = True
        state_event["payloadDigest"] = payload_digest(payload)
        canonical_blob = canonicalize(state_event)
        connection.execute("DROP TRIGGER trg_authoritative_state_events__reject_update")
        connection.execute(
            "UPDATE authoritative_state_events SET canonical_state_event=?,payload_digest=? WHERE state_event_id=?",
            (canonical_blob, state_event["payloadDigest"], row[0]),
        )

        with pytest.raises(BudgetRepositoryError, match="digest"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_budget_rebuild_rejects_exhausted_run_clock_state_drift() -> None:
    """预算耗尽 Run 的 BLOCKED 业务态不能冒充 SUSPENDED 时钟态恢复。"""
    from factory_agent.storage.sqlite.budget_repository import BudgetRepository, BudgetRepositoryError

    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection),
            clock=_Clock(
                _sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z"),
                _sample("boot-a", 11_000_000_000, "2026-08-24T00:00:10Z"),
            ),
            state_event_id_factory=iter(("state-run-state-drift-1", "state-run-state-drift-2")).__next__,
        )
        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        await service.observe(run_id="run-budget-1", budget_limit_ms=5_000)
        connection.execute("UPDATE runs SET budget_clock_state='BLOCKED' WHERE run_id='run-budget-1'")

        with pytest.raises(BudgetRepositoryError, match="budget clock state does not match"):
            BudgetRepository(SqliteUnitOfWork(connection, failure_probe=lambda _point: None)).reconstruct_projection(
                "run-budget-1"
            )
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_point",
    ["after_state_cas", "before_state_event_insert", "before_budget_clock_event_insert", "before_commit"],
)
async def test_budget_projection_and_clock_event_rollback_at_every_uow_gate(failure_point: str) -> None:
    """CAS、state event、clock event 和 COMMIT 任一门禁失败都不得留下半条写入。"""
    connection = _new_connection()
    try:
        _insert_run(connection)
        module = _budget_module()
        service = module.BudgetService(
            coordinator=_Coordinator(connection, failing_points={failure_point}),
            clock=_Clock(_sample("boot-a", 1_000_000_000, "2026-08-24T00:00:00Z")),
            state_event_id_factory=iter((f"state-failure-{failure_point}",)).__next__,
        )

        with pytest.raises(_InjectedFailure, match=failure_point):
            await service.observe(run_id="run-budget-1", budget_limit_ms=120_000)

        assert connection.execute("SELECT state_version,budget_accumulated_ms FROM runs").fetchone() == (0, 0)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM budget_clock_events").fetchone() == (0,)
    finally:
        connection.close()
