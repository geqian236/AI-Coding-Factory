"""factory_agent.domain.events — PreparedBatchV2 → DurableEventV2 纯函数物化器。

Phase 0 只冻结「纯输入 → 纯输出」的事件身份与摘要链算法；并发 CAS、
SQLite 事务、writer epoch 竞争留到 Phase 1。物化器与 TypeScript
(packages/factory-contracts/src/event.ts) 及 Rust
(factory_contracts::event) 字节级一致，共用同一 canonical_json 底座
（NFC + RFC 8785 JCS）。

算法（严格来自 Master Spec §10.1，映射到冻结的 v2 schema 字段名）：

  eventId
      "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))
      仅由 ingestEventId 决定，使崩溃重试得到同一身份（幂等）。

  payloadDigest
      "sha256:" + lowercaseHex(SHA-256(JCS(payload)))

  eventDigest
      "sha256:" + lowercaseHex(SHA-256(JCS(DurableEventV2 去除 eventDigest 与 head
      两个字段后的完整对象)))；因此计算输入仍包含 previousHead、payload、
      payloadDigest 和全部 identity，构成防篡改链。

  head
      等于本事件的 eventDigest，作为链式指针；批内下一事件的 previousHead
      指向前一事件的 head，genesis 事件使用固定 predecessor（"sha256:" + 64 个 0）。

物化规则（fail-closed）：
  - batch.previousHead 必须与 anchor.committedHead 相等（同为 None 或同字符串），
    否则视为前驱漂移 / 竞争批次抢占同一旧 head，拒绝。
  - batch 内所有事件的 taskId 必须等于 batch.taskId。
  - batch 至少含一个事件；ingestEventId 批内不得重复。
  - taskSeq 由 anchor 单调延续：genesis 从 0 开始，否则从 committedTaskSeq+1 开始。
  - batchOrdinal 为批次级序号，原样复制到每个 DurableEventV2。

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

# eventDigest 计算时必须排除的字段（自身摘要值与链指针，避免自指循环）。
_DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"eventDigest", "head"})

# PreparedEventV2 的必需字段（缺任一即 fail closed）。
_REQUIRED_PREPARED_EVENT_FIELDS: tuple[str, ...] = (
    "ingestEventId",
    "eventType",
    "sourceSeq",
    "payload",
)

# PreparedEventV2 允许出现的字段（对齐冻结 schema 的 additionalProperties:false）。
# 出现集合外字段即 fail closed，兑现 §10.1「schema 未声明的扩展字段不得混入 v2 hash」。
# taskId 允许出现以便批内逐事件校验一致性。
_ALLOWED_PREPARED_EVENT_FIELDS: frozenset[str] = frozenset(
    {
        "ingestEventId",
        "taskId",
        "eventType",
        "sourceSeq",
        "transportSpanDigest",
        "payload",
        "payloadDigest",
        "preparedAt",
    }
)


class EventHashError(FactoryError):
    """事件物化输入非法（缺字段、类型错误、前驱漂移、重复摄取 ID 等）时抛出。"""

    error_code = "event-hash-error"


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
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


def payload_digest(payload: Any) -> str:
    """计算 payloadDigest（脱敏后 payload 的 JCS 摘要）。

    Args:
        payload: 已脱敏的事件负载对象。

    Returns:
        形如 "sha256:<64 位小写十六进制>" 的摘要。

    Raises:
        CanonicalJsonError: payload 含非法数字/类型/重复键。
    """
    return _sha256_prefixed(canonicalize(payload))


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

    Args:
        batch:      PreparedBatchV2 对象（同一 Task，含 events / previousHead /
                    batchOrdinal / taskId）。
        anchor:     该 Task 当前已提交锚点，含 committedTaskSeq（None 表示 genesis）
                    与 committedHead（None 表示 genesis）。
        durable_at: 本批次耐久化完成时间（RFC3339 字符串），写入每个 DurableEventV2。

    Returns:
        与 batch.events 同序的 DurableEventV2 字典列表，taskSeq 单调、
        eventDigest/head 链式连接。

    Raises:
        EventHashError:     结构非法、前驱漂移、taskId 不一致、批次为空或
                            ingestEventId 重复。
        CanonicalJsonError: 任一字段值无法规范化。
    """
    batch = _require_mapping(batch, "batch")
    anchor = _require_mapping(anchor, "anchor")

    task_id = batch.get("taskId")
    if not isinstance(task_id, str) or not task_id:
        raise EventHashError("batch.taskId 必须为非空字符串")

    events = batch.get("events")
    if not isinstance(events, list) or len(events) == 0:
        raise EventHashError("batch.events 必须为非空数组")

    batch_ordinal = batch.get("batchOrdinal")
    if isinstance(batch_ordinal, bool) or not isinstance(batch_ordinal, int):
        raise EventHashError("batch.batchOrdinal 必须为整数")

    # 前驱校验：batch.previousHead 必须与 anchor.committedHead 完全一致。
    # 二者同为 None（genesis）或同一字符串；不一致即前驱漂移 / 竞争抢占旧 head。
    batch_prev = batch.get("previousHead")
    committed_head = anchor.get("committedHead")
    if batch_prev != committed_head:
        raise EventHashError(
            "前驱漂移：batch.previousHead 与 anchor.committedHead 不一致，拒绝物化"
        )

    base_seq = _base_task_seq(anchor)

    # genesis（无已提交 head）首事件前驱为全零 predecessor，否则接已提交 head。
    prev_head = GENESIS_PREDECESSOR if committed_head is None else committed_head

    durable_events: list[dict[str, Any]] = []
    seen_ingest: set[str] = set()

    for index, raw_event in enumerate(events):
        event = _require_mapping(raw_event, f"events[{index}]")
        for field in _REQUIRED_PREPARED_EVENT_FIELDS:
            if field not in event:
                raise EventHashError(
                    f"events[{index}] 缺少必需字段 '{field}'（{EVENT_HASH_VERSION}）"
                )
        # 未知字段 fail closed：兑现 §10.1「schema 未声明的扩展字段不得混入 v2 hash」。
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

        # 批内事件 taskId 必须与 batch.taskId 一致（若显式给出）。
        event_task = event.get("taskId", task_id)
        if event_task != task_id:
            raise EventHashError(
                f"events[{index}].taskId 与 batch.taskId 不一致，拒绝物化"
            )

        task_seq = base_seq + index

        # 组装 DurableEventV2（不含 eventDigest 与 head，二者随后计算）。
        durable: dict[str, Any] = {
            "eventId": event_id(ingest_id),
            "taskId": task_id,
            "taskSeq": task_seq,
            "batchOrdinal": batch_ordinal,
            "previousHead": prev_head,
            "ingestEventId": ingest_id,
            "eventType": event["eventType"],
            "sourceSeq": event["sourceSeq"],
            "payload": event["payload"],
            "payloadDigest": payload_digest(event["payload"]),
            "durableAt": durable_at,
        }

        # eventDigest = JCS(去除 eventDigest 与 head 后的完整对象) 的 SHA-256。
        digest_input = {
            k: v for k, v in durable.items() if k not in _DIGEST_EXCLUDED_FIELDS
        }
        event_digest = _sha256_prefixed(canonicalize(digest_input))
        durable["eventDigest"] = event_digest
        durable["head"] = event_digest  # head 即本事件 eventDigest，作为链指针。

        durable_events.append(durable)
        prev_head = event_digest  # 下一事件前驱指向本事件 head。

    return durable_events
