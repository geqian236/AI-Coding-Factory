//! crates/factory-contracts/src/plan.rs
//!
//! 计划语义哈希、修订摘要与 barrier 身份 —— Rust 实现。
//! 三个标识使用同一 canonical.rs 底座（NFC + RFC 8785 JCS），
//! 必须与 Python (factory_agent.policy.plan_hash) 及 TypeScript
//! (packages/factory-contracts/src/plan.ts) 字节级一致。
//!
//!   semanticPlanHash
//!       只标识计划语义，跨修订版本稳定；纳入字段严格来自 Master Spec §6.2：
//!         schemaVersion, goal, assumptions, scope, constraints,
//!         acceptanceCriteria, targetStage,
//!         repository{mode, root, baseBranch, baseCommit},
//!         workPlan{dagVersion, nodes, barriers},
//!         riskProfile, nodeCapabilityMapVersion, stageCapabilityMapVersion
//!       排除 taskId / specRevision / parentRevisionId / intentAuthorizationId /
//!       生成时间 / 事件·授权·receipt ID / hash·signature 字段。
//!
//!   planRevisionDigest
//!       对「除 planRevisionDigest 与 signature 外」的完整不可变 PlanRevision
//!       做同一 canonicalizer；语义相同可共享 semanticPlanHash，
//!       但不同谱系必须得到不同 planRevisionDigest。
//!
//!   barrierId
//!       "bar_" + lowercaseHex(SHA-256(JCS(
//!         ["factory-barrier-v1", runId, planRevisionDigest,
//!          businessPhase, barrierOrdinal])))
//!
//! 失败错误码：plan-hash-error（不记录完整计划正文）。

use crate::canonical::{canonicalize, canonicalize_value, CanonValue, CanonicalJsonError};
use crate::generated::{
    PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON, PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
    PLAN_REVISION_SCHEMA_JSON, PLAN_REVISION_SCHEMA_JSON_SHA256, RUN_SPEC_SCHEMA_JSON,
    RUN_SPEC_SCHEMA_JSON_SHA256,
};
use jsonschema::{Draft, Retrieve, Uri, Validator};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::error::Error;
use std::fmt;

/// 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。
pub const PLAN_HASH_VERSION: &str = "plan-hash-v1";

/// barrier 身份的域分离标签（数组首元素）。
const BARRIER_DOMAIN: &str = "factory-barrier-v1";

/// semanticPlanHash 纳入的顶层字段（严格来自 Master Spec §6.2）。
const SEMANTIC_TOP_FIELDS: [&str; 10] = [
    "schemaVersion",
    "goal",
    "assumptions",
    "scope",
    "constraints",
    "acceptanceCriteria",
    "targetStage",
    "riskProfile",
    "nodeCapabilityMapVersion",
    "stageCapabilityMapVersion",
];

/// repository 子对象纳入的字段。
const REPO_FIELDS: [&str; 4] = ["mode", "root", "baseBranch", "baseCommit"];

/// workPlan 子对象纳入的字段。
const WORKPLAN_FIELDS: [&str; 3] = ["dagVersion", "nodes", "barriers"];

/// 摘要输出前缀。
const SHA256_PREFIX: &str = "sha256:";

/// 不可信 wire、损坏生成常量和 validator 初始化失败共用的稳定脱敏明细。
const STABLE_VALIDATION_DETAIL: &str = "INVALID_PLAN_HASH_INPUT";

/// 运行时只接受 codegen 锁定的精确 Draft7 schema URI。
const DRAFT7_SCHEMA_URI: &str = "http://json-schema.org/draft-07/schema#";

/// 计划哈希输入非法（缺字段、类型错误等）时抛出。
#[derive(Debug)]
pub struct PlanHashError {
    /// 已脱敏的错误明细。
    pub detail: String,
}

impl PlanHashError {
    /// 稳定错误码。
    pub const ERROR_CODE: &'static str = "plan-hash-error";

    /// 构造错误。
    fn new(detail: impl Into<String>) -> Self {
        Self {
            detail: detail.into(),
        }
    }
}

impl fmt::Display for PlanHashError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", Self::ERROR_CODE, self.detail)
    }
}

impl std::error::Error for PlanHashError {}

impl From<CanonicalJsonError> for PlanHashError {
    /// 规范化错误上抛为稳定计划哈希错误，禁止泄露不可信 payload 或底层正文。
    fn from(_error: CanonicalJsonError) -> Self {
        PlanHashError::new(STABLE_VALIDATION_DETAIL)
    }
}

/// 外部 $ref 的拒绝型 retriever：即使未来 schema 绕过前置检查，也绝不访问网络或文件。
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

/// 将固定位置的 ASCII 数字解析为整数；非 ASCII 数字或越界索引一律拒绝。
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

/// 与 Python/TypeScript 共享的 Factory date-time 子集：wire pattern 后再核验日历与 UTC offset。
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

/// 只沿 Draft7 的子 schema keyword 递归，不能把 properties 的用户字段名误判为 format/$ref。
fn assert_local_known_subschemas(value: &Value) -> Result<(), PlanHashError> {
    let Some(object) = value.as_object() else {
        return Ok(());
    };
    if let Some(reference) = object.get("$ref") {
        if !reference.as_str().is_some_and(|item| item.starts_with('#')) {
            return Err(PlanHashError::new(STABLE_VALIDATION_DETAIL));
        }
    }
    if let Some(format_name) = object.get("format") {
        if format_name.as_str() != Some("date-time") {
            return Err(PlanHashError::new(STABLE_VALIDATION_DETAIL));
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

/// 编译生成常量中的完整 Draft7 schema；任何第三方错误都归一化为计划哈希稳定错误。
fn compile_validator_value(schema: &Value) -> Result<Validator, PlanHashError> {
    if schema.as_object().is_none()
        || schema.get("$schema").and_then(Value::as_str) != Some(DRAFT7_SCHEMA_URI)
    {
        return Err(PlanHashError::new(STABLE_VALIDATION_DETAIL));
    }
    assert_local_known_subschemas(schema)?;
    jsonschema::options()
        .with_draft(Draft::Draft7)
        .with_retriever(DenyExternalReferences)
        .with_format("date-time", is_factory_rfc3339_date_time)
        .should_validate_formats(true)
        .should_ignore_unknown_formats(false)
        .build(schema)
        .map_err(|_| PlanHashError::new(STABLE_VALIDATION_DETAIL))
}

/// 恒定时间比较同长度摘要文本；长度不符先拒绝，避免损坏常量进入 JSON 解析。
fn constant_time_equal(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let difference = left
        .iter()
        .zip(right)
        .fold(0_u8, |difference, (a, b)| difference | (a ^ b));
    difference == 0
}

/// 惰性解析生成的原始 JSON 字符串；先核对 UTF-8 内容 SHA，再归一化第三方错误。
fn compile_validator_schema(
    schema_json: &str,
    expected_sha256: &str,
) -> Result<Validator, PlanHashError> {
    let actual_sha256 = sha256_prefixed(schema_json.as_bytes());
    if !constant_time_equal(actual_sha256.as_bytes(), expected_sha256.as_bytes()) {
        return Err(PlanHashError::new(STABLE_VALIDATION_DETAIL));
    }
    let schema = serde_json::from_str(schema_json)
        .map_err(|_| PlanHashError::new(STABLE_VALIDATION_DETAIL))?;
    compile_validator_value(&schema)
}

/// 运行完整权威 schema；非法 wire 在投影或摘要前一律 fail closed。
fn validate_wire(
    value: &Value,
    schema_json: &str,
    expected_sha256: &str,
) -> Result<(), PlanHashError> {
    let validator = compile_validator_schema(schema_json, expected_sha256)?;
    if validator.is_valid(value) {
        Ok(())
    } else {
        Err(PlanHashError::new(STABLE_VALIDATION_DETAIL))
    }
}

/// 校验 value 为对象，否则 fail closed。
fn require_object(value: Option<&Value>) -> Result<&serde_json::Map<String, Value>, PlanHashError> {
    match value {
        Some(Value::Object(mapping)) => Ok(mapping),
        _ => Err(PlanHashError::new(STABLE_VALIDATION_DETAIL)),
    }
}

/// 从 source 提取 fields 指定的字段子集，缺字段即 fail closed。
fn project_subset(
    source: &serde_json::Map<String, Value>,
    fields: &[&str],
) -> Result<Value, PlanHashError> {
    let mut projected = serde_json::Map::new();
    for name in fields {
        match source.get(*name) {
            Some(value) => {
                projected.insert((*name).to_string(), value.clone());
            }
            None => return Err(PlanHashError::new(STABLE_VALIDATION_DETAIL)),
        }
    }
    Ok(Value::Object(projected))
}

/// 构造 semanticPlanHash 的字段投影（不含任何谱系/时间/ID/hash 字段）。
pub fn build_semantic_projection(plan: &Value) -> Result<Value, PlanHashError> {
    validate_wire(plan, RUN_SPEC_SCHEMA_JSON, RUN_SPEC_SCHEMA_JSON_SHA256)?;
    let obj = require_object(Some(plan))?;
    let mut projection = match project_subset(obj, &SEMANTIC_TOP_FIELDS)? {
        Value::Object(m) => m,
        _ => unreachable!(),
    };

    // repository：仅纳入 mode/root/baseBranch/baseCommit。
    let repository = require_object(obj.get("repository"))?;
    projection.insert(
        "repository".to_string(),
        project_subset(repository, &REPO_FIELDS)?,
    );

    // workPlan：仅纳入 dagVersion/nodes/barriers。
    let work_plan = require_object(obj.get("workPlan"))?;
    projection.insert(
        "workPlan".to_string(),
        project_subset(work_plan, &WORKPLAN_FIELDS)?,
    );

    Ok(Value::Object(projection))
}

/// 计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。
fn sha256_prefixed(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{SHA256_PREFIX}{:x}", hasher.finalize())
}

/// 计算 semanticPlanHash（跨修订版本稳定的计划语义身份）。
pub fn semantic_plan_hash(plan: &Value) -> Result<String, PlanHashError> {
    let projection = build_semantic_projection(plan)?;
    let bytes = canonicalize_value(&projection)?;
    Ok(sha256_prefixed(&bytes))
}

/// 计算 planRevisionDigest（完整不可变修订记录身份）。
/// 排除 planRevisionDigest 与 signature，其余字段（含谱系、授权关联、
/// semanticPlanHash、生成时间）全部纳入。
pub fn plan_revision_digest(revision: &Value) -> Result<String, PlanHashError> {
    validate_wire(
        revision,
        PLAN_REVISION_SCHEMA_JSON,
        PLAN_REVISION_SCHEMA_JSON_SHA256,
    )?;
    let obj = require_object(Some(revision))?;
    // full schema 已保证摘要必填、签名可选；精确剥离后再校验机械派生的 material。
    let mut material = obj.clone();
    material
        .remove("planRevisionDigest")
        .ok_or_else(|| PlanHashError::new(STABLE_VALIDATION_DETAIL))?;
    material.remove("signature");
    let material = Value::Object(material);
    validate_wire(
        &material,
        PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
        PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
    )?;
    // 纯函数没有日志出口；稳定错误由后续应用入口脱敏记录，本层不声称已实现 runtime 日志。
    let bytes = canonicalize_value(&material)?;
    Ok(sha256_prefixed(&bytes))
}

/// 计算 barrierId（稳定 barrier 身份，使用域分离数组）。
pub fn barrier_id(
    run_id: &str,
    plan_revision_digest_value: &str,
    business_phase: &str,
    barrier_ordinal: i64,
) -> Result<String, PlanHashError> {
    if run_id.is_empty() {
        return Err(PlanHashError::new("run_id 必须为非空字符串"));
    }
    if plan_revision_digest_value.is_empty() {
        return Err(PlanHashError::new("planRevisionDigest 必须为非空字符串"));
    }
    if business_phase.is_empty() {
        return Err(PlanHashError::new("businessPhase 必须为非空字符串"));
    }

    // 域分离数组：["factory-barrier-v1", runId, planRevisionDigest, businessPhase, barrierOrdinal]。
    let domain_array = CanonValue::Array(vec![
        CanonValue::Str(BARRIER_DOMAIN.to_string()),
        CanonValue::Str(run_id.to_string()),
        CanonValue::Str(plan_revision_digest_value.to_string()),
        CanonValue::Str(business_phase.to_string()),
        CanonValue::Int(barrier_ordinal),
    ]);
    let bytes = canonicalize(&domain_array)?;
    let mut hasher = Sha256::new();
    hasher.update(&bytes);
    Ok(format!("bar_{:x}", hasher.finalize()))
}

#[cfg(test)]
mod validator_tests {
    use super::*;
    use crate::generated::{
        PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256, PLAN_REVISION_SCHEMA_JSON,
        PLAN_REVISION_SCHEMA_JSON_SHA256, RUN_SPEC_SCHEMA_JSON_SHA256,
    };
    use serde_json::json;

    #[test]
    fn external_schema_references_cannot_build_validators() {
        // DenyExternalReferences 与前置 schema 遍历必须同时覆盖网络和本地文件 URI。
        for reference in [
            "https://example.invalid/schema.json",
            "file:///D:/outside/schema.json",
        ] {
            let schema = json!({"$schema": DRAFT7_SCHEMA_URI, "$ref": reference});
            assert!(compile_validator_value(&schema).is_err(), "{reference}");
        }

        // 损坏的 generated JSON 常量只能归一化为计划哈希错误，不能泄露 serde/jsonschema 正文。
        let corrupt = compile_validator_schema("{", RUN_SPEC_SCHEMA_JSON_SHA256);
        assert!(matches!(
            corrupt,
            Err(error)
                if error.detail == STABLE_VALIDATION_DETAIL
                    && error.to_string() == "[plan-hash-error] INVALID_PLAN_HASH_INPUT"
        ));

        // 三份 embedded JSON 的重复约束与单字节合法 JSON 变异都必须在解析前被内容 SHA 拒绝。
        for (schema_json, expected_sha256) in [
            (RUN_SPEC_SCHEMA_JSON, RUN_SPEC_SCHEMA_JSON_SHA256),
            (PLAN_REVISION_SCHEMA_JSON, PLAN_REVISION_SCHEMA_JSON_SHA256),
            (
                PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
                PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
            ),
        ] {
            for corrupted in [
                format!(
                    "{},\"required\":[]}}",
                    &schema_json[..schema_json.len() - 1]
                ),
                schema_json.replacen("\"title\":\"", "\"title\":\"X", 1),
            ] {
                assert!(compile_validator_schema(&corrupted, expected_sha256).is_err());
            }
        }
    }
}
