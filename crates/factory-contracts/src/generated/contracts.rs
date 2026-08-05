//! 此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。
//! 源 schema: contracts/schemas/*.schema.json
//! 算法版本: v1

#![allow(dead_code)]
#![allow(unused_imports)]

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
    /// RunSpec 唯一 ID
    #[serde(rename = "runSpecId")]
    pub run_spec_id: String,
    /// 关联任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 语义计划哈希（跨 revision 稳定）
    #[serde(rename = "semanticPlanHash")]
    pub semantic_plan_hash: String,
    /// 目标终点阶段（Master Spec §6.1 权威 target_stage 集合）
    #[serde(rename = "targetStage")]
    pub target_stage: String,
    #[serde(rename = "repositoryBinding")]
    pub repository_binding: serde_json::Value,
    /// IntentAuthorization 唯一 ID
    #[serde(rename = "intentAuthorizationId")]
    pub intent_authorization_id: String,
    /// 服务端求交后的 capability set 摘要
    #[serde(rename = "allowedCapabilitySetDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub allowed_capability_set_digest: Option<String>,
    /// 自主执行预算（毫秒）
    #[serde(rename = "budgetMs")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub budget_ms: Option<i64>,
    /// 授权绝对过期时间
    #[serde(rename = "expiresAt")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub expires_at: Option<String>,
    /// RunSpec 创建时间
    #[serde(rename = "createdAt")]
    pub created_at: String,
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
    /// 语义计划哈希，跨修订版本稳定
    #[serde(rename = "semanticPlanHash")]
    pub semantic_plan_hash: String,
    /// 本修订版本内容摘要
    #[serde(rename = "planRevisionDigest")]
    pub plan_revision_digest: String,
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
    /// 批次唯一 ID
    #[serde(rename = "batchId")]
    pub batch_id: String,
    /// 所属任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 批次序号，用于稳定 identity 生成
    #[serde(rename = "batchOrdinal")]
    pub batch_ordinal: i64,
    /// 批次内预备事件列表
    pub events: Vec<serde_json::Value>,
    /// 前驱批次 head 摘要（genesis 批次为 null）
    #[serde(rename = "previousHead")]
    pub previous_head: Option<String>,
    /// claim 时的 writer epoch，用于竞争检测
    #[serde(rename = "claimEpoch")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub claim_epoch: Option<i64>,
    /// 批次预备时间
    #[serde(rename = "preparedAt")]
    pub prepared_at: String,
}

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct DurableEventV2 {
    /// 物化器分配的全局唯一事件 ID
    #[serde(rename = "eventId")]
    pub event_id: String,
    /// 所属任务 ID
    #[serde(rename = "taskId")]
    pub task_id: String,
    /// 任务内单调递增序号（由物化器分配）
    #[serde(rename = "taskSeq")]
    pub task_seq: i64,
    /// 所属批次序号
    #[serde(rename = "batchOrdinal")]
    pub batch_ordinal: i64,
    /// 事件内容摘要（SHA-256 hex）
    #[serde(rename = "eventDigest")]
    pub event_digest: String,
    /// 本批次物化后的 head 摘要（链式验证用）
    pub head: String,
    /// 前驱批次 head 摘要（genesis 为 null）
    #[serde(rename = "previousHead")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub previous_head: Option<String>,
    /// 原始摄取 ID（来自 PreparedEventV2）
    #[serde(rename = "ingestEventId")]
    pub ingest_event_id: String,
    /// 事件类型
    #[serde(rename = "eventType")]
    pub event_type: String,
    /// 来源端序号
    #[serde(rename = "sourceSeq")]
    pub source_seq: i64,
    /// 事件负载（已脱敏）
    pub payload: serde_json::Value,
    /// 负载内容摘要
    #[serde(rename = "payloadDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub payload_digest: Option<String>,
    /// 耐久化完成时间
    #[serde(rename = "durableAt")]
    pub durable_at: String,
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

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct TestReceipt {
    /// 回执唯一 ID
    #[serde(rename = "receiptId")]
    pub receipt_id: String,
    /// 测试 ID（来自 required-test-catalog.v1.json）
    #[serde(rename = "testId")]
    pub test_id: String,
    /// 环境 Compatibility Manifest 摘要
    #[serde(rename = "environmentManifestDigest")]
    pub environment_manifest_digest: String,
    /// 执行动作列表
    pub actions: Vec<serde_json::Value>,
    /// 期望结果描述（机器可读）
    pub expected: serde_json::Value,
    /// 实际观察结果（机器可读）
    pub actual: serde_json::Value,
    /// 外部副作用计数
    #[serde(rename = "sideEffectCount")]
    pub side_effect_count: i64,
    /// 相关 Artifact 摘要列表
    #[serde(rename = "artifactDigests")]
    pub artifact_digests: Vec<String>,
    /// 测试结论（不能通过手工文字改为 PASS）
    pub result: String,
    /// 失败原因（result=FAIL 时必填）
    #[serde(rename = "failureReason")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub failure_reason: Option<String>,
    /// 场景合同内容摘要（来自 required-test-catalog）
    #[serde(rename = "scenarioContractDigest")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub scenario_contract_digest: Option<String>,
    /// 实现贡献者列表
    #[serde(rename = "implementationContributors")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub implementation_contributors: Option<Vec<String>>,
    /// 最终通过责任人
    #[serde(rename = "finalPassOwner")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub final_pass_owner: Option<String>,
    /// 要求重放次数
    #[serde(rename = "requiredReplays")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub required_replays: Option<i64>,
    /// 回执创建时间
    #[serde(rename = "createdAt")]
    pub created_at: String,
}
