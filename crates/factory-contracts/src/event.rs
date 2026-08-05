//! crates/factory-contracts/src/event.rs
//!
//! PreparedBatchV2 → DurableEventV2 纯函数物化器 —— Rust 实现。
//! 与 Python (factory_agent.domain.events) 及 TypeScript
//! (packages/factory-contracts/src/event.ts) 字节级一致，共用同一
//! canonical.rs 底座（NFC + RFC 8785 JCS）。
//!
//! GPT 第二轮审核指出早期实现只放了 §10.1 子集字段；本修订补齐：
//! 执行身份（runId/runSeq/stepId/attemptId/source/processIdentity）、
//! 双 span（sourceTransportSpan/sanitizedStreamSpan）、
//! 脱敏证据（redactions/redactionManifestDigest/sanitizedProviderFrameDigest/
//! mappingPrecision）、schemaVersion、durabilityClass、providerEventId、
//! streamId、preparedBatchId、wallTime、monotonicTimeNs、ingestedAt、
//! providerVersion、adapterVersion、eventType 改为 §10.1 具体枚举。
//!
//! 算法（严格来自 Master Spec §10.1）：
//!
//!   eventId
//!       "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))
//!       仅由 ingestEventId 决定，使崩溃重试得到同一身份（幂等）。
//!
//!   payloadDigest
//!       "sha256:" + lowercaseHex(SHA-256(JCS(payload)))
//!
//!   redactionManifestDigest
//!       "sha256:" + lowercaseHex(SHA-256(JCS(redactions 列表)))
//!
//!   eventDigest
//!       "sha256:" + lowercaseHex(SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后的
//!       完整对象)))；previousEventDigest 仍参与计算。previousEventDigest 单链模型：
//!       本事件无 head 字段，下一事件的 previousEventDigest 指向前一事件的 eventDigest；
//!       genesis 事件 previousEventDigest = "sha256:" + 64 个 0。
//!
//! 物化规则（fail-closed）：
//!   - batch.previousHead 必须与 anchor.committedHead 相等（同为 null 或同字符串）。
//!   - 批内所有事件的 taskId 必须等于 batch.taskId。
//!   - 批至少含一个事件；ingestEventId 批内不得重复。
//!   - 每个事件必须含 §10.1 完整字段集（required by durable-event.v2 schema）。
//!   - processIdentity 必填子字段不能缺失。
//!   - 双 span 必填子字段不能缺失。
//!   - taskSeq 由 anchor 单调延续：genesis 从 0 开始，否则从 committedTaskSeq+1 开始。
//!   - batchOrdinal 为事件在批内序号，原样从 event 拷贝。
//!   - redactions 列表空表示本事件未脱敏。
//!
//! 失败错误码：event-hash-error。

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

/// PreparedEventV2 必需字段（每个 batch.events[i] 必须含）。
/// 全字段集按 §10.1：执行身份、双 span、脱敏、时间、版本。
const REQUIRED_PREPARED_EVENT_FIELDS: [&str; 22] = [
    // 身份
    "ingestEventId", "taskId", "runId", "stepId", "attemptId", "source",
    // 类型与 Provider 引用
    "eventType", "providerEventId", "sourceSeq", "streamId",
    // Prepared 关联（preparedBatchId 在 batch 顶层；events 每项含 batchOrdinal）
    "batchOrdinal",
    // 双 span
    "sourceTransportSpan", "sanitizedStreamSpan",
    // 时间 / 版本
    "wallTime", "monotonicTimeNs", "ingestedAt", "providerVersion", "adapterVersion",
    // 受管进程身份
    "processIdentity",
    // Payload / 脱敏
    "payload", "sanitizedProviderFrameDigest", "redactions",
];

/// PreparedEventV2 允许出现的字段集（对齐 schema 的 additionalProperties:false）。
/// 出现集合外字段即 fail closed。
const ALLOWED_EXTRA_PREPARED_EVENT_FIELDS: [&str; 6] = [
    "payloadDigest",
    "redactionManifestDigest",
    "previousEventDigest",
    "eventDigest",
    "runSeq",
    "durabilityClass",
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

/// 计算 redactionManifestDigest（redactions 列表的 JCS 摘要）。
pub fn redaction_manifest_digest(redactions: &Value) -> Result<String, EventHashError> {
    let bytes = canonicalize_value(redactions)?;
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
/// 每个 PreparedEventV2 必须含 §10.1 全字段集；物化器直通到 DurableEventV2。
///
/// # 参数
/// - `batch`：PreparedBatchV2 简化输入：{taskId, preparedBatchId, events[],
///   batchOrdinal, previousHead}。events[] 每项含 §10.1 全字段集
///   （不含 schemaVersion/durabilityClass/eventId/taskSeq/payloadDigest/
///   previousEventDigest/eventDigest/redactionManifestDigest —— 这些由物化器派生）。
/// - `anchor`：该 Task 当前已提交锚点 {committedTaskSeq, committedHead}。
/// - `durable_at`：本批次耐久化完成时间（RFC3339）。
///
/// # 错误
/// - `EventHashError`：缺字段 / 未知字段 / 前驱漂移 / taskId 不一致 /
///   ingestEventId 重复 / processIdentity 不合规。
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

    let prepared_batch_id = match batch_obj.get("preparedBatchId") {
        Some(Value::String(s)) if !s.is_empty() => s.clone(),
        _ => return Err(EventHashError::new("batch.preparedBatchId 必须为非空字符串")),
    };

    let events = match batch_obj.get("events") {
        Some(Value::Array(a)) if !a.is_empty() => a,
        _ => return Err(EventHashError::new("batch.events 必须为非空数组")),
    };

    // 前驱校验：batch.previousHead 必须与 anchor.committedHead 完全一致。
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
    let mut prev_event_digest: String = if committed_head.is_null() {
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
            let k = key.as_str();
            if !REQUIRED_PREPARED_EVENT_FIELDS.contains(&k)
                && !ALLOWED_EXTRA_PREPARED_EVENT_FIELDS.contains(&k)
            {
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

        let event_task = match event.get("taskId") {
            Some(Value::String(s)) => s.clone(),
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].taskId 缺失或非字符串"
                )))
            }
        };
        if event_task != task_id {
            return Err(EventHashError::new(format!(
                "events[{index}].taskId 与 batch.taskId 不一致，拒绝物化"
            )));
        }

        // processIdentity 必填子字段校验（schema required 重复保险）
        let pi = match event.get("processIdentity") {
            Some(Value::Object(m)) => m,
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].processIdentity 必须为对象"
                )))
            }
        };
        for sub in [
            "executorId", "hostId", "runtime", "executableDigest",
            "pid", "processStartTime", "jobObjectId",
        ] {
            if !pi.contains_key(sub) {
                return Err(EventHashError::new(format!(
                    "events[{index}].processIdentity 缺子字段 '{sub}'"
                )));
            }
        }

        // 双 span 必填子字段校验
        let sts = match event.get("sourceTransportSpan") {
            Some(Value::Object(m)) => m,
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].sourceTransportSpan 必须为对象"
                )))
            }
        };
        for sub in ["coordinate", "start", "endExclusive", "mappingPrecision"] {
            if !sts.contains_key(sub) {
                return Err(EventHashError::new(format!(
                    "events[{index}].sourceTransportSpan 缺子字段 '{sub}'"
                )));
            }
        }
        let sss = match event.get("sanitizedStreamSpan") {
            Some(Value::Object(m)) => m,
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].sanitizedStreamSpan 必须为对象"
                )))
            }
        };
        for sub in ["segmentId", "start", "endExclusive"] {
            if !sss.contains_key(sub) {
                return Err(EventHashError::new(format!(
                    "events[{index}].sanitizedStreamSpan 缺子字段 '{sub}'"
                )));
            }
        }

        let task_seq = base_seq + index as i64;

        // 派生字段
        let redactions = match event.get("redactions") {
            Some(Value::Array(a)) => a,
            _ => {
                return Err(EventHashError::new(format!(
                    "events[{index}].redactions 必须为数组"
                )))
            }
        };

        let durability_class = event
            .get("durabilityClass")
            .cloned()
            .unwrap_or(Value::String("derived".to_string()));

        let run_seq = event
            .get("runSeq")
            .cloned()
            .unwrap_or(Value::from(0));

        let ingested_at = event
            .get("ingestedAt")
            .cloned()
            .unwrap_or(Value::String(durable_at.to_string()));

        // 组装 DurableEventV2（不含 eventDigest，随后计算）。
        let mut durable = Map::new();
        durable.insert("schemaVersion".to_string(), Value::from(2));
        durable.insert("durabilityClass".to_string(), durability_class);
        durable.insert("eventId".to_string(), Value::String(event_id(&ingest_id)?));
        durable.insert("taskId".to_string(), Value::String(task_id.clone()));
        durable.insert("taskSeq".to_string(), Value::from(task_seq));
        durable.insert("runId".to_string(), event["runId"].clone());
        durable.insert("runSeq".to_string(), run_seq);
        durable.insert("stepId".to_string(), event["stepId"].clone());
        durable.insert("attemptId".to_string(), event["attemptId"].clone());
        durable.insert("source".to_string(), event["source"].clone());
        durable.insert("ingestEventId".to_string(), Value::String(ingest_id.clone()));
        durable.insert("eventType".to_string(), event["eventType"].clone());
        durable.insert("providerEventId".to_string(), event["providerEventId"].clone());
        durable.insert("sourceSeq".to_string(), event["sourceSeq"].clone());
        durable.insert("streamId".to_string(), event["streamId"].clone());
        durable.insert("preparedBatchId".to_string(), Value::String(prepared_batch_id.clone()));
        // batchOrdinal 是事件在批内序号（per-event；schema 一致语义）
        durable.insert("batchOrdinal".to_string(), event["batchOrdinal"].clone());
        durable.insert("sourceTransportSpan".to_string(), Value::Object(sts.clone()));
        durable.insert("sanitizedStreamSpan".to_string(), Value::Object(sss.clone()));
        durable.insert("wallTime".to_string(), event["wallTime"].clone());
        durable.insert("monotonicTimeNs".to_string(), event["monotonicTimeNs"].clone());
        durable.insert("ingestedAt".to_string(), ingested_at);
        durable.insert("providerVersion".to_string(), event["providerVersion"].clone());
        durable.insert("adapterVersion".to_string(), event["adapterVersion"].clone());
        durable.insert("processIdentity".to_string(), Value::Object(pi.clone()));
        durable.insert("payload".to_string(), event["payload"].clone());
        durable.insert(
            "sanitizedProviderFrameDigest".to_string(),
            event["sanitizedProviderFrameDigest"].clone(),
        );
        durable.insert(
            "payloadDigest".to_string(),
            Value::String(payload_digest(&event["payload"])?),
        );
        durable.insert(
            "previousEventDigest".to_string(),
            Value::String(prev_event_digest.clone()),
        );
        durable.insert(
            "redactions".to_string(),
            Value::Array(redactions.clone()),
        );
        durable.insert(
            "redactionManifestDigest".to_string(),
            Value::String(redaction_manifest_digest(&Value::Array(redactions.clone()))?),
        );

        // eventDigest = JCS(去除 eventDigest 字段后完整对象) 的 SHA-256。
        // 此时 durable 尚未包含 eventDigest，故直接对其规范化即符合定义。
        let bytes = canonicalize_value(&Value::Object(durable.clone()))?;
        let event_digest = sha256_prefixed(&bytes);
        durable.insert("eventDigest".to_string(), Value::String(event_digest.clone()));

        durable_events.push(Value::Object(durable));
        prev_event_digest = event_digest; // 下一事件 previousEventDigest 指向前一事件 eventDigest。
    }

    Ok(durable_events)
}