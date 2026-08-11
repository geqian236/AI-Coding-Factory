"""tests.contract.test_event_hash_vectors — 跨语言 event-hash 向量测试（Python 端）。

本测试从 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json 读取
冻结向量，断言 Python 物化器（factory_agent.domain.events）产出的 eventId、
payloadDigest、DurableEventV2 链与冻结值字节级一致。同一批 golden 向量同时供
TypeScript / Rust 实现校验，是 EVENT-HASH-001 三语言字节一致性的权威锚点。
"""

from __future__ import annotations

import importlib.util
import json
import unicodedata
from copy import deepcopy
from typing import Any

import jsonschema
import pytest
from factory_agent.domain import events as event_contracts
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


def _load_schema(name: str) -> dict[str, Any]:
    """读取一份权威 JSON Schema，供事件合同边界测试直接使用。"""
    schema_path = REPO_ROOT / "contracts" / "schemas" / name
    assert schema_path.is_file(), f"缺少事件合同 schema: {name}"
    return json.loads(schema_path.read_text(encoding="utf-8"))


# ── event-hash 向量 ────────────────────────────────────────────────────────────

_EVENT = _load_golden("event-hash.v2.json")
_EXPECTED_REJECT_MUTATION_NAMES = {
    "input-root-unknown",
    "event-missing-run-id",
    "event-writer-field",
    "event-coarse-type",
    "event-nested-process-unknown",
    "event-digest-uppercase",
    "event-frame-digest-uppercase",
    "process-runtime-wsl-process",
    "process-runtime-container",
    "process-wsl-docker-missing-container",
    "process-local-missing-job",
    "process-local-unexpected-wsl",
    "source-span-equal-range",
    "source-span-reversed-range",
    "source-span-none-with-offset",
    "source-span-bool",
    "source-span-unsafe-integer",
    "sanitized-span-equal-range",
    "sanitized-span-reversed-range",
    "sanitized-span-bool",
    "redaction-range-equal",
    "redaction-range-reversed",
    "redaction-range-bool",
    "redaction-replacement-nonfrozen",
    "redaction-replacement-uppercase-tag",
    "redaction-nested-unknown",
    "wall-time-invalid",
    "ingested-at-invalid",
    "process-start-time-invalid",
    "head-task-seq-bool",
    "head-task-seq-unsafe-integer",
    "source-seq-bool",
    "source-seq-unsafe-integer",
    "batch-ordinal-bool",
    "batch-ordinal-unsafe-integer",
    "run-seq-bool",
    "run-seq-unsafe-integer",
    "monotonic-time-bool",
    "monotonic-time-unsafe-integer",
    "pid-bool",
    "pid-unsafe-integer",
    "checked-task-seq-overflow",
    "anchor-event-digest-drift",
    "anchor-task-seq-drift",
    "empty-events",
    "duplicate-ingest-id",
    "nfc-equivalent-ingest-id",
    "multi-task-slice",
    "model-summary-fail-closed",
    "state-changed-adapter-lane",
    "event-authoritative-durability-class",
    "missing-source-seq",
    "missing-source-span",
    "missing-process-identity",
    "stream-event-null-stream-id",
    "anchor-unknown-field",
    "batch-ordinal-negative",
    "batch-ordinal-nonmonotonic",
    "missing-coordinator-field",
    "half-null-input-head",
    "digest-pattern-short",
    "digest-pattern-wrong-prefix",
    "sanitized-span-unsafe-integer",
    "redaction-range-unsafe-integer",
}
_EXPECTED_INTEGER_CASE_NAMES = {
    "integer-one",
    "decimal-integral-one",
    "exponent-integral-one",
    "negative-zero",
    "fractional-one-point-five",
    "nan",
    "positive-infinity",
    "negative-infinity",
    "unsafe-positive-integer",
    "boolean-true",
}
_EXPECTED_SOURCE_SPAN_CASE_NAMES = {
    "provider-bytes-byte-range",
    "master-provider-bytes-frame-null",
    "provider-chars-byte-range",
    "provider-chars-field-null",
    "provider-bytes-none-null",
    "none-null",
    "provider-bytes-byte-start-null",
    "provider-bytes-byte-equal-range",
    "provider-bytes-frame-start-present",
    "provider-chars-field-end-present",
    "provider-bytes-none-end-present",
    "none-frame-precision",
    "none-start-present",
    "legacy-coordinate-byte",
}


def _json_pointer_parts(pointer: str) -> list[str]:
    """解析测试向量使用的 JSON Pointer；只接受绝对 pointer。"""
    if pointer == "":
        return []
    assert pointer.startswith("/")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _apply_json_pointer_mutation(
    document: dict[str, Any],
    mutation: dict[str, Any],
) -> dict[str, Any]:
    """原地执行单个 add/remove/replace，并返回可精确恢复的逆操作。"""
    parts = _json_pointer_parts(mutation["path"])
    if not parts:
        assert mutation["operation"] == "replace"
        previous = deepcopy(document)
        replacement = deepcopy(mutation["value"])
        assert isinstance(replacement, dict)
        document.clear()
        document.update(replacement)
        return {"operation": "replace", "path": "", "value": previous}
    parent: Any = document
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    leaf = parts[-1]
    operation = mutation["operation"]

    if operation == "add":
        value = deepcopy(mutation["value"])
        if isinstance(parent, list):
            index = len(parent) if leaf == "-" else int(leaf)
            parent.insert(index, value)
            inverse_path = "/" + "/".join([*parts[:-1], str(index)])
        else:
            assert leaf not in parent
            parent[leaf] = value
            inverse_path = mutation["path"]
        return {"operation": "remove", "path": inverse_path}

    if isinstance(parent, list):
        index = int(leaf)
        previous = deepcopy(parent[index])
        if operation == "remove":
            parent.pop(index)
        else:
            assert operation == "replace"
            parent[index] = deepcopy(mutation["value"])
    else:
        assert leaf in parent
        previous = deepcopy(parent[leaf])
        if operation == "remove":
            del parent[leaf]
        else:
            assert operation == "replace"
            parent[leaf] = deepcopy(mutation["value"])
    inverse: dict[str, Any] = {
        "operation": "add" if operation == "remove" else "replace",
        "path": mutation["path"],
        "value": previous,
    }
    return inverse


def _materialize_mutation_fixture(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    """把共享真实 API envelope 交给物化器，避免测试夹具隐藏字段漂移。"""
    return materialize_batch(fixture["input"], fixture["anchor"])


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


class TestIntegerSemantics:
    """三语事件数字统一采用 Draft7 数学整数语义，并规范化为整数输出。"""

    def test_case_names_are_an_exact_shared_set(self) -> None:
        """共享矩阵必须无重复、无静默漏跑。"""
        names = [case["name"] for case in _EVENT["integerSemantics"]]
        assert len(names) == len(set(names))
        assert set(names) == _EXPECTED_INTEGER_CASE_NAMES

    @pytest.mark.parametrize(
        "case",
        _EVENT["integerSemantics"],
        ids=[case["name"] for case in _EVENT["integerSemantics"]],
    )
    def test_source_seq_uses_mathematical_integer_semantics(self, case: dict[str, Any]) -> None:
        """1/1.0/1e0/-0 接受并输出 int；其余非整数、非有限值与越界值拒绝。"""
        fixture = deepcopy(_EVENT["rejectBase"])
        fixture["input"]["events"][0]["sourceSeq"] = json.loads(case["json"])
        if case["accepted"]:
            result = _materialize_mutation_fixture(fixture)
            assert type(result[0]["sourceSeq"]) is int
            assert result[0]["sourceSeq"] == case["expected"]
        else:
            with pytest.raises(EventHashError):
                _materialize_mutation_fixture(fixture)


class TestSourceSpanSemantics:
    """source coordinate 与 mappingPrecision 是独立维度，仅共同决定 offset 形态。"""

    def test_case_names_are_an_exact_shared_set(self) -> None:
        """三语消费同一组 transport coordinate 与精度组合正负例。"""
        names = [case["name"] for case in _EVENT["sourceSpanSemantics"]]
        assert len(names) == len(set(names))
        assert set(names) == _EXPECTED_SOURCE_SPAN_CASE_NAMES

    @pytest.mark.parametrize(
        "case",
        _EVENT["sourceSpanSemantics"],
        ids=[case["name"] for case in _EVENT["sourceSpanSemantics"]],
    )
    def test_coordinate_precision_pairing(self, case: dict[str, Any]) -> None:
        """transport 坐标只在 byte 精度携带范围；每个负例必须是单 pointer 变异。"""
        accepted = {
            item["name"]: item["span"]
            for item in _EVENT["sourceSpanSemantics"]
            if item["accepted"]
        }
        if case["accepted"]:
            fixture = deepcopy(_EVENT["rejectBase"])
            fixture["input"]["events"][0]["sourceTransportSpan"] = deepcopy(case["span"])
            events = _materialize_mutation_fixture(fixture)
            assert events[0]["sourceTransportSpan"] == case["span"]
        else:
            base_span = deepcopy(accepted[case["baseCase"]])
            base_fixture = deepcopy(_EVENT["rejectBase"])
            base_fixture["input"]["events"][0]["sourceTransportSpan"] = deepcopy(base_span)
            assert _materialize_mutation_fixture(base_fixture)[0]["sourceTransportSpan"] == base_span

            mutated_span = deepcopy(base_span)
            inverse = _apply_json_pointer_mutation(mutated_span, case["mutation"])
            fixture = deepcopy(_EVENT["rejectBase"])
            fixture["input"]["events"][0]["sourceTransportSpan"] = mutated_span
            with pytest.raises(EventHashError):
                _materialize_mutation_fixture(fixture)

            _apply_json_pointer_mutation(mutated_span, inverse)
            assert mutated_span == base_span


class TestMaterializeVectors:
    """materialize_batch：物化整批事件，逐字段必须等于冻结 DurableEventV2 列表。"""

    @pytest.mark.parametrize(
        "case",
        _EVENT["materialize"],
        ids=[c["name"] for c in _EVENT["materialize"]],
    )
    def test_materialize(self, case: dict[str, Any]) -> None:
        """物化输出（含 eventId/taskSeq/previousHead/eventDigest/head 链）必须一致。"""
        result = materialize_batch(case["input"], case["anchor"])
        assert result == case["expected"]

    def test_interleaved_tasks_independent(self) -> None:
        """多 Task 交错：taskA 与 taskB 各自从 genesis 起链，taskSeq 与链互不影响。"""
        inv = _EVENT["invariants"]["interleavedTasksIndependent"]
        by_name = {c["name"]: c for c in _EVENT["materialize"]}
        a = by_name[inv["taskA"]]
        b = by_name[inv["taskB"]]
        ra = materialize_batch(a["input"], a["anchor"])
        rb = materialize_batch(b["input"], b["anchor"])
        # 两条链各自 genesis（taskSeq 从 0 起），eventDigest 相互独立（previousEventDigest 单链模型）。
        assert ra[0]["taskSeq"] == 0
        assert rb[0]["taskSeq"] == 0
        assert ra[0]["eventDigest"] != rb[0]["eventDigest"]
        # previousEventDigest 都是全零 predecessor（genesis）
        assert ra[0]["previousEventDigest"] == "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        assert rb[0]["previousEventDigest"] == "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        # 同一真实 PreparedBatch 的全局 ordinal 可交错；每个 slice 只保留本 Task 相对顺序。
        assert a["input"]["preparedBatchId"] == b["input"]["preparedBatchId"] == "batch-multi-0"
        assert [event["batchOrdinal"] for event in ra] == [0, 2]
        assert [event["batchOrdinal"] for event in rb] == [1]


class TestMaterializeRejectVectors:
    """锚点漂移仍由单 pointer mutation 覆盖，纯函数不声称实现 CAS。"""

    def test_anchor_digest_drift_rejected(self) -> None:
        """锚点摘要漂移用例必须指向共享 mutation 集合中的实际用例。"""
        name = _EVENT["invariants"]["anchorDigestDriftRejected"]
        mutation = next(c for c in _EVENT["rejectMutations"] if c["name"] == name)
        candidate = deepcopy(_EVENT["rejectBase"])
        _apply_json_pointer_mutation(candidate, mutation)
        with pytest.raises(EventHashError):
            _materialize_mutation_fixture(candidate)

    def test_stale_expected_head_rejected_without_claiming_cas(self) -> None:
        """纯物化器只能拒绝 stale expected head；真实 CAS 仍由 Task 7/存储承担。"""
        name = _EVENT["invariants"]["staleExpectedHeadRejected"]
        mutation = next(c for c in _EVENT["rejectMutations"] if c["name"] == name)
        candidate = deepcopy(_EVENT["rejectBase"])
        _apply_json_pointer_mutation(candidate, mutation)
        with pytest.raises(EventHashError):
            _materialize_mutation_fixture(candidate)

    def test_error_code_stable(self) -> None:
        """错误码必须稳定为 event-hash-error。"""
        assert EventHashError.error_code == "event-hash-error"


class TestSinglePointerMutationVectors:
    """共享 mutation 必须从合法基座出发，且每例可由单 pointer 精确撤销。"""

    def test_base_and_valid_runtime_variants_materialize(self) -> None:
        """合法 local-windows 基座与完整 wsl-docker 身份都必须可物化。"""
        base = deepcopy(_EVENT["rejectBase"])
        _materialize_mutation_fixture(base)
        for mutation in _EVENT["validVariants"]:
            candidate = deepcopy(base)
            inverse = _apply_json_pointer_mutation(candidate, mutation)
            _materialize_mutation_fixture(candidate)
            _apply_json_pointer_mutation(candidate, inverse)
            assert candidate == base
            _materialize_mutation_fixture(candidate)

    def test_mutation_case_name_exact_set(self) -> None:
        """三语言消费同一稳定 case-name 集合，禁止静默漏跑或重复用例。"""
        names = [case["name"] for case in _EVENT["rejectMutations"]]
        assert len(names) == len(set(names))
        assert set(names) == _EXPECTED_REJECT_MUTATION_NAMES

    @pytest.mark.parametrize(
        "mutation",
        _EVENT["rejectMutations"],
        ids=[case["name"] for case in _EVENT["rejectMutations"]],
    )
    def test_single_pointer_rejects_and_inverse_restores(
        self,
        mutation: dict[str, Any],
    ) -> None:
        """base 通过、单 mutation 拒绝、逆操作恢复后再次通过。"""
        base = deepcopy(_EVENT["rejectBase"])
        _materialize_mutation_fixture(base)
        candidate = deepcopy(base)
        inverse = _apply_json_pointer_mutation(candidate, mutation)
        with pytest.raises(EventHashError) as raised:
            _materialize_mutation_fixture(candidate)
        assert raised.value.error_code == "event-hash-error"
        assert "must-not-appear" not in str(raised.value)
        _apply_json_pointer_mutation(candidate, inverse)
        assert candidate == base
        _materialize_mutation_fixture(candidate)


class TestIndependentDigestInvariants:
    """各摘要和链字段必须独立重算，eventId 只能随 ingestEventId 改变。"""

    @staticmethod
    def _observable(events: list[dict[str, Any]], label: str) -> str:
        """把 golden 中的稳定标签映射到首事件或后继链字段。"""
        if label == "successor.previousEventDigest":
            return events[1]["previousEventDigest"]
        return events[0][label]

    def test_base_digests_recompute_and_chain(self) -> None:
        """payload/redaction/event 摘要与 predecessor chain 均由实际内容重算。"""
        fixture = deepcopy(_EVENT["rejectBase"])
        events = _materialize_mutation_fixture(fixture)
        first, second = events
        assert first["payloadDigest"] == payload_digest(first["payload"])
        assert first["redactionManifestDigest"] == payload_digest(first["redactions"])
        assert first["eventDigest"] == payload_digest(
            {key: value for key, value in first.items() if key != "eventDigest"}
        )
        assert first["previousEventDigest"] == fixture["anchor"]["committedEventDigest"]
        assert second["previousEventDigest"] == first["eventDigest"]

    @pytest.mark.parametrize(
        "mutation",
        _EVENT["digestInvariants"],
        ids=[case["name"] for case in _EVENT["digestInvariants"]],
    )
    def test_covered_field_changes_only_declared_identities(
        self,
        mutation: dict[str, Any],
    ) -> None:
        """每个 covered mutation 必须更新对应摘要，且非 ingest 变更不得改 eventId。"""
        fixture = deepcopy(_EVENT["rejectBase"])
        baseline = _materialize_mutation_fixture(fixture)
        candidate = deepcopy(fixture)
        inverse = _apply_json_pointer_mutation(candidate, mutation)
        changed = _materialize_mutation_fixture(candidate)
        for label in mutation["changes"]:
            assert self._observable(changed, label) != self._observable(baseline, label)
        for label in mutation["preserves"]:
            assert self._observable(changed, label) == self._observable(baseline, label)
        assert changed[1]["previousEventDigest"] == changed[0]["eventDigest"]
        _apply_json_pointer_mutation(candidate, inverse)
        assert _materialize_mutation_fixture(candidate) == baseline


class TestEventContractBoundaries:
    """PreparedEvent、非 wire 物化切片和权威状态事件的边界契约。"""

    def test_prepared_event_is_full_adapter_input_without_writer_fields(self) -> None:
        """Adapter 必须提供完整身份，且不得偷塞由单写者分配的字段。"""
        prepared = _load_schema("prepared-event.v2.schema.json")
        durable = _load_schema("durable-event.v2.schema.json")
        required = set(prepared["required"])
        expected = {
            "schemaVersion",
            "ingestEventId",
            "taskId",
            "runId",
            "stepId",
            "attemptId",
            "source",
            "eventType",
            "providerEventId",
            "sourceSeq",
            "streamId",
            "sourceTransportSpan",
            "sanitizedStreamSpan",
            "wallTime",
            "monotonicTimeNs",
            "ingestedAt",
            "providerVersion",
            "adapterVersion",
            "processIdentity",
            "payload",
            "sanitizedProviderFrameDigest",
            "redactions",
        }
        writer_assigned = {
            "preparedBatchId",
            "writerEpoch",
            "batchOrdinal",
            "taskSeq",
            "runSeq",
            "durabilityClass",
            "eventId",
            "payloadDigest",
            "previousEventDigest",
            "eventDigest",
            "redactionManifestDigest",
        }

        assert prepared["additionalProperties"] is False
        assert required == expected
        assert not writer_assigned.intersection(prepared["properties"])
        assert prepared["properties"]["schemaVersion"]["const"] == 2
        assert prepared["properties"]["eventType"]["enum"] == [
            event_type
            for event_type in durable["properties"]["eventType"]["enum"]
            if event_type not in {"state.changed", "model.summary"}
        ]

    def test_vectors_use_explicit_nonwire_single_task_materialization_input(self) -> None:
        """纯函数切片必须显式携带已提交 anchor，不能冒充真实 PreparedBatchV2。"""
        case = _EVENT["materialize"][0]
        materialization_input = case["input"]
        assert set(materialization_input) == {
            "taskId",
            "preparedBatchId",
            "expectedTaskSeq",
            "expectedEventDigest",
            "events",
        }
        assert materialization_input["taskId"] == "task-a"
        assert materialization_input["expectedTaskSeq"] is None
        assert materialization_input["expectedEventDigest"] is None
        assert set(case["anchor"]) == {
            "committedTaskSeq",
            "committedEventDigest",
        }
        assert case["anchor"] == {
            "committedTaskSeq": None,
            "committedEventDigest": None,
        }
        prepared = _load_schema("prepared-event.v2.schema.json")
        expected_entry = set(prepared["properties"]) | {
            "batchOrdinal",
            "runSeq",
            "durabilityClass",
        }
        for entry in materialization_input["events"]:
            assert set(entry) == expected_entry
            assert not {
                "previousEventDigest",
                "eventDigest",
                "eventId",
                "taskSeq",
            }.intersection(entry)

    def test_reject_vectors_cover_adapter_and_slice_boundary_mutations(self) -> None:
        """跨语言向量必须覆盖字段越权、伪 manifest、排序和单 Task 边界。"""
        reject_names = {case["name"] for case in _EVENT["rejectMutations"]}
        assert {
            "event-writer-field",
            "event-coarse-type",
            "model-summary-fail-closed",
            "state-changed-adapter-lane",
            "event-missing-run-id",
            "missing-source-span",
            "missing-process-identity",
            "input-root-unknown",
            "anchor-unknown-field",
            "batch-ordinal-bool",
            "missing-coordinator-field",
            "multi-task-slice",
            "anchor-task-seq-drift",
            "anchor-event-digest-drift",
            "stream-event-null-stream-id",
            "batch-ordinal-nonmonotonic",
            "nfc-equivalent-ingest-id",
        }.issubset(reject_names)


def _prepared_event() -> dict[str, Any]:
    """构造一条完整 Adapter PreparedEventV2，供权威 wire schema mutation 使用。"""
    return {
        "schemaVersion": 2,
        "ingestEventId": "ingest-schema-1",
        "taskId": "task-a",
        "runId": "run-a",
        "stepId": "step-a",
        "attemptId": "attempt-a",
        "source": "claude",
        "eventType": "process.started",
        "providerEventId": None,
        "sourceSeq": 0,
        "streamId": None,
        "sourceTransportSpan": {
            "coordinate": "none",
            "start": None,
            "endExclusive": None,
            "mappingPrecision": "none",
        },
        "sanitizedStreamSpan": {
            "segmentId": "seg-schema-1",
            "start": 0,
            "endExclusive": 1,
        },
        "wallTime": "2026-08-11T00:00:00Z",
        "monotonicTimeNs": 1,
        "ingestedAt": "2026-08-11T00:00:01Z",
        "providerVersion": "provider-v1",
        "adapterVersion": "adapter-v1",
        "processIdentity": {
            "executorId": "executor-a",
            "hostId": "host-a",
            "runtime": "local-windows",
            "executableDigest": "sha256:" + "a" * 64,
            "pid": 1,
            "processStartTime": "2026-08-11T00:00:00Z",
            "jobObjectId": "job-a",
            "wslDistro": None,
            "containerId": None,
            "imageDigest": None,
        },
        "payload": {},
        "sanitizedProviderFrameDigest": None,
        "redactions": [],
    }


class TestPreparedAndDurableWireSchemas:
    """Prepared 输入和物化输出必须分别通过其权威 Draft7 schema。"""

    def test_prepared_schema_has_runtime_identity_span_and_stream_teeth(self) -> None:
        """身份、双 span、运行时身份和 streamId 的条件必须由 schema 直接拒绝。"""
        schema = _load_schema("prepared-event.v2.schema.json")
        validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
        assert schema["properties"]["processIdentity"]["properties"]["runtime"]["enum"] == [
            "local-windows",
            "wsl-docker",
        ]
        valid = _prepared_event()
        assert not list(validator.iter_errors(valid))

        missing_identity = deepcopy(valid)
        del missing_identity["runId"]
        assert list(validator.iter_errors(missing_identity))

        missing_span = deepcopy(valid)
        del missing_span["sourceTransportSpan"]
        assert list(validator.iter_errors(missing_span))

        missing_process_key = deepcopy(valid)
        del missing_process_key["processIdentity"]["wslDistro"]
        assert list(validator.iter_errors(missing_process_key))

        bad_stream = deepcopy(valid)
        bad_stream["eventType"] = "stream.segment.committed"
        bad_stream["streamId"] = None
        assert list(validator.iter_errors(bad_stream))

        forbidden_writer_field = deepcopy(valid)
        forbidden_writer_field["previousEventDigest"] = "sha256:" + "0" * 64
        assert list(validator.iter_errors(forbidden_writer_field))

    @pytest.mark.parametrize(
        "case",
        _EVENT["materialize"],
        ids=[c["name"] for c in _EVENT["materialize"]],
    )
    def test_materialized_output_conforms_to_durable_schema(self, case: dict[str, Any]) -> None:
        """物化器返回前后的 DurableEventV2 都必须通过同一权威 schema。"""
        schema = _load_schema("durable-event.v2.schema.json")
        validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
        durable_events = materialize_batch(case["input"], case["anchor"])
        for durable_event in durable_events:
            assert not list(validator.iter_errors(durable_event)), case["name"]

    def test_durable_state_changed_is_bidirectionally_bound_to_authoritative_lane(self) -> None:
        """state.changed、authoritative_state 与 orchestrator 必须三者闭合，禁止单边伪装。"""
        schema = _load_schema("durable-event.v2.schema.json")
        validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
        case = _EVENT["materialize"][0]
        base = materialize_batch(case["input"], case["anchor"])[0]

        non_state_authoritative = deepcopy(base)
        non_state_authoritative["durabilityClass"] = "authoritative_state"
        assert list(validator.iter_errors(non_state_authoritative))

        state_wrong_class = deepcopy(base)
        state_wrong_class.update({"eventType": "state.changed", "source": "orchestrator"})
        assert list(validator.iter_errors(state_wrong_class))

        state_wrong_source = deepcopy(base)
        state_wrong_source.update(
            {"eventType": "state.changed", "durabilityClass": "authoritative_state"}
        )
        assert list(validator.iter_errors(state_wrong_source))

        valid_state = deepcopy(base)
        valid_state.update(
            {
                "eventType": "state.changed",
                "durabilityClass": "authoritative_state",
                "source": "orchestrator",
            }
        )
        assert not list(validator.iter_errors(valid_state))

    def test_rust_codegen_preserves_integer_const_type(self) -> None:
        """Rust 生成类型不得把 schemaVersion 等整数 const 降级为 String。"""
        generator_path = REPO_ROOT / "contracts" / "codegen" / "generate.py"
        spec = importlib.util.spec_from_file_location("event_contract_codegen", generator_path)
        assert spec is not None and spec.loader is not None
        codegen = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(codegen)
        assert codegen.prop_rs_type({"const": 2}) == "i64"

        validator_schemas = codegen.load_required_validator_schemas()
        assert {"preparedEvent", "durableEvent"}.issubset(validator_schemas)
        assert "PREPARED_EVENT_V2_SCHEMA_JSON" in codegen.generate_typescript(
            [], validator_schemas
        )
        assert "DURABLE_EVENT_V2_SCHEMA_JSON" in codegen.generate_rust([], validator_schemas)


def _authoritative_state_event() -> dict[str, Any]:
    """构造一条 Run 聚合的权威 state.changed 基准事件。"""
    payload = {"observedState": "QUEUED"}
    return {
        "schemaVersion": 1,
        "stateEventId": "state-event-1",
        "eventType": "state.changed",
        "durabilityClass": "authoritative_state",
        "taskId": "task-a",
        "scope": "RUN",
        "aggregateType": "RUN",
        "aggregateId": "run-a",
        "runId": "run-a",
        "stepId": None,
        "attemptId": None,
        "previousStateVersion": 0,
        "stateVersion": 1,
        "payload": payload,
        "payloadDigest": payload_digest(payload),
    }


class TestAuthoritativeStateEventContract:
    """权威 state.changed 必须具备真实 scope 组合，不能让 schema 假装承担存储 CAS。"""

    def test_schema_accepts_five_aggregate_shapes_and_rejects_forged_step_attempt(self) -> None:
        """Task/Run/Barrier/Step/Attempt 仅能使用冻结的可空身份组合。"""
        schema = _load_schema("authoritative-state-event.v1.schema.json")
        validator = jsonschema.Draft7Validator(schema)

        assert not list(validator.iter_errors(_authoritative_state_event()))

        task = _authoritative_state_event()
        task.update(
            {
                "scope": "TASK",
                "aggregateType": "TASK",
                "aggregateId": "task-a",
                "runId": None,
                "stepId": None,
                "attemptId": None,
            }
        )
        assert not list(validator.iter_errors(task))

        barrier = _authoritative_state_event()
        barrier.update({"aggregateType": "PHASE_BARRIER", "aggregateId": "barrier-a"})
        assert not list(validator.iter_errors(barrier))

        step = _authoritative_state_event()
        step.update(
            {
                "scope": "STEP",
                "aggregateType": "STEP",
                "aggregateId": "step-a",
                "stepId": "step-a",
            }
        )
        assert not list(validator.iter_errors(step))

        attempt = _authoritative_state_event()
        attempt.update(
            {
                "scope": "ATTEMPT",
                "aggregateType": "ATTEMPT",
                "aggregateId": "attempt-a",
                "stepId": "step-a",
                "attemptId": "attempt-a",
            }
        )
        assert not list(validator.iter_errors(attempt))

        forged_step_attempt = _authoritative_state_event()
        forged_step_attempt.update({"scope": "STEP", "aggregateType": "STEP", "stepId": None})
        assert list(validator.iter_errors(forged_step_attempt))

        wrong_scope = _authoritative_state_event()
        wrong_scope.update({"scope": "TASK", "aggregateType": "TASK", "runId": "invented-run"})
        assert list(validator.iter_errors(wrong_scope))

    def test_schema_binds_versions_but_routes_cas_and_uniqueness_to_task1_storage(self) -> None:
        """JSON Schema 只约束单记录；跨记录 version/CAS 与唯一键必须由同一 UoW 实现。"""
        schema = _load_schema("authoritative-state-event.v1.schema.json")
        validator = jsonschema.Draft7Validator(schema)
        first = _authoritative_state_event()
        duplicate_tuple = deepcopy(first)
        duplicate_tuple["stateEventId"] = "state-event-2"

        assert not list(validator.iter_errors(first))
        assert not list(validator.iter_errors(duplicate_tuple))
        assert schema["x-persistence"]["uniqueKey"] == [
            "aggregateType",
            "aggregateId",
            "stateVersion",
        ]
        assert schema["x-persistence"]["enforcedBy"] == "Task 1 0002_event_store.sql UoW"

        negative_previous = deepcopy(first)
        negative_previous["previousStateVersion"] = -1
        assert list(validator.iter_errors(negative_previous))

        invalid_new_version = deepcopy(first)
        invalid_new_version["stateVersion"] = 0
        assert list(validator.iter_errors(invalid_new_version))

        version_drift = deepcopy(first)
        version_drift["previousStateVersion"] = 1
        version_drift["stateVersion"] = 1
        # Draft 7 无法表达跨字段加一关系；该冲突必须由 Task 1 同一 UoW 的 CAS 拒绝。
        assert not list(validator.iter_errors(version_drift))

        backlog = (REPO_ROOT / "docs" / "operations" / "PHASE_1_BACKLOG.md").read_text(encoding="utf-8")
        assert "(aggregateType, aggregateId, stateVersion)" in backlog
        assert "0002_event_store.sql" in backlog
        assert "stateVersion" in backlog
        assert "PHASE_BARRIER -> aggregateId == phase_barriers.barrier_id" in backlog
        assert "phase_barriers.run_id == runId" in backlog

    def test_payload_digest_truth_and_two_one_sided_mutations_route_to_uow(self) -> None:
        """Draft7 接受单边漂移；构造器/UoW 必须重算 payloadDigest 后才可写入。"""
        schema = _load_schema("authoritative-state-event.v1.schema.json")
        validator = jsonschema.Draft7Validator(schema)
        event = _authoritative_state_event()
        assert event["payloadDigest"] == payload_digest(event["payload"])
        assert not list(validator.iter_errors(event))

        payload_only = deepcopy(event)
        payload_only["payload"]["observedState"] = "RUNNING"
        digest_only = deepcopy(event)
        digest_only["payloadDigest"] = "sha256:" + "f" * 64
        for drifted in (payload_only, digest_only):
            assert not list(validator.iter_errors(drifted))
            assert drifted["payloadDigest"] != payload_digest(drifted["payload"])

        protocol = (REPO_ROOT / "docs" / "protocols" / "contracts-v1.md").read_text(
            encoding="utf-8"
        )
        backlog = (REPO_ROOT / "docs" / "operations" / "PHASE_1_BACKLOG.md").read_text(
            encoding="utf-8"
        )
        for text in (protocol, backlog):
            assert "payloadDigest == sha256(JCS(payload))" in text
            assert "构造器/UoW" in text

    def test_task1_uow_freezes_all_five_aggregate_identity_routes(self) -> None:
        """五类 aggregate 的真实身份来源必须逐路写明，不能让 Draft7 冒充外表 FK。"""
        schema = _load_schema("authoritative-state-event.v1.schema.json")
        protocol = (REPO_ROOT / "docs" / "protocols" / "contracts-v1.md").read_text(
            encoding="utf-8"
        )
        backlog = (REPO_ROOT / "docs" / "operations" / "PHASE_1_BACKLOG.md").read_text(
            encoding="utf-8"
        )
        combined = " ".join(
            (
                str(schema["properties"]["aggregateId"]["description"]),
                " ".join(protocol.split()),
                " ".join(backlog.split()),
            )
        )
        for route in (
            "TASK -> aggregateId == taskId",
            "RUN -> aggregateId == runId",
            "PHASE_BARRIER -> aggregateId == phase_barriers.barrier_id",
            "phase_barriers.run_id == runId",
            "STEP -> aggregateId == stepId",
            "ATTEMPT -> aggregateId == attemptId",
        ):
            assert route in combined
        assert "外表 FK" in combined
        assert "Task 1" in combined
        assert "UoW" in combined

    def test_docs_name_event_prerequisite_and_keep_deferred_boundaries(self) -> None:
        """文档状态必须同时点名事件前置，并精确保留 Task 7/10 的禁入边界。"""
        protocol = (REPO_ROOT / "docs" / "protocols" / "contracts-v1.md").read_text(
            encoding="utf-8"
        )
        backlog = (REPO_ROOT / "docs" / "operations" / "PHASE_1_BACKLOG.md").read_text(
            encoding="utf-8"
        )
        protocol_lines = protocol.splitlines()
        assert "Phase 1 授权与事件合同前置" in protocol_lines[2]
        assert "Phase 1 授权与事件合同前置" in protocol_lines[-1]

        protocol_flat = " ".join(protocol.split())
        backlog_flat = " ".join(backlog.split())
        assert "model.summary.frameRef" in protocol_flat
        assert "fail-closed" in protocol_flat
        assert "PreparedBatch manifest/segments" in protocol_flat
        assert "Task 7" in protocol_flat
        assert "eventContractSetDigest" in protocol_flat
        assert "Phase 1 Task 10" in protocol_flat
        assert "不修改 CompatibilityManifest、receipt 或 Master" in protocol_flat
        assert "model.summary" in backlog_flat
        assert "fail-closed" in backlog_flat
        assert "eventContractSetDigest" in backlog_flat
        assert "不修改 manifest、receipt 或 Master" in backlog_flat

    def test_descriptions_use_frozen_runtime_and_expected_head_names(self) -> None:
        """描述必须与闭合 runtime 枚举及 nullable expected head 字段逐字一致。"""
        for schema_name in (
            "prepared-event.v2.schema.json",
            "durable-event.v2.schema.json",
        ):
            schema_text = json.dumps(_load_schema(schema_name), ensure_ascii=False)
            assert "local-windows" in schema_text
            assert "wsl-docker" in schema_text
            for stale_term in (
                "wsl-process",
                "runtime=wsl-*",
                "runtime=container",
                "WSL/container exec",
                "wsl/container",
            ):
                assert stale_term not in schema_text

        batch_text = json.dumps(
            _load_schema("prepared-batch.v2.schema.json"), ensure_ascii=False
        )
        protocol = (REPO_ROOT / "docs" / "protocols" / "contracts-v1.md").read_text(
            encoding="utf-8"
        )
        for text in (batch_text, " ".join(protocol.split())):
            assert "`expectedTaskSeq`" in text
            assert "`expectedEventDigest`" in text
            assert "`expectedEventDigest` 显式可空" in text

    def test_rust_trusted_validators_are_once_cached_and_redacted(self) -> None:
        """Rust 三个可信 schema 只能初始化一次，失败仍只暴露稳定 EventHashError。"""
        rust_source = (
            REPO_ROOT / "crates" / "factory-contracts" / "src" / "event.rs"
        ).read_text(encoding="utf-8")
        assert "use std::sync::OnceLock;" in rust_source
        for validator_name in (
            "PREPARED_EVENT_VALIDATOR",
            "DURABLE_EVENT_VALIDATOR",
            "PREPARED_BATCH_VALIDATOR",
        ):
            declaration = (
                f"static {validator_name}: OnceLock<Result<Validator, EventHashError>>"
            )
            assert declaration in rust_source
            assert f"{validator_name}.get_or_init(" in rust_source

        validate_wire_body = rust_source.split("fn validate_wire", maxsplit=1)[1].split(
            "\n}\n", maxsplit=1
        )[0]
        assert "compile_validator_schema" not in validate_wire_body
        assert "fn trusted_validators_are_cached()" in rust_source
        assert "fn validator_initialization_error_is_redacted()" in rust_source


# ── prepared-batch canonical 向量 ──────────────────────────────────────────────

_BATCH = _load_golden("prepared-batch.v2.json")
_EXPECTED_BATCH_MUTATION_NAMES = {
    "prepared-batch-event-count-mismatch",
    "prepared-batch-ordered-ingest-length-mismatch",
    "prepared-batch-global-ordinal-gap",
    "prepared-batch-half-null-head",
    "prepared-batch-duplicate-task-head",
    "prepared-batch-reversed-task-range",
    "prepared-batch-event-count-bool",
    "prepared-batch-writer-epoch-unsafe-integer",
    "prepared-batch-nfc-equivalent-ingest-id",
}


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

    def test_manifest_vectors_conform_to_draft7_schema(self) -> None:
        """golden 必须是可校验的真实 manifest，而非旧的简化物化输入。"""
        schema = _load_schema("prepared-batch.v2.schema.json")
        assert "state" not in schema["properties"]
        assert "state" not in schema["required"]
        assert schema["properties"]["orderedIngestIds"]["uniqueItems"] is True
        validator = jsonschema.Draft7Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
        for case in _BATCH["cases"]:
            assert not list(validator.iter_errors(case["batch"])), case["name"]

        multi_task = next(
            case for case in _BATCH["cases"] if case["name"] == "multi-task-interleaved-manifest"
        )
        assert [head["taskId"] for head in multi_task["batch"]["perTaskExpectedHeads"]] == [
            "task-a",
            "task-b",
        ]
        assert multi_task["batch"]["preparedBatchId"] == "batch-multi-0"

        single = next(
            case for case in _BATCH["cases"] if case["name"] == "genesis-single-task-manifest"
        )["batch"]
        assert single["firstBatchOrdinal"] == 0
        assert single["lastBatchOrdinal"] == 1
        assert single["eventCount"] == len(single["orderedIngestIds"]) == 2

    def test_multi_task_event_slices_union_matches_wire_manifest(self) -> None:
        """同 preparedBatchId 的多 Task slices 按全局 ordinal 合并后必须等于 manifest。"""
        manifest = next(
            case["batch"]
            for case in _BATCH["cases"]
            if case["name"] == "multi-task-interleaved-manifest"
        )
        sliced_events = [
            event
            for case in _EVENT["materialize"]
            if case["input"]["preparedBatchId"] == manifest["preparedBatchId"]
            for event in case["input"]["events"]
        ]
        ordered = sorted(sliced_events, key=lambda event: event["batchOrdinal"])
        assert [event["batchOrdinal"] for event in ordered] == list(
            range(manifest["firstBatchOrdinal"], manifest["lastBatchOrdinal"] + 1)
        )
        assert [event["ingestEventId"] for event in ordered] == manifest["orderedIngestIds"]
        normalized_ids = [
            unicodedata.normalize("NFC", event["ingestEventId"]) for event in ordered
        ]
        assert len(normalized_ids) == len(set(normalized_ids))
        assert normalized_ids == [
            unicodedata.normalize("NFC", ingest_id)
            for ingest_id in manifest["orderedIngestIds"]
        ]
        assert {event["taskId"] for event in ordered} == {
            head["taskId"] for head in manifest["perTaskExpectedHeads"]
        }

    def test_semantic_validator_rejects_single_pointer_mutations_and_restores(self) -> None:
        """schema 之后仍须执行计数、连续 ordinal、head pair 与 Task 唯一性语义。"""
        validate = getattr(event_contracts, "validate_prepared_batch_manifest", None)
        assert callable(validate), "缺少 PreparedBatchV2 可执行语义 validator"
        schema = _load_schema("prepared-batch.v2.schema.json")
        schema_validator = jsonschema.Draft7Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
        base = deepcopy(_BATCH["rejectBase"])
        assert not list(schema_validator.iter_errors(base))
        validate(base)

        names = [mutation["name"] for mutation in _BATCH["rejectMutations"]]
        assert len(names) == len(set(names))
        assert set(names) == _EXPECTED_BATCH_MUTATION_NAMES
        for mutation in _BATCH["rejectMutations"]:
            candidate = deepcopy(base)
            inverse = _apply_json_pointer_mutation(candidate, mutation)
            with pytest.raises(EventHashError) as raised:
                validate(candidate)
            assert raised.value.error_code == "event-hash-error"
            _apply_json_pointer_mutation(candidate, inverse)
            assert candidate == base
            validate(candidate)

    def test_segment_source_span_semantics(self) -> None:
        """segment 仅 byte 精度允许非空递增 sourceSpan，其余精度必须为 null。"""
        validate = event_contracts.validate_prepared_batch_manifest
        names = [case["name"] for case in _BATCH["segmentSpanSemantics"]]
        assert len(names) == len(set(names)) == 9
        for case in _BATCH["segmentSpanSemantics"]:
            candidate = deepcopy(_BATCH["rejectBase"])
            segment = candidate["segmentDigests"][0]
            segment["mappingPrecision"] = case["mappingPrecision"]
            segment["sourceSpan"] = deepcopy(case["sourceSpan"])
            if case["accepted"]:
                validate(candidate)
            else:
                with pytest.raises(EventHashError):
                    validate(candidate)
