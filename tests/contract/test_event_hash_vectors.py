"""tests.contract.test_event_hash_vectors — 跨语言 event-hash 向量测试（Python 端）。

本测试从 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json 读取
冻结向量，断言 Python 物化器（factory_agent.domain.events）产出的 eventId、
payloadDigest、DurableEventV2 链与冻结值字节级一致。同一批 golden 向量同时供
TypeScript / Rust 实现校验，是 EVENT-HASH-001 三语言字节一致性的权威锚点。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from factory_agent.domain.events import (
    EventHashError,
    event_id,
    materialize_batch,
    payload_digest,
)
from factory_agent.policy.canonical_json import canonicalize_json_text
from tests.conftest import REPO_ROOT

# golden 向量目录
_GOLDEN = REPO_ROOT / "contracts" / "golden"


def _load_golden(name: str) -> dict[str, Any]:
    """读取并解析指定 golden 向量文件。

    Args:
        name: 文件名（如 "event-hash.v2.json"）。

    Returns:
        解析后的向量字典。
    """
    return json.loads((_GOLDEN / name).read_text(encoding="utf-8"))


# ── event-hash 向量 ────────────────────────────────────────────────────────────

_EVENT = _load_golden("event-hash.v2.json")


class TestEventIdVectors:
    """eventId：幂等身份，仅由 ingestEventId 决定，输出必须等于冻结值。"""

    @pytest.mark.parametrize(
        "case",
        _EVENT["eventId"],
        ids=[c["name"] for c in _EVENT["eventId"]],
    )
    def test_event_id(self, case: dict[str, Any]) -> None:
        """计算结果必须与冻结 expected 一致。"""
        assert event_id(case["ingestEventId"]) == case["expected"]

    def test_idempotent(self) -> None:
        """同一 ingestEventId 多次计算必须得到同一 eventId（崩溃重试幂等）。"""
        inv = _EVENT["invariants"]["idempotentEventId"]
        by_name = {c["name"]: c for c in _EVENT["eventId"]}
        ingest = by_name[inv["a"]]["ingestEventId"]
        assert event_id(ingest) == event_id(ingest)


class TestPayloadDigestVectors:
    """payloadDigest：脱敏 payload 的 JCS 摘要，输出必须等于冻结值。"""

    @pytest.mark.parametrize(
        "case",
        _EVENT["payloadDigest"],
        ids=[c["name"] for c in _EVENT["payloadDigest"]],
    )
    def test_payload_digest(self, case: dict[str, Any]) -> None:
        """计算结果必须与冻结 expected 一致。"""
        assert payload_digest(case["payload"]) == case["expected"]


class TestMaterializeVectors:
    """materialize_batch：物化整批事件，逐字段必须等于冻结 DurableEventV2 列表。"""

    @pytest.mark.parametrize(
        "case",
        _EVENT["materialize"],
        ids=[c["name"] for c in _EVENT["materialize"]],
    )
    def test_materialize(self, case: dict[str, Any]) -> None:
        """物化输出（含 eventId/taskSeq/previousHead/eventDigest/head 链）必须一致。"""
        result = materialize_batch(case["batch"], case["anchor"], case["durableAt"])
        assert result == case["expected"]

    def test_interleaved_tasks_independent(self) -> None:
        """多 Task 交错：taskA 与 taskB 各自从 genesis 起链，taskSeq 与链互不影响。"""
        inv = _EVENT["invariants"]["interleavedTasksIndependent"]
        by_name = {c["name"]: c for c in _EVENT["materialize"]}
        a = by_name[inv["taskA"]]
        b = by_name[inv["taskB"]]
        ra = materialize_batch(a["batch"], a["anchor"], a["durableAt"])
        rb = materialize_batch(b["batch"], b["anchor"], b["durableAt"])
        # 两条链各自 genesis（taskSeq 从 0 起），eventDigest 相互独立（previousEventDigest 单链模型）。
        assert ra[0]["taskSeq"] == 0
        assert rb[0]["taskSeq"] == 0
        assert ra[0]["eventDigest"] != rb[0]["eventDigest"]
        # previousEventDigest 都是全零 predecessor（genesis）
        assert ra[0]["previousEventDigest"] == "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        assert rb[0]["previousEventDigest"] == "sha256:0000000000000000000000000000000000000000000000000000000000000000"


class TestMaterializeRejectVectors:
    """物化拒绝向量：前驱漂移、竞争抢占、空批、重复 ID、taskId 不一致等必须 fail closed。"""

    @pytest.mark.parametrize(
        "case",
        _EVENT["reject"],
        ids=[c["name"] for c in _EVENT["reject"]],
    )
    def test_reject(self, case: dict[str, Any]) -> None:
        """非法输入必须抛出 EventHashError（错误码 event-hash-error）。"""
        with pytest.raises(EventHashError):
            materialize_batch(case["batch"], case["anchor"], case["durableAt"])

    def test_predecessor_drift_rejected(self) -> None:
        """前驱漂移用例名必须指向 reject 集合中的实际用例。"""
        name = _EVENT["invariants"]["predecessorDriftRejected"]
        assert any(c["name"] == name for c in _EVENT["reject"])

    def test_competing_batch_only_one_wins(self) -> None:
        """竞争批次：败者（previousHead 已过期）必须被拒。"""
        name = _EVENT["invariants"]["competingBatchOnlyOneWins"]
        case = next(c for c in _EVENT["reject"] if c["name"] == name)
        with pytest.raises(EventHashError):
            materialize_batch(case["batch"], case["anchor"], case["durableAt"])

    def test_error_code_stable(self) -> None:
        """错误码必须稳定为 event-hash-error。"""
        assert EventHashError.error_code == "event-hash-error"


# ── prepared-batch canonical 向量 ──────────────────────────────────────────────

_BATCH = _load_golden("prepared-batch.v2.json")


class TestPreparedBatchCanonicalVectors:
    """PreparedBatchV2 canonical 字节：JCS 规范化输出必须等于冻结值。"""

    @pytest.mark.parametrize(
        "case",
        _BATCH["cases"],
        ids=[c["name"] for c in _BATCH["cases"]],
    )
    def test_canonical(self, case: dict[str, Any]) -> None:
        """整个 PreparedBatchV2 规范化后必须与冻结 expectedCanonical 字节一致。"""
        text = json.dumps(case["batch"], ensure_ascii=False)
        out = canonicalize_json_text(text).decode("utf-8")
        assert out == case["expectedCanonical"]
