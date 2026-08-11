"""事件 MaterializationInput 到 DurableEventV2 的纯函数物化器。

本模块只处理单 Task 的非 wire MaterializationInput 切片。它不读取或推进
数据库 head，不 claim PreparedBatch，也不实现 Task 7 的 group commit；调用方须
把已提交锚点显式传入，物化器只在 expected pair 与该锚点完全一致时工作。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import unicodedata
from copy import deepcopy
from functools import lru_cache
from typing import Any

# jsonschema 当前没有完整 PEP 561 stubs；只在第三方导入点收窄 mypy 豁免。
import jsonschema  # type: ignore[import-untyped]

from factory_agent.contracts.generated.models import (
    DURABLE_EVENT_V2_SCHEMA_JSON,
    DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
    PREPARED_BATCH_V2_SCHEMA_JSON,
    PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
    PREPARED_EVENT_V2_SCHEMA_JSON,
    PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
)
from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize
from factory_agent.policy.plan_hash import factory_format_checker

# 任何会影响输出字节的变更都必须同步更新三语言 golden vectors。
EVENT_HASH_VERSION = "event-hash-v2"
_EVENT_ID_DOMAIN = "factory-event-id-v2"
_SHA256_PREFIX = "sha256:"
GENESIS_PREDECESSOR = f"{_SHA256_PREFIX}{'0' * 64}"
_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"eventDigest"})
_SHA256_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_REDACTION_REPLACEMENT = re.compile(r"^\[REDACTED(?::[a-z0-9._-]+)?\]$")
_DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#"
_INVALID_EVENT_INPUT = "INVALID_EVENT_HASH_INPUT"
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

# PreparedEventV2 是 adapter wire；下列字段均由 Adapter 给出。
_PREPARED_EVENT_FIELDS: tuple[str, ...] = (
    "schemaVersion",
    "taskId",
    "runId",
    "stepId",
    "attemptId",
    "source",
    "ingestEventId",
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
)

# 这三个字段只可由 coordinator 放入非 wire 的单 Task 输入切片。
_COORDINATOR_EVENT_FIELDS: tuple[str, ...] = (
    "batchOrdinal",
    "runSeq",
    "durabilityClass",
)
_MATERIALIZATION_INPUT_FIELDS: tuple[str, ...] = (
    "preparedBatchId",
    "taskId",
    "expectedTaskSeq",
    "expectedEventDigest",
    "events",
)
_COMMITTED_ANCHOR_FIELDS: tuple[str, ...] = (
    "committedTaskSeq",
    "committedEventDigest",
)
_MATERIALIZATION_EVENT_FIELDS = frozenset(
    (*_PREPARED_EVENT_FIELDS, *_COORDINATOR_EVENT_FIELDS)
)
_DURABILITY_CLASSES = frozenset(
    {"side_effect_receipt", "provider_source", "derived"}
)


class EventHashError(FactoryError):
    """MaterializationInput 或权威嵌入 schema 校验失败时抛出。"""

    error_code = "event-hash-error"


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    """要求对象输入，避免 array/null 被当作对象而产生跨语言行为漂移。"""
    if not isinstance(value, dict):
        raise EventHashError(f"{label} 必须为对象")
    return value


def _require_exact_keys(
    value: dict[str, Any],
    required: tuple[str, ...] | frozenset[str],
    label: str,
) -> None:
    """执行 exact-set 边界：缺字段和未知字段都 fail-closed。"""
    actual = frozenset(value)
    expected = frozenset(required)
    if actual != expected:
        raise EventHashError(f"{label} 字段集合不符合 MaterializationInput 合同")


def _sha256_prefixed(data: bytes) -> str:
    """计算 sha256:<lowercase hex>。"""
    return f"{_SHA256_PREFIX}{hashlib.sha256(data).hexdigest()}"


def _is_sha256_digest(value: object) -> bool:
    """判断冻结的 SHA-256 wire 形态，不接受空串、大写或其他摘要算法。"""
    return isinstance(value, str) and _SHA256_DIGEST.fullmatch(value) is not None


def _require_safe_nonnegative_integer(value: object, label: str) -> int:
    """按 Draft7 数学整数语义校验安全范围，并把 -0/1.0 规范化为 int。"""
    if isinstance(value, bool):
        raise EventHashError(_INVALID_EVENT_INPUT)
    if isinstance(value, int):
        integer = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        integer = int(value)
    else:
        raise EventHashError(_INVALID_EVENT_INPUT)
    if integer < 0 or integer > _MAX_SAFE_INTEGER:
        raise EventHashError(_INVALID_EVENT_INPUT)
    return integer


def _checked_safe_add(value: int, increment: int) -> int:
    """执行 taskSeq 等 writer 序号加法，超过 I-JSON 上界即 fail closed。"""
    result = value + increment
    if result > _MAX_SAFE_INTEGER:
        raise EventHashError(_INVALID_EVENT_INPUT)
    return result


def _canonicalize_event_hash(value: object) -> bytes:
    """在事件边界归一 canonical 异常，禁止泄漏第三方异常正文或 payload。"""
    try:
        return canonicalize(value)
    except EventHashError:
        raise
    except Exception as exc:  # noqa: BLE001 - 对外只暴露稳定 event-hash-error。
        raise EventHashError(_INVALID_EVENT_INPUT) from exc


def _optional_task_seq(value: object, label: str) -> int | None:
    """校验显式的 genesis/null 或已提交非负 taskSeq。"""
    if value is None:
        return None
    return _require_safe_nonnegative_integer(value, label)


def _optional_digest(value: object, label: str) -> str | None:
    """校验显式的 genesis/null 或冻结的 SHA-256 digest。"""
    if value is None:
        return None
    if not isinstance(value, str) or not _is_sha256_digest(value):
        raise EventHashError(f"{label} 必须为 sha256 digest 或 null")
    return value


def _require_head_pair(task_seq: int | None, digest: str | None, label: str) -> None:
    """head 的 genesis 必须是 (null, null)，避免半空锚点歧义。"""
    if (task_seq is None) != (digest is None):
        raise EventHashError(f"{label} 的 taskSeq 与 eventDigest 必须同时为 null 或同时存在")


def _verify_embedded_schema_integrity(schema_json: str, expected_sha256: str) -> None:
    """在解析前校验 codegen 内嵌 schema 字节，防止运行时常量漂移。"""
    actual_sha256 = _sha256_prefixed(schema_json.encode("utf-8"))
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise EventHashError(_INVALID_EVENT_INPUT)


def _build_event_validator(schema_json: str, expected_sha256: str) -> jsonschema.Draft7Validator:
    """惰性构建可信 Draft7 validator；底层错误不携带不可信 payload。"""
    try:
        _verify_embedded_schema_integrity(schema_json, expected_sha256)
        schema = json.loads(schema_json)
        if not isinstance(schema, dict) or schema.get("$schema") != _DRAFT7_SCHEMA_URI:
            raise ValueError("invalid generated draft7 schema")
        jsonschema.Draft7Validator.check_schema(schema)
        return jsonschema.Draft7Validator(schema, format_checker=factory_format_checker())
    except EventHashError:
        raise
    except Exception as exc:  # noqa: BLE001 - 边界统一为稳定错误码。
        raise EventHashError(_INVALID_EVENT_INPUT) from exc


@lru_cache(maxsize=1)
def _prepared_event_validator() -> jsonschema.Draft7Validator:
    """返回 codegen 内嵌的 PreparedEventV2 权威校验器。"""
    return _build_event_validator(
        PREPARED_EVENT_V2_SCHEMA_JSON,
        PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
    )


@lru_cache(maxsize=1)
def _durable_event_validator() -> jsonschema.Draft7Validator:
    """返回 codegen 内嵌的 DurableEventV2 权威校验器。"""
    return _build_event_validator(
        DURABLE_EVENT_V2_SCHEMA_JSON,
        DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
    )


@lru_cache(maxsize=1)
def _prepared_batch_validator() -> jsonschema.Draft7Validator:
    """返回 codegen 内嵌的 PreparedBatchV2 权威校验器。"""
    return _build_event_validator(
        PREPARED_BATCH_V2_SCHEMA_JSON,
        PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
    )


def _validate_with_authoritative_schema(
    value: dict[str, Any],
    validator: jsonschema.Draft7Validator,
) -> None:
    """不泄漏事件正文地执行权威 schema 验证。"""
    if next(validator.iter_errors(value), None) is not None:
        raise EventHashError(_INVALID_EVENT_INPUT)


def event_id(ingest_event_id: str) -> str:
    """从 Adapter 稳定 ingestEventId 推导幂等 eventId。"""
    if not isinstance(ingest_event_id, str) or not ingest_event_id:
        raise EventHashError("ingestEventId 必须为非空字符串")
    digest = hashlib.sha256(
        _canonicalize_event_hash([_EVENT_ID_DOMAIN, ingest_event_id])
    ).hexdigest()
    return f"evt_{digest}"


def payload_digest(payload: object) -> str:
    """计算已脱敏 payload 的 JCS SHA-256 摘要。"""
    return _sha256_prefixed(_canonicalize_event_hash(payload))


def redaction_manifest_digest(redactions: object) -> str:
    """计算已脱敏 redactions 列表的 JCS SHA-256 摘要。"""
    return _sha256_prefixed(_canonicalize_event_hash(redactions))


def _validate_materialization_input(
    raw_input: object,
    raw_anchor: object,
) -> tuple[dict[str, Any], dict[str, Any], int | None, str | None]:
    """校验非 wire 输入和锚点，并返回已配对的已提交 head。"""
    materialization_input = _require_mapping(raw_input, "input")
    anchor = _require_mapping(raw_anchor, "anchor")
    _require_exact_keys(
        materialization_input, _MATERIALIZATION_INPUT_FIELDS, "input"
    )
    _require_exact_keys(anchor, _COMMITTED_ANCHOR_FIELDS, "anchor")

    expected_task_seq = _optional_task_seq(
        materialization_input["expectedTaskSeq"], "input.expectedTaskSeq"
    )
    expected_event_digest = _optional_digest(
        materialization_input["expectedEventDigest"], "input.expectedEventDigest"
    )
    committed_task_seq = _optional_task_seq(
        anchor["committedTaskSeq"], "anchor.committedTaskSeq"
    )
    committed_event_digest = _optional_digest(
        anchor["committedEventDigest"], "anchor.committedEventDigest"
    )
    _require_head_pair(expected_task_seq, expected_event_digest, "input")
    _require_head_pair(committed_task_seq, committed_event_digest, "anchor")
    if (
        expected_task_seq != committed_task_seq
        or expected_event_digest != committed_event_digest
    ):
        raise EventHashError("expected head 与已提交 anchor 不一致")
    return materialization_input, anchor, committed_task_seq, committed_event_digest


def _validate_prepared_event_semantics(prepared_event: dict[str, Any]) -> None:
    """校验 Draft7 无法表达的范围顺序，并统一所有事件数字的安全整数边界。"""
    prepared_event["schemaVersion"] = _require_safe_nonnegative_integer(
        prepared_event["schemaVersion"], "schemaVersion"
    )
    prepared_event["sourceSeq"] = _require_safe_nonnegative_integer(
        prepared_event["sourceSeq"], "sourceSeq"
    )
    prepared_event["monotonicTimeNs"] = _require_safe_nonnegative_integer(
        prepared_event["monotonicTimeNs"], "monotonicTimeNs"
    )
    process_identity = _require_mapping(prepared_event["processIdentity"], "processIdentity")
    process_identity["pid"] = _require_safe_nonnegative_integer(
        process_identity["pid"], "processIdentity.pid"
    )

    source_span = _require_mapping(prepared_event["sourceTransportSpan"], "sourceTransportSpan")
    coordinate = source_span["coordinate"]
    mapping_precision = source_span["mappingPrecision"]
    # 坐标轴描述 Provider 原始传输单位，mappingPrecision 描述映射精度；二者不能按同名字面绑定。
    if coordinate == "none":
        if (
            mapping_precision != "none"
            or source_span["start"] is not None
            or source_span["endExclusive"] is not None
        ):
            raise EventHashError(_INVALID_EVENT_INPUT)
    elif coordinate in {"provider_transport_bytes", "provider_transport_chars"}:
        if mapping_precision == "byte":
            source_start = _require_safe_nonnegative_integer(
                source_span["start"], "sourceSpan.start"
            )
            source_end = _require_safe_nonnegative_integer(
                source_span["endExclusive"], "sourceSpan.endExclusive"
            )
            if source_start >= source_end:
                raise EventHashError(_INVALID_EVENT_INPUT)
            source_span["start"] = source_start
            source_span["endExclusive"] = source_end
        elif mapping_precision in {"field", "frame", "none"}:
            if source_span["start"] is not None or source_span["endExclusive"] is not None:
                raise EventHashError(_INVALID_EVENT_INPUT)
        else:
            raise EventHashError(_INVALID_EVENT_INPUT)
    else:
        raise EventHashError(_INVALID_EVENT_INPUT)

    sanitized_span = _require_mapping(
        prepared_event["sanitizedStreamSpan"], "sanitizedStreamSpan"
    )
    sanitized_start = _require_safe_nonnegative_integer(
        sanitized_span["start"], "sanitizedSpan.start"
    )
    sanitized_end = _require_safe_nonnegative_integer(
        sanitized_span["endExclusive"], "sanitizedSpan.endExclusive"
    )
    if sanitized_start >= sanitized_end:
        raise EventHashError(_INVALID_EVENT_INPUT)
    sanitized_span["start"] = sanitized_start
    sanitized_span["endExclusive"] = sanitized_end

    redactions = prepared_event["redactions"]
    if not isinstance(redactions, list):
        raise EventHashError(_INVALID_EVENT_INPUT)
    for redaction in redactions:
        redaction_object = _require_mapping(redaction, "redaction")
        byte_range = _require_mapping(redaction_object["byteRange"], "redaction.byteRange")
        redaction_start = _require_safe_nonnegative_integer(
            byte_range["start"], "redaction.start"
        )
        redaction_end = _require_safe_nonnegative_integer(
            byte_range["endExclusive"], "redaction.endExclusive"
        )
        replacement = redaction_object["replacement"]
        if (
            redaction_start >= redaction_end
            or not isinstance(replacement, str)
            or _REDACTION_REPLACEMENT.fullmatch(replacement) is None
        ):
            raise EventHashError(_INVALID_EVENT_INPUT)
        byte_range["start"] = redaction_start
        byte_range["endExclusive"] = redaction_end


def validate_prepared_batch_manifest(raw_manifest: object) -> None:
    """验证 PreparedBatchV2 wire manifest 的 schema 与跨字段不变量。

    本函数只验证不可变 manifest，不执行 claim、CAS、group commit 或持久化；Task 7
    才负责用真实 segments 构造 MaterializationInput 并提交。
    """
    manifest = _require_mapping(raw_manifest, "preparedBatch")
    _validate_with_authoritative_schema(manifest, _prepared_batch_validator())
    event_count = _require_safe_nonnegative_integer(manifest["eventCount"], "eventCount")
    first_ordinal = _require_safe_nonnegative_integer(
        manifest["firstBatchOrdinal"], "firstBatchOrdinal"
    )
    last_ordinal = _require_safe_nonnegative_integer(
        manifest["lastBatchOrdinal"], "lastBatchOrdinal"
    )
    ordered_ingest_ids = manifest["orderedIngestIds"]
    if (
        not isinstance(ordered_ingest_ids, list)
        or last_ordinal < first_ordinal
        or event_count != len(ordered_ingest_ids)
        or event_count != last_ordinal - first_ordinal + 1
    ):
        raise EventHashError(_INVALID_EVENT_INPUT)
    normalized_ingest_ids: set[str] = set()
    for ingest_id in ordered_ingest_ids:
        if not isinstance(ingest_id, str) or not ingest_id:
            raise EventHashError(_INVALID_EVENT_INPUT)
        normalized_ingest_id = unicodedata.normalize("NFC", ingest_id)
        if normalized_ingest_id in normalized_ingest_ids:
            raise EventHashError(_INVALID_EVENT_INPUT)
        normalized_ingest_ids.add(normalized_ingest_id)

    task_ids: set[str] = set()
    heads = manifest["perTaskExpectedHeads"]
    if not isinstance(heads, list):
        raise EventHashError(_INVALID_EVENT_INPUT)
    for raw_head in heads:
        head = _require_mapping(raw_head, "perTaskExpectedHead")
        task_id = head["taskId"]
        head_first = _require_safe_nonnegative_integer(
            head["firstBatchOrdinal"], "head.firstBatchOrdinal"
        )
        head_last = _require_safe_nonnegative_integer(
            head["lastBatchOrdinal"], "head.lastBatchOrdinal"
        )
        expected_task_seq = _optional_task_seq(head["expectedTaskSeq"], "head.expectedTaskSeq")
        expected_digest = _optional_digest(
            head["expectedEventDigest"], "head.expectedEventDigest"
        )
        _require_head_pair(expected_task_seq, expected_digest, "head")
        if (
            not isinstance(task_id, str)
            or task_id in task_ids
            or head_first > head_last
            or head_first < first_ordinal
            or head_last > last_ordinal
        ):
            raise EventHashError(_INVALID_EVENT_INPUT)
        task_ids.add(task_id)

    segments = manifest["segmentDigests"]
    if not isinstance(segments, list):
        raise EventHashError(_INVALID_EVENT_INPUT)
    for raw_segment in segments:
        segment = _require_mapping(raw_segment, "segmentDigest")
        ordinal_range = _require_mapping(segment["ordinalRange"], "segment.ordinalRange")
        segment_first = _require_safe_nonnegative_integer(
            ordinal_range["first"], "segment.ordinalRange.first"
        )
        segment_last = _require_safe_nonnegative_integer(
            ordinal_range["lastExclusive"], "segment.ordinalRange.lastExclusive"
        )
        sanitized_span = _require_mapping(segment["sanitizedSpan"], "segment.sanitizedSpan")
        sanitized_start = _require_safe_nonnegative_integer(
            sanitized_span["start"], "segment.sanitizedSpan.start"
        )
        sanitized_end = _require_safe_nonnegative_integer(
            sanitized_span["endExclusive"], "segment.sanitizedSpan.endExclusive"
        )
        mapping_precision = segment["mappingPrecision"]
        source_span = segment["sourceSpan"]
        if mapping_precision == "byte":
            source_span_object = _require_mapping(source_span, "segment.sourceSpan")
            source_start = _require_safe_nonnegative_integer(
                source_span_object["start"], "segment.sourceSpan.start"
            )
            source_end = _require_safe_nonnegative_integer(
                source_span_object["endExclusive"], "segment.sourceSpan.endExclusive"
            )
            if source_start >= source_end:
                raise EventHashError(_INVALID_EVENT_INPUT)
        elif mapping_precision in {"field", "frame", "none"}:
            if source_span is not None:
                raise EventHashError(_INVALID_EVENT_INPUT)
        else:
            raise EventHashError(_INVALID_EVENT_INPUT)
        if segment_first >= segment_last or sanitized_start >= sanitized_end:
            raise EventHashError(_INVALID_EVENT_INPUT)


def materialize_batch(
    input: dict[str, Any],
    anchor: dict[str, Any],
) -> list[dict[str, Any]]:
    """物化单 Task MaterializationInput 为已校验 DurableEventV2 列表。

    MaterializationInput 不是 PreparedBatchV2 wire manifest：它只含一个 Task 的
    已 claim slice 与 coordinator 派生字段。Task 7 以后才会从真实 manifest/segments
    构造此输入并负责 CAS、claim、持久化和多 Task 原子性。
    """
    (
        materialization_input,
        _anchor,
        committed_task_seq,
        committed_event_digest,
    ) = _validate_materialization_input(input, anchor)

    task_id = materialization_input["taskId"]
    prepared_batch_id = materialization_input["preparedBatchId"]
    events = materialization_input["events"]
    if not isinstance(task_id, str) or not task_id:
        raise EventHashError("input.taskId 必须为非空字符串")
    if not isinstance(prepared_batch_id, str) or not prepared_batch_id:
        raise EventHashError("input.preparedBatchId 必须为非空字符串")
    if not isinstance(events, list) or not events:
        raise EventHashError("input.events 必须为非空数组")

    base_task_seq = (
        0 if committed_task_seq is None else _checked_safe_add(committed_task_seq, 1)
    )
    previous_event_digest = (
        GENESIS_PREDECESSOR
        if committed_event_digest is None
        else committed_event_digest
    )
    durable_events: list[dict[str, Any]] = []
    seen_ingest_ids: set[str] = set()
    previous_batch_ordinal: int | None = None

    for index, raw_event in enumerate(events):
        event = _require_mapping(raw_event, f"input.events[{index}]")
        _require_exact_keys(
            event,
            _MATERIALIZATION_EVENT_FIELDS,
            f"input.events[{index}]",
        )
        if event["taskId"] != task_id:
            raise EventHashError("切片内 event.taskId 与 input.taskId 不一致")

        # 先投影回 wire PreparedEventV2，再交给三端同源嵌入 schema 验证。
        prepared_event = deepcopy({field: event[field] for field in _PREPARED_EVENT_FIELDS})
        _validate_with_authoritative_schema(prepared_event, _prepared_event_validator())
        _validate_prepared_event_semantics(prepared_event)

        ingest_event_id = event["ingestEventId"]
        if not isinstance(ingest_event_id, str) or not ingest_event_id:
            raise EventHashError(_INVALID_EVENT_INPUT)
        # NFC 等价 ingest ID 也会导出相同 eventId，必须在进入 hash 前拒绝。
        normalized_ingest_id = unicodedata.normalize("NFC", ingest_event_id)
        if normalized_ingest_id in seen_ingest_ids:
            raise EventHashError("切片内 ingestEventId 重复")
        seen_ingest_ids.add(normalized_ingest_id)

        batch_ordinal = event["batchOrdinal"]
        run_seq = event["runSeq"]
        durability_class = event["durabilityClass"]
        batch_ordinal = _require_safe_nonnegative_integer(batch_ordinal, "event.batchOrdinal")
        if previous_batch_ordinal is not None and batch_ordinal <= previous_batch_ordinal:
            raise EventHashError("单 Task slice 的 batchOrdinal 必须严格递增")
        previous_batch_ordinal = batch_ordinal
        run_seq = _require_safe_nonnegative_integer(run_seq, "event.runSeq")
        if not isinstance(durability_class, str) or durability_class not in _DURABILITY_CLASSES:
            raise EventHashError("event.durabilityClass 不在 DurableEventV2 枚举内")

        # writer 仅分配与已提交锚点有关的字段；Adapter 字段一律逐字复制。
        durable: dict[str, Any] = {
            "schemaVersion": prepared_event["schemaVersion"],
            "durabilityClass": durability_class,
            "eventId": event_id(ingest_event_id),
            "taskId": task_id,
            "taskSeq": _checked_safe_add(base_task_seq, index),
            "runId": prepared_event["runId"],
            "runSeq": run_seq,
            "stepId": prepared_event["stepId"],
            "attemptId": prepared_event["attemptId"],
            "source": prepared_event["source"],
            "ingestEventId": ingest_event_id,
            "eventType": prepared_event["eventType"],
            "providerEventId": prepared_event["providerEventId"],
            "sourceSeq": prepared_event["sourceSeq"],
            "streamId": prepared_event["streamId"],
            "preparedBatchId": prepared_batch_id,
            "batchOrdinal": batch_ordinal,
            "sourceTransportSpan": prepared_event["sourceTransportSpan"],
            "sanitizedStreamSpan": prepared_event["sanitizedStreamSpan"],
            "wallTime": prepared_event["wallTime"],
            "monotonicTimeNs": prepared_event["monotonicTimeNs"],
            "ingestedAt": prepared_event["ingestedAt"],
            "providerVersion": prepared_event["providerVersion"],
            "adapterVersion": prepared_event["adapterVersion"],
            "processIdentity": prepared_event["processIdentity"],
            "payload": prepared_event["payload"],
            "sanitizedProviderFrameDigest": prepared_event[
                "sanitizedProviderFrameDigest"
            ],
            "payloadDigest": payload_digest(prepared_event["payload"]),
            "previousEventDigest": previous_event_digest,
            "redactions": prepared_event["redactions"],
            "redactionManifestDigest": redaction_manifest_digest(
                prepared_event["redactions"]
            ),
        }
        durable["eventDigest"] = _sha256_prefixed(
            _canonicalize_event_hash(
                {
                    key: value
                    for key, value in durable.items()
                    if key not in _DIGEST_EXCLUDED_FIELDS
                }
            )
        )

        # digest 已落入输出后再执行 DurableEventV2 schema，阻断任何构造漂移。
        _validate_with_authoritative_schema(durable, _durable_event_validator())
        durable_events.append(durable)
        previous_event_digest = durable["eventDigest"]

    return durable_events
