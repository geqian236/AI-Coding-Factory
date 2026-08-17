"""控制命令 requestId、CAS、追加 receipt 与 RESUME 语义集成测试。"""

from __future__ import annotations

import hashlib
import json
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
    BarrierMilestoneCommitError,
    BarrierMilestoneRequest,
    ObservedStateEvidence,
    TransitionPredicateError,
    TransitionService,
)
from factory_agent.domain.control import ControlCommandReceiptPhase, ControlCommandType
from factory_agent.domain.workflow import AchievedStage, RunObservedState, RunPhase, StepOutcome, StepPhase
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import plan_revision_digest, semantic_plan_hash
from factory_agent.state_machine.barriers import BarrierStepFacts
from factory_agent.state_machine.milestones import MilestoneEvidence
from factory_agent.state_machine.transitions import StateTransitionError
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork, StaleStateVersionError
from factory_agent.storage.sqlite.workflow_repository import SqliteWorkflowRepository

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


def _insert_plan_revision_for_barrier(
    connection: sqlite3.Connection,
    *,
    required_node_ids: tuple[str, ...],
    additional_barriers: tuple[tuple[str, int, tuple[str, ...], str], ...] = (),
    plan_revision_id: str = "plan-control-1",
    spec_revision: int = 1,
) -> str:
    """写入 FK-on 且双合同自洽的 PlanRevision fixture，供仓储作为唯一权威。"""
    barriers = [
        {
            "businessPhase": "PLANNING",
            "barrierOrdinal": 0,
            "requiredNodeIds": list(required_node_ids),
            "settleTimeoutMs": 30_000,
            "passPredicateId": "planning-approved-v1",
        }
    ]
    barriers.extend(
        {
            "businessPhase": business_phase,
            "barrierOrdinal": barrier_ordinal,
            "requiredNodeIds": list(node_ids),
            "settleTimeoutMs": 30_000,
            "passPredicateId": predicate_id,
        }
        for business_phase, barrier_ordinal, node_ids, predicate_id in additional_barriers
    )
    node_ids = ["plan"]
    node_ids.extend(node_id for node_id in required_node_ids if node_id not in node_ids)
    for _business_phase, _ordinal, extra_node_ids, _predicate_id in additional_barriers:
        node_ids.extend(node_id for node_id in extra_node_ids if node_id not in node_ids)
    node_location = {
        node_id: (business_phase, ordinal)
        for business_phase, ordinal, barrier_node_ids in (
            ("PLANNING", 0, required_node_ids),
            *(item[:3] for item in additional_barriers),
        )
        for node_id in barrier_node_ids
    }
    node_location.setdefault("plan", ("PLANNING", 0))
    artifact_ids = {
        "plan": ["artifact-planning"],
        "design-review": ["artifact-design-stale"],
    }
    node_type_by_id = {
        "plan": "PLAN",
        "design-review": "DESIGN_REVIEW",
        "implement": "IMPLEMENT",
        "node-unknown": "RECONCILE_TARGET",
    }
    success_predicate_by_id = {
        "plan": "planning-complete-v1",
        "design-review": "design-complete-v1",
        "implement": "implementation-complete-v1",
        "node-unknown": "planning-complete-v1",
    }
    nodes = []
    for node_id in node_ids:
        business_phase, barrier_ordinal = node_location.get(node_id, ("PLANNING", 0))
        node_type = node_type_by_id.get(
            node_id,
            "RECONCILE_TARGET" if node_id.startswith("node-unknown") else "PLAN",
        )
        nodes.append(
            {
                "logicalNodeId": node_id,
                "businessPhase": business_phase,
                "barrierOrdinal": barrier_ordinal,
                "nodeType": node_type,
                "required": any(
                    node_id in declared_ids
                    for _phase, _ordinal, declared_ids, _pred in [
                        ("PLANNING", 0, required_node_ids, "planning-approved-v1"),
                        *additional_barriers,
                    ]
                ),
                "dependsOn": [],
                "sideEffectClass": "workspace_write" if node_type == "IMPLEMENT" else "none",
                "requiredArtifacts": artifact_ids.get(node_id, []),
                "successPredicateId": success_predicate_by_id.get(node_id, "planning-complete-v1"),
                "timeoutMs": 30_000,
                "retryPolicyId": "no-retry-v1",
            }
        )
    run_spec: dict[str, object] = {
        "schemaVersion": 1,
        "specRevision": spec_revision,
        "parentRevisionId": None,
        "taskId": "task-control-1",
        "goal": "验证 Phase 1 状态控制面",
        "assumptions": ["测试使用真实事务"],
        "scope": {"include": ["apps/agent/src/factory_agent"], "exclude": ["Task 3+"]},
        "constraints": ["单 writer"],
        "acceptanceCriteria": ["状态 CAS 与 gate 原子"],
        "targetStage": "CODEX_APPROVED",
        "repository": {
            "mode": "existing",
            "root": "D:/codex项目/AI-Coding-Factory",
            "baseBranch": "factory/bootstrap-plan",
            "baseCommit": "a" * 40,
        },
        "workPlan": {"dagVersion": 1, "nodes": nodes, "barriers": barriers},
        "riskProfile": {"level": "medium", "reasons": ["状态控制测试"]},
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "intentAuthorizationId": "intent-control-1",
        "semanticPlanHash": SHA_A,
        "planRevisionDigest": SHA_A,
        "createdAt": "2026-08-17T08:00:00Z",
    }
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision: dict[str, object] = {
        "planRevisionId": plan_revision_id,
        "taskId": "task-control-1",
        "specRevision": spec_revision,
        "intentAuthorizationId": "intent-control-1",
        "semanticPlanHash": run_spec["semanticPlanHash"],
        "planRevisionDigest": SHA_A,
        "dagVersion": 1,
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "nodes": nodes,
        "barriers": barriers,
        "stageMaps": {"DESIGN_APPROVED": ["plan"], "CODEX_APPROVED": node_ids},
        "createdAt": "2026-08-17T08:00:00Z",
    }
    revision["planRevisionDigest"] = plan_revision_digest(revision)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision["semanticPlanHash"] = run_spec["semanticPlanHash"]
    revision["planRevisionDigest"] = plan_revision_digest(revision)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    canonical_run_spec = canonicalize(run_spec)
    canonical_revision = canonicalize(revision)
    _insert(
        connection,
        "plan_revisions",
        {
            "plan_revision_id": plan_revision_id,
            "task_id": "task-control-1",
            "spec_revision": spec_revision,
            "parent_revision_id": None,
            "intent_authorization_id": "intent-control-1",
            "semantic_plan_hash": revision["semanticPlanHash"],
            "plan_revision_digest": revision["planRevisionDigest"],
            "dag_version": 1,
            "node_capability_map_version": "node-capability-map.v1",
            "stage_capability_map_version": "stage-capability-map.v1",
            "created_at": "2026-08-17T08:00:00Z",
            "run_spec_schema_id": "run-spec.v1",
            "run_spec_schema_version": 1,
            "canonical_run_spec": canonical_run_spec,
            "plan_revision_schema_id": "plan-revision.v1",
            "plan_revision_schema_version": 1,
            "canonical_plan_revision": canonical_revision,
        },
    )
    connection.execute(
        "UPDATE tasks SET active_plan_revision_id=? WHERE task_id=?",
        (plan_revision_id, "task-control-1"),
    )
    return "sha256:" + hashlib.sha256(canonicalize(list(required_node_ids))).hexdigest()


def _connection(*, desired_state: str = "RUNNING", observed_state: str = "QUEUED") -> sqlite3.Connection:
    """加载真实 migration，并以 FK ON 建立可验证的控制测试 lineage。"""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
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
            "active_run_id": None,
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
        "intent_authorizations",
        {
            "intent_authorization_id": "intent-control-1",
            "task_id": "task-control-1",
            "user_id": "user-control-1",
            "requirement_digest": b"requirement",
            "project_id": "project-control-1",
            "repository_id": "repo-control-1",
            "repository_binding_digest": b"repository",
            "baseline_digest": b"baseline",
            "target_stage": "CODEX_APPROVED",
            "stage_capability_map_version": "stage-capability-map.v1",
            "allowed_capability_set_digest": b"capabilities",
            "target_binding_digest": b"target",
            "risk_ceiling": "medium",
            "estimated_cost_alert_digest": b"cost",
            "autonomous_execution_budget_ms": 60_000,
            "repair_loop_limit": 1,
            "auto_replan_limit": 1,
            "attempt_limit": 3,
            "issued_at": "2026-08-17T07:00:00Z",
            "expires_at": "2026-08-17T10:00:00Z",
            "revoked_at": None,
            "revoke_reason": None,
            "contract_schema_id": "intent-authorization.v1",
            "contract_schema_version": 1,
            "canonical_contract": b"{}",
            "canonical_contract_digest": b"contract",
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
    connection.execute(
        "UPDATE tasks SET active_run_id=? WHERE task_id=?",
        ("run-control-1", "task-control-1"),
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
async def test_immediate_stop_on_queued_run_without_attempt_completes_immediately() -> None:
    """QUEUED 且无 Attempt 时没有 executor 可停止，接受命令后必须追加 COMPLETED receipt。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        service = _service(connection)
        accepted = await service.submit(
            _request(request_id="request-stop-queued", command_type=ControlCommandType.IMMEDIATE_STOP)
        )
        assert accepted.command.acknowledged_attempt_id is None
        assert connection.execute("SELECT desired_state,observed_state,control_command_seq FROM runs").fetchone() == (
            "PAUSED",
            "QUEUED",
            1,
        )
        assert connection.execute(
            "SELECT phase FROM control_command_receipt_events ORDER BY receipt_seq"
        ).fetchall() == [("ACKNOWLEDGED",), ("COMPLETED",)]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_terminated_run_rejects_every_control_command() -> None:
    """终止 Run 没有可恢复控制面，任何命令都必须在事务入口拒绝。"""
    connection = _connection(desired_state="RUNNING", observed_state="TERMINATED")
    try:
        service = _service(connection)
        with pytest.raises(StateTransitionError):
            await service.submit(
                _request(request_id="request-terminal-pause", command_type=ControlCommandType.SOFT_PAUSE)
            )
        assert connection.execute("SELECT count(*) FROM control_commands").fetchone() == (0,)
        assert connection.execute("SELECT desired_state,state_version FROM runs").fetchone() == ("RUNNING", 0)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_queued_to_blocked_requires_authoritative_block_reason() -> None:
    """QUEUED→BLOCKED 必须有数据库阻断事实与原因，不能只凭目标枚举值写入。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-blocked-without-reason"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate=RunObservedState.BLOCKED,
                evidence=ObservedStateEvidence(0, False, False, False, False, False, False),
                request_id="request-blocked-without-reason",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
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
        planning_required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            additional_barriers=(("DESIGN_REVIEWING", 1, ("design-review",), "design-approved-v1"),),
        )
        design_required_node_set_digest = "sha256:" + hashlib.sha256(canonicalize(["design-review"])).hexdigest()
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-planning",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": planning_required_node_set_digest,
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
                "required_node_set_digest": design_required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "design-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-planning",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-planning",
                "logical_node_id": "plan",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-planning-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-planning",
                "step_id": "step-planning",
                "supersedes_attempt_id": None,
                "phase": "TERMINATED",
                "outcome": "SUCCEEDED",
                "executor_id": "executor-planning",
                "process_session_id": "process-planning",
                "pid": 1234,
                "process_start_time": "2026-08-14T07:59:00Z",
                "job_object_id": "job-planning",
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": 0,
                "termination_reason": None,
                "fencing_token": 1,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "DRAINED",
                "started_at": "2026-08-14T07:59:00Z",
                "ended_at": "2026-08-14T08:00:00Z",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "artifacts",
            {
                "artifact_id": "artifact-planning",
                "producer_attempt_id": "attempt-planning",
                "media_type": "text/plain",
                "confidentiality": "INTERNAL",
                "commit_state": "COMMITTED",
                "storage_path": "artifact-planning",
                "size_bytes": 1,
                "digest": SHA_A,
                "created_at": "2026-08-14T08:00:00Z",
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
        persisted_gate_digest = connection.execute(
            "SELECT settled,passed,gate_digest,state_version FROM phase_barriers WHERE barrier_id='barrier-planning'"
        ).fetchone()
        assert persisted_gate_digest[0] == 1
        assert persisted_gate_digest[1] == 1
        assert isinstance(persisted_gate_digest[2], str) and persisted_gate_digest[2].startswith("sha256:")
        assert persisted_gate_digest[2] != SHA_A
        assert persisted_gate_digest[3] == 1
        assert connection.execute(
            "SELECT phase,active_barrier_id,state_version FROM runs WHERE run_id='run-control-1'"
        ).fetchone() == ("DESIGN_REVIEWING", "barrier-design", 1)
        assert connection.execute(
            "SELECT achieved_stage,lifecycle,outcome_version,state_version FROM tasks WHERE task_id='task-control-1'"
        ).fetchone() == ("DESIGN_APPROVED", "ACTIVE", 1, 1)
        assert connection.execute(
            "SELECT aggregate_type FROM authoritative_state_events ORDER BY aggregate_type"
        ).fetchall() == [("PHASE_BARRIER",), ("RUN",), ("TASK",)]

        _insert(
            connection,
            "steps",
            {
                "step_id": "step-design-stale",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-design",
                "logical_node_id": "design-review",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "design-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-design-stale-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-design-stale",
                "step_id": "step-design-stale",
                "supersedes_attempt_id": None,
                "phase": "TERMINATED",
                "outcome": "SUCCEEDED",
                "executor_id": "executor-design-stale",
                "process_session_id": "process-design-stale",
                "pid": 1235,
                "process_start_time": "2026-08-14T08:01:00Z",
                "job_object_id": "job-design-stale",
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": 0,
                "termination_reason": None,
                "fencing_token": 2,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "DRAINED",
                "started_at": "2026-08-14T08:01:00Z",
                "ended_at": "2026-08-14T08:02:00Z",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "artifacts",
            {
                "artifact_id": "artifact-design-stale",
                "producer_attempt_id": "attempt-design-stale",
                "media_type": "text/plain",
                "confidentiality": "INTERNAL",
                "commit_state": "COMMITTED",
                "storage_path": "artifact-design-stale",
                "size_bytes": 1,
                "digest": SHA_A,
                "created_at": "2026-08-14T08:02:00Z",
            },
        )

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


@pytest.mark.asyncio
async def test_same_business_phase_barrier_uses_frozen_successor() -> None:
    """同一 business phase 的多个 barrier 必须按 PlanRevision 精确后继推进。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            additional_barriers=(("PLANNING", 1, ("node-next",), "planning-next-v1"),),
        )
        next_digest = "sha256:" + hashlib.sha256(canonicalize(["node-next"])).hexdigest()
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-planning-current",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
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
                "barrier_id": "barrier-planning-next",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 1,
                "required_node_set_digest": next_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-next-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-planning-current'")
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            updated = SqliteWorkflowRepository(unit_of_work).advance_run_phase(
                run_id="run-control-1",
                expected_state_version=0,
                candidate_phase=RunPhase.PLANNING,
                next_barrier_id="barrier-planning-next",
            )
            assert updated.phase is RunPhase.PLANNING
            assert updated.active_barrier_id == "barrier-planning-next"
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_transition_observed_rejects_fabricated_facts_when_authority_tables_empty() -> None:
    """空 lease/auth/receipt/artifact/finding 权威表不得被调用方 True 证据越过。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("implement",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-authority-1",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-authority-1",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-authority-1",
                "logical_node_id": "implement",
                "business_phase": "IMPLEMENTING",
                "node_type": "IMPLEMENT",
                "required": 1,
                "side_effect_class": "workspace_write",
                "phase": "RUNNING",
                "outcome": "NONE",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "implementation-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-authority-1-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-authority-1",
                "step_id": "step-authority-1",
                "supersedes_attempt_id": None,
                "phase": "RUNNING",
                "outcome": "NONE",
                "executor_id": "executor-authority-1",
                "process_session_id": "process-authority-1",
                "pid": 1234,
                "process_start_time": "2026-08-17T09:00:00Z",
                "job_object_id": "job-authority-1",
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": None,
                "termination_reason": None,
                "fencing_token": 7,
                "control_epoch": 9,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "NONE",
                "started_at": "2026-08-17T09:00:00Z",
                "ended_at": None,
                "state_version": 0,
            },
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authority"),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate="RUNNING",
                evidence=ObservedStateEvidence(1, True, True, True, True, False, False),
                request_id="request-authority-fabricated",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_step_from_stale_revision_or_phase() -> None:
    """同名节点若来自旧 PlanRevision 或错误业务阶段，不能伪造当前 barrier 成功。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-current",),
        )
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-current",),
            plan_revision_id="plan-old",
            spec_revision=2,
        )
        connection.execute(
            "UPDATE tasks SET active_plan_revision_id=? WHERE task_id=?",
            ("plan-control-1", "task-control-1"),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-current-lineage",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-stale-revision",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-old",
                "barrier_id": "barrier-current-lineage",
                "logical_node_id": "node-current",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-stale-revision-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-current-lineage'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-stale-revision"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-stale-revision",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-current-lineage",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(
                BarrierStepFacts(
                    step_id="step-stale-revision",
                    required=False,
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
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
        assert connection.execute("SELECT phase,state_version FROM runs").fetchone() == ("PLANNING", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_required_node_with_persisted_required_false() -> None:
    """Plan 声明的节点若 persisted required 漂移为 false，不能绕过 Artifact gate。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-current",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-required-flag-drift",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-required-flag-drift",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-required-flag-drift",
                "logical_node_id": "node-current",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-required-flag-drift-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-required-flag-drift'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-required-flag-drift"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-required-flag-drift",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-required-flag-drift",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(
                BarrierStepFacts(
                    step_id="step-required-flag-drift",
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
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
        assert connection.execute("SELECT phase,state_version FROM runs").fetchone() == ("PLANNING", 0)
        assert connection.execute("SELECT achieved_stage,state_version FROM tasks").fetchone() == ("NONE", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("step_phase", "attempt_phase", "drain_state", "should_reject"),
    [
        ("RUNNING", "RUNNING", "NONE", False),
        ("DISPATCHED", "STARTING", "NONE", True),
        ("RUNNING", "RUNNING", "DRAINING", True),
    ],
)
@pytest.mark.asyncio
async def test_transition_observed_requires_authoritative_dispatch_state(
    step_phase: str,
    attempt_phase: str,
    drain_state: str,
    should_reject: bool,
) -> None:
    """QUEUED→RUNNING 还必须有可派发的 Step/Attempt，DRAINING 不能复用旧执行。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("implement",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-authority-valid",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-authority-valid",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-authority-valid",
                "logical_node_id": "implement",
                "business_phase": "IMPLEMENTING",
                "node_type": "IMPLEMENT",
                "required": 1,
                "side_effect_class": "workspace_write",
                "phase": step_phase,
                "outcome": "NONE",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "implementation-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-authority-valid-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-authority-valid",
                "step_id": "step-authority-valid",
                "supersedes_attempt_id": None,
                "phase": attempt_phase,
                "outcome": "NONE",
                "executor_id": "executor-authority-valid",
                "process_session_id": "process-authority-valid",
                "pid": 1234,
                "process_start_time": "2026-08-17T08:59:00Z",
                "job_object_id": "job-authority-valid",
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": None,
                "termination_reason": None,
                "fencing_token": 7,
                "control_epoch": 9,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": drain_state,
                "started_at": "2026-08-17T08:59:00Z",
                "ended_at": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "resource_leases",
            {
                "resource_key": "resource-authority-valid",
                "owner_executor_id": "executor-authority-valid",
                "fencing_token": 7,
                "control_epoch": 9,
                "acquired_at": "2026-08-17T08:59:00Z",
                "heartbeat_at": "2026-08-17T08:59:00Z",
                "expires_at": "2026-08-17T10:00:00Z",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "execution_authorizations",
            {
                "execution_authorization_id": "auth-authority-valid",
                "intent_authorization_id": "intent-control-1",
                "plan_revision_id": "plan-control-1",
                "semantic_plan_hash": b"semantic",
                "plan_revision_digest": b"revision",
                "stage_capability_map_version": "stage-v1",
                "stage_capability_map_digest": b"stage",
                "node_capability_map_version": "node-v1",
                "node_capability_map_digest": b"node",
                "run_id": "run-control-1",
                "step_id": "step-authority-valid",
                "attempt_id": "attempt-authority-valid",
                "node_type": "IMPLEMENT",
                "executor_id": "executor-authority-valid",
                "resource_fingerprint": b"resource",
                "capability_scope_digest": b"scope",
                "idempotency_key": b"idempotency",
                "input_bindings": b"bindings",
                "action_capability": "repo.read",
                "action_policy_snapshot_digest": b"policy",
                "fencing_token": 7,
                "control_epoch": 9,
                "accepted_control_command_seq": 0,
                "max_uses": 1,
                "consumption_state": "AVAILABLE",
                "issued_at": "2026-08-17T08:59:00Z",
                "expires_at": "2026-08-17T10:00:00Z",
                "revoked_at": None,
                "revoke_reason": None,
                "contract_schema_id": "execution-authorization.v1",
                "contract_schema_version": 1,
                "canonical_contract": b"{}",
                "canonical_contract_digest": b"contract",
            },
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authority-valid"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        if should_reject:
            with pytest.raises(TransitionPredicateError):
                await service.transition_observed(
                    run_id="run-control-1",
                    expected_state_version=0,
                    candidate="RUNNING",
                    evidence=ObservedStateEvidence(0, False, False, False, False, True, True),
                    request_id="request-authority-invalid-dispatch",
                )
            assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
            assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
        else:
            updated = await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate="RUNNING",
                evidence=ObservedStateEvidence(0, False, False, False, False, True, True),
                request_id="request-authority-valid",
            )
            assert updated.observed_state.value == "RUNNING"
            assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("RUNNING", 1)
            assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_fabricated_request_facts_when_authority_steps_empty() -> None:
    """空 steps/attempts/artifacts/findings 时，调用方伪造成功摘要必须整事务回滚。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-authority-empty",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-authority-empty'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authority-empty"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-barrier-authority-empty",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-authority-empty",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(
                BarrierStepFacts(
                    step_id="fabricated-step",
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

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
        assert connection.execute("SELECT phase,state_version FROM runs").fetchone() == ("PLANNING", 0)
        assert connection.execute("SELECT achieved_stage,state_version FROM tasks").fetchone() == ("NONE", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_incomplete_authoritative_required_node_set() -> None:
    """声明 node-a/node-b 而只落库 node-a 时，gate 必须拒绝并保持三表不变。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-a", "node-b"),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-required-node-set",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-node-a",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-required-node-set",
                "logical_node_id": "node-a",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-node-a-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-required-node-set'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-required-node-set"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-required-node-set",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-required-node-set",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(
                BarrierStepFacts(
                    step_id="step-node-a",
                    required=False,
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
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
        assert connection.execute("SELECT phase,state_version FROM runs").fetchone() == ("PLANNING", 0)
        assert connection.execute("SELECT achieved_stage,state_version FROM tasks").fetchone() == ("NONE", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("expected_task_state_version", "expected_barrier_state_version", "should_reject"),
    [(0, 0, False), (41, 0, True), (0, 43, True)],
)
@pytest.mark.asyncio
async def test_unknown_remote_state_timeout_requires_all_expected_versions(
    expected_task_state_version: int,
    expected_barrier_state_version: int,
    should_reject: bool,
) -> None:
    """UNKNOWN 超时必须同时校验 Task、barrier、Run 版本后才可落 BLOCKED。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-unknown",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-unknown-timeout",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": "2026-08-17T08:00:00+00:00",
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-unknown",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-unknown-timeout",
                "logical_node_id": "node-unknown",
                "business_phase": "PLANNING",
                "node_type": "RECONCILE_TARGET",
                "required": 1,
                "side_effect_class": "none",
                "phase": "RECONCILING",
                "outcome": "UNKNOWN_REMOTE_STATE",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-unknown-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-unknown-timeout'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-unknown-timeout"),
            # 真实超时以服务时钟为准；请求时间故意落在 deadline 之前，形成回归 RED。
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-unknown-timeout",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-unknown-timeout",
            expected_task_state_version=expected_task_state_version,
            expected_run_state_version=0,
            expected_barrier_state_version=expected_barrier_state_version,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 7, 0, tzinfo=UTC),
            # 调用方故意声称成功；服务必须只采用事务内权威 UNKNOWN 事实。
            steps=(
                BarrierStepFacts(
                    step_id="step-unknown",
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
        if should_reject:
            with pytest.raises(StaleStateVersionError):
                await service.commit_passed_barrier(request)
            assert connection.execute(
                "SELECT observed_state,requires_user_action,block_reason_code,state_version FROM runs"
            ).fetchone() == ("QUEUED", 0, None, 0)
            assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
            assert connection.execute("SELECT achieved_stage,state_version FROM tasks").fetchone() == ("NONE", 0)
            assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
        else:
            result = await service.commit_passed_barrier(request)
            assert result.run.observed_state.value == "BLOCKED"
            assert result.run.requires_user_action is True
            assert result.run.block_reason_code == "UNKNOWN_REMOTE_STATE_TIMEOUT"
            assert connection.execute(
                "SELECT observed_state,requires_user_action,block_reason_code,state_version FROM runs"
            ).fetchone() == ("BLOCKED", 1, "UNKNOWN_REMOTE_STATE_TIMEOUT", 1)
            assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchone() == (0, 0, 0)
            assert connection.execute("SELECT achieved_stage,state_version FROM tasks").fetchone() == ("NONE", 0)
            event = connection.execute(
                "SELECT aggregate_type,canonical_state_event FROM authoritative_state_events"
            ).fetchone()
            assert event[0] == "RUN"
            assert json.loads(event[1].decode("utf-8"))["payload"]["actionRequired"] is True
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_attempt_only_unknown_timeout_persists_blocked_run_event() -> None:
    """仅 Attempt 带 UNKNOWN_REMOTE_STATE 时也必须形成 BLOCKED/行动闭环。"""
    connection = _connection()
    try:
        required_node_set_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("node-unknown-attempt",),
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": "barrier-unknown-attempt",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": required_node_set_digest,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": "2026-08-17T08:00:00+00:00",
                "pass_predicate_id": "planning-approved-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-unknown-attempt",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": "barrier-unknown-attempt",
                "logical_node_id": "node-unknown-attempt",
                "business_phase": "PLANNING",
                "node_type": "RECONCILE_TARGET",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "NONE",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-unknown-attempt-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-unknown-only",
                "step_id": "step-unknown-attempt",
                "supersedes_attempt_id": None,
                "phase": "TERMINATED",
                "outcome": "UNKNOWN_REMOTE_STATE",
                "executor_id": "executor-unknown-only",
                "process_session_id": "process-unknown-only",
                "pid": 1234,
                "process_start_time": "2026-08-17T07:00:00Z",
                "job_object_id": "job-unknown-only",
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": None,
                "termination_reason": "remote state unknown",
                "fencing_token": 1,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "DRAINED",
                "started_at": "2026-08-17T07:00:00Z",
                "ended_at": "2026-08-17T07:30:00Z",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id='barrier-unknown-attempt'")
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-unknown-attempt"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-unknown-attempt",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id="barrier-unknown-attempt",
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 7, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        result = await service.commit_passed_barrier(request)
        assert result.run.observed_state is RunObservedState.BLOCKED
        assert result.run.requires_user_action is True
        assert connection.execute(
            "SELECT observed_state,requires_user_action,block_reason_code,state_version FROM runs"
        ).fetchone() == ("BLOCKED", 1, "UNKNOWN_REMOTE_STATE_TIMEOUT", 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()
