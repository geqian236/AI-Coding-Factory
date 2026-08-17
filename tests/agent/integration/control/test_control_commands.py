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
    ControlRequestError,
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
from factory_agent.domain.workflow import (
    AchievedStage,
    PhaseBarrier,
    RunObservedState,
    RunPhase,
    StepOutcome,
    StepPhase,
)
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import plan_revision_digest, semantic_plan_hash
from factory_agent.state_machine.barriers import BarrierStepFacts
from factory_agent.state_machine.milestones import MilestoneEvidence
from factory_agent.state_machine.transitions import StateTransitionError
from factory_agent.storage.sqlite.unit_of_work import SqliteUnitOfWork, StaleStateVersionError
from factory_agent.storage.sqlite.workflow_repository import SqliteWorkflowRepository, WorkflowRepositoryError

SHA_A = "sha256:" + "a" * 64


def _selector_digest(values: list[str]) -> str:
    """按 PlanRevision selector 的 JCS 数组重算 Step 依赖/Artifact 摘要。"""
    return "sha256:" + hashlib.sha256(canonicalize(values)).hexdigest()


def _derived_barrier_id(
    connection: sqlite3.Connection,
    *,
    business_phase: str,
    barrier_ordinal: int,
    plan_revision_id: str = "plan-control-1",
    run_id: str = "run-control-1",
    barrier_id_override: str | None = None,
) -> str:
    """用冻结 PlanBarrier 工厂派生 fixture identity，测试污染时才显式覆盖 raw ID。"""
    row = connection.execute(
        "SELECT plan_revision_digest,canonical_plan_revision FROM plan_revisions WHERE plan_revision_id=?",
        (plan_revision_id,),
    ).fetchone()
    assert row is not None
    revision = json.loads(bytes(row[1]).decode("utf-8"))
    plan_barriers = [
        item
        for item in revision["barriers"]
        if item["businessPhase"] == business_phase and item["barrierOrdinal"] == barrier_ordinal
    ]
    assert len(plan_barriers) == 1
    derived = PhaseBarrier.from_plan_barrier(
        run_id=run_id,
        plan_revision_id=plan_revision_id,
        plan_revision_digest=str(row[0]),
        plan_barrier=plan_barriers[0],
    ).barrier_id
    return derived if barrier_id_override is None else barrier_id_override


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
    dependencies_by_node: Mapping[str, tuple[str, ...]] | None = None,
    optional_nodes: tuple[tuple[str, int, str], ...] = (),
    plan_revision_id: str = "plan-control-1",
    spec_revision: int = 1,
    run_spec_target_stage: str = "CODEX_APPROVED",
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
    dependency_map = {} if dependencies_by_node is None else dict(dependencies_by_node)
    node_ids = ["plan"]
    node_ids.extend(node_id for node_id in required_node_ids if node_id not in node_ids)
    for _business_phase, _ordinal, extra_node_ids, _predicate_id in additional_barriers:
        node_ids.extend(node_id for node_id in extra_node_ids if node_id not in node_ids)
    for dependency_ids in dependency_map.values():
        node_ids.extend(node_id for node_id in dependency_ids if node_id not in node_ids)
    for _business_phase, _ordinal, optional_node_id in optional_nodes:
        if optional_node_id not in node_ids:
            node_ids.append(optional_node_id)
    node_location = {
        node_id: (business_phase, ordinal)
        for business_phase, ordinal, barrier_node_ids in (
            ("PLANNING", 0, required_node_ids),
            *(item[:3] for item in additional_barriers),
        )
        for node_id in barrier_node_ids
    }
    node_location.setdefault("plan", ("PLANNING", 0))
    node_location.update({node_id: (business_phase, ordinal) for business_phase, ordinal, node_id in optional_nodes})
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
                "dependsOn": list(dependency_map.get(node_id, ())),
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
        "targetStage": run_spec_target_stage,
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


def _insert_current_barrier(
    connection: sqlite3.Connection,
    *,
    barrier_id: str | None = None,
    required_node_ids: tuple[str, ...] = ("plan",),
    settle_timeout_ms: int = 30_000,
    run_spec_target_stage: str = "CODEX_APPROVED",
    dependencies_by_node: Mapping[str, tuple[str, ...]] | None = None,
) -> str:
    """插入 FK-on 的当前 barrier，PlanRevision 与 Run/Task selector 保持有效谱系。"""
    required_digest = _insert_plan_revision_for_barrier(
        connection,
        required_node_ids=required_node_ids,
        run_spec_target_stage=run_spec_target_stage,
        dependencies_by_node=dependencies_by_node,
    )
    persisted_barrier_id = _derived_barrier_id(
        connection,
        business_phase="PLANNING",
        barrier_ordinal=0,
        barrier_id_override=barrier_id,
    )
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": persisted_barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": required_digest,
            "settle_timeout_ms": settle_timeout_ms,
            "settle_deadline_at": None,
            "pass_predicate_id": "planning-approved-v1",
            "settled": 0,
            "passed": 0,
            "gate_digest": None,
            "state_version": 0,
        },
    )
    connection.execute("UPDATE runs SET active_barrier_id=?", (persisted_barrier_id,))
    return required_digest


def _insert_passing_planning_graph(
    connection: sqlite3.Connection,
    *,
    next_pass_predicate_id: str = "design-approved-v1",
    next_required_node_set_digest: str | None = None,
    next_settle_timeout_ms: int = 30_000,
    run_spec_target_stage: str = "CODEX_APPROVED",
    dependencies_by_node: Mapping[str, tuple[str, ...]] | None = None,
) -> None:
    """插入可通过 planning gate 的完整图，并允许单独污染 next barrier 持久 selector。"""
    planning_digest = _insert_plan_revision_for_barrier(
        connection,
        required_node_ids=("plan",),
        additional_barriers=(("DESIGN_REVIEWING", 1, ("design-review",), "design-approved-v1"),),
        run_spec_target_stage=run_spec_target_stage,
        dependencies_by_node=dependencies_by_node,
    )
    design_digest = _selector_digest(["design-review"])
    planning_barrier_id = _derived_barrier_id(
        connection,
        business_phase="PLANNING",
        barrier_ordinal=0,
    )
    design_barrier_id = _derived_barrier_id(
        connection,
        business_phase="DESIGN_REVIEWING",
        barrier_ordinal=1,
    )
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": planning_barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": planning_digest,
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
            "barrier_id": design_barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "business_phase": "DESIGN_REVIEWING",
            "barrier_ordinal": 1,
            "required_node_set_digest": (
                design_digest if next_required_node_set_digest is None else next_required_node_set_digest
            ),
            "settle_timeout_ms": next_settle_timeout_ms,
            "settle_deadline_at": None,
            "pass_predicate_id": next_pass_predicate_id,
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
            "step_id": "step-planning-contract",
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "barrier_id": planning_barrier_id,
            "logical_node_id": "plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest(["artifact-planning"]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-planning-contract-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": "attempt-planning-contract",
            "step_id": "step-planning-contract",
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": "executor-planning-contract",
            "process_session_id": "process-planning-contract",
            "pid": 1234,
            "process_start_time": "2026-08-17T07:00:00Z",
            "job_object_id": "job-planning-contract",
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
            "started_at": "2026-08-17T07:00:00Z",
            "ended_at": "2026-08-17T07:30:00Z",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "artifacts",
        {
            "artifact_id": "artifact-planning",
            "producer_attempt_id": "attempt-planning-contract",
            "media_type": "text/plain",
            "confidentiality": "INTERNAL",
            "commit_state": "COMMITTED",
            "storage_path": "artifact-planning",
            "size_bytes": 1,
            "digest": SHA_A,
            "created_at": "2026-08-17T07:30:00Z",
        },
    )
    connection.execute("UPDATE runs SET active_barrier_id=?", (planning_barrier_id,))


def _insert_target_close_lease(connection: sqlite3.Connection) -> None:
    """插入只与已终止 Attempt 归属匹配的有效 lease，验证授权缺失时也必须阻断终局。"""
    _insert(
        connection,
        "resource_leases",
        {
            "resource_key": "resource-target-lease-only",
            "owner_executor_id": "executor-planning-contract",
            "fencing_token": 1,
            "control_epoch": 1,
            "acquired_at": "2026-08-17T08:00:00Z",
            "heartbeat_at": "2026-08-17T08:30:00Z",
            "expires_at": "2026-08-17T10:00:00Z",
            "state_version": 0,
        },
    )


def _insert_target_close_authorization(connection: sqlite3.Connection) -> None:
    """插入独立 AVAILABLE 授权但不插入 lease，验证授权路径不会依赖 lease 查询。"""
    plan_row = connection.execute(
        "SELECT semantic_plan_hash,plan_revision_digest FROM plan_revisions WHERE plan_revision_id=?",
        ("plan-control-1",),
    ).fetchone()
    assert plan_row is not None
    _insert(
        connection,
        "execution_authorizations",
        {
            "execution_authorization_id": "auth-target-auth-only",
            "intent_authorization_id": "intent-control-1",
            "plan_revision_id": "plan-control-1",
            # AVAILABLE 正例必须是真实的当前 PlanRevision 合同，避免测试只因伪造摘要而阻断。
            "semantic_plan_hash": canonicalize(plan_row[0]),
            "plan_revision_digest": canonicalize(plan_row[1]),
            "stage_capability_map_version": "stage-capability-map.v1",
            "stage_capability_map_digest": b"stage",
            "node_capability_map_version": "node-capability-map.v1",
            "node_capability_map_digest": b"node",
            "run_id": "run-control-1",
            "step_id": "step-planning-contract",
            "attempt_id": "attempt-planning-contract",
            "node_type": "PLAN",
            "executor_id": "executor-planning-contract",
            "resource_fingerprint": b"resource",
            "capability_scope_digest": b"scope",
            "idempotency_key": b"idempotency-auth-only",
            "input_bindings": b"bindings",
            "action_capability": "repo.read",
            "action_policy_snapshot_digest": b"policy",
            "fencing_token": 1,
            "control_epoch": 1,
            "accepted_control_command_seq": 0,
            "max_uses": 1,
            "consumption_state": "AVAILABLE",
            "issued_at": "2026-08-17T08:00:00Z",
            "expires_at": "2026-08-17T10:00:00Z",
            "revoked_at": None,
            "revoke_reason": None,
            "contract_schema_id": "execution-authorization.v1",
            "contract_schema_version": 1,
            "canonical_contract": b"{}",
            "canonical_contract_digest": b"contract",
        },
    )


def _insert_second_intent_authorization(connection: sqlite3.Connection) -> None:
    """复制 FK 完整的意图授权，供只漂移 execution authorization intent 绑定的反例使用。"""
    cursor = connection.execute(
        "SELECT intent_authorization_id,task_id,user_id,requirement_digest,project_id,"
        "repository_id,repository_binding_digest,baseline_digest,target_stage,"
        "stage_capability_map_version,allowed_capability_set_digest,target_binding_digest,"
        "risk_ceiling,estimated_cost_alert_digest,autonomous_execution_budget_ms,"
        "repair_loop_limit,auto_replan_limit,attempt_limit,issued_at,expires_at,revoked_at,"
        "revoke_reason,contract_schema_id,contract_schema_version,canonical_contract,"
        "canonical_contract_digest FROM intent_authorizations WHERE intent_authorization_id=?",
        ("intent-control-1",),
    )
    row = cursor.fetchone()
    assert row is not None
    values = dict(zip((item[0] for item in cursor.description or ()), row, strict=True))
    values["intent_authorization_id"] = "intent-control-2"
    _insert(connection, "intent_authorizations", values)


def _target_close_request(*, planning_barrier_id: str, request_id: str) -> BarrierMilestoneRequest:
    """构造统一 target close 请求，调用方证据仍是故意无效的兼容摘要。"""
    return BarrierMilestoneRequest(
        request_id=request_id,
        task_id="task-control-1",
        run_id="run-control-1",
        barrier_id=planning_barrier_id,
        expected_task_state_version=0,
        expected_run_state_version=0,
        expected_barrier_state_version=0,
        next_phase=RunPhase.PLANNING,
        next_barrier_id=None,
        candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
        gate_digest=SHA_A,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        steps=(),
        milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
    )


def _insert_secondary_run_graph(connection: sqlite3.Connection) -> tuple[str, str]:
    """插入 FK-on 的第二 Run/Step/Attempt，供授权双向错配测试使用。"""
    planning_barrier_id = _derived_barrier_id(
        connection,
        business_phase="PLANNING",
        barrier_ordinal=0,
        run_id="run-control-2",
    )
    current_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    required_digest = connection.execute(
        "SELECT required_node_set_digest FROM phase_barriers WHERE barrier_id=?", (current_barrier_id,)
    ).fetchone()[0]
    _insert(
        connection,
        "runs",
        {
            "run_id": "run-control-2",
            "task_id": "task-control-1",
            "desired_state": "RUNNING",
            "observed_state": "QUEUED",
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
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": planning_barrier_id,
            "run_id": "run-control-2",
            "plan_revision_id": "plan-control-1",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": required_digest,
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
            "step_id": "step-control-2",
            "run_id": "run-control-2",
            "plan_revision_id": "plan-control-1",
            "barrier_id": planning_barrier_id,
            "logical_node_id": "plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest(["artifact-planning"]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-control-2-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": "attempt-control-2",
            "step_id": "step-control-2",
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": "executor-control-2",
            "process_session_id": "process-control-2",
            "pid": 2345,
            "process_start_time": "2026-08-17T07:00:00Z",
            "job_object_id": "job-control-2",
            "wsl_distro": None,
            "container_id": None,
            "image_digest": None,
            "exit_code": 0,
            "termination_reason": None,
            "fencing_token": 7,
            "control_epoch": 2,
            "accepted_control_command_seq": 0,
            "interrupt_command_id": None,
            "drain_state": "DRAINED",
            "started_at": "2026-08-17T07:00:00Z",
            "ended_at": "2026-08-17T07:30:00Z",
            "state_version": 0,
        },
    )
    connection.execute("UPDATE runs SET active_barrier_id=? WHERE run_id=?", (planning_barrier_id, "run-control-2"))
    return "step-control-2", "attempt-control-2"


def _insert_historical_attempt_graph(connection: sqlite3.Connection) -> tuple[str, str, str]:
    """插入同一 Task 的旧 PlanRevision 与自洽终态 Step/Attempt，保持 FK-on。"""
    current_plan_revision_id = connection.execute(
        "SELECT active_plan_revision_id FROM tasks WHERE task_id=?", ("task-control-1",)
    ).fetchone()[0]
    required_digest = _insert_plan_revision_for_barrier(
        connection,
        required_node_ids=("plan",),
        plan_revision_id="plan-control-old",
        spec_revision=2,
        run_spec_target_stage="DESIGN_APPROVED",
    )
    connection.execute(
        "UPDATE tasks SET active_plan_revision_id=? WHERE task_id=?",
        (current_plan_revision_id, "task-control-1"),
    )
    old_barrier_id = _derived_barrier_id(
        connection,
        business_phase="PLANNING",
        barrier_ordinal=0,
        plan_revision_id="plan-control-old",
    )
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": old_barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-old",
            "business_phase": "PLANNING",
            "barrier_ordinal": 0,
            "required_node_set_digest": required_digest,
            "settle_timeout_ms": 30_000,
            "settle_deadline_at": None,
            "pass_predicate_id": "planning-approved-v1",
            "settled": 1,
            "passed": 1,
            "gate_digest": SHA_A,
            "state_version": 1,
        },
    )
    _insert(
        connection,
        "steps",
        {
            "step_id": "step-planning-historical",
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-old",
            "barrier_id": old_barrier_id,
            "logical_node_id": "plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest(["artifact-planning"]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-planning-historical-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": "attempt-planning-historical",
            "step_id": "step-planning-historical",
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": "executor-planning-historical",
            "process_session_id": "process-planning-historical",
            "pid": 3456,
            "process_start_time": "2026-08-17T06:00:00Z",
            "job_object_id": "job-planning-historical",
            "wsl_distro": None,
            "container_id": None,
            "image_digest": None,
            "exit_code": 0,
            "termination_reason": None,
            "fencing_token": 11,
            "control_epoch": 4,
            "accepted_control_command_seq": 0,
            "interrupt_command_id": None,
            "drain_state": "DRAINED",
            "started_at": "2026-08-17T06:00:00Z",
            "ended_at": "2026-08-17T06:30:00Z",
            "state_version": 0,
        },
    )
    return "plan-control-old", "step-planning-historical", "attempt-planning-historical"


def _insert_historical_target_authorization(
    connection: sqlite3.Connection,
    *,
    plan_revision_id: str,
    step_id: str,
    attempt_id: str,
    active: bool,
) -> None:
    """把授权完整绑定到旧 Step/Attempt，再切换为历史失效或当前有效状态。"""
    _insert_target_close_authorization(connection)
    plan_row = connection.execute(
        "SELECT intent_authorization_id,semantic_plan_hash,plan_revision_digest "
        "FROM plan_revisions WHERE plan_revision_id=?",
        (plan_revision_id,),
    ).fetchone()
    assert plan_row is not None
    state = "AVAILABLE" if active else "CONSUMED"
    max_uses = 1 if active else 0
    issued_at = "2026-08-17T08:00:00Z" if active else "2026-08-17T06:00:00Z"
    expires_at = "2026-08-17T10:00:00Z" if active else "2026-08-17T07:00:00Z"
    revoked_at = None if active else "2026-08-17T07:30:00Z"
    revoke_reason = None if active else "superseded-by-replan"
    connection.execute(
        "UPDATE execution_authorizations SET intent_authorization_id=?,plan_revision_id=?,"
        "semantic_plan_hash=?,plan_revision_digest=?,step_id=?,attempt_id=?,executor_id=?,"
        "fencing_token=?,control_epoch=?,consumption_state=?,max_uses=?,issued_at=?,expires_at=?,"
        "revoked_at=?,revoke_reason=? WHERE execution_authorization_id=?",
        (
            plan_row[0],
            plan_revision_id,
            canonicalize(plan_row[1]),
            canonicalize(plan_row[2]),
            step_id,
            attempt_id,
            "executor-planning-historical",
            11,
            4,
            state,
            max_uses,
            issued_at,
            expires_at,
            revoked_at,
            revoke_reason,
            "auth-target-auth-only",
        ),
    )


_LINEAGE_MUTATIONS = (
    "run_id",
    "step_attempt",
    "step_run",
    "plan_revision",
    "intent_authorization",
    "semantic_plan_hash",
    "plan_revision_digest",
    "node_type",
    "executor_id",
    "fencing_token",
    "control_epoch",
    "control_seq",
)


def _prepare_inactive_authorization_lineage_mutation(
    connection: sqlite3.Connection,
    *,
    mutation: str,
) -> str:
    """构造已失效授权并仅漂移一个绑定字段，确保 lineage 校验本身被命中。"""
    if mutation not in _LINEAGE_MUTATIONS:
        raise AssertionError(f"未知授权 lineage 变异：{mutation}")
    _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
    connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
    planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    if mutation in {"run_id", "step_run"}:
        _insert_secondary_run_graph(connection)
    old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
    _insert_historical_target_authorization(
        connection,
        plan_revision_id=old_plan_revision_id,
        step_id=old_step_id,
        attempt_id=old_attempt_id,
        active=False,
    )
    if mutation == "run_id":
        connection.execute(
            "UPDATE execution_authorizations SET run_id=? WHERE execution_authorization_id=?",
            ("run-control-2", "auth-target-auth-only"),
        )
    elif mutation == "step_attempt":
        connection.execute(
            "UPDATE execution_authorizations SET attempt_id=? WHERE execution_authorization_id=?",
            ("attempt-planning-contract", "auth-target-auth-only"),
        )
    elif mutation == "step_run":
        connection.execute("UPDATE steps SET run_id=? WHERE step_id=?", ("run-control-2", old_step_id))
    elif mutation == "plan_revision":
        connection.execute(
            "UPDATE execution_authorizations SET plan_revision_id=? WHERE execution_authorization_id=?",
            ("plan-control-1", "auth-target-auth-only"),
        )
    elif mutation == "intent_authorization":
        _insert_second_intent_authorization(connection)
        connection.execute(
            "UPDATE execution_authorizations SET intent_authorization_id=? WHERE execution_authorization_id=?",
            ("intent-control-2", "auth-target-auth-only"),
        )
    elif mutation == "semantic_plan_hash":
        connection.execute(
            "UPDATE execution_authorizations SET semantic_plan_hash=? WHERE execution_authorization_id=?",
            (b"mutated-semantic-plan-hash", "auth-target-auth-only"),
        )
    elif mutation == "plan_revision_digest":
        connection.execute(
            "UPDATE execution_authorizations SET plan_revision_digest=? WHERE execution_authorization_id=?",
            (b"mutated-plan-revision-digest", "auth-target-auth-only"),
        )
    elif mutation == "node_type":
        connection.execute(
            "UPDATE execution_authorizations SET node_type=? WHERE execution_authorization_id=?",
            ("IMPLEMENT", "auth-target-auth-only"),
        )
    elif mutation == "executor_id":
        connection.execute(
            "UPDATE execution_authorizations SET executor_id=? WHERE execution_authorization_id=?",
            ("executor-planning-contract", "auth-target-auth-only"),
        )
    elif mutation == "fencing_token":
        connection.execute(
            "UPDATE execution_authorizations SET fencing_token=? WHERE execution_authorization_id=?",
            (1, "auth-target-auth-only"),
        )
    elif mutation == "control_epoch":
        connection.execute(
            "UPDATE execution_authorizations SET control_epoch=? WHERE execution_authorization_id=?",
            (1, "auth-target-auth-only"),
        )
    elif mutation == "control_seq":
        connection.execute(
            "UPDATE execution_authorizations SET accepted_control_command_seq=? WHERE execution_authorization_id=?",
            (1, "auth-target-auth-only"),
        )
    return planning_barrier_id


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


def _insert_active_attempt_graph(
    connection: sqlite3.Connection,
    *,
    step_phase: str = "RUNNING",
    step_outcome: str = "NONE",
    attempt_phase: str = "RUNNING",
    attempt_outcome: str = "NONE",
    drain_state: str = "NONE",
) -> None:
    """插入带 FK 谱系的执行事实，供控制 receipt 与 observed authority 反例复用。"""
    _insert_current_barrier(connection)
    barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    _insert(
        connection,
        "steps",
        {
            "step_id": "step-control-active",
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "barrier_id": barrier_id,
            "logical_node_id": "plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": step_phase,
            "outcome": step_outcome,
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest(["artifact-planning"]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-control-active-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": "attempt-control-lineage",
            "step_id": "step-control-active",
            "supersedes_attempt_id": None,
            "phase": attempt_phase,
            "outcome": attempt_outcome,
            "executor_id": "executor-control-lineage",
            "process_session_id": "process-control-lineage",
            "pid": 1234,
            "process_start_time": "2026-08-17T07:00:00Z",
            "job_object_id": "job-control-lineage",
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
            "started_at": "2026-08-17T07:00:00Z",
            "ended_at": None,
            "state_version": 0,
        },
    )


def _insert_successful_plan_step(
    connection: sqlite3.Connection,
    *,
    barrier_id: str,
    step_id: str,
    attempt_id: str,
    logical_node_id: str = "plan",
    required_artifact_id: str = "artifact-planning",
) -> None:
    """为 barrier 顺序测试插入已收口且有精确 Artifact 的成功 Step。"""
    _insert(
        connection,
        "steps",
        {
            "step_id": step_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "barrier_id": barrier_id,
            "logical_node_id": logical_node_id,
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 1,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest([required_artifact_id]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": f"{step_id}-v1",
            "state_version": 0,
        },
    )

    _insert(
        connection,
        "attempts",
        {
            "attempt_id": attempt_id,
            "step_id": step_id,
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": f"executor-{attempt_id}",
            "process_session_id": f"process-{attempt_id}",
            "pid": 1234,
            "process_start_time": "2026-08-17T07:00:00Z",
            "job_object_id": f"job-{attempt_id}",
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
            "started_at": "2026-08-17T07:00:00Z",
            "ended_at": "2026-08-17T07:30:00Z",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "artifacts",
        {
            "artifact_id": required_artifact_id,
            "producer_attempt_id": attempt_id,
            "media_type": "text/plain",
            "confidentiality": "INTERNAL",
            "commit_state": "COMMITTED",
            "storage_path": required_artifact_id,
            "size_bytes": 1,
            "digest": SHA_A,
            "created_at": "2026-08-17T07:30:00Z",
        },
    )


def _insert_successful_design_review_step(
    connection: sqlite3.Connection,
    *,
    barrier_id: str,
    step_id: str,
    attempt_id: str,
    plan_revision_id: str = "plan-control-1",
    artifact_id: str = "artifact-design-stale",
) -> None:
    """为后继 barrier 预执行反例写入完整成功 Step、Attempt 与 Artifact。"""
    _insert(
        connection,
        "steps",
        {
            "step_id": step_id,
            "run_id": "run-control-1",
            "plan_revision_id": plan_revision_id,
            "barrier_id": barrier_id,
            "logical_node_id": "design-review",
            "business_phase": "DESIGN_REVIEWING",
            "node_type": "DESIGN_REVIEW",
            "required": 1,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest([artifact_id]),
            "success_predicate_id": "design-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": f"{step_id}-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": attempt_id,
            "step_id": step_id,
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": f"executor-{attempt_id}",
            "process_session_id": f"process-{attempt_id}",
            "pid": 1234,
            "process_start_time": "2026-08-17T07:00:00Z",
            "job_object_id": f"job-{attempt_id}",
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
            "started_at": "2026-08-17T07:00:00Z",
            "ended_at": "2026-08-17T07:30:00Z",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "artifacts",
        {
            "artifact_id": artifact_id,
            "producer_attempt_id": attempt_id,
            "media_type": "text/plain",
            "confidentiality": "INTERNAL",
            "commit_state": "COMMITTED",
            "storage_path": artifact_id,
            "size_bytes": 1,
            "digest": SHA_A,
            "created_at": "2026-08-17T07:30:00Z",
        },
    )


def _planning_to_design_request(
    *,
    request_id: str,
    barrier_id: str,
    next_barrier_id: str,
    next_phase: RunPhase = RunPhase.DESIGN_REVIEWING,
) -> BarrierMilestoneRequest:
    """构造 planning gate 的最小请求；证据摘要不参与权威判定。"""
    return BarrierMilestoneRequest(
        request_id=request_id,
        task_id="task-control-1",
        run_id="run-control-1",
        barrier_id=barrier_id,
        expected_task_state_version=0,
        expected_run_state_version=0,
        expected_barrier_state_version=0,
        next_phase=next_phase,
        next_barrier_id=next_barrier_id,
        candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
        gate_digest=SHA_A,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        steps=(),
        milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
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
async def test_control_receipt_requires_attempt_lineage_and_terminal_fsm() -> None:
    """receipt 必须沿 acknowledged Attempt 追加，COMPLETED/FAILED 后禁止冲突终态。"""
    connection = _connection()
    try:
        _insert_active_attempt_graph(connection)
        service = _service(connection)
        accepted = await service.submit(_request())
        assert accepted.command.acknowledged_attempt_id == "attempt-control-lineage"
        with pytest.raises(ControlRequestError):
            await service.append_receipt(
                command_id=accepted.command.command_id,
                phase=ControlCommandReceiptPhase.COMPLETED,
                attempt_id="attempt-other",
                evidence_digest=SHA_A,
            )
        completed = await service.append_receipt(
            command_id=accepted.command.command_id,
            phase=ControlCommandReceiptPhase.COMPLETED,
            attempt_id="attempt-control-lineage",
            evidence_digest=SHA_A,
        )
        assert completed.receipt_seq == 1
        with pytest.raises(ControlRequestError):
            await service.append_receipt(
                command_id=accepted.command.command_id,
                phase=ControlCommandReceiptPhase.FAILED,
                attempt_id="attempt-control-lineage",
                evidence_digest=SHA_A,
            )
        assert connection.execute(
            "SELECT phase,attempt_id FROM control_command_receipt_events ORDER BY receipt_seq"
        ).fetchall() == [
            ("ACKNOWLEDGED", "attempt-control-lineage"),
            ("COMPLETED", "attempt-control-lineage"),
        ]
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
async def test_unknown_interrupting_attempt_cannot_transition_to_stopping() -> None:
    """UNKNOWN_REMOTE_STATE 与 INTERRUPTING/DRAINING 并存时必须保持 RECONCILING。"""
    connection = _connection(desired_state="PAUSED", observed_state="RUNNING")
    try:
        _insert_active_attempt_graph(
            connection,
            step_phase="RUNNING",
            step_outcome="UNKNOWN_REMOTE_STATE",
            attempt_phase="INTERRUPTING",
            attempt_outcome="UNKNOWN_REMOTE_STATE",
            drain_state="DRAINING",
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-unknown-stopping"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate=RunObservedState.STOPPING,
                evidence=ObservedStateEvidence(1, False, False, True, False, True, False),
                request_id="request-unknown-stopping",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("RUNNING", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_invalid_observed_state_enum_maps_to_factory_error() -> None:
    """非法 observed 枚举必须映射为稳定 FactoryError，而不是泄漏 ValueError。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-invalid-observed"),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate="NOT_A_RUN_STATE",
                evidence=ObservedStateEvidence(0, False, False, False, False, False, False),
                request_id="request-invalid-observed",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("step_outcome", "attempt_outcome"),
    [("NONE", "SUCCEEDED"), ("SUCCEEDED", "NONE")],
)
@pytest.mark.asyncio
async def test_terminated_requires_known_terminal_step_and_attempt_outcomes(
    step_outcome: str,
    attempt_outcome: str,
) -> None:
    """TERMINATED 必须同时有 known terminal Step 与 Attempt 事实，不能接受 NONE。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_current_barrier(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-terminal-outcome",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": barrier_id,
                "logical_node_id": "plan",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": step_outcome,
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-planning"]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-terminal-outcome-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-terminal-outcome",
                "step_id": "step-terminal-outcome",
                "supersedes_attempt_id": None,
                "phase": "TERMINATED",
                "outcome": attempt_outcome,
                "executor_id": "executor-terminal-outcome",
                "process_session_id": "process-terminal-outcome",
                "pid": 1234,
                "process_start_time": "2026-08-17T07:00:00Z",
                "job_object_id": "job-terminal-outcome",
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
                "started_at": "2026-08-17T07:00:00Z",
                "ended_at": "2026-08-17T07:30:00Z",
                "state_version": 0,
            },
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-terminal-outcome"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate=RunObservedState.TERMINATED,
                evidence=ObservedStateEvidence(0, False, False, False, False, False, False),
                request_id=f"request-terminal-outcome-{step_outcome}-{attempt_outcome}",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_terminated_allows_zero_attempt_cancelled_before_dispatch() -> None:
    """尚未派发即取消的终态 Step 不需要虚构 Attempt，且只追加一条 Run event。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_current_barrier(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-cancelled-before-dispatch",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": barrier_id,
                "logical_node_id": "plan",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "CANCELLED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-planning"]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-cancelled-before-dispatch-v1",
                "state_version": 0,
            },
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-cancelled-before-dispatch"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        result = await service.transition_observed(
            run_id="run-control-1",
            expected_state_version=0,
            candidate=RunObservedState.TERMINATED,
            evidence=ObservedStateEvidence(0, False, False, False, False, False, False),
            request_id="request-cancelled-before-dispatch",
        )
        assert result.observed_state is RunObservedState.TERMINATED
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("TERMINATED", 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


def test_barrier_authority_rejects_settle_timeout_drift() -> None:
    """PlanRevision 的 settleTimeoutMs 与运行时 barrier 漂移时必须 fail closed。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection, settle_timeout_ms=60_000)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_plan_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize("drift_field", ["dependency_hash", "required_artifacts_digest"])
def test_barrier_authority_rejects_node_selector_digest_drift(drift_field: str) -> None:
    """Step 的依赖与 Artifact selector 摘要必须由 PlanRevision 派生，不能接受任意 sha256。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-selector-digest-drift",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": barrier_id,
                "logical_node_id": "plan",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": SHA_A if drift_field == "dependency_hash" else _selector_digest([]),
                "required_artifacts_digest": (
                    SHA_A if drift_field == "required_artifacts_digest" else _selector_digest(["artifact-planning"])
                ),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-selector-digest-drift-v1",
                "state_version": 0,
            },
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


def test_barrier_authority_rejects_run_dag_version_drift() -> None:
    """Run.dag_version 必须与 active PlanRevision 的 DAG selector 一致。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        connection.execute("UPDATE runs SET dag_version=999")
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_plan_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


def test_barrier_authority_rejects_run_spec_target_stage_drift() -> None:
    """active RunSpec.targetStage 必须与 Task.target_stage 一致，不能只看 Task 投影。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection, run_spec_target_stage="DESIGN_APPROVED")
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_plan_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize("dependency_outcome", [None, "FAILED"])
def test_barrier_authority_rejects_missing_or_failed_dependency_step(dependency_outcome: str | None) -> None:
    """冻结 dependsOn 必须有同 Run/Revision 的成功 Step，不能只校验摘要。"""
    connection = _connection()
    try:
        _insert_current_barrier(
            connection,
            required_node_ids=("node-current",),
            dependencies_by_node={"node-current": ("node-dependency",)},
        )
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-current-dependency-gate",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": barrier_id,
                "logical_node_id": "node-current",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest(["node-dependency"]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-current-dependency-gate-v1",
                "state_version": 0,
            },
        )
        if dependency_outcome is not None:
            _insert(
                connection,
                "steps",
                {
                    "step_id": "step-dependency-gate",
                    "run_id": "run-control-1",
                    "plan_revision_id": "plan-control-1",
                    "barrier_id": barrier_id,
                    "logical_node_id": "node-dependency",
                    "business_phase": "PLANNING",
                    "node_type": "PLAN",
                    "required": 0,
                    "side_effect_class": "none",
                    "phase": "TERMINAL",
                    "outcome": dependency_outcome,
                    "dependency_hash": _selector_digest([]),
                    "required_artifacts_digest": _selector_digest([]),
                    "success_predicate_id": "planning-complete-v1",
                    "timeout_ms": 30_000,
                    "retry_policy_id": "no-retry-v1",
                    "idempotency_key": "step-dependency-gate-v1",
                    "state_version": 0,
                },
            )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pass_predicate_id", "CORRUPTED-NEXT-PREDICATE"),
        ("required_node_set_digest", SHA_A),
        ("settle_timeout_ms", 60_000),
    ],
)
@pytest.mark.asyncio
async def test_passed_barrier_rejects_next_selector_contract_drift(field: str, value: object) -> None:
    """next barrier 的 predicate/node-set/timeout 漂移必须阻断三聚合原子提交。"""
    connection = _connection()
    try:
        overrides = {
            "next_pass_predicate_id": "design-approved-v1",
            "next_required_node_set_digest": None,
            "next_settle_timeout_ms": 30_000,
        }
        overrides[
            {
                "pass_predicate_id": "next_pass_predicate_id",
                "required_node_set_digest": "next_required_node_set_digest",
                "settle_timeout_ms": "next_settle_timeout_ms",
            }[field]
        ] = value
        _insert_passing_planning_graph(connection, **overrides)
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-next-selector-drift"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id=f"request-next-selector-drift-{field}",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=planning_barrier_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=design_barrier_id,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute("SELECT settled,passed,state_version FROM phase_barriers").fetchall() == [
            (0, 0, 0),
            (0, 0, 0),
        ]
        assert connection.execute("SELECT phase,active_barrier_id,state_version FROM runs").fetchone() == (
            "PLANNING",
            planning_barrier_id,
            0,
        )
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


def test_barrier_authority_rejects_non_derived_barrier_identity() -> None:
    """barrier_id 必须由 run/revisionDigest/phase/ordinal 的冻结公式派生。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection, barrier_id="barrier-probe")
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError):
                SqliteWorkflowRepository(unit_of_work).load_barrier_plan_authority(
                    barrier_id="barrier-probe",
                    run_id="run-control-1",
                    task_id="task-control-1",
                )
        finally:
            unit_of_work.rollback()
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
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": planning_barrier_id,
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
                "barrier_id": design_barrier_id,
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
                "barrier_id": planning_barrier_id,
                "logical_node_id": "plan",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-planning"]),
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
        connection.execute("UPDATE runs SET active_barrier_id=?", (planning_barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-barrier"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-barrier-1",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=planning_barrier_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=design_barrier_id,
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
            "SELECT settled,passed,gate_digest,state_version FROM phase_barriers WHERE barrier_id=?",
            (planning_barrier_id,),
        ).fetchone()
        assert persisted_gate_digest[0] == 1
        assert persisted_gate_digest[1] == 1
        assert isinstance(persisted_gate_digest[2], str) and persisted_gate_digest[2].startswith("sha256:")
        assert persisted_gate_digest[2] != SHA_A
        assert persisted_gate_digest[3] == 1
        assert connection.execute(
            "SELECT phase,active_barrier_id,state_version FROM runs WHERE run_id='run-control-1'"
        ).fetchone() == ("DESIGN_REVIEWING", design_barrier_id, 1)
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
                "barrier_id": design_barrier_id,
                "logical_node_id": "design-review",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-design-stale"]),
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
            barrier_id=design_barrier_id,
            expected_task_state_version=1,
            expected_run_state_version=0,
            next_phase=RunPhase.PREPARING_WORKSPACE,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.CODEX_APPROVED,
        )
        with pytest.raises(StaleStateVersionError):
            await service.commit_passed_barrier(stale)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?",
            (design_barrier_id,),
        ).fetchone() == (0, 0, 0)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_closes_run_without_selecting_successor() -> None:
    """达到 RunSpec targetStage 时必须原子收口 Run，不得把 Task 标成成功后继续执行。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-close"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-target-close",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=planning_barrier_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.PLANNING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        result = await service.commit_passed_barrier(request)
        assert result.task.lifecycle.value == "SUCCEEDED"
        assert result.run.observed_state is RunObservedState.TERMINATED
        assert result.run.active_barrier_id is None
        assert connection.execute(
            "SELECT phase,active_barrier_id,observed_state FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == ("PLANNING", None, "TERMINATED")
        assert connection.execute(
            "SELECT lifecycle,achieved_stage FROM tasks WHERE task_id=?", ("task-control-1",)
        ).fetchone() == ("SUCCEEDED", "DESIGN_APPROVED")
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_precreated_successor_step_and_rolls_back() -> None:
    """目标收口前必须确认后继 Step 已合法收口，预建 PENDING Step 不能伪造 TERMINATED。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-design-precreated-pending",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": design_barrier_id,
                "logical_node_id": "design-review",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 1,
                "side_effect_class": "none",
                "phase": "PENDING",
                "outcome": "NONE",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-design-stale"]),
                "success_predicate_id": "design-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-design-precreated-pending-v1",
                "state_version": 0,
            },
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-pending"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-target-pending-successor",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=planning_barrier_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.PLANNING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_active_authorization_and_lease_and_rolls_back() -> None:
    """目标收口必须撤销全部执行副作用，残留 AVAILABLE 授权或 lease 时三路回滚。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "resource_leases",
            {
                "resource_key": "resource-target-close",
                "owner_executor_id": "executor-planning-contract",
                "fencing_token": 1,
                "control_epoch": 1,
                "acquired_at": "2026-08-17T08:00:00Z",
                "heartbeat_at": "2026-08-17T08:30:00Z",
                "expires_at": "2026-08-17T10:00:00Z",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "execution_authorizations",
            {
                "execution_authorization_id": "auth-target-close",
                "intent_authorization_id": "intent-control-1",
                "plan_revision_id": "plan-control-1",
                "semantic_plan_hash": b"semantic",
                "plan_revision_digest": b"revision",
                "stage_capability_map_version": "stage-capability-map.v1",
                "stage_capability_map_digest": b"stage",
                "node_capability_map_version": "node-capability-map.v1",
                "node_capability_map_digest": b"node",
                "run_id": "run-control-1",
                "step_id": "step-planning-contract",
                "attempt_id": "attempt-planning-contract",
                "node_type": "PLAN",
                "executor_id": "executor-planning-contract",
                "resource_fingerprint": b"resource",
                "capability_scope_digest": b"scope",
                "idempotency_key": b"idempotency",
                "input_bindings": b"bindings",
                "action_capability": "repo.read",
                "action_policy_snapshot_digest": b"policy",
                "fencing_token": 1,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "max_uses": 1,
                "consumption_state": "AVAILABLE",
                "issued_at": "2026-08-17T08:00:00Z",
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
            state_event_id_factory=_ids("state-event-target-side-effect"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-target-active-side-effect",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=planning_barrier_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.PLANNING,
            next_barrier_id=None,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT consumption_state,revoked_at FROM execution_authorizations WHERE execution_authorization_id=?",
            ("auth-target-close",),
        ).fetchone() == ("AVAILABLE", None)
        assert connection.execute(
            "SELECT state_version FROM resource_leases WHERE resource_key=?", ("resource-target-close",)
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_lease_without_authorization_and_rolls_back() -> None:
    """已终止 Attempt 仍持有效 lease 时，即使授权缺失也不能提交 TERMINATED。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_target_close_lease(connection)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-lease-only"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-lease-only",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT state_version FROM resource_leases WHERE resource_key=?", ("resource-target-lease-only",)
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM execution_authorizations").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_authorization_without_lease_and_rolls_back() -> None:
    """独立 AVAILABLE 授权本身也必须阻断目标收口，不能依赖 lease 分支。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_target_close_authorization(connection)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-auth-only"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-auth-only",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT consumption_state,revoked_at FROM execution_authorizations WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("AVAILABLE", None)
        assert connection.execute("SELECT count(*) FROM resource_leases").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_cross_run_authorization_and_rolls_back() -> None:
    """已失效授权只漂移 run_id 时也必须由 lineage 校验 fail closed。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_secondary_run_graph(connection)
        old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
        _insert_historical_target_authorization(
            connection,
            plan_revision_id=old_plan_revision_id,
            step_id=old_step_id,
            attempt_id=old_attempt_id,
            active=False,
        )
        connection.execute(
            "UPDATE execution_authorizations SET run_id=? WHERE execution_authorization_id=?",
            ("run-control-2", "auth-target-auth-only"),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-cross-run-auth"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-cross-run-auth",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute(
            "SELECT observed_state,state_version FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == (
            "QUEUED",
            0,
        )
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT run_id,step_id,attempt_id FROM execution_authorizations WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("run-control-2", old_step_id, old_attempt_id)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_reverse_mismatched_authorization_and_rolls_back() -> None:
    """已失效授权只漂移 attempt 绑定时也必须由 lineage 校验 fail closed。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
        _insert_historical_target_authorization(
            connection,
            plan_revision_id=old_plan_revision_id,
            step_id=old_step_id,
            attempt_id=old_attempt_id,
            active=False,
        )
        connection.execute(
            "UPDATE execution_authorizations SET attempt_id=? WHERE execution_authorization_id=?",
            ("attempt-planning-contract", "auth-target-auth-only"),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-reverse-auth"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-reverse-auth",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute(
            "SELECT observed_state,state_version FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == (
            "QUEUED",
            0,
        )
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT run_id,step_id,attempt_id FROM execution_authorizations WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("run-control-1", old_step_id, "attempt-planning-contract")
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("mutation", _LINEAGE_MUTATIONS, ids=_LINEAGE_MUTATIONS)
@pytest.mark.asyncio
async def test_target_reached_rejects_each_inactive_authorization_lineage_mutation(mutation: str) -> None:
    """失效授权只要任一身份字段漂移，终局也必须拒绝并保持三路状态不变。"""
    connection = _connection()
    try:
        planning_barrier_id = _prepare_inactive_authorization_lineage_mutation(connection, mutation=mutation)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids(f"state-event-target-lineage-{mutation}"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id=f"request-target-lineage-{mutation}",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute(
            "SELECT observed_state,active_barrier_id,state_version FROM runs WHERE run_id=?",
            ("run-control-1",),
        ).fetchone() == ("QUEUED", planning_barrier_id, 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT consumption_state,max_uses,revoked_at FROM execution_authorizations "
            "WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("CONSUMED", 0, "2026-08-17T07:30:00Z")
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_allows_self_consistent_expired_historical_authorization() -> None:
    """同一 Task 旧 PlanRevision 的 CONSUMED/revoked/expired 授权不应阻断合法终局。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
        _insert_historical_target_authorization(
            connection,
            plan_revision_id=old_plan_revision_id,
            step_id=old_step_id,
            attempt_id=old_attempt_id,
            active=False,
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-historical-auth"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        result = await service.commit_passed_barrier(
            _target_close_request(
                planning_barrier_id=planning_barrier_id,
                request_id="request-target-historical-auth",
            )
        )

        assert result.run.observed_state is RunObservedState.TERMINATED
        assert result.task.lifecycle.value == "SUCCEEDED"
        assert connection.execute(
            "SELECT phase,active_barrier_id,observed_state FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == ("PLANNING", None, "TERMINATED")
        assert connection.execute(
            "SELECT consumption_state,max_uses,revoked_at FROM execution_authorizations "
            "WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("CONSUMED", 0, "2026-08-17T07:30:00Z")
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (3,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_active_stale_plan_authorization_and_rolls_back() -> None:
    """旧 PlanRevision 上仍有效的授权必须阻断终局，不能借历史豁免绕过 lineage。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        connection.execute("UPDATE tasks SET target_stage=? WHERE task_id=?", ("DESIGN_APPROVED", "task-control-1"))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
        _insert_historical_target_authorization(
            connection,
            plan_revision_id=old_plan_revision_id,
            step_id=old_step_id,
            attempt_id=old_attempt_id,
            active=True,
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-target-stale-plan-auth"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-stale-plan-auth",
                )
            )
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers WHERE barrier_id=?", (planning_barrier_id,)
        ).fetchone() == (0, 0, 0)
        assert connection.execute(
            "SELECT observed_state,state_version FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT lifecycle,achieved_stage,state_version FROM tasks").fetchone() == (
            "ACTIVE",
            "NONE",
            0,
        )
        assert connection.execute(
            "SELECT consumption_state,max_uses,revoked_at FROM execution_authorizations "
            "WHERE execution_authorization_id=?",
            ("auth-target-auth-only",),
        ).fetchone() == ("AVAILABLE", 1, None)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_requires_first_unpassed_sequence() -> None:
    """当前 barrier 必须是冻结序列首个未通过项，前驱未通过时全事务回滚。"""
    connection = _connection()
    try:
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("predecessor",),
            additional_barriers=(
                ("PLANNING", 1, ("plan",), "planning-next-v1"),
                ("DESIGN_REVIEWING", 2, ("design-review",), "design-approved-v1"),
            ),
        )
        predecessor_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=1)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=2)
        for barrier_id, phase, ordinal, node_ids, predicate_id in (
            (predecessor_id, "PLANNING", 0, ("predecessor",), "planning-approved-v1"),
            (current_id, "PLANNING", 1, ("plan",), "planning-next-v1"),
            (next_id, "DESIGN_REVIEWING", 2, ("design-review",), "design-approved-v1"),
        ):
            _insert(
                connection,
                "phase_barriers",
                {
                    "barrier_id": barrier_id,
                    "run_id": "run-control-1",
                    "plan_revision_id": "plan-control-1",
                    "business_phase": phase,
                    "barrier_ordinal": ordinal,
                    "required_node_set_digest": _selector_digest(list(node_ids)),
                    "settle_timeout_ms": 30_000,
                    "settle_deadline_at": None,
                    "pass_predicate_id": predicate_id,
                    "settled": 0,
                    "passed": 0,
                    "gate_digest": None,
                    "state_version": 0,
                },
            )
        _insert_successful_plan_step(
            connection,
            barrier_id=current_id,
            step_id="step-sequence-current",
            attempt_id="attempt-sequence-current",
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (current_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-sequence-predecessor"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-sequence-predecessor",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=current_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=next_id,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (0, 0, 0), (0, 0, 0)]
        assert connection.execute(
            "SELECT phase,active_barrier_id,state_version FROM runs WHERE run_id=?", ("run-control-1",)
        ).fetchone() == ("PLANNING", current_id, 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_prepassed_successor() -> None:
    """后继 barrier 若被预置 settled/passed，当前推进必须拒绝并回滚。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        connection.execute(
            "UPDATE phase_barriers SET settled=1,passed=1,state_version=1,gate_digest=? WHERE barrier_id=?",
            (SHA_A, next_id),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-sequence-successor"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-sequence-successor",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=current_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=next_id,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (1, 1, 1)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_dependency_on_future_barrier() -> None:
    """当前 barrier 的 dependsOn 不得跨越 wire 序列依赖未来 barrier。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, dependencies_by_node={"plan": ("design-review",)})
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert_successful_design_review_step(
            connection,
            barrier_id=next_id,
            step_id="step-design-review-future-dependency",
            attempt_id="attempt-design-review-future-dependency",
        )
        connection.execute(
            "UPDATE steps SET dependency_hash=? WHERE logical_node_id='plan'",
            (_selector_digest(["design-review"]),),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-future-dependency"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-future-dependency",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=current_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=next_id,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (0, 0, 0)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_preexecuted_successor_step() -> None:
    """后继 barrier 即使仍未 passed，也不得已有终态 Step、Attempt 和副作用 Artifact。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert_successful_design_review_step(
            connection,
            barrier_id=next_id,
            step_id="step-design-review-preexecuted",
            attempt_id="attempt-design-review-preexecuted",
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-preexecuted-successor"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-preexecuted-successor",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=current_id,
            expected_task_state_version=0,
            expected_run_state_version=0,
            expected_barrier_state_version=0,
            next_phase=RunPhase.DESIGN_REVIEWING,
            next_barrier_id=next_id,
            candidate_achieved_stage=AchievedStage.DESIGN_APPROVED,
            gate_digest=SHA_A,
            now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            steps=(),
            milestone_evidence=MilestoneEvidence(True, 0, False, True, 0),
        )
        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(request)
        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (0, 0, 0)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_stale_revision_successor_step() -> None:
    """同 barrier_id 下的旧 PlanRevision 终态 Step 不能逃过 successor 扫描。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            additional_barriers=(("DESIGN_REVIEWING", 1, ("design-review",), "design-approved-v1"),),
            plan_revision_id="plan-stale-1",
            spec_revision=2,
        )
        connection.execute("UPDATE tasks SET active_plan_revision_id='plan-control-1' WHERE task_id='task-control-1'")
        _insert_successful_design_review_step(
            connection,
            barrier_id=next_id,
            step_id="step-design-review-stale-revision",
            attempt_id="attempt-design-review-stale-revision",
            plan_revision_id="plan-stale-1",
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-stale-revision-successor"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await service.commit_passed_barrier(
                _planning_to_design_request(
                    request_id="request-stale-revision-successor",
                    barrier_id=current_id,
                    next_barrier_id=next_id,
                )
            )

        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (0, 0, 0)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("successor_phase", [StepPhase.PENDING.value, StepPhase.READY.value])
async def test_passed_barrier_allows_unstarted_prebuilt_successor_step(successor_phase: str) -> None:
    """后继可预建 PENDING/READY 空壳，但不得已有 Attempt 或副作用。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert(
            connection,
            "steps",
            {
                "step_id": f"step-design-review-{successor_phase.lower()}",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": next_id,
                "logical_node_id": "design-review",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 1,
                "side_effect_class": "none",
                "phase": successor_phase,
                "outcome": StepOutcome.NONE.value,
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest(["artifact-design-stale"]),
                "success_predicate_id": "design-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": f"step-design-review-{successor_phase.lower()}-v1",
                "state_version": 0,
            },
        )
        result = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids(f"state-event-{successor_phase.lower()}"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).commit_passed_barrier(
            _planning_to_design_request(
                request_id=f"request-{successor_phase.lower()}",
                barrier_id=current_id,
                next_barrier_id=next_id,
            )
        )

        assert result.run.phase is RunPhase.DESIGN_REVIEWING
        assert result.run.active_barrier_id == next_id
        assert connection.execute("SELECT settled,passed FROM phase_barriers ORDER BY barrier_ordinal").fetchall() == [
            (1, 1),
            (0, 0),
        ]
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_passed_barrier_rejects_optional_future_node_misattached_to_current_barrier() -> None:
    """同 business phase 的未来 optional node 错挂当前 barrier 时必须全事务回滚。"""
    connection = _connection()
    try:
        planning_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            additional_barriers=(("PLANNING", 1, (), "planning-next-v1"),),
            optional_nodes=(("PLANNING", 1, "node-optional-future"),),
        )
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=1)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": current_id,
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": planning_digest,
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
                "barrier_id": next_id,
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 1,
                "required_node_set_digest": _selector_digest([]),
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-next-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        _insert_successful_plan_step(
            connection,
            barrier_id=current_id,
            step_id="step-planning-optional-future-current",
            attempt_id="attempt-planning-optional-future-current",
        )
        _insert(
            connection,
            "steps",
            {
                "step_id": "step-optional-future-misattached",
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-1",
                "barrier_id": current_id,
                "logical_node_id": "node-optional-future",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 0,
                "side_effect_class": "none",
                "phase": StepPhase.TERMINAL.value,
                "outcome": StepOutcome.SUCCEEDED.value,
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-optional-future-misattached-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (current_id,))

        with pytest.raises(BarrierMilestoneCommitError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-optional-future-misattached"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _planning_to_design_request(
                    request_id="request-optional-future-misattached",
                    barrier_id=current_id,
                    next_barrier_id=next_id,
                    next_phase=RunPhase.PLANNING,
                )
            )

        assert connection.execute(
            "SELECT settled,passed,state_version FROM phase_barriers ORDER BY barrier_ordinal"
        ).fetchall() == [(0, 0, 0), (0, 0, 0)]
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
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
        current_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        next_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=1)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": current_barrier_id,
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
                "barrier_id": next_barrier_id,
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
        connection.execute("UPDATE runs SET active_barrier_id=?", (current_barrier_id,))
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            updated = SqliteWorkflowRepository(unit_of_work).advance_run_phase(
                run_id="run-control-1",
                expected_state_version=0,
                candidate_phase=RunPhase.PLANNING,
                next_barrier_id=next_barrier_id,
            )
            assert updated.phase is RunPhase.PLANNING
            assert updated.active_barrier_id == next_barrier_id
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "implement",
                "business_phase": "IMPLEMENTING",
                "node_type": "IMPLEMENT",
                "required": 1,
                "side_effect_class": "workspace_write",
                "phase": "RUNNING",
                "outcome": "NONE",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "node-current",
                "business_phase": "DESIGN_REVIEWING",
                "node_type": "DESIGN_REVIEW",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-stale-revision-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-stale-revision"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-stale-revision",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=barrier_id,
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "node-current",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-required-flag-drift-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-required-flag-drift"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-required-flag-drift",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=barrier_id,
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "implement",
                "business_phase": "IMPLEMENTING",
                "node_type": "IMPLEMENT",
                "required": 1,
                "side_effect_class": "workspace_write",
                "phase": step_phase,
                "outcome": "NONE",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authority-empty"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-barrier-authority-empty",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=barrier_id,
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "node-a",
                "business_phase": "PLANNING",
                "node_type": "PLAN",
                "required": 0,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "SUCCEEDED",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-node-a-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-required-node-set"),
        )
        request = BarrierMilestoneRequest(
            request_id="request-required-node-set",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=barrier_id,
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "node-unknown",
                "business_phase": "PLANNING",
                "node_type": "RECONCILE_TARGET",
                "required": 1,
                "side_effect_class": "none",
                "phase": "RECONCILING",
                "outcome": "UNKNOWN_REMOTE_STATE",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
                "success_predicate_id": "planning-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-unknown-v1",
                "state_version": 0,
            },
        )
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
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
            barrier_id=barrier_id,
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
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
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
                "barrier_id": barrier_id,
                "logical_node_id": "node-unknown-attempt",
                "business_phase": "PLANNING",
                "node_type": "RECONCILE_TARGET",
                "required": 1,
                "side_effect_class": "none",
                "phase": "TERMINAL",
                "outcome": "NONE",
                "dependency_hash": _selector_digest([]),
                "required_artifacts_digest": _selector_digest([]),
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
        connection.execute("UPDATE runs SET active_barrier_id=?", (barrier_id,))
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-unknown-attempt"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        request = BarrierMilestoneRequest(
            request_id="request-unknown-attempt",
            task_id="task-control-1",
            run_id="run-control-1",
            barrier_id=barrier_id,
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
