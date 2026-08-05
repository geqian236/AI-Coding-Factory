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
    /// 仓库绑定（§8：mode/root/baseBranch/baseCommit）
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

/// 由 generate.py 自动生成，禁止手动修改。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "camelCase")]
#[serde(deny_unknown_fields)]
pub struct TestReceipt {
    /// 回执唯一 ID（全局唯一，verify_receipts 强制 DUPLICATE_RECEIPT_ID 检查）
    #[serde(rename = "receiptId")]
    pub receipt_id: String,
    /// 测试 ID（来自 required-test-catalog.v1.json）
    #[serde(rename = "testId")]
    pub test_id: String,
    /// 环境 Compatibility Manifest 摘要
    #[serde(rename = "environmentManifestDigest")]
    pub environment_manifest_digest: String,
    /// 执行动作列表（P0-6：至少 1 条，杜绝手工文字 PASS）
    pub actions: Vec<serde_json::Value>,
    /// 期望结果描述（机器可读；P0-6：至少 1 个属性以防伪造 PASS）
    pub expected: serde_json::Value,
    /// 实际观察结果（机器可读；P0-6：至少 1 个属性以防伪造 PASS）
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
    /// 场景合同内容摘要（来自 required-test-catalog；P0-6：必填以防伪造 PASS）
    #[serde(rename = "scenarioContractDigest")]
    pub scenario_contract_digest: String,
    /// 实现贡献者列表
    #[serde(rename = "implementationContributors")]
    #[serde(skip_serializing_if = "Option::is_none")]
    pub implementation_contributors: Option<Vec<String>>,
    /// 最终通过责任人（P0-6：必填；verify_receipts 强制匹配 catalog.owner + 白名单）
    #[serde(rename = "finalPassOwner")]
    pub final_pass_owner: String,
    /// 执行资质标签（P0-6：必填，标识评审/实施/审计等角色资格，如 codex-reviewer / claude-implementer）
    pub qualification: String,
    /// 要求重放次数（P0-6：>= 1，verify_receipts 按 catalog.requiredReplays 强制覆盖门禁）
    #[serde(rename = "requiredReplays")]
    pub required_replays: i64,
    /// 回执创建时间
    #[serde(rename = "createdAt")]
    pub created_at: String,
}
