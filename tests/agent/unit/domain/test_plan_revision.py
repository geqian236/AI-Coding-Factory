"""PlanRevisionBundle 双合同与双 canonical persistence 的 RED 测试。

RunSpec 与 PlanRevision 是两份彼此独立、均通过冻结 schema 的合同。测试先让
被修改的一侧重新计算自己的摘要，再验证 bundle 因跨合同漂移而 fail closed，
避免用“摘要本来就错了”的假阳性代替真正的一致性检查。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import plan_revision_digest, semantic_plan_hash

RUN_SPEC_SCHEMA_ID = "run-spec.v1"
PLAN_REVISION_SCHEMA_ID = "plan-revision.v1"
CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "contracts/schemas"


def _plans_module() -> ModuleType:
    """延迟导入待实现模块，使缺实现表现为测试 RED 而不是收集中断。"""
    return importlib.import_module("factory_agent.domain.plans")


def _database_module() -> ModuleType:
    """延迟导入待实现 SQLite database，供真实 close/reopen RED 使用。"""
    return importlib.import_module("factory_agent.storage.sqlite.database")


def _schema(filename: str) -> dict[str, Any]:
    """读取冻结 schema；测试不得复制顶层合同或静默容忍 schema 漂移。"""
    return json.loads((CONTRACTS_DIR / filename).read_text(encoding="utf-8"))


def _lossless_plan_revision_schema() -> dict[str, Any]:
    """构造即将集成的无损合同，用 RunSpec workPlan 机械替换旧缩小 aliases。

    f67 的 plan-revision schema 仍是旧形状；本 helper 只让 RED fixture 可以自证，
    独立 prerequisite 测试会要求磁盘 schema 精确等于这个目标并诚实保持 RED。
    """
    run_spec_schema = _schema("run-spec.v1.schema.json")
    revision_schema = _schema("plan-revision.v1.schema.json")
    work_plan = run_spec_schema["properties"]["workPlan"]["properties"]
    revision_schema["properties"]["nodes"] = deepcopy(work_plan["nodes"])
    revision_schema["properties"]["barriers"] = deepcopy(work_plan["barriers"])
    return revision_schema


def _unchecked_plan_revision_digest(revision: Mapping[str, Any]) -> str:
    """按冻结 digest material 算法计算新合同摘要，不借旧 f67 schema 制造假 RED。"""
    material = dict(revision)
    del material["planRevisionDigest"]
    material.pop("signature", None)
    return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()


def _run_spec_contract(
    *,
    spec_revision: int = 2,
    parent_revision_id: str | None = "plan-revision-domain-0",
) -> dict[str, Any]:
    """构造完整且可独立通过 run-spec.v1 的 post-bootstrap RunSpec。"""
    run_spec: dict[str, Any] = {
        "schemaVersion": 1,
        "specRevision": spec_revision,
        "parentRevisionId": parent_revision_id,
        "taskId": "task-domain-1",
        "goal": "实现纯领域模型和 SQLite 原子底座",
        "assumptions": ["事件合同已冻结"],
        "scope": {
            "include": ["apps/agent/src/factory_agent/domain"],
            "exclude": ["Task 2+"],
        },
        "constraints": ["禁止多 writer"],
        "acceptanceCriteria": ["Task 1 RED/GREEN 门禁通过"],
        "targetStage": "CODEX_APPROVED",
        "repository": {
            "mode": "existing",
            "root": "D:/codex项目/AI-Coding-Factory",
            "baseBranch": "factory/bootstrap-plan",
            "baseCommit": "0123456789abcdef0123456789abcdef01234567",
        },
        "workPlan": {
            "dagVersion": 1,
            "nodes": [
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
            ],
            "barriers": [
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
            ],
        },
        "riskProfile": {"level": "medium", "reasons": ["持久化边界变更"]},
        "nodeCapabilityMapVersion": "node-capability-map.v1",
        "stageCapabilityMapVersion": "stage-capability-map.v1",
        "intentAuthorizationId": "intent-domain-1",
        "semanticPlanHash": "sha256:" + "0" * 64,
        # 这两个字段在 schema 中可选，但 Task 1 持久合同明确要求不能丢失。
        "planRevisionDigest": "sha256:" + "0" * 64,
        "createdAt": "2026-08-12T00:00:00Z",
    }
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    return run_spec


def _plan_revision_contract(run_spec: dict[str, Any]) -> dict[str, Any]:
    """从 RunSpec 构造 nodes/barriers 无损同形的新 PlanRevision 合同。"""
    revision: dict[str, Any] = {
        "planRevisionId": "plan-revision-domain-1",
        "taskId": run_spec["taskId"],
        "specRevision": run_spec["specRevision"],
        "intentAuthorizationId": run_spec["intentAuthorizationId"],
        "semanticPlanHash": run_spec["semanticPlanHash"],
        "planRevisionDigest": "sha256:" + "0" * 64,
        "dagVersion": run_spec["workPlan"]["dagVersion"],
        "nodeCapabilityMapVersion": run_spec["nodeCapabilityMapVersion"],
        "stageCapabilityMapVersion": run_spec["stageCapabilityMapVersion"],
        "nodes": deepcopy(run_spec["workPlan"]["nodes"]),
        "barriers": deepcopy(run_spec["workPlan"]["barriers"]),
        "stageMaps": {
            "DESIGN_APPROVED": ["plan"],
            "CODEX_APPROVED": ["plan", "implement"],
        },
        "createdAt": run_spec["createdAt"],
    }
    # RunSpec 用显式 null 表示 genesis；PlanRevision 合同用字段缺席表示同一事实。
    if run_spec["parentRevisionId"] is not None:
        revision["parentRevisionId"] = run_spec["parentRevisionId"]
    revision["planRevisionDigest"] = _unchecked_plan_revision_digest(revision)
    return revision


def _valid_bundle_inputs() -> tuple[dict[str, Any], dict[str, Any]]:
    """返回两份独立 schema-valid 且摘要、自身字段和共同 selector 一致的合同。"""
    run_spec = _run_spec_contract()
    revision = _plan_revision_contract(run_spec)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    # planRevisionDigest 不参与 semanticPlanHash，但仍重算一次证明完整 RunSpec 可验证。
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
    return run_spec, revision


def _persistence_record(run_spec: dict[str, Any], revision: dict[str, Any]) -> dict[str, object]:
    """构造 Task 1 双 canonical blob 与 §11 selector 的完整持久记录。"""
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
        "run_spec_schema_id": RUN_SPEC_SCHEMA_ID,
        "run_spec_schema_version": 1,
        "canonical_run_spec": canonicalize(run_spec),
        "plan_revision_schema_id": PLAN_REVISION_SCHEMA_ID,
        "plan_revision_schema_version": 1,
        "canonical_plan_revision": canonicalize(revision),
    }


def _assert_lossless_contract_pair(run_spec: dict[str, Any], revision: dict[str, Any]) -> None:
    """用当前 RunSpec 与即将集成的新 PlanRevision schema 分别验证两侧。"""
    jsonschema.Draft7Validator(
        _schema("run-spec.v1.schema.json"),
        format_checker=jsonschema.FormatChecker(),
    ).validate(run_spec)
    jsonschema.Draft7Validator(
        _lossless_plan_revision_schema(),
        format_checker=jsonschema.FormatChecker(),
    ).validate(revision)
    assert run_spec["semanticPlanHash"] == semantic_plan_hash(run_spec)
    assert revision["planRevisionDigest"] == _unchecked_plan_revision_digest(revision)
    for value in (run_spec, revision):
        canonical = canonicalize(value)
        assert canonicalize(json.loads(canonical.decode("utf-8"))) == canonical


def _recompute_changed_side(
    side: str,
    run_spec: dict[str, Any],
    revision: dict[str, Any],
) -> None:
    """重算被改合同自身摘要，使 RED 只针对跨合同语义漂移。"""
    if side == "run_spec":
        run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)
        return
    revision["planRevisionDigest"] = _unchecked_plan_revision_digest(revision)


def _mutate_path(target: dict[str, Any], path: tuple[object, ...], replacement: object) -> None:
    """只修改一个测试声明的嵌套字段；路径均来自本文件固定参数。"""
    current: Any = target
    for part in path[:-1]:
        current = current[part]
    assert current[path[-1]] != replacement, f"mutation 不得 no-op：path={path!r}"
    current[path[-1]] = replacement


def test_plan_revision_bundle_preserves_both_frozen_contract_shapes() -> None:
    """Bundle 必须保留两份完整合同及其独立 canonical bytes。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()

    bundle = plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)

    assert bundle.run_spec["goal"] == run_spec["goal"]
    assert bundle.run_spec["planRevisionDigest"] == revision["planRevisionDigest"]
    assert bundle.plan_revision["planRevisionId"] == revision["planRevisionId"]
    assert bundle.plan_revision is not bundle.run_spec
    assert bundle.canonical_run_spec == canonicalize(run_spec)
    assert bundle.canonical_plan_revision == canonicalize(revision)
    assert bundle.canonical_run_spec != bundle.canonical_plan_revision


def test_plan_revision_schema_prerequisite_matches_lossless_run_spec_fragments() -> None:
    """合同前置：磁盘 PlanRevision nodes/barriers 必须机械复用 RunSpec 片段。"""
    disk_schema = _schema("plan-revision.v1.schema.json")
    target_schema = _lossless_plan_revision_schema()
    for collection in ("nodes", "barriers"):
        assert disk_schema["properties"][collection] == target_schema["properties"][collection]

    node_keys = set(disk_schema["properties"]["nodes"]["items"]["properties"])
    barrier_keys = set(disk_schema["properties"]["barriers"]["items"]["properties"])
    assert node_keys == set(_run_spec_contract()["workPlan"]["nodes"][0])
    assert barrier_keys == set(_run_spec_contract()["workPlan"]["barriers"][0])
    assert node_keys.isdisjoint({"dependencies", "hasSideEffect", "gate"})
    assert barrier_keys.isdisjoint({"barrierId", "nodeIds"})

    _run_spec, revision = _valid_bundle_inputs()
    assert revision["planRevisionDigest"] == plan_revision_digest(revision)


def test_lossless_bundle_fixtures_validate_against_target_schema_jcs_and_digest() -> None:
    """新合同 fixture 独立通过目标 schema/JCS/摘要，避免把 f67 前置 RED 当自错。"""
    run_spec, revision = _valid_bundle_inputs()
    _assert_lossless_contract_pair(run_spec, revision)
    assert revision["nodes"] == run_spec["workPlan"]["nodes"]
    assert revision["barriers"] == run_spec["workPlan"]["barriers"]


def test_genesis_parent_normalizes_explicit_run_spec_null_to_absent_plan_revision_field() -> None:
    """genesis 双合同的 null/缺席是唯一允许的等价规范化。"""
    plans = _plans_module()
    run_spec = _run_spec_contract(spec_revision=1, parent_revision_id=None)
    revision = _plan_revision_contract(run_spec)
    run_spec["planRevisionDigest"] = revision["planRevisionDigest"]
    run_spec["semanticPlanHash"] = semantic_plan_hash(run_spec)

    _assert_lossless_contract_pair(run_spec, revision)
    assert run_spec["parentRevisionId"] is None
    assert "parentRevisionId" not in revision
    record = _persistence_record(run_spec, revision)
    assert record["parent_revision_id"] is None

    bundle = plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)
    hydrated = plans.PlanRevisionBundle.from_persistence_record(record)
    for value in (bundle, hydrated):
        assert value.run_spec["parentRevisionId"] is None
        assert "parentRevisionId" not in value.plan_revision


def test_plan_revision_bundle_is_deeply_immutable() -> None:
    """两份合同、嵌套 DAG、barrier 与 stageMaps 都不得被原位修改。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    bundle = plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)

    with pytest.raises((AttributeError, TypeError)):
        bundle.spec_revision = 3
    for mutation in (
        lambda: bundle.run_spec.__setitem__("goal", "mutated"),
        lambda: bundle.run_spec["workPlan"]["nodes"][1].__setitem__("logicalNodeId", "mutated"),
        lambda: bundle.run_spec["workPlan"]["barriers"][1].__setitem__("barrierOrdinal", 9),
        lambda: bundle.plan_revision["nodes"][1].__setitem__("successPredicateId", "mutated"),
        lambda: bundle.plan_revision["barriers"][1].__setitem__("requiredNodeIds", ("other",)),
        lambda: bundle.plan_revision["stageMaps"].__setitem__("CODEX_APPROVED", ("other",)),
    ):
        with pytest.raises(TypeError):
            mutation()

    # 调用方原始字典后续变化也不得穿透领域对象。
    run_spec["workPlan"]["nodes"][0]["logicalNodeId"] = "caller-mutated"
    revision["stageMaps"]["CODEX_APPROVED"][0] = "caller-mutated"
    assert bundle.run_spec["workPlan"]["nodes"][0]["logicalNodeId"] == "plan"
    assert bundle.plan_revision["stageMaps"]["CODEX_APPROVED"][0] == "plan"


@pytest.mark.parametrize(
    ("side", "path", "replacement"),
    [
        ("run_spec", ("taskId",), "task-other"),
        ("revision", ("taskId",), "task-other"),
        ("run_spec", ("specRevision",), 3),
        ("revision", ("specRevision",), 3),
        ("run_spec", ("parentRevisionId",), "plan-parent-other"),
        ("revision", ("parentRevisionId",), "plan-parent-other"),
        ("run_spec", ("intentAuthorizationId",), "intent-other"),
        ("revision", ("intentAuthorizationId",), "intent-other"),
        ("run_spec", ("planRevisionDigest",), "sha256:" + "e" * 64),
        ("run_spec", ("nodeCapabilityMapVersion",), "node-map-other"),
        ("revision", ("nodeCapabilityMapVersion",), "node-map-other"),
        ("run_spec", ("stageCapabilityMapVersion",), "stage-map-other"),
        ("revision", ("stageCapabilityMapVersion",), "stage-map-other"),
        ("run_spec", ("createdAt",), "2026-08-12T00:00:01Z"),
        ("revision", ("createdAt",), "2026-08-12T00:00:01Z"),
        ("revision", ("semanticPlanHash",), "sha256:" + "f" * 64),
        ("run_spec", ("workPlan", "dagVersion"), 2),
        ("revision", ("dagVersion",), 2),
    ],
)
def test_bundle_rejects_individually_valid_shared_selector_drift(
    side: str,
    path: tuple[object, ...],
    replacement: object,
) -> None:
    """共同 selector 漂移时，被改侧自身摘要正确仍必须拒绝。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    target = run_spec if side == "run_spec" else revision
    _mutate_path(target, path, replacement)
    _recompute_changed_side(side, run_spec, revision)

    _assert_lossless_contract_pair(run_spec, revision)
    with pytest.raises(plans.PlanContractError) as caught:
        plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)
    assert caught.value.error_code == "INVALID_PLAN_REVISION_BUNDLE"


@pytest.mark.parametrize(
    ("side", "path", "replacement"),
    [
        ("run_spec", ("workPlan", "nodes", 1, "logicalNodeId"), "implement-other"),
        ("run_spec", ("workPlan", "nodes", 1, "nodeType"), "VERIFY"),
        ("run_spec", ("workPlan", "nodes", 1, "businessPhase"), "VERIFYING"),
        ("run_spec", ("workPlan", "nodes", 1, "barrierOrdinal"), 2),
        ("run_spec", ("workPlan", "nodes", 1, "required"), False),
        ("run_spec", ("workPlan", "nodes", 1, "dependsOn"), []),
        ("run_spec", ("workPlan", "nodes", 1, "sideEffectClass"), "none"),
        ("run_spec", ("workPlan", "nodes", 1, "requiredArtifacts"), []),
        ("run_spec", ("workPlan", "nodes", 1, "successPredicateId"), "other-gate-v1"),
        ("run_spec", ("workPlan", "nodes", 1, "timeoutMs"), 120_001),
        ("run_spec", ("workPlan", "nodes", 1, "retryPolicyId"), "other-retry-v1"),
        ("revision", ("nodes", 1, "logicalNodeId"), "implement-other"),
        ("revision", ("nodes", 1, "nodeType"), "VERIFY"),
        ("revision", ("nodes", 1, "businessPhase"), "VERIFYING"),
        ("revision", ("nodes", 1, "barrierOrdinal"), 2),
        ("revision", ("nodes", 1, "required"), False),
        ("revision", ("nodes", 1, "dependsOn"), []),
        ("revision", ("nodes", 1, "sideEffectClass"), "none"),
        ("revision", ("nodes", 1, "requiredArtifacts"), []),
        ("revision", ("nodes", 1, "successPredicateId"), "other-gate-v1"),
        ("revision", ("nodes", 1, "timeoutMs"), 120_001),
        ("revision", ("nodes", 1, "retryPolicyId"), "other-retry-v1"),
    ],
)
def test_bundle_rejects_individually_valid_common_node_drift(
    side: str,
    path: tuple[object, ...],
    replacement: object,
) -> None:
    """两份合同的共同 node 语义必须逐节点一致，不能只核对总 hash。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    _mutate_path(run_spec if side == "run_spec" else revision, path, replacement)
    _recompute_changed_side(side, run_spec, revision)

    _assert_lossless_contract_pair(run_spec, revision)
    with pytest.raises(plans.PlanContractError):
        plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)


@pytest.mark.parametrize(
    ("side", "path", "replacement"),
    [
        ("run_spec", ("workPlan", "barriers", 1, "businessPhase"), "VERIFYING"),
        ("run_spec", ("workPlan", "barriers", 1, "barrierOrdinal"), 2),
        ("run_spec", ("workPlan", "barriers", 1, "requiredNodeIds"), ["plan"]),
        ("run_spec", ("workPlan", "barriers", 1, "settleTimeoutMs"), 120_001),
        ("run_spec", ("workPlan", "barriers", 1, "passPredicateId"), "other-barrier-v1"),
        ("revision", ("barriers", 1, "businessPhase"), "VERIFYING"),
        ("revision", ("barriers", 1, "barrierOrdinal"), 2),
        ("revision", ("barriers", 1, "requiredNodeIds"), ["plan"]),
        ("revision", ("barriers", 1, "settleTimeoutMs"), 120_001),
        ("revision", ("barriers", 1, "passPredicateId"), "other-barrier-v1"),
    ],
)
def test_bundle_rejects_individually_valid_common_barrier_drift(
    side: str,
    path: tuple[object, ...],
    replacement: object,
) -> None:
    """barrier 的 phase 与 node 集必须按 ordinal 对齐，不能只比较数量。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    _mutate_path(run_spec if side == "run_spec" else revision, path, replacement)
    _recompute_changed_side(side, run_spec, revision)

    _assert_lossless_contract_pair(run_spec, revision)
    with pytest.raises(plans.PlanContractError):
        plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)


@pytest.mark.parametrize("stage", ["DESIGN_APPROVED", "CODEX_APPROVED"])
def test_bundle_rejects_stage_map_reference_outside_revision_nodes(stage: str) -> None:
    """stageMaps 内每个 node reference 都必须解析到同一不可变 revision。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    revision["stageMaps"][stage] = ["missing-node"]
    revision["planRevisionDigest"] = _unchecked_plan_revision_digest(revision)

    # schema 只证明形状；领域 bundle 必须补上引用完整性。
    _assert_lossless_contract_pair(run_spec, revision)
    with pytest.raises(plans.PlanContractError) as caught:
        plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)
    assert caught.value.error_code == "INVALID_PLAN_REVISION_BUNDLE"


@pytest.mark.parametrize("side", ["run_spec", "revision"])
def test_plan_revision_bundle_rejects_unknown_contract_fields(side: str) -> None:
    """canonical 入库前必须用冻结 schema 拒绝未知字段。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    target = run_spec if side == "run_spec" else revision
    target["forgedUnknownField"] = "must-reject"

    with pytest.raises(plans.PlanContractError) as caught:
        plans.PlanRevisionBundle.from_contracts(run_spec=run_spec, plan_revision=revision)
    assert caught.value.error_code == "INVALID_PLAN_REVISION_BUNDLE"


def test_dual_canonical_persistence_round_trip_is_byte_exact() -> None:
    """重启 hydration 必须从两份 canonical bytes 恢复同一 bundle。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    record = _persistence_record(run_spec, revision)

    restored = plans.PlanRevisionBundle.from_persistence_record(record)

    assert restored.canonical_run_spec == record["canonical_run_spec"]
    assert restored.canonical_plan_revision == record["canonical_plan_revision"]
    assert canonicalize(restored.run_spec) == record["canonical_run_spec"]
    assert canonicalize(restored.plan_revision) == record["canonical_plan_revision"]


@pytest.mark.parametrize(
    ("selector", "replacement"),
    [
        ("plan_revision_id", "plan-other"),
        ("task_id", "task-other"),
        ("spec_revision", 3),
        ("parent_revision_id", "plan-parent-other"),
        ("intent_authorization_id", "intent-other"),
        ("semantic_plan_hash", "sha256:" + "f" * 64),
        ("plan_revision_digest", "sha256:" + "e" * 64),
        ("dag_version", 2),
        ("node_capability_map_version", "node-map-other"),
        ("stage_capability_map_version", "stage-map-other"),
        ("created_at", "2026-08-12T00:00:01Z"),
    ],
)
def test_persistence_hydration_rejects_every_selector_drift(selector: str, replacement: object) -> None:
    """SQLite selector 与 canonical contract 任一漂移都必须 fail closed。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    record = _persistence_record(run_spec, revision)
    assert record[selector] != replacement, f"selector mutation 不得 no-op：{selector}"
    record[selector] = replacement

    with pytest.raises(plans.PlanPersistenceError) as caught:
        plans.PlanRevisionBundle.from_persistence_record(record)
    assert caught.value.error_code == "PLAN_REVISION_PERSISTENCE_MISMATCH"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("run_spec_schema_id", "run-spec.v2"),
        ("run_spec_schema_version", 2),
        ("plan_revision_schema_id", "plan-revision.v2"),
        ("plan_revision_schema_version", 2),
    ],
)
def test_persistence_hydration_rejects_unknown_schema_identity(field: str, replacement: object) -> None:
    """schema id/version 不得通过猜测兼容或宽松 fallback。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    record = _persistence_record(run_spec, revision)
    record[field] = replacement

    with pytest.raises(plans.PlanPersistenceError) as caught:
        plans.PlanRevisionBundle.from_persistence_record(record)
    assert caught.value.error_code == "UNSUPPORTED_PLAN_SCHEMA"


@pytest.mark.parametrize("blob_field", ["canonical_run_spec", "canonical_plan_revision"])
def test_persistence_hydration_rejects_noncanonical_json(blob_field: str) -> None:
    """语义相同但非 JCS bytes 也不得作为 canonical blob 入库。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    record = _persistence_record(run_spec, revision)
    value = run_spec if blob_field == "canonical_run_spec" else revision
    record[blob_field] = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")

    with pytest.raises(plans.PlanPersistenceError) as caught:
        plans.PlanRevisionBundle.from_persistence_record(record)
    assert caught.value.error_code == "NONCANONICAL_PLAN_BLOB"


@pytest.mark.parametrize("blob_field", ["canonical_run_spec", "canonical_plan_revision"])
def test_persistence_hydration_rejects_invalid_utf8(blob_field: str) -> None:
    """非法 UTF-8 不得被替换字符、系统 locale 或宽松 decoder 洗白。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    record = _persistence_record(run_spec, revision)
    record[blob_field] = b'{"invalid":"\xff"}'

    with pytest.raises(plans.PlanPersistenceError) as caught:
        plans.PlanRevisionBundle.from_persistence_record(record)
    assert caught.value.error_code == "INVALID_PLAN_BLOB_ENCODING"


@pytest.mark.parametrize(
    "required_field",
    ["specRevision", "parentRevisionId", "planRevisionDigest", "createdAt"],
)
def test_persisted_run_spec_requires_fields_that_schema_marks_optional(required_field: str) -> None:
    """持久合同不得以 schema 可选为由丢失重启所需 revision 谱系。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    del run_spec[required_field]
    record = _persistence_record(run_spec, revision)

    with pytest.raises(plans.PlanPersistenceError) as caught:
        plans.PlanRevisionBundle.from_persistence_record(record)
    assert caught.value.error_code == "INCOMPLETE_PERSISTED_RUN_SPEC"


def test_plan_barrier_remains_lossless_spec_without_runtime_identity() -> None:
    """Plan 只保存无损 barrier spec；运行时 bar_<hash> 必须按真实 run 另行派生。"""
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    bundle = plans.PlanRevisionBundle.from_contracts(
        run_spec=deepcopy(run_spec),
        plan_revision=deepcopy(revision),
    )

    barrier = bundle.plan_revision["barriers"][0]
    assert barrier == bundle.run_spec["workPlan"]["barriers"][0]
    assert set(barrier) == {
        "businessPhase",
        "barrierOrdinal",
        "requiredNodeIds",
        "settleTimeoutMs",
        "passPredicateId",
    }
    assert "barrierId" not in barrier
    assert "nodeIds" not in barrier
    assert "runId" not in barrier


def test_append_get_plan_revision_survives_database_close_and_reopen(tmp_path: Path) -> None:
    """append/get 必须真实落库；关闭重开后仍逐项重验双 canonical 与 selectors。"""
    database_module = _database_module()
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    expected = _persistence_record(run_spec, revision)
    database_path = (tmp_path / "plan-revision-reopen.sqlite3").resolve()
    database_path.relative_to(Path("D:/codex项目").resolve())

    database = database_module.SqliteDatabase(database_path)
    database.open()
    try:
        uow = database.new_unit_of_work()
        uow.begin_immediate()
        uow.workflow.append_plan_revision(expected)
        uow.precommit()
        uow.commit()
    finally:
        database.close()

    reopened = database_module.SqliteDatabase(database_path)
    reopened.open()
    try:
        loaded = reopened.new_unit_of_work().workflow.get_plan_revision(revision["planRevisionId"])
        assert isinstance(loaded, Mapping)
        assert dict(loaded) == expected
        hydrated = plans.PlanRevisionBundle.from_persistence_record(loaded)
        assert hydrated.canonical_run_spec == expected["canonical_run_spec"]
        assert hydrated.canonical_plan_revision == expected["canonical_plan_revision"]
    finally:
        reopened.close()


@pytest.mark.parametrize("blob_column", ["canonical_run_spec", "canonical_plan_revision"])
def test_raw_blob_corruption_after_close_reopen_is_rejected_by_production_hydration(
    tmp_path: Path,
    blob_column: str,
) -> None:
    """incremental BLOB 写模拟磁盘腐化；reopen 后必须由生产 hydration fail closed。"""
    database_module = _database_module()
    plans = _plans_module()
    run_spec, revision = _valid_bundle_inputs()
    expected = _persistence_record(run_spec, revision)
    database_path = (tmp_path / f"plan-revision-corrupt-{blob_column}.sqlite3").resolve()
    database_path.relative_to(Path("D:/codex项目").resolve())

    database = database_module.SqliteDatabase(database_path)
    database.open()
    try:
        uow = database.new_unit_of_work()
        uow.begin_immediate()
        uow.workflow.append_plan_revision(expected)
        uow.precommit()
        uow.commit()
    finally:
        database.close()

    # blobopen 绕过 UPDATE trigger，仅改同长一字节，保留合法 UTF-8/JCS 形状以聚焦 selector/digest 重验。
    with sqlite3.connect(database_path) as raw:
        row = raw.execute(
            f'SELECT rowid, "{blob_column}" FROM plan_revisions WHERE plan_revision_id=?',  # noqa: S608
            (revision["planRevisionId"],),
        ).fetchone()
        assert row is not None
        rowid, stored_blob = row
        assert isinstance(stored_blob, bytes)
        marker = b"task-domain-1"
        offset = stored_blob.index(marker) + len(marker) - 1
        with raw.blobopen("plan_revisions", blob_column, rowid, readonly=False) as blob:
            blob.seek(offset)
            blob.write(b"X")
        raw.commit()

    reopened = database_module.SqliteDatabase(database_path)
    reopened.open()
    try:
        loaded = reopened.new_unit_of_work().workflow.get_plan_revision(revision["planRevisionId"])
        with pytest.raises(plans.PlanPersistenceError) as caught:
            plans.PlanRevisionBundle.from_persistence_record(loaded)
        assert caught.value.error_code == "PLAN_REVISION_PERSISTENCE_MISMATCH"
    finally:
        reopened.close()
