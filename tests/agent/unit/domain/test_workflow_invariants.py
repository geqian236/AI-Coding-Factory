"""Task 1 纯领域对象、辅助领域模块与九个 port 的 RED 测试。"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import pkgutil
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import barrier_id

CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts/schemas"
AGENT_PACKAGE_DIR = Path(__file__).resolve().parents[4] / "apps/agent/src/factory_agent"


def _module(name: str) -> ModuleType:
    """延迟导入待实现模块，使缺实现保持为单项 RED 而非 collection error。"""
    return importlib.import_module(name)


def _workflow_module() -> ModuleType:
    """返回纯工作流领域模块。"""
    return _module("factory_agent.domain.workflow")


def _task_record(**overrides: object) -> dict[str, object]:
    """构造与冻结 tasks selector 完全同形的领域记录。"""
    record: dict[str, object] = {
        "task_id": "task-domain-1",
        "project_id": "project-domain-1",
        "lifecycle": "ACTIVE",
        "target_stage": "CODEX_APPROVED",
        "achieved_stage": "NONE",
        "active_plan_revision_id": None,
        "active_run_id": None,
        "state_version": 0,
        "outcome_version": 0,
    }
    record.update(overrides)
    return record


def _run_record(**overrides: object) -> dict[str, object]:
    """构造与冻结 runs selector 完全同形的领域记录。"""
    record: dict[str, object] = {
        "run_id": "run-domain-1",
        "task_id": "task-domain-1",
        "desired_state": "RUNNING",
        "observed_state": "QUEUED",
        "phase": "CREATED",
        "active_barrier_id": None,
        "dag_version": 1,
        "run_cursor": None,
        "durable_cursor": None,
        "control_command_seq": 0,
        "repair_loop_used": 0,
        "auto_replan_used": 0,
        "requires_user_action": False,
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
    }
    record.update(overrides)
    return record


def _phase_barrier_record(**overrides: object) -> dict[str, object]:
    """构造仅含运行时 identity 与 RunSpec barrier primitive 的领域记录。"""
    record: dict[str, object] = {
        "barrier_id": "bar-runtime-1",
        "run_id": "run-domain-1",
        "plan_revision_id": "plan-revision-domain-1",
        "business_phase": "PLANNING",
        "barrier_ordinal": 0,
        "required_node_set_digest": "sha256:" + "a" * 64,
        "settle_timeout_ms": 30_000,
        "settle_deadline_at": None,
        "pass_predicate_id": "planning-barrier-v1",
        "settled": False,
        "passed": False,
        "gate_digest": None,
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _step_record(**overrides: object) -> dict[str, object]:
    """构造与冻结 steps selector 完全同形的领域记录。"""
    record: dict[str, object] = {
        "step_id": "step-domain-1",
        "run_id": "run-domain-1",
        "plan_revision_id": "plan-revision-domain-1",
        "barrier_id": "bar-runtime-1",
        "logical_node_id": "plan",
        "business_phase": "PLANNING",
        "node_type": "PLAN",
        "required": True,
        "side_effect_class": "none",
        "phase": "PENDING",
        "outcome": "NONE",
        "dependency_hash": "sha256:" + "1" * 64,
        "required_artifacts_digest": "sha256:" + "2" * 64,
        "success_predicate_id": "planning-complete-v1",
        "timeout_ms": 30_000,
        "retry_policy_id": "no-retry-v1",
        "idempotency_key": "step-domain-1-v1",
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _attempt_record(**overrides: object) -> dict[str, object]:
    """构造含显式 state_version 的冻结 attempts selector 记录。"""
    record: dict[str, object] = {
        "attempt_id": "attempt-domain-1",
        "step_id": "step-domain-1",
        "supersedes_attempt_id": None,
        "phase": "CREATED",
        "outcome": "NONE",
        "executor_id": None,
        "process_session_id": None,
        "pid": None,
        "process_start_time": None,
        "job_object_id": None,
        "wsl_distro": None,
        "container_id": None,
        "image_digest": None,
        "exit_code": None,
        "termination_reason": None,
        "fencing_token": 0,
        "control_epoch": 0,
        "accepted_control_command_seq": 0,
        "interrupt_command_id": None,
        "drain_state": "NONE",
        "started_at": None,
        "ended_at": None,
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _artifact_record(**overrides: object) -> dict[str, object]:
    """构造 Task 1 只读值对象所需的 artifact selector。"""
    record: dict[str, object] = {
        "artifact_id": "artifact-domain-1",
        "producer_attempt_id": "attempt-domain-1",
        "media_type": "application/json",
        "confidentiality": "internal",
        "commit_state": "COMMITTED",
        "storage_path": "artifacts/sha256/aa/object",
        "size_bytes": 17,
        "digest": "sha256:" + "a" * 64,
        "created_at": "2026-08-12T00:00:00Z",
    }
    record.update(overrides)
    return record


def _control_command_record(**overrides: object) -> dict[str, object]:
    """构造内部持久命令；wire PAUSE 绝不能出现在此记录。"""
    record: dict[str, object] = {
        "command_id": "command-domain-1",
        "run_id": "run-domain-1",
        "command_seq": 1,
        "request_id": "request-domain-1",
        "command_type": "SOFT_PAUSE",
        "actor_id": "user-domain-1",
        "expected_state_version": 0,
        "accepted_state_version": 1,
        "issued_at": "2026-08-12T00:00:00Z",
        "acknowledged_attempt_id": "attempt-domain-1",
        "reason_digest": "sha256:" + "b" * 64,
    }
    record.update(overrides)
    return record


def _resource_lease_record(**overrides: object) -> dict[str, object]:
    """构造只承载 selector 的 lease 值对象，不实现 Task 5 调度。"""
    record: dict[str, object] = {
        "resource_key": "repo:project-domain-1",
        "owner_executor_id": "executor-domain-1",
        "fencing_token": 1,
        "control_epoch": 1,
        "acquired_at": "2026-08-12T00:00:00Z",
        "heartbeat_at": "2026-08-12T00:00:01Z",
        "expires_at": "2026-08-12T00:01:00Z",
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _target_guard_record(**overrides: object) -> dict[str, object]:
    """构造稳定资源指纹 guard；不猜测 Task 5 clear policy。"""
    record: dict[str, object] = {
        "resource_fingerprint": "sha256:" + "c" * 64,
        "guard_state": "OPEN",
        "reason_code": None,
        "evidence_digest": None,
        "created_by_release_id": None,
        "created_at": "2026-08-12T00:00:00Z",
        "cleared_by": None,
        "cleared_at": None,
        "clear_receipt_digest": None,
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _budget_clock_event_record(**overrides: object) -> dict[str, object]:
    """构造追加型 budget clock event；本阶段不计算时间差。"""
    record: dict[str, object] = {
        "run_id": "run-domain-1",
        "clock_seq": 1,
        "transition": "RUNNING_STARTED",
        "suspension_reason": None,
        "boot_id": "boot-domain-1",
        "monotonic_ns": 1_000_000,
        "wall_time": "2026-08-12T00:00:00Z",
        "state_event_id": "state-event-domain-1",
        "previous_clock_digest": None,
        "clock_digest": "sha256:" + "d" * 64,
    }
    record.update(overrides)
    return record


def _snapshot_ref(schema_id: str, character: str) -> dict[str, str]:
    """构造 schema-valid 的闭合授权快照引用。"""
    return {
        "artifactId": f"artifact-{character}",
        "schemaId": schema_id,
        "schemaVersion": "1",
        "digest": "sha256:" + character * 64,
    }


def _valid_intent_authorization() -> dict[str, Any]:
    """构造完整、真实通过 intent-authorization.v1 的合同。"""
    return {
        "intentAuthorizationId": "intent-domain-1",
        "taskId": "task-domain-1",
        "userId": "user-domain-1",
        "requirementDigest": _snapshot_ref("factory.authorization.intent.requirement.v1", "1"),
        "projectId": "project-domain-1",
        "repositoryId": "repo-domain-1",
        "repositoryBindingDigest": _snapshot_ref("factory.authorization.intent.repository-binding.v1", "2"),
        "baselineDigest": _snapshot_ref("factory.authorization.intent.baseline.v1", "3"),
        "targetStage": "CODEX_APPROVED",
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "allowedCapabilitySetDigest": _snapshot_ref("factory.authorization.intent.allowed-capability-set.v1", "4"),
        "targetBindingDigest": _snapshot_ref("factory.authorization.intent.target-binding.v1", "5"),
        "riskCeiling": "medium",
        "estimatedCostAlertDigest": _snapshot_ref("factory.authorization.intent.estimated-cost-alert.v1", "6"),
        "autonomousExecutionBudgetMs": 3_600_000,
        "repairLoopLimit": 3,
        "autoReplanLimit": 2,
        "attemptLimit": 4,
        "issuedAt": "2026-08-12T00:00:00Z",
        "expiresAt": "2026-08-13T00:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


def _valid_execution_authorization() -> dict[str, Any]:
    """构造完整、真实通过 execution-authorization.v1 的合同。"""
    return {
        "executionAuthorizationId": "execution-domain-1",
        "intentAuthorizationId": "intent-domain-1",
        "planRevisionId": "plan-revision-domain-1",
        "semanticPlanHash": _snapshot_ref("factory.authorization.execution.semantic-plan.v1", "1"),
        "planRevisionDigest": _snapshot_ref("factory.authorization.execution.plan-revision.v1", "2"),
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "stageCapabilityMapDigest": _snapshot_ref("factory.authorization.execution.stage-capability-map.v1", "3"),
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "nodeCapabilityMapDigest": _snapshot_ref("factory.authorization.execution.node-capability-map.v1", "4"),
        "runId": "run-domain-1",
        "stepId": "step-domain-1",
        "attemptId": "attempt-domain-1",
        "nodeType": "IMPLEMENT",
        "executorId": "executor-domain-1",
        "resourceFingerprint": _snapshot_ref("factory.authorization.execution.resource-fingerprint.v1", "5"),
        "capabilityScopeDigest": _snapshot_ref("factory.authorization.execution.capability-scope.v1", "6"),
        "idempotencyKey": _snapshot_ref("factory.authorization.execution.idempotency-key.v1", "7"),
        "inputBindings": {"baseSha": "a" * 40},
        "actionCapability": "worktree.write",
        "actionPolicySnapshotDigest": _snapshot_ref("factory.authorization.execution.action-policy.v1", "8"),
        "fencingToken": 1,
        "controlEpoch": 1,
        "acceptedControlCommandSeq": 0,
        "maxUses": 1,
        "consumptionState": "AVAILABLE",
        "issuedAt": "2026-08-12T00:00:00Z",
        "expiresAt": "2026-08-12T01:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


WORKFLOW_ENUMS: tuple[tuple[str, set[str]], ...] = (
    ("TaskLifecycle", {"ACTIVE", "SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK", "FAILED_NEEDS_INTERVENTION"}),
    (
        "TargetStage",
        {"DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"},
    ),
    (
        "AchievedStage",
        {"NONE", "DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"},
    ),
    ("RunDesiredState", {"RUNNING", "PAUSED", "CANCELLED"}),
    (
        "RunObservedState",
        {"QUEUED", "RUNNING", "PAUSING", "PAUSED", "STOPPING", "INTERRUPTED", "RECONCILING", "BLOCKED", "TERMINATED"},
    ),
    (
        "RunPhase",
        {
            "CREATED",
            "PREFLIGHT",
            "BOOTSTRAPPING_REPOSITORY",
            "PLANNING",
            "DESIGN_REVIEWING",
            "PREPARING_WORKSPACE",
            "IMPLEMENTING",
            "VERIFYING",
            "CODE_REVIEWING",
            "PUBLISHING_PR",
            "MERGING",
            "BUILDING_ARTIFACT",
            "DEPLOYING_STAGING",
            "ACCEPTING_STAGING",
            "DEPLOYING_PRODUCTION",
            "ACCEPTING_PRODUCTION",
            "ROLLING_BACK",
            "FINALIZING",
        },
    ),
    ("StepPhase", {"PENDING", "READY", "DISPATCHED", "RUNNING", "RECONCILING", "TERMINAL"}),
    ("StepOutcome", {"NONE", "SUCCEEDED", "FAILED", "INTERRUPTED", "SKIPPED", "CANCELLED", "UNKNOWN_REMOTE_STATE"}),
    ("AttemptPhase", {"CREATED", "STARTING", "RUNNING", "INTERRUPTING", "RECONCILING", "TERMINATED"}),
    ("AttemptOutcome", {"NONE", "SUCCEEDED", "FAILED", "INTERRUPTED", "KILLED", "LOST", "UNKNOWN_REMOTE_STATE"}),
    ("DrainState", {"NONE", "DRAINING", "DRAINED"}),
)


@pytest.mark.parametrize(("enum_name", "expected"), WORKFLOW_ENUMS)
def test_domain_enums_match_frozen_closed_sets(enum_name: str, expected: set[str]) -> None:
    """未知枚举必须在纯领域边界被拒，且五个正交维度不得折叠。"""
    enum_type = getattr(_workflow_module(), enum_name)
    assert {member.value for member in enum_type} == expected
    with pytest.raises(ValueError):
        enum_type("UNKNOWN_VALUE")


@pytest.mark.parametrize(
    ("factory_name", "record_factory"),
    [
        ("Task", _task_record),
        ("Run", _run_record),
        ("PhaseBarrier", _phase_barrier_record),
        ("Step", _step_record),
        ("Attempt", _attempt_record),
    ],
)
def test_workflow_entities_reject_mutation_of_every_selector(
    factory_name: str,
    record_factory: Callable[..., dict[str, object]],
) -> None:
    """逐字段证明领域实体不可原位改写，不用只改首字段的弱 oracle。"""
    workflow = _workflow_module()
    record = record_factory()
    entity = getattr(workflow, factory_name).from_record(record)

    for field in record:
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(entity, field, "mutated")


def test_attempt_has_explicit_state_version_for_authoritative_cas() -> None:
    """ATTEMPT 必须与另四路 aggregate 一样有真实 CAS version。"""
    workflow = _workflow_module()
    attempt = workflow.Attempt.from_record(_attempt_record(state_version=7))
    assert attempt.state_version == 7


def test_achieved_stage_advances_monotonically_without_false_target_ceiling() -> None:
    """achieved_stage 只能前进；不得错误强制 achieved<=target。"""
    workflow = _workflow_module()
    original = workflow.Task.from_record(_task_record(target_stage="DESIGN_APPROVED"))
    advanced = original.advance_achieved_stage(workflow.AchievedStage.STAGING_ACCEPTED)

    assert original.achieved_stage is workflow.AchievedStage.NONE
    assert original.state_version == 0
    assert advanced.achieved_stage is workflow.AchievedStage.STAGING_ACCEPTED
    assert advanced.target_stage is workflow.TargetStage.DESIGN_APPROVED
    assert advanced.state_version == 1
    with pytest.raises(workflow.StageRegressionError) as caught:
        advanced.advance_achieved_stage(workflow.AchievedStage.CODEX_APPROVED)
    assert caught.value.error_code == "ACHIEVED_STAGE_REGRESSION"


def test_attempt_supersession_requires_a_fresh_identity_and_version() -> None:
    """恢复或重派创建全新 Attempt，旧执行事实、identity 与 version 均不得被复用。"""
    workflow = _workflow_module()
    first = workflow.Attempt.from_record(
        _attempt_record(
            phase="TERMINATED",
            outcome="KILLED",
            executor_id="executor-old",
            process_session_id="process-session-old",
            pid=4217,
            process_start_time="2026-08-12T00:00:01Z",
            job_object_id="job-old",
            wsl_distro="Ubuntu-old",
            container_id="container-old",
            image_digest="sha256:" + "a" * 64,
            exit_code=137,
            termination_reason="old-attempt-killed",
            fencing_token=41,
            control_epoch=7,
            accepted_control_command_seq=11,
            interrupt_command_id="command-old",
            drain_state="DRAINED",
            started_at="2026-08-12T00:00:00Z",
            ended_at="2026-08-12T00:01:00Z",
            state_version=3,
        )
    )
    with pytest.raises(workflow.AttemptReuseError) as caught:
        first.supersede(
            new_attempt_id=first.attempt_id,
            fencing_token=42,
            control_epoch=8,
            accepted_control_command_seq=12,
        )
    assert caught.value.error_code == "ATTEMPT_ID_REUSE"

    successor = first.supersede(
        new_attempt_id="attempt-domain-2",
        fencing_token=42,
        control_epoch=8,
        accepted_control_command_seq=12,
    )
    assert successor.attempt_id == "attempt-domain-2"
    assert successor.supersedes_attempt_id == first.attempt_id
    assert successor.step_id == first.step_id
    assert successor.phase is workflow.AttemptPhase.CREATED
    assert successor.outcome is workflow.AttemptOutcome.NONE
    assert (
        successor.executor_id,
        successor.process_session_id,
        successor.pid,
        successor.process_start_time,
        successor.job_object_id,
        successor.wsl_distro,
        successor.container_id,
        successor.image_digest,
        successor.exit_code,
        successor.termination_reason,
        successor.interrupt_command_id,
        successor.started_at,
        successor.ended_at,
    ) == (None,) * 13
    assert successor.drain_state is workflow.DrainState.NONE
    assert successor.fencing_token == 42
    assert successor.control_epoch == 8
    assert successor.accepted_control_command_seq == 12
    assert successor.state_version == 0
    assert first.supersedes_attempt_id is None
    assert first.phase is workflow.AttemptPhase.TERMINATED
    assert first.outcome is workflow.AttemptOutcome.KILLED
    assert first.executor_id == "executor-old"
    assert first.fencing_token == 41
    assert first.control_epoch == 7
    assert first.accepted_control_command_seq == 11
    assert first.state_version == 3


@pytest.mark.parametrize(
    ("field", "replacement", "error_name", "error_code"),
    [
        pytest.param("new_attempt_id", "", "AttemptReuseError", "ATTEMPT_ID_REUSE", id="empty-id"),
        pytest.param("new_attempt_id", 42, "AttemptReuseError", "ATTEMPT_ID_REUSE", id="non-string-id"),
        pytest.param(
            "fencing_token",
            41,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="equal-fencing-token",
        ),
        pytest.param(
            "fencing_token",
            40,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="lower-fencing-token",
        ),
        pytest.param("fencing_token", -1, "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="negative-fencing"),
        pytest.param("fencing_token", True, "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="bool-fencing"),
        pytest.param("fencing_token", "42", "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="string-fencing"),
        pytest.param(
            "fencing_token",
            2**53,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="overflow-fencing",
        ),
        pytest.param("control_epoch", -1, "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="negative-epoch"),
        pytest.param("control_epoch", True, "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="bool-epoch"),
        pytest.param("control_epoch", "8", "AttemptDispatchError", "INVALID_ATTEMPT_DISPATCH", id="string-epoch"),
        pytest.param(
            "control_epoch",
            2**53,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="overflow-epoch",
        ),
        pytest.param(
            "accepted_control_command_seq",
            -1,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="negative-control-seq",
        ),
        pytest.param(
            "accepted_control_command_seq",
            True,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="bool-control-seq",
        ),
        pytest.param(
            "accepted_control_command_seq",
            "12",
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="string-control-seq",
        ),
        pytest.param(
            "accepted_control_command_seq",
            2**53,
            "AttemptDispatchError",
            "INVALID_ATTEMPT_DISPATCH",
            id="overflow-control-seq",
        ),
    ],
)
def test_attempt_supersession_rejects_invalid_identity_and_dispatch_facts(
    field: str,
    replacement: object,
    error_name: str,
    error_code: str,
) -> None:
    """新 identity 与派发整数必须严格闭合，bool/字符串也不得冒充安全整数。"""
    workflow = _workflow_module()
    first = workflow.Attempt.from_record(_attempt_record(fencing_token=41, state_version=3))
    arguments: dict[str, object] = {
        "new_attempt_id": "attempt-domain-2",
        "fencing_token": 42,
        "control_epoch": 8,
        "accepted_control_command_seq": 12,
    }
    arguments[field] = replacement
    with pytest.raises(getattr(workflow, error_name)) as caught:
        first.supersede(**arguments)
    assert caught.value.error_code == error_code
    assert first.fencing_token == 41
    assert first.state_version == 3


def test_runtime_phase_barrier_is_derived_per_run_not_copied_from_plan() -> None:
    """同一 RunSpec barrier 绑定不同 Run 时必须得到不同运行时 identity。"""
    workflow = _workflow_module()
    revision_digest = "sha256:" + "a" * 64
    plan_barrier: dict[str, object] = {
        "businessPhase": "PLANNING",
        "barrierOrdinal": 0,
        "requiredNodeIds": ["plan"],
        "settleTimeoutMs": 30_000,
        "passPredicateId": "planning-barrier-v1",
    }
    common = {
        "plan_revision_id": "plan-revision-domain-1",
        "plan_revision_digest": revision_digest,
        "plan_barrier": plan_barrier,
    }
    first = workflow.PhaseBarrier.from_plan_barrier(run_id="run-domain-1", **common)
    second = workflow.PhaseBarrier.from_plan_barrier(run_id="run-domain-2", **common)

    assert first.barrier_id == barrier_id("run-domain-1", revision_digest, "PLANNING", 0)
    assert second.barrier_id == barrier_id("run-domain-2", revision_digest, "PLANNING", 0)
    assert first.barrier_id != second.barrier_id
    assert first.required_node_ids == second.required_node_ids == ("plan",)


@pytest.mark.parametrize(
    ("module_name", "enum_name", "expected"),
    [
        ("factory_agent.domain.artifacts", "ArtifactCommitState", {"STAGING", "COMMITTED"}),
        (
            "factory_agent.domain.control",
            "ControlCommandType",
            {"SOFT_PAUSE", "IMMEDIATE_STOP", "RESUME", "CANCEL"},
        ),
        (
            "factory_agent.domain.control",
            "ControlCommandReceiptPhase",
            {"ACKNOWLEDGED", "COMPLETED", "FAILED"},
        ),
        ("factory_agent.domain.resources", "TargetGuardState", {"OPEN", "INTERVENTION_REQUIRED"}),
        (
            "factory_agent.domain.budgets",
            "BudgetClockTransition",
            {"RUNNING_STARTED", "SUSPENSION_STARTED"},
        ),
        (
            "factory_agent.domain.budgets",
            "BudgetSuspensionReason",
            {"USER_PAUSED", "REQUIRES_USER_ACTION"},
        ),
    ],
)
def test_support_domain_enums_match_frozen_closed_sets(
    module_name: str,
    enum_name: str,
    expected: set[str],
) -> None:
    """五个辅助领域模块只冻结 Master/schema 已明确的闭集。"""
    enum_type = getattr(_module(module_name), enum_name)
    assert {member.value for member in enum_type} == expected
    with pytest.raises(ValueError):
        enum_type("UNKNOWN_VALUE")


@pytest.mark.parametrize(
    ("module_name", "factory_name", "error_name", "record_factory"),
    [
        ("factory_agent.domain.artifacts", "Artifact", "ArtifactContractError", _artifact_record),
        ("factory_agent.domain.control", "ControlCommand", "ControlContractError", _control_command_record),
        ("factory_agent.domain.resources", "ResourceLease", "ResourceContractError", _resource_lease_record),
        ("factory_agent.domain.resources", "TargetGuard", "ResourceContractError", _target_guard_record),
        ("factory_agent.domain.budgets", "BudgetClockEvent", "BudgetContractError", _budget_clock_event_record),
    ],
)
def test_support_domain_records_are_exact_and_immutable(
    module_name: str,
    factory_name: str,
    error_name: str,
    record_factory: Callable[..., dict[str, object]],
) -> None:
    """辅助领域值对象逐字段不可改，且 unknown selector 必须拒绝。"""
    module = _module(module_name)
    record = record_factory()
    entity = getattr(module, factory_name).from_record(record)
    for field in record:
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            setattr(entity, field, "mutated")

    forged = dict(record)
    forged["unknown_selector"] = "must-reject"
    with pytest.raises(getattr(module, error_name)):
        getattr(module, factory_name).from_record(forged)


def test_artifact_gate_eligibility_requires_committed_complete_identity() -> None:
    """只有带完整 path/size/digest 的 COMMITTED Artifact 才能满足 Gate。"""
    artifacts = _module("factory_agent.domain.artifacts")
    committed = artifacts.Artifact.from_record(_artifact_record())
    assert committed.is_gate_eligible is True

    staging = artifacts.Artifact.from_record(_artifact_record(commit_state="STAGING"))
    assert staging.is_gate_eligible is False
    for missing in ("storage_path", "size_bytes", "digest"):
        invalid = _artifact_record()
        invalid[missing] = None
        with pytest.raises(artifacts.ArtifactContractError):
            artifacts.Artifact.from_record(invalid)


def test_control_pause_compatibility_exists_only_at_v1_wire_boundary() -> None:
    """外部冻结 PAUSE 只在 adapter 映射为内部 SOFT_PAUSE，不能渗入领域。"""
    control = _module("factory_agent.domain.control")
    assert control.from_v1_wire_command_type("PAUSE") is control.ControlCommandType.SOFT_PAUSE
    assert control.to_v1_wire_command_type(control.ControlCommandType.SOFT_PAUSE) == "PAUSE"
    assert control.from_v1_wire_command_type("IMMEDIATE_STOP") is control.ControlCommandType.IMMEDIATE_STOP

    with pytest.raises(ValueError):
        control.ControlCommandType("PAUSE")
    with pytest.raises(control.ControlContractError):
        control.ControlCommand.from_record(_control_command_record(command_type="PAUSE"))


@pytest.mark.parametrize(
    ("transition", "reason", "valid"),
    [
        ("RUNNING_STARTED", None, True),
        ("RUNNING_STARTED", "USER_PAUSED", False),
        ("SUSPENSION_STARTED", "USER_PAUSED", True),
        ("SUSPENSION_STARTED", "REQUIRES_USER_ACTION", True),
        ("SUSPENSION_STARTED", None, False),
    ],
)
def test_budget_transition_and_suspension_reason_pair_is_fail_closed(
    transition: str,
    reason: str | None,
    valid: bool,
) -> None:
    """budget event 只冻结启动/暂停配对，不提前实现 Task 3 时间算法。"""
    budgets = _module("factory_agent.domain.budgets")
    if valid:
        event = budgets.BudgetClockEvent.from_record(
            _budget_clock_event_record(transition=transition, suspension_reason=reason)
        )
        assert event.transition.value == transition
        return
    with pytest.raises(budgets.BudgetContractError):
        budgets.BudgetClockEvent.from_record(
            _budget_clock_event_record(transition=transition, suspension_reason=reason)
        )


@pytest.mark.parametrize(
    ("parser_name", "fixture_factory", "schema_id"),
    [
        ("parse_intent_authorization", _valid_intent_authorization, "intent-authorization.v1"),
        ("parse_execution_authorization", _valid_execution_authorization, "execution-authorization.v1"),
    ],
)
def test_authorization_outer_contract_uses_runtime_schema_jcs_and_deep_immutability(
    parser_name: str,
    fixture_factory: Callable[[], dict[str, Any]],
    schema_id: str,
) -> None:
    """授权外层合同必须由 runtime schema 校验后再生成不可变 JCS。"""
    authorization = _module("factory_agent.domain.authorization")
    contract = fixture_factory()
    parsed = getattr(authorization, parser_name)(contract)
    expected_bytes = canonicalize(contract)

    assert parsed.schema_id == schema_id
    assert parsed.schema_version == 1
    assert parsed.canonical_bytes == expected_bytes
    assert parsed.contract_digest == "sha256:" + hashlib.sha256(expected_bytes).hexdigest()
    assert canonicalize(parsed.contract) == expected_bytes

    first_nested = next(value for value in parsed.contract.values() if isinstance(value, Mapping))
    with pytest.raises(TypeError):
        first_nested["digest"] = "sha256:" + "f" * 64
    contract[next(key for key, value in contract.items() if isinstance(value, dict))]["digest"] = "sha256:" + "e" * 64
    assert canonicalize(parsed.contract) == expected_bytes


def test_authorization_runtime_schema_constants_are_generated_digest_bound_and_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产 parser 必须实际消费 generated schema，不得只比较常量或手写 key 表。"""
    models = _module("factory_agent.contracts.generated.models")
    authorization = _module("factory_agent.domain.authorization")
    fixtures = {
        "INTENT_AUTHORIZATION": (
            "intent-authorization.v1",
            "intent-authorization.v1.schema.json",
            "parse_intent_authorization",
            _valid_intent_authorization(),
        ),
        "EXECUTION_AUTHORIZATION": (
            "execution-authorization.v1",
            "execution-authorization.v1.schema.json",
            "parse_execution_authorization",
            _valid_execution_authorization(),
        ),
    }
    for prefix, (schema_id, filename, parser_name, fixture) in fixtures.items():
        schema_json = getattr(models, f"{prefix}_SCHEMA_JSON")
        expected_digest = getattr(models, f"{prefix}_SCHEMA_JSON_SHA256")
        actual = "sha256:" + hashlib.sha256(schema_json.encode("utf-8")).hexdigest()
        assert actual == expected_digest
        assert getattr(models, f"{prefix}_SCHEMA_ID") == schema_id
        assert getattr(models, f"{prefix}_SCHEMA_VERSION") == 1
        runtime_schema = json.loads(schema_json)
        disk_schema = json.loads((CONTRACTS_DIR / filename).read_text(encoding="utf-8"))
        assert runtime_schema == disk_schema
        jsonschema.Draft7Validator(
            runtime_schema,
            format_checker=jsonschema.FormatChecker(),
        ).validate(fixture)

        poisoned_schema = deepcopy(runtime_schema)
        poisoned_schema["properties"]["generatedSchemaConsumptionSentinel"] = {"const": "required"}
        poisoned_schema["required"] = [*poisoned_schema["required"], "generatedSchemaConsumptionSentinel"]
        poisoned_json = json.dumps(poisoned_schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        poisoned_digest = "sha256:" + hashlib.sha256(poisoned_json.encode("utf-8")).hexdigest()
        with monkeypatch.context() as patch:
            patch.setattr(models, f"{prefix}_SCHEMA_JSON", poisoned_json)
            patch.setattr(models, f"{prefix}_SCHEMA_JSON_SHA256", poisoned_digest)
            reloaded = importlib.reload(authorization)
            with pytest.raises(reloaded.AuthorizationContractError):
                getattr(reloaded, parser_name)(fixture)

        # 撤销 monkeypatch 后重载，避免被注入 validator 泄漏到后续用例。
        authorization = importlib.reload(authorization)
        getattr(authorization, parser_name)(fixture)


@pytest.mark.parametrize(
    "mutation",
    [
        "schema-draft",
        "schema-id",
        "schema-title",
        "generated-id-random",
        "generated-id-case",
        "generated-id-v999",
        "generated-version-v999",
    ],
)
def test_authorization_runtime_rejects_generated_identity_metadata_drift(
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """runtime 必须把 generated ID/version 与嵌入 schema 的精确身份同时绑定。"""
    models = _module("factory_agent.contracts.generated.models")
    authorization = _module("factory_agent.domain.authorization")
    fixtures = {
        "INTENT_AUTHORIZATION": ("parse_intent_authorization", _valid_intent_authorization()),
        "EXECUTION_AUTHORIZATION": ("parse_execution_authorization", _valid_execution_authorization()),
    }
    for prefix, (parser_name, fixture) in fixtures.items():
        try:
            with monkeypatch.context() as patch:
                if mutation.startswith("schema-"):
                    schema = json.loads(getattr(models, f"{prefix}_SCHEMA_JSON"))
                    field, replacement = {
                        "schema-draft": ("$schema", "https://json-schema.org/draft/2020-12/schema"),
                        "schema-id": ("$id", "https://poison.invalid/authorization.schema.json"),
                        "schema-title": ("title", "PoisonedAuthorization"),
                    }[mutation]
                    schema[field] = replacement
                    poisoned_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                    poisoned_digest = "sha256:" + hashlib.sha256(poisoned_json.encode("utf-8")).hexdigest()
                    patch.setattr(models, f"{prefix}_SCHEMA_JSON", poisoned_json)
                    patch.setattr(models, f"{prefix}_SCHEMA_JSON_SHA256", poisoned_digest)
                elif mutation == "generated-version-v999":
                    patch.setattr(models, f"{prefix}_SCHEMA_VERSION", 999)
                else:
                    schema_id = getattr(models, f"{prefix}_SCHEMA_ID")
                    replacement = {
                        "generated-id-random": "random-authorization.v1",
                        "generated-id-case": schema_id.swapcase(),
                        "generated-id-v999": schema_id.rsplit(".v", maxsplit=1)[0] + ".v999",
                    }[mutation]
                    patch.setattr(models, f"{prefix}_SCHEMA_ID", replacement)

                with pytest.raises(FactoryError) as caught:
                    reloaded = importlib.reload(authorization)
                    getattr(reloaded, parser_name)(fixture)
                assert caught.value.error_code == "INVALID_AUTHORIZATION_CONTRACT"
                assert caught.value.detail == "authorization validator initialization failed"
                assert str(caught.value) == (
                    "[INVALID_AUTHORIZATION_CONTRACT] authorization validator initialization failed"
                )
                assert caught.value.__cause__ is None
        finally:
            # 即使 reload 在 module 初始化中途失败，也必须恢复权威常量后的可用模块。
            authorization = importlib.reload(authorization)


@pytest.mark.parametrize(
    ("parser_name", "fixture_factory"),
    [
        ("parse_intent_authorization", _valid_intent_authorization),
        ("parse_execution_authorization", _valid_execution_authorization),
    ],
)
def test_authorization_outer_contract_rejects_unknown_field_and_non_jcs_number(
    parser_name: str,
    fixture_factory: Callable[[], dict[str, Any]],
) -> None:
    """unknown field 与超出 JCS 安全范围的数字必须在持久化前拒绝。"""
    authorization = _module("factory_agent.domain.authorization")
    contract = fixture_factory()
    contract["forgedUnknownField"] = "must-reject"
    with pytest.raises(authorization.AuthorizationContractError):
        getattr(authorization, parser_name)(contract)

    contract = fixture_factory()
    numeric_field = "autonomousExecutionBudgetMs" if "autonomousExecutionBudgetMs" in contract else "maxUses"
    contract[numeric_field] = 2**53
    with pytest.raises(authorization.AuthorizationContractError):
        getattr(authorization, parser_name)(contract)


PORT_SURFACES: dict[str, tuple[str, frozenset[str]]] = {
    "factory_agent.ports.unit_of_work": (
        "UnitOfWork",
        frozenset(),
    ),
    "factory_agent.ports.workflow_store": (
        "WorkflowStore",
        frozenset(
            {
                "insert_project",
                "get_task",
                "insert_task",
                "compare_and_set_task",
                "get_plan_revision",
                "append_plan_revision",
                "get_run",
                "insert_run",
                "compare_and_set_run",
                "get_phase_barrier",
                "insert_phase_barrier",
                "compare_and_set_phase_barrier",
                "get_step",
                "insert_step",
                "compare_and_set_step",
                "get_attempt",
                "insert_attempt",
                "compare_and_set_attempt",
            }
        ),
    ),
    "factory_agent.ports.event_store": (
        "EventStore",
        frozenset({"append_authoritative_state_event", "get_authoritative_state_event"}),
    ),
    "factory_agent.ports.authorization_store": (
        "AuthorizationStore",
        frozenset(
            {
                "append_intent_authorization",
                "get_intent_authorization",
                "append_execution_authorization",
                "get_execution_authorization",
            }
        ),
    ),
    "factory_agent.ports.resource_store": (
        "ResourceStore",
        frozenset(
            {
                "get_resource_lease",
                "insert_resource_lease",
                "compare_and_set_resource_lease",
                "get_target_guard",
                "insert_target_guard",
                "compare_and_set_target_guard",
            }
        ),
    ),
    "factory_agent.ports.object_store": ("ObjectStore", frozenset({"verify", "open_verified"})),
    "factory_agent.ports.inspectors": ("Inspector", frozenset({"inspect"})),
    "factory_agent.ports.clock": ("Clock", frozenset({"sample"})),
    "factory_agent.ports.timeline_sink": ("TimelineSink", frozenset({"publish_committed"})),
}


def _public_protocol_methods(protocol: type[object]) -> frozenset[str]:
    """提取 port 自己声明的方法，忽略 typing/dunder 实现细节。"""
    return frozenset(
        name for name, value in vars(protocol).items() if not name.startswith("_") and inspect.isfunction(value)
    )


@pytest.mark.parametrize(("module_name", "surface"), PORT_SURFACES.items())
def test_task1_declares_exact_nine_primitive_only_port_surfaces(
    module_name: str,
    surface: tuple[str, frozenset[str]],
) -> None:
    """九个 port 只冻结 Task 1 primitive，禁止偷跑 Task 2/7/8 行为。"""
    protocol_name, expected_methods = surface
    protocol = getattr(_module(module_name), protocol_name)
    assert getattr(protocol, "_is_protocol", False) is True
    assert _public_protocol_methods(protocol) == expected_methods


def test_task1_ports_package_contains_exactly_the_nine_planned_modules() -> None:
    """磁盘文件与 import discovery 都必须 exact-set，任何第十个 port 都是越界设计。"""
    ports_dir = AGENT_PACKAGE_DIR / "ports"
    expected_modules = {name.rsplit(".", 1)[1] for name in PORT_SURFACES}
    expected_files = {"__init__.py", *(f"{name}.py" for name in expected_modules)}

    assert ports_dir.is_dir()
    assert {path.name for path in ports_dir.iterdir() if path.is_file() and path.suffix == ".py"} == expected_files
    package = _module("factory_agent.ports")
    discovered = {module.name for module in pkgutil.iter_modules(package.__path__)}
    assert discovered == expected_modules


def test_unit_of_work_exposes_only_four_transactional_repositories() -> None:
    """UoW 不暴露 commit/rollback，也不把外部 object/clock/timeline 纳入事务。"""
    protocol = _module("factory_agent.ports.unit_of_work").UnitOfWork
    assert set(protocol.__annotations__) == {"workflow", "events", "authorization", "resources"}
    assert not any(hasattr(protocol, name) for name in ("begin", "commit", "rollback", "retry"))
    assert not any(hasattr(protocol, name) for name in ("objects", "inspectors", "clock", "timeline"))


def test_event_store_port_has_no_task7_batch_or_segment_api() -> None:
    """Task 1 EventStore 只能写独立 authoritative lane。"""
    protocol = _module("factory_agent.ports.event_store").EventStore
    forbidden_fragments = ("prepared", "batch", "segment", "claim", "materialize", "replay")
    assert not any(fragment in name for name in _public_protocol_methods(protocol) for fragment in forbidden_fragments)


def test_support_domain_and_port_modules_do_not_import_infrastructure_or_future_services() -> None:
    """纯领域/port 不反向依赖 SQLite、IPC、scheduler、recovery 或 Runner。"""
    module_names = (
        "factory_agent.domain.workflow",
        "factory_agent.domain.plans",
        "factory_agent.domain.artifacts",
        "factory_agent.domain.authorization",
        "factory_agent.domain.control",
        "factory_agent.domain.resources",
        "factory_agent.domain.budgets",
        *PORT_SURFACES,
    )
    banned_prefixes = (
        "sqlite3",
        "factory_agent.storage",
        "factory_agent.integrations",
        "factory_agent.scheduler",
        "factory_agent.ipc",
        "factory_agent.ui",
        "factory_agent.runners",
        "factory_agent.recovery",
        "factory_agent.application",
    )

    for name in module_names:
        module = _module(name)
        source_path = Path(module.__file__ or "")
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        assert not any(import_name.startswith(banned_prefixes) for import_name in imports), (name, sorted(imports))


def test_fixture_mutation_does_not_change_authorization_baselines() -> None:
    """测试夹具自身必须每次新建，避免前一 mutation 污染后续 schema oracle。"""
    first = _valid_execution_authorization()
    second = _valid_execution_authorization()
    first["inputBindings"]["baseSha"] = "b" * 40
    assert second["inputBindings"]["baseSha"] == "a" * 40
    assert deepcopy(second) == _valid_execution_authorization()
