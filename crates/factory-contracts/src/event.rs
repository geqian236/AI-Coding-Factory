//! 单 Task MaterializationInput 到 DurableEventV2 的纯函数物化器。
//!
//! MaterializationInput 不是 PreparedBatchV2 wire manifest：本模块只校验一个
//! coordinator 已 claim 的 Task slice 与显式已提交锚点。Task 7 以后才会从真实
//! manifest/segments 构造多个 slice，并负责 CAS、claim 和存储提交。

use crate::canonical::{canonicalize, canonicalize_value, CanonValue, CanonicalJsonError};
use crate::generated::{
    DURABLE_EVENT_V2_SCHEMA_JSON, DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
    PREPARED_BATCH_V2_SCHEMA_JSON, PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
    PREPARED_EVENT_V2_SCHEMA_JSON, PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
};
use jsonschema::{Draft, Retrieve, Uri, Validator};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::error::Error;
use std::fmt;
use std::sync::OnceLock;
use unicode_normalization::UnicodeNormalization;

/// 任何会影响输出字节的改动都必须同步更新三语言 golden vectors。
pub const EVENT_HASH_VERSION: &str = "event-hash-v2";
const EVENT_ID_DOMAIN: &str = "factory-event-id-v2";
const SHA256_PREFIX: &str = "sha256:";
const INVALID_EVENT_INPUT: &str = "INVALID_EVENT_HASH_INPUT";
const DRAFT7_SCHEMA_URI: &str = "http://json-schema.org/draft-07/schema#";
const MAX_SAFE_INTEGER: u64 = 9_007_199_254_740_991;

// 三个可信 schema validator 按进程惰性初始化一次；缓存中不保存不可信错误正文。
static PREPARED_EVENT_VALIDATOR: OnceLock<Result<Validator, EventHashError>> = OnceLock::new();
static DURABLE_EVENT_VALIDATOR: OnceLock<Result<Validator, EventHashError>> = OnceLock::new();
static PREPARED_BATCH_VALIDATOR: OnceLock<Result<Validator, EventHashError>> = OnceLock::new();

/// genesis 的固定 predecessor，不由 Adapter 或 coordinator 传入。
pub const GENESIS_PREDECESSOR: &str =
    "sha256:0000000000000000000000000000000000000000000000000000000000000000";

/// Adapter wire PreparedEventV2 的精确字段集。
const PREPARED_EVENT_FIELDS: [&str; 22] = [
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
];

/// coordinator 只能在非 wire MaterializationInput 中附加的三个字段。
const COORDINATOR_EVENT_FIELDS: [&str; 3] = ["batchOrdinal", "runSeq", "durabilityClass"];
const MATERIALIZATION_INPUT_FIELDS: [&str; 5] = [
    "preparedBatchId",
    "taskId",
    "expectedTaskSeq",
    "expectedEventDigest",
    "events",
];
const COMMITTED_ANCHOR_FIELDS: [&str; 2] = ["committedTaskSeq", "committedEventDigest"];

/// 事件物化边界的稳定错误类型，不回显不可信 payload。
#[derive(Debug)]
pub struct EventHashError {
    /// 已脱敏的稳定错误细节。
    pub detail: String,
}

impl EventHashError {
    /// 稳定错误码。
    pub const ERROR_CODE: &'static str = "event-hash-error";

    /// 构造错误。
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for EventHashError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "[{}] {}", Self::ERROR_CODE, self.detail)
    }
}

impl std::error::Error for EventHashError {}

impl From<CanonicalJsonError> for EventHashError {
    /// 规范化异常不能把不可信 payload 带出纯哈希边界。
    fn from(_error: CanonicalJsonError) -> Self {
        EventHashError::new(INVALID_EVENT_INPUT)
    }
}

/// 外部 schema reference 的拒绝型 retriever，绝不访问网络或文件。
#[derive(Debug)]
struct DeniedExternalReference;

impl fmt::Display for DeniedExternalReference {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("external schema reference denied")
    }
}

impl Error for DeniedExternalReference {}

struct DenyExternalReferences;

impl Retrieve for DenyExternalReferences {
    fn retrieve(&self, _uri: &Uri<String>) -> Result<Value, Box<dyn Error + Send + Sync>> {
        Err(Box::new(DeniedExternalReference))
    }
}

/// 解析固定位置的 ASCII 数字，失败时拒绝格式校验。
fn parse_decimal(bytes: &[u8], start: usize, length: usize) -> Option<u32> {
    let slice = bytes.get(start..start.checked_add(length)?)?;
    if !slice.iter().all(u8::is_ascii_digit) {
        return None;
    }
    Some(
        slice
            .iter()
            .fold(0_u32, |value, byte| value * 10 + u32::from(byte - b'0')),
    )
}

/// 三端共用的 Factory RFC3339 子集。
fn is_factory_rfc3339_date_time(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.len() < 20
        || bytes.get(4) != Some(&b'-')
        || bytes.get(7) != Some(&b'-')
        || !matches!(bytes.get(10), Some(b'T' | b't'))
        || bytes.get(13) != Some(&b':')
        || bytes.get(16) != Some(&b':')
    {
        return false;
    }
    let (Some(year), Some(month), Some(day), Some(hour), Some(minute), Some(second)) = (
        parse_decimal(bytes, 0, 4),
        parse_decimal(bytes, 5, 2),
        parse_decimal(bytes, 8, 2),
        parse_decimal(bytes, 11, 2),
        parse_decimal(bytes, 14, 2),
        parse_decimal(bytes, 17, 2),
    ) else {
        return false;
    };
    if year == 0 || !(1..=12).contains(&month) || hour > 23 || minute > 59 || second > 59 {
        return false;
    }
    let days_in_month = if month == 2 {
        if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) {
            29
        } else {
            28
        }
    } else if matches!(month, 4 | 6 | 9 | 11) {
        30
    } else {
        31
    };
    if !(1..=days_in_month).contains(&day) {
        return false;
    }
    let mut cursor = 19;
    if bytes.get(cursor) == Some(&b'.') {
        cursor += 1;
        let fraction_start = cursor;
        while bytes.get(cursor).is_some_and(u8::is_ascii_digit) {
            cursor += 1;
        }
        if cursor == fraction_start {
            return false;
        }
    }
    match bytes.get(cursor) {
        Some(b'Z' | b'z') => cursor + 1 == bytes.len(),
        Some(b'+' | b'-') if cursor + 6 == bytes.len() && bytes.get(cursor + 3) == Some(&b':') => {
            let Some(offset_hour) = parse_decimal(bytes, cursor + 1, 2) else {
                return false;
            };
            let Some(offset_minute) = parse_decimal(bytes, cursor + 4, 2) else {
                return false;
            };
            offset_hour <= 23 && offset_minute <= 59
        }
        _ => false,
    }
}

/// schema 递归只允许本地 reference 和冻结 date-time format。
fn assert_local_known_subschemas(value: &Value) -> Result<(), EventHashError> {
    let Some(object) = value.as_object() else {
        return Ok(());
    };
    if let Some(reference) = object.get("$ref") {
        if !reference.as_str().is_some_and(|item| item.starts_with('#')) {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }
    if let Some(format_name) = object.get("format") {
        if format_name.as_str() != Some("date-time") {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }
    for keyword in [
        "additionalItems",
        "additionalProperties",
        "contains",
        "if",
        "then",
        "else",
        "not",
        "propertyNames",
    ] {
        if let Some(child_schema) = object.get(keyword) {
            assert_local_known_subschemas(child_schema)?;
        }
    }
    for keyword in ["properties", "patternProperties", "definitions", "$defs"] {
        if let Some(schema_map) = object.get(keyword).and_then(Value::as_object) {
            for child_schema in schema_map.values() {
                assert_local_known_subschemas(child_schema)?;
            }
        }
    }
    if let Some(items) = object.get("items") {
        if let Some(item_array) = items.as_array() {
            for child_schema in item_array {
                assert_local_known_subschemas(child_schema)?;
            }
        } else {
            assert_local_known_subschemas(items)?;
        }
    }
    for keyword in ["allOf", "anyOf", "oneOf"] {
        if let Some(schema_array) = object.get(keyword).and_then(Value::as_array) {
            for child_schema in schema_array {
                assert_local_known_subschemas(child_schema)?;
            }
        }
    }
    if let Some(dependencies) = object.get("dependencies").and_then(Value::as_object) {
        for dependency in dependencies.values() {
            if dependency.is_object() || dependency.is_boolean() {
                assert_local_known_subschemas(dependency)?;
            }
        }
    }
    Ok(())
}

/// 恒定时间比较 schema 内容摘要。
fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    left.iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b))
        == 0
}

/// 编译 codegen 内嵌的可信 Draft7 schema。
fn compile_validator_schema(
    schema_json: &str,
    expected_sha256: &str,
) -> Result<Validator, EventHashError> {
    let actual_sha256 = sha256_prefixed(schema_json.as_bytes());
    if !constant_time_equal(actual_sha256.as_bytes(), expected_sha256.as_bytes()) {
        return Err(EventHashError::new(INVALID_EVENT_INPUT));
    }
    let schema: Value =
        serde_json::from_str(schema_json).map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))?;
    if schema.as_object().is_none()
        || schema.get("$schema").and_then(Value::as_str) != Some(DRAFT7_SCHEMA_URI)
    {
        return Err(EventHashError::new(INVALID_EVENT_INPUT));
    }
    assert_local_known_subschemas(&schema)?;
    jsonschema::options()
        .with_draft(Draft::Draft7)
        .with_retriever(DenyExternalReferences)
        .with_format("date-time", is_factory_rfc3339_date_time)
        .should_validate_formats(true)
        .should_ignore_unknown_formats(false)
        .build(&schema)
        .map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))
}

/// 返回单次编译的 PreparedEventV2 validator；初始化失败统一为稳定错误。
fn prepared_event_validator() -> Result<&'static Validator, EventHashError> {
    let cached = PREPARED_EVENT_VALIDATOR.get_or_init(|| {
        compile_validator_schema(
            PREPARED_EVENT_V2_SCHEMA_JSON,
            PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
        )
    });
    cached
        .as_ref()
        .map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))
}

/// 返回单次编译的 DurableEventV2 validator；初始化失败统一为稳定错误。
fn durable_event_validator() -> Result<&'static Validator, EventHashError> {
    let cached = DURABLE_EVENT_VALIDATOR.get_or_init(|| {
        compile_validator_schema(
            DURABLE_EVENT_V2_SCHEMA_JSON,
            DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
        )
    });
    cached
        .as_ref()
        .map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))
}

/// 返回单次编译的 PreparedBatchV2 validator；初始化失败统一为稳定错误。
fn prepared_batch_validator() -> Result<&'static Validator, EventHashError> {
    let cached = PREPARED_BATCH_VALIDATOR.get_or_init(|| {
        compile_validator_schema(
            PREPARED_BATCH_V2_SCHEMA_JSON,
            PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
        )
    });
    cached
        .as_ref()
        .map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))
}

/// 输入和输出都必须经过内嵌权威 schema。
fn validate_wire(value: &Value, validator: &Validator) -> Result<(), EventHashError> {
    if validator.is_valid(value) {
        Ok(())
    } else {
        Err(EventHashError::new(INVALID_EVENT_INPUT))
    }
}

/// 要求 JSON 对象，拒绝 array/null。
fn require_object<'a>(
    value: &'a Value,
    label: &str,
) -> Result<&'a Map<String, Value>, EventHashError> {
    match value {
        Value::Object(object) => Ok(object),
        _ => Err(EventHashError::new(format!("{label} 必须为对象"))),
    }
}

/// exact-set 让未知字段无法静默进入 MaterializationInput 或 hash。
fn require_exact_keys(
    object: &Map<String, Value>,
    expected: &[&str],
    label: &str,
) -> Result<(), EventHashError> {
    if object.len() != expected.len() || expected.iter().any(|key| !object.contains_key(*key)) {
        return Err(EventHashError::new(format!(
            "{label} 字段集合不符合 MaterializationInput 合同"
        )));
    }
    Ok(())
}

/// 计算 sha256:<lowercase hex>。
fn sha256_prefixed(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{SHA256_PREFIX}{:x}", hasher.finalize())
}

/// 只接受冻结的小写 sha256 摘要。
fn is_sha256_digest(value: &str) -> bool {
    value.len() == 71
        && value.starts_with(SHA256_PREFIX)
        && value
            .as_bytes()
            .iter()
            .skip(SHA256_PREFIX.len())
            .all(u8::is_ascii_hexdigit)
        && value
            .as_bytes()
            .iter()
            .skip(SHA256_PREFIX.len())
            .all(|byte| !byte.is_ascii_uppercase())
}

/// 按 Draft7 数学整数语义接受 1/1.0/1e0/-0，并限制在 I-JSON 安全范围。
fn require_safe_nonnegative_integer(value: &Value) -> Result<u64, EventHashError> {
    if let Some(candidate) = value.as_u64() {
        return (candidate <= MAX_SAFE_INTEGER)
            .then_some(candidate)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT));
    }
    let candidate = value
        .as_f64()
        .filter(|number| {
            number.is_finite()
                && *number >= 0.0
                && number.fract() == 0.0
                && *number <= MAX_SAFE_INTEGER as f64
        })
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    Ok(candidate as u64)
}

/// 规范化对象中的数学整数，避免 -0/1.0 进入三语 hash 输出。
fn normalize_integer_field(
    object: &mut Map<String, Value>,
    field: &str,
) -> Result<u64, EventHashError> {
    let integer = require_safe_nonnegative_integer(
        object
            .get(field)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    object.insert(field.to_string(), Value::from(integer));
    Ok(integer)
}

/// taskSeq、ordinal、runSeq 的递增必须显式检查 I-JSON 上界。
fn checked_safe_add(value: u64, increment: usize) -> Result<u64, EventHashError> {
    let increment =
        u64::try_from(increment).map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))?;
    value
        .checked_add(increment)
        .filter(|candidate| *candidate <= MAX_SAFE_INTEGER)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))
}

/// 校验显式 genesis/null 或非负安全整数 taskSeq。
fn optional_task_seq(value: &Value, label: &str) -> Result<Option<u64>, EventHashError> {
    match value {
        Value::Null => Ok(None),
        Value::Number(_) => require_safe_nonnegative_integer(value).map(Some),
        _ => Err(EventHashError::new(format!(
            "{label} 必须为 I-JSON 非负安全整数或 null"
        ))),
    }
}

/// 校验显式 genesis/null 或 SHA-256 event digest。
fn optional_digest(value: &Value, label: &str) -> Result<Option<String>, EventHashError> {
    match value {
        Value::Null => Ok(None),
        Value::String(digest) if is_sha256_digest(digest) => Ok(Some(digest.clone())),
        _ => Err(EventHashError::new(format!(
            "{label} 必须为 sha256 digest 或 null"
        ))),
    }
}

/// 禁止半空 head。
fn require_head_pair(
    task_seq: Option<u64>,
    event_digest: &Option<String>,
    label: &str,
) -> Result<(), EventHashError> {
    if task_seq.is_none() != event_digest.is_none() {
        return Err(EventHashError::new(format!(
            "{label} 的 taskSeq 与 eventDigest 必须同时为 null 或同时存在"
        )));
    }
    Ok(())
}

/// 从 Adapter 稳定 ingestEventId 推导幂等 eventId。
pub fn event_id(ingest_event_id: &str) -> Result<String, EventHashError> {
    if ingest_event_id.is_empty() {
        return Err(EventHashError::new("ingestEventId 必须为非空字符串"));
    }
    let bytes = canonicalize(&CanonValue::Array(vec![
        CanonValue::Str(EVENT_ID_DOMAIN.to_string()),
        CanonValue::Str(ingest_event_id.to_string()),
    ]))?;
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    Ok(format!("evt_{:x}", hasher.finalize()))
}

/// 计算已脱敏 payload 的 JCS SHA-256 摘要。
pub fn payload_digest(payload: &Value) -> Result<String, EventHashError> {
    Ok(sha256_prefixed(&canonicalize_value(payload)?))
}

/// 计算 redactions 列表的 JCS SHA-256 摘要。
pub fn redaction_manifest_digest(redactions: &Value) -> Result<String, EventHashError> {
    Ok(sha256_prefixed(&canonicalize_value(redactions)?))
}

/// 从对象复制精确字段投影，缺字段立即失败。
fn project_prepared_event(event: &Map<String, Value>) -> Result<Value, EventHashError> {
    let mut prepared = Map::new();
    for field in PREPARED_EVENT_FIELDS {
        let value = event
            .get(field)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        prepared.insert(field.to_string(), value.clone());
    }
    Ok(Value::Object(prepared))
}

/// v1 redaction replacement 只允许冻结的不可逆标记，禁止把原文塞回事件。
fn is_frozen_redaction_replacement(value: &str) -> bool {
    let Some(body) = value
        .strip_prefix("[REDACTED")
        .and_then(|suffix| suffix.strip_suffix(']'))
    else {
        return false;
    };
    if body.is_empty() {
        return true;
    }
    body.strip_prefix(':').is_some_and(|tag| {
        !tag.is_empty()
            && tag.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
            })
    })
}

/// 校验 Draft7 无法表达的 span/redaction 顺序，并把全部事件整数规范化。
fn validate_prepared_event_semantics(
    prepared: &mut Map<String, Value>,
) -> Result<(), EventHashError> {
    normalize_integer_field(prepared, "schemaVersion")?;
    normalize_integer_field(prepared, "sourceSeq")?;
    normalize_integer_field(prepared, "monotonicTimeNs")?;

    let process_identity = prepared
        .get_mut("processIdentity")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    normalize_integer_field(process_identity, "pid")?;

    let source_span = prepared
        .get_mut("sourceTransportSpan")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    let coordinate = source_span
        .get("coordinate")
        .and_then(Value::as_str)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?
        .to_string();
    let mapping_precision = source_span
        .get("mappingPrecision")
        .and_then(Value::as_str)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?
        .to_string();
    // 坐标轴描述 Provider 原始传输单位，mappingPrecision 描述映射精度；二者不能按同名字面绑定。
    match (coordinate.as_str(), mapping_precision.as_str()) {
        ("none", "none") => {
            if !source_span.get("start").is_some_and(Value::is_null)
                || !source_span.get("endExclusive").is_some_and(Value::is_null)
            {
                return Err(EventHashError::new(INVALID_EVENT_INPUT));
            }
        }
        ("provider_transport_bytes" | "provider_transport_chars", "byte") => {
            let start = normalize_integer_field(source_span, "start")?;
            let end = normalize_integer_field(source_span, "endExclusive")?;
            if start >= end {
                return Err(EventHashError::new(INVALID_EVENT_INPUT));
            }
        }
        ("provider_transport_bytes" | "provider_transport_chars", "field" | "frame" | "none") => {
            if !source_span.get("start").is_some_and(Value::is_null)
                || !source_span.get("endExclusive").is_some_and(Value::is_null)
            {
                return Err(EventHashError::new(INVALID_EVENT_INPUT));
            }
        }
        _ => return Err(EventHashError::new(INVALID_EVENT_INPUT)),
    }

    let sanitized_span = prepared
        .get_mut("sanitizedStreamSpan")
        .and_then(Value::as_object_mut)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    let sanitized_start = normalize_integer_field(sanitized_span, "start")?;
    let sanitized_end = normalize_integer_field(sanitized_span, "endExclusive")?;
    if sanitized_start >= sanitized_end {
        return Err(EventHashError::new(INVALID_EVENT_INPUT));
    }

    let redactions = prepared
        .get_mut("redactions")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    for redaction_value in redactions {
        let redaction = redaction_value
            .as_object_mut()
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        let replacement_is_valid = redaction
            .get("replacement")
            .and_then(Value::as_str)
            .is_some_and(is_frozen_redaction_replacement);
        let byte_range = redaction
            .get_mut("byteRange")
            .and_then(Value::as_object_mut)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        let start = normalize_integer_field(byte_range, "start")?;
        let end = normalize_integer_field(byte_range, "endExclusive")?;
        if start >= end || !replacement_is_valid {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }
    Ok(())
}

/// 验证 PreparedBatchV2 manifest 及 Draft7 无法表达的全局连续性语义。
///
/// 本函数只校验不可变 wire manifest，不执行 Task 7 的 claim、CAS 或 group commit。
pub fn validate_prepared_batch_manifest(manifest: &Value) -> Result<(), EventHashError> {
    validate_wire(manifest, prepared_batch_validator()?)?;
    let object = require_object(manifest, "PreparedBatchV2")?;
    require_safe_nonnegative_integer(
        object
            .get("writerEpoch")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    let event_count = require_safe_nonnegative_integer(
        object
            .get("eventCount")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    require_safe_nonnegative_integer(
        object
            .get("payloadBytes")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    let first_ordinal = require_safe_nonnegative_integer(
        object
            .get("firstBatchOrdinal")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    let last_ordinal = require_safe_nonnegative_integer(
        object
            .get("lastBatchOrdinal")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
    )?;
    let ordinal_span = last_ordinal
        .checked_sub(first_ordinal)
        .and_then(|value| value.checked_add(1))
        .filter(|value| *value <= MAX_SAFE_INTEGER)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    let ordered_ingest_ids = object
        .get("orderedIngestIds")
        .and_then(Value::as_array)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    let ingest_count = u64::try_from(ordered_ingest_ids.len())
        .map_err(|_| EventHashError::new(INVALID_EVENT_INPUT))?;
    if event_count != ingest_count || event_count != ordinal_span {
        return Err(EventHashError::new(INVALID_EVENT_INPUT));
    }
    let mut normalized_ingest_ids = HashSet::new();
    for ingest_id_value in ordered_ingest_ids {
        let ingest_id = ingest_id_value
            .as_str()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        let normalized_ingest_id: String = ingest_id.nfc().collect();
        if !normalized_ingest_ids.insert(normalized_ingest_id) {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }

    let heads = object
        .get("perTaskExpectedHeads")
        .and_then(Value::as_array)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    let mut task_ids = HashSet::new();
    for head_value in heads {
        let head = require_object(head_value, "perTaskExpectedHead")?;
        let task_id = head
            .get("taskId")
            .and_then(Value::as_str)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        if !task_ids.insert(task_id) {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
        let task_seq = optional_task_seq(
            head.get("expectedTaskSeq")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
            "expectedTaskSeq",
        )?;
        let event_digest = optional_digest(
            head.get("expectedEventDigest")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
            "expectedEventDigest",
        )?;
        require_head_pair(task_seq, &event_digest, "perTaskExpectedHead")?;
        let head_first = require_safe_nonnegative_integer(
            head.get("firstBatchOrdinal")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        let head_last = require_safe_nonnegative_integer(
            head.get("lastBatchOrdinal")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        if head_first > head_last || head_first < first_ordinal || head_last > last_ordinal {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }

    let segments = object
        .get("segmentDigests")
        .and_then(Value::as_array)
        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
    for segment_value in segments {
        let segment = require_object(segment_value, "segmentDigest")?;
        let ordinal_range = require_object(
            segment
                .get("ordinalRange")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
            "segment.ordinalRange",
        )?;
        let segment_first = require_safe_nonnegative_integer(
            ordinal_range
                .get("first")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        let segment_last = require_safe_nonnegative_integer(
            ordinal_range
                .get("lastExclusive")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        let sanitized_span = require_object(
            segment
                .get("sanitizedSpan")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
            "segment.sanitizedSpan",
        )?;
        let sanitized_start = require_safe_nonnegative_integer(
            sanitized_span
                .get("start")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        let sanitized_end = require_safe_nonnegative_integer(
            sanitized_span
                .get("endExclusive")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        match segment
            .get("mappingPrecision")
            .and_then(Value::as_str)
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?
        {
            "byte" => {
                let source_span = require_object(
                    segment
                        .get("sourceSpan")
                        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
                    "segment.sourceSpan",
                )?;
                let source_start = require_safe_nonnegative_integer(
                    source_span
                        .get("start")
                        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
                )?;
                let source_end = require_safe_nonnegative_integer(
                    source_span
                        .get("endExclusive")
                        .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
                )?;
                if source_start >= source_end {
                    return Err(EventHashError::new(INVALID_EVENT_INPUT));
                }
            }
            "field" | "frame" | "none" => {
                if !segment.get("sourceSpan").is_some_and(Value::is_null) {
                    return Err(EventHashError::new(INVALID_EVENT_INPUT));
                }
            }
            _ => return Err(EventHashError::new(INVALID_EVENT_INPUT)),
        }
        if segment_first >= segment_last || sanitized_start >= sanitized_end {
            return Err(EventHashError::new(INVALID_EVENT_INPUT));
        }
    }
    Ok(())
}

/// 物化单 Task MaterializationInput 为已验证 DurableEventV2 列表。
///
/// 本函数不执行数据库读写、日志、claim 或 Task 7 group commit。
pub fn materialize_batch(
    raw_input: &Value,
    raw_anchor: &Value,
) -> Result<Vec<Value>, EventHashError> {
    let input = require_object(raw_input, "input")?;
    let anchor = require_object(raw_anchor, "anchor")?;
    require_exact_keys(input, &MATERIALIZATION_INPUT_FIELDS, "input")?;
    require_exact_keys(anchor, &COMMITTED_ANCHOR_FIELDS, "anchor")?;

    let expected_task_seq = optional_task_seq(
        input
            .get("expectedTaskSeq")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        "input.expectedTaskSeq",
    )?;
    let expected_event_digest = optional_digest(
        input
            .get("expectedEventDigest")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        "input.expectedEventDigest",
    )?;
    let committed_task_seq = optional_task_seq(
        anchor
            .get("committedTaskSeq")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        "anchor.committedTaskSeq",
    )?;
    let committed_event_digest = optional_digest(
        anchor
            .get("committedEventDigest")
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        "anchor.committedEventDigest",
    )?;
    require_head_pair(expected_task_seq, &expected_event_digest, "input")?;
    require_head_pair(committed_task_seq, &committed_event_digest, "anchor")?;
    if expected_task_seq != committed_task_seq || expected_event_digest != committed_event_digest {
        return Err(EventHashError::new("expected head 与已提交 anchor 不一致"));
    }

    let task_id = input
        .get("taskId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| EventHashError::new("input.taskId 必须为非空字符串"))?
        .to_string();
    let prepared_batch_id = input
        .get("preparedBatchId")
        .and_then(Value::as_str)
        .filter(|value| !value.is_empty())
        .ok_or_else(|| EventHashError::new("input.preparedBatchId 必须为非空字符串"))?
        .to_string();
    let events = input
        .get("events")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| EventHashError::new("input.events 必须为非空数组"))?;

    let base_task_seq = match committed_task_seq {
        Some(value) => checked_safe_add(value, 1)?,
        None => 0,
    };
    let mut previous_event_digest =
        committed_event_digest.unwrap_or_else(|| GENESIS_PREDECESSOR.to_string());
    let mut previous_batch_ordinal: Option<u64> = None;
    let mut seen_ingest_ids: HashSet<String> = HashSet::new();
    let mut durable_events = Vec::with_capacity(events.len());

    for (index, raw_event) in events.iter().enumerate() {
        let event = require_object(raw_event, &format!("input.events[{index}]"))?;
        let expected_event_fields = [
            PREPARED_EVENT_FIELDS.as_slice(),
            COORDINATOR_EVENT_FIELDS.as_slice(),
        ]
        .concat();
        require_exact_keys(
            event,
            &expected_event_fields,
            &format!("input.events[{index}]"),
        )?;
        if event.get("taskId") != Some(&Value::String(task_id.clone())) {
            return Err(EventHashError::new(
                "切片内 event.taskId 与 input.taskId 不一致",
            ));
        }

        // coordinator 字段外的输入必须先通过权威 PreparedEventV2 schema。
        let mut prepared_event = project_prepared_event(event)?;
        validate_wire(&prepared_event, prepared_event_validator()?)?;
        let prepared = prepared_event
            .as_object_mut()
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?;
        validate_prepared_event_semantics(prepared)?;

        let ingest_event_id = prepared
            .get("ingestEventId")
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
            .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?
            .to_string();
        // NFC 等价 ID 会导出相同 eventId，必须在 hash 前拒绝。
        let normalized_ingest_id: String = ingest_event_id.nfc().collect();
        if !seen_ingest_ids.insert(normalized_ingest_id) {
            return Err(EventHashError::new("切片内 ingestEventId 重复"));
        }

        let batch_ordinal = require_safe_nonnegative_integer(
            event
                .get("batchOrdinal")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        if previous_batch_ordinal.is_some_and(|value| batch_ordinal <= value) {
            return Err(EventHashError::new(
                "单 Task slice 的 batchOrdinal 必须严格递增",
            ));
        }
        previous_batch_ordinal = Some(batch_ordinal);
        let run_seq = require_safe_nonnegative_integer(
            event
                .get("runSeq")
                .ok_or_else(|| EventHashError::new(INVALID_EVENT_INPUT))?,
        )?;
        let durability_class = event
            .get("durabilityClass")
            .and_then(Value::as_str)
            .filter(|value| {
                matches!(
                    *value,
                    "side_effect_receipt" | "provider_source" | "derived"
                )
            })
            .ok_or_else(|| EventHashError::new("event.durabilityClass 不在 DurableEventV2 枚举内"))?
            .to_string();

        // writer 只分配 head/digest 相关字段；所有 Adapter 字段逐字复制。
        let mut durable = Map::new();
        durable.insert(
            "schemaVersion".to_string(),
            prepared["schemaVersion"].clone(),
        );
        durable.insert(
            "durabilityClass".to_string(),
            Value::String(durability_class),
        );
        durable.insert(
            "eventId".to_string(),
            Value::String(event_id(&ingest_event_id)?),
        );
        durable.insert("taskId".to_string(), Value::String(task_id.clone()));
        durable.insert(
            "taskSeq".to_string(),
            Value::from(checked_safe_add(base_task_seq, index)?),
        );
        durable.insert("runId".to_string(), prepared["runId"].clone());
        durable.insert("runSeq".to_string(), Value::from(run_seq));
        durable.insert("stepId".to_string(), prepared["stepId"].clone());
        durable.insert("attemptId".to_string(), prepared["attemptId"].clone());
        durable.insert("source".to_string(), prepared["source"].clone());
        durable.insert(
            "ingestEventId".to_string(),
            Value::String(ingest_event_id.clone()),
        );
        durable.insert("eventType".to_string(), prepared["eventType"].clone());
        durable.insert(
            "providerEventId".to_string(),
            prepared["providerEventId"].clone(),
        );
        durable.insert("sourceSeq".to_string(), prepared["sourceSeq"].clone());
        durable.insert("streamId".to_string(), prepared["streamId"].clone());
        durable.insert(
            "preparedBatchId".to_string(),
            Value::String(prepared_batch_id.clone()),
        );
        durable.insert("batchOrdinal".to_string(), Value::from(batch_ordinal));
        durable.insert(
            "sourceTransportSpan".to_string(),
            prepared["sourceTransportSpan"].clone(),
        );
        durable.insert(
            "sanitizedStreamSpan".to_string(),
            prepared["sanitizedStreamSpan"].clone(),
        );
        durable.insert("wallTime".to_string(), prepared["wallTime"].clone());
        durable.insert(
            "monotonicTimeNs".to_string(),
            prepared["monotonicTimeNs"].clone(),
        );
        durable.insert("ingestedAt".to_string(), prepared["ingestedAt"].clone());
        durable.insert(
            "providerVersion".to_string(),
            prepared["providerVersion"].clone(),
        );
        durable.insert(
            "adapterVersion".to_string(),
            prepared["adapterVersion"].clone(),
        );
        durable.insert(
            "processIdentity".to_string(),
            prepared["processIdentity"].clone(),
        );
        durable.insert("payload".to_string(), prepared["payload"].clone());
        durable.insert(
            "sanitizedProviderFrameDigest".to_string(),
            prepared["sanitizedProviderFrameDigest"].clone(),
        );
        durable.insert(
            "payloadDigest".to_string(),
            Value::String(payload_digest(&prepared["payload"])?),
        );
        durable.insert(
            "previousEventDigest".to_string(),
            Value::String(previous_event_digest.clone()),
        );
        durable.insert("redactions".to_string(), prepared["redactions"].clone());
        durable.insert(
            "redactionManifestDigest".to_string(),
            Value::String(redaction_manifest_digest(&prepared["redactions"])?),
        );

        let event_digest = sha256_prefixed(&canonicalize_value(&Value::Object(durable.clone()))?);
        durable.insert(
            "eventDigest".to_string(),
            Value::String(event_digest.clone()),
        );
        let durable_value = Value::Object(durable);
        // eventDigest 已加入后验证最终 DurableEventV2，阻断构造/枚举漂移。
        validate_wire(&durable_value, durable_event_validator()?)?;
        durable_events.push(durable_value);
        previous_event_digest = event_digest;
    }

    Ok(durable_events)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn trusted_validators_are_cached() {
        // 指针身份证明每个可信 schema 在进程内只编译一次。
        let prepared_first =
            prepared_event_validator().expect("PreparedEvent validator") as *const _;
        let prepared_second =
            prepared_event_validator().expect("PreparedEvent validator") as *const _;
        let durable_first = durable_event_validator().expect("DurableEvent validator") as *const _;
        let durable_second = durable_event_validator().expect("DurableEvent validator") as *const _;
        let batch_first = prepared_batch_validator().expect("PreparedBatch validator") as *const _;
        let batch_second = prepared_batch_validator().expect("PreparedBatch validator") as *const _;
        assert_eq!(prepared_first, prepared_second);
        assert_eq!(durable_first, durable_second);
        assert_eq!(batch_first, batch_second);
    }

    #[test]
    fn validator_initialization_error_is_redacted() {
        // 初始化失败只能返回稳定错误，不得回显 schema 中的敏感 sentinel。
        let schema_with_secret = r#"{"secret":"must-not-appear"}"#;
        let wrong_digest = format!("sha256:{}", "0".repeat(64));
        let error = match compile_validator_schema(schema_with_secret, &wrong_digest) {
            Ok(_) => panic!("错误摘要不得通过可信 schema 初始化"),
            Err(error) => error,
        };
        assert_eq!(error.detail, INVALID_EVENT_INPUT);
        assert!(!error.to_string().contains("must-not-appear"));
    }
}
