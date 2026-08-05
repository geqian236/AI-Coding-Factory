// 此文件由 contracts/codegen/generate.py 自动生成，禁止手动修改。
// 源 schema: contracts/schemas/*.schema.json
// 算法版本: v1

/* eslint-disable */
// @ts-nocheck

/** CredentialRef — 由 generate.py 自动生成，禁止手动修改 */
export interface CredentialRef {
  /** 凭据唯一 ID（不含明文值） */
  credentialId: string;
  /** 凭据类型 */
  credentialType: "SSH_KEY" | "REGISTRY_TOKEN" | "API_KEY" | "GITHUB_APP" | "FACTORY_PROFILE_TOKEN";
  /** 凭据所属 D-backed 隔离 profile 作用域 */
  profileScope: string;
  /** 绑定凭据的资源指纹（如 host-key、repository ID） */
  resourceFingerprint?: string;
  /** 凭据绝对过期时间（ISO 8601） */
  expiresAt?: string;
}

/** RunSpec — 由 generate.py 自动生成，禁止手动修改 */
export interface RunSpec {
  /** RunSpec schema 版本号 */
  schemaVersion: number;
  /** 同一 taskId 下的规格修订序号 */
  specRevision?: number;
  /** 父 PlanRevision ID；genesis 修订为 null */
  parentRevisionId?: string | null;
  /** 关联任务 ID */
  taskId: string;
  /** 用户目标的结构化表达 */
  goal: string;
  /** 规划假设列表 */
  assumptions: string[];
  /** 范围声明（§8：include/exclude） */
  scope: Record<string, unknown>;
  /** 约束条件列表 */
  constraints: string[];
  /** 验收条件列表 */
  acceptanceCriteria: string[];
  /** 目标终点阶段（Master Spec §6.1 权威 6 阶段） */
  targetStage: "DESIGN_APPROVED" | "CODEX_APPROVED" | "PR_READY" | "MERGED" | "STAGING_ACCEPTED" | "PRODUCTION_ACCEPTED";
  /** 仓库绑定（§8：mode/root/baseBranch/baseCommit） */
  repository: Record<string, unknown>;
  /** 工作计划 DAG（§8：dagVersion/nodes/barriers） */
  workPlan: Record<string, unknown>;
  /** 风险画像（§8：level/reasons） */
  riskProfile: Record<string, unknown>;
  /** node-capability-map 版本标识 */
  nodeCapabilityMapVersion: string;
  /** stage-capability-map 版本标识 */
  stageCapabilityMapVersion: string;
  /** 关联 IntentAuthorization ID */
  intentAuthorizationId: string;
  /** 语义计划哈希（跨修订版本稳定，从语义字段投影派生） */
  semanticPlanHash: string;
  /** 完整不可变修订记录摘要（写入 PlanRevision 后填入） */
  planRevisionDigest?: string;
  /** RunSpec 创建时间 */
  createdAt?: string;
}

/** PlanRevision — 由 generate.py 自动生成，禁止手动修改 */
export interface PlanRevision {
  /** 本修订版本唯一 ID */
  planRevisionId: string;
  /** 父修订版本 ID（范围内重规划时设置） */
  parentRevisionId?: string;
  /** 所属任务 ID（§11 plan_revisions.task_id） */
  taskId: string;
  /** 规格修订号，单调递增（§11 plan_revisions.spec_revision） */
  specRevision: number;
  /** 关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id） */
  intentAuthorizationId: string;
  /** DAG 版本号（§11 plan_revisions.dag_version） */
  dagVersion: number;
  /** node→capability 映射版本（§11 plan_revisions.node_capability_map_version） */
  nodeCapabilityMapVersion: string;
  /** stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version） */
  stageCapabilityMapVersion: string;
  /** 语义计划哈希，跨修订版本稳定 */
  semanticPlanHash: string;
  /** 本修订版本内容摘要 */
  planRevisionDigest: string;
  /** DAG 节点列表 */
  nodes: Record<string, unknown>[];
  /** 阶段屏障列表 */
  barriers: Record<string, unknown>[];
  /** target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类） */
  stageMaps: Record<string, unknown>;
  createdAt: string;
}

/** ControlCommand — 由 generate.py 自动生成，禁止手动修改 */
export interface ControlCommand {
  /** 命令唯一 ID */
  commandId: string;
  /** 命令类型：软暂停、立即停止、取消（不可逆）、恢复 */
  commandType: "PAUSE" | "IMMEDIATE_STOP" | "CANCEL" | "RESUME";
  /** 目标任务 ID */
  taskId: string;
  /** 目标 Run ID（可选，指定具体 Run） */
  runId?: string;
  /** 命令签发时间 */
  issuedAt: string;
  /** 控制 epoch，用于防重放和顺序保证 */
  controlEpoch: number;
  /** 命令签发者标识 */
  issuedBy?: string;
  /** 命令原因说明（可选） */
  reason?: string;
}

/** TaskIntakeRequest — 由 generate.py 自动生成，禁止手动修改 */
export interface TaskIntakeRequest {
  /** 用户自然语言需求描述 */
  requirementText: string;
  /** 用户选择的目标终点阶段（Master Spec §6.1 权威 target_stage 分类） */
  targetStage: "DESIGN_APPROVED" | "CODEX_APPROVED" | "PR_READY" | "MERGED" | "STAGING_ACCEPTED" | "PRODUCTION_ACCEPTED";
  repositorySelection: Record<string, unknown>;
  environmentSelection?: Record<string, unknown>;
  /** 用户设定的自主执行预算上限（毫秒） */
  budgetCapMs?: number;
  /** 用户已确认风险 */
  userRiskAcknowledged: true;
}

/** TaskIntakeAccepted — 由 generate.py 自动生成，禁止手动修改 */
export interface TaskIntakeAccepted {
  /** Agent 生成的任务唯一 ID */
  taskId: string;
  /** IntentAuthorization 唯一 ID */
  intentAuthorizationId: string;
  normalizedRepositoryBinding: Record<string, unknown>;
  /** 服务端求交后的允许 capability set 摘要 */
  allowedCapabilitySetDigest: string;
  /** 有效自主执行预算（毫秒） */
  effectiveBudgetMs: number;
  /** 风险摘要内容的摘要（不含明文风险详情） */
  riskSummaryDigest: string;
  /** 授权绝对过期时间 */
  expiresAt: string;
  /** 接受时间 */
  acceptedAt: string;
}

/** ClaudeSelfReview — 由 generate.py 自动生成，禁止手动修改 */
export interface ClaudeSelfReview {
  /** 自审回执唯一 ID */
  selfReviewId: string;
  /** 关联任务 ID */
  taskId?: string;
  /** 关联 Step ID */
  stepId?: string;
  /** 被审核的 base commit SHA；漂移后本回执失效 */
  reviewedBaseSha: string;
  /** 被审核的候选 commit SHA；漂移后本回执失效 */
  reviewedCandidateSha: string;
  /** 所用审核规则集版本 */
  rubricVersion: string;
  /** 自审结论：pass 表示通过，needs_fix 表示需要修复 */
  result: "pass" | "needs_fix";
  /** 发现问题列表（result=pass 时可为空数组） */
  findings: Record<string, unknown>[];
  /** 证据摘要列表（每个证据的内容寻址摘要） */
  evidenceDigests: string[];
  /** 自审完成时间 */
  createdAt: string;
}

/** PreparedEventV2 — 由 generate.py 自动生成，禁止手动修改 */
export interface PreparedEventV2 {
  /** Adapter 分配的摄取 ID，用于幂等去重 */
  ingestEventId: string;
  /** 所属任务 ID */
  taskId: string;
  /** 事件类型 */
  eventType: "stream" | "provider-semantic" | "tool" | "state" | "gate" | "receipt" | "control" | "heartbeat";
  /** 来源端序号（用于顺序校验和去重） */
  sourceSeq: number;
  /** 传输 span 摘要（用于 STREAM-002 完整性检查） */
  transportSpanDigest?: string;
  /** 事件负载（已脱敏） */
  payload: Record<string, unknown>;
  /** 负载内容摘要（SHA-256 hex） */
  payloadDigest?: string;
  /** 预备时间 */
  preparedAt: string;
}

/** PreparedBatchV2 — 由 generate.py 自动生成，禁止手动修改 */
export interface PreparedBatchV2 {
  /** 批次唯一 ID */
  batchId: string;
  /** 所属任务 ID */
  taskId: string;
  /** 批次序号，用于稳定 identity 生成 */
  batchOrdinal: number;
  /** 批次内预备事件列表 */
  events: unknown[];
  /** 前驱批次 head 摘要（genesis 批次为 null） */
  previousHead: string | null;
  /** claim 时的 writer epoch，用于竞争检测 */
  claimEpoch?: number;
  /** 批次预备时间 */
  preparedAt: string;
}

/** DurableEventV2 — 由 generate.py 自动生成，禁止手动修改 */
export interface DurableEventV2 {
  /** 物化器分配的全局唯一事件 ID */
  eventId: string;
  /** 所属任务 ID */
  taskId: string;
  /** 任务内单调递增序号（由物化器分配） */
  taskSeq: number;
  /** 所属批次序号 */
  batchOrdinal: number;
  /** 事件内容摘要（SHA-256 hex）。计算输入为 DurableEventV2 去除本字段后的完整对象（JCS），previousEventDigest 仍参与计算。 */
  eventDigest: string;
  /** 前驱事件 eventDigest（genesis 为 sha256:<64 个 0>，固定全零 predecessor）。批内下一事件指向前一事件的 eventDigest。 */
  previousEventDigest: string | null;
  /** 原始摄取 ID（来自 PreparedEventV2） */
  ingestEventId: string;
  /** 事件类型 */
  eventType: "stream" | "provider-semantic" | "tool" | "state" | "gate" | "receipt" | "control" | "heartbeat";
  /** 来源端序号 */
  sourceSeq: number;
  /** 事件负载（已脱敏） */
  payload: Record<string, unknown>;
  /** 负载内容摘要 */
  payloadDigest?: string;
  /** 耐久化完成时间 */
  durableAt: string;
}

/** IpcEnvelope — 由 generate.py 自动生成，禁止手动修改 */
export interface IpcEnvelope {
  /** 信封唯一 ID（防重放） */
  envelopeId: string;
  /** 消息类型 */
  messageType: "CONTROL_COMMAND" | "STATUS_UPDATE" | "EVENT_BATCH" | "HEARTBEAT" | "ACK" | "ERROR" | "RUNNER_PROTOCOL";
  /** 发送方进程身份（含 SID） */
  senderId: string;
  /** 接收方进程身份 */
  recipientId: string;
  /** 随机 nonce，防重放攻击 */
  nonce: string;
  /** 关联 ID，用于请求-响应匹配 */
  correlationId?: string;
  /** 消息负载（类型由 messageType 决定） */
  payload: Record<string, unknown>;
  /** 负载内容摘要，用于完整性验证 */
  payloadDigest?: string;
  /** 发送时间 */
  sentAt: string;
}

/** RunnerProtocol — 由 generate.py 自动生成，禁止手动修改 */
export interface RunnerProtocol {
  /** 协议消息唯一 ID */
  messageId: string;
  /** 协议消息类型 */
  protocolMessageType: "START" | "INSPECT" | "INTERRUPT" | "KILL" | "STATUS" | "COMPLETED" | "FAILED" | "RECONCILING" | "HEARTBEAT";
  /** Attempt 唯一 ID（每次派发不可复用） */
  attemptId: string;
  /** 执行者唯一标识 */
  executorId: string;
  /** Fencing token，严格单调递增 */
  fencingToken: number;
  /** 控制 epoch */
  controlEpoch: number;
  /** 进程启动身份（containerId/execId/PID等） */
  processStartIdentity?: Record<string, unknown>;
  /** 消息负载（按 protocolMessageType 定义结构） */
  payload: Record<string, unknown>;
  /** 消息时间戳 */
  timestamp: string;
}

/** TestReceipt — 由 generate.py 自动生成，禁止手动修改 */
export interface TestReceipt {
  /** 回执唯一 ID（全局唯一，verify_receipts 强制 DUPLICATE_RECEIPT_ID 检查） */
  receiptId: string;
  /** 测试 ID（来自 required-test-catalog.v1.json） */
  testId: string;
  /** 环境 Compatibility Manifest 摘要 */
  environmentManifestDigest: string;
  /** 执行动作列表（P0-6：至少 1 条，杜绝手工文字 PASS） */
  actions: Record<string, unknown>[];
  /** 期望结果描述（机器可读；P0-6：至少 1 个属性以防伪造 PASS） */
  expected: Record<string, unknown>;
  /** 实际观察结果（机器可读；P0-6：至少 1 个属性以防伪造 PASS） */
  actual: Record<string, unknown>;
  /** 外部副作用计数 */
  sideEffectCount: number;
  /** 相关 Artifact 摘要列表 */
  artifactDigests: string[];
  /** 测试结论（不能通过手工文字改为 PASS） */
  result: "PASS" | "FAIL" | "BLOCKED_UNCERTIFIED";
  /** 失败原因（result=FAIL 时必填） */
  failureReason?: string;
  /** 场景合同内容摘要（来自 required-test-catalog；P0-6：必填以防伪造 PASS） */
  scenarioContractDigest: string;
  /** 实现贡献者列表 */
  implementationContributors?: string[];
  /** 最终通过责任人（P0-6：必填；verify_receipts 强制匹配 catalog.owner + 白名单） */
  finalPassOwner: string;
  /** 执行资质标签（P0-6：必填，标识评审/实施/审计等角色资格，如 codex-reviewer / claude-implementer） */
  qualification: string;
  /** 要求重放次数（P0-6：>= 1，verify_receipts 按 catalog.requiredReplays 强制覆盖门禁） */
  requiredReplays: number;
  /** 回执创建时间 */
  createdAt: string;
}
