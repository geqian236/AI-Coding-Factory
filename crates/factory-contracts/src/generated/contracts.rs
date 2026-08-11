//! 此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。
//! 源 schema: contracts/schemas/*.schema.json
//! 算法版本: v1

#![allow(dead_code)]
#![allow(unused_imports)]

// 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。
pub const RUN_SPEC_SCHEMA_SOURCE_SHA256: &str =
    "sha256:185d4067779d4830d03b861f2e6b61f512675aa32e02bc54326351a79d99087e";
pub const RUN_SPEC_SCHEMA_JSON: &str = r#"{"$id":"https://factory.local/contracts/schemas/run-spec.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"规划层生成的不可变、版本化 RunSpec：完整语义文档（Master Spec §8）。semanticPlanHash 从本文档的语义字段投影派生；写入后不可修改，修复只能新建子版本。","properties":{"acceptanceCriteria":{"description":"验收条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"assumptions":{"description":"规划假设列表","items":{"minLength":1,"type":"string"},"type":"array"},"constraints":{"description":"约束条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"createdAt":{"description":"RunSpec 创建时间","format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"goal":{"description":"用户目标的结构化表达","minLength":1,"type":"string"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node-capability-map 版本标识","minLength":1,"type":"string"},"parentRevisionId":{"description":"父 PlanRevision ID；genesis 修订为 null","type":["string","null"]},"planRevisionDigest":{"description":"完整不可变修订记录摘要（写入 PlanRevision 后填入）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"repository":{"additionalProperties":false,"description":"仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）","properties":{"baseBranch":{"description":"基础分支名","minLength":1,"type":"string"},"baseCommit":{"description":"bootstrap 完成后可信 base commit 的 40 位小写完整 SHA","pattern":"^[0-9a-f]{40}$","type":"string"},"mode":{"description":"仓库模式","enum":["existing","new"],"type":"string"},"root":{"description":"规范化后的 D 盘仓库根路径","minLength":1,"type":"string"}},"required":["mode","root","baseBranch","baseCommit"],"type":"object"},"riskProfile":{"additionalProperties":false,"description":"风险画像（§8：level/reasons）","properties":{"level":{"description":"风险等级","enum":["low","medium","high","critical"],"type":"string"},"reasons":{"description":"风险原因列表","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["level","reasons"],"type":"object"},"schemaVersion":{"description":"RunSpec schema 版本号","minimum":1,"type":"integer"},"scope":{"additionalProperties":false,"description":"范围声明（§8：include/exclude）","properties":{"exclude":{"description":"排除范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"},"include":{"description":"纳入范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["include","exclude"],"type":"object"},"semanticPlanHash":{"description":"语义计划哈希（跨修订版本稳定，从语义字段投影派生）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"specRevision":{"description":"同一 taskId 下的规格修订序号","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage-capability-map 版本标识","minLength":1,"type":"string"},"targetStage":{"description":"目标终点阶段（Master Spec §6.1 权威 6 阶段）","enum":["DESIGN_APPROVED","CODEX_APPROVED","PR_READY","MERGED","STAGING_ACCEPTED","PRODUCTION_ACCEPTED"],"type":"string"},"taskId":{"description":"关联任务 ID","minLength":1,"type":"string"},"workPlan":{"additionalProperties":false,"description":"工作计划 DAG（§8：dagVersion/nodes/barriers）","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"minLength":1,"type":"string"},"passPredicateId":{"minLength":1,"type":"string"},"requiredNodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"},"settleTimeoutMs":{"minimum":1,"type":"integer"}},"required":["businessPhase","barrierOrdinal","requiredNodeIds","settleTimeoutMs","passPredicateId"],"type":"object"},"type":"array"},"dagVersion":{"description":"DAG 结构版本","minimum":1,"type":"integer"},"nodes":{"description":"DAG 工作节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"minLength":1,"type":"string"},"dependsOn":{"items":{"minLength":1,"type":"string"},"type":"array"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"required":{"type":"boolean"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"},"retryPolicyId":{"minLength":1,"type":"string"},"sideEffectClass":{"minLength":1,"type":"string"},"successPredicateId":{"minLength":1,"type":"string"},"timeoutMs":{"minimum":1,"type":"integer"}},"required":["logicalNodeId","businessPhase","barrierOrdinal","nodeType","required","dependsOn","sideEffectClass","requiredArtifacts","successPredicateId","timeoutMs","retryPolicyId"],"type":"object"},"type":"array"}},"required":["dagVersion","nodes","barriers"],"type":"object"}},"required":["schemaVersion","taskId","goal","assumptions","scope","constraints","acceptanceCriteria","targetStage","repository","workPlan","riskProfile","nodeCapabilityMapVersion","stageCapabilityMapVersion","intentAuthorizationId","semanticPlanHash"],"title":"RunSpec","type":"object"}"#;
pub const RUN_SPEC_SCHEMA_JSON_SHA256: &str =
    "sha256:f5f68bff70e3f688f08b33f682533cde9616fdcc19d88090409f112b8fef16a3";
pub const PLAN_REVISION_SCHEMA_SOURCE_SHA256: &str =
    "sha256:1095c85161c56edaf4fda8e344b0d813106b695eacfaf517ce698134a1a4d0cc";
pub const PLAN_REVISION_SCHEMA_JSON: &str = r#"{"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"minLength":1,"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"minLength":1,"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionDigest":{"description":"本修订版本内容摘要","minLength":1,"type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"description":"语义计划哈希，跨修订版本稳定","minLength":1,"type":"string"},"signature":{"minLength":1,"type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","planRevisionDigest","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}"#;
pub const PLAN_REVISION_SCHEMA_JSON_SHA256: &str =
    "sha256:2491645a45eae4fd3ba6272c887213056a16a75832129078df51f81fbf327c3f";
// 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。
pub const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON: &str = r#"{"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"minLength":1,"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"minLength":1,"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"description":"语义计划哈希，跨修订版本稳定","minLength":1,"type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}"#;
pub const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256: &str =
    "sha256:748d62a28b85afcfc01d42a028833a7c2f0bfdd88979eb1f33071dccaea35c48";

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct CredentialRef {
    /// 凭据唯一 ID（不含明文值）
    #[serde(rename = "credentialId")]
    pub credential_id: String,
    /// 凭据类型
    #[serde(rename = "credentialType")]
    pub credential_type: String,
    /// 凭据所属 D-backed 隔离 profile 作用域
    #[serde(rename = "profileScope")]
    pub profile_scope: String,
    /// 绑定凭据的资源指纹（如 host-key、repository ID）
    #[serde(rename = "resourceFingerprint")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub resource_fingerprint: Option<String>,
    /// 凭据绝对过期时间（ISO 8601）
    #[serde(rename = "expiresAt")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct RunSpec {
    /// RunSpec schema 版本号
    #[serde(rename = "schemaVersion")]
    pub schema_version: i64,
    /// 同一 taskId 下的规格修订序号
    #[serde(rename = "specRevision")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub spec_revision: Option<i64>,
    /// 父 PlanRevision ID；genesis 修订为 null
    #[serde(rename = "parentRevisionId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_revision_id: Option<String>,
    /// 关联任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 用户目标的结构化表达
    pub goal: String,
    /// 规划假设列表
    pub assumptions: Vec<String>,
    /// 范围声明（§8：include/exclude）
    pub scope: serde_json::Value,
    /// 约束条件列表
    pub constraints: Vec<String>,
    /// 验收条件列表
    #[serde(rename = "acceptanceCriteria")]
    pub acceptance_criteria: Vec<String>,
    /// 目标终点阶段（Master Spec §6.1 权威 6 阶段）
    #[serde(rename = "targetStage")]
    pub target_stage: String,
    /// 仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）
    pub repository: serde_json::Value,
    /// 工作计划 DAG（§8：dagVersion/nodes/barriers）
    #[serde(rename = "workPlan")]
    pub work_plan: serde_json::Value,
    /// 风险画像（§8：level/reasons）
    #[serde(rename = "riskProfile")]
    pub risk_profile: serde_json::Value,
    /// node-capability-map 版本标识
    #[serde(rename = "nodeCapabilityMapVersion")]
    pub node_capability_map_version: String,
    /// stage-capability-map 版本标识
    #[serde(rename = "stageCapabilityMapVersion")]
    pub stage_capability_map_version: String,
    /// 关联 IntentAuthorization ID
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    /// 语义计划哈希（跨修订版本稳定，从语义字段投影派生）
    #[serde(rename = "semanticPlanHash")]
    pub semantic_plan_hash: String,
    /// 完整不可变修订记录摘要（写入 PlanRevision 后填入）
    #[serde(rename = "planRevisionDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub plan_revision_digest: Option<String>,
    /// RunSpec 创建时间
    #[serde(rename = "createdAt")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub created_at: Option<String>,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct PlanRevision {
    /// 本修订版本唯一 ID
    #[serde(rename = "planRevisionId")]
    pub plan_revision_id: String,
    /// 父修订版本 ID（范围内重规划时设置）
    #[serde(rename = "parentRevisionId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_revision_id: Option<String>,
    /// 所属任务 ID（§11 plan_revisions.task_id）
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 规格修订号，单调递增（§11 plan_revisions.spec_revision）
    #[serde(rename = "specRevision")]
    pub spec_revision: i64,
    /// 关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    /// DAG 版本号（§11 plan_revisions.dag_version）
    #[serde(rename = "dagVersion")]
    pub dag_version: i64,
    /// node→capability 映射版本（§11 plan_revisions.node_capability_map_version）
    #[serde(rename = "nodeCapabilityMapVersion")]
    pub node_capability_map_version: String,
    /// stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）
    #[serde(rename = "stageCapabilityMapVersion")]
    pub stage_capability_map_version: String,
    /// 语义计划哈希，跨修订版本稳定
    #[serde(rename = "semanticPlanHash")]
    pub semantic_plan_hash: String,
    /// 本修订版本内容摘要
    #[serde(rename = "planRevisionDigest")]
    pub plan_revision_digest: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub signature: Option<String>,
    /// DAG 节点列表
    pub nodes: Vec<serde_json::Value>,
    /// 阶段屏障列表
    pub barriers: Vec<serde_json::Value>,
    /// target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）
    #[serde(rename = "stageMaps")]
    pub stage_maps: serde_json::Value,
    #[serde(rename = "createdAt")]
    pub created_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct IntentAuthorization {
    /// IntentAuthorization 的稳定身份。
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    /// 被授权任务的稳定身份。
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 做出一次性授权的用户身份。
    #[serde(rename = "userId")]
    pub user_id: String,
    /// 规范化用户需求的闭合 JCS/NFC 快照引用。
    #[serde(rename = "requirementDigest")]
    pub requirement_digest: serde_json::Value,
    /// 项目身份，禁止跨项目复用授权。
    #[serde(rename = "projectId")]
    pub project_id: String,
    /// 规范化后的仓库身份。
    #[serde(rename = "repositoryId")]
    pub repository_id: String,
    /// 可解析的仓库/物理路径/模式绑定快照引用。
    #[serde(rename = "repositoryBindingDigest")]
    pub repository_binding_digest: serde_json::Value,
    /// 可解析的基础分支、base SHA 或 bootstrap 前置事实快照引用。
    #[serde(rename = "baselineDigest")]
    pub baseline_digest: serde_json::Value,
    /// 用户选择的目标终点阶段；只给出 capability 上限。
    #[serde(rename = "targetStage")]
    pub target_stage: String,
    /// 派生授权时使用的 stage-capability-map 版本。
    #[serde(rename = "stageCapabilityMapVersion")]
    pub stage_capability_map_version: String,
    /// targetStage 展开后的排序 capability 集合快照引用。
    #[serde(rename = "allowedCapabilitySetDigest")]
    pub allowed_capability_set_digest: serde_json::Value,
    /// 可解析的环境、ServerProfile、数据库和资源目标绑定快照引用。
    #[serde(rename = "targetBindingDigest")]
    pub target_binding_digest: serde_json::Value,
    /// 用户接受的最高风险等级。
    #[serde(rename = "riskCeiling")]
    pub risk_ceiling: String,
    /// 可解析的估算成本告警策略快照引用；不把订阅 CLI 估算伪装成真实账单。
    #[serde(rename = "estimatedCostAlertDigest")]
    pub estimated_cost_alert_digest: serde_json::Value,
    /// 端到端自主执行预算（毫秒）；禁止无限值。
    #[serde(rename = "autonomousExecutionBudgetMs")]
    pub autonomous_execution_budget_ms: i64,
    /// 自动修复轮数上限。
    #[serde(rename = "repairLoopLimit")]
    pub repair_loop_limit: i64,
    /// 自动重规划次数上限。
    #[serde(rename = "autoReplanLimit")]
    pub auto_replan_limit: i64,
    /// Attempt 总数上限。
    #[serde(rename = "attemptLimit")]
    pub attempt_limit: i64,
    /// 授权签发时间。
    #[serde(rename = "issuedAt")]
    pub issued_at: String,
    /// 绝对有效期；暂停不会延长它。
    #[serde(rename = "expiresAt")]
    pub expires_at: String,
    /// 撤销时间；未撤销时显式为 null。
    #[serde(rename = "revokedAt")]
    pub revoked_at: Option<String>,
    /// 撤销原因；未撤销时显式为 null。
    #[serde(rename = "revokeReason")]
    pub revoke_reason: Option<String>,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct ExecutionAuthorization {
    /// 派生授权稳定身份。
    #[serde(rename = "executionAuthorizationId")]
    pub execution_authorization_id: String,
    /// 来源 IntentAuthorization 身份。
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    /// 绑定的不可变 PlanRevision 身份。
    #[serde(rename = "planRevisionId")]
    pub plan_revision_id: String,
    /// 计划语义的闭合快照引用。
    #[serde(rename = "semanticPlanHash")]
    pub semantic_plan_hash: serde_json::Value,
    /// 完整 PlanRevision 的闭合快照引用。
    #[serde(rename = "planRevisionDigest")]
    pub plan_revision_digest: serde_json::Value,
    /// stage capability map 版本。
    #[serde(rename = "stageCapabilityMapVersion")]
    pub stage_capability_map_version: String,
    /// stage capability map 的闭合快照引用。
    #[serde(rename = "stageCapabilityMapDigest")]
    pub stage_capability_map_digest: serde_json::Value,
    /// node capability map 版本。
    #[serde(rename = "nodeCapabilityMapVersion")]
    pub node_capability_map_version: String,
    /// node capability map raw-file 内容的闭合快照引用。
    #[serde(rename = "nodeCapabilityMapDigest")]
    pub node_capability_map_digest: serde_json::Value,
    /// 绑定的 Run 身份。
    #[serde(rename = "runId")]
    pub run_id: String,
    /// 绑定的 Step 身份。
    #[serde(rename = "stepId")]
    pub step_id: String,
    /// 绑定的不可复用 Attempt 身份。
    #[serde(rename = "attemptId")]
    pub attempt_id: String,
    /// 冻结 node-capability-map 的节点类型。
    #[serde(rename = "nodeType")]
    pub node_type: String,
    /// 当前 lease owner 的执行器身份。
    #[serde(rename = "executorId")]
    pub executor_id: String,
    /// 资源指纹的闭合快照引用；其 payload 必须按 nodeType 使用 node map 内嵌 resourceFingerprintSchema 校验。
    #[serde(rename = "resourceFingerprint")]
    pub resource_fingerprint: serde_json::Value,
    /// 可解析 capability/resource scope 的闭合快照引用。
    #[serde(rename = "capabilityScopeDigest")]
    pub capability_scope_digest: serde_json::Value,
    /// 按 factory-action-v1 的 H=sha256(JCS/NFC(...)) 计算的稳定 action key 快照引用。
    #[serde(rename = "idempotencyKey")]
    pub idempotency_key: serde_json::Value,
    /// 闭合且无歧义的输入身份对象；同一种 SHA/digest 至多一个字段，禁止数组重复绑定或自由文本。
    #[serde(rename = "inputBindings")]
    pub input_bindings: serde_json::Value,
    /// 本 ExecutionAuthorization 唯一对应的 capability action。
    #[serde(rename = "actionCapability")]
    pub action_capability: String,
    /// selected action 的 key template、completion fact 与消费点确定性投影的闭合快照引用；payload 必须绑定 nodeCapabilityMapDigest、nodeType 和 actionCapability。
    #[serde(rename = "actionPolicySnapshotDigest")]
    pub action_policy_snapshot_digest: serde_json::Value,
    /// 严格单调 lease fencing token。
    #[serde(rename = "fencingToken")]
    pub fencing_token: i64,
    /// 控制权 epoch。
    #[serde(rename = "controlEpoch")]
    pub control_epoch: i64,
    /// 派生时接受的控制命令序号。
    #[serde(rename = "acceptedControlCommandSeq")]
    pub accepted_control_command_seq: i64,
    /// 剩余可消费次数；不会使用第二个 ttlMs 真源。
    #[serde(rename = "maxUses")]
    pub max_uses: i64,
    /// 仅表示消费投影；撤销/过期分别由 revokedAt/expiresAt 表达，未知状态不能视为成功。
    #[serde(rename = "consumptionState")]
    pub consumption_state: String,
    /// 派生授权签发时间。
    #[serde(rename = "issuedAt")]
    pub issued_at: String,
    /// 派生授权绝对 TTL。
    #[serde(rename = "expiresAt")]
    pub expires_at: String,
    /// 撤销时间；未撤销时显式为 null。
    #[serde(rename = "revokedAt")]
    pub revoked_at: Option<String>,
    /// 撤销原因；未撤销时显式为 null。
    #[serde(rename = "revokeReason")]
    pub revoke_reason: Option<String>,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct ControlCommand {
    /// 命令唯一 ID
    #[serde(rename = "commandId")]
    pub command_id: String,
    /// 命令类型：软暂停、立即停止、取消（不可逆）、恢复
    #[serde(rename = "commandType")]
    pub command_type: String,
    /// 目标任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 目标 Run ID（可选，指定具体 Run）
    #[serde(rename = "runId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    /// 命令签发时间
    #[serde(rename = "issuedAt")]
    pub issued_at: String,
    /// 控制 epoch，用于防重放和顺序保证
    #[serde(rename = "controlEpoch")]
    pub control_epoch: i64,
    /// 命令签发者标识
    #[serde(rename = "issuedBy")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub issued_by: Option<String>,
    /// 命令原因说明（可选）
    #[serde(skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct TaskIntakeRequest {
    /// 用户自然语言需求描述
    #[serde(rename = "requirementText")]
    pub requirement_text: String,
    /// 用户选择的目标终点阶段（Master Spec §6.1 权威 target_stage 分类）
    #[serde(rename = "targetStage")]
    pub target_stage: String,
    #[serde(rename = "repositorySelection")]
    pub repository_selection: serde_json::Value,
    #[serde(rename = "environmentSelection")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub environment_selection: Option<serde_json::Value>,
    /// 用户设定的自主执行预算上限（毫秒）
    #[serde(rename = "budgetCapMs")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub budget_cap_ms: Option<i64>,
    /// 用户已确认风险
    #[serde(rename = "userRiskAcknowledged")]
    pub user_risk_acknowledged: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct TaskIntakeAccepted {
    /// Agent 生成的任务唯一 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// IntentAuthorization 唯一 ID
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    #[serde(rename = "normalizedRepositoryBinding")]
    pub normalized_repository_binding: serde_json::Value,
    /// 服务端求交后的允许 capability set 摘要
    #[serde(rename = "allowedCapabilitySetDigest")]
    pub allowed_capability_set_digest: String,
    /// 有效自主执行预算（毫秒）
    #[serde(rename = "effectiveBudgetMs")]
    pub effective_budget_ms: i64,
    /// 风险摘要内容的摘要（不含明文风险详情）
    #[serde(rename = "riskSummaryDigest")]
    pub risk_summary_digest: String,
    /// 授权绝对过期时间
    #[serde(rename = "expiresAt")]
    pub expires_at: String,
    /// 接受时间
    #[serde(rename = "acceptedAt")]
    pub accepted_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct ClaudeSelfReview {
    /// 自审回执唯一 ID
    #[serde(rename = "selfReviewId")]
    pub self_review_id: String,
    /// 关联任务 ID
    #[serde(rename = "taskId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub task_id: Option<String>,
    /// 关联 Step ID
    #[serde(rename = "stepId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub step_id: Option<String>,
    /// 被审核的 base commit SHA；漂移后本回执失效
    #[serde(rename = "reviewedBaseSha")]
    pub reviewed_base_sha: String,
    /// 被审核的候选 commit SHA；漂移后本回执失效
    #[serde(rename = "reviewedCandidateSha")]
    pub reviewed_candidate_sha: String,
    /// 所用审核规则集版本
    #[serde(rename = "rubricVersion")]
    pub rubric_version: String,
    /// 自审结论：pass 表示通过，needs_fix 表示需要修复
    pub result: String,
    /// 发现问题列表（result=pass 时可为空数组）
    pub findings: Vec<serde_json::Value>,
    /// 证据摘要列表（每个证据的内容寻址摘要）
    #[serde(rename = "evidenceDigests")]
    pub evidence_digests: Vec<String>,
    /// 自审完成时间
    #[serde(rename = "createdAt")]
    pub created_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct PreparedEventV2 {
    /// Adapter 分配的摄取 ID，用于幂等去重
    #[serde(rename = "ingestEventId")]
    pub ingest_event_id: String,
    /// 所属任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 事件类型
    #[serde(rename = "eventType")]
    pub event_type: String,
    /// 来源端序号（用于顺序校验和去重）
    #[serde(rename = "sourceSeq")]
    pub source_seq: i64,
    /// 传输 span 摘要（用于 STREAM-002 完整性检查）
    #[serde(rename = "transportSpanDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub transport_span_digest: Option<String>,
    /// 事件负载（已脱敏）
    pub payload: serde_json::Value,
    /// 负载内容摘要（SHA-256 hex）
    #[serde(rename = "payloadDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload_digest: Option<String>,
    /// 预备时间
    #[serde(rename = "preparedAt")]
    pub prepared_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct PreparedBatchV2 {
    /// 批次唯一稳定 ID。
    #[serde(rename = "preparedBatchId")]
    pub prepared_batch_id: String,
    /// claim 时的 writer epoch（用于竞争检测）。
    #[serde(rename = "writerEpoch")]
    pub writer_epoch: i64,
    /// 批次状态机（§11 ingest_batches）。正常转换仅 CLAIMED → PREPARING → PREPARED → COMMITTED；旧 epoch pre-commit 状态只能由 Reconciler CAS 为 ABANDONED。COMMITTED/ABANDONED 均为终态。
    pub state: String,
    /// 按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。
    #[serde(rename = "orderedIngestIds")]
    pub ordered_ingest_ids: Vec<String>,
    /// 每个 Task 唯一的 expectedCommittedHead 锚点 + 批内 ordinal 范围（§11 ingest_batch_task_heads）。
    #[serde(rename = "perTaskExpectedHeads")]
    pub per_task_expected_heads: Vec<serde_json::Value>,
    /// segment digest 列表。
    #[serde(rename = "segmentDigests")]
    pub segment_digests: Vec<serde_json::Value>,
    /// 总事件数。
    #[serde(rename = "eventCount")]
    pub event_count: i64,
    /// 总 payload 字节数。
    #[serde(rename = "payloadBytes")]
    pub payload_bytes: i64,
    /// 本批首事件 batchOrdinal。
    #[serde(rename = "firstBatchOrdinal")]
    pub first_batch_ordinal: i64,
    /// 本批末事件 batchOrdinal。
    #[serde(rename = "lastBatchOrdinal")]
    pub last_batch_ordinal: i64,
    /// 本批最旧 ingestedAt。
    #[serde(rename = "oldestIngestedAt")]
    pub oldest_ingested_at: String,
    /// 本批 prepared 完成时间。
    #[serde(rename = "preparedAt")]
    pub prepared_at: String,
    /// PreparedBatch schema 版本。
    #[serde(rename = "schemaVersion")]
    pub schema_version: String,
    /// coordinator 版本（compatibility manifest 绑定）。
    #[serde(rename = "coordinatorVersion")]
    pub coordinator_version: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct DurableEventV2 {
    /// 事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。
    #[serde(rename = "schemaVersion")]
    pub schema_version: String,
    /// 耐久化等级（§10.4）：authoritative_state=内部权威 state.changed/lease/授权；side_effect_receipt=外部副作用 receipt；provider_source=Provider semantic/public frame；derived=UI summary/指标等可重建派生。
    #[serde(rename = "durabilityClass")]
    pub durability_class: String,
    /// 物化器分配的全局唯一事件 ID（evt_<64 位小写十六进制>，SHA-256(JCS([factory-event-id-v2, ingestEventId]))）。崩溃重试得到同一身份。
    #[serde(rename = "eventId")]
    pub event_id: String,
    /// 所属任务 ID。
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 任务内单调递增序号（由物化器分配，CAS 持久）。UI cursor 用 (taskId, taskSeq)。
    #[serde(rename = "taskSeq")]
    pub task_seq: i64,
    /// 所属 Run ID。
    #[serde(rename = "runId")]
    pub run_id: String,
    /// 单 Run 内诊断序号（不能跨 Run 唯一）。
    #[serde(rename = "runSeq")]
    pub run_seq: i64,
    /// 所属 Step ID。
    #[serde(rename = "stepId")]
    pub step_id: String,
    /// 所属 Attempt ID。
    #[serde(rename = "attemptId")]
    pub attempt_id: String,
    /// 事件来源 actor 身份。
    pub source: String,
    /// 原始摄取 ID（来自 PreparedEventV2）。
    #[serde(rename = "ingestEventId")]
    pub ingest_event_id: String,
    /// 具体事件类型（§10.1）。model.summary 必带 summaryOrigin=provider_public + providerEventId + sanitizedProviderFrameRef + sanitizedProviderFrameDigest。
    #[serde(rename = "eventType")]
    pub event_type: String,
    /// Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。
    #[serde(rename = "providerEventId")]
    pub provider_event_id: Option<String>,
    /// Provider 来源端序号（用于去重与对齐）。
    #[serde(rename = "sourceSeq")]
    pub source_seq: i64,
    /// Provider stream 稳定 ID（stream.* 事件必填；非 stream 事件 null）。
    #[serde(rename = "streamId")]
    pub stream_id: Option<String>,
    /// 所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。
    #[serde(rename = "preparedBatchId")]
    pub prepared_batch_id: String,
    /// 所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。
    #[serde(rename = "batchOrdinal")]
    pub batch_ordinal: i64,
    /// 源传输 span：内存中以原始传输字节精确计数。credential/secret/PII 仅存 providerEventId/sourceSeq 与诚实的 mappingPrecision（byte|field|frame|none），精确字节起止不能进普通事件。
    #[serde(rename = "sourceTransportSpan")]
    pub source_transport_span: serde_json::Value,
    /// 脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。
    #[serde(rename = "sanitizedStreamSpan")]
    pub sanitized_stream_span: serde_json::Value,
    /// RFC3339 接收 wall time（仅用于显示，不能参与排序）。
    #[serde(rename = "wallTime")]
    pub wall_time: String,
    /// 单调时钟纳秒。
    #[serde(rename = "monotonicTimeNs")]
    pub monotonic_time_ns: i64,
    /// 完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。
    #[serde(rename = "ingestedAt")]
    pub ingested_at: String,
    /// Provider CLI 上报版本。
    #[serde(rename = "providerVersion")]
    pub provider_version: String,
    /// Adapter 自身 semver 版本。
    #[serde(rename = "adapterVersion")]
    pub adapter_version: String,
    /// 受管进程身份（§10.1）：用于 RUNNING 真实性校验（PID/Job Object/WSL/container exec）。
    #[serde(rename = "processIdentity")]
    pub process_identity: serde_json::Value,
    /// 事件负载（已脱敏，未脱敏原文不得落盘）。
    pub payload: serde_json::Value,
    /// 已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃。model.summary 必填，其它 null。
    #[serde(rename = "sanitizedProviderFrameDigest")]
    pub sanitized_provider_frame_digest: Option<String>,
    /// 脱敏后 payload 的 JCS 摘要。
    #[serde(rename = "payloadDigest")]
    pub payload_digest: String,
    /// 前驱事件 eventDigest（genesis 用 sha256:<64 个 0> 固定 predecessor）。
    #[serde(rename = "previousEventDigest")]
    pub previous_event_digest: String,
    /// 本事件 SHA-256(JCS(去除 eventDigest 字段后完整对象))。previousEventDigest 仍参与计算。
    #[serde(rename = "eventDigest")]
    pub event_digest: String,
    /// 本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。
    pub redactions: Vec<serde_json::Value>,
    /// redactions 列表内容的 SHA-256 摘要。
    #[serde(rename = "redactionManifestDigest")]
    pub redaction_manifest_digest: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct IpcEnvelope {
    /// 信封唯一 ID（防重放）
    #[serde(rename = "envelopeId")]
    pub envelope_id: String,
    /// 消息类型
    #[serde(rename = "messageType")]
    pub message_type: String,
    /// 发送方进程身份（含 SID）
    #[serde(rename = "senderId")]
    pub sender_id: String,
    /// 接收方进程身份
    #[serde(rename = "recipientId")]
    pub recipient_id: String,
    /// 随机 nonce，防重放攻击
    pub nonce: String,
    /// 关联 ID，用于请求-响应匹配
    #[serde(rename = "correlationId")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
    /// 消息负载（类型由 messageType 决定）
    pub payload: serde_json::Value,
    /// 负载内容摘要，用于完整性验证
    #[serde(rename = "payloadDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload_digest: Option<String>,
    /// 发送时间
    #[serde(rename = "sentAt")]
    pub sent_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct RunnerProtocol {
    /// 协议消息唯一 ID
    #[serde(rename = "messageId")]
    pub message_id: String,
    /// 协议消息类型
    #[serde(rename = "protocolMessageType")]
    pub protocol_message_type: String,
    /// Attempt 唯一 ID（每次派发不可复用）
    #[serde(rename = "attemptId")]
    pub attempt_id: String,
    /// 执行者唯一标识
    #[serde(rename = "executorId")]
    pub executor_id: String,
    /// Fencing token，严格单调递增
    #[serde(rename = "fencingToken")]
    pub fencing_token: i64,
    /// 控制 epoch
    #[serde(rename = "controlEpoch")]
    pub control_epoch: i64,
    /// 进程启动身份（containerId/execId/PID等）
    #[serde(rename = "processStartIdentity")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub process_start_identity: Option<serde_json::Value>,
    /// 消息负载（按 protocolMessageType 定义结构）
    pub payload: serde_json::Value,
    /// 消息时间戳
    pub timestamp: String,
}
