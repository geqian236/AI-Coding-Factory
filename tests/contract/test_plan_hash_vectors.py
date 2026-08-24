"""tests.contract.test_plan_hash_vectors — 跨语言 canonical/plan-hash 向量测试。

本测试从 contracts/golden/ 下的冻结向量驱动断言，确保 Python 实现的
canonical JSON、semanticPlanHash、planRevisionDigest、barrierId 输出与
冻结值字节级一致。同一批 golden 向量同时供 TypeScript / Rust 实现校验，
是三语言字节一致性的权威锚点。
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from factory_agent.contracts.generated import models as generated_models
from factory_agent.policy import plan_hash as plan_hash_module
from factory_agent.policy.canonical_json import (
    CanonicalJsonError,
    canonicalize_json_text,
)
from factory_agent.policy.plan_hash import (
    PlanHashError,
    barrier_id,
    build_semantic_projection,
    plan_revision_digest,
    semantic_plan_hash,
)

from tests.conftest import REPO_ROOT

# golden 向量目录
_GOLDEN = REPO_ROOT / "contracts" / "golden"


def _load_golden(name: str) -> dict[str, Any]:
    """读取并解析指定 golden 向量文件。

    Args:
        name: 文件名（如 "canonical-json.v1.json"）。

    Returns:
        解析后的向量字典。
    """
    return json.loads((_GOLDEN / name).read_text(encoding="utf-8"))


# ── canonical-json 向量 ────────────────────────────────────────────────────────

_CANONICAL = _load_golden("canonical-json.v1.json")


class TestCanonicalJsonAcceptVectors:
    """canonical JSON 接受向量：输入文本规范化后必须等于冻结的 expectedCanonical。"""

    @pytest.mark.parametrize(
        "case",
        _CANONICAL["accept"],
        ids=[c["name"] for c in _CANONICAL["accept"]],
    )
    def test_accept(self, case: dict[str, Any]) -> None:
        """规范化输出必须与冻结值字节级一致。"""
        out = canonicalize_json_text(case["inputJsonText"]).decode("utf-8")
        assert out == case["expectedCanonical"]


class TestCanonicalJsonRejectVectors:
    """canonical JSON 拒绝向量：非法输入必须抛出 CanonicalJsonError。"""

    @pytest.mark.parametrize(
        "case",
        _CANONICAL["reject"],
        ids=[c["name"] for c in _CANONICAL["reject"]],
    )
    def test_reject(self, case: dict[str, Any]) -> None:
        """NaN/Infinity/小数/超范围/重复键必须 fail closed。"""
        with pytest.raises(CanonicalJsonError):
            canonicalize_json_text(case["inputJsonText"])
        assert case["expectedErrorCode"] == "canonical-json-error"


# ── plan-hash 向量 ─────────────────────────────────────────────────────────────

_PLAN = _load_golden("plan-hash.v1.json")


def _find_named_case(cases: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """按共享 golden 名称查找基准用例，找不到即让测试配置失败。"""
    for case in cases:
        if case["name"] == name:
            return case
    raise AssertionError(f"missing golden base case: {name}")


def _apply_golden_mutation(payload: object, mutation: dict[str, Any]) -> object:
    """对单份基准 payload 应用一条声明式变异，调用方必须每次传入独立副本。"""
    path = mutation["path"]
    assert isinstance(path, list) and path

    # 路径段既可定位对象键，也可用十进制字符串定位数组下标；DAG 反例仍由同一 golden 驱动。
    target: Any = payload
    for segment in path[:-1]:
        if isinstance(target, dict):
            target = target[segment]
        else:
            assert isinstance(target, list)
            target = target[int(segment)]

    if mutation["op"] == "remove":
        if isinstance(target, dict):
            del target[path[-1]]
        else:
            assert isinstance(target, list)
            del target[int(path[-1])]
    elif mutation["op"] == "replace":
        if isinstance(target, dict):
            target[path[-1]] = deepcopy(mutation["value"])
        else:
            assert isinstance(target, list)
            target[int(path[-1])] = deepcopy(mutation["value"])
    else:
        raise AssertionError(f"unsupported golden mutation: {mutation['op']}")
    return payload


def _materialize_mutation_cases(
    case: dict[str, Any],
    source_cases: list[dict[str, Any]],
    payload_key: str,
) -> list[tuple[str, object]]:
    """从共享 golden 的声明式变异生成原始输入，不在各语言测试手抄反例。

    golden 只声明基准、路径和替换值；本帮助函数保证 Python、TypeScript、Rust 都消费
    同一组非法 wire。它只服务测试，运行时仍必须由权威 JSON Schema 拒绝输入。
    """
    if "input" in case:
        return [(case["name"], deepcopy(case["input"]))]

    base = _find_named_case(source_cases, case["base"])
    mutations = case.get("mutations")
    if mutations is None:
        mutations = [case["mutation"]]
    assert isinstance(mutations, list) and mutations
    materialized: list[tuple[str, object]] = []
    for ordinal, mutation in enumerate(mutations):
        assert isinstance(mutation, dict)
        label = mutation.get("name", str(ordinal))
        assert isinstance(label, str) and label
        # 一个 golden case 可以承载多个同类反例，维持冻结 pytest 节点数仍逐项执行。
        materialized.append(
            (f"{case['name']}:{label}", _apply_golden_mutation(deepcopy(base[payload_key]), mutation))
        )
    return materialized


class TestSemanticPlanHashVectors:
    """semanticPlanHash：实现输出必须等于冻结值。"""

    @pytest.mark.parametrize(
        "case",
        _PLAN["semanticPlanHash"],
        ids=[c["name"] for c in _PLAN["semanticPlanHash"]],
    )
    def test_semantic_hash(self, case: dict[str, Any]) -> None:
        """计算结果必须与冻结 expected 一致。"""
        assert semantic_plan_hash(case["plan"]) == case["expected"]

    def test_same_semantics_share_hash(self) -> None:
        """语义相同但排除字段不同的两个计划，semanticPlanHash 必须相同。"""
        inv = _PLAN["invariants"]["sameSemanticsShareHash"]
        by_name = {c["name"]: c for c in _PLAN["semanticPlanHash"]}
        assert by_name[inv["a"]]["expected"] == by_name[inv["b"]]["expected"]


class TestRunSpecSemanticValidation:
    """非法 RunSpec 不得获得投影或 semanticPlanHash。"""

    @pytest.mark.parametrize(
        "case",
        _PLAN["invalidRunSpec"],
        ids=[c["name"] for c in _PLAN["invalidRunSpec"]],
    )
    def test_projection_and_hash_reject_shared_invalid_vectors(self, case: dict[str, Any]) -> None:
        """投影入口和哈希入口都必须在 canonicalize 前 fail closed。"""
        materialized = _materialize_mutation_cases(case, _PLAN["semanticPlanHash"], "plan")
        if case["name"] == "missing-base-commit":
            assert [name for name, _ in materialized] == ["missing-base-commit:0"]
        if case["name"] == "invalid-base-commit-or-zero-sentinel":
            empty_name_case = deepcopy(case)
            empty_name_case["mutations"][0]["name"] = ""
            with pytest.raises(AssertionError):
                _materialize_mutation_cases(empty_name_case, _PLAN["semanticPlanHash"], "plan")
        for name, plan in materialized:
            with pytest.raises(PlanHashError) as projection_error:
                build_semantic_projection(plan)
            assert projection_error.value.error_code == case["expectedErrorCode"], name

            with pytest.raises(PlanHashError) as hash_error:
                semantic_plan_hash(plan)
            assert hash_error.value.error_code == case["expectedErrorCode"], name

    def test_generated_schema_is_runtime_source_independent_and_corruption_is_stable(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """hash API 只能读取生成常量；常量损坏也只能返回稳定 plan-hash-error。"""
        plan = deepcopy(_PLAN["semanticPlanHash"][0]["plan"])

        # 运行时不得回读权威 schema 文件：即使路径读取全面失败，嵌入常量仍可正常构建 validator。
        with monkeypatch.context() as patch:
            patch.setattr(
                Path,
                "read_bytes",
                lambda *args, **kwargs: (_ for _ in ()).throw(OSError("source unavailable")),
            )
            plan_hash_module._run_spec_validator.cache_clear()
            assert semantic_plan_hash(plan).startswith("sha256:")

        revision = deepcopy(_PLAN["planRevisionDigest"][0]["revision"])
        schema_cases = (
            (
                "RUN_SPEC_SCHEMA_JSON",
                generated_models.RUN_SPEC_SCHEMA_JSON,
                "_run_spec_validator",
                lambda: semantic_plan_hash(plan),
            ),
            (
                "PLAN_REVISION_SCHEMA_JSON",
                generated_models.PLAN_REVISION_SCHEMA_JSON,
                "_plan_revision_validator",
                lambda: plan_revision_digest(revision),
            ),
            (
                "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON",
                generated_models.PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
                "_plan_revision_digest_material_validator",
                lambda: plan_revision_digest(revision),
            ),
        )
        for constant_name, original_json, cache_name, invoke in schema_cases:
            expected_sha = "sha256:" + sha256(original_json.encode("utf-8")).hexdigest()
            corruptions = (
                original_json[:-1] + ',"required":[]}',
                original_json.replace('"title":"', '"title":"X', 1),
            )
            assert all(corrupted != original_json for corrupted in corruptions)
            for corrupted in corruptions:
                with monkeypatch.context() as patch:
                    patch.setattr(plan_hash_module, constant_name, corrupted, raising=False)
                    patch.setattr(
                        plan_hash_module,
                        constant_name.replace("_JSON", "_JSON_SHA256"),
                        expected_sha,
                        raising=False,
                    )
                    validator_cache = getattr(plan_hash_module, cache_name, None)
                    if validator_cache is not None:
                        validator_cache.cache_clear()
                    with pytest.raises(PlanHashError) as error:
                        invoke()
                    assert error.value.error_code == "plan-hash-error"
                    assert str(error.value).endswith("INVALID_PLAN_HASH_INPUT")
                if validator_cache is not None:
                    validator_cache.cache_clear()


class TestRfc3339DateTimeGolden:
    """三端共享的 RFC3339 子集必须同时约束 RunSpec 与 PlanRevision。"""

    @pytest.mark.parametrize(
        "case",
        _PLAN["rfc3339DateTime"]["valid"],
        ids=[c["name"] for c in _PLAN["rfc3339DateTime"]["valid"]],
    )
    def test_valid_values_are_hashable_for_both_contracts(self, case: dict[str, Any]) -> None:
        """权威 schema 的 format/pattern 同时接受冻结的可移植子集。"""
        run_spec = deepcopy(_PLAN["semanticPlanHash"][0]["plan"])
        run_spec["createdAt"] = case["value"]
        revision = deepcopy(_PLAN["planRevisionDigest"][0]["revision"])
        revision["createdAt"] = case["value"]

        assert semantic_plan_hash(run_spec).startswith("sha256:")
        assert plan_revision_digest(revision).startswith("sha256:")

    @pytest.mark.parametrize(
        "case",
        _PLAN["rfc3339DateTime"]["invalid"],
        ids=[c["name"] for c in _PLAN["rfc3339DateTime"]["invalid"]],
    )
    def test_invalid_values_cannot_produce_projection_or_hash(self, case: dict[str, Any]) -> None:
        """format/pattern 任一不合规时，所有摘要入口必须在投影前拒绝。"""
        run_spec = deepcopy(_PLAN["semanticPlanHash"][0]["plan"])
        run_spec["createdAt"] = case["value"]
        revision = deepcopy(_PLAN["planRevisionDigest"][0]["revision"])
        revision["createdAt"] = case["value"]

        with pytest.raises(PlanHashError):
            build_semantic_projection(run_spec)
        with pytest.raises(PlanHashError):
            semantic_plan_hash(run_spec)
        with pytest.raises(PlanHashError):
            plan_revision_digest(revision)


class TestPlanRevisionDigestVectors:
    """planRevisionDigest：实现输出必须等于冻结值，且不同谱系必须不同。"""

    @pytest.mark.parametrize(
        "case",
        _PLAN["planRevisionDigest"],
        ids=[c["name"] for c in _PLAN["planRevisionDigest"]],
    )
    def test_digest(self, case: dict[str, Any]) -> None:
        """计算结果必须与冻结 expected 一致。"""
        assert plan_revision_digest(case["revision"]) == case["expected"]

    def test_different_lineage_differs(self) -> None:
        """谱系或任一 DAG material 变化都必须改变 digest。"""
        inv = _PLAN["invariants"]["differentLineageDiffersDigest"]
        by_name = {c["name"]: c for c in _PLAN["planRevisionDigest"]}
        assert by_name[inv["a"]]["expected"] != by_name[inv["b"]]["expected"]

        dag_inv = _PLAN["invariants"]["differentDagMaterialDiffersDigest"]
        baseline = by_name[dag_inv["base"]]["revision"]
        changed = _apply_golden_mutation(deepcopy(baseline), dag_inv["mutation"])
        assert plan_revision_digest(baseline) != plan_revision_digest(changed)

    def test_signature_excluded_from_digest(self) -> None:
        """signature 字段不得影响 planRevisionDigest。"""
        case = _PLAN["planRevisionDigest"][0]
        revision = case["revision"]
        without_sig = {k: v for k, v in revision.items() if k != "signature"}
        assert plan_revision_digest(without_sig) == case["expected"]


class TestPlanRevisionDigestValidation:
    """digest material 仅排除自身摘要和签名，其他 wire 缺陷必须拒绝。"""

    @pytest.mark.parametrize(
        "case",
        _PLAN["invalidPlanRevision"],
        ids=[c["name"] for c in _PLAN["invalidPlanRevision"]],
    )
    def test_digest_rejects_shared_invalid_vectors(self, case: dict[str, Any]) -> None:
        """缺字段、未知字段和类型错误都不能被排除规则掩盖。"""
        for name, revision in _materialize_mutation_cases(case, _PLAN["planRevisionDigest"], "revision"):
            with pytest.raises(PlanHashError) as error:
                plan_revision_digest(revision)
            assert error.value.error_code == case["expectedErrorCode"], name


# ── barrier-id 向量 ────────────────────────────────────────────────────────────

_BARRIER = _load_golden("barrier-id.v1.json")


class TestBarrierIdVectors:
    """barrierId：域分离数组编码，实现输出必须等于冻结值。"""

    @pytest.mark.parametrize(
        "case",
        _BARRIER["cases"],
        ids=[c["name"] for c in _BARRIER["cases"]],
    )
    def test_barrier_id(self, case: dict[str, Any]) -> None:
        """计算结果必须与冻结 expected 一致。"""
        result = barrier_id(
            case["runId"],
            case["planRevisionDigest"],
            case["businessPhase"],
            case["barrierOrdinal"],
        )
        assert result == case["expected"]

    def test_all_barrier_ids_distinct(self) -> None:
        """四组不同输入必须产生四个不同 barrierId（无碰撞）。"""
        ids = {c["expected"] for c in _BARRIER["cases"]}
        assert len(ids) == len(_BARRIER["cases"])

    def test_barrier_id_prefix(self) -> None:
        """barrierId 必须带 'bar_' 前缀且为 64 位十六进制。"""
        for case in _BARRIER["cases"]:
            assert case["expected"].startswith("bar_")
            assert len(case["expected"]) == len("bar_") + 64


class TestBarrierIdRejectsInvalidInput:
    """barrierId 非法输入必须 fail closed。"""

    def test_rejects_bool_ordinal(self) -> None:
        """barrierOrdinal 为 bool 时必须拒绝（bool 是 int 子类的陷阱）。"""
        from factory_agent.policy.plan_hash import PlanHashError

        with pytest.raises(PlanHashError):
            barrier_id("run-1", "sha256:abc", "IMPLEMENTING", True)  # type: ignore[arg-type]

    def test_rejects_empty_run_id(self) -> None:
        """空 runId 必须拒绝。"""
        from factory_agent.policy.plan_hash import PlanHashError

        with pytest.raises(PlanHashError):
            barrier_id("", "sha256:abc", "IMPLEMENTING", 1)
