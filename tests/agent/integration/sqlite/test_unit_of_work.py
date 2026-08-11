"""SQLite Database/UnitOfWork 原子性、CAS 与失败闭合 RED 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
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

test_module_path, database_path, scenario, failure_point = sys.argv[1:]
namespace = runpy.run_path(test_module_path)

async def main():
    kwargs = {}
    if scenario == "fault":
        kwargs["failing_points"] = {failure_point}
    elif scenario == "connect_failure":
        def fail_connect(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected-connect-failure")
        kwargs["connect_factory"] = fail_connect
    elif scenario == "foreign_keys_in_transaction":
        def connect_in_transaction(*args, **kwargs):
            connection = sqlite3.connect(*args, **kwargs)
            connection.execute("BEGIN")
            return connection
        kwargs["connect_factory"] = connect_in_transaction
    elif scenario != "normal_failure":
        raise AssertionError(f"unknown startup scenario: {scenario}")

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
    print(json.dumps({
        "receiptVersion": 1,
        "kind": "sqlite-startup-failure",
        "status": "failed_closed",
        "errorCode": error_code,
        "coordinatorState": coordinator.state.name,
        "ownerThreadAlive": owner_alive,
        "mutexEvents": [name for name, _thread in mutex.events],
        "probePoints": [point for point, _thread in probe.calls],
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


def _valid_intent_authorization() -> dict[str, object]:
    """返回完整通过 intent-authorization.v1 的真实合同，不使用 ID-only 伪 blob。"""
    return {
        "intentAuthorizationId": "intent-uow-1",
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


def _valid_execution_authorization() -> dict[str, object]:
    """返回完整通过 execution-authorization.v1 的真实 IMPLEMENT/worktree.write 合同。"""
    return {
        "executionAuthorizationId": "execution-uow-1",
        "intentAuthorizationId": "intent-uow-1",
        "planRevisionId": "plan-uow-1",
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


class _FailureProbe:
    """记录生产路径经过的故障点，并在指定点确定性抛错。"""

    def __init__(self, failing_points: Iterable[str] = ()) -> None:
        self.failing_points = frozenset(failing_points)
        self.calls: list[tuple[str, int]] = []

    def __call__(self, point: str) -> None:
        """记录调用线程；命中点时抛出不含业务 payload 的测试异常。"""
        self.calls.append((point, threading.get_ident()))
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

    def close(self) -> None:
        """模拟 CloseHandle；必须发生在 release 之后。"""
        assert self._released
        self._owner.events.append(("close_handle", threading.get_ident()))


class _RecordingMutex:
    """跨平台测试 mutex；只验证 coordinator 的线程和启动顺序。"""

    def __init__(self) -> None:
        self.owner_thread_id: int | None = None
        self.events: list[tuple[str, int]] = []

    def acquire(self) -> _RecordingLease:
        """在 writer owner 线程记录 acquire 并返回 lease。"""
        assert self.owner_thread_id is None
        self.owner_thread_id = threading.get_ident()
        self.events.append(("acquire", self.owner_thread_id))
        return _RecordingLease(self)


def _new_stack(
    database_path: Path,
    *,
    failing_points: Iterable[str] = (),
    connect_factory: Callable[..., sqlite3.Connection] | None = None,
    queue_capacity: int = 8,
) -> tuple[Any, _RecordingMutex, _FailureProbe]:
    """装配可注入故障的真实 SQLite database + 单 writer coordinator。"""
    database_module = _database_module()
    coordinator_module = _coordinator_module()
    probe = _FailureProbe(failing_points)
    database_kwargs: dict[str, object] = {"failure_probe": probe}
    if connect_factory is not None:
        database_kwargs["connect_factory"] = connect_factory
    database = database_module.SqliteDatabase(
        _assert_d_test_path(database_path),
        **database_kwargs,
    )
    mutex = _RecordingMutex()
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
    scenario: str,
    failure_point: str = "",
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
                "canonical_contract_digest": payload_digest(intent_contract),
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
                "canonical_contract_digest": payload_digest(execution_contract),
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


def _task_state_event(
    *,
    state_event_id: str = "state-event-task-1",
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
        "taskId": "task-uow-1",
        "scope": "TASK",
        "aggregateType": "TASK",
        "aggregateId": "task-uow-1",
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
    coordinator, mutex, probe = _new_stack(database_path)
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
    with sqlite3.connect(database_path) as connection:
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
    with sqlite3.connect(database_path, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(SCHEMA_MIGRATIONS_SQL)
        for version, name, checksum in _expected_migration_ledger()[:count]:
            connection.executescript((MIGRATIONS_DIR / name).read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES(?,?,?,?)",
                (version, name, checksum, "2026-08-12T00:00:00Z"),
            )
        connection.execute(f"PRAGMA user_version={count}")


@pytest.mark.asyncio
async def test_bootstrap_order_owner_thread_and_pragma_readback(tmp_path: Path) -> None:
    """mutex 后才 connect；连接只在非 daemon owner 线程且三项 PRAGMA 读回正确。"""
    path = _assert_d_test_path(tmp_path / "owner.sqlite3")
    connect_calls: list[tuple[int, dict[str, object]]] = []

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connect_calls.append((threading.get_ident(), dict(kwargs)))
        return sqlite3.connect(*args, **kwargs)

    coordinator, mutex, probe = _new_stack(path, connect_factory=recording_connect)
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

        async def main():
            database = SqliteDatabase(
                pathlib.Path(sys.argv[1]),
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
                "casSqlCount": sum(
                    1 for _thread_id, sql in traces
                    if sql.lstrip().upper().startswith("UPDATE TASKS")
                ),
                "eventInsertCount": sum(
                    1 for _thread_id, sql in traces
                    if sql.lstrip().upper().startswith("INSERT INTO AUTHORITATIVE_STATE_EVENTS")
                ),
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

    coordinator, _mutex, _probe = _new_stack(path, connect_factory=traced_connect)
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
    user_version = [item for item in items if item[:3] == ("sql", "pragma", "user_version")]
    assert user_version == [("sql", *_trace_key(f"PRAGMA user_version={len(MIGRATION_NAMES)}"))]


@pytest.mark.asyncio
async def test_p02_reopen_is_idempotent_and_does_not_rerun_migrations(tmp_path: Path) -> None:
    """第二次打开同库只核验版本/完整性，不重复执行 0001→0004。"""
    path = _assert_d_test_path(tmp_path / "reopen.sqlite3")
    first, _mutex, _probe = _new_stack(path)
    first_readiness = await _bounded(first.start())
    await _bounded(first.close())

    second, _mutex2, _probe2 = _new_stack(path)
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

    coordinator, _mutex, _probe = _new_stack(path, connect_factory=traced_connect)
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
    receipt = _startup_failure_receipt(path, scenario="normal_failure")
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
        receipt = _startup_failure_receipt(path, scenario="fault", failure_point=failure_point)
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
    receipt = _startup_failure_receipt(path, scenario="foreign_keys_in_transaction")
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
    receipt = _startup_failure_receipt(path, scenario="connect_failure")
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
    receipt = _startup_failure_receipt(path, scenario="fault", failure_point=failure_point)
    assert receipt["coordinatorState"] == "FAILED"
    assert receipt["mutexEvents"] == ["acquire", "release", "close_handle"]


@pytest.mark.asyncio
async def test_all_uow_repositories_share_the_owner_connection(tmp_path: Path) -> None:
    """workflow/events/auth/resources stores 必须共享同一连接与显式事务。"""
    path = _assert_d_test_path(tmp_path / "one-connection.sqlite3")
    coordinator, _mutex, _probe = _new_stack(path)
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


@pytest.mark.asyncio
async def test_p05_p06_task_cas_and_authoritative_state_event_commit_together(tmp_path: Path) -> None:
    """CAS n→n+1、outcomeVersion 与 state.changed 必须同事务可见。"""
    path = _assert_d_test_path(tmp_path / "atomic-success.sqlite3")
    await _bootstrap_empty_database(path)
    _seed_task(path)
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path, failing_points={failure_point})
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
    coordinator, _mutex, _probe = _new_stack(path)
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
