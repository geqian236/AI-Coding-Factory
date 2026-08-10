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
use serde_json::Value;
use sha2::{Digest, Sha256};
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
        Self { detail: detail.into() }
    }
}

impl fmt::Display for PlanHashError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "[{}] {}", Self::ERROR_CODE, self.detail)
    }
}

impl std::error::Error for PlanHashError {}

impl From<CanonicalJsonError> for PlanHashError {
    /// 规范化错误上抛为计划哈希错误，保留明细。
    fn from(e: CanonicalJsonError) -> Self {
        PlanHashError::new(format!("canonicalize 失败：{}", e.detail))
    }
}

/// 校验 value 为对象，否则 fail closed。
fn require_object<'a>(value: Option<&'a Value>, label: &str) -> Result<&'a serde_json::Map<String, Value>, PlanHashError> {
    match value {
        Some(Value::Object(m)) => Ok(m),
        _ => Err(PlanHashError::new(format!("字段 '{label}' 必须为对象"))),
    }
}

/// 从 source 提取 fields 指定的字段子集，缺字段即 fail closed。
fn project_subset(
    source: &serde_json::Map<String, Value>,
    fields: &[&str],
    label: &str,
) -> Result<Value, PlanHashError> {
    let mut projected = serde_json::Map::new();
    for name in fields {
        match source.get(*name) {
            Some(v) => {
                projected.insert((*name).to_string(), v.clone());
            }
            None => {
                return Err(PlanHashError::new(format!(
                    "{label} 缺少语义字段 '{name}'（{PLAN_HASH_VERSION}）"
                )));
            }
        }
    }
    Ok(Value::Object(projected))
}

/// 构造 semanticPlanHash 的字段投影（不含任何谱系/时间/ID/hash 字段）。
pub fn build_semantic_projection(plan: &Value) -> Result<Value, PlanHashError> {
    let obj = require_object(Some(plan), "plan")?;
    let mut projection = match project_subset(obj, &SEMANTIC_TOP_FIELDS, "plan")? {
        Value::Object(m) => m,
        _ => unreachable!(),
    };

    // repository：仅纳入 mode/root/baseBranch/baseCommit。
    let repository = require_object(obj.get("repository"), "repository")?;
    projection.insert(
        "repository".to_string(),
        project_subset(repository, &REPO_FIELDS, "repository")?,
    );

    // workPlan：仅纳入 dagVersion/nodes/barriers。
    let work_plan = require_object(obj.get("workPlan"), "workPlan")?;
    projection.insert(
        "workPlan".to_string(),
        project_subset(work_plan, &WORKPLAN_FIELDS, "workPlan")?,
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
    let obj = require_object(Some(revision), "revision")?;
    let mut material = serde_json::Map::new();
    for (k, v) in obj {
        if k != "planRevisionDigest" && k != "signature" {
            material.insert(k.clone(), v.clone());
        }
    }
    let bytes = canonicalize_value(&Value::Object(material))?;
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
