"""tests.contract.test_plan_hash_vectors — 跨语言 canonical/plan-hash 向量测试。

本测试从 contracts/golden/ 下的冻结向量驱动断言，确保 Python 实现的
canonical JSON、semanticPlanHash、planRevisionDigest、barrierId 输出与
冻结值字节级一致。同一批 golden 向量同时供 TypeScript / Rust 实现校验，
是三语言字节一致性的权威锚点。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from factory_agent.policy.canonical_json import (
    CanonicalJsonError,
    canonicalize_json_text,
)
from factory_agent.policy.plan_hash import (
    barrier_id,
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
        """相同语义、不同谱系（parentRevisionId）的 digest 必须不同。"""
        inv = _PLAN["invariants"]["differentLineageDiffersDigest"]
        by_name = {c["name"]: c for c in _PLAN["planRevisionDigest"]}
        assert by_name[inv["a"]]["expected"] != by_name[inv["b"]]["expected"]

    def test_signature_excluded_from_digest(self) -> None:
        """signature 字段不得影响 planRevisionDigest。"""
        case = _PLAN["planRevisionDigest"][0]
        revision = case["revision"]
        without_sig = {k: v for k, v in revision.items() if k != "signature"}
        assert plan_revision_digest(without_sig) == case["expected"]


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
