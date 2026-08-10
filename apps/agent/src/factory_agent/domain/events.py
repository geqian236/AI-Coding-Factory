"""factory_agent.domain.events — PreparedBatchV2 → DurableEventV2 纯函数物化器。

Phase 0 冻结「纯输入 → 纯输出」的事件身份、摘要链与 §10.1 完整字段集算法。
GPT 第二轮审核指出早期实现只放 §10.1 子集字段；本修订补齐：
执行身份（runId/runSeq/stepId/attemptId/source/processIdentity）、
双 span（sourceTransportSpan/sanitizedStreamSpan）、
脱敏证据（redactions/redactionManifestDigest/sanitizedProviderFrameDigest/
mappingPrecision）、schemaVersion、durabilityClass、providerEventId、
streamId、preparedBatchId、wallTime、monotonicTimeNs、ingestedAt、
providerVersion、adapterVersion、eventType 改为 §10.1 具体枚举。

物化器与 TypeScript (packages/factory-contracts/src/event.ts) 及 Rust
(factory_contracts::event) 字节级一致，共用同一 canonical_json 底座
（NFC + RFC 8785 JCS）。

算法（严格来自 Master Spec §10.1）：

  eventId
      "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))
      仅由 ingestEventId 决定，使崩溃重试得到同一身份（幂等）。

  payloadDigest
      "sha256:" + lowercaseHex(SHA-256(JCS(payload)))

  redactionManifestDigest
      "sha256:" + lowercaseHex(SHA-256(JCS(redactions 列表)))

  eventDigest
      "sha256:" + lowercaseHex(SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后的
      完整对象)))；previousEventDigest 仍参与计算。previousEventDigest 单链模型：
      本事件无 head 字段，下一事件的 previousEventDigest 指向前一事件的 eventDigest；
      genesis 事件 previousEventDigest = "sha256:" + 64 个 0。

物化规则（fail-closed）：
  - batch.previousHead 必须与 anchor.committedHead 相等（同为 None 或同字符串）。
  - 批内所有事件的 taskId 必须等于 batch.taskId。
  - 批至少含一个事件；ingestEventId 批内不得重复。
  - 每个事件必须含 §10.1 完整字段集（required by durable-event.v2 schema）。
  - processIdentity 必填子字段不能缺失。
  - 双 span 必填子字段不能缺失。
  - taskSeq 由 anchor 单调延续：genesis 从 0 开始，否则从 committedTaskSeq+1 开始。
  - batchOrdinal 为批次级序号，原样复制到每个 DurableEventV2。
  - redactions 列表空表示本事件未脱敏；非空则每条必含 patternId/byteRange/replacement。

失败错误码：event-hash-error（不记录完整事件正文，只暴露稳定 error_code 与脱敏明细）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from factory_agent.errors import FactoryError
from factory_agent.policy.canonical_json import canonicalize

# 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。
EVENT_HASH_VERSION = "event-hash-v2"

# eventId 域分离标签（数组首元素）。
_EVENT_ID_DOMAIN = "factory-event-id-v2"

# 摘要输出前缀。
_SHA256_PREFIX = "sha256:"

# genesis 事件的固定前驱：sha256: + 64 个 0（Master Spec §10.1 全零 predecessor）。
GENESIS_PREDECESSOR = f"{_SHA256_PREFIX}{'0' * 64}"

# eventDigest 计算时必须排除的字段（仅排除自身摘要，避免自指循环）。
# previousEventDigest 仍参与计算（Master Spec §10.1）。
_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"eventDigest"})

# PreparedEventV2 必需字段（每个 batch.events[i] 必须含）。
# 全字段集按 §10.1：执行身份、双 span、脱敏、时间、版本。
_REQUIRED_PREPARED_EVENT_FIELDS: tuple[str, ...] = (
    # 身份
    "ingestEventId", "taskId", "runId", "stepId", "attemptId", "source",
    # 类型与 Provider 引用
    "eventType", "providerEventId", "sourceSeq", "streamId",
    # Prepared 关联（preparedBatchId 在 batch 顶层；events 每项含 batchOrdinal）
    "batchOrdinal",
    # 双 span
    "sourceTransportSpan", "sanitizedStreamSpan",
    # 时间 / 版本
    "wallTime", "monotonicTimeNs", "ingestedAt", "providerVersion", "adapterVersion",
    # 受管进程身份
    "processIdentity",
    # Payload / 脱敏
    "payload", "sanitizedProviderFrameDigest", "redactions",
)

# PreparedEventV2 允许出现的字段集（对齐 schema 的 additionalProperties:false）。
# 出现集合外字段即 fail closed。
_ALLOWED_PREPARED_EVENT_FIELDS: frozenset[str] = frozenset(_REQUIRED_PREPARED_EVENT_FIELDS) | {
    "payloadDigest",
    "redactionManifestDigest",
    "previousEventDigest",
    "eventDigest",
    "runSeq",
    "durabilityClass",
}


class EventHashError(FactoryError):
    """事件物化输入非法（缺字段、类型错误、前驱漂移、重复摄取 ID 等）时抛出。"""

    error_code = "event-hash-error"


def _require_mapping(value: object, label: str) -> dict[str, object]:  # noqa: ANN401
    """校验 value 为字典，否则 fail closed。

    Args:
        value: 待校验对象。
        label: 出错时的字段标签（不含敏感内容）。

    Returns:
        校验通过的字典。

    Raises:
        EventHashError: value 非字典。
    """
    if not isinstance(value, dict):
        raise EventHashError(f"字段 '{label}' 必须为对象")
    return value


def _sha256_prefixed(data: bytes) -> str:
    """计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。"""
    return f"{_SHA256_PREFIX}{hashlib.sha256(data).hexdigest()}"


def event_id(ingest_event_id: str) -> str:
    """计算幂等 eventId（仅由 ingestEventId 决定）。

    Args:
        ingest_event_id: Adapter 分配的稳定摄取 ID。

    Returns:
        形如 "evt_<64 位小写十六进制>" 的事件身份。

    Raises:
        EventHashError:     ingestEventId 非非空字符串。
        CanonicalJsonError: 值无法规范化。
    """
    if not isinstance(ingest_event_id, str) or not ingest_event_id:
        raise EventHashError("ingestEventId 必须为非空字符串")
    domain_array = [_EVENT_ID_DOMAIN, ingest_event_id]
    digest = hashlib.sha256(canonicalize(domain_array)).hexdigest()
    return f"evt_{digest}"


def payload_digest(payload: object) -> str:  # noqa: ANN401
    """计算 payloadDigest（脱敏后 payload 的 JCS 摘要）。

    Args:
        payload: 已脱敏的事件负载对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        CanonicalJsonError: payload 含非法数字/类型/重复键。
    """
    return _sha256_prefixed(canonicalize(payload))


def redaction_manifest_digest(redactions: list[dict[str, object]]) -> str:
    """计算 redactionManifestDigest（redactions 列表的 JCS 摘要）。

    Args:
        redactions: 本事件命中脱敏点列表（schema 见 durable-event.v2）。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        CanonicalJsonError: redactions 含非法数字/类型/重复键。
    """
    return _sha256_prefixed(canonicalize(redactions))


def _base_task_seq(anchor: dict[str, Any]) -> int:
    """由 anchor 推导本批次首事件的 taskSeq。

    genesis（committedTaskSeq 为 None）从 0 开始，否则从 committedTaskSeq+1 开始。

    Raises:
        EventHashError: committedTaskSeq 存在但非整数。
    """
    committed = anchor.get("committedTaskSeq")
    if committed is None:
        return 0
    # bool 是 int 子类，必须显式排除，避免 True 被当作 1。
    if isinstance(committed, bool) or not isinstance(committed, int):
        raise EventHashError("anchor.committedTaskSeq 必须为整数或 null")
    if committed < 0:
        raise EventHashError("anchor.committedTaskSeq 不得为负")
    return committed + 1


def materialize_batch(
    batch: dict[str, Any],
    anchor: dict[str, Any],
    durable_at: str,
) -> list[dict[str, Any]]:
    """把一个 PreparedBatchV2 物化为按序连接的 DurableEventV2 列表（纯函数）。

    每个 PreparedEventV2 必须含 §10.1 全字段集；物化器直通到 DurableEventV2
    （schema 见 durable-event.v2）。本函数不臆造任何字段——缺任一必填即
    EventHashError（fail-closed）。

    Args:
        batch:      PreparedBatchV2 简化输入：{taskId, preparedBatchId, events[],
                    batchOrdinal, previousHead}。events[] 每项含 §10.1 全字段集
                    （不含 schemaVersion/durabilityClass/eventId/taskSeq/payloadDigest/
                    previousEventDigest/eventDigest/redactionManifestDigest —— 这些由
                    物化器派生）。
        anchor:     该 Task 当前已提交锚点 {committedTaskSeq, committedHead}。
        durable_at: 本批次耐久化完成时间（RFC3339）。仍写入 wallTime 之外的
                    ingestedAt 取 durable_at（见实现）。

    Returns:
        与 batch.events 同序的 DurableEventV2 字典列表，含 §10.1 完整字段集
        且 taskSeq / previousEventDigest 链式连接。

    Raises:
        EventHashError:     缺字段 / 未知字段 / 前驱漂移 / taskId 不一致 /
                            ingestEventId 重复 / processIdentity 不合规。
        CanonicalJsonError: 任一字段值无法规范化。
    """
    batch = _require_mapping(batch, "batch")
    anchor = _require_mapping(anchor, "anchor")

    task_id = batch.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        raise EventHashError("batch.taskId 必须为非空字符串")

    prepared_batch_id = batch.get("preparedBatchId")
    if not isinstance(prepared_batch_id, str) or not prepared_batch_id:
        raise EventHashError("batch.preparedBatchId 必须为非空字符串")

    events = batch.get("events")
    if not isinstance(events, list) or len(events) == 0:
        raise EventHashError("batch.events 必须为非空数组")

    batch_ordinal = batch.get("batchOrdinal")
    if isinstance(batch_ordinal, bool) or not isinstance(batch_ordinal, int):
        raise EventHashError("batch.batchOrdinal 必须为整数")

    # 前驱校验：batch.previousHead 必须与 anchor.committedHead 完全一致。
    batch_prev = batch.get("previousHead")
    committed_head = anchor.get("committedHead")
    if batch_prev != committed_head:
        raise EventHashError(
            "前驱漂移：batch.previousHead 与 anchor.committedHead 不一致，拒绝物化"
        )

    base_seq = _base_task_seq(anchor)
    prev_event_digest = GENESIS_PREDECESSOR if committed_head is None else committed_head

    durable_events: list[dict[str, Any]] = []
    seen_ingest: set[str] = set()

    for index, raw_event in enumerate(events):
        event = _require_mapping(raw_event, f"events[{index}]")
        # 必需字段校验
        for field in _REQUIRED_PREPARED_EVENT_FIELDS:
            if field not in event:
                raise EventHashError(
                    f"events[{index}] 缺少必需字段 '{field}'（{EVENT_HASH_VERSION}）"
                )
        # 未知字段 fail closed（§10.1 schema 未声明字段不得混入 v2 hash）
        unknown = set(event.keys()) - _ALLOWED_PREPARED_EVENT_FIELDS
        if unknown:
            raise EventHashError(
                f"events[{index}] 含未声明字段 {sorted(unknown)}，拒绝混入 v2 hash"
            )

        ingest_id = event["ingestEventId"]
        if not isinstance(ingest_id, str) or not ingest_id:
            raise EventHashError(f"events[{index}].ingestEventId 必须为非空字符串")
        if ingest_id in seen_ingest:
            raise EventHashError(f"批内 ingestEventId 重复：'{ingest_id}'")
        seen_ingest.add(ingest_id)

        event_task = event["taskId"]
        if event_task != task_id:
            raise EventHashError(
                f"events[{index}].taskId 与 batch.taskId 不一致，拒绝物化"
            )

        # processIdentity 必填子字段校验（schema required 重复保险）
        pi = event["processIdentity"]
        if not isinstance(pi, dict):
            raise EventHashError(f"events[{index}].processIdentity 必须为对象")
        for sub in (
            "executorId", "hostId", "runtime", "executableDigest",
            "pid", "processStartTime", "jobObjectId",
        ):
            if sub not in pi:
                raise EventHashError(
                    f"events[{index}].processIdentity 缺子字段 '{sub}'"
                )

        # 双 span 必填子字段校验
        sts = event["sourceTransportSpan"]
        sss = event["sanitizedStreamSpan"]
        if not isinstance(sts, dict) or not isinstance(sss, dict):
            raise EventHashError(
                f"events[{index}].sourceTransportSpan/sanitizedStreamSpan 必须为对象"
            )
        for sub in ("coordinate", "start", "endExclusive", "mappingPrecision"):
            if sub not in sts:
                raise EventHashError(
                    f"events[{index}].sourceTransportSpan 缺子字段 '{sub}'"
                )
        for sub in ("segmentId", "start", "endExclusive"):
            if sub not in sss:
                raise EventHashError(
                    f"events[{index}].sanitizedStreamSpan 缺子字段 '{sub}'"
                )

        task_seq = base_seq + index

        # 派生字段
        redactions = event["redactions"]
        if not isinstance(redactions, list):
            raise EventHashError(f"events[{index}].redactions 必须为数组")

        durable: dict[str, Any] = {
            # 版本 / 等级
            "schemaVersion": 2,
            "durabilityClass": event.get("durabilityClass", "derived"),
            # 身份（执行身份链）
            "eventId": event_id(ingest_id),
            "taskId": task_id,
            "taskSeq": task_seq,
            "runId": event["runId"],
            "runSeq": event.get("runSeq", 0),
            "stepId": event["stepId"],
            "attemptId": event["attemptId"],
            "source": event["source"],
            # Provider 引用
            "ingestEventId": ingest_id,
            "eventType": event["eventType"],
            "providerEventId": event["providerEventId"],
            "sourceSeq": event["sourceSeq"],
            "streamId": event["streamId"],
            # Prepared 关联
            "preparedBatchId": prepared_batch_id,
            "batchOrdinal": event["batchOrdinal"],
            # 双 span
            "sourceTransportSpan": sts,
            "sanitizedStreamSpan": sss,
            # 时间 / 版本
            "wallTime": event["wallTime"],
            "monotonicTimeNs": event["monotonicTimeNs"],
            "ingestedAt": event.get("ingestedAt", durable_at),
            "providerVersion": event["providerVersion"],
            "adapterVersion": event["adapterVersion"],
            # 受管进程身份
            "processIdentity": pi,
            # Payload / 脱敏
            "payload": event["payload"],
            "sanitizedProviderFrameDigest": event["sanitizedProviderFrameDigest"],
            "payloadDigest": payload_digest(event["payload"]),
            # 链
            "previousEventDigest": prev_event_digest,
            "redactions": redactions,
            "redactionManifestDigest": redaction_manifest_digest(redactions),
        }

        # eventDigest = JCS(去除 eventDigest 字段后完整对象) 的 SHA-256。
        digest_input = {
            k: v for k, v in durable.items() if k not in _DIGEST_EXCLUDED_FIELDS
        }
        event_digest = _sha256_prefixed(canonicalize(digest_input))
        durable["eventDigest"] = event_digest

        durable_events.append(durable)
        prev_event_digest = event_digest  # 下一事件 previousEventDigest 指向前一事件 eventDigest。

    return durable_events