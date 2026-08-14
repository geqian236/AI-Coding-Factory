"""SQLite Database/UnitOfWork 原子性、CAS 与失败闭合 RED 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

import jsonschema
import pytest
from factory_agent.domain.events import payload_digest
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import semantic_plan_hash

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
MIGRATION_NAMES = (
    "0001_workflow.sql",
    "0002_event_store.sql",
    "0003_auth_resources.sql",
    "0004_control_budget.sql",
)
SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE schema_migrations ("
    "version INTEGER PRIMARY KEY NOT NULL,"
    "name TEXT NOT NULL,"
    "checksum TEXT NOT NULL,"
    "applied_at TEXT NOT NULL"
    ") STRICT"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_DIR = REPOSITORY_ROOT / "apps/agent/src/factory_agent/storage/sqlite/migrations"
CONTRACTS_DIR = REPOSITORY_ROOT / "contracts/schemas"
STARTUP_CHILD_TIMEOUT_SECONDS = 8.0

STARTUP_FAILURE_CHILD_PROGRAM = r"""
import asyncio
import json
import pathlib
import runpy
import sqlite3
import sys

test_module_path, database_path, scenario, failure_point, backup_root = sys.argv[1:]
namespace = runpy.run_path(test_module_path)

async def main():
    timeline = []
    delegate_connect = sqlite3.connect
    kwargs = {
        "backup_root": pathlib.Path(backup_root),
        "timeline": timeline,
    }
    if scenario == "fault":
        kwargs["failing_points"] = {failure_point}
    elif scenario == "connect_failure":
        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected-connect-failure")
        delegate_connect = fail_connect
    elif scenario == "foreign_keys_in_transaction":
        def connect_in_transaction(*args, **kwargs):
            connection = sqlite3.connect(*args, **kwargs)
            connection.execute("BEGIN")
            return connection
        delegate_connect = connect_in_transaction
    elif scenario != "normal_failure":
        raise AssertionError(f"unknown startup scenario: {scenario}")
    kwargs["connect_factory"] = namespace["_traced_source_connect_factory"](
        pathlib.Path(database_path),
        timeline,
        delegate=delegate_connect,
    )

    coordinator, mutex, probe = namespace["_new_stack"](pathlib.Path(database_path), **kwargs)
    database_module = namespace["_database_module"]()
    try:
        await asyncio.wait_for(
            asyncio.wait_for(coordinator.start(), timeout=4.0),
            timeout=4.5,
        )
    except database_module.DatabaseStartupError as exc:
        error_code = exc.error_code
    else:
        raise AssertionError("startup unexpectedly succeeded")

    owner_thread = getattr(coordinator, "owner_thread", None)
    owner_alive = bool(owner_thread is not None and owner_thread.is_alive())
    if owner_alive:
        raise AssertionError("startup failure left owner thread alive")
    migration_writes = namespace["_migration_write_timeline_items"](timeline)
    print(json.dumps({
        "receiptVersion": 1,
        "kind": "sqlite-startup-failure",
        "status": "failed_closed",
        "errorCode": error_code,
        "coordinatorState": coordinator.state.name,
        "ownerThreadAlive": owner_alive,
        "mutexEvents": [name for name, _thread in mutex.events],
        "probePoints": [point for point, _thread in probe.calls],
        "timeline": timeline,
        "migrationBeginCount": sum(
            1 for item in migration_writes if item == "sql:write:BEGIN IMMEDIATE"
        ),
        "migrationWriteCount": len(migration_writes),
    }, separators=(",", ":")), flush=True)

asyncio.run(main())
"""


def _snapshot_ref(schema_id: str, character: str) -> dict[str, str]:
    """构造可解析、schema/version/digest 均完整的授权快照引用。"""
    return {
        "artifactId": f"artifact-uow-{character}",
        "schemaId": schema_id,
        "schemaVersion": "1",
        "digest": "sha256:" + character * 64,
    }


def _valid_intent_authorization(
    *,
    authorization_id: str = "intent-uow-1",
) -> dict[str, object]:
    """返回完整通过 intent-authorization.v1 的真实合同，不使用 ID-only 伪 blob。"""
    return {
        "intentAuthorizationId": authorization_id,
        "taskId": "task-uow-1",
        "userId": "user-uow-1",
        "requirementDigest": _snapshot_ref("factory.authorization.intent.requirement.v1", "1"),
        "projectId": "project-uow-1",
        "repositoryId": "repository-uow-1",
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


def _valid_execution_authorization(
    *,
    authorization_id: str = "execution-uow-1",
    intent_authorization_id: str = "intent-uow-1",
    plan_revision_id: str = "plan-uow-1",
) -> dict[str, object]:
    """返回完整通过 execution-authorization.v1 的真实 IMPLEMENT/worktree.write 合同。"""
    return {
        "executionAuthorizationId": authorization_id,
        "intentAuthorizationId": intent_authorization_id,
        "planRevisionId": plan_revision_id,
        "semanticPlanHash": _snapshot_ref("factory.authorization.execution.semantic-plan.v1", "1"),
        "planRevisionDigest": _snapshot_ref("factory.authorization.execution.plan-revision.v1", "2"),
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "stageCapabilityMapDigest": _snapshot_ref("factory.authorization.execution.stage-capability-map.v1", "3"),
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "nodeCapabilityMapDigest": _snapshot_ref("factory.authorization.execution.node-capability-map.v1", "4"),
        "runId": "run-uow-1",
        "stepId": "step-uow-1",
        "attemptId": "attempt-uow-1",
        "nodeType": "IMPLEMENT",
        "executorId": "executor-uow-1",
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


def _assert_authorization_fixture_is_real(contract: dict[str, object], schema_name: str) -> None:
    """用仓库权威 JSON Schema 和 JCS round-trip 验证测试 fixture 自身。"""
    schema = json.loads((CONTRACTS_DIR / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(contract)
    canonical = canonicalize(contract)
    assert canonicalize(json.loads(canonical.decode("utf-8"))) == canonical


def _authorization_contract_digest_blob(contract: dict[str, object], *, kind: str) -> bytes:
    """把 parser 产生的文档摘要冻结为 JCS BLOB selector，不向 STRICT BLOB 列写 TEXT。"""
    authorization = _authorization_module()
    parser = {
        "intent": authorization.parse_intent_authorization,
        "execution": authorization.parse_execution_authorization,
    }[kind]
    parsed = parser(contract)
    return canonicalize({"digest": parsed.contract_digest})


def _intent_authorization_record(contract: dict[str, object]) -> dict[str, object]:
    """把完整 IntentAuthorization 映射为 selector + 独立 JCS BLOB 持久记录。"""
    authorization = _authorization_module()
    parsed = authorization.parse_intent_authorization(contract)
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


def _execution_authorization_record(contract: dict[str, object]) -> dict[str, object]:
    """把完整 ExecutionAuthorization 映射为 selector + 独立 JCS BLOB 持久记录。"""
    authorization = _authorization_module()
    parsed = authorization.parse_execution_authorization(contract)
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


def _contract_schema(schema_name: str) -> dict[str, Any]:
    """读取仓库权威合同，测试不得复制或放宽顶层 schema。"""
    return json.loads((CONTRACTS_DIR / schema_name).read_text(encoding="utf-8"))


def _lossless_plan_revision_schema() -> dict[str, Any]:
    """以 RunSpec workPlan 子 schema 机械替换 f67 的旧 nodes/barriers aliases。"""
    run_spec_schema = _contract_schema("run-spec.v1.schema.json")
    revision_schema = _contract_schema("plan-revision.v1.schema.json")
    work_plan = run_spec_schema["properties"]["workPlan"]["properties"]
    revision_schema["properties"]["nodes"] = deepcopy(work_plan["nodes"])
    revision_schema["properties"]["barriers"] = deepcopy(work_plan["barriers"])
    return revision_schema


def _plan_revision_digest(contract: Mapping[str, object]) -> str:
    """按冻结 digest material 计算新形状 PlanRevision 摘要。"""
    material = dict(contract)
    material.pop("planRevisionDigest", None)
    material.pop("signature", None)
    return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()


def _valid_plan_bundle(
    *,
    plan_revision_id: str,
    spec_revision: int,
    parent_revision_id: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """构造完整、独立 schema-valid、JCS/digest 自洽的 RunSpec/PlanRevision。"""
    nodes = [
        {
            "logicalNodeId": "plan",
            "businessPhase": "PLANNING",
            "barrierOrdinal": 0,
            "nodeType": "PLAN",
            "required": True,
            "dependsOn": [],
            "sideEffectClass": "none",
            "requiredArtifacts": [],
            "successPredicateId": "planning-complete-v1",
            "timeoutMs": 30_000,
            "retryPolicyId": "no-retry-v1",
        },
        {
            "logicalNodeId": "implement",
            "businessPhase": "IMPLEMENTING",
            "barrierOrdinal": 1,
            "nodeType": "IMPLEMENT",
            "required": True,
            "dependsOn": ["plan"],
            "sideEffectClass": "workspace_write",
            "requiredArtifacts": ["candidate-tree"],
            "successPredicateId": "implementation-complete-v1",
            "timeoutMs": 120_000,
            "retryPolicyId": "bounded-retry-v1",
        },
    ]
    barriers = [
        {
            "businessPhase": "PLANNING",
            "barrierOrdinal": 0,
            "requiredNodeIds": ["plan"],
            "settleTimeoutMs": 30_000,
            "passPredicateId": "planning-barrier-v1",
        },
        {
            "businessPhase": "IMPLEMENTING",
            "barrierOrdinal": 1,
            "requiredNodeIds": ["implement"],
            "settleTimeoutMs": 120_000,
            "passPredicateId": "implementation-barrier-v1",
        },
    ]
    run_spec: dict[str, object] = {
        "schemaVersion": 1,
        "specRevision": spec_revision,
        "parentRevisionId": parent_revision_id,
        "taskId": "task-uow-1",
        "goal": "验证 SQLite UoW 的五路 CAS 与权威事件原子性",
        "assumptions": ["Task 1 合同已冻结"],
        "scope": {"include": ["apps/agent/src/factory_agent/storage/sqlite"], "exclude": ["Task 2+"]},
        "constraints": ["单 writer"],
        "acceptanceCriteria": ["CAS 与 state.changed 一一配对"],
        "targetStage": "CODEX_APPROVED",
        "repository": {
            "mode": "existing",
            "root": "D:/codex项目/AI-Coding-Factory",
            "baseBranch": "factory/bootstrap-plan",
            "baseCommit": "a" * 40,
        },
        "workPlan": {"dagVersion": 1, "nodes": nodes, "barriers": barriers},
        "riskProfile": {"level": "medium", "reasons": ["原子持久化边界"]},
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "intentAuthorizationId": "intent-uow-1",
        "semanticPlanHash": SHA_A,
        "planRevisionDigest": SHA_B,
        "createdAt": "2026-08-12T00:00:00Z",
    }
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision: dict[str, object] = {
        "planRevisionId": plan_revision_id,
        "taskId": run_spec["taskId"],
        "specRevision": spec_revision,
        "intentAuthorizationId": run_spec["intentAuthorizationId"],
        "semanticPlanHash": run_spec["semanticPlanHash"],
        "planRevisionDigest": SHA_B,
        "dagVersion": 1,
        "nodeCapabilityMapVersion": run_spec["nodeCapabilityMapVersion"],
        "stageCapabilityMapVersion": run_spec["stageCapabilityMapVersion"],
        "nodes": deepcopy(nodes),
        "barriers": deepcopy(barriers),
        "stageMaps": {"DESIGN_APPROVED": ["plan"], "CODEX_APPROVED": ["plan", "implement"]},
        "createdAt": run_spec["createdAt"],
    }
    if parent_revision_id is not None:
        revision["parentRevisionId"] = parent_revision_id
    revision["planRevisionDigest"] = _plan_revision_digest(revision)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    revision["semanticPlanHash"] = run_spec["semanticPlanHash"]
    revision["planRevisionDigest"] = _plan_revision_digest(revision)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    _assert_plan_bundle_is_real(run_spec, revision)
    return run_spec, revision


def _assert_plan_bundle_is_real(run_spec: dict[str, object], revision: dict[str, object]) -> None:
    """分别校验 schema/JCS/digest，并核对所有共同 selector 与无损 fragments。"""
    jsonschema.Draft7Validator(
        _contract_schema("run-spec.v1.schema.json"),
        format_checker=jsonschema.FormatChecker(),
    ).validate(run_spec)
    jsonschema.Draft7Validator(
        _lossless_plan_revision_schema(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(revision)
    assert run_spec["semanticPlanHash"] == semantic_plan_hash(run_spec)
    assert revision["planRevisionDigest"] == _plan_revision_digest(revision)
    assert revision["nodes"] == run_spec["workPlan"]["nodes"]  # type: ignore[index]
    assert revision["barriers"] == run_spec["workPlan"]["barriers"]  # type: ignore[index]
    for selector in (
        "taskId",
        "specRevision",
        "parentRevisionId",
        "intentAuthorizationId",
        "semanticPlanHash",
        "planRevisionDigest",
        "nodeCapabilityMapVersion",
        "stageCapabilityMapVersion",
        "createdAt",
    ):
        assert revision.get(selector) == run_spec.get(selector)
    for contract in (run_spec, revision):
        canonical = canonicalize(contract)
        assert canonicalize(json.loads(canonical.decode("utf-8"))) == canonical


def _plan_revision_record(run_spec: dict[str, object], revision: dict[str, object]) -> dict[str, object]:
    """把完整合同映射为 selector + 双 canonical blob 的无损持久记录。"""
    return {
        "plan_revision_id": revision["planRevisionId"],
        "task_id": revision["taskId"],
        "spec_revision": revision["specRevision"],
        "parent_revision_id": revision.get("parentRevisionId"),
        "intent_authorization_id": revision["intentAuthorizationId"],
        "semantic_plan_hash": revision["semanticPlanHash"],
        "plan_revision_digest": revision["planRevisionDigest"],
        "dag_version": revision["dagVersion"],
        "node_capability_map_version": revision["nodeCapabilityMapVersion"],
        "stage_capability_map_version": revision["stageCapabilityMapVersion"],
        "created_at": revision["createdAt"],
        "run_spec_schema_id": "run-spec.v1",
        "run_spec_schema_version": 1,
        "canonical_run_spec": canonicalize(run_spec),
        "plan_revision_schema_id": "plan-revision.v1",
        "plan_revision_schema_version": 1,
        "canonical_plan_revision": canonicalize(revision),
    }


def _database_module() -> ModuleType:
    """延迟导入待实现 database，避免 RED 阶段 collection error。"""
    return importlib.import_module("factory_agent.storage.sqlite.database")


def _plans_module() -> ModuleType:
    """延迟导入 PlanRevision 持久化 hydrator 与稳定错误。"""
    return importlib.import_module("factory_agent.domain.plans")


def _authorization_module() -> ModuleType:
    """延迟导入 Intent/Execution authorization 领域 parser。"""
    return importlib.import_module("factory_agent.domain.authorization")


def _coordinator_module() -> ModuleType:
    """延迟导入唯一 write coordinator。"""
    return importlib.import_module("factory_agent.scheduler.write_coordinator")


def _uow_module() -> ModuleType:
    """延迟导入 SQLite UoW 的稳定错误类型。"""
    return importlib.import_module("factory_agent.storage.sqlite.unit_of_work")


def _assert_d_test_path(path: Path) -> Path:
    """测试 DB 物理路径必须位于 D:\\codex项目。"""
    resolved = path.resolve()
    try:
        resolved.relative_to(Path("D:/codex项目").resolve())
    except ValueError as exc:
        raise AssertionError(f"测试 DB 越出 D 盘受控根：{resolved}") from exc
    return resolved


class _InjectedFailure(RuntimeError):
    """测试故障点异常；生产层必须归一为稳定错误码且不得记录正文。"""


class _CommitOutcomeUnknownConnection(sqlite3.Connection):
    """仅在业务 COMMIT 执行前模拟 ack 通道断开，保留真实活动事务供 close 检查。"""

    fail_commit = False

    def execute(
        self,
        sql: str,
        parameters: Iterable[object] = (),
        /,
    ) -> sqlite3.Cursor:
        """只拦截显式 COMMIT；其余 SQL 仍由真实 SQLite 执行。"""
        if self.fail_commit and _trace_key(sql) == ("commit",):
            raise sqlite3.OperationalError("injected-commit-outcome-unknown")
        return super().execute(sql, parameters)


class _FailureProbe:
    """记录生产路径经过的故障点，并在指定点确定性抛错。"""

    def __init__(self, failing_points: Iterable[str] = (), *, timeline: list[str] | None = None) -> None:
        self.failing_points = frozenset(failing_points)
        self.calls: list[tuple[str, int]] = []
        self.timeline = timeline if timeline is not None else []

    def __call__(self, point: str) -> None:
        """记录调用线程；命中点时抛出不含业务 payload 的测试异常。"""
        self.calls.append((point, threading.get_ident()))
        self.timeline.append(f"probe:{point}")
        if point in self.failing_points:
            raise _InjectedFailure(point)


class _RecordingLease:
    """模拟已取得的 mutex lease，并强制同一 OS 线程释放。"""

    abandoned = False

    def __init__(self, owner: _RecordingMutex) -> None:
        self._owner = owner
        self._released = False

    def release(self) -> None:
        """记录释放，若跨线程则立即使测试失败。"""
        assert threading.get_ident() == self._owner.owner_thread_id
        assert not self._released
        self._released = True
        self._owner.events.append(("release", threading.get_ident()))
        self._owner.timeline.append("mutex:release")

    def close(self) -> None:
        """模拟 CloseHandle；必须发生在 release 之后。"""
        assert self._released
        self._owner.events.append(("close_handle", threading.get_ident()))
        self._owner.timeline.append("mutex:close_handle")


class _RecordingMutex:
    """跨平台测试 mutex；只验证 coordinator 的线程和启动顺序。"""

    def __init__(self, *, timeline: list[str] | None = None) -> None:
        self.owner_thread_id: int | None = None
        self.events: list[tuple[str, int]] = []
        self.timeline = timeline if timeline is not None else []

    def acquire(self) -> _RecordingLease:
        """在 writer owner 线程记录 acquire 并返回 lease。"""
        assert self.owner_thread_id is None
        self.owner_thread_id = threading.get_ident()
        self.events.append(("acquire", self.owner_thread_id))
        self.timeline.append("mutex:acquire")
        return _RecordingLease(self)


def _controlled_backup_root(database_path: Path) -> Path:
    """为每个测试装配同一受控 D 根下的 backups 目录，禁止可选参数绕过升级前备份。"""
    return _assert_d_test_path(database_path.parent / "backups")


def _sql_timeline_item(statement: str) -> str:
    """将 trace 脱敏为读写类别与操作类型；不把 literal、表名或 identity 写入 receipt。"""
    tokens = tuple(token.upper() for token in _trace_key(statement))
    assert tokens
    verb = tokens[0]
    if verb in {"SELECT", "EXPLAIN"}:
        return f"sql:read:{verb}"
    if verb == "PRAGMA":
        pragma_name = tokens[1] if len(tokens) > 1 else "UNKNOWN"
        access = "write" if "=" in tokens else "read"
        return f"sql:{access}:PRAGMA {pragma_name}"
    if verb == "BEGIN":
        mode = tokens[1] if len(tokens) > 1 else "DEFERRED"
        return f"sql:write:BEGIN {mode}"
    return f"sql:write:{verb}"


def _traced_source_connect_factory(
    database_path: Path,
    timeline: list[str],
    *,
    delegate: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> Callable[..., sqlite3.Connection]:
    """仅给真实状态库连接安装 trace；备份目标连接不得混入 migration 顺序证据。"""
    expected = database_path.resolve()

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = delegate(*args, **kwargs)
        candidate = Path(os.fspath(args[0])).resolve() if args else None
        if candidate == expected:
            connection.set_trace_callback(lambda statement: timeline.append(_sql_timeline_item(statement)))
        return connection

    return connect


def _migration_write_timeline_items(timeline: Iterable[str]) -> tuple[str, ...]:
    """筛出 migration 全部非只读 SQL，只排除迁移前固定的三项启动 PRAGMA setter。"""
    startup_setters = {
        "sql:write:PRAGMA FOREIGN_KEYS",
        "sql:write:PRAGMA JOURNAL_MODE",
        "sql:write:PRAGMA SYNCHRONOUS",
    }
    return tuple(item for item in timeline if item.startswith("sql:write:") and item not in startup_setters)


def _new_stack(
    database_path: Path,
    *,
    backup_root: Path | None = None,
    failing_points: Iterable[str] = (),
    connect_factory: Callable[..., sqlite3.Connection] | None = None,
    timeline: list[str] | None = None,
    queue_capacity: int = 8,
) -> tuple[Any, _RecordingMutex, _FailureProbe]:
    """装配可注入故障的真实 SQLite database + 单 writer coordinator。"""
    database_module = _database_module()
    coordinator_module = _coordinator_module()
    shared_timeline = timeline if timeline is not None else []
    probe = _FailureProbe(failing_points, timeline=shared_timeline)
    database_kwargs: dict[str, object] = {"failure_probe": probe}
    if backup_root is not None:
        database_kwargs["backup_root"] = _assert_d_test_path(backup_root)
    if connect_factory is not None:
        database_kwargs["connect_factory"] = connect_factory
    database = database_module.SqliteDatabase(
        _assert_d_test_path(database_path),
        **database_kwargs,
    )
    mutex = _RecordingMutex(timeline=shared_timeline)
    coordinator = coordinator_module.WriteCoordinator(
        database=database,
        mutex=mutex,
        queue_capacity=queue_capacity,
        failure_probe=probe,
    )
    return coordinator, mutex, probe


async def _bounded[ResultT](awaitable: Awaitable[ResultT], *, timeout: float = 5.0) -> ResultT:
    """以双层硬 deadline 等待真实 writer，防止失败 RED 遗留 non-daemon owner thread。"""
    inner = asyncio.wait_for(awaitable, timeout=timeout)
    return await asyncio.wait_for(inner, timeout=timeout + 0.5)


def _startup_failure_receipt(
    database_path: Path,
    *,
    backup_root: Path,
    scenario: str,
    failure_point: str = "",
    sensitive_identities: Iterable[str] = (),
) -> dict[str, object]:
    """在可 terminate/kill 的 child 中运行 startup 失败，禁止 non-daemon owner 泄漏挂死 pytest。"""
    environment = dict(os.environ)
    agent_src = REPOSITORY_ROOT / "apps/agent/src"
    current_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(agent_src) + (os.pathsep + current_pythonpath if current_pythonpath else "")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    for variable in ("TEMP", "TMP"):
        assert variable in environment
        _assert_d_test_path(Path(environment[variable]))

    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            STARTUP_FAILURE_CHILD_PROGRAM,
            str(Path(__file__).resolve()),
            str(_assert_d_test_path(database_path)),
            scenario,
            failure_point,
            str(_assert_d_test_path(backup_root)),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=environment,
        close_fds=True,
    )
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=STARTUP_CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.terminate()
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate(timeout=2)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    assert process.poll() is not None
    assert not timed_out, f"startup child timeout; stderr={stderr!r}"
    assert process.returncode == 0, stderr
    # 失败回执只允许稳定码与阶段，不得把状态库或备份物理 identity 输出到日志。
    sensitive_paths = (database_path, backup_root)
    for sensitive_path in sensitive_paths:
        if sensitive_path is None:
            continue
        identity = str(sensitive_path.resolve())
        assert identity not in stdout
        assert identity not in stderr
    normalized_output = unicodedata.normalize("NFC", stdout + stderr).casefold()
    for identity in (database_path.name, database_path.stem, *sensitive_identities):
        normalized_identity = unicodedata.normalize("NFC", identity).casefold()
        assert normalized_identity not in normalized_output
    lines = stdout.splitlines()
    assert len(lines) == 1, f"startup child 必须仅有一条 receipt：{lines!r}"
    receipt = json.loads(lines[0])
    assert receipt["receiptVersion"] == 1
    assert receipt["kind"] == "sqlite-startup-failure"
    assert receipt["status"] == "failed_closed"
    assert receipt["ownerThreadAlive"] is False
    return receipt


def _insert(connection: sqlite3.Connection, table: str, values: dict[str, object]) -> None:
    """测试 setup 使用参数化 SQL；业务测试仍只能经 UoW repository 写。"""
    columns = tuple(values)
    quoted = ",".join(f'"{column}"' for column in columns)
    placeholders = ",".join("?" for _ in columns)
    # 表名与列名只来自本文件固定 fixture，业务值始终通过占位符绑定。
    connection.execute(
        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',  # noqa: S608
        tuple(values[column] for column in columns),
    )


class _WorkflowStorePort(Protocol):
    """仅描述回滚用例会触达的 workflow repository 端口。"""

    def compare_and_set_task(self, **changes: object) -> object:
        """以冻结 CAS 参数更新 Task；实际返回类型由生产实现决定。"""
        ...

    def compare_and_set_run(self, **changes: object) -> object:
        """以冻结 CAS 参数更新 Run。"""
        ...

    def compare_and_set_phase_barrier(self, **changes: object) -> object:
        """以冻结 CAS 参数更新 runtime barrier。"""
        ...

    def compare_and_set_step(self, **changes: object) -> object:
        """以冻结 CAS 参数更新 Step。"""
        ...

    def compare_and_set_attempt(self, **changes: object) -> object:
        """以冻结 CAS 参数更新 Attempt 自身 state_version。"""
        ...


class _EventStorePort(Protocol):
    """仅描述 authoritative state event 的同事务追加端口。"""

    def append_authoritative_state_event(self, event: Mapping[str, object]) -> object:
        """追加已通过事件合同校验的权威状态事件。"""
        ...


class _AtomicUnitOfWorkPort(Protocol):
    """测试 callback 的最小同连接 UoW 视图，不冻结额外生产 API。"""

    workflow: _WorkflowStorePort
    events: _EventStorePort


@contextmanager
def _raw_connection(database_path: Path) -> Iterator[sqlite3.Connection]:
    """只在 coordinator 完全关闭后打开 setup/断言连接。"""
    connection = sqlite3.connect(database_path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def _clone_seed_row(
    connection: sqlite3.Connection,
    table: str,
    identity_column: str,
    source_identity: str,
    **overrides: object,
) -> None:
    """克隆一条完整合法 seed row，只改显式 identity/lineage，避免 ID-only 伪 fixture。"""
    columns = tuple(str(row[1]) for row in connection.execute(f'PRAGMA table_xinfo("{table}")'))  # noqa: S608
    source = connection.execute(
        f'SELECT * FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
        (source_identity,),
    ).fetchone()
    assert source is not None
    record = dict(zip(columns, source, strict=True))
    record.update(overrides)
    _insert(connection, table, record)


def _seed_task(
    database_path: Path,
    *,
    include_phase_barrier: bool = False,
    include_execution_graph: bool = False,
    include_identity_alternates: bool = False,
) -> None:
    """在 writer 关闭时插入最小有效图，避免 setup 绕过被测 UoW 事务。"""
    intake_blob = canonicalize(
        {
            "requirementText": "UoW atomicity",
            "targetStage": "CODEX_APPROVED",
            "repositorySelection": {"mode": "existing", "baseBranch": "main"},
            "userRiskAcknowledged": True,
        }
    )
    with _raw_connection(database_path) as connection:
        _insert(connection, "projects", {"project_id": "project-uow-1"})
        _insert(
            connection,
            "tasks",
            {
                "task_id": "task-uow-1",
                "project_id": "project-uow-1",
                "lifecycle": "ACTIVE",
                "target_stage": "CODEX_APPROVED",
                "achieved_stage": "NONE",
                "active_plan_revision_id": None,
                "active_run_id": None,
                "state_version": 0,
                "outcome_version": 0,
                "intake_schema_id": "task-intake.v1",
                "intake_schema_version": 1,
                "canonical_intake": intake_blob,
                "intake_digest": payload_digest(
                    {
                        "requirementText": "UoW atomicity",
                        "targetStage": "CODEX_APPROVED",
                        "repositorySelection": {"mode": "existing", "baseBranch": "main"},
                        "userRiskAcknowledged": True,
                    }
                ),
            },
        )
        if not include_phase_barrier:
            return

        intent_contract = _valid_intent_authorization()
        _assert_authorization_fixture_is_real(
            intent_contract,
            "intent-authorization.v1.schema.json",
        )
        auth_blob = canonicalize(intent_contract)
        _insert(
            connection,
            "intent_authorizations",
            {
                "intent_authorization_id": "intent-uow-1",
                "task_id": "task-uow-1",
                "user_id": "user-uow-1",
                "requirement_digest": canonicalize(intent_contract["requirementDigest"]),
                "project_id": "project-uow-1",
                "repository_id": "repository-uow-1",
                "repository_binding_digest": canonicalize(intent_contract["repositoryBindingDigest"]),
                "baseline_digest": canonicalize(intent_contract["baselineDigest"]),
                "target_stage": "CODEX_APPROVED",
                "stage_capability_map_version": "stage-capability-map.v1",
                "allowed_capability_set_digest": canonicalize(intent_contract["allowedCapabilitySetDigest"]),
                "target_binding_digest": canonicalize(intent_contract["targetBindingDigest"]),
                "risk_ceiling": "medium",
                "estimated_cost_alert_digest": canonicalize(intent_contract["estimatedCostAlertDigest"]),
                "autonomous_execution_budget_ms": intent_contract["autonomousExecutionBudgetMs"],
                "repair_loop_limit": intent_contract["repairLoopLimit"],
                "auto_replan_limit": intent_contract["autoReplanLimit"],
                "attempt_limit": intent_contract["attemptLimit"],
                "issued_at": "2026-08-12T00:00:00Z",
                "expires_at": "2026-08-12T01:00:00Z",
                "revoked_at": None,
                "revoke_reason": None,
                "contract_schema_id": "intent-authorization.v1",
                "contract_schema_version": 1,
                "canonical_contract": auth_blob,
                "canonical_contract_digest": _authorization_contract_digest_blob(intent_contract, kind="intent"),
            },
        )
        parent_run_spec, parent_revision = _valid_plan_bundle(
            plan_revision_id="plan-uow-0",
            spec_revision=1,
            parent_revision_id=None,
        )
        run_spec, revision = _valid_plan_bundle(
            plan_revision_id="plan-uow-1",
            spec_revision=2,
            parent_revision_id="plan-uow-0",
        )
        _insert(connection, "plan_revisions", _plan_revision_record(parent_run_spec, parent_revision))
        _insert(connection, "plan_revisions", _plan_revision_record(run_spec, revision))
        _insert(
            connection,
            "runs",
            {
                "run_id": "run-uow-1",
                "task_id": "task-uow-1",
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
                "barrier_id": "bar-uow-1",
                "run_id": "run-uow-1",
                "plan_revision_id": "plan-uow-1",
                "business_phase": "PLANNING",
                "barrier_ordinal": 0,
                "required_node_set_digest": SHA_A,
                "settle_timeout_ms": 30_000,
                "settle_deadline_at": None,
                "pass_predicate_id": "planning-barrier-v1",
                "settled": 0,
                "passed": 0,
                "gate_digest": None,
                "state_version": 0,
            },
        )
        connection.execute(
            "UPDATE runs SET active_barrier_id = ? WHERE run_id = ?",
            ("bar-uow-1", "run-uow-1"),
        )
        connection.execute(
            "UPDATE tasks SET active_plan_revision_id = ?, active_run_id = ? WHERE task_id = ?",
            ("plan-uow-1", "run-uow-1", "task-uow-1"),
        )
        if not include_execution_graph:
            return

        _insert(
            connection,
            "steps",
            {
                "step_id": "step-uow-1",
                "run_id": "run-uow-1",
                "plan_revision_id": "plan-uow-1",
                "barrier_id": "bar-uow-1",
                "logical_node_id": "implement",
                "business_phase": "IMPLEMENTING",
                "node_type": "IMPLEMENT",
                "required": 1,
                "side_effect_class": "workspace_write",
                "phase": "PENDING",
                "outcome": "NONE",
                "dependency_hash": SHA_A,
                "required_artifacts_digest": SHA_A,
                "success_predicate_id": "implementation-complete-v1",
                "timeout_ms": 30_000,
                "retry_policy_id": "no-retry-v1",
                "idempotency_key": "step-uow-1-v1",
                "state_version": 0,
            },
        )
        _insert(
            connection,
            "attempts",
            {
                "attempt_id": "attempt-uow-1",
                "step_id": "step-uow-1",
                "supersedes_attempt_id": None,
                "phase": "CREATED",
                "outcome": "NONE",
                "executor_id": "executor-uow-1",
                "process_session_id": None,
                "pid": None,
                "process_start_time": None,
                "job_object_id": None,
                "wsl_distro": None,
                "container_id": None,
                "image_digest": None,
                "exit_code": None,
                "termination_reason": None,
                "fencing_token": 1,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "interrupt_command_id": None,
                "drain_state": "NONE",
                "started_at": None,
                "ended_at": None,
                "state_version": 0,
            },
        )
        execution_contract = _valid_execution_authorization()
        _assert_authorization_fixture_is_real(
            execution_contract,
            "execution-authorization.v1.schema.json",
        )
        execution_blob = canonicalize(execution_contract)
        _insert(
            connection,
            "execution_authorizations",
            {
                "execution_authorization_id": "execution-uow-1",
                "intent_authorization_id": "intent-uow-1",
                "plan_revision_id": "plan-uow-1",
                "semantic_plan_hash": canonicalize(execution_contract["semanticPlanHash"]),
                "plan_revision_digest": canonicalize(execution_contract["planRevisionDigest"]),
                "stage_capability_map_version": "stage-capability-map.v1",
                "stage_capability_map_digest": canonicalize(execution_contract["stageCapabilityMapDigest"]),
                "node_capability_map_version": "node-capability-map.v1",
                "node_capability_map_digest": canonicalize(execution_contract["nodeCapabilityMapDigest"]),
                "run_id": "run-uow-1",
                "step_id": "step-uow-1",
                "attempt_id": "attempt-uow-1",
                "node_type": "IMPLEMENT",
                "executor_id": "executor-uow-1",
                "resource_fingerprint": canonicalize(execution_contract["resourceFingerprint"]),
                "capability_scope_digest": canonicalize(execution_contract["capabilityScopeDigest"]),
                "idempotency_key": canonicalize(execution_contract["idempotencyKey"]),
                "input_bindings": canonicalize(execution_contract["inputBindings"]),
                "action_capability": "worktree.write",
                "action_policy_snapshot_digest": canonicalize(execution_contract["actionPolicySnapshotDigest"]),
                "fencing_token": 1,
                "control_epoch": 1,
                "accepted_control_command_seq": 0,
                "max_uses": 1,
                "consumption_state": "AVAILABLE",
                "issued_at": "2026-08-12T00:00:00Z",
                "expires_at": "2026-08-12T01:00:00Z",
                "revoked_at": None,
                "revoke_reason": None,
                "contract_schema_id": "execution-authorization.v1",
                "contract_schema_version": 1,
                "canonical_contract": execution_blob,
                "canonical_contract_digest": _authorization_contract_digest_blob(
                    execution_contract,
                    kind="execution",
                ),
            },
        )
        if include_identity_alternates:
            # 所有错误 identity 都指向已存在的同类 row，使失败只聚焦 CAS↔event 绑定。
            _clone_seed_row(
                connection,
                "tasks",
                "task_id",
                "task-uow-1",
                task_id="task-uow-other",
                active_plan_revision_id=None,
                active_run_id=None,
            )
            _clone_seed_row(
                connection,
                "runs",
                "run_id",
                "run-uow-1",
                run_id="run-uow-other",
                active_barrier_id=None,
            )
            _clone_seed_row(
                connection,
                "phase_barriers",
                "barrier_id",
                "bar-uow-1",
                barrier_id="bar-uow-other",
                business_phase="IMPLEMENTING",
                barrier_ordinal=1,
                required_node_set_digest=SHA_B,
                pass_predicate_id="implementation-barrier-v1",
            )
            _clone_seed_row(
                connection,
                "steps",
                "step_id",
                "step-uow-1",
                step_id="step-uow-other",
                logical_node_id="plan",
                business_phase="PLANNING",
                node_type="PLAN",
                idempotency_key="step-uow-other-v1",
            )
            _clone_seed_row(
                connection,
                "attempts",
                "attempt_id",
                "attempt-uow-1",
                attempt_id="attempt-uow-other",
                executor_id="executor-uow-other",
            )


def _bootstrap_and_seed_repository_graph(database_path: Path) -> None:
    """不依赖 G3 coordinator，用真实 Database 完成迁移后建立完整 FK 图。"""
    database_module = _database_module()
    database = database_module.SqliteDatabase(
        database_path,
        backup_root=_controlled_backup_root(database_path),
    )
    database.open()
    database.close()
    _seed_task(
        database_path,
        include_phase_barrier=True,
        include_execution_graph=True,
    )


def _repository_contract_case(
    kind: str,
) -> tuple[dict[str, object], str, str, str, str]:
    """构造三类仓储的合法记录、方法名与稳定身份。"""
    if kind == "plan_revision":
        run_spec, revision = _valid_plan_bundle(
            plan_revision_id="plan-uow-2",
            spec_revision=3,
            parent_revision_id="plan-uow-1",
        )
        return (
            _plan_revision_record(run_spec, revision),
            "workflow",
            "append_plan_revision",
            "get_plan_revision",
            "plan-uow-2",
        )
    if kind == "intent_authorization":
        contract = _valid_intent_authorization(authorization_id="intent-uow-2")
        _assert_authorization_fixture_is_real(contract, "intent-authorization.v1.schema.json")
        return (
            _intent_authorization_record(contract),
            "authorization",
            "append_intent_authorization",
            "get_intent_authorization",
            "intent-uow-2",
        )
    assert kind == "execution_authorization"
    contract = _valid_execution_authorization(authorization_id="execution-uow-2")
    _assert_authorization_fixture_is_real(contract, "execution-authorization.v1.schema.json")
    return (
        _execution_authorization_record(contract),
        "authorization",
        "append_execution_authorization",
        "get_execution_authorization",
        "execution-uow-2",
    )


def _corrupt_repository_contract_record(
    kind: str,
    record: Mapping[str, object],
    mutation: str,
) -> dict[str, object]:
    """只漂移一个 JCS/selector/digest 维度，同时保持 STRICT 类型和 FK 合法。"""
    corrupted = dict(record)
    if mutation == "noncanonical_blob":
        column = "canonical_plan_revision" if kind == "plan_revision" else "canonical_contract"
        blob = corrupted[column]
        assert isinstance(blob, bytes)
        assert blob.startswith(b"{")
        corrupted[column] = b"[" + blob[1:]
    elif mutation == "selector_drift":
        column = {
            "plan_revision": "created_at",
            "intent_authorization": "user_id",
            "execution_authorization": "executor_id",
        }[kind]
        corrupted[column] = {
            "plan_revision": "2026-08-12T00:00:01Z",
            "intent_authorization": "user-uow-X",
            "execution_authorization": "executor-uow-X",
        }[kind]
    else:
        assert mutation == "digest_drift"
        if kind == "plan_revision":
            corrupted["plan_revision_digest"] = "sha256:" + "f" * 64
        else:
            corrupted["canonical_contract_digest"] = canonicalize({"digest": "sha256:" + "f" * 64})
    return corrupted


def _repository_contract_error(kind: str) -> tuple[type[Exception], str]:
    """返回仓储边界应保留的领域错误类与稳定错误码。"""
    if kind == "plan_revision":
        return _plans_module().PlanPersistenceError, "PLAN_REVISION_PERSISTENCE_MISMATCH"
    return _authorization_module().AuthorizationContractError, "INVALID_AUTHORIZATION_CONTRACT"


def _repository_contract_error_for_mutation(
    kind: str,
    mutation: str,
) -> tuple[type[Exception], str]:
    """精确区分 Plan canonical 字节错误与 selector/digest 不一致。"""
    error_type, error_code = _repository_contract_error(kind)
    if kind == "plan_revision" and mutation == "noncanonical_blob":
        return error_type, "NONCANONICAL_PLAN_BLOB"
    return error_type, error_code


def _resource_lease_record(**overrides: object) -> dict[str, object]:
    """构造 Task1 resource lease 持久化行，用于验证仓储边界而不提前实现调度策略。"""
    record: dict[str, object] = {
        "resource_key": "repo:project-uow-1",
        "owner_executor_id": "executor-uow-1",
        "fencing_token": 1,
        "control_epoch": 1,
        "acquired_at": "2026-08-12T00:00:00Z",
        "heartbeat_at": "2026-08-12T00:00:01Z",
        "expires_at": "2026-08-12T00:01:00Z",
        "state_version": 0,
    }
    record.update(overrides)
    return record


def _task_state_event(
    *,
    state_event_id: str = "state-event-task-1",
    task_id: str = "task-uow-1",
    previous_version: int = 0,
    state_version: int = 1,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    """构造通过 f67 AuthoritativeStateEventV1 的 Task 聚合事件。"""
    actual_payload = payload or {"achievedStage": "DESIGN_APPROVED", "outcomeVersion": 1}
    return {
        "schemaVersion": 1,
        "stateEventId": state_event_id,
        "eventType": "state.changed",
        "durabilityClass": "authoritative_state",
        "taskId": task_id,
        "scope": "TASK",
        "aggregateType": "TASK",
        "aggregateId": task_id,
        "runId": None,
        "stepId": None,
        "attemptId": None,
        "previousStateVersion": previous_version,
        "stateVersion": state_version,
        "payload": actual_payload,
        "payloadDigest": payload_digest(actual_payload),
    }


def _barrier_state_event(*, aggregate_id: str, run_id: str) -> dict[str, object]:
    """构造 PHASE_BARRIER 权威事件；外表 run/aggregate 一致性由 UoW 验证。"""
    payload = {"settled": True, "passed": False}
    return {
        "schemaVersion": 1,
        "stateEventId": "state-event-barrier-1",
        "eventType": "state.changed",
        "durabilityClass": "authoritative_state",
        "taskId": "task-uow-1",
        "scope": "RUN",
        "aggregateType": "PHASE_BARRIER",
        "aggregateId": aggregate_id,
        "runId": run_id,
        "stepId": None,
        "attemptId": None,
        "previousStateVersion": 0,
        "stateVersion": 1,
        "payload": payload,
        "payloadDigest": payload_digest(payload),
    }


def _aggregate_state_event(
    aggregate_type: str,
    *,
    task_id: str = "task-uow-1",
    aggregate_id: str | None = None,
    previous_version: int = 0,
    state_version: int = 1,
) -> dict[str, object]:
    """构造五路 schema-valid state.changed，ATTEMPT 明确绑定自身 state_version。"""
    identities = {
        "TASK": ("TASK", "task-uow-1", None, None, None),
        "RUN": ("RUN", "run-uow-1", "run-uow-1", None, None),
        "PHASE_BARRIER": ("RUN", "bar-uow-1", "run-uow-1", None, None),
        "STEP": ("STEP", "step-uow-1", "run-uow-1", "step-uow-1", None),
        "ATTEMPT": (
            "ATTEMPT",
            "attempt-uow-1",
            "run-uow-1",
            "step-uow-1",
            "attempt-uow-1",
        ),
    }
    scope, default_aggregate_id, run_id, step_id, attempt_id = identities[aggregate_type]
    payload = {"projection": aggregate_type, "stateVersion": state_version}
    return {
        "schemaVersion": 1,
        "stateEventId": f"state-event-{aggregate_type.casefold()}-{state_version}",
        "eventType": "state.changed",
        "durabilityClass": "authoritative_state",
        "taskId": task_id,
        "scope": scope,
        "aggregateType": aggregate_type,
        "aggregateId": aggregate_id or default_aggregate_id,
        "runId": run_id,
        "stepId": step_id,
        "attemptId": attempt_id,
        "previousStateVersion": previous_version,
        "stateVersion": state_version,
        "payload": payload,
        "payloadDigest": payload_digest(payload),
    }


def _assert_authoritative_state_event_fixture_is_real(event: Mapping[str, object]) -> None:
    """所有 identity/version 负例先通过冻结外层 schema/JCS，避免用非法形状制造假 RED。"""
    schema = _contract_schema("authoritative-state-event.v1.schema.json")
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(event)
    canonical = canonicalize(event)
    assert canonicalize(json.loads(canonical.decode("utf-8"))) == canonical


def _drifting_state_event(aggregate_type: str, mutation: str) -> dict[str, object]:
    """只漂移一个已命名维度，保持外层 aggregateType 与其余身份形状合法。"""
    event = _aggregate_state_event(aggregate_type)
    if mutation == "same_type_wrong_aggregate":
        wrong_aggregate_id = {
            "TASK": "task-uow-other",
            "RUN": "run-uow-other",
            "PHASE_BARRIER": "bar-uow-other",
            "STEP": "step-uow-other",
            "ATTEMPT": "attempt-uow-other",
        }[aggregate_type]
        event["aggregateId"] = wrong_aggregate_id
        identity_field = {
            "TASK": "taskId",
            "RUN": "runId",
            "PHASE_BARRIER": None,
            "STEP": "stepId",
            "ATTEMPT": "attemptId",
        }[aggregate_type]
        if identity_field is not None:
            event[identity_field] = wrong_aggregate_id
    elif mutation == "wrong_task_lineage":
        event["taskId"] = "task-uow-other"
    elif mutation == "wrong_run_lineage":
        assert aggregate_type in {"RUN", "PHASE_BARRIER", "STEP", "ATTEMPT"}
        event["runId"] = "run-uow-other"
    elif mutation == "wrong_step_lineage":
        assert aggregate_type in {"STEP", "ATTEMPT"}
        event["stepId"] = "step-uow-other"
    elif mutation == "wrong_version":
        event = _aggregate_state_event(aggregate_type, previous_version=1, state_version=2)
    elif mutation == "wrong_digest":
        event["payloadDigest"] = "sha256:" + "f" * 64
    else:
        assert mutation in {"state_only", "event_only", "duplicate_event"}
    _assert_authoritative_state_event_fixture_is_real(event)
    return event


CAS_CASES: tuple[tuple[str, str, dict[str, object], str, str], ...] = (
    (
        "TASK",
        "compare_and_set_task",
        {
            "task_id": "task-uow-1",
            "expected_state_version": 0,
            "achieved_stage": "DESIGN_APPROVED",
            "outcome_version": 1,
        },
        "tasks",
        "task_id",
    ),
    (
        "RUN",
        "compare_and_set_run",
        {"run_id": "run-uow-1", "expected_state_version": 0, "observed_state": "RUNNING"},
        "runs",
        "run_id",
    ),
    (
        "PHASE_BARRIER",
        "compare_and_set_phase_barrier",
        {
            "barrier_id": "bar-uow-1",
            "expected_state_version": 0,
            "settled": True,
            "passed": False,
        },
        "phase_barriers",
        "barrier_id",
    ),
    (
        "STEP",
        "compare_and_set_step",
        {"step_id": "step-uow-1", "expected_state_version": 0, "phase": "READY"},
        "steps",
        "step_id",
    ),
    (
        "ATTEMPT",
        "compare_and_set_attempt",
        {"attempt_id": "attempt-uow-1", "expected_state_version": 0, "phase": "STARTING"},
        "attempts",
        "attempt_id",
    ),
)


def _task_snapshot(database_path: Path) -> tuple[tuple[object, ...], list[tuple[object, ...]], tuple[int, int, int]]:
    """在 writer 关闭后读取业务投影、state event 与 Task7 表计数。"""
    uri = database_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        task = connection.execute(
            "SELECT achieved_stage, state_version, outcome_version FROM tasks WHERE task_id = ?",
            ("task-uow-1",),
        ).fetchone()
        assert task is not None
        state_events = connection.execute(
            "SELECT state_event_id, previous_state_version, state_version, payload_digest "
            "FROM authoritative_state_events WHERE task_id = ? ORDER BY state_version",
            ("task-uow-1",),
        ).fetchall()
        task7_counts = (
            connection.execute("SELECT COUNT(*) FROM ingest_batches").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM stream_segments").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
        )
        return task, state_events, task7_counts  # type: ignore[return-value]
    finally:
        connection.close()


async def _bootstrap_empty_database(database_path: Path) -> tuple[Any, _RecordingMutex, _FailureProbe]:
    """经真实 coordinator 完成一次 mutex→SQLite→migrate→integrity 启动并关闭。"""
    coordinator, mutex, probe = _new_stack(
        database_path,
        backup_root=_controlled_backup_root(database_path),
    )
    await _bounded(coordinator.start())
    await _bounded(coordinator.close())
    return coordinator, mutex, probe


def _expected_migration_ledger() -> list[tuple[int, str, str]]:
    """以 migration 原始 bytes 计算带算法前缀的 exact checksum。"""
    return [
        (
            version,
            name,
            "sha256:" + hashlib.sha256((MIGRATIONS_DIR / name).read_bytes()).hexdigest(),
        )
        for version, name in enumerate(MIGRATION_NAMES, start=1)
    ]


def _migration_statements(name: str) -> tuple[str, ...]:
    """用 SQLite 自身 complete_statement 切分 raw migration，包含 trigger body。"""
    text = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    statements: list[str] = []
    buffer = ""
    for character in text:
        buffer += character
        if character == ";" and sqlite3.complete_statement(buffer):
            statements.append(buffer.strip().rstrip(";").strip())
            buffer = ""
    assert not buffer.strip(), f"{name} 存在不完整 SQL 尾部"
    assert statements, f"{name} 不得是空 migration"
    return tuple(statements)


def _trace_key(statement: str) -> tuple[str, ...]:
    """词法化 executable SQL：折叠非 literal 差异，完整保留 string literal 字节。"""
    tokens: list[str] = []
    index = 0
    while index < len(statement):
        character = statement[index]
        if character.isspace() or character == ";":
            index += 1
            continue
        if statement.startswith("--", index):
            newline = statement.find("\n", index + 2)
            index = len(statement) if newline < 0 else newline + 1
            continue
        if statement.startswith("/*", index):
            end = statement.find("*/", index + 2)
            if end < 0:
                raise AssertionError("SQL block comment 未闭合")
            index = end + 2
            continue
        if character == "'":
            end = index + 1
            while end < len(statement):
                if statement[end] != "'":
                    end += 1
                    continue
                if end + 1 < len(statement) and statement[end + 1] == "'":
                    end += 2
                    continue
                end += 1
                break
            else:
                raise AssertionError("SQL string literal 未闭合")
            tokens.append(statement[index:end])
            index = end
            continue
        if character in {'"', "`", "["}:
            closing = "]" if character == "[" else character
            end = index + 1
            decoded: list[str] = []
            while end < len(statement):
                if statement[end] != closing:
                    decoded.append(statement[end])
                    end += 1
                    continue
                if closing != "]" and end + 1 < len(statement) and statement[end + 1] == closing:
                    decoded.append(closing)
                    end += 2
                    continue
                end += 1
                break
            else:
                raise AssertionError("SQL quoted identifier 未闭合")
            tokens.append("".join(decoded).casefold())
            index = end
            continue
        if character.isalnum() or character in {"_", "$"}:
            end = index + 1
            while end < len(statement) and (statement[end].isalnum() or statement[end] in {"_", "$"}):
                end += 1
            tokens.append(statement[index:end].casefold())
            index = end
            continue
        operator = next(
            (
                candidate
                for candidate in ("->>", ">=", "<=", "<>", "!=", "==", "||", "->")
                if statement.startswith(candidate, index)
            ),
            None,
        )
        if operator is not None:
            tokens.append(operator)
            index += len(operator)
            continue
        tokens.append(character)
        index += 1
    return tuple(tokens)


def _unquote_trace_literal(token: str) -> str:
    """解码 SQLite trace 中的单引号 literal，不容忍其他表达式。"""
    assert len(token) >= 2 and token[0] == token[-1] == "'"
    return token[1:-1].replace("''", "'")


def _trace_item(statement: str) -> tuple[str, ...]:
    """将参数展开的 ledger INSERT 规范为带精确 identity/checksum 的唯一 marker。"""
    tokens = _trace_key(statement)
    ledger_shape = (
        "insert",
        "into",
        "schema_migrations",
        "(",
        "version",
        ",",
        "name",
        ",",
        "checksum",
        ",",
        "applied_at",
        ")",
        "values",
        "(",
    )
    if tokens[: len(ledger_shape)] != ledger_shape:
        return ("sql", *tokens)
    assert len(tokens) == 22 and tokens[15::2] == (",", ",", ",", ")")
    version = int(tokens[14])
    name = _unquote_trace_literal(tokens[16])
    checksum = _unquote_trace_literal(tokens[18])
    applied_at = _unquote_trace_literal(tokens[20])
    expected_version, expected_name, expected_checksum = _expected_migration_ledger()[version - 1]
    assert (version, name, checksum) == (expected_version, expected_name, expected_checksum)
    assert applied_at and "T" in applied_at
    return ("ledger", str(version), name, checksum, "<applied_at>")


def _expected_transaction_trace(prefix_count: int) -> tuple[tuple[str, ...], ...]:
    """冻结 BEGIN IMMEDIATE..COMMIT 内唯一允许的 executable 序列。"""
    expected: list[tuple[str, ...]] = [("sql", *_trace_key("BEGIN IMMEDIATE"))]
    if prefix_count == 0:
        expected.append(("sql", *_trace_key(SCHEMA_MIGRATIONS_SQL)))
    for version, name, checksum in _expected_migration_ledger()[prefix_count:]:
        expected.extend(("sql", *_trace_key(statement)) for statement in _migration_statements(name))
        expected.append(("ledger", str(version), name, checksum, "<applied_at>"))
    expected.extend(
        (
            ("sql", *_trace_key("PRAGMA foreign_key_check")),
            ("sql", *_trace_key("PRAGMA integrity_check")),
            ("sql", *_trace_key(f"PRAGMA user_version={len(MIGRATION_NAMES)}")),
            ("sql", *_trace_key("COMMIT")),
        )
    )
    return tuple(expected)


@dataclass(frozen=True)
class _DatabaseSnapshot:
    """迁移失败前后的完整 durable 视图，不只比较对象名称。"""

    schema_sql: tuple[tuple[object, ...], ...]
    ledger_rows: tuple[tuple[object, ...], ...]
    schema_version: int
    user_version: int
    row_fingerprints: tuple[tuple[str, int, str], ...]


EMPTY_DATABASE_SNAPSHOT = _DatabaseSnapshot((), (), 0, 0, ())


def _snapshot_cell(value: object) -> object:
    """把 SQLite BLOB 转为可排序、可摘要的无损测试投影。"""
    if isinstance(value, bytes):
        return {"blobHex": value.hex()}
    return value


def _schema_snapshot(database_path: Path) -> _DatabaseSnapshot:
    """读取 sqlite_schema 全 SQL、ledger 全行、版本及每张业务表行指纹。"""
    if not database_path.exists():
        return EMPTY_DATABASE_SNAPSHOT
    with closing(sqlite3.connect(database_path)) as connection:
        schema_sql = tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
        )
        table_names = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name<>'schema_migrations' ORDER BY name"
            )
        )
        fingerprints: list[tuple[str, int, str]] = []
        for table in table_names:
            rows = [
                [_snapshot_cell(value) for value in row]
                for row in connection.execute(f'SELECT * FROM "{table}"')  # noqa: S608
            ]
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            digest = hashlib.sha256(canonicalize(rows)).hexdigest()
            fingerprints.append((table, len(rows), f"sha256:{digest}"))
        has_ledger = any(row[0] == "table" and row[1] == "schema_migrations" for row in schema_sql)
        ledger_rows = (
            tuple(
                connection.execute(
                    "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
            if has_ledger
            else ()
        )
        return _DatabaseSnapshot(
            schema_sql=schema_sql,
            ledger_rows=ledger_rows,
            schema_version=int(connection.execute("PRAGMA schema_version").fetchone()[0]),
            user_version=int(connection.execute("PRAGMA user_version").fetchone()[0]),
            row_fingerprints=tuple(fingerprints),
        )


def _migration_failure_points(*, start_index: int = 0) -> tuple[str, ...]:
    """为合法前缀的每个 pending statement 生成 before/after 与 ledger/check/commit 切点。"""
    points = ["migration_transaction:before_begin_immediate"]
    if start_index == 0:
        points.extend(("migration_ledger:before_create", "migration_ledger:after_create"))
    for name in MIGRATION_NAMES[start_index:]:
        for ordinal, _statement in enumerate(_migration_statements(name), start=1):
            points.extend(
                (
                    f"migration:{name}:statement:{ordinal}:before",
                    f"migration:{name}:statement:{ordinal}:after",
                )
            )
        points.append(f"migration:{name}:after_ledger")
    points.extend(
        (
            "migration_transaction:before_foreign_key_check",
            "migration_transaction:after_foreign_key_check",
            "migration_transaction:before_integrity_check",
            "migration_transaction:after_integrity_check",
            "migration_transaction:before_commit",
        )
    )
    return tuple(points)


def _migration_probe_sequence(*, start_index: int = 0) -> tuple[str, ...]:
    """冻结 pending migration 事务的可观测 fault-point 顺序。"""
    sequence = ["migration_transaction:before_begin_immediate"]
    if start_index == 0:
        sequence.extend(("migration_ledger:before_create", "migration_ledger:after_create"))
    for name in MIGRATION_NAMES[start_index:]:
        for ordinal, _statement in enumerate(_migration_statements(name), start=1):
            sequence.extend(
                (
                    f"migration:{name}:statement:{ordinal}:before",
                    f"migration:{name}:statement:{ordinal}:after",
                )
            )
        sequence.append(f"migration:{name}:after_ledger")
    sequence.extend(
        (
            "migration_transaction:before_foreign_key_check",
            "migration_transaction:after_foreign_key_check",
            "migration_transaction:before_integrity_check",
            "migration_transaction:after_integrity_check",
            "migration_transaction:before_commit",
        )
    )
    return tuple(sequence)


def _assert_migration_probe_prefix(
    probe_points: Iterable[str],
    failure_point: str,
    *,
    start_index: int = 0,
) -> None:
    """失败点必须首次出现且 migration 子序列精确止于该点，不能越界执行。"""
    expected_sequence = _migration_probe_sequence(start_index=start_index)
    expected = expected_sequence[: expected_sequence.index(failure_point) + 1]
    actual = tuple(point for point in probe_points if point.startswith("migration"))
    assert actual == expected
    assert actual.count(failure_point) == 1


def _create_registered_prefix(database_path: Path, count: int) -> None:
    """仅为升级回滚 oracle 创建已提交的合法 migration 前缀。"""
    with closing(sqlite3.connect(database_path, isolation_level=None)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(SCHEMA_MIGRATIONS_SQL)
        for version, name, checksum in _expected_migration_ledger()[:count]:
            connection.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                (version, name, checksum, "2026-08-12T00:00:00Z"),
            )
        connection.execute(f"PRAGMA user_version={count}")


def _sqlite_backup_files(backup_root: Path) -> tuple[Path, ...]:
    """只按 SQLite 文件头发现状态库备份，避免提前冻结命名或 receipt wire。"""
    if not backup_root.exists():
        return ()

    def has_sqlite_header(path: Path) -> bool:
        """仅读固定 16 bytes，不因备份体积放大测试内存。"""
        if not path.is_file():
            return False
        with path.open("rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"

    return tuple(sorted(path for path in backup_root.rglob("*") if has_sqlite_header(path)))


def _backup_root_inventory(backup_root: Path) -> tuple[tuple[str, str, int], ...]:
    """冻结备份根内全部目录与文件，失败后任何 provisional/sidecar 残留都会改变投影。"""
    if not backup_root.exists():
        return ()
    inventory: list[tuple[str, str, int]] = []
    for path in backup_root.rglob("*"):
        relative = path.relative_to(backup_root).as_posix()
        if path.is_dir():
            inventory.append((relative, "directory", 0))
        else:
            inventory.append((relative, "file", path.stat().st_size))
    return tuple(sorted(inventory))


def _assert_rename_roundtrip(path: Path) -> None:
    """用同目录 rename 往返证明测试与生产均未泄漏 Windows 文件句柄。"""
    renamed = path.with_name(f"{path.name}.handle-check")
    assert path.exists()
    assert not renamed.exists()
    path.rename(renamed)
    try:
        renamed.rename(path)
    finally:
        if renamed.exists() and not path.exists():
            renamed.rename(path)
    assert path.exists()


@contextmanager
def _live_directory_junction(link: Path, target: Path) -> Iterator[Path]:
    """在同一受限 PowerShell 进程内创建并删除 junction，绝不递归删除目标目录。"""
    link = _assert_d_test_path(link)
    target = _assert_d_test_path(target)
    assert not link.exists()
    assert not target.exists()
    target.mkdir()
    assert target.is_dir()
    token = uuid.uuid4().hex
    ready = _assert_d_test_path(link.parent / f".junction-{token}.ready")
    ready_stage = _assert_d_test_path(link.parent / f".junction-{token}.ready-stage")
    go = _assert_d_test_path(link.parent / f".junction-{token}.go")
    assert not any(os.path.lexists(path) for path in (ready, ready_stage, go))
    script = """
$ErrorActionPreference = 'Stop'
$link = $env:R11_JUNCTION_LINK
$target = $env:R11_JUNCTION_TARGET
$ready = $env:R11_JUNCTION_READY
$readyStage = $env:R11_JUNCTION_READY_STAGE
$go = $env:R11_JUNCTION_GO
$exitCode = 0
try {
    if (-not [IO.Directory]::Exists($target)) { throw 'target-missing-before-create' }
    New-Item -ItemType Junction -Path $link -Target $target | Out-Null
    $attributes = [IO.File]::GetAttributes($link)
    if (-not ($attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'created-path-is-not-reparse-point'
    }
    [IO.File]::WriteAllText($readyStage, 'ready')
    [IO.File]::Move($readyStage, $ready)
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while (-not [IO.File]::Exists($go)) {
        if ([DateTime]::UtcNow -ge $deadline) {
            $exitCode = 25
            break
        }
        Start-Sleep -Milliseconds 10
    }
    if ($exitCode -eq 0) {
        if (-not [IO.Directory]::Exists($target)) { throw 'target-missing-before-delete' }
        $attributes = [IO.File]::GetAttributes($link)
        if (-not ($attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'link-changed-before-delete'
        }
    }
}
catch {
    $exitCode = 26
}
finally {
    if ([IO.Directory]::Exists($link)) {
        $attributes = [IO.File]::GetAttributes($link)
        if ($attributes -band [IO.FileAttributes]::ReparsePoint) {
            [IO.Directory]::Delete($link)
        }
        else {
            $exitCode = 27
        }
    }
    if (-not [IO.Directory]::Exists($target)) { $exitCode = 24 }
    if ([IO.File]::Exists($readyStage)) { [IO.File]::Delete($readyStage) }
}
exit $exitCode
"""
    environment = dict(os.environ)
    environment["R11_JUNCTION_LINK"] = str(link)
    environment["R11_JUNCTION_TARGET"] = str(target)
    environment["R11_JUNCTION_READY"] = str(ready)
    environment["R11_JUNCTION_READY_STAGE"] = str(ready_stage)
    environment["R11_JUNCTION_GO"] = str(go)
    process = subprocess.Popen(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        close_fds=True,
    )
    deadline = time.monotonic() + 5.0
    try:
        while not ready.exists():
            assert process.poll() is None, "junction helper 在发布 ready 前退出"
            assert time.monotonic() < deadline, "junction helper 创建超时"
            time.sleep(0.01)
        attributes = link.lstat().st_file_attributes
        assert attributes & 0x400
        assert target.is_dir()
        yield link
    finally:
        if not go.exists():
            go.write_bytes(b"go")
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        returncode = process.returncode
        if os.path.lexists(link):
            attributes = link.lstat().st_file_attributes
            assert attributes & 0x400
            os.rmdir(link)
        target_preserved = target.is_dir()
        for signal in (ready, ready_stage, go):
            signal.unlink(missing_ok=True)
        if target_preserved:
            target.rmdir()
        assert returncode == 0
        assert not os.path.lexists(link)
        assert target_preserved


def _assert_backup_precedes_real_migration_sql(timeline: list[str]) -> None:
    """用真实 sqlite trace 证明 verified 前没有 migration BEGIN、DDL、DML 或 user_version 写。"""
    verified = "probe:backup:verified"
    begin = "sql:write:BEGIN IMMEDIATE"
    writes = _migration_write_timeline_items(timeline)
    assert verified in timeline
    assert begin in writes
    assert writes[0] == begin
    verified_index = timeline.index(verified)
    assert verified_index < timeline.index(begin)
    assert not [item for item in timeline[:verified_index] if item in writes]
    assert all(verified_index < timeline.index(item) for item in writes)


def _sqlite_row_exists(database_path: Path, project_id: str, *, immutable: bool = False) -> bool:
    """只读重开指定 SQLite 文件并检查 WAL 业务事实，immutable 用于模拟裸主文件副本。"""
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    with closing(sqlite3.connect(database_path.resolve().as_uri() + query, uri=True)) as connection:
        return connection.execute("SELECT 1 FROM projects WHERE project_id=?", (project_id,)).fetchone() == (1,)


def _assert_reopenable_backup(backup_path: Path, expected: _DatabaseSnapshot) -> None:
    """证明备份是可独立只读重开的完整 SQLite 库，而非仅复制了主文件字节。"""
    actual = _schema_snapshot(backup_path)
    # SQLite backup 会更新目标库的内部 schema cookie；Master 冻结的是可恢复内容而非该实现计数器。
    assert actual.schema_sql == expected.schema_sql
    assert actual.ledger_rows == expected.ledger_rows
    assert actual.user_version == expected.user_version
    assert actual.row_fingerprints == expected.row_fingerprints
    uri = backup_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_count", [1, 2, 3])
async def test_schema_upgrade_backs_up_each_legal_prefix_before_migration_transaction(
    tmp_path: Path,
    prefix_count: int,
) -> None:
    """Master §11 要求本地 schema 升级前备份；Task 8 的远端发布备份不在此处发明。"""
    path = _assert_d_test_path(tmp_path / f"backup-prefix-{prefix_count}.sqlite3")
    backup_root = _assert_d_test_path(tmp_path / "backups")
    project_id = f"project-backup-{prefix_count}"
    wal_keeper: sqlite3.Connection | None = None
    bare_main_copy = _assert_d_test_path(tmp_path / "bare-main-without-wal.sqlite3")
    try:
        _create_registered_prefix(path, prefix_count)
        if prefix_count == 1:
            # WAL keeper 保持存活，业务事实只落 active WAL；裸复制主文件必须读不到它。
            wal_keeper = sqlite3.connect(path, isolation_level=None)
            assert wal_keeper.execute("PRAGMA journal_mode=wal").fetchone() == ("wal",)
            wal_keeper.execute("PRAGMA wal_autocheckpoint=0")
            _insert(wal_keeper, "projects", {"project_id": project_id})
            assert path.with_name(path.name + "-wal").stat().st_size > 0
            shutil.copyfile(path, bare_main_copy)
            assert not _sqlite_row_exists(bare_main_copy, project_id, immutable=True)
        else:
            with _raw_connection(path) as connection:
                _insert(connection, "projects", {"project_id": project_id})
        before = _schema_snapshot(path)

        timeline: list[str] = []
        coordinator, mutex, probe = _new_stack(
            path,
            backup_root=backup_root,
            connect_factory=_traced_source_connect_factory(path, timeline),
            timeline=timeline,
        )
        await _bounded(coordinator.start())
        await _bounded(coordinator.close())

        backup_files = _sqlite_backup_files(backup_root)
        assert len(backup_files) == 1
        backup_path = backup_files[0]
        _assert_reopenable_backup(backup_path, before)
        assert _sqlite_row_exists(backup_path, project_id)
        _assert_rename_roundtrip(backup_path)
        _assert_reopenable_backup(backup_path, before)
        assert _schema_snapshot(path).user_version == len(MIGRATION_NAMES)

        expected_backup_points = (
            "probe:backup:start",
            "probe:backup:created",
            "probe:backup:flushed",
            "probe:backup:verified",
        )
        actual_backup_points = tuple(item for item in probe.timeline if item.startswith("probe:backup:"))
        assert actual_backup_points == expected_backup_points
        assert probe.timeline.index("mutex:acquire") < probe.timeline.index(expected_backup_points[0])
        _assert_backup_precedes_real_migration_sql(probe.timeline)
        assert mutex.timeline is probe.timeline
    finally:
        if wal_keeper is not None:
            wal_keeper.close()
        for suffix in ("-wal", "-shm", "-journal"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        bare_main_copy.unlink(missing_ok=True)
        if path.exists():
            _assert_rename_roundtrip(path)


@pytest.mark.parametrize(
    "failure_point",
    ["backup:start", "backup:created", "backup:flushed", "backup:verified"],
)
def test_backup_stage_failure_blocks_ready_and_all_migration_writes(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """备份 create/copy/flush/verify 任一切点失败都必须在 pending migration 写入前闭合。"""
    path = _assert_d_test_path(tmp_path / f"backup-failure-{failure_point.split(':')[-1]}.sqlite3")
    backup_root = _assert_d_test_path(tmp_path / "backups")
    backup_root.mkdir()
    sentinel = backup_root / "preexisting.keep"
    sentinel.write_bytes(b"keep")
    before_inventory = _backup_root_inventory(backup_root)
    _create_registered_prefix(path, 2)
    with _raw_connection(path) as connection:
        _insert(connection, "projects", {"project_id": "project-backup-failure"})
    before = _schema_snapshot(path)

    receipt = _startup_failure_receipt(
        path,
        scenario="fault",
        failure_point=failure_point,
        backup_root=backup_root,
    )

    assert receipt["errorCode"] == "SQLITE_BACKUP_FAILED"
    assert receipt["coordinatorState"] == "FAILED"
    assert receipt["mutexEvents"] == ["acquire", "release", "close_handle"]
    assert receipt["migrationBeginCount"] == receipt["migrationWriteCount"] == 0
    timeline = receipt["timeline"]
    assert isinstance(timeline, list)
    assert _migration_write_timeline_items(timeline) == ()
    assert _schema_snapshot(path) == before
    successful_points = ("backup:start", "backup:created", "backup:flushed", "backup:verified")
    expected_prefix = successful_points[: successful_points.index(failure_point) + 1]
    probe_points = receipt["probePoints"]
    assert isinstance(probe_points, list)
    assert tuple(point for point in probe_points if point.startswith("backup:")) == expected_prefix
    assert not [point for point in probe_points if point.startswith("migration")]
    assert _backup_root_inventory(backup_root) == before_inventory
    assert sentinel.read_bytes() == b"keep"
    _assert_rename_roundtrip(path)


def test_later_migration_failure_preserves_the_verified_pre_upgrade_backup(tmp_path: Path) -> None:
    """备份成功后 migration 回滚不能删除或改写升级前副本，供 STORE-001 后续恢复使用。"""
    path = _assert_d_test_path(tmp_path / "backup-survives-migration-failure.sqlite3")
    backup_root = _assert_d_test_path(tmp_path / "backups")
    _create_registered_prefix(path, 2)
    with _raw_connection(path) as connection:
        _insert(connection, "projects", {"project_id": "project-backup-survives"})
    before = _schema_snapshot(path)
    failure_point = "migration:0003_auth_resources.sql:statement:1:after"

    receipt = _startup_failure_receipt(
        path,
        scenario="fault",
        failure_point=failure_point,
        backup_root=backup_root,
    )

    assert receipt["errorCode"] == "SQLITE_MIGRATION_FAILED"
    assert receipt["coordinatorState"] == "FAILED"
    assert _schema_snapshot(path) == before
    backup_files = _sqlite_backup_files(backup_root)
    assert len(backup_files) == 1
    backup_path = backup_files[0]
    _assert_reopenable_backup(backup_path, before)
    _assert_rename_roundtrip(backup_path)
    _assert_reopenable_backup(backup_path, before)
    _assert_rename_roundtrip(path)
    probe_points = receipt["probePoints"]
    assert isinstance(probe_points, list)
    backup_points = tuple(point for point in probe_points if point.startswith("backup:"))
    assert backup_points == ("backup:start", "backup:created", "backup:flushed", "backup:verified")
    assert probe_points.index("backup:verified") < probe_points.index("migration_transaction:before_begin_immediate")
    timeline = receipt["timeline"]
    assert isinstance(timeline, list)
    _assert_backup_precedes_real_migration_sql(timeline)
    _assert_migration_probe_prefix(probe_points, failure_point, start_index=2)


@pytest.mark.asyncio
async def test_fresh_nonexistent_database_is_backup_not_applicable(tmp_path: Path) -> None:
    """首库没有升级前状态可备份；必须显式 not_applicable 且不得制造空备份文件。"""
    path = _assert_d_test_path(tmp_path / "fresh-no-backup.sqlite3")
    backup_root = _assert_d_test_path(tmp_path / "backups")
    assert not path.exists()

    coordinator, _mutex, probe = _new_stack(path, backup_root=backup_root)
    readiness = await _bounded(coordinator.start())
    await _bounded(coordinator.close())

    assert readiness.schema_version == len(MIGRATION_NAMES)
    assert _sqlite_backup_files(backup_root) == ()
    assert not backup_root.exists() or not tuple(backup_root.rglob("*"))
    backup_points = tuple(point for point, _thread in probe.calls if point.startswith("backup:"))
    assert backup_points == ("backup:not_applicable",)
    assert probe.timeline.index("probe:backup:not_applicable") < probe.timeline.index(
        "probe:migration_transaction:before_begin_immediate"
    )


@pytest.mark.parametrize(
    "invalid_kind",
    [
        "c_drive",
        "relative",
        "dotdot",
        "database_file",
        "source_directory",
        "same_drive_cross_root",
        "prefix_confusion",
        "leaf_junction",
        "ancestor_junction",
        "omitted_prefix",
    ],
)
@pytest.mark.asyncio
async def test_backup_root_api_and_storage_location_fail_closed_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    """backup_root 可省略但不能绕过既有库备份；显式根仍须满足受控 D StorageLocation。"""
    database_module = _database_module()
    constructor = inspect.signature(database_module.SqliteDatabase.__init__)
    backup_root_parameter = constructor.parameters.get("backup_root")
    assert backup_root_parameter is not None
    assert backup_root_parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert backup_root_parameter.default is not inspect.Parameter.empty
    assert "backup_factory" not in constructor.parameters

    source_path = _assert_d_test_path(tmp_path / "path-contract-source.sqlite3")
    if invalid_kind == "omitted_prefix":
        _create_registered_prefix(source_path, 1)
        project_id = "project-omitted-backup-root"
        with _raw_connection(source_path) as connection:
            _insert(connection, "projects", {"project_id": project_id})
        before = _schema_snapshot(source_path)
        derived_backup_root = _controlled_backup_root(source_path)
        assert derived_backup_root == _assert_d_test_path(source_path.parent / "backups")
        timeline: list[str] = []
        coordinator, mutex, probe = _new_stack(
            source_path,
            connect_factory=_traced_source_connect_factory(source_path, timeline),
            timeline=timeline,
        )
        await _bounded(coordinator.start())
        await _bounded(coordinator.close())

        backup_files = _sqlite_backup_files(derived_backup_root)
        assert len(backup_files) == 1
        backup_path = backup_files[0]
        backup_path.resolve().relative_to(derived_backup_root.resolve())
        _assert_reopenable_backup(backup_path, before)
        assert _sqlite_row_exists(backup_path, project_id)
        _assert_rename_roundtrip(backup_path)
        _assert_reopenable_backup(backup_path, before)
        expected_backup_points = (
            "probe:backup:start",
            "probe:backup:created",
            "probe:backup:flushed",
            "probe:backup:verified",
        )
        assert tuple(item for item in probe.timeline if item.startswith("probe:backup:")) == (expected_backup_points)
        assert probe.timeline.index("mutex:acquire") < probe.timeline.index(expected_backup_points[0])
        _assert_backup_precedes_real_migration_sql(probe.timeline)
        assert mutex.timeline is probe.timeline
        assert _schema_snapshot(source_path).user_version == len(MIGRATION_NAMES)
        _assert_rename_roundtrip(source_path)
        return

    project_root = Path("D:/codex项目")
    reparse_target = _assert_d_test_path(tmp_path / "junction-target")
    if invalid_kind not in {"leaf_junction", "ancestor_junction"}:
        reparse_target.mkdir()
    if invalid_kind == "leaf_junction":
        junction = _assert_d_test_path(tmp_path / "backups-junction")
        invalid_root = junction
    elif invalid_kind == "ancestor_junction":
        junction = _assert_d_test_path(tmp_path / "ancestor-junction")
        invalid_root = junction / "backups"
    else:
        junction = None
        invalid_root = {
            "c_drive": Path("C:/CodexForbidden/factory-state-backups"),
            "relative": Path("relative-backups"),
            "dotdot": tmp_path / "backups" / ".." / "escaped",
            "database_file": source_path,
            "source_directory": source_path.parent,
            "same_drive_cross_root": Path("D:/factory-backups-outside-codex-project"),
            "prefix_confusion": Path("D:/codex项目-evil/backups"),
        }[invalid_kind]
    before_children = tuple(sorted(str(path) for path in tmp_path.rglob("*")))
    write_attempts: list[str] = []
    connect_attempts: list[object] = []

    def deny_directory_write(path: object, *_args: object, **_kwargs: object) -> None:
        write_attempts.append(str(path))
        raise AssertionError("backup_root 校验前不得创建目录")

    def deny_connect(*args: object, **_kwargs: object) -> sqlite3.Connection:
        connect_attempts.append(args[0] if args else None)
        raise AssertionError("backup_root 校验前不得连接或创建状态库")

    junction_context = _live_directory_junction(junction, reparse_target) if junction is not None else nullcontext()
    with junction_context:
        if junction is not None:
            assert junction.lstat().st_file_attributes & 0x400
            before_children = tuple(sorted(str(path) for path in tmp_path.rglob("*")))
        monkeypatch.setattr(os, "mkdir", deny_directory_write)
        monkeypatch.setattr(os, "makedirs", deny_directory_write)
        probe = _FailureProbe()
        with pytest.raises(database_module.DatabaseStartupError) as caught:
            database = database_module.SqliteDatabase(
                source_path,
                backup_root=invalid_root,
                connect_factory=deny_connect,
                failure_probe=probe,
            )
            database.open()

        assert caught.value.error_code == "SQLITE_BACKUP_PATH_INVALID"
        assert str(source_path) not in str(caught.value)
        assert str(invalid_root) not in str(caught.value)
        assert write_attempts == connect_attempts == []
        assert probe.calls == []
        assert not source_path.exists()
        assert tuple(sorted(str(path) for path in tmp_path.rglob("*"))) == before_children
    if junction is None:
        reparse_target.rmdir()
    assert project_root == Path("D:/codex项目")


@pytest.mark.asyncio
async def test_each_upgrade_creates_one_opaque_noncolliding_backup_under_root(tmp_path: Path) -> None:
    """同一 startup 最多一份；跨升级不碰撞且文件名不暴露 source/业务 identity。"""
    identity_parent = _assert_d_test_path(tmp_path / "Source-Secret-Ålpha" / "nested")
    identity_parent.mkdir(parents=True)
    backup_root = _assert_d_test_path(identity_parent / "backups")
    sources = (
        (_assert_d_test_path(tmp_path / "source-secret-alpha.sqlite3"), 1, "project-secret-alpha"),
        (_assert_d_test_path(tmp_path / "source-secret-beta.sqlite3"), 3, "project-secret-beta"),
    )
    previous_files: tuple[Path, ...] = ()
    expected_snapshots: dict[Path, _DatabaseSnapshot] = {}

    for source_path, prefix_count, project_id in sources:
        _create_registered_prefix(source_path, prefix_count)
        with _raw_connection(source_path) as connection:
            _insert(connection, "projects", {"project_id": project_id})
        expected = _schema_snapshot(source_path)

        coordinator, _mutex, _probe = _new_stack(source_path, backup_root=backup_root)
        await _bounded(coordinator.start())
        await _bounded(coordinator.close())

        current_files = _sqlite_backup_files(backup_root)
        new_files = tuple(path for path in current_files if path not in previous_files)
        assert len(new_files) == 1
        backup_path = new_files[0]
        backup_path.resolve().relative_to(backup_root.resolve())
        relative = unicodedata.normalize("NFC", backup_path.relative_to(backup_root).as_posix()).casefold()
        forbidden_identities = (
            source_path.name,
            source_path.stem,
            project_id,
            *source_path.parts,
        )
        for identity in forbidden_identities:
            normalized_identity = unicodedata.normalize("NFC", identity).casefold()
            assert normalized_identity not in relative
        _assert_reopenable_backup(backup_path, expected)
        _assert_rename_roundtrip(backup_path)
        _assert_reopenable_backup(backup_path, expected)
        expected_snapshots[backup_path] = _schema_snapshot(backup_path)
        for previous_path in previous_files:
            assert _schema_snapshot(previous_path) == expected_snapshots[previous_path]
        previous_files = current_files

    assert len(previous_files) == len(sources)
    assert len({path.resolve() for path in previous_files}) == len(sources)


@pytest.mark.asyncio
async def test_bootstrap_order_owner_thread_and_pragma_readback(tmp_path: Path) -> None:
    """mutex 后才 connect；连接只在非 daemon owner 线程且三项 PRAGMA 读回正确。"""
    path = _assert_d_test_path(tmp_path / "owner.sqlite3")
    connect_calls: list[tuple[int, dict[str, object]]] = []

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connect_calls.append((threading.get_ident(), dict(kwargs)))
        return sqlite3.connect(*args, **kwargs)

    coordinator, mutex, probe = _new_stack(
        path,
        backup_root=_controlled_backup_root(path),
        connect_factory=recording_connect,
    )
    readiness = await _bounded(coordinator.start())
    try:
        assert mutex.events[0][0] == "acquire"
        assert probe.calls[0][0] == "sqlite_connect"
        assert mutex.events[0][1] == connect_calls[0][0] == readiness.owner_thread_id
        assert readiness.owner_thread_id != threading.get_ident()
        assert coordinator.owner_thread.daemon is False
        assert connect_calls[0][1]["isolation_level"] is None
        assert connect_calls[0][1].get("check_same_thread", True) is True
        assert readiness.foreign_keys == 1
        assert readiness.journal_mode.casefold() == "wal"
        assert readiness.synchronous == 2
        assert readiness.schema_version == 4
    finally:
        await _bounded(coordinator.close())
    assert [name for name, _thread in mutex.events][-2:] == ["release", "close_handle"]


def test_authorization_fixtures_are_complete_schema_valid_jcs_contracts() -> None:
    """UoW fixture 必须是完整真实 Authorization/RunSpec/PlanRevision 合同。"""
    _assert_authorization_fixture_is_real(
        _valid_intent_authorization(),
        "intent-authorization.v1.schema.json",
    )
    _assert_authorization_fixture_is_real(
        _valid_execution_authorization(),
        "execution-authorization.v1.schema.json",
    )
    run_spec, revision = _valid_plan_bundle(
        plan_revision_id="plan-uow-1",
        spec_revision=2,
        parent_revision_id="plan-uow-0",
    )
    _assert_plan_bundle_is_real(run_spec, revision)


@pytest.mark.asyncio
async def test_import_time_audit_sees_one_owner_connection_and_real_cas_sql_thread(tmp_path: Path) -> None:
    """子进程在任何 import 前装 audit hook，并以一条 owner 连接执行真实 CAS+event。"""
    path = _assert_d_test_path(tmp_path / "global-connect-audit.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    event_json = json.dumps(_task_state_event(), ensure_ascii=False, separators=(",", ":"))
    script = textwrap.dedent(
        """
        import asyncio
        import json
        import pathlib
        import re
        import sys
        import threading

        audit = []
        def audit_hook(name, args):
            if name == "sqlite3.connect":
                audit.append({"thread": threading.get_ident(), "database": str(args[0])})
        sys.addaudithook(audit_hook)

        import sqlite3
        from factory_agent.scheduler.write_coordinator import WriteCoordinator
        from factory_agent.storage.sqlite.database import SqliteDatabase

        traces = []
        original_connect = sqlite3.connect
        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(lambda sql: traces.append((threading.get_ident(), sql)))
            return connection

        class Lease:
            abandoned = False
            def __init__(self, mutex):
                self.mutex = mutex
            def release(self):
                assert threading.get_ident() == self.mutex.owner_thread
            def close(self):
                assert threading.get_ident() == self.mutex.owner_thread

        class Mutex:
            def __init__(self):
                self.owner_thread = None
            def acquire(self):
                self.owner_thread = threading.get_ident()
                return Lease(self)

        CAS_SQL = re.compile(r'^UPDATE\\s+(?:"tasks"|tasks)\\s+SET\\b', re.IGNORECASE)
        EVENT_INSERT_SQL = re.compile(
            r'^INSERT\\s+INTO\\s+(?:"authoritative_state_events"|authoritative_state_events)\\s*(?:\\(|\\s)',
            re.IGNORECASE,
        )

        def unique_statement_count(pattern):
            # SQLite trace callback 会把带 trigger 的同一 top-level UPDATE 重放一次；
            # 这里按归一化 SQL 去重，同时允许生产代码为了安全引用标识符。
            return len(
                {
                    " ".join(sql.split())
                    for _thread_id, sql in traces
                    if pattern.match(sql.lstrip())
                }
            )

        async def main():
            database = SqliteDatabase(
                pathlib.Path(sys.argv[1]),
                backup_root=pathlib.Path(sys.argv[1]).parent / "backups",
                connect_factory=traced_connect,
                failure_probe=lambda _point: None,
            )
            mutex = Mutex()
            coordinator = WriteCoordinator(
                database=database,
                mutex=mutex,
                queue_capacity=4,
                failure_probe=lambda _point: None,
            )
            readiness = await asyncio.wait_for(
                asyncio.wait_for(coordinator.start(), timeout=5.0),
                timeout=5.5,
            )
            event = json.loads(sys.argv[2])
            try:
                result = await asyncio.wait_for(
                    asyncio.wait_for(
                        coordinator.execute(
                            operation="subprocess-real-cas",
                            context={"correlation_id": "corr-subprocess", "task_id": "task-uow-1"},
                            command=lambda uow: (
                                uow.workflow.compare_and_set_task(
                                    task_id="task-uow-1",
                                    expected_state_version=0,
                                    achieved_stage="DESIGN_APPROVED",
                                    outcome_version=1,
                                ),
                                uow.events.append_authoritative_state_event(event),
                            ),
                        ),
                        timeout=5.0,
                    ),
                    timeout=5.5,
                )
            finally:
                await asyncio.wait_for(
                    asyncio.wait_for(coordinator.close(), timeout=5.0),
                    timeout=5.5,
                )
            receipt = {
                "audit": audit,
                "ownerThread": readiness.owner_thread_id,
                "mutexThread": mutex.owner_thread,
                "traceThreads": sorted({thread_id for thread_id, _sql in traces}),
                "casSqlCount": unique_statement_count(CAS_SQL),
                "eventInsertCount": unique_statement_count(EVENT_INSERT_SQL),
                "stateVersion": result[0].state_version,
            }
            print("AUDIT_RECEIPT=" + json.dumps(receipt, separators=(",", ":")))

        asyncio.run(main())
        """
    )
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    source_root = str(REPOSITORY_ROOT / "apps/agent/src")
    environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), event_json],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    receipts = [line for line in completed.stdout.splitlines() if line.startswith("AUDIT_RECEIPT=")]
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].removeprefix("AUDIT_RECEIPT="))
    assert len(receipt["audit"]) == 1
    assert Path(receipt["audit"][0]["database"]).resolve() == path
    assert receipt["audit"][0]["thread"] == receipt["ownerThread"] == receipt["mutexThread"]
    assert receipt["traceThreads"] == [receipt["ownerThread"]]
    assert receipt["casSqlCount"] == receipt["eventInsertCount"] == 1
    assert receipt["stateVersion"] == 1
    assert _task_snapshot(path) == (
        ("DESIGN_APPROVED", 1, 1),
        [("state-event-task-1", 0, 1, _task_state_event()["payloadDigest"])],
        (0, 0, 0),
    )


@pytest.mark.asyncio
async def test_migration_ledger_has_exact_shape_names_and_raw_byte_checksums(tmp_path: Path) -> None:
    """schema_migrations 四列 exact，checksum 必须是 migration raw bytes 的 sha256: 值。"""
    path = _assert_d_test_path(tmp_path / "migration-ledger.sqlite3")
    await _bootstrap_empty_database(path)
    with _raw_connection(path) as connection:
        xinfo = connection.execute("PRAGMA table_xinfo(schema_migrations)").fetchall()
        assert tuple(row[1:6] for row in xinfo) == (
            ("version", "INTEGER", 1, None, 1),
            ("name", "TEXT", 1, None, 0),
            ("checksum", "TEXT", 1, None, 0),
            ("applied_at", "TEXT", 1, None, 0),
        )
        assert all(row[6] == 0 for row in xinfo)
        table_list = connection.execute("PRAGMA table_list('schema_migrations')").fetchone()
        assert table_list is not None
        assert (table_list[2], table_list[3], table_list[4], table_list[5]) == ("table", 4, 0, 1)
        ledger = connection.execute(
            "SELECT version,name,checksum,applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [row[:3] for row in ledger] == _expected_migration_ledger()
        assert all(isinstance(applied_at, str) and applied_at for *_prefix, applied_at in ledger)
        assert all(checksum.startswith("sha256:") and len(checksum) == 71 for _, _, checksum, _ in ledger)
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix_count", [0, 1, 2, 3])
async def test_each_legal_prefix_runs_exact_pending_trace_in_one_immediate_transaction(
    tmp_path: Path,
    prefix_count: int,
) -> None:
    """每个合法前缀的 pending SQL/ledger/check/user_version 序列必须完全相等。"""
    path = _assert_d_test_path(tmp_path / f"one-migration-transaction-prefix-{prefix_count}.sqlite3")
    if prefix_count:
        _create_registered_prefix(path, prefix_count)
    original_connect = sqlite3.connect
    trace: list[str] = []

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(trace.append)
        return connection

    coordinator, _mutex, _probe = _new_stack(
        path,
        backup_root=_controlled_backup_root(path),
        connect_factory=traced_connect,
    )
    await _bounded(coordinator.start())
    await _bounded(coordinator.close())

    items = tuple(_trace_item(statement) for statement in trace)
    begin = ("sql", *_trace_key("BEGIN IMMEDIATE"))
    commit = ("sql", *_trace_key("COMMIT"))
    assert items.count(begin) == items.count(commit) == 1
    begin_index = items.index(begin)
    commit_index = items.index(commit)
    assert begin_index < commit_index
    assert items[begin_index : commit_index + 1] == _expected_transaction_trace(prefix_count)

    # 迁移事务外禁止 DDL，全 trace 禁止 DROP，user_version setter 只能出现精确一次且为 4。
    outside = (*items[:begin_index], *items[commit_index + 1 :])
    assert not [item for item in outside if item[:2] in {("sql", "create"), ("sql", "alter"), ("sql", "drop")}]
    assert not [item for item in items if item[:2] == ("sql", "drop")]
    expected_user_version_reads = {
        ("sql", *_trace_key("PRAGMA user_version")),
        ("sql", *_trace_key("PRAGMA main.user_version")),
    }
    user_version_mutations = [
        item
        for item in items
        if (item[:3] == ("sql", "pragma", "user_version") or item[:5] == ("sql", "pragma", "main", ".", "user_version"))
        and item not in expected_user_version_reads
    ]
    assert user_version_mutations == [("sql", *_trace_key(f"PRAGMA user_version={len(MIGRATION_NAMES)}"))]


@pytest.mark.asyncio
async def test_p02_reopen_is_idempotent_and_does_not_rerun_migrations(tmp_path: Path) -> None:
    """第二次打开同库只核验版本/完整性，不重复执行 0001→0004。"""
    path = _assert_d_test_path(tmp_path / "reopen.sqlite3")
    first, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    first_readiness = await _bounded(first.start())
    await _bounded(first.close())

    second, _mutex2, _probe2 = _new_stack(path, backup_root=_controlled_backup_root(path))
    second_readiness = await _bounded(second.start())
    await _bounded(second.close())

    assert tuple(first_readiness.applied_migrations) == MIGRATION_NAMES
    assert tuple(second_readiness.applied_migrations) == ()
    assert second_readiness.schema_version == 4


@pytest.mark.asyncio
async def test_reopen_real_trace_has_no_ddl_or_migration_ledger_insert(tmp_path: Path) -> None:
    """第二次真实打开只审计，不得重放任何 DDL 或 schema_migrations INSERT。"""
    path = _assert_d_test_path(tmp_path / "reopen-trace.sqlite3")
    await _bootstrap_empty_database(path)
    before = _schema_snapshot(path)
    original_connect = sqlite3.connect
    trace: list[str] = []

    def traced_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(trace.append)
        return connection

    coordinator, _mutex, _probe = _new_stack(
        path,
        backup_root=_controlled_backup_root(path),
        connect_factory=traced_connect,
    )
    await _bounded(coordinator.start())
    await _bounded(coordinator.close())
    normalized = [" ".join(statement.upper().split()) for statement in trace]
    forbidden = ("CREATE ", "ALTER ", "DROP ", "INSERT INTO SCHEMA_MIGRATIONS")
    assert not [statement for statement in normalized if statement.startswith(forbidden)]
    assert _schema_snapshot(path) == before


@pytest.mark.parametrize(
    ("corruption", "error_code"),
    [
        ("future", "MIGRATION_FUTURE_VERSION"),
        ("gap", "MIGRATION_GAP"),
        ("name", "MIGRATION_NAME_MISMATCH"),
        ("checksum", "MIGRATION_CHECKSUM_MISMATCH"),
        ("unregistered_schema", "MIGRATION_UNREGISTERED_SCHEMA"),
    ],
)
@pytest.mark.asyncio
async def test_migration_ledger_and_unregistered_schema_drift_fail_closed(
    tmp_path: Path,
    corruption: str,
    error_code: str,
) -> None:
    """future/gap/name/checksum/未登记 schema 均不得被启动路径自动修补或忽略。"""
    path = _assert_d_test_path(tmp_path / f"migration-drift-{corruption}.sqlite3")
    await _bootstrap_empty_database(path)
    with _raw_connection(path) as connection:
        if corruption == "future":
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(5,?,?,?)",
                ("0005_future.sql", "sha256:" + "f" * 64, "2026-08-12T00:00:00Z"),
            )
        elif corruption == "gap":
            connection.execute("DELETE FROM schema_migrations WHERE version=2")
        elif corruption == "name":
            connection.execute("UPDATE schema_migrations SET name='renamed.sql' WHERE version=2")
        elif corruption == "checksum":
            connection.execute(
                "UPDATE schema_migrations SET checksum=? WHERE version=2",
                ("sha256:" + "f" * 64,),
            )
        else:
            connection.execute("CREATE TABLE unregistered_task1_table(id TEXT PRIMARY KEY NOT NULL)")

    before = _schema_snapshot(path)
    receipt = _startup_failure_receipt(
        path,
        scenario="normal_failure",
        backup_root=_controlled_backup_root(path),
    )
    assert receipt["errorCode"] == error_code
    assert _schema_snapshot(path) == before


@pytest.mark.parametrize("prefix_count", [0, 1, 2, 3])
def test_every_pending_statement_and_finalization_failure_restores_exact_legal_prefix(
    tmp_path: Path,
    prefix_count: int,
) -> None:
    """首库与 1–3 合法前缀都遍历每条 pending SQL 前后、ledger 后与最终检查切点。"""
    for case_ordinal, failure_point in enumerate(
        _migration_failure_points(start_index=prefix_count),
        start=1,
    ):
        path = _assert_d_test_path(tmp_path / f"prefix-{prefix_count}-fault-{case_ordinal}.sqlite3")
        if prefix_count:
            _create_registered_prefix(path, prefix_count)
        before = _schema_snapshot(path)
        receipt = _startup_failure_receipt(
            path,
            scenario="fault",
            failure_point=failure_point,
            backup_root=_controlled_backup_root(path),
        )
        assert receipt["errorCode"] == "SQLITE_MIGRATION_FAILED"
        probe_points = receipt["probePoints"]
        assert isinstance(probe_points, list)
        _assert_migration_probe_prefix(probe_points, failure_point, start_index=prefix_count)
        assert _schema_snapshot(path) == before
        if prefix_count:
            with _raw_connection(path) as connection:
                assert (
                    connection.execute(
                        "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
                    ).fetchall()
                    == _expected_migration_ledger()[:prefix_count]
                )
        else:
            assert before == EMPTY_DATABASE_SNAPSHOT


def test_n11_foreign_keys_setter_inside_transaction_fails_readback_and_blocks_ready(tmp_path: Path) -> None:
    """PRAGMA foreign_keys 在事务内静默无效时必须因 readback!=1 fail closed。"""
    path = _assert_d_test_path(tmp_path / "pragma-in-txn.sqlite3")
    receipt = _startup_failure_receipt(
        path,
        scenario="foreign_keys_in_transaction",
        backup_root=_controlled_backup_root(path),
    )
    assert receipt["errorCode"] == "SQLITE_PRAGMA_READBACK_FAILED"
    assert receipt["coordinatorState"] == "FAILED"
    assert receipt["mutexEvents"] == ["acquire", "release", "close_handle"]

    if path.exists():
        with sqlite3.connect(path) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            assert tables == []


def test_f01_connect_failure_is_stable_and_releases_mutex_without_ready(tmp_path: Path) -> None:
    """connect 失败时不得迁移/发布 ready；已取得 mutex 必须在 owner 线程闭合。"""
    path = _assert_d_test_path(tmp_path / "connect-failure.sqlite3")
    receipt = _startup_failure_receipt(
        path,
        scenario="connect_failure",
        backup_root=_controlled_backup_root(path),
    )
    assert receipt["errorCode"] == "SQLITE_CONNECT_FAILED"
    assert receipt["probePoints"] == ["sqlite_connect"]
    assert receipt["mutexEvents"] == ["acquire", "release", "close_handle"]


@pytest.mark.parametrize(
    "failure_point",
    [
        "pragma_foreign_keys_set",
        "pragma_foreign_keys_read",
        "pragma_journal_mode_set",
        "pragma_journal_mode_read",
        "pragma_synchronous_set",
        "pragma_synchronous_read",
        "migration:0002_event_store.sql:statement:1:after",
        "migration_transaction:after_foreign_key_check",
        "migration_transaction:after_integrity_check",
    ],
)
def test_f02_f04_bootstrap_failure_points_never_publish_ready(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """每个 PRAGMA/migration/integrity 切点都关闭连接并保持 coordinator FAILED。"""
    path = _assert_d_test_path(tmp_path / f"{failure_point.replace(':', '-')}.sqlite3")
    receipt = _startup_failure_receipt(
        path,
        scenario="fault",
        failure_point=failure_point,
        backup_root=_controlled_backup_root(path),
    )
    assert receipt["coordinatorState"] == "FAILED"
    assert receipt["mutexEvents"] == ["acquire", "release", "close_handle"]


@pytest.mark.asyncio
async def test_all_uow_repositories_share_the_owner_connection(tmp_path: Path) -> None:
    """workflow/events/auth/resources stores 必须共享同一连接与显式事务。"""
    path = _assert_d_test_path(tmp_path / "one-connection.sqlite3")
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    await _bounded(coordinator.start())
    try:
        identities = await _bounded(
            coordinator.execute(
                operation="inspect-uow-identity",
                context={"correlation_id": "corr-uow-identity"},
                command=lambda uow: (
                    uow.connection_identity,
                    uow.workflow.connection_identity,
                    uow.events.connection_identity,
                    uow.authorization.connection_identity,
                    uow.resources.connection_identity,
                ),
            )
        )
    finally:
        await _bounded(coordinator.close())
    assert len(set(identities)) == 1


@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        ("plan_revision", "noncanonical_blob"),
        ("intent_authorization", "selector_drift"),
        ("execution_authorization", "digest_drift"),
    ],
)
def test_repository_append_validates_canonical_contract_and_selectors_before_insert(
    tmp_path: Path,
    kind: str,
    mutation: str,
) -> None:
    """Plan/Authorization append 必须调用领域 hydrator/parser，不能依赖 SQLite 形状。"""
    path = _assert_d_test_path(tmp_path / f"append-boundary-{kind}-{mutation}.sqlite3")
    _bootstrap_and_seed_repository_graph(path)
    record, repository_name, append_name, get_name, identity = _repository_contract_case(kind)
    invalid = _corrupt_repository_contract_record(kind, record, mutation)
    error_type, error_code = _repository_contract_error_for_mutation(kind, mutation)
    database = _database_module().SqliteDatabase(path, backup_root=_controlled_backup_root(path))
    database.open()
    try:
        unit_of_work = database.new_unit_of_work()
        unit_of_work.begin_immediate()
        repository = getattr(unit_of_work, repository_name)
        with pytest.raises(error_type) as caught:
            getattr(repository, append_name)(invalid)
        assert caught.value.error_code == error_code
        assert getattr(repository, get_name)(identity) is None
        unit_of_work.rollback()
    finally:
        database.close()


@pytest.mark.parametrize(
    ("kind", "mutation"),
    [
        ("plan_revision", "noncanonical_blob"),
        ("intent_authorization", "selector_drift"),
        ("execution_authorization", "digest_drift"),
    ],
)
def test_repository_get_rehydrates_and_rejects_raw_persistence_corruption(
    tmp_path: Path,
    kind: str,
    mutation: str,
) -> None:
    """raw SQLite 绕过 append 后，get 自身仍须重验 JCS、digest 和 selector。"""
    path = _assert_d_test_path(tmp_path / f"get-boundary-{kind}-{mutation}.sqlite3")
    _bootstrap_and_seed_repository_graph(path)
    record, repository_name, append_name, get_name, identity = _repository_contract_case(kind)
    table, identity_column = {
        "plan_revision": ("plan_revisions", "plan_revision_id"),
        "intent_authorization": ("intent_authorizations", "intent_authorization_id"),
        "execution_authorization": ("execution_authorizations", "execution_authorization_id"),
    }[kind]
    database = _database_module().SqliteDatabase(path, backup_root=_controlled_backup_root(path))
    database.open()
    try:
        unit_of_work = database.new_unit_of_work()
        unit_of_work.begin_immediate()
        repository = getattr(unit_of_work, repository_name)
        getattr(repository, append_name)(record)
        assert getattr(repository, get_name)(identity) is not None
        unit_of_work.precommit()
        unit_of_work.commit()
    finally:
        database.close()

    corrupted = _corrupt_repository_contract_record(kind, record, mutation)
    changed = [column for column in record if corrupted[column] != record[column]]
    assert len(changed) == 1
    column = changed[0]
    with _raw_connection(path) as connection:
        row = connection.execute(
            f'SELECT rowid, "{column}" FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity,),
        ).fetchone()
        assert row is not None
        rowid, stored = row
        replacement = corrupted[column]
        stored_bytes = stored if isinstance(stored, bytes) else str(stored).encode("utf-8")
        replacement_bytes = replacement if isinstance(replacement, bytes) else str(replacement).encode("utf-8")
        assert len(stored_bytes) == len(replacement_bytes)
        # blobopen 绕过 append-only UPDATE trigger，模拟同长存储字节腐化。
        with connection.blobopen(table, column, rowid, readonly=False) as blob:
            blob.write(replacement_bytes)
        persisted = connection.execute(
            f'SELECT "{column}" FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity,),
        ).fetchone()
        assert persisted is not None
        persisted_value = persisted[0]
        if isinstance(persisted_value, bytes):
            persisted_bytes = persisted_value
        else:
            persisted_bytes = str(persisted_value).encode("utf-8")
        assert persisted_bytes == replacement_bytes
        assert persisted_bytes != stored_bytes

    error_type, error_code = _repository_contract_error_for_mutation(kind, mutation)
    reopened = _database_module().SqliteDatabase(path, backup_root=_controlled_backup_root(path))
    reopened.open()
    try:
        repository = getattr(reopened.new_unit_of_work(), repository_name)
        with pytest.raises(error_type) as caught:
            getattr(repository, get_name)(identity)
        assert caught.value.error_code == error_code
    finally:
        reopened.close()


def test_sqlite_database_open_rejects_reentrant_open_without_leaking_connection(tmp_path: Path) -> None:
    """同一 Database 实例只能持有一条 owner connection；二次 open 必须在 connect 前 fail closed。"""
    database_module = _database_module()
    path = _assert_d_test_path(tmp_path / "double-open.sqlite3")
    connections: list[sqlite3.Connection] = []

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = sqlite3.connect(*args, **kwargs)
        connections.append(connection)
        return connection

    database = database_module.SqliteDatabase(
        path,
        backup_root=_controlled_backup_root(path),
        connect_factory=connect,
    )
    database.open()
    with pytest.raises(database_module.DatabaseStartupError) as caught:
        database.open()
    assert caught.value.error_code == "SQLITE_ALREADY_OPEN"
    assert len(connections) == 1

    database.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


@pytest.mark.parametrize(
    ("operation", "invalid_key"),
    [
        ("insert", "unknown_selector"),
        ("insert", 'owner_executor_id" = "owner_executor_id'),
        ("cas", "unknown_selector"),
        ("cas", 'owner_executor_id" = "owner_executor_id'),
    ],
)
def test_resource_repository_rejects_untrusted_sql_identifier_keys(
    tmp_path: Path,
    operation: str,
    invalid_key: str,
) -> None:
    """Resource repository 的动态 Mapping key 不能进入 SQL identifier；必须先转成稳定仓储错误。"""
    path = _assert_d_test_path(tmp_path / f"resource-identifier-{operation}.sqlite3")
    database = _database_module().SqliteDatabase(path, backup_root=_controlled_backup_root(path))
    database.open()
    try:
        unit_of_work = database.new_unit_of_work()
        unit_of_work.begin_immediate()
        if operation == "insert":
            forged = _resource_lease_record(**{invalid_key: "attacker-controlled"})

            def action() -> object:
                return unit_of_work.resources.insert_resource_lease(forged)

        else:
            unit_of_work.resources.insert_resource_lease(_resource_lease_record())

            def action() -> object:
                return unit_of_work.resources.compare_and_set_resource_lease(
                    "repo:project-uow-1",
                    0,
                    {invalid_key: "attacker-controlled"},
                )

        with pytest.raises(Exception) as caught:
            action()
        assert caught.value.__class__.__name__ == "StorageIdentifierError"
        assert getattr(caught.value, "error_code", None) == "INVALID_STORAGE_IDENTIFIER"
        unit_of_work.rollback()
    finally:
        database.close()


@pytest.mark.asyncio
async def test_p05_p06_task_cas_and_authoritative_state_event_commit_together(tmp_path: Path) -> None:
    """CAS n→n+1、outcomeVersion 与 state.changed 必须同事务可见。"""
    path = _assert_d_test_path(tmp_path / "atomic-success.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    await _bounded(coordinator.start())
    event = _task_state_event()
    try:
        result = await _bounded(
            coordinator.execute(
                operation="advance-task-stage",
                context={"correlation_id": "corr-atomic-success", "task_id": "task-uow-1"},
                command=lambda uow: (
                    uow.workflow.compare_and_set_task(
                        task_id="task-uow-1",
                        expected_state_version=0,
                        achieved_stage="DESIGN_APPROVED",
                        outcome_version=1,
                    ),
                    uow.events.append_authoritative_state_event(event),
                ),
            )
        )
        assert result[0].state_version == 1
    finally:
        await _bounded(coordinator.close())

    task, state_events, task7_counts = _task_snapshot(path)
    assert task == ("DESIGN_APPROVED", 1, 1)
    assert state_events == [("state-event-task-1", 0, 1, event["payloadDigest"])]
    assert task7_counts == (0, 0, 0)


def test_same_type_multi_task_cas_pairs_by_identity_when_events_arrive_out_of_order(tmp_path: Path) -> None:
    """同事务两个 TASK CAS 必须按 identity 与乱序 event 2↔2 配对，不能只按 type。"""
    path = _assert_d_test_path(tmp_path / "same-type-two-task-pairing.sqlite3")
    _bootstrap_and_seed_repository_graph(path)
    with _raw_connection(path) as connection:
        _clone_seed_row(
            connection,
            "tasks",
            "task_id",
            "task-uow-1",
            task_id="task-uow-2",
            active_plan_revision_id=None,
            active_run_id=None,
        )
    events = (
        _task_state_event(state_event_id="state-event-task-2", task_id="task-uow-2"),
        _task_state_event(state_event_id="state-event-task-1", task_id="task-uow-1"),
    )
    database = _database_module().SqliteDatabase(path, backup_root=_controlled_backup_root(path))
    database.open()
    try:
        unit_of_work = database.new_unit_of_work()
        unit_of_work.begin_immediate()
        for task_id in ("task-uow-1", "task-uow-2"):
            unit_of_work.workflow.compare_and_set_task(
                task_id=task_id,
                expected_state_version=0,
                achieved_stage="DESIGN_APPROVED",
                outcome_version=1,
            )
        for event in events:
            unit_of_work.events.append_authoritative_state_event(event)
        unit_of_work.precommit()
        unit_of_work.commit()
    finally:
        database.close()

    with _raw_connection(path) as connection:
        assert connection.execute(
            "SELECT task_id,achieved_stage,state_version,outcome_version FROM tasks "
            "WHERE task_id IN ('task-uow-1','task-uow-2') ORDER BY task_id"
        ).fetchall() == [
            ("task-uow-1", "DESIGN_APPROVED", 1, 1),
            ("task-uow-2", "DESIGN_APPROVED", 1, 1),
        ]
        assert connection.execute(
            "SELECT state_event_id,task_id,aggregate_id FROM authoritative_state_events ORDER BY state_event_id"
        ).fetchall() == [
            ("state-event-task-1", "task-uow-1", "task-uow-1"),
            ("state-event-task-2", "task-uow-2", "task-uow-2"),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_type", "method_name", "changes", "table", "identity_column"),
    CAS_CASES,
)
async def test_five_authoritative_aggregate_cas_paths_pair_one_to_one_with_state_event(
    tmp_path: Path,
    aggregate_type: str,
    method_name: str,
    changes: dict[str, object],
    table: str,
    identity_column: str,
) -> None:
    """Task/Run/Barrier/Step/Attempt 五路 CAS 均须以同 identity/version 配对一个权威事件。"""
    path = _assert_d_test_path(tmp_path / f"five-cas-{aggregate_type.casefold()}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path, include_phase_barrier=True, include_execution_graph=True)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    await _bounded(coordinator.start())
    event = _aggregate_state_event(aggregate_type)

    def paired_change(uow: _AtomicUnitOfWorkPort) -> object:
        result = getattr(uow.workflow, method_name)(**changes)
        uow.events.append_authoritative_state_event(event)
        return result

    try:
        result = await _bounded(
            coordinator.execute(
                operation=f"cas-{aggregate_type.casefold()}",
                context={
                    "correlation_id": f"corr-{aggregate_type.casefold()}",
                    "task_id": "task-uow-1",
                },
                command=paired_change,
            )
        )
        assert result.state_version == 1
    finally:
        await _bounded(coordinator.close())

    identity_value = next(value for key, value in changes.items() if key == identity_column)
    with _raw_connection(path) as connection:
        assert connection.execute(
            f'SELECT state_version FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity_value,),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT aggregate_type,aggregate_id,previous_state_version,state_version FROM authoritative_state_events"
        ).fetchall() == [(aggregate_type, event["aggregateId"], 0, 1)]
        assert connection.execute("SELECT COUNT(*) FROM ingest_batches").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM stream_segments").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


PAIRING_FAILURES: tuple[tuple[str, str, str], ...] = (
    ("state_only", "StateEventPairingError", "STATE_EVENT_PAIRING_MISMATCH"),
    ("event_only", "StateEventPairingError", "STATE_EVENT_PAIRING_MISMATCH"),
    ("duplicate_event", "StateEventPairingError", "STATE_EVENT_PAIRING_MISMATCH"),
    ("same_type_wrong_aggregate", "StateEventIdentityError", "STATE_EVENT_IDENTITY_MISMATCH"),
    ("wrong_task_lineage", "StateEventIdentityError", "STATE_EVENT_IDENTITY_MISMATCH"),
    ("wrong_version", "StateEventVersionError", "STATE_EVENT_VERSION_MISMATCH"),
    ("wrong_digest", "StateEventValidationError", "INVALID_AUTHORITATIVE_STATE_EVENT"),
)

PAIRING_CASES = tuple((*cas_case, *failure) for cas_case in CAS_CASES for failure in PAIRING_FAILURES) + tuple(
    (*cas_case, mutation, "StateEventIdentityError", "STATE_EVENT_IDENTITY_MISMATCH")
    for cas_case in CAS_CASES
    for mutation in (
        *(("wrong_run_lineage",) if cas_case[0] in {"RUN", "PHASE_BARRIER", "STEP", "ATTEMPT"} else ()),
        *(("wrong_step_lineage",) if cas_case[0] in {"STEP", "ATTEMPT"} else ()),
    )
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "aggregate_type",
        "method_name",
        "changes",
        "table",
        "identity_column",
        "mutation",
        "error_type",
        "error_code",
    ),
    PAIRING_CASES,
)
async def test_precommit_rejects_unpaired_duplicate_or_drifting_authoritative_events(
    tmp_path: Path,
    aggregate_type: str,
    method_name: str,
    changes: dict[str, object],
    table: str,
    identity_column: str,
    mutation: str,
    error_type: str,
    error_code: str,
) -> None:
    """五类 aggregate 均覆盖 state/event 数量、identity、lineage、version、digest 漂移。"""
    path = _assert_d_test_path(tmp_path / f"pairing-{aggregate_type.casefold()}-{mutation}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(
        path,
        include_phase_barrier=True,
        include_execution_graph=True,
        include_identity_alternates=True,
    )
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    uow_module = _uow_module()
    event = _drifting_state_event(aggregate_type, mutation)

    def invalid_pair(uow: _AtomicUnitOfWorkPort) -> None:
        if mutation != "event_only":
            getattr(uow.workflow, method_name)(**changes)
        if mutation != "state_only":
            uow.events.append_authoritative_state_event(event)
        if mutation == "duplicate_event":
            duplicate = dict(event)
            duplicate["stateEventId"] = f"state-event-{aggregate_type.casefold()}-duplicate"
            uow.events.append_authoritative_state_event(duplicate)

    await _bounded(coordinator.start())
    try:
        expected_error = getattr(uow_module, error_type)
        with pytest.raises(expected_error) as caught:
            await _bounded(
                coordinator.execute(
                    operation=f"invalid-pair-{aggregate_type.casefold()}-{mutation}",
                    context={
                        "correlation_id": f"corr-pair-{aggregate_type.casefold()}-{mutation}",
                        "task_id": "task-uow-1",
                    },
                    command=invalid_pair,
                )
            )
        assert caught.value.error_code == error_code
        assert coordinator.state.name == "READY"
    finally:
        await _bounded(coordinator.close())

    identity = changes[identity_column]
    with _raw_connection(path) as connection:
        assert connection.execute(
            f'SELECT state_version FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity,),
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM ingest_batches").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM stream_segments").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("ratio", ["two_cas_one_event", "one_cas_two_events"])
async def test_precommit_rejects_non_bijective_cas_event_cardinality(tmp_path: Path, ratio: str) -> None:
    """显式证明 2CAS↔1event 与 1CAS↔2event 都不是可提交的一一配对。"""
    path = _assert_d_test_path(tmp_path / f"pair-cardinality-{ratio}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path, include_phase_barrier=True)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    uow_module = _uow_module()

    def non_bijective(uow: _AtomicUnitOfWorkPort) -> None:
        uow.workflow.compare_and_set_task(
            task_id="task-uow-1",
            expected_state_version=0,
            achieved_stage="DESIGN_APPROVED",
            outcome_version=1,
        )
        if ratio == "two_cas_one_event":
            uow.workflow.compare_and_set_run(
                run_id="run-uow-1",
                expected_state_version=0,
                observed_state="RUNNING",
            )
        uow.events.append_authoritative_state_event(_aggregate_state_event("TASK"))
        if ratio == "one_cas_two_events":
            second = _aggregate_state_event("TASK")
            second["stateEventId"] = "state-event-task-second"
            uow.events.append_authoritative_state_event(second)

    await _bounded(coordinator.start())
    try:
        with pytest.raises(uow_module.StateEventPairingError) as caught:
            await _bounded(
                coordinator.execute(
                    operation=f"pair-cardinality-{ratio}",
                    context={
                        "correlation_id": f"corr-cardinality-{ratio}",
                        "task_id": "task-uow-1",
                    },
                    command=non_bijective,
                )
            )
        assert caught.value.error_code == "STATE_EVENT_PAIRING_MISMATCH"
    finally:
        await _bounded(coordinator.close())
    with _raw_connection(path) as connection:
        assert connection.execute("SELECT state_version FROM tasks WHERE task_id='task-uow-1'").fetchone() == (0,)
        assert connection.execute("SELECT state_version FROM runs WHERE run_id='run-uow-1'").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)


@pytest.mark.asyncio
async def test_p07_callback_failure_explicitly_rolls_back_state_and_event(tmp_path: Path) -> None:
    """callback 在 COMMIT 前失败时必须显式 ROLLBACK，不能隐式 commit。"""
    path = _assert_d_test_path(tmp_path / "explicit-rollback.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    await _bounded(coordinator.start())

    def fail_after_both_writes(uow: _AtomicUnitOfWorkPort) -> None:
        """先写两侧再抛错，用于证明整个 UoW 回到 before snapshot。"""
        uow.workflow.compare_and_set_task(
            task_id="task-uow-1",
            expected_state_version=0,
            achieved_stage="DESIGN_APPROVED",
            outcome_version=1,
        )
        uow.events.append_authoritative_state_event(_task_state_event())
        raise _InjectedFailure("callback-before-commit")

    try:
        with pytest.raises(_InjectedFailure):
            await _bounded(
                coordinator.execute(
                    operation="rollback-callback",
                    context={"correlation_id": "corr-explicit-rollback", "task_id": "task-uow-1"},
                    command=fail_after_both_writes,
                )
            )
    finally:
        await _bounded(coordinator.close())
    assert _task_snapshot(path) == (("NONE", 0, 0), [], (0, 0, 0))


def test_poisoned_commit_outcome_unknown_close_discards_connection_without_rollback(tmp_path: Path) -> None:
    """COMMIT outcome unknown 后只能废弃连接；close 不得伪称显式 ROLLBACK。"""
    path = _assert_d_test_path(tmp_path / "commit-outcome-unknown.sqlite3")
    database_module = _database_module()
    statements: list[str] = []
    connections: list[_CommitOutcomeUnknownConnection] = []

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        """创建可拦截 COMMIT 的真实 SQLite Connection 子类并保留 trace。"""
        kwargs["factory"] = _CommitOutcomeUnknownConnection
        connection = sqlite3.connect(*args, **kwargs)
        assert isinstance(connection, _CommitOutcomeUnknownConnection)
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    database = database_module.SqliteDatabase(
        path,
        backup_root=_controlled_backup_root(path),
        connect_factory=connect,
    )
    database.open()
    assert len(connections) == 1
    connection = connections[0]
    unit_of_work = database.new_unit_of_work()
    unit_of_work.begin_immediate()
    connection.fail_commit = True

    with pytest.raises(sqlite3.OperationalError, match="injected-commit-outcome-unknown"):
        unit_of_work.commit()
    assert connection.in_transaction is True

    database.poison()
    database.close()

    # outcome unknown 绝不允许用 ROLLBACK 日志把不确定事实改写成已回滚。
    assert not any(_trace_key(statement) == ("rollback",) for statement in statements)
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="not available"):
        database.connection_identity


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_type", "method_name", "changes", "table", "identity_column"),
    CAS_CASES,
)
async def test_n09_each_authoritative_aggregate_stale_cas_persists_no_event(
    tmp_path: Path,
    aggregate_type: str,
    method_name: str,
    changes: dict[str, object],
    table: str,
    identity_column: str,
) -> None:
    """五路 rowcount=0 都必须映射 STALE_STATE_VERSION，回滚且不自动重读重试。"""
    path = _assert_d_test_path(tmp_path / f"stale-{aggregate_type.casefold()}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path, include_phase_barrier=True, include_execution_graph=True)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    uow_module = _uow_module()
    stale_changes = dict(changes)
    stale_changes["expected_state_version"] = 9
    event = _aggregate_state_event(aggregate_type)
    await _bounded(coordinator.start())
    try:
        with pytest.raises(uow_module.StaleStateVersionError) as caught:
            await _bounded(
                coordinator.execute(
                    operation=f"stale-{aggregate_type.casefold()}",
                    context={
                        "correlation_id": f"corr-stale-{aggregate_type.casefold()}",
                        "task_id": "task-uow-1",
                    },
                    command=lambda uow: (
                        getattr(uow.workflow, method_name)(**stale_changes),
                        uow.events.append_authoritative_state_event(event),
                    ),
                ),
            )
        assert caught.value.error_code == "STALE_STATE_VERSION"
    finally:
        await _bounded(coordinator.close())
    identity = changes[identity_column]
    with _raw_connection(path) as connection:
        assert connection.execute(
            f'SELECT state_version FROM "{table}" WHERE "{identity_column}"=?',  # noqa: S608
            (identity,),
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["after_state_cas", "before_state_event_insert", "before_commit"])
async def test_f06_f08_injected_precommit_failures_restore_exact_before_snapshot(
    tmp_path: Path,
    failure_point: str,
) -> None:
    """state/event/COMMIT 前任一故障都必须使业务投影与 event 同时不可见。"""
    path = _assert_d_test_path(tmp_path / f"{failure_point}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    coordinator, _mutex, _probe = _new_stack(
        path,
        backup_root=_controlled_backup_root(path),
        failing_points={failure_point},
    )
    await _bounded(coordinator.start())
    try:
        with pytest.raises(_InjectedFailure):
            await _bounded(
                coordinator.execute(
                    operation="faulted-state-change",
                    context={"correlation_id": "corr-precommit-fault", "task_id": "task-uow-1"},
                    command=lambda uow: (
                        uow.workflow.compare_and_set_task(
                            task_id="task-uow-1",
                            expected_state_version=0,
                            achieved_stage="DESIGN_APPROVED",
                            outcome_version=1,
                        ),
                        uow.events.append_authoritative_state_event(_task_state_event()),
                    ),
                ),
            )
    finally:
        await _bounded(coordinator.close())
    assert _task_snapshot(path) == (("NONE", 0, 0), [], (0, 0, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["digest", "unknown_field"])
async def test_n08_authoritative_event_is_schema_validated_and_payload_digest_recomputed(
    tmp_path: Path,
    mutation: str,
) -> None:
    """未知字段或与 payload 不匹配的摘要必须在 insert 前 fail closed 并回滚 CAS。"""
    path = _assert_d_test_path(tmp_path / f"invalid-event-{mutation}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    uow_module = _uow_module()
    event = _task_state_event()
    if mutation == "digest":
        event["payloadDigest"] = "sha256:" + "f" * 64
    else:
        event["forgedUnknownField"] = "must-reject"
    await _bounded(coordinator.start())
    try:
        with pytest.raises(uow_module.StateEventValidationError) as caught:
            await _bounded(
                coordinator.execute(
                    operation="invalid-state-event",
                    context={"correlation_id": "corr-invalid-event", "task_id": "task-uow-1"},
                    command=lambda uow: (
                        uow.workflow.compare_and_set_task(
                            task_id="task-uow-1",
                            expected_state_version=0,
                            achieved_stage="DESIGN_APPROVED",
                            outcome_version=1,
                        ),
                        uow.events.append_authoritative_state_event(event),
                    ),
                ),
            )
        assert caught.value.error_code == "INVALID_AUTHORITATIVE_STATE_EVENT"
    finally:
        await _bounded(coordinator.close())
    assert _task_snapshot(path) == (("NONE", 0, 0), [], (0, 0, 0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("aggregate_id", "run_id"),
    [("wrong-barrier", "run-uow-1"), ("bar-uow-1", "run-other")],
)
async def test_phase_barrier_state_event_checks_aggregate_id_and_row_run_id(
    tmp_path: Path,
    aggregate_id: str,
    run_id: str,
) -> None:
    """PHASE_BARRIER 必须同时满足 aggregateId==barrier_id 与 row.run_id==runId。"""
    path = _assert_d_test_path(tmp_path / f"barrier-{aggregate_id}-{run_id}.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path, include_phase_barrier=True)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    uow_module = _uow_module()
    await _bounded(coordinator.start())
    try:
        with pytest.raises(uow_module.StateEventIdentityError) as caught:
            await _bounded(
                coordinator.execute(
                    operation="invalid-barrier-event",
                    context={"correlation_id": "corr-barrier-invalid", "task_id": "task-uow-1"},
                    command=lambda uow: (
                        uow.workflow.compare_and_set_phase_barrier(
                            barrier_id="bar-uow-1",
                            expected_state_version=0,
                            settled=True,
                            passed=False,
                        ),
                        uow.events.append_authoritative_state_event(
                            _barrier_state_event(aggregate_id=aggregate_id, run_id=run_id)
                        ),
                    ),
                ),
            )
        assert caught.value.error_code == "STATE_EVENT_IDENTITY_MISMATCH"
    finally:
        await _bounded(coordinator.close())

    with _raw_connection(path) as connection:
        assert connection.execute(
            "SELECT settled, passed, state_version FROM phase_barriers WHERE barrier_id = ?",
            ("bar-uow-1",),
        ).fetchone() == (0, 0, 0)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (0,)


@pytest.mark.asyncio
async def test_valid_phase_barrier_event_commits_without_task7_materialization(tmp_path: Path) -> None:
    """合法 barrier state event 走独立 lane，不制造 batch/segment/attempt。"""
    path = _assert_d_test_path(tmp_path / "barrier-valid.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path, include_phase_barrier=True)
    coordinator, _mutex, _probe = _new_stack(path, backup_root=_controlled_backup_root(path))
    await _bounded(coordinator.start())
    try:
        await _bounded(
            coordinator.execute(
                operation="settle-barrier",
                context={"correlation_id": "corr-barrier-valid", "task_id": "task-uow-1"},
                command=lambda uow: (
                    uow.workflow.compare_and_set_phase_barrier(
                        barrier_id="bar-uow-1",
                        expected_state_version=0,
                        settled=True,
                        passed=False,
                    ),
                    uow.events.append_authoritative_state_event(
                        _barrier_state_event(aggregate_id="bar-uow-1", run_id="run-uow-1")
                    ),
                ),
            ),
        )
    finally:
        await _bounded(coordinator.close())

    with _raw_connection(path) as connection:
        assert connection.execute(
            "SELECT settled, passed, state_version FROM phase_barriers WHERE barrier_id = ?",
            ("bar-uow-1",),
        ).fetchone() == (1, 0, 1)
        assert connection.execute("SELECT COUNT(*) FROM authoritative_state_events").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM ingest_batches").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM stream_segments").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (0,)
