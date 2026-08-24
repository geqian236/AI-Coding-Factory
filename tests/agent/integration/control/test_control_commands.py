"""控制命令 requestId、CAS、追加 receipt 与 RESUME 语义集成测试。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
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
from factory_agent.domain import authorization as authorization_domain
from factory_agent.domain.control import ControlCommandReceiptPhase, ControlCommandType
from factory_agent.domain.workflow import (
    AchievedStage,
    AttemptOutcome,
    AttemptPhase,
    PhaseBarrier,
    RunObservedState,
    RunPhase,
    StepOutcome,
    StepPhase,
)
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import plan_revision_digest, semantic_plan_hash
from factory_agent.state_machine.barriers import BarrierStepFacts, evaluate_barrier
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


class _RecordingCoordinator:
    """记录应用 selector 是否越过校验边界进入事务 coordinator。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute[ResultT](
        self,
        *,
        operation: str,
        context: Mapping[str, object],
        command: Callable[[Any], ResultT],
    ) -> ResultT:
        self.calls += 1
        raise AssertionError(f"invalid selector reached coordinator: {operation}")


class _RecordingClock:
    """按序返回权威时刻并记录调用，直接验证事务入口是否只取时一次。"""

    def __init__(self, *values: object) -> None:
        if not values:
            raise AssertionError("recording clock requires at least one value")
        self._values = values
        self.calls: list[object] = []

    def __call__(self) -> datetime:
        value = self._values[min(len(self.calls), len(self._values) - 1)]
        self.calls.append(value)
        return value  # type: ignore[return-value]


class _InvalidOffsetTimezone(tzinfo):
    """返回 RFC 不允许的 offset，验证权威时钟拒绝无效 aware datetime。"""

    def utcoffset(self, value: datetime | None) -> timedelta | None:
        return timedelta(hours=24)

    def dst(self, value: datetime | None) -> timedelta | None:
        return None

    def tzname(self, value: datetime | None) -> str | None:
        return "INVALID"


class _BrokenOffsetTimezone(tzinfo):
    """让 utcoffset 转换抛错，验证异常不会越过应用时钟边界。"""

    def utcoffset(self, value: datetime | None) -> timedelta | None:
        raise ValueError("broken test offset")

    def dst(self, value: datetime | None) -> timedelta | None:
        return None

    def tzname(self, value: datetime | None) -> str | None:
        return "BROKEN"


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


def _insert_control_command_ack(
    connection: sqlite3.Connection,
    *,
    command_id: str,
    command_seq: int,
    acknowledged_attempt_id: str | None,
    run_id: str = "run-control-1",
    command_type: ControlCommandType = ControlCommandType.SOFT_PAUSE,
    include_ack: bool = True,
    receipt_attempt_id: str | None = None,
) -> None:
    """直接插入 command 与初始 ACK，供 drain lineage 单变量 fixture 复用。"""
    issued_at = "2026-08-14T08:00:00Z"
    _insert(
        connection,
        "control_commands",
        {
            "command_id": command_id,
            "run_id": run_id,
            "command_seq": command_seq,
            "request_id": f"request-{command_id}",
            "command_type": command_type.value,
            "actor_id": "actor-control-lineage",
            "expected_state_version": max(command_seq - 1, 0),
            "accepted_state_version": command_seq,
            "issued_at": issued_at,
            "acknowledged_attempt_id": acknowledged_attempt_id,
            "reason_digest": SHA_A,
        },
    )
    if include_ack:
        _insert(
            connection,
            "control_command_receipt_events",
            {
                "command_receipt_event_id": f"receipt-{command_id}",
                "command_id": command_id,
                "receipt_seq": 0,
                "phase": ControlCommandReceiptPhase.ACKNOWLEDGED.value,
                "attempt_id": acknowledged_attempt_id if receipt_attempt_id is None else receipt_attempt_id,
                "state_event_id": None,
                "evidence_digest": None,
                "previous_receipt_digest": None,
                "created_at": issued_at,
            },
        )


def _authorization_snapshot(schema_id: str, marker: str, *, digest: str | None = None) -> dict[str, str]:
    """构造真实授权合同引用；指定 digest 时用于闭合 PlanRevision 权威摘要。"""
    return {
        "artifactId": f"artifact-control-auth-{marker}",
        "schemaId": schema_id,
        "schemaVersion": "1",
        "digest": digest or f"sha256:{marker * 64}",
    }


def _canonical_intent_authorization_record(
    *,
    authorization_id: str = "intent-control-1",
    task_id: str = "task-control-1",
    project_id: str = "project-control-1",
    target_stage: str = "CODEX_APPROVED",
    stage_capability_map_version: str = "stage-capability-map.v1",
    issued_at: str = "2026-08-17T07:00:00Z",
    expires_at: str = "2026-08-17T10:00:00Z",
    revoked_at: str | None = None,
    revoke_reason: str | None = None,
) -> dict[str, object]:
    """生成可由 UoW canonical getter 完整复验的 IntentAuthorization 持久投影。"""
    contract: dict[str, object] = {
        "intentAuthorizationId": authorization_id,
        "taskId": task_id,
        "userId": "user-control-1",
        "requirementDigest": _authorization_snapshot("factory.authorization.intent.requirement.v1", "1"),
        "projectId": project_id,
        "repositoryId": "repo-control-1",
        "repositoryBindingDigest": _authorization_snapshot("factory.authorization.intent.repository-binding.v1", "2"),
        "baselineDigest": _authorization_snapshot("factory.authorization.intent.baseline.v1", "3"),
        "targetStage": target_stage,
        "stageCapabilityMapVersion": stage_capability_map_version,
        "allowedCapabilitySetDigest": _authorization_snapshot(
            "factory.authorization.intent.allowed-capability-set.v1", "4"
        ),
        "targetBindingDigest": _authorization_snapshot("factory.authorization.intent.target-binding.v1", "5"),
        "riskCeiling": "medium",
        "estimatedCostAlertDigest": _authorization_snapshot(
            "factory.authorization.intent.estimated-cost-alert.v1", "6"
        ),
        "autonomousExecutionBudgetMs": 60_000,
        "repairLoopLimit": 1,
        "autoReplanLimit": 1,
        "attemptLimit": 3,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "revokedAt": revoked_at,
        "revokeReason": revoke_reason,
    }
    parsed = authorization_domain.parse_intent_authorization(contract)
    return {
        "intent_authorization_id": contract["intentAuthorizationId"],
        "task_id": contract["taskId"],
        "user_id": contract["userId"],
        "requirement_digest": canonicalize(contract["requirementDigest"]),
        "project_id": contract["projectId"],
        "repository_id": contract["repositoryId"],
        "repository_binding_digest": canonicalize(contract["repositoryBindingDigest"]),
        "baseline_digest": canonicalize(contract["baselineDigest"]),
        "target_stage": contract["targetStage"],
        "stage_capability_map_version": contract["stageCapabilityMapVersion"],
        "allowed_capability_set_digest": canonicalize(contract["allowedCapabilitySetDigest"]),
        "target_binding_digest": canonicalize(contract["targetBindingDigest"]),
        "risk_ceiling": contract["riskCeiling"],
        "estimated_cost_alert_digest": canonicalize(contract["estimatedCostAlertDigest"]),
        "autonomous_execution_budget_ms": contract["autonomousExecutionBudgetMs"],
        "repair_loop_limit": contract["repairLoopLimit"],
        "auto_replan_limit": contract["autoReplanLimit"],
        "attempt_limit": contract["attemptLimit"],
        "issued_at": contract["issuedAt"],
        "expires_at": contract["expiresAt"],
        "revoked_at": contract["revokedAt"],
        "revoke_reason": contract["revokeReason"],
        "contract_schema_id": parsed.schema_id,
        "contract_schema_version": parsed.schema_version,
        "canonical_contract": parsed.canonical_bytes,
        "canonical_contract_digest": canonicalize({"digest": parsed.contract_digest}),
    }


def _replace_canonical_intent_authorization(
    connection: sqlite3.Connection,
    **overrides: object,
) -> None:
    """同步替换 Intent canonical bytes 与全部投影，确保反例只漂移跨实体条件。"""
    record = _canonical_intent_authorization_record(**overrides)  # type: ignore[arg-type]
    columns = tuple(name for name in record if name != "intent_authorization_id")
    assignments = ",".join(f'"{name}"=?' for name in columns)
    connection.execute(
        f'UPDATE "intent_authorizations" SET {assignments} WHERE intent_authorization_id=?',  # noqa: S608
        (*[record[name] for name in columns], record["intent_authorization_id"]),
    )


def _align_target_stage_authority(connection: sqlite3.Connection, target_stage: str) -> None:
    """同步 Task 与 canonical Intent target，避免终局测试被无关 target 漂移遮挡。"""
    connection.execute(
        "UPDATE tasks SET target_stage=? WHERE task_id='task-control-1'",
        (target_stage,),
    )
    _replace_canonical_intent_authorization(connection, target_stage=target_stage)


def _canonical_execution_authorization_record(
    connection: sqlite3.Connection,
    *,
    authorization_id: str = "auth-authority-valid",
    intent_authorization_id: str = "intent-control-1",
    plan_revision_id: str = "plan-control-1",
    step_id: str = "step-authority-valid",
    attempt_id: str = "attempt-authority-valid",
    node_type: str = "IMPLEMENT",
    executor_id: str = "executor-authority-valid",
    action_capability: str = "worktree.write",
    input_bindings: Mapping[str, object] | None = None,
    fencing_token: int = 7,
    control_epoch: int = 9,
    accepted_control_command_seq: int = 0,
    semantic_plan_digest: str | None = None,
    plan_revision_digest_value: str | None = None,
    stage_capability_map_version: str | None = None,
    node_capability_map_version: str | None = None,
    run_id: str = "run-control-1",
    idempotency_marker: str = "d",
    max_uses: int = 1,
    consumption_state: str = "AVAILABLE",
    issued_at: str = "2026-08-17T08:59:00Z",
    expires_at: str = "2026-08-17T10:00:00Z",
    revoked_at: str | None = None,
    revoke_reason: str | None = None,
) -> dict[str, object]:
    """绑定当前 Plan/Run/Step/Attempt，生成真实 canonical ExecutionAuthorization。"""
    plan_row = connection.execute(
        "SELECT semantic_plan_hash,plan_revision_digest,stage_capability_map_version,"
        "node_capability_map_version FROM plan_revisions WHERE plan_revision_id=?",
        (plan_revision_id,),
    ).fetchone()
    assert plan_row is not None
    bindings = {"baseSha": "a" * 40} if input_bindings is None else dict(input_bindings)
    contract: dict[str, object] = {
        "executionAuthorizationId": authorization_id,
        "intentAuthorizationId": intent_authorization_id,
        "planRevisionId": plan_revision_id,
        "semanticPlanHash": _authorization_snapshot(
            "factory.authorization.execution.semantic-plan.v1",
            "7",
            digest=semantic_plan_digest or str(plan_row[0]),
        ),
        "planRevisionDigest": _authorization_snapshot(
            "factory.authorization.execution.plan-revision.v1",
            "8",
            digest=plan_revision_digest_value or str(plan_row[1]),
        ),
        "stageCapabilityMapVersion": stage_capability_map_version or str(plan_row[2]),
        "stageCapabilityMapDigest": _authorization_snapshot(
            "factory.authorization.execution.stage-capability-map.v1", "9"
        ),
        "nodeCapabilityMapVersion": node_capability_map_version or str(plan_row[3]),
        "nodeCapabilityMapDigest": _authorization_snapshot(
            "factory.authorization.execution.node-capability-map.v1", "a"
        ),
        "runId": run_id,
        "stepId": step_id,
        "attemptId": attempt_id,
        "nodeType": node_type,
        "executorId": executor_id,
        "resourceFingerprint": _authorization_snapshot("factory.authorization.execution.resource-fingerprint.v1", "b"),
        "capabilityScopeDigest": _authorization_snapshot("factory.authorization.execution.capability-scope.v1", "c"),
        "idempotencyKey": _authorization_snapshot(
            "factory.authorization.execution.idempotency-key.v1", idempotency_marker
        ),
        "inputBindings": bindings,
        "actionCapability": action_capability,
        "actionPolicySnapshotDigest": _authorization_snapshot("factory.authorization.execution.action-policy.v1", "e"),
        "fencingToken": fencing_token,
        "controlEpoch": control_epoch,
        "acceptedControlCommandSeq": accepted_control_command_seq,
        "maxUses": max_uses,
        "consumptionState": consumption_state,
        "issuedAt": issued_at,
        "expiresAt": expires_at,
        "revokedAt": revoked_at,
        "revokeReason": revoke_reason,
    }
    parsed = authorization_domain.parse_execution_authorization(contract)
    return {
        "execution_authorization_id": contract["executionAuthorizationId"],
        "intent_authorization_id": contract["intentAuthorizationId"],
        "plan_revision_id": contract["planRevisionId"],
        "semantic_plan_hash": canonicalize(contract["semanticPlanHash"]),
        "plan_revision_digest": canonicalize(contract["planRevisionDigest"]),
        "stage_capability_map_version": contract["stageCapabilityMapVersion"],
        "stage_capability_map_digest": canonicalize(contract["stageCapabilityMapDigest"]),
        "node_capability_map_version": contract["nodeCapabilityMapVersion"],
        "node_capability_map_digest": canonicalize(contract["nodeCapabilityMapDigest"]),
        "run_id": contract["runId"],
        "step_id": contract["stepId"],
        "attempt_id": contract["attemptId"],
        "node_type": contract["nodeType"],
        "executor_id": contract["executorId"],
        "resource_fingerprint": canonicalize(contract["resourceFingerprint"]),
        "capability_scope_digest": canonicalize(contract["capabilityScopeDigest"]),
        "idempotency_key": canonicalize(contract["idempotencyKey"]),
        "input_bindings": canonicalize(contract["inputBindings"]),
        "action_capability": contract["actionCapability"],
        "action_policy_snapshot_digest": canonicalize(contract["actionPolicySnapshotDigest"]),
        "fencing_token": contract["fencingToken"],
        "control_epoch": contract["controlEpoch"],
        "accepted_control_command_seq": contract["acceptedControlCommandSeq"],
        "max_uses": contract["maxUses"],
        "consumption_state": contract["consumptionState"],
        "issued_at": contract["issuedAt"],
        "expires_at": contract["expiresAt"],
        "revoked_at": contract["revokedAt"],
        "revoke_reason": contract["revokeReason"],
        "contract_schema_id": parsed.schema_id,
        "contract_schema_version": parsed.schema_version,
        "canonical_contract": parsed.canonical_bytes,
        "canonical_contract_digest": canonicalize({"digest": parsed.contract_digest}),
    }


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
    task_id: str = "task-control-1",
    intent_authorization_id: str = "intent-control-1",
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
        "taskId": task_id,
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
        "intentAuthorizationId": intent_authorization_id,
        "semanticPlanHash": SHA_A,
        "planRevisionDigest": SHA_A,
        "createdAt": "2026-08-17T08:00:00Z",
    }
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision: dict[str, object] = {
        "planRevisionId": plan_revision_id,
        "taskId": task_id,
        "specRevision": spec_revision,
        "intentAuthorizationId": intent_authorization_id,
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
            "task_id": task_id,
            "spec_revision": spec_revision,
            "parent_revision_id": None,
            "intent_authorization_id": intent_authorization_id,
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
        (plan_revision_id, task_id),
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
    optional_nodes: tuple[tuple[str, int, str], ...] = (),
) -> None:
    """插入可通过 planning gate 的完整图，并允许单独污染 next barrier 持久 selector。"""
    planning_digest = _insert_plan_revision_for_barrier(
        connection,
        required_node_ids=("plan",),
        additional_barriers=(("DESIGN_REVIEWING", 1, ("design-review",), "design-approved-v1"),),
        run_spec_target_stage=run_spec_target_stage,
        dependencies_by_node=dependencies_by_node,
        optional_nodes=optional_nodes,
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
            "drain_state": "NONE",
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
    """插入可被 canonical getter 复验的 AVAILABLE 授权，不依赖 lease 查询。"""
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(
            connection,
            authorization_id="auth-target-auth-only",
            step_id="step-planning-contract",
            attempt_id="attempt-planning-contract",
            node_type="PLAN",
            executor_id="executor-planning-contract",
            action_capability="repo.read",
            fencing_token=1,
            control_epoch=1,
            accepted_control_command_seq=0,
            issued_at="2026-08-17T08:00:00Z",
            expires_at="2026-08-17T10:00:00Z",
        ),
    )


def _insert_intent_authorization_copy(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    intent_id: str,
    task_id: str,
) -> None:
    """从来源 selector 重建 canonical Intent，避免复制后留下伪 canonical bytes。"""
    source = connection.execute(
        "SELECT project_id,target_stage,stage_capability_map_version,issued_at,expires_at,"
        "revoked_at,revoke_reason FROM intent_authorizations WHERE intent_authorization_id=?",
        (source_id,),
    ).fetchone()
    assert source is not None
    _insert(
        connection,
        "intent_authorizations",
        _canonical_intent_authorization_record(
            authorization_id=intent_id,
            task_id=task_id,
            project_id=str(source[0]),
            target_stage=str(source[1]),
            stage_capability_map_version=str(source[2]),
            issued_at=str(source[3]),
            expires_at=str(source[4]),
            revoked_at=None if source[5] is None else str(source[5]),
            revoke_reason=None if source[6] is None else str(source[6]),
        ),
    )


def _insert_second_intent_authorization(connection: sqlite3.Connection) -> None:
    """复制当前 Task 的意图授权，供只漂移 execution authorization intent 绑定的反例使用。"""
    _insert_intent_authorization_copy(
        connection,
        source_id="intent-control-1",
        intent_id="intent-control-2",
        task_id="task-control-1",
    )


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


def _prepare_target_close_expiry_boundary(connection: sqlite3.Connection) -> str:
    """构造只由 matching lease 的严格 expiry 边界决定能否收口的完整事实图。"""
    _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
    _align_target_stage_authority(connection, "DESIGN_APPROVED")
    planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    _insert_target_close_lease(connection)
    connection.execute(
        "UPDATE resource_leases SET acquired_at='2026-08-17T08:00:00Z',"
        "heartbeat_at='2026-08-17T08:30:00Z',expires_at='2026-08-17T09:00:00Z' "
        "WHERE resource_key='resource-target-lease-only'"
    )
    return planning_barrier_id


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
            "drain_state": "NONE",
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
            "drain_state": "NONE",
            "started_at": "2026-08-17T06:00:00Z",
            "ended_at": "2026-08-17T06:30:00Z",
            "state_version": 0,
        },
    )
    return "plan-control-old", "step-planning-historical", "attempt-planning-historical"


def _insert_invalid_optional_planning_step(connection: sqlite3.Connection) -> str:
    """插入当前 Plan 声明但不属于 requiredNodeIds 的坏 Step，避免测试依赖缺失 node。"""
    step_id = "step-planning-optional-invalid"
    planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    _insert(
        connection,
        "steps",
        {
            "step_id": step_id,
            "run_id": "run-control-1",
            "plan_revision_id": "plan-control-1",
            "barrier_id": planning_barrier_id,
            "logical_node_id": "optional-plan",
            "business_phase": "PLANNING",
            "node_type": "PLAN",
            "required": 0,
            "side_effect_class": "none",
            "phase": "TERMINAL",
            "outcome": "NONE",
            "dependency_hash": _selector_digest([]),
            "required_artifacts_digest": _selector_digest([]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-planning-optional-invalid-v1",
            "state_version": 0,
        },
    )
    return step_id


def _corrupt_persisted_step_phase(connection: sqlite3.Connection, *, step_id: str) -> None:
    """仅在测试事务中绕过 CHECK 制造 raw Step enum 损坏，并立即恢复约束开关。"""
    previous = int(connection.execute("PRAGMA ignore_check_constraints").fetchone()[0])
    connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        connection.execute("UPDATE steps SET phase='CORRUPTED' WHERE step_id=?", (step_id,))
    finally:
        connection.execute(f"PRAGMA ignore_check_constraints={previous}")  # noqa: S608


def _insert_historical_target_authorization(
    connection: sqlite3.Connection,
    *,
    plan_revision_id: str,
    step_id: str,
    attempt_id: str,
    active: bool,
) -> None:
    """直接生成绑定旧 Step/Attempt 的 canonical 历史授权，避免伪投影遮挡终局校验。"""
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(
            connection,
            authorization_id="auth-target-auth-only",
            plan_revision_id=plan_revision_id,
            step_id=step_id,
            attempt_id=attempt_id,
            node_type="PLAN",
            executor_id="executor-planning-historical",
            action_capability="repo.read",
            fencing_token=11,
            control_epoch=4,
            accepted_control_command_seq=0,
            max_uses=1 if active else 0,
            consumption_state="AVAILABLE" if active else "CONSUMED",
            issued_at="2026-08-17T08:00:00Z" if active else "2026-08-17T06:00:00Z",
            expires_at="2026-08-17T10:00:00Z" if active else "2026-08-17T07:00:00Z",
            revoked_at=None if active else "2026-08-17T06:30:00Z",
            revoke_reason=None if active else "superseded-by-replan",
        ),
    )


def _insert_other_task_plan_graph(connection: sqlite3.Connection) -> None:
    """插入 canonical 的另一 Task Plan，并仅让该 Plan 与当前 Run 的 Task 不一致。"""
    _insert(
        connection,
        "tasks",
        {
            "task_id": "task-control-2",
            "project_id": "project-control-1",
            "lifecycle": "ACTIVE",
            "target_stage": "DESIGN_APPROVED",
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
    plan_cursor = connection.execute("SELECT * FROM plan_revisions WHERE plan_revision_id=?", ("plan-control-1",))
    plan_row = plan_cursor.fetchone()
    assert plan_row is not None
    plan_values = dict(zip((item[0] for item in plan_cursor.description or ()), plan_row, strict=True))
    run_spec = json.loads(bytes(plan_values["canonical_run_spec"]).decode("utf-8"))
    revision = json.loads(bytes(plan_values["canonical_plan_revision"]).decode("utf-8"))
    run_spec["taskId"] = "task-control-2"
    revision["planRevisionId"] = "plan-control-other-task"
    revision["taskId"] = "task-control-2"
    for document in (run_spec, revision):
        node_collection = document["workPlan"]["nodes"] if document is run_spec else document["nodes"]
        plan_node = next(node for node in node_collection if node["logicalNodeId"] == "plan")
        plan_node["requiredArtifacts"] = ["artifact-planning-other-task"]
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision["semanticPlanHash"] = run_spec["semanticPlanHash"]
    revision["planRevisionDigest"] = plan_revision_digest(revision)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    canonical_run_spec = canonicalize(run_spec)
    canonical_revision = canonicalize(revision)
    other_plan_revision_id = "plan-control-other-task"
    other_plan_digest = str(revision["planRevisionDigest"])
    plan_values.update(
        {
            "plan_revision_id": other_plan_revision_id,
            "task_id": "task-control-2",
            "intent_authorization_id": "intent-control-1",
            "semantic_plan_hash": revision["semanticPlanHash"],
            "plan_revision_digest": other_plan_digest,
            "canonical_run_spec": canonical_run_spec,
            "canonical_plan_revision": canonical_revision,
        }
    )
    _insert(connection, "plan_revisions", plan_values)
    connection.execute(
        "UPDATE tasks SET active_plan_revision_id=? WHERE task_id=?",
        (other_plan_revision_id, "task-control-2"),
    )
    raw_barrier = revision["barriers"][0]
    assert isinstance(raw_barrier, Mapping)
    required_node_ids = list(raw_barrier["requiredNodeIds"])
    other_barrier_id = PhaseBarrier.from_plan_barrier(
        run_id="run-control-1",
        plan_revision_id=other_plan_revision_id,
        plan_revision_digest=other_plan_digest,
        plan_barrier=raw_barrier,
    ).barrier_id
    _insert(
        connection,
        "phase_barriers",
        {
            "barrier_id": other_barrier_id,
            "run_id": "run-control-1",
            "plan_revision_id": other_plan_revision_id,
            "business_phase": raw_barrier["businessPhase"],
            "barrier_ordinal": raw_barrier["barrierOrdinal"],
            "required_node_set_digest": _selector_digest(required_node_ids),
            "settle_timeout_ms": raw_barrier["settleTimeoutMs"],
            "settle_deadline_at": None,
            "pass_predicate_id": raw_barrier["passPredicateId"],
            "settled": 0,
            "passed": 0,
            "gate_digest": None,
            "state_version": 0,
        },
    )
    plan_node = next(node for node in revision["nodes"] if node["logicalNodeId"] == "plan")
    other_step_id = "step-planning-other-task"
    other_attempt_id = "attempt-planning-other-task"
    _insert(
        connection,
        "steps",
        {
            "step_id": other_step_id,
            "run_id": "run-control-1",
            "plan_revision_id": other_plan_revision_id,
            "barrier_id": other_barrier_id,
            "logical_node_id": "plan",
            "business_phase": plan_node["businessPhase"],
            "node_type": plan_node["nodeType"],
            "required": 1,
            "side_effect_class": plan_node["sideEffectClass"],
            "phase": "TERMINAL",
            "outcome": "SUCCEEDED",
            "dependency_hash": _selector_digest(list(plan_node["dependsOn"])),
            "required_artifacts_digest": _selector_digest(list(plan_node["requiredArtifacts"])),
            "success_predicate_id": plan_node["successPredicateId"],
            "timeout_ms": plan_node["timeoutMs"],
            "retry_policy_id": plan_node["retryPolicyId"],
            "idempotency_key": "step-planning-other-task-v1",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "attempts",
        {
            "attempt_id": other_attempt_id,
            "step_id": other_step_id,
            "supersedes_attempt_id": None,
            "phase": "TERMINATED",
            "outcome": "SUCCEEDED",
            "executor_id": "executor-planning-other-task",
            "process_session_id": "process-planning-other-task",
            "pid": 4567,
            "process_start_time": "2026-08-17T06:00:00Z",
            "job_object_id": "job-planning-other-task",
            "wsl_distro": None,
            "container_id": None,
            "image_digest": None,
            "exit_code": 0,
            "termination_reason": None,
            "fencing_token": 13,
            "control_epoch": 5,
            "accepted_control_command_seq": 0,
            "interrupt_command_id": None,
            "drain_state": "NONE",
            "started_at": "2026-08-17T06:00:00Z",
            "ended_at": "2026-08-17T06:30:00Z",
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "artifacts",
        {
            "artifact_id": "artifact-planning-other-task",
            "producer_attempt_id": other_attempt_id,
            "media_type": "text/plain",
            "confidentiality": "INTERNAL",
            "commit_state": "COMMITTED",
            "storage_path": "artifact-planning-other-task",
            "size_bytes": 1,
            "digest": SHA_A,
            "created_at": "2026-08-17T06:30:00Z",
        },
    )
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(
            connection,
            authorization_id="auth-target-auth-only",
            plan_revision_id=other_plan_revision_id,
            step_id=other_step_id,
            attempt_id=other_attempt_id,
            node_type="PLAN",
            executor_id="executor-planning-other-task",
            action_capability="repo.read",
            fencing_token=13,
            control_epoch=5,
            accepted_control_command_seq=0,
            max_uses=0,
            consumption_state="CONSUMED",
            issued_at="2026-08-17T06:00:00Z",
            expires_at="2026-08-17T07:00:00Z",
            revoked_at="2026-08-17T06:30:00Z",
            revoke_reason="superseded-by-task-lineage-probe",
        ),
    )


_LINEAGE_MUTATIONS = (
    "run_id",
    "step_attempt",
    "step_run",
    "plan_revision",
    "plan_task",
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
    _align_target_stage_authority(connection, "DESIGN_APPROVED")
    planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    if mutation in {"run_id", "step_run"}:
        _insert_secondary_run_graph(connection)
    if mutation == "plan_task":
        _insert_other_task_plan_graph(connection)
        return planning_barrier_id
    old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
    options: dict[str, object] = {
        "authorization_id": "auth-target-auth-only",
        "plan_revision_id": old_plan_revision_id,
        "step_id": old_step_id,
        "attempt_id": old_attempt_id,
        "node_type": "PLAN",
        "executor_id": "executor-planning-historical",
        "action_capability": "repo.read",
        "fencing_token": 11,
        "control_epoch": 4,
        "accepted_control_command_seq": 0,
        "max_uses": 0,
        "consumption_state": "CONSUMED",
        "issued_at": "2026-08-17T06:00:00Z",
        "expires_at": "2026-08-17T07:00:00Z",
        "revoked_at": "2026-08-17T06:30:00Z",
        "revoke_reason": "superseded-by-replan",
    }
    if mutation == "run_id":
        options["run_id"] = "run-control-2"
    elif mutation == "step_attempt":
        options.update(
            {
                "attempt_id": "attempt-planning-contract",
                "executor_id": "executor-planning-contract",
                "fencing_token": 1,
                "control_epoch": 1,
            }
        )
    elif mutation == "step_run":
        connection.execute("UPDATE steps SET run_id=? WHERE step_id=?", ("run-control-2", old_step_id))
    elif mutation == "plan_revision":
        options["plan_revision_id"] = "plan-control-1"
    elif mutation == "intent_authorization":
        _insert_second_intent_authorization(connection)
        options["intent_authorization_id"] = "intent-control-2"
    elif mutation == "semantic_plan_hash":
        options["semantic_plan_digest"] = "sha256:" + "b" * 64
    elif mutation == "plan_revision_digest":
        options["plan_revision_digest_value"] = "sha256:" + "b" * 64
    elif mutation == "node_type":
        options["node_type"] = "DESIGN_REVIEW"
    elif mutation == "executor_id":
        options["executor_id"] = "executor-planning-contract"
    elif mutation == "fencing_token":
        options["fencing_token"] = 1
    elif mutation == "control_epoch":
        options["control_epoch"] = 1
    elif mutation == "control_seq":
        options["accepted_control_command_seq"] = 1
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(connection, **options),  # type: ignore[arg-type]
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
    _insert(connection, "intent_authorizations", _canonical_intent_authorization_record())
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


def _transaction_projection_snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    """冻结状态、版本、事件、命令与 receipt，用于证明拒绝路径没有留下部分事务。"""
    queries = {
        "tasks": "SELECT * FROM tasks ORDER BY task_id",
        "runs": "SELECT * FROM runs ORDER BY run_id",
        "steps": "SELECT * FROM steps ORDER BY step_id",
        "attempts": "SELECT * FROM attempts ORDER BY attempt_id",
        "barriers": "SELECT * FROM phase_barriers ORDER BY barrier_id",
        "events": "SELECT * FROM authoritative_state_events ORDER BY state_event_id",
        "commands": "SELECT * FROM control_commands ORDER BY command_id",
        "control_receipts": "SELECT * FROM control_command_receipt_events ORDER BY command_receipt_event_id",
        "action_receipts": "SELECT * FROM action_receipt_events ORDER BY receipt_event_id",
    }
    return {name: connection.execute(sql).fetchall() for name, sql in queries.items()}


def _service(connection: sqlite3.Connection) -> ControlService:
    """构造注入稳定时钟和 identity 的真实服务。"""
    return ControlService(
        coordinator=_SqliteCoordinator(connection),
        now=lambda: datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        command_id_factory=_ids("command"),
        receipt_id_factory=_ids("receipt"),
        state_event_id_factory=_ids("state-event"),
    )


_TRANSITION_SELECTOR_CASES: dict[str, dict[str, object]] = {
    "transition_control_plane_observed": {
        "run_id": "run-control-1",
        "expected_state_version": 0,
        "candidate": RunObservedState.BLOCKED,
        "request_id": "request-selector-1",
    },
    "transition_executor_observed": {
        "run_id": "run-control-1",
        "attempt_id": "attempt-control-1",
        "expected_state_version": 0,
        "executor_id": "executor-control-1",
        "fencing_token": 7,
        "control_epoch": 9,
        "request_id": "request-selector-1",
    },
    "transition_executor_step_state": {
        "request_id": "request-selector-1",
        "run_id": "run-control-1",
        "step_id": "step-control-1",
        "attempt_id": "attempt-control-1",
        "expected_run_state_version": 0,
        "expected_step_state_version": 0,
        "executor_id": "executor-control-1",
        "fencing_token": 7,
        "control_epoch": 9,
        "candidate": StepPhase.RECONCILING,
    },
    "transition_executor_attempt_state": {
        "request_id": "request-selector-1",
        "run_id": "run-control-1",
        "attempt_id": "attempt-control-1",
        "expected_run_state_version": 0,
        "expected_attempt_state_version": 0,
        "executor_id": "executor-control-1",
        "fencing_token": 7,
        "control_epoch": 9,
        "candidate": AttemptPhase.RECONCILING,
    },
    "terminate_executor_attempt": {
        "request_id": "request-selector-1",
        "run_id": "run-control-1",
        "attempt_id": "attempt-control-1",
        "expected_run_state_version": 0,
        "expected_attempt_state_version": 0,
        "executor_id": "executor-control-1",
        "fencing_token": 7,
        "control_epoch": 9,
        "outcome": AttemptOutcome.SUCCEEDED,
        "ended_at": "2026-08-14T08:00:00+00:00",
        "termination_reason": None,
    },
}
_TRANSITION_ID_FIELDS = {
    "transition_control_plane_observed": ("request_id", "run_id"),
    "transition_executor_observed": ("request_id", "run_id", "attempt_id", "executor_id"),
    "transition_executor_step_state": (
        "request_id",
        "run_id",
        "step_id",
        "attempt_id",
        "executor_id",
    ),
    "transition_executor_attempt_state": ("request_id", "run_id", "attempt_id", "executor_id"),
    "terminate_executor_attempt": ("request_id", "run_id", "attempt_id", "executor_id"),
}
_TRANSITION_NUMBER_FIELDS = {
    "transition_control_plane_observed": ("expected_state_version",),
    "transition_executor_observed": ("expected_state_version", "fencing_token", "control_epoch"),
    "transition_executor_step_state": (
        "expected_run_state_version",
        "expected_step_state_version",
        "fencing_token",
        "control_epoch",
    ),
    "transition_executor_attempt_state": (
        "expected_run_state_version",
        "expected_attempt_state_version",
        "fencing_token",
        "control_epoch",
    ),
    "terminate_executor_attempt": (
        "expected_run_state_version",
        "expected_attempt_state_version",
        "fencing_token",
        "control_epoch",
    ),
}


async def _assert_transition_selector_rejected(
    api_name: str,
    field: str,
    invalid: object,
) -> None:
    """调用真实 application API，并证明非法 selector 未进入 coordinator。"""
    coordinator = _RecordingCoordinator()
    service = TransitionService(
        coordinator=coordinator,
        state_event_id_factory=lambda: "unused-state-event",
    )
    arguments = dict(_TRANSITION_SELECTOR_CASES[api_name])
    arguments[field] = invalid

    with pytest.raises(FactoryError) as caught:
        await getattr(service, api_name)(**arguments)

    assert caught.value.error_code == "STATE_PREDICATE_REJECTED"
    assert coordinator.calls == 0


@pytest.mark.parametrize(
    ("api_name", "field", "invalid"),
    [
        (api_name, field, invalid)
        for api_name, fields in _TRANSITION_ID_FIELDS.items()
        for field in fields
        for invalid in ("", b"identity", ["identity"])
    ],
)
@pytest.mark.asyncio
async def test_transition_selector_rejects_invalid_identity_before_coordinator(
    api_name: str,
    field: str,
    invalid: object,
) -> None:
    await _assert_transition_selector_rejected(api_name, field, invalid)


@pytest.mark.parametrize(
    ("api_name", "field", "invalid"),
    [
        (api_name, field, invalid)
        for api_name, fields in _TRANSITION_NUMBER_FIELDS.items()
        for field in fields
        for invalid in (True, -1, 2**53)
    ],
)
@pytest.mark.asyncio
async def test_transition_selector_rejects_invalid_number_before_coordinator(
    api_name: str,
    field: str,
    invalid: object,
) -> None:
    await _assert_transition_selector_rejected(api_name, field, invalid)


@pytest.mark.parametrize(
    ("api_name", "field", "invalid"),
    [
        ("transition_control_plane_observed", "candidate", "NOT_AN_OBSERVED_STATE"),
        ("transition_executor_step_state", "candidate", "NOT_A_STEP_PHASE"),
        ("transition_executor_attempt_state", "candidate", "NOT_AN_ATTEMPT_PHASE"),
        ("terminate_executor_attempt", "outcome", "NOT_AN_ATTEMPT_OUTCOME"),
        ("terminate_executor_attempt", "ended_at", b"2026-08-14T08:00:00Z"),
        ("terminate_executor_attempt", "ended_at", "2026-08-14T08:00:00"),
        ("terminate_executor_attempt", "ended_at", "not-a-time"),
        ("terminate_executor_attempt", "termination_reason", b"reason"),
        ("terminate_executor_attempt", "termination_reason", []),
        ("terminate_executor_attempt", "termination_reason", ""),
    ],
)
@pytest.mark.asyncio
async def test_transition_selector_rejects_invalid_enum_time_or_nullable_before_coordinator(
    api_name: str,
    field: str,
    invalid: object,
) -> None:
    await _assert_transition_selector_rejected(api_name, field, invalid)


@pytest.mark.parametrize("invalid", [True, -1, 2**53, 2**63, [], b"version"])
@pytest.mark.asyncio
async def test_control_command_version_rejects_unsafe_integer_before_coordinator(invalid: object) -> None:
    """Control command 版本必须是跨语言安全整数，拒绝时 coordinator 不得执行。"""
    coordinator = _RecordingCoordinator()
    service = ControlService(
        coordinator=coordinator,
        now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        command_id_factory=_ids("unused-command-version"),
        receipt_id_factory=_ids("unused-receipt-version"),
        state_event_id_factory=_ids("unused-event-version"),
    )
    with pytest.raises(ControlRequestError):
        request = ControlCommandRequest(
            request_id="request-version-boundary",
            run_id="run-control-1",
            command_type=ControlCommandType.SOFT_PAUSE,
            actor_id="actor-control-1",
            expected_state_version=invalid,  # type: ignore[arg-type]
            reason_digest=SHA_A,
        )
        await service.submit(request)
    assert coordinator.calls == 0


def test_control_command_version_accepts_max_safe_integer() -> None:
    """2**53-1 是 Control command selector 的可移植闭区间上界。"""
    assert _request(expected_state_version=2**53 - 1).expected_state_version == 2**53 - 1


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        (field, invalid)
        for field in (
            "expected_task_state_version",
            "expected_run_state_version",
            "expected_barrier_state_version",
        )
        for invalid in (True, -1, 2**53, 2**63, [], b"version")
    ],
)
@pytest.mark.asyncio
async def test_barrier_versions_reject_unsafe_integer_before_coordinator(
    field: str,
    invalid: object,
) -> None:
    """Barrier 三路 CAS 版本必须在构造请求时稳定拒绝，不能进入事务。"""
    coordinator = _RecordingCoordinator()
    service = TransitionService(coordinator=coordinator, state_event_id_factory=_ids("unused-barrier-version"))
    base = _target_close_request(planning_barrier_id="barrier-version", request_id="request-barrier-version")
    with pytest.raises(BarrierMilestoneCommitError):
        request = replace(base, **{field: invalid})
        await service.commit_passed_barrier(request)
    assert coordinator.calls == 0


def test_barrier_versions_accept_max_safe_integer() -> None:
    """Barrier 的 Task/Run/Barrier 三个版本都接受 2**53-1。"""
    maximum = 2**53 - 1
    request = replace(
        _target_close_request(planning_barrier_id="barrier-version-max", request_id="request-version-max"),
        expected_task_state_version=maximum,
        expected_run_state_version=maximum,
        expected_barrier_state_version=maximum,
    )
    assert (
        request.expected_task_state_version,
        request.expected_run_state_version,
        request.expected_barrier_state_version,
    ) == (maximum, maximum, maximum)


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
    if drain_state == "DRAINING":
        _insert_control_command_ack(
            connection,
            command_id="command-control-lineage-drain",
            command_seq=1,
            acknowledged_attempt_id="attempt-control-lineage",
        )
        connection.execute(
            "UPDATE attempts SET interrupt_command_id='command-control-lineage-drain' "
            "WHERE attempt_id='attempt-control-lineage'"
        )
        connection.execute("UPDATE runs SET control_command_seq=1 WHERE run_id='run-control-1'")


def _insert_canonical_active_authority(
    connection: sqlite3.Connection,
    *,
    authorization_issued_at: str = "2026-08-17T08:59:00Z",
    authorization_expires_at: str = "2026-08-17T10:00:00Z",
    lease_acquired_at: str = "2026-08-17T08:58:00Z",
    lease_heartbeat_at: str = "2026-08-17T08:59:00Z",
    lease_expires_at: str = "2026-08-17T10:00:00Z",
) -> None:
    """写入真实 Intent/Execution 合同、活动 Attempt 与时间自洽 lease 的完整 RUNNING 权威链。"""
    _insert_active_attempt_graph(connection)
    _insert(
        connection,
        "resource_leases",
        {
            "resource_key": "resource-control-lineage",
            "owner_executor_id": "executor-control-lineage",
            "fencing_token": 7,
            "control_epoch": 9,
            "acquired_at": lease_acquired_at,
            "heartbeat_at": lease_heartbeat_at,
            "expires_at": lease_expires_at,
            "state_version": 0,
        },
    )
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(
            connection,
            authorization_id="auth-control-lineage",
            step_id="step-control-active",
            attempt_id="attempt-control-lineage",
            node_type="PLAN",
            executor_id="executor-control-lineage",
            action_capability="repo.read",
            issued_at=authorization_issued_at,
            expires_at=authorization_expires_at,
        ),
    )


@pytest.mark.parametrize("table", ["steps", "attempts"])
@pytest.mark.asyncio
async def test_executor_authority_rejects_nonterminal_outcome_and_rolls_back(table: str) -> None:
    """executor 写权威必须先校验当前 Step/Attempt phase-outcome，不得提交 Run 或事件。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        connection.execute(f"UPDATE {table} SET outcome='SUCCEEDED'")  # noqa: S608
        before = _transaction_projection_snapshot(connection)
        with pytest.raises(WorkflowRepositoryError, match="phase/outcome"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-invalid-{table}-outcome"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-invalid-{table}-outcome",
            )
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize("write_kind", ["step", "attempt"])
@pytest.mark.asyncio
async def test_guarded_wrapper_rejects_cross_projection_corruption(write_kind: str) -> None:
    """Step/Attempt wrapper 都必须交叉校验同一 active lineage 的另一个 projection。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_canonical_active_authority(connection)
        corrupted_table = "attempts" if write_kind == "step" else "steps"
        connection.execute(f"UPDATE {corrupted_table} SET outcome='SUCCEEDED'")  # noqa: S608
        before = _transaction_projection_snapshot(connection)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids(f"state-event-cross-corruption-{write_kind}"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(WorkflowRepositoryError, match="phase/outcome"):
            if write_kind == "step":
                await service.transition_executor_step_state(
                    request_id="request-cross-corruption-step",
                    run_id="run-control-1",
                    step_id="step-control-active",
                    attempt_id="attempt-control-lineage",
                    expected_run_state_version=0,
                    expected_step_state_version=0,
                    executor_id="executor-control-lineage",
                    fencing_token=7,
                    control_epoch=9,
                    candidate=StepPhase.RECONCILING,
                )
            else:
                await service.transition_executor_attempt_state(
                    request_id="request-cross-corruption-attempt",
                    run_id="run-control-1",
                    attempt_id="attempt-control-lineage",
                    expected_run_state_version=0,
                    expected_attempt_state_version=0,
                    executor_id="executor-control-lineage",
                    fencing_token=7,
                    control_epoch=9,
                    candidate=AttemptPhase.RECONCILING,
                )
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("table", "phase", "outcome", "ended_at"),
    [
        ("steps", "PENDING", "SUCCEEDED", None),
        ("steps", "TERMINAL", "NONE", None),
        ("steps", "TERMINAL", "UNKNOWN_REMOTE_STATE", None),
        ("attempts", "RUNNING", "SUCCEEDED", None),
        ("attempts", "TERMINATED", "NONE", "2026-08-17T09:00:00Z"),
    ],
)
def test_observed_authority_rejects_invalid_current_phase_outcome_fact(
    table: str,
    phase: str,
    outcome: str,
    ended_at: str | None,
) -> None:
    """observed authority 必须扫描当前 Run 的全部 Step/Attempt，损坏组合不能降级为布尔假。"""
    connection = _connection()
    try:
        _insert_active_attempt_graph(connection)
        if table == "attempts":
            connection.execute(
                "UPDATE attempts SET phase=?,outcome=?,ended_at=?",
                (phase, outcome, ended_at),
            )
        else:
            connection.execute("UPDATE steps SET phase=?,outcome=?", (phase, outcome))
        before = _transaction_projection_snapshot(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError, match="phase/outcome"):
                SqliteWorkflowRepository(unit_of_work).load_observed_state_authority(
                    run_id="run-control-1",
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                )
        finally:
            unit_of_work.rollback()
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("phase", "outcome", "started_at", "ended_at"),
    [
        ("RUNNING", "NONE", "2026-08-17T07:00:00Z", "2026-08-17T08:00:00Z"),
        ("TERMINATED", "SUCCEEDED", "2026-08-17T07:00:00Z", None),
        ("TERMINATED", "SUCCEEDED", "2026-08-17T07:00:00Z", "2026-08-17T09:00:00.001Z"),
        ("TERMINATED", "SUCCEEDED", "2026-08-17T08:30:00Z", "2026-08-17T08:00:00Z"),
    ],
    ids=["nonterminal-ended", "terminal-ended-null", "terminal-ended-future", "terminal-ended-before-started"],
)
def test_observed_authority_rejects_invalid_attempt_chronology(
    phase: str,
    outcome: str,
    started_at: str,
    ended_at: str | None,
) -> None:
    """observed authority 必须按 authoritative now 校验每个 Attempt 的完整时间线。"""
    connection = _connection()
    try:
        _insert_active_attempt_graph(connection)
        connection.execute("UPDATE steps SET phase='TERMINAL',outcome='SUCCEEDED'")
        connection.execute(
            "UPDATE attempts SET phase=?,outcome=?,started_at=?,ended_at=?",
            (phase, outcome, started_at, ended_at),
        )
        before = _transaction_projection_snapshot(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError, match="chronology"):
                SqliteWorkflowRepository(unit_of_work).load_observed_state_authority(
                    run_id="run-control-1",
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                )
        finally:
            unit_of_work.rollback()
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_observed_authority_accepts_reconciling_unknown_but_does_not_settle_terminal_unknown() -> None:
    """Step RECONCILING/UNKNOWN 是阻断事实；Attempt TERMINATED/UNKNOWN 合法但绝不算 settled。"""
    reconciling = _connection()
    terminal = _connection()
    try:
        _insert_active_attempt_graph(
            reconciling,
            step_phase="RECONCILING",
            step_outcome="UNKNOWN_REMOTE_STATE",
            attempt_phase="RECONCILING",
            attempt_outcome="NONE",
        )
        reconciling_uow = SqliteUnitOfWork(reconciling, failure_probe=lambda _point: None)
        reconciling_uow.begin_immediate()
        try:
            reconciling_facts = SqliteWorkflowRepository(reconciling_uow).load_observed_state_authority(
                run_id="run-control-1",
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            )
        finally:
            reconciling_uow.rollback()
        assert reconciling_facts.unknown_remote_state is True
        assert reconciling_facts.reconciling_fact is True
        blocked = await TransitionService(
            coordinator=_SqliteCoordinator(reconciling),
            state_event_id_factory=_ids("state-event-reconciling-unknown"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).transition_control_plane_observed(
            run_id="run-control-1",
            expected_state_version=0,
            candidate=RunObservedState.BLOCKED,
            request_id="request-reconciling-unknown",
        )
        assert blocked.observed_state is RunObservedState.BLOCKED

        _insert_active_attempt_graph(terminal)
        terminal.execute("UPDATE steps SET phase='TERMINAL',outcome='SUCCEEDED'")
        terminal.execute(
            "UPDATE attempts SET phase='TERMINATED',outcome='UNKNOWN_REMOTE_STATE',"
            "started_at=NULL,ended_at='2026-08-17T09:00:00Z'"
        )
        terminal_uow = SqliteUnitOfWork(terminal, failure_probe=lambda _point: None)
        terminal_uow.begin_immediate()
        try:
            terminal_facts = SqliteWorkflowRepository(terminal_uow).load_observed_state_authority(
                run_id="run-control-1",
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            )
        finally:
            terminal_uow.rollback()
        assert terminal_facts.unknown_remote_state is True
        assert terminal_facts.terminated_fact is False
    finally:
        reconciling.close()
        terminal.close()


def _replace_active_execution_authorization(
    connection: sqlite3.Connection,
    **overrides: object,
) -> None:
    """仅替换一份 canonical ExecutionAuthorization 条件，供单变量 lineage 反例复用。"""
    options: dict[str, object] = {
        "authorization_id": "auth-control-lineage",
        "intent_authorization_id": "intent-control-1",
        "plan_revision_id": "plan-control-1",
        "step_id": "step-control-active",
        "attempt_id": "attempt-control-lineage",
        "node_type": "PLAN",
        "executor_id": "executor-control-lineage",
        "action_capability": "repo.read",
        "fencing_token": 7,
        "control_epoch": 9,
        "accepted_control_command_seq": 0,
    }
    options.update(overrides)
    connection.execute("DELETE FROM execution_authorizations WHERE execution_authorization_id='auth-control-lineage'")
    _insert(
        connection,
        "execution_authorizations",
        _canonical_execution_authorization_record(connection, **options),
    )


def _mutate_active_authority_lineage(connection: sqlite3.Connection, mutation: str) -> None:
    """每个分支只漂移一个已持久 selector，辅助行仅满足 FK，不改变当前权威指针。"""
    if mutation == "intent_project":
        _insert(connection, "projects", {"project_id": "project-control-other"})
        _replace_canonical_intent_authorization(connection, project_id="project-control-other")
    elif mutation == "intent_target":
        _replace_canonical_intent_authorization(connection, target_stage="DESIGN_APPROVED")
    elif mutation == "run_spec_target":
        required_digest = _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            plan_revision_id="plan-control-target-drift",
            spec_revision=2,
            run_spec_target_stage="DESIGN_APPROVED",
        )
        barrier_id = _derived_barrier_id(
            connection,
            business_phase="PLANNING",
            barrier_ordinal=0,
            plan_revision_id="plan-control-target-drift",
        )
        _insert(
            connection,
            "phase_barriers",
            {
                "barrier_id": barrier_id,
                "run_id": "run-control-1",
                "plan_revision_id": "plan-control-target-drift",
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
        connection.execute("UPDATE runs SET active_barrier_id=? WHERE run_id='run-control-1'", (barrier_id,))
        connection.execute(
            "UPDATE steps SET plan_revision_id='plan-control-target-drift',barrier_id=? "
            "WHERE step_id='step-control-active'",
            (barrier_id,),
        )
        _replace_active_execution_authorization(connection, plan_revision_id="plan-control-target-drift")
    elif mutation == "task_active_run":
        connection.execute("UPDATE tasks SET active_run_id=NULL WHERE task_id='task-control-1'")
    elif mutation == "task_active_plan":
        connection.execute("UPDATE tasks SET active_plan_revision_id=NULL WHERE task_id='task-control-1'")
    elif mutation == "step_run":
        cursor = connection.execute("SELECT * FROM runs WHERE run_id='run-control-1'")
        row = cursor.fetchone()
        assert row is not None
        clone = dict(zip((item[0] for item in cursor.description), row, strict=True))
        clone["run_id"] = "run-control-other"
        _insert(connection, "runs", clone)
        connection.execute("UPDATE steps SET run_id='run-control-other' WHERE step_id='step-control-active'")
    elif mutation == "step_plan":
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            plan_revision_id="plan-control-other",
            spec_revision=2,
        )
        connection.execute("UPDATE tasks SET active_plan_revision_id='plan-control-1' WHERE task_id='task-control-1'")
        connection.execute("UPDATE steps SET plan_revision_id='plan-control-other' WHERE step_id='step-control-active'")
    elif mutation == "plan_task":
        cursor = connection.execute("SELECT * FROM tasks WHERE task_id='task-control-1'")
        row = cursor.fetchone()
        assert row is not None
        clone = dict(zip((item[0] for item in cursor.description), row, strict=True))
        clone.update(
            {
                "task_id": "task-control-other",
                "active_plan_revision_id": None,
                "active_run_id": None,
            }
        )
        _insert(connection, "tasks", clone)
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            plan_revision_id="plan-control-other",
            task_id="task-control-other",
        )
        connection.execute(
            "UPDATE tasks SET active_plan_revision_id='plan-control-other' WHERE task_id='task-control-1'"
        )
        connection.execute("UPDATE steps SET plan_revision_id='plan-control-other' WHERE step_id='step-control-active'")
        _replace_active_execution_authorization(connection, plan_revision_id="plan-control-other")
    elif mutation == "plan_intent":
        _insert(
            connection,
            "intent_authorizations",
            _canonical_intent_authorization_record(authorization_id="intent-control-other"),
        )
        _insert_plan_revision_for_barrier(
            connection,
            required_node_ids=("plan",),
            plan_revision_id="plan-control-other",
            spec_revision=2,
            intent_authorization_id="intent-control-other",
        )
        connection.execute("UPDATE steps SET plan_revision_id='plan-control-other' WHERE step_id='step-control-active'")
        _replace_active_execution_authorization(connection, plan_revision_id="plan-control-other")
    elif mutation == "semantic_plan_hash":
        _replace_active_execution_authorization(connection, semantic_plan_digest="sha256:" + "f" * 64)
    elif mutation == "plan_revision_digest":
        _replace_active_execution_authorization(connection, plan_revision_digest_value="sha256:" + "f" * 64)
    elif mutation == "stage_map_version":
        _replace_active_execution_authorization(connection, stage_capability_map_version="stage-map.other")
    elif mutation == "node_map_version":
        _replace_active_execution_authorization(connection, node_capability_map_version="node-map.other")
    elif mutation == "node_type":
        _replace_active_execution_authorization(
            connection,
            node_type="IMPLEMENT",
            action_capability="worktree.write",
        )
    elif mutation == "executor_id":
        _replace_active_execution_authorization(connection, executor_id="executor-control-other")
    elif mutation == "fencing_token":
        _replace_active_execution_authorization(connection, fencing_token=8)
    elif mutation == "control_epoch":
        _replace_active_execution_authorization(connection, control_epoch=10)
    elif mutation == "dispatch_seq":
        _replace_active_execution_authorization(connection, accepted_control_command_seq=1)
    elif mutation == "intent_projection":
        connection.execute(
            "UPDATE intent_authorizations SET requirement_digest=? WHERE intent_authorization_id='intent-control-1'",
            (b"not-the-canonical-selector",),
        )
    else:  # pragma: no cover - 参数表属于测试自身的闭集。
        raise AssertionError(f"unknown active authority mutation: {mutation}")


def _insert_successful_plan_step(
    connection: sqlite3.Connection,
    *,
    barrier_id: str,
    step_id: str,
    attempt_id: str,
    logical_node_id: str = "plan",
    required_artifact_id: str = "artifact-planning",
    required: bool = True,
    extra_artifact_ids: tuple[str, ...] = (),
) -> None:
    """插入成功 Step；额外 Artifact 用于证明 requiredArtifacts 是包含门禁。"""
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
            "required": int(required),
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
            "drain_state": "NONE",
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
    for artifact_id in extra_artifact_ids:
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


def _insert_successful_artifact_dependency_graph(
    connection: sqlite3.Connection,
    *,
    extra_artifact_ids: tuple[str, ...] = (),
) -> str:
    """插入 required current Step 与 optional dependency，二者共享当前 barrier。"""
    _insert_current_barrier(
        connection,
        required_node_ids=("node-current",),
        dependencies_by_node={"node-current": ("plan",)},
    )
    barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
    _insert_successful_plan_step(
        connection,
        barrier_id=barrier_id,
        step_id="step-artifact-dependency",
        attempt_id="attempt-artifact-dependency",
        required=False,
        extra_artifact_ids=extra_artifact_ids,
    )
    _insert(
        connection,
        "steps",
        {
            "step_id": "step-current-artifact-dependency",
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
            "dependency_hash": _selector_digest(["plan"]),
            "required_artifacts_digest": _selector_digest([]),
            "success_predicate_id": "planning-complete-v1",
            "timeout_ms": 30_000,
            "retry_policy_id": "no-retry-v1",
            "idempotency_key": "step-current-artifact-dependency-v1",
            "state_version": 0,
        },
    )
    return barrier_id


def _insert_successful_design_review_step(
    connection: sqlite3.Connection,
    *,
    barrier_id: str,
    step_id: str,
    attempt_id: str,
    plan_revision_id: str = "plan-control-1",
    artifact_id: str = "artifact-design-stale",
    dependency_ids: tuple[str, ...] = (),
) -> None:
    """写入完整成功的 design Step，并允许冻结合法前驱依赖。"""
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
            "dependency_hash": _selector_digest(list(dependency_ids)),
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
            "drain_state": "NONE",
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


@pytest.mark.parametrize(
    ("field", "invalid"),
    [(field, invalid) for field in ("command_id", "attempt_id") for invalid in ("", b"identity", ["identity"])],
)
@pytest.mark.asyncio
async def test_control_receipt_selector_rejects_before_coordinator(
    field: str,
    invalid: object,
) -> None:
    """receipt identity 不合法时不得开启事务或产生任何 receipt。"""
    coordinator = _RecordingCoordinator()
    service = ControlService(
        coordinator=coordinator,
        now=lambda: datetime(2026, 8, 14, 8, 0, tzinfo=UTC),
        command_id_factory=_ids("unused-command"),
        receipt_id_factory=_ids("unused-receipt"),
        state_event_id_factory=_ids("unused-event"),
    )
    arguments: dict[str, object] = {
        "command_id": "command-control-1",
        "phase": ControlCommandReceiptPhase.COMPLETED,
        "attempt_id": "attempt-control-1",
        "evidence_digest": SHA_A,
    }
    arguments[field] = invalid

    with pytest.raises(ControlRequestError):
        await service.append_receipt(**arguments)  # type: ignore[arg-type]

    assert coordinator.calls == 0


@pytest.mark.parametrize("evidence_digest", [7, b"sha256:" + b"a" * 64, [SHA_A]])
@pytest.mark.asyncio
async def test_control_receipt_non_string_evidence_digest_maps_to_request_error(
    evidence_digest: object,
) -> None:
    """非字符串 digest 必须在服务边界统一归类，不能泄漏 re.TypeError。"""
    connection = _connection()
    try:
        with pytest.raises(ControlRequestError):
            await _service(connection).append_receipt(
                command_id="command-does-not-matter",
                phase=ControlCommandReceiptPhase.COMPLETED,
                attempt_id=None,
                evidence_digest=evidence_digest,  # type: ignore[arg-type]
            )
        assert connection.execute("SELECT count(*) FROM control_command_receipt_events").fetchone() == (0,)
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
            await service.transition_control_plane_observed(
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
            step_phase="RECONCILING",
            step_outcome="UNKNOWN_REMOTE_STATE",
            attempt_phase="INTERRUPTING",
            attempt_outcome="NONE",
            drain_state="DRAINING",
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-unknown-stopping"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        with pytest.raises(TransitionPredicateError):
            await service.transition_control_plane_observed(
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
            await service.transition_control_plane_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate="NOT_A_RUN_STATE",
                evidence=ObservedStateEvidence(0, False, False, False, False, False, False),
                request_id="request-invalid-observed",
            )
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_control_plane_observed_entry_rejects_executor_running_projection() -> None:
    """trusted control-plane 入口不得代替 executor 提交 QUEUED→RUNNING。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-control-plane-running"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(TransitionPredicateError, match="executor-only"):
            await service.transition_control_plane_observed(
                run_id="run-control-1",
                expected_state_version=0,
                candidate=RunObservedState.RUNNING,
                request_id="request-control-plane-running",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("mutation", "selector"),
    [
        ("attempt_id", "attempt-other"),
        ("run_state_version", 1),
        ("executor_id", "executor-other"),
        ("fencing_token", 8),
        ("control_epoch", 10),
    ],
)
@pytest.mark.asyncio
async def test_executor_running_entry_rolls_back_each_stale_selector(
    mutation: str,
    selector: object,
) -> None:
    """executor identity、Run version、token 或 epoch 任一陈旧都必须整笔回滚。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        arguments: dict[str, object] = {
            "run_id": "run-control-1",
            "attempt_id": "attempt-control-lineage",
            "expected_state_version": 0,
            "executor_id": "executor-control-lineage",
            "fencing_token": 7,
            "control_epoch": 9,
            "request_id": f"request-stale-executor-{mutation}",
        }
        arguments[mutation if mutation != "run_state_version" else "expected_state_version"] = selector

        with pytest.raises(FactoryError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-stale-executor-{mutation}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(**arguments)  # type: ignore[arg-type]

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_transition_rejects_noncanonical_execution_authorization_projection() -> None:
    """Execution selector 若偏离真实 canonical 合同，RUNNING CAS 与事件必须整笔回滚。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        connection.execute(
            "UPDATE execution_authorizations SET semantic_plan_hash=? "
            "WHERE execution_authorization_id='auth-control-lineage'",
            (
                canonicalize(
                    _authorization_snapshot(
                        "factory.authorization.execution.semantic-plan.v1",
                        "f",
                    )
                ),
            ),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-noncanonical-auth"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(authorization_domain.AuthorizationContractError):
            await service.transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id="request-noncanonical-auth",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_transition_rejects_terminal_task_authority_and_rolls_back() -> None:
    """当前 Task 已进入终态时，executor 不得再把同一 Run 推进为 RUNNING。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        connection.execute("UPDATE tasks SET lifecycle='SUCCEEDED' WHERE task_id='task-control-1'")

        with pytest.raises(WorkflowRepositoryError, match="active authorization lineage"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-terminal-task-authority"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id="request-terminal-task-authority",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "intent_project",
        "intent_target",
        "run_spec_target",
        "task_active_run",
        "task_active_plan",
        "step_run",
        "step_plan",
        "plan_task",
        "plan_intent",
        "semantic_plan_hash",
        "plan_revision_digest",
        "stage_map_version",
        "node_map_version",
        "node_type",
        "executor_id",
        "fencing_token",
        "control_epoch",
        "dispatch_seq",
        "intent_projection",
    ],
)
@pytest.mark.asyncio
async def test_running_transition_rejects_each_active_authority_lineage_drift(mutation: str) -> None:
    """活动授权链每次只漂移一个条件，任一损坏都不得提交 RUNNING 或状态事件。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        _mutate_active_authority_lineage(connection, mutation)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids(f"state-event-active-lineage-{mutation}"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(FactoryError):
            await service.transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-active-lineage-{mutation}",
            )

        assert connection.execute(
            "SELECT observed_state,state_version FROM runs WHERE run_id='run-control-1'"
        ).fetchone() == (
            "QUEUED",
            0,
        )
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_transition_rejects_duplicate_active_stable_action_key() -> None:
    """同 Attempt/capability/idempotency key 的双活动授权必须歧义拒绝。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        _insert(
            connection,
            "execution_authorizations",
            _canonical_execution_authorization_record(
                connection,
                authorization_id="auth-control-lineage-duplicate",
                step_id="step-control-active",
                attempt_id="attempt-control-lineage",
                node_type="PLAN",
                executor_id="executor-control-lineage",
                action_capability="repo.read",
            ),
        )

        with pytest.raises(WorkflowRepositoryError, match="stable action key"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-duplicate-stable-action"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id="request-duplicate-stable-action",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("node_type", "base_capability", "action_capability", "idempotency_marker"),
    [
        ("IMPLEMENT", "repo.read", "worktree.write", "d"),
        ("PLAN", "repo.read", "repo.read", "f"),
    ],
    ids=["different-capability", "different-idempotency-key"],
)
@pytest.mark.asyncio
async def test_running_transition_ors_distinct_active_stable_action_keys(
    node_type: str,
    base_capability: str,
    action_capability: str,
    idempotency_marker: str,
) -> None:
    """capability 或 canonical idempotency key 不同的活动授权允许按 OR 聚合。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        connection.execute(
            "UPDATE steps SET node_type=? WHERE step_id='step-control-active'",
            (node_type,),
        )
        _replace_active_execution_authorization(
            connection,
            node_type=node_type,
            action_capability=base_capability,
        )
        _insert(
            connection,
            "execution_authorizations",
            _canonical_execution_authorization_record(
                connection,
                authorization_id="auth-control-lineage-distinct",
                step_id="step-control-active",
                attempt_id="attempt-control-lineage",
                node_type=node_type,
                executor_id="executor-control-lineage",
                action_capability=action_capability,
                idempotency_marker=idempotency_marker,
            ),
        )

        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-distinct-stable-action"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).transition_executor_observed(
            run_id="run-control-1",
            attempt_id="attempt-control-lineage",
            expected_state_version=0,
            executor_id="executor-control-lineage",
            fencing_token=7,
            control_epoch=9,
            request_id=f"request-distinct-stable-action-{idempotency_marker}",
        )

        assert (updated.observed_state, updated.state_version) == (RunObservedState.RUNNING, 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    (
        "authorization_issued_at",
        "authorization_expires_at",
        "lease_acquired_at",
        "lease_heartbeat_at",
        "lease_expires_at",
    ),
    [
        (
            "2026-08-17T10:59:00+02:00",
            "2026-08-17T12:00:00+02:00",
            "2026-08-17T10:58:00+02:00",
            "2026-08-17T11:00:00+02:00",
            "2026-08-17T12:00:00+02:00",
        ),
        (
            "2026-08-17T03:59:00-05:00",
            "2026-08-17T05:00:00-05:00",
            "2026-08-17T03:58:00-05:00",
            "2026-08-17T03:59:00-05:00",
            "2026-08-17T05:00:00-05:00",
        ),
        (
            "2026-08-17T09:00:00Z",
            "2026-08-17T10:00:00Z",
            "2026-08-17T08:58:00Z",
            "2026-08-17T09:00:00Z",
            "2026-08-17T10:00:00Z",
        ),
    ],
)
@pytest.mark.asyncio
async def test_running_authority_compares_rfc3339_offsets_as_utc_instants(
    authorization_issued_at: str,
    authorization_expires_at: str,
    lease_acquired_at: str,
    lease_heartbeat_at: str,
    lease_expires_at: str,
) -> None:
    """正负 offset 与边界等价时刻必须按 UTC 瞬时比较，而不是按原始字符串排序。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(
            connection,
            authorization_issued_at=authorization_issued_at,
            authorization_expires_at=authorization_expires_at,
            lease_acquired_at=lease_acquired_at,
            lease_heartbeat_at=lease_heartbeat_at,
            lease_expires_at=lease_expires_at,
        )
        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-rfc3339-offset"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).transition_executor_observed(
            run_id="run-control-1",
            attempt_id="attempt-control-lineage",
            expected_state_version=0,
            executor_id="executor-control-lineage",
            fencing_token=7,
            control_epoch=9,
            request_id="request-rfc3339-offset",
        )

        assert updated.observed_state is RunObservedState.RUNNING
        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("RUNNING", 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_authority_ignores_valid_historical_attempt_authorization() -> None:
    """同 Run 的合法历史授权不得被误当成当前 active Attempt 的 lineage 漂移。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-control-history",
                "step_id": "step-control-active",
                "supersedes_attempt_id": None,
                "phase": "TERMINATED",
                "outcome": "KILLED",
                "executor_id": "executor-control-history",
                "process_session_id": None,
                "pid": None,
                "process_start_time": None,
                "job_object_id": None,
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": None,
                "termination_reason": "superseded",
                "fencing_token": 6,
                "control_epoch": 8,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "NONE",
                "started_at": "2026-08-17T07:00:00Z",
                "ended_at": "2026-08-17T08:00:00Z",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "execution_authorizations",
            _canonical_execution_authorization_record(
                connection,
                authorization_id="auth-control-history",
                step_id="step-control-active",
                attempt_id="attempt-control-history",
                node_type="PLAN",
                executor_id="executor-control-history",
                action_capability="repo.read",
                fencing_token=6,
                control_epoch=8,
                issued_at="2026-08-17T07:00:00Z",
                expires_at="2026-08-17T08:00:00Z",
            ),
        )

        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-current-auth-only"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).transition_executor_observed(
            run_id="run-control-1",
            attempt_id="attempt-control-lineage",
            expected_state_version=0,
            executor_id="executor-control-lineage",
            fencing_token=7,
            control_epoch=9,
            request_id="request-current-auth-only",
        )

        assert (updated.observed_state, updated.state_version) == (RunObservedState.RUNNING, 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "authorization_not_yet_issued",
        "authorization_expired_at_boundary",
        "authorization_inverted",
        "authorization_naive",
        "lease_expired_at_boundary",
        "lease_future_heartbeat",
        "lease_inverted",
        "lease_naive",
        "now_naive",
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_inactive_authority_time_rolls_back_running_transition(mutation: str) -> None:
    """时间格式、顺序、半开区间或未来心跳任一失败，都不能留下 Run CAS 或事件。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        now_value = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        if mutation == "authorization_not_yet_issued":
            _replace_active_execution_authorization(
                connection,
                issued_at="2026-08-17T09:00:00.001Z",
                expires_at="2026-08-17T10:00:00Z",
            )
        elif mutation == "authorization_expired_at_boundary":
            _replace_active_execution_authorization(
                connection,
                issued_at="2026-08-17T08:00:00Z",
                expires_at="2026-08-17T11:00:00+02:00",
            )
        elif mutation == "authorization_inverted":
            _replace_active_execution_authorization(
                connection,
                issued_at="2026-08-17T10:00:00Z",
                expires_at="2026-08-17T09:00:00Z",
            )
        elif mutation == "authorization_naive":
            connection.execute(
                "UPDATE execution_authorizations SET issued_at='2026-08-17T08:59:00' "
                "WHERE execution_authorization_id='auth-control-lineage'"
            )
        elif mutation == "lease_expired_at_boundary":
            connection.execute("UPDATE resource_leases SET expires_at='2026-08-17T09:00:00Z'")
        elif mutation == "lease_future_heartbeat":
            connection.execute("UPDATE resource_leases SET heartbeat_at='2026-08-17T09:00:00.001Z'")
        elif mutation == "lease_inverted":
            connection.execute(
                "UPDATE resource_leases SET acquired_at='2026-08-17T09:00:00Z',heartbeat_at='2026-08-17T08:59:00Z'"
            )
        elif mutation == "lease_naive":
            connection.execute("UPDATE resource_leases SET heartbeat_at='2026-08-17T08:59:00'")
        elif mutation == "now_naive":
            now_value = datetime(2026, 8, 17, 9, 0)
        else:  # pragma: no cover - 参数表属于测试自身的闭集。
            raise AssertionError(f"unknown time mutation: {mutation}")

        with pytest.raises(FactoryError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-time-{mutation}"),
                now=lambda: now_value,
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-time-{mutation}",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("authorization_kind", "issued_at", "expires_at", "revoked_at"),
    [
        ("execution", "2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T06:30:00Z"),
        ("execution", "2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T09:30:00Z"),
        ("execution", "2026-08-17T07:00:00Z", "2026-08-17T08:00:00Z", "2026-08-17T08:30:00Z"),
        ("intent", "2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T06:30:00Z"),
        ("intent", "2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T09:30:00Z"),
        ("intent", "2026-08-17T07:00:00Z", "2026-08-17T08:00:00Z", "2026-08-17T08:30:00Z"),
    ],
    ids=[
        "execution-before-issued",
        "execution-future",
        "execution-after-expiry",
        "intent-before-issued",
        "intent-future",
        "intent-after-expiry",
    ],
)
@pytest.mark.asyncio
async def test_running_authority_rejects_invalid_revocation_timeline_and_rolls_back(
    authorization_kind: str,
    issued_at: str,
    expires_at: str,
    revoked_at: str,
) -> None:
    """Execution/Intent 的倒置、未来或过期后撤销必须作为独立损坏事实拒绝。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        if authorization_kind == "execution":
            _replace_active_execution_authorization(
                connection,
                issued_at=issued_at,
                expires_at=expires_at,
                revoked_at=revoked_at,
                revoke_reason="invalid-revocation-probe",
            )
        else:
            _replace_canonical_intent_authorization(
                connection,
                issued_at=issued_at,
                expires_at=expires_at,
                revoked_at=revoked_at,
                revoke_reason="invalid-revocation-probe",
            )

        with pytest.raises(WorkflowRepositoryError, match="revocation time"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-revocation-{authorization_kind}-{revoked_at}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-revocation-{authorization_kind}-{revoked_at}",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("acquired_at", "heartbeat_at", "expires_at"),
    [
        ("2026-08-17T08:58:00Z", "2026-08-17T09:00:00.001Z", "2026-08-17T10:00:00Z"),
        ("2026-08-17T07:00:00Z", "2026-08-17T08:30:00Z", "2026-08-17T08:00:00Z"),
    ],
    ids=["future-heartbeat", "heartbeat-after-expiry"],
)
@pytest.mark.asyncio
async def test_running_authority_rejects_invalid_matching_lease_timeline_and_rolls_back(
    acquired_at: str,
    heartbeat_at: str,
    expires_at: str,
) -> None:
    """匹配 lease 的未来心跳或 heartbeat>=expires 是损坏事实，必须抛仓储错误并回滚。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        connection.execute(
            "UPDATE resource_leases SET acquired_at=?,heartbeat_at=?,expires_at=?",
            (acquired_at, heartbeat_at, expires_at),
        )

        with pytest.raises(WorkflowRepositoryError, match="lease time ordering"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-invalid-lease-{heartbeat_at}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-invalid-lease-{heartbeat_at}",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_authority_validates_every_matching_lease_before_or() -> None:
    """一条活动 lease 不能用 OR 短路掩盖同 selector 下另一条未来心跳损坏行。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        _insert(
            connection,
            "resource_leases",
            {
                "resource_key": "resource-control-lineage-corrupt",
                "owner_executor_id": "executor-control-lineage",
                "fencing_token": 7,
                "control_epoch": 9,
                "acquired_at": "2026-08-17T08:58:00Z",
                "heartbeat_at": "2026-08-17T09:00:00.001Z",
                "expires_at": "2026-08-17T10:00:00Z",
                "state_version": 0,
            },
        )

        with pytest.raises(WorkflowRepositoryError, match="lease time ordering"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-all-matching-leases"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id="request-all-matching-leases",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.parametrize("lease_mutation", ["missing", "owner", "token", "epoch", "expired"])
@pytest.mark.asyncio
async def test_running_authority_requires_current_matching_active_lease(lease_mutation: str) -> None:
    """缺失、selector 错配或已过期 lease 都不能授权当前 executor 推进 RUNNING。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        if lease_mutation == "missing":
            connection.execute("DELETE FROM resource_leases")
        elif lease_mutation == "owner":
            connection.execute("UPDATE resource_leases SET owner_executor_id='executor-other'")
        elif lease_mutation == "token":
            connection.execute("UPDATE resource_leases SET fencing_token=8")
        elif lease_mutation == "epoch":
            connection.execute("UPDATE resource_leases SET control_epoch=10")
        elif lease_mutation == "expired":
            connection.execute("UPDATE resource_leases SET expires_at='2026-08-17T09:00:00Z'")
        else:  # pragma: no cover - 参数表属于测试自身闭集。
            raise AssertionError(f"unknown lease mutation: {lease_mutation}")

        with pytest.raises(TransitionPredicateError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-lease-selector-{lease_mutation}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-control-lineage",
                expected_state_version=0,
                executor_id="executor-control-lineage",
                fencing_token=7,
                control_epoch=9,
                request_id=f"request-lease-selector-{lease_mutation}",
            )

        assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_running_authority_allows_multiple_valid_matching_leases() -> None:
    """同 owner/token/epoch 的多条合法活动 lease 可按 OR 汇总，资源映射仍留给 Task 4/5。"""
    connection = _connection(desired_state="RUNNING", observed_state="QUEUED")
    try:
        _insert_canonical_active_authority(connection)
        _insert(
            connection,
            "resource_leases",
            {
                "resource_key": "resource-control-lineage-secondary",
                "owner_executor_id": "executor-control-lineage",
                "fencing_token": 7,
                "control_epoch": 9,
                "acquired_at": "2026-08-17T08:55:00Z",
                "heartbeat_at": "2026-08-17T08:59:30Z",
                "expires_at": "2026-08-17T10:00:00Z",
                "state_version": 0,
            },
        )

        updated = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-multiple-matching-leases"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).transition_executor_observed(
            run_id="run-control-1",
            attempt_id="attempt-control-lineage",
            expected_state_version=0,
            executor_id="executor-control-lineage",
            fencing_token=7,
            control_epoch=9,
            request_id="request-multiple-matching-leases",
        )

        assert (updated.observed_state, updated.state_version) == (RunObservedState.RUNNING, 1)
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (1,)
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
                "drain_state": "NONE",
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
        with pytest.raises(WorkflowRepositoryError, match="phase/outcome"):
            await service.transition_control_plane_observed(
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
        result = await service.transition_control_plane_observed(
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
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
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


def test_barrier_dependency_allows_extra_committed_artifact_from_same_attempt() -> None:
    """依赖的必需 Artifact 已提交时，同 Attempt 的额外合法产物不能阻断 barrier。"""
    connection = _connection()
    try:
        barrier_id = _insert_successful_artifact_dependency_graph(
            connection,
            extra_artifact_ids=("artifact-planning-diagnostics",),
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            authority = SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                barrier_id=barrier_id,
                run_id="run-control-1",
                task_id="task-control-1",
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            )
            decision = evaluate_barrier(
                authority.steps,
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                settle_deadline_at=None,
            )
            assert decision.passed is True
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


def test_barrier_current_step_allows_extra_committed_artifact_from_same_attempt() -> None:
    """当前 required Step 的 requiredArtifacts 是下限，额外 COMMITTED Artifact 合法。"""
    connection = _connection()
    try:
        _insert_current_barrier(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_successful_plan_step(
            connection,
            barrier_id=barrier_id,
            step_id="step-current-artifact-subset",
            attempt_id="attempt-current-artifact-subset",
            extra_artifact_ids=("artifact-planning-diagnostics",),
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            authority = SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                barrier_id=barrier_id,
                run_id="run-control-1",
                task_id="task-control-1",
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            )
            decision = evaluate_barrier(
                authority.steps,
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                settle_deadline_at=None,
            )
            assert decision.passed is True
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize("authority_path", ["dependency", "current"])
def test_barrier_rejects_missing_required_artifact(authority_path: str) -> None:
    """subset 门禁仍必须拒绝 requiredArtifacts 缺失，防止正例放宽成任意产物通过。"""
    connection = _connection()
    try:
        if authority_path == "dependency":
            barrier_id = _insert_successful_artifact_dependency_graph(connection)
        else:
            _insert_current_barrier(connection)
            barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
            _insert_successful_plan_step(
                connection,
                barrier_id=barrier_id,
                step_id="step-current-artifact-missing",
                attempt_id="attempt-current-artifact-missing",
            )
        connection.execute("DELETE FROM artifacts WHERE artifact_id='artifact-planning'")
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            repository = SqliteWorkflowRepository(unit_of_work)
            if authority_path == "dependency":
                with pytest.raises(WorkflowRepositoryError, match="artifacts are incomplete"):
                    repository.load_barrier_authority(
                        barrier_id=barrier_id,
                        run_id="run-control-1",
                        task_id="task-control-1",
                        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                    )
            else:
                authority = repository.load_barrier_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                )
                decision = evaluate_barrier(
                    authority.steps,
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                    settle_deadline_at=None,
                )
                assert decision.passed is False
                assert decision.block_reason_code == "REQUIRED_ARTIFACT_MISSING"
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
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                )
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("corruption", "expected_detail"),
    [
        ("terminal-none", "Attempt phase/outcome is invalid"),
        ("ended-future", "attempt chronology is invalid"),
    ],
)
@pytest.mark.asyncio
async def test_non_target_barrier_rejects_invalid_historical_attempt_projection(
    corruption: str,
    expected_detail: str,
) -> None:
    """普通 planning gate 也必须扫描同 Run 的历史/非目标 Attempt，并整笔回滚三聚合与事件。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        _, _, historical_attempt_id = _insert_historical_attempt_graph(connection)
        if corruption == "terminal-none":
            connection.execute("UPDATE attempts SET outcome='NONE' WHERE attempt_id=?", (historical_attempt_id,))
        else:
            connection.execute(
                "UPDATE attempts SET ended_at='2026-08-17T09:00:00.001Z' WHERE attempt_id=?",
                (historical_attempt_id,),
            )
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        authority_now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        clock = _RecordingClock(authority_now)
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority facts are incomplete") as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-nontarget-attempt-{corruption}"),
                now=clock,
            ).commit_passed_barrier(
                _planning_to_design_request(
                    request_id=f"request-nontarget-attempt-{corruption}",
                    barrier_id=planning_barrier_id,
                    next_barrier_id=design_barrier_id,
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == expected_detail
        assert clock.calls == [authority_now]
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_non_target_barrier_rejects_invalid_old_plan_step_projection() -> None:
    """普通 barrier 必须扫描旧 Plan Step；TERMINAL/NONE 即使 Attempt 合法也要整笔回滚。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        _, historical_step_id, _ = _insert_historical_attempt_graph(connection)
        connection.execute("UPDATE steps SET outcome='NONE' WHERE step_id=?", (historical_step_id,))
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority facts are incomplete") as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-old-plan-invalid-step"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _planning_to_design_request(
                    request_id="request-old-plan-invalid-step",
                    barrier_id=planning_barrier_id,
                    next_barrier_id=design_barrier_id,
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == "Step phase/outcome is invalid"
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_non_target_barrier_rejects_invalid_current_plan_optional_step_projection() -> None:
    """当前 Plan 的非 required Step 也属于 Run-wide 权威投影，不能被 required gate 忽略。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(
            connection,
            optional_nodes=(("PLANNING", 0, "optional-plan"),),
        )
        _insert_invalid_optional_planning_step(connection)
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority facts are incomplete") as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-current-plan-optional-invalid-step"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _planning_to_design_request(
                    request_id="request-current-plan-optional-invalid-step",
                    barrier_id=planning_barrier_id,
                    next_barrier_id=design_barrier_id,
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == "Step phase/outcome is invalid"
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_non_target_barrier_maps_corrupt_old_plan_step_projection() -> None:
    """旧 Plan Step raw enum 损坏必须稳定映射为仓储错误，不能泄漏领域 hydrate 异常。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        _, historical_step_id, _ = _insert_historical_attempt_graph(connection)
        _corrupt_persisted_step_phase(connection, step_id=historical_step_id)
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority facts are incomplete") as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-old-plan-corrupt-step"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _planning_to_design_request(
                    request_id="request-old-plan-corrupt-step",
                    barrier_id=planning_barrier_id,
                    next_barrier_id=design_barrier_id,
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == "Step projection is invalid"
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_non_target_barrier_allows_valid_old_plan_step_projection() -> None:
    """合法旧 Plan TERMINAL/SUCCEEDED Step 仍可被扫描，且不阻断当前 planning barrier。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        _insert_historical_attempt_graph(connection)
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        design_barrier_id = _derived_barrier_id(
            connection,
            business_phase="DESIGN_REVIEWING",
            barrier_ordinal=1,
        )

        result = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-valid-old-plan-step"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).commit_passed_barrier(
            _planning_to_design_request(
                request_id="request-valid-old-plan-step",
                barrier_id=planning_barrier_id,
                next_barrier_id=design_barrier_id,
            )
        )

        assert result.barrier.passed is True
        assert connection.execute(
            "SELECT phase,active_barrier_id,state_version FROM runs WHERE run_id='run-control-1'"
        ).fetchone() == ("DESIGN_REVIEWING", design_barrier_id, 1)
        assert connection.execute(
            "SELECT achieved_stage,lifecycle,outcome_version,state_version FROM tasks WHERE task_id='task-control-1'"
        ).fetchone() == ("DESIGN_APPROVED", "ACTIVE", 1, 1)
        assert connection.execute(
            "SELECT aggregate_type FROM authoritative_state_events ORDER BY aggregate_type"
        ).fetchall() == [("PHASE_BARRIER",), ("RUN",), ("TASK",)]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "authority_now",
    [
        datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        datetime.fromisoformat("2026-08-17T17:00:00+08:00"),
    ],
    ids=["ended-equals-now", "equivalent-offset"],
)
def test_barrier_authority_allows_terminal_attempt_at_equivalent_now(authority_now: datetime) -> None:
    """历史 Attempt 的 ended_at==now 合法，带 offset 的等价权威瞬时也不得误拒绝。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        _, _, historical_attempt_id = _insert_historical_attempt_graph(connection)
        connection.execute(
            "UPDATE attempts SET started_at='2026-08-17T08:00:00Z',ended_at='2026-08-17T09:00:00Z' WHERE attempt_id=?",
            (historical_attempt_id,),
        )
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        before = _transaction_projection_snapshot(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            authority = SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                barrier_id=barrier_id,
                run_id="run-control-1",
                task_id="task-control-1",
                now=authority_now,
            )
            assert authority.active_attempt_count == 0
        finally:
            unit_of_work.rollback()
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    "invalid_now",
    [
        "2026-08-17T09:00:00Z",
        datetime(2026, 8, 17, 9, 0),
        datetime(2026, 8, 17, 9, 0, tzinfo=_InvalidOffsetTimezone()),
        datetime(2026, 8, 17, 9, 0, tzinfo=_BrokenOffsetTimezone()),
    ],
    ids=["not-datetime", "naive", "invalid-offset", "conversion-error"],
)
def test_barrier_authority_rejects_invalid_clock_without_writes(invalid_now: object) -> None:
    """仓储 barrier 边界拒绝任意非法时钟，且不得读取墙钟或留下状态写。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection)
        barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        before = _transaction_projection_snapshot(connection)
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError, match="barrier authority clock is invalid"):
                SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                    barrier_id=barrier_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                    now=invalid_now,  # type: ignore[arg-type]
                )
        finally:
            unit_of_work.rollback()
        assert _transaction_projection_snapshot(connection) == before
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
                "drain_state": "NONE",
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
                "drain_state": "NONE",
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


@pytest.mark.parametrize(
    ("table", "identity_column", "identity", "expected_detail"),
    [
        ("steps", "step_id", "step-planning-historical", "Step phase/outcome is invalid"),
        ("attempts", "attempt_id", "attempt-planning-historical", "Attempt phase/outcome is invalid"),
    ],
    ids=["step-terminal-none", "attempt-terminated-none"],
)
@pytest.mark.asyncio
async def test_target_close_rejects_invalid_historical_phase_outcome_and_rolls_back(
    table: str,
    identity_column: str,
    identity: str,
    expected_detail: str,
) -> None:
    """目标收口必须扫描同一 Run 的历史投影，终态 NONE 不能被当前 barrier 正例掩盖。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_historical_attempt_graph(connection)
        connection.execute(
            f"UPDATE {table} SET outcome='NONE' WHERE {identity_column}=?",  # noqa: S608
            (identity,),
        )
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority facts are incomplete") as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-target-invalid-outcome-{table}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id=f"request-target-invalid-outcome-{table}",
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == expected_detail
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("started_at", "ended_at"),
    [
        ("2026-08-17T07:00:00Z", None),
        ("2026-08-17T08:00:00Z", "2026-08-17T07:30:00Z"),
        ("2026-08-17T07:00:00Z", "2026-08-17T09:00:00.001Z"),
    ],
    ids=["ended-null", "ended-before-started", "ended-future"],
)
@pytest.mark.asyncio
async def test_target_close_rejects_invalid_terminal_attempt_chronology(
    started_at: str,
    ended_at: str | None,
) -> None:
    """目标收口必须复验历史 Attempt 时间线，并整笔回滚 barrier/Run/Task/event。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        connection.execute(
            "UPDATE attempts SET started_at=?,ended_at=? WHERE attempt_id='attempt-planning-contract'",
            (started_at, ended_at),
        )
        before = _transaction_projection_snapshot(connection)
        with pytest.raises(BarrierMilestoneCommitError) as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-target-invalid-chronology"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-target-invalid-chronology",
                )
            )
        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == "attempt chronology is invalid"
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_closes_run_without_selecting_successor() -> None:
    """达到 RunSpec targetStage 时必须原子收口 Run，不得把 Task 标成成功后继续执行。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
async def test_target_close_uses_one_authority_instant_across_expiry_boundary() -> None:
    """gate 与 terminal authority 必须复用同一瞬时，不能跨 expiry 产生 TOCTOU 提交。"""
    connection = _connection()
    try:
        planning_barrier_id = _prepare_target_close_expiry_boundary(connection)
        before_expiry = datetime(2026, 8, 17, 8, 59, 59, 999999, tzinfo=UTC)
        expiry_boundary = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        clock = _RecordingClock(before_expiry, expiry_boundary)
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="target termination authority is incomplete"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-single-authority-now"),
                now=clock,
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-single-authority-now",
                )
            )

        assert clock.calls == [before_expiry]
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.parametrize("at_expiry", [False, True], ids=["before-expiry-active", "at-expiry-inactive"])
@pytest.mark.asyncio
async def test_target_close_expiry_boundary_uses_exactly_one_clock_read(at_expiry: bool) -> None:
    """严格 now < expires 为 active；边界相等即 inactive，且两条路径都只能读取一次时钟。"""
    connection = _connection()
    try:
        planning_barrier_id = _prepare_target_close_expiry_boundary(connection)
        authority_now = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)
        if not at_expiry:
            authority_now = datetime(2026, 8, 17, 8, 59, 59, 999999, tzinfo=UTC)
        clock = _RecordingClock(authority_now)
        before = _transaction_projection_snapshot(connection)
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids(f"state-event-expiry-boundary-{at_expiry}"),
            now=clock,
        )
        request = _target_close_request(
            planning_barrier_id=planning_barrier_id,
            request_id=f"request-expiry-boundary-{at_expiry}",
        )

        if at_expiry:
            result = await service.commit_passed_barrier(request)
            assert result.run.observed_state is RunObservedState.TERMINATED
            assert result.task.lifecycle.value == "SUCCEEDED"
            assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (3,)
        else:
            with pytest.raises(BarrierMilestoneCommitError, match="target termination authority is incomplete"):
                await service.commit_passed_barrier(request)
            assert _transaction_projection_snapshot(connection) == before
        assert clock.calls == [authority_now]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "invalid_now",
    [
        datetime(2026, 8, 17, 9, 0),
        "2026-08-17T09:00:00Z",
        datetime(2026, 8, 17, 9, 0, tzinfo=_InvalidOffsetTimezone()),
        datetime(2026, 8, 17, 9, 0, tzinfo=_BrokenOffsetTimezone()),
    ],
    ids=["naive", "not-datetime", "invalid-offset", "conversion-error"],
)
@pytest.mark.asyncio
async def test_target_close_rejects_invalid_authority_clock_before_writes(invalid_now: object) -> None:
    """非法服务端时钟必须稳定映射为 application error，并在任何 CAS/event 前整体回滚。"""
    connection = _connection()
    try:
        planning_barrier_id = _prepare_target_close_expiry_boundary(connection)
        clock = _RecordingClock(invalid_now)
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError, match="barrier authority clock is invalid"):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-invalid-authority-clock"),
                now=clock,
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-invalid-authority-clock",
                )
            )

        assert len(clock.calls) == 1
        assert clock.calls[0] is invalid_now
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_lease_without_authorization_and_rolls_back() -> None:
    """已终止 Attempt 仍持有效 lease 时，即使授权缺失也不能提交 TERMINATED。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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


@pytest.mark.parametrize(
    ("acquired_at", "heartbeat_at", "expires_at"),
    [
        ("2026-08-17T08:58:00Z", "2026-08-17T09:00:00.001Z", "2026-08-17T10:00:00Z"),
        ("2026-08-17T07:00:00Z", "2026-08-17T08:30:00Z", "2026-08-17T08:00:00Z"),
    ],
    ids=["future-heartbeat", "heartbeat-after-expiry"],
)
@pytest.mark.asyncio
async def test_target_reached_rejects_invalid_matching_lease_timeline_and_rolls_back(
    acquired_at: str,
    heartbeat_at: str,
    expires_at: str,
) -> None:
    """终局也必须验证已终止 Attempt 的每条匹配 lease，不能把损坏时间当作已失效。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_target_close_lease(connection)
        connection.execute(
            "UPDATE resource_leases SET acquired_at=?,heartbeat_at=?,expires_at=? "
            "WHERE resource_key='resource-target-lease-only'",
            (acquired_at, heartbeat_at, expires_at),
        )

        with pytest.raises(BarrierMilestoneCommitError) as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-terminal-invalid-lease-{heartbeat_at}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id=f"request-terminal-invalid-lease-{heartbeat_at}",
                )
            )
        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert caught.value.__cause__.detail == "lease time ordering is invalid"

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
async def test_target_reached_allows_valid_expired_matching_lease() -> None:
    """时间自洽且已过期的匹配 lease 是合法 inactive 历史事实，不阻断目标收口。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_target_close_lease(connection)
        connection.execute(
            "UPDATE resource_leases SET acquired_at='2026-08-17T07:00:00Z',"
            "heartbeat_at='2026-08-17T07:30:00Z',expires_at='2026-08-17T08:00:00Z' "
            "WHERE resource_key='resource-target-lease-only'"
        )

        result = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-terminal-expired-lease"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).commit_passed_barrier(
            _target_close_request(
                planning_barrier_id=planning_barrier_id,
                request_id="request-terminal-expired-lease",
            )
        )

        assert result.run.observed_state is RunObservedState.TERMINATED
        assert result.task.lifecycle.value == "SUCCEEDED"
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (3,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_authorization_without_lease_and_rolls_back() -> None:
    """独立 AVAILABLE 授权本身也必须阻断目标收口，不能依赖 lease 分支。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
            "UPDATE execution_authorizations SET attempt_id=?,executor_id=?,fencing_token=?,control_epoch=?,"
            "accepted_control_command_seq=? WHERE execution_authorization_id=?",
            ("attempt-planning-contract", "executor-planning-contract", 1, 1, 0, "auth-target-auth-only"),
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
        ).fetchone() == ("CONSUMED", 0, "2026-08-17T06:30:00Z")
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_allows_self_consistent_expired_historical_authorization() -> None:
    """同一 Task 旧 PlanRevision 的 CONSUMED/revoked/expired 授权不应阻断合法终局。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
        ).fetchone() == ("CONSUMED", 0, "2026-08-17T06:30:00Z")
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (3,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("issued_at", "expires_at", "revoked_at"),
    [
        ("2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T06:30:00Z"),
        ("2026-08-17T07:00:00Z", "2026-08-17T10:00:00Z", "2026-08-17T09:30:00Z"),
        ("2026-08-17T07:00:00Z", "2026-08-17T08:00:00Z", "2026-08-17T08:30:00Z"),
    ],
    ids=["revocation-before-issued", "future-revocation", "revocation-after-expiry"],
)
@pytest.mark.asyncio
async def test_target_reached_rejects_invalid_historical_revocation_timeline(
    issued_at: str,
    expires_at: str,
    revoked_at: str,
) -> None:
    """历史授权的未来/过期后撤销是损坏事实，不能被 CONSUMED 状态掩盖后错误收口。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        old_plan_revision_id, old_step_id, old_attempt_id = _insert_historical_attempt_graph(connection)
        _insert(
            connection,
            "execution_authorizations",
            _canonical_execution_authorization_record(
                connection,
                authorization_id="auth-target-invalid-revocation",
                plan_revision_id=old_plan_revision_id,
                step_id=old_step_id,
                attempt_id=old_attempt_id,
                node_type="PLAN",
                executor_id="executor-planning-historical",
                action_capability="repo.read",
                fencing_token=11,
                control_epoch=4,
                accepted_control_command_seq=0,
                max_uses=0,
                consumption_state="CONSUMED",
                issued_at=issued_at,
                expires_at=expires_at,
                revoked_at=revoked_at,
                revoke_reason="invalid-revocation-probe",
            ),
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids(f"state-event-terminal-revocation-{revoked_at}"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id=f"request-terminal-revocation-{revoked_at}",
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
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_noncanonical_historical_authorization() -> None:
    """历史授权 canonical bytes 损坏时必须整体回滚，不能信任仍看似自洽的 SQL selector。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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
            "UPDATE execution_authorizations SET canonical_contract=x'7b7d' "
            "WHERE execution_authorization_id='auth-target-auth-only'"
        )

        with pytest.raises(BarrierMilestoneCommitError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-terminal-noncanonical-auth"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-terminal-noncanonical-auth",
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
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_reached_rejects_active_stale_plan_authorization_and_rolls_back() -> None:
    """旧 PlanRevision 上仍有效的授权必须阻断终局，不能借历史豁免绕过 lineage。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
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


def test_barrier_authority_directly_rejects_dependency_on_future_barrier() -> None:
    """直接仓储 tooth 必须命中 future dependency，不能被后继预执行门禁代替。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, dependencies_by_node={"plan": ("design-review",)})
        current_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        future_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert_successful_design_review_step(
            connection,
            barrier_id=future_id,
            step_id="step-design-review-direct-future-dependency",
            attempt_id="attempt-design-review-direct-future-dependency",
        )
        connection.execute(
            "UPDATE steps SET dependency_hash=? WHERE logical_node_id='plan'",
            (_selector_digest(["design-review"]),),
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            with pytest.raises(WorkflowRepositoryError) as caught:
                SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                    barrier_id=current_id,
                    run_id="run-control-1",
                    task_id="task-control-1",
                    now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                )
            assert caught.value.detail == "barrier dependency points to a future barrier"
        finally:
            unit_of_work.rollback()
    finally:
        connection.close()


def test_barrier_authority_allows_successful_predecessor_dependency() -> None:
    """已通过前驱 barrier 的成功依赖仍可作为当前 required Step 的权威事实。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, dependencies_by_node={"design-review": ("plan",)})
        predecessor_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        current_id = _derived_barrier_id(connection, business_phase="DESIGN_REVIEWING", barrier_ordinal=1)
        _insert_successful_design_review_step(
            connection,
            barrier_id=current_id,
            step_id="step-design-review-predecessor-dependency",
            attempt_id="attempt-design-review-predecessor-dependency",
            dependency_ids=("plan",),
        )
        connection.execute(
            "UPDATE phase_barriers SET settled=1,passed=1,gate_digest=?,state_version=1 WHERE barrier_id=?",
            (SHA_A, predecessor_id),
        )
        connection.execute(
            "UPDATE runs SET phase='DESIGN_REVIEWING',active_barrier_id=?,state_version=1 WHERE run_id=?",
            (current_id, "run-control-1"),
        )
        unit_of_work = SqliteUnitOfWork(connection, failure_probe=lambda _point: None)
        unit_of_work.begin_immediate()
        try:
            authority = SqliteWorkflowRepository(unit_of_work).load_barrier_authority(
                barrier_id=current_id,
                run_id="run-control-1",
                task_id="task-control-1",
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            )
            decision = evaluate_barrier(
                authority.steps,
                now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                settle_deadline_at=None,
            )
            assert decision.passed is True
        finally:
            unit_of_work.rollback()
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
            await service.transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-authority-1",
                expected_state_version=0,
                executor_id="executor-authority-1",
                fencing_token=7,
                control_epoch=9,
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
        if drain_state == "DRAINING":
            _insert_control_command_ack(
                connection,
                command_id="command-authority-valid-drain",
                command_seq=1,
                acknowledged_attempt_id="attempt-authority-valid",
            )
            connection.execute(
                "UPDATE attempts SET interrupt_command_id='command-authority-valid-drain' "
                "WHERE attempt_id='attempt-authority-valid'"
            )
            connection.execute("UPDATE runs SET control_command_seq=1 WHERE run_id='run-control-1'")
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
            _canonical_execution_authorization_record(connection),
        )
        service = TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authority-valid"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        )
        if should_reject:
            with pytest.raises(TransitionPredicateError):
                await service.transition_executor_observed(
                    run_id="run-control-1",
                    attempt_id="attempt-authority-valid",
                    expected_state_version=0,
                    executor_id="executor-authority-valid",
                    fencing_token=7,
                    control_epoch=9,
                    request_id="request-authority-invalid-dispatch",
                )
            assert connection.execute("SELECT observed_state,state_version FROM runs").fetchone() == ("QUEUED", 0)
            assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (0,)
        else:
            updated = await service.transition_executor_observed(
                run_id="run-control-1",
                attempt_id="attempt-authority-valid",
                expected_state_version=0,
                executor_id="executor-authority-valid",
                fencing_token=7,
                control_epoch=9,
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
                "phase": "RECONCILING",
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
                "drain_state": "NONE",
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


@pytest.mark.asyncio
async def test_running_drained_attempt_cannot_be_hidden_as_inactive_for_queued_transition() -> None:
    """RUNNING/DRAINED 是损坏投影，不能被 SQL 过滤后伪装成零活动 Attempt。"""
    connection = _connection(desired_state="RUNNING", observed_state="RUNNING")
    try:
        _insert_active_attempt_graph(connection, drain_state="DRAINED")
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(WorkflowRepositoryError):
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-running-drained"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).transition_control_plane_observed(
                request_id="request-running-drained",
                run_id="run-control-1",
                expected_state_version=0,
                candidate=RunObservedState.QUEUED,
            )

        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_close_rejects_historical_drained_attempt_without_interrupt_lineage() -> None:
    """历史 DRAINED 必须闭合阻断命令与 ACK，不能只凭枚举值通过目标收口。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        connection.execute(
            "UPDATE attempts SET drain_state='DRAINED',interrupt_command_id=NULL "
            "WHERE attempt_id='attempt-planning-contract'"
        )
        before = _transaction_projection_snapshot(connection)

        with pytest.raises(BarrierMilestoneCommitError) as caught:
            await TransitionService(
                coordinator=_SqliteCoordinator(connection),
                state_event_id_factory=_ids("state-event-drained-without-lineage"),
                now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
            ).commit_passed_barrier(
                _target_close_request(
                    planning_barrier_id=planning_barrier_id,
                    request_id="request-drained-without-lineage",
                )
            )

        assert isinstance(caught.value.__cause__, WorkflowRepositoryError)
        assert _transaction_projection_snapshot(connection) == before
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_target_close_accepts_historical_drained_attempt_with_authoritative_interrupt_lineage() -> None:
    """闭合 blocking command 与初始 ACK 的历史 DRAINED 仍可合法完成目标收口。"""
    connection = _connection()
    try:
        _insert_passing_planning_graph(connection, run_spec_target_stage="DESIGN_APPROVED")
        _align_target_stage_authority(connection, "DESIGN_APPROVED")
        planning_barrier_id = _derived_barrier_id(connection, business_phase="PLANNING", barrier_ordinal=0)
        _insert_control_command_ack(
            connection,
            command_id="command-target-close-drain",
            command_seq=1,
            acknowledged_attempt_id="attempt-planning-contract",
        )
        connection.execute(
            "UPDATE attempts SET drain_state='DRAINED',interrupt_command_id='command-target-close-drain' "
            "WHERE attempt_id='attempt-planning-contract'"
        )
        connection.execute("UPDATE runs SET control_command_seq=1 WHERE run_id='run-control-1'")

        result = await TransitionService(
            coordinator=_SqliteCoordinator(connection),
            state_event_id_factory=_ids("state-event-authoritative-drained-target"),
            now=lambda: datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
        ).commit_passed_barrier(
            _target_close_request(
                planning_barrier_id=planning_barrier_id,
                request_id="request-authoritative-drained-target",
            )
        )

        assert result.run.observed_state is RunObservedState.TERMINATED
        assert result.task.lifecycle.value == "SUCCEEDED"
        assert connection.execute("SELECT count(*) FROM authoritative_state_events").fetchone() == (3,)
    finally:
        connection.close()
