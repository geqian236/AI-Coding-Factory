//! crates/factory-contracts/src/event.rs
//!
//! PreparedBatchV2 → DurableEventV2 纯函数物化器 —— Rust 实现。
//! 与 Python (factory_agent.domain.events) 及 TypeScript
//! (packages/factory-contracts/src/event.ts) 字节级一致，共用同一
//! canonical.rs 底座（NFC + RFC 8785 JCS）。
//!
//! 算法（严格来自 Master Spec §10.1，映射到冻结的 v2 schema 字段名）：
//!
//!   eventId
//!       "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))
//!       仅由 ingestEventId 决定，使崩溃重试得到同一身份（幂等）。
//!
//!   payloadDigest
//!       "sha256:" + lowercaseHex(SHA-256(JCS(payload)))
//!
//!   eventDigest
//!       "sha256:" + lowercaseHex(SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后
//!       的完整对象)))；计算输入仍包含 previousEventDigest、payload、payloadDigest
//!       和全部 identity，构成防篡改链。
//!
//!   previousEventDigest
//!       上一条事件的 eventDigest；genesis 事件使用固定 predecessor（"sha256:" + 64
//!       个 0）。本事件无 head 字段，链式指针完全通过 previousEventDigest 单链表达
//!       （Master Spec §10.1）。
//!
//! 物化规则（fail-closed）：
//!   - batch.previousHead 必须与 anchor.committedHead 相等（同为 null 或同字符串），
//!     否则视为前驱漂移 / 竞争批次抢占同一旧 head，拒绝。
//!   - batch 内所有事件的 taskId 必须等于 batch.taskId。
//!   - batch 至少含一个事件；ingestEventId 批内不得重复。
//!   - taskSeq 由 anchor 单调延续：genesis 从 0 开始，否则从 committedTaskSeq+1 开始。
//!   - batchOrdinal 为批次级序号，原样复制到每个 DurableEventV2。
//!
//! 失败错误码：event-hash-error（不记录完整事件正文）。

use crate::canonical::{canonicalize, canonicalize_value, CanonValue, CanonicalJsonError};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fmt;

/// 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。
pub const EVENT_HASH_VERSION: &str = "event-hash-v2";

/// eventId 域分离标签（数组首元素）。
const EVENT_ID_DOMAIN: &str = "factory-event-id-v2";

/// 摘要输出前缀。
const SHA256_PREFIX: &str = "sha256:";

/// genesis 事件的固定前驱：sha256: + 64 个 0（Master Spec §10.1 全零 predecessor）。
pub const GENESIS_PREDECESSOR: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";

/// PreparedEventV2 的必需字段（缺任一即 fail closed）。
const REQUIRED_PREPARED_EVENT_FIELDS: [&str; 4] =
    ["ingestEventId", "eventType", "sourceSeq", "payload"];

/// PreparedEventV2 允许出现的字段（对齐冻结 schema 的 additionalProperties:false）。
/// 出现集合外字段即 fail closed，兑现 §10.1「schema 未声明的扩展字段不得混入 v2 hash」。
/// taskId 允许出现以便批内逐事件校验一致性。
const ALLOWED_PREPARED_EVENT_FIELDS: [&str; 8] = [
    "ingestEventId",
    "taskId",
    "eventType",
    "sourceSeq",
    "transportSpanDigest",
    "payload",
    "payloadDigest",
    "preparedAt",
];

/// 事件物化输入非法（缺字段、类型错误、前驱漂移、重复摄取 ID 等）时抛出。
#[derive(Debug)]
pub struct EventHashError {
    /// 已脱敏的错误明细。
    pub detail: String,
}

impl EventHashError {
    /// 稳定错误码。
    pub const ERROR_CODE: &'static str = "event-hash-error";

    /// 构造错误。
    fn new(detail: impl Into<String>) -> Self {
        Self { detail: detail.into() }
    }
}

impl fmt::Display for EventHashError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", Self::ERROR_CODE, self.detail)
    }
}

impl std::error::Error for EventHashError {}

impl From<CanonicalJsonError> for EventHashError {
    /// 规范化错误上抛为事件哈希错误，保留明细。
    fn from(e: CanonicalJsonError) -> Self {
        EventHashError::new(format!("canonicalize 失败：{}", e.detail))
    }
}

/// 计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。
fn sha256_prefixed(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{SHA256_PREFIX}{:x}", hasher.finalize())
}

/// 计算幂等 eventId（仅由 ingestEventId 决定）。
pub fn event_id(ingest_event_id: &str) -> Result<String, EventHashError> {
    if ingest_event_id.is_empty() {
        return Err(EventHashError::new("ingestEventId 必须为非空字符串"));
    }
    // 域分离数组：["factory-event-id-v2", ingestEventId]。
    let domain_array = CanonValue::Array(vec![
        CanonValue::Str(EVENT_ID_DOMAIN.to_string()),
        CanonValue::Str(ingest_event_id.to_string()),
    ]);
    let bytes = canonicalize(&domain_array)?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok(format!("evt_{:x}", hasher.finalize()))
}

/// 计算 payloadDigest（脱敏后 payload 的 JCS 摘要）。
pub fn payload_digest(payload: &Value) -> Result<String, EventHashError> {
    let bytes = canonicalize_value(payload)?;
    Ok(sha256_prefixed(&bytes))
}

/// 由 anchor 推导本批次首事件的 taskSeq。
/// genesis（committedTaskSeq 为 null/缺省）从 0 开始，否则从 committedTaskSeq+1 开始。
fn base_task_seq(anchor: &Value) -> Result<i64, EventHashError> {
    match anchor.get("committedTaskSeq") {
        None | Some(Value::Null) => Ok(0),
        Some(Value::Number(n)) => {
            let v = n
                .as_i64()
                .ok_or_else(|| EventHashError::new("anchor.committedTaskSeq 必须为整数"))?;
            if v < 0 {
                return Err(EventHashError::new("anchor.committedTaskSeq 不得为负"));
            }
            Ok(v + 1)
        }
        Some(_) => Err(EventHashError::new("anchor.committedTaskSeq 必须为整数或 null")),
    }
}

/// 校验 value 为对象，否则 fail closed。
fn require_object<'a>(value: &'a Value, label: &str) -> Result<&'a Map<String, Value>, EventHashError> {
    match value {
        Value::Object(m) => Ok(m),
        _ => Err(EventHashError::new(format!("字段 '{label}' 必须为对象"))),
    }
}

/// 把一个 PreparedBatchV2 物化为按序连接的 DurableEventV2 列表（纯函数）。
///
/// # 参数
/// - `batch`：PreparedBatchV2 对象（同一 Task，含 events / previousHead / batchOrdinal / taskId）。
/// - `anchor`：该 Task 当前已提交锚点，含 committedTaskSeq（null 表示 genesis）与
///   committedHead（null 表示 genesis）。
/// - `durable_at`：本批次耐久化完成时间（RFC3339 字符串），写入每个 DurableEventV2。
///
/// # 错误
/// - `EventHashError`：结构非法、前驱漂移、taskId 不一致、批次为空或 ingestEventId 重复。
/// - 规范化失败（经 `From<CanonicalJsonError>` 上抛）。
pub fn materialize_batch(
    batch: &Value,
    anchor: &Value,
    durable_at: &str,
) -> Result<Vec<Value>, EventHashError> {
    let batch_obj = require_object(batch, "batch")?;

    let task_id = match batch_obj.get("taskId") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(EventHashError::new("batch.taskId 必须为非空字符串")),
    };

    let events = match batch_obj.get("events") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err(EventHashError::new("batch.events 必须为非空数组")),
    };

    let batch_ordinal: i64 = match batch_obj.get("batchOrdinal") {
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| EventHashError::new("batch.batchOrdinal 必须为整数"))?,
        _ => return Err(EventHashError::new("batch.batchOrdinal 必须为整数")),
    };

    // 前驱校验：batch.previousHead 必须与 anchor.committedHead 完全一致。
    // 二者同为 null（genesis）或同一字符串；不一致即前驱漂移 / 竞争抢占旧 head。
    let null = Value::Null;
    let batch_prev = batch_obj.get("previousHead").unwrap_or(&null);
    let committed_head = anchor.get("committedHead").unwrap_or(&null);
    if batch_prev != committed_head {
        return Err(EventHashError::new(
            "前驱漂移：batch.previousHead 与 anchor.committedHead 不一致，拒绝物化",
        ));
    }

    let base_seq = base_task_seq(anchor)?;

    // genesis（无已提交 head）首事件前驱为全零 predecessor，否则接已提交 head。
    let mut prev_head: String = if committed_head.is_null() {
        GENESIS_PREDECESSOR.to_string()
    } else {
        committed_head
            .as_str()
            .ok_or_else(|| EventHashError::new("anchor.committedHead 必须为字符串或 null"))?
            .to_string()
    };

    let mut durable_events: Vec<Value> = Vec::with_capacity(events.len());
    let mut seen_ingest: HashSet<String> = HashSet::new();

    for (index, raw_event) in events.iter().enumerate() {
        let event = require_object(raw_event, &format!("events[{index}]"))?;

        // 未知字段拒绝（对齐 schema additionalProperties:false）。
        for key in event.keys() {
            if !ALLOWED_PREPARED_EVENT_FIELDS.contains(&key.as_str()) {
                return Err(EventHashError::new(format!(
                    "events[{index}] 含未声明字段 '{key}'（{EVENT_HASH_VERSION}）"
                )));
            }
        }
        // 必需字段校验。
        for field in REQUIRED_PREPARED_EVENT_FIELDS {
            if !event.contains_key(field) {
                return Err(EventHashError::new(format!(
                    "events[{index}] 缺少必需字段 '{field}'（{EVENT_HASH_VERSION}）"
                )));
            }
        }

        let ingest_id = match event.get("ingestEventId") {
            Some(Value::String(s)) if !s.is_empty() => s.clone(),
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].ingestEventId 必须为非空字符串"
                )))
            }
        };
        if !seen_ingest.insert(ingest_id.clone()) {
            return Err(EventHashError::new(format!(
                "批内 ingestEventId 重复：'{ingest_id}'"
            )));
        }

        // 批内事件 taskId 必须与 batch.taskId 一致（若显式给出）。
        if let Some(event_task) = event.get("taskId") {
            if event_task.as_str() != Some(task_id.as_str()) {
                return Err(EventHashError::new(format!(
                    "events[{index}].taskId 与 batch.taskId 不一致，拒绝物化"
                )));
            }
        }

        let task_seq = base_seq + index as i64;

        // 组装 DurableEventV2（不含 eventDigest，随后计算）。
        // previousEventDigest 单链：下条事件的 previousEventDigest 指向本条 eventDigest。
        // 字段插入顺序仅为可读性；canonicalizer 会按 UTF-16 序重排。
        let mut durable = Map::new();
        durable.insert("eventId".to_string(), Value::String(event_id(&ingest_id)?));
        durable.insert("taskId".to_string(), Value::String(task_id.clone()));
        durable.insert("taskSeq".to_string(), Value::from(task_seq));
        durable.insert("batchOrdinal".to_string(), Value::from(batch_ordinal));
        durable.insert("previousEventDigest".to_string(), Value::String(prev_head.clone()));
        durable.insert("ingestEventId".to_string(), Value::String(ingest_id.clone()));
        durable.insert("eventType".to_string(), event["eventType"].clone());
        durable.insert("sourceSeq".to_string(), event["sourceSeq"].clone());
        durable.insert("payload".to_string(), event["payload"].clone());
        durable.insert(
            "payloadDigest".to_string(),
            Value::String(payload_digest(&event["payload"])?),
        );
        durable.insert("durableAt".to_string(), Value::String(durable_at.to_string()));

        // eventDigest = JCS(去除 eventDigest 字段后的完整对象) 的 SHA-256。
        // previousEventDigest 仍参与计算（Master Spec §10.1）。此时 durable 尚未
        // 包含 eventDigest，故直接对其规范化即符合定义。
        let bytes = canonicalize_value(&Value::Object(durable.clone()))?;
        let event_digest = sha256_prefixed(&bytes);
        durable.insert("eventDigest".to_string(), Value::String(event_digest.clone()));

        durable_events.push(Value::Object(durable));
        prev_head = event_digest; // 下一事件 previousEventDigest 指向前一事件 eventDigest。
    }

    Ok(durable_events)
}
