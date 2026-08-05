"""
此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。
源 schema: contracts/schemas/*.schema.json
算法版本: v1
"""
from __future__ import annotations

from typing import Any, Literal, Optional, TypedDict, Union

__all__ = [
    "CredentialRef",
    "RunSpec",
    "PlanRevision",
    "ControlCommand",
    "TaskIntakeRequest",
    "TaskIntakeAccepted",
    "ClaudeSelfReview",
    "PreparedEventV2",
    "PreparedBatchV2",
    "DurableEventV2",
    "IpcEnvelope",
    "RunnerProtocol",
    "TestReceipt",
]

class CredentialRef(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 凭据唯一 ID（不含明文值）
    credentialId: str
    # 凭据类型
    credentialType: Literal["SSH_KEY", "REGISTRY_TOKEN", "API_KEY", "GITHUB_APP", "FACTORY_PROFILE_TOKEN"]
    # 凭据所属 D-backed 隔离 profile 作用域
    profileScope: str
    # 绑定凭据的资源指纹（如 host-key、repository ID）
    resourceFingerprint: Optional[str]
    # 凭据绝对过期时间（ISO 8601）
    expiresAt: Optional[str]

class RunSpec(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # RunSpec schema 版本号
    schemaVersion: int
    # 同一 taskId 下的规格修订序号
    specRevision: Optional[int]
    # 父 PlanRevision ID；genesis 修订为 null
    parentRevisionId: Optional[str]
    # 关联任务 ID
    taskId: str
    # 用户目标的结构化表达
    goal: str
    # 规划假设列表
    assumptions: list[str]
    # 范围声明（§8：include/exclude）
    scope: dict[str, Any]
    # 约束条件列表
    constraints: list[str]
    # 验收条件列表
    acceptanceCriteria: list[str]
    # 目标终点阶段（Master Spec §6.1 权威 6 阶段）
    targetStage: Literal["DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"]
    # 仓库绑定（§8：mode/root/baseBranch/baseCommit）
    repository: dict[str, Any]
    # 工作计划 DAG（§8：dagVersion/nodes/barriers）
    workPlan: dict[str, Any]
    # 风险画像（§8：level/reasons）
    riskProfile: dict[str, Any]
    # node-capability-map 版本标识
    nodeCapabilityMapVersion: str
    # stage-capability-map 版本标识
    stageCapabilityMapVersion: str
    # 关联 IntentAuthorization ID
    intentAuthorizationId: str
    # 语义计划哈希（跨修订版本稳定，从语义字段投影派生）
    semanticPlanHash: str
    # 完整不可变修订记录摘要（写入 PlanRevision 后填入）
    planRevisionDigest: Optional[str]
    # RunSpec 创建时间
    createdAt: Optional[str]

class PlanRevision(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 本修订版本唯一 ID
    planRevisionId: str
    # 父修订版本 ID（范围内重规划时设置）
    parentRevisionId: Optional[str]
    # 所属任务 ID（§11 plan_revisions.task_id）
    taskId: str
    # 规格修订号，单调递增（§11 plan_revisions.spec_revision）
    specRevision: int
    # 关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）
    intentAuthorizationId: str
    # DAG 版本号（§11 plan_revisions.dag_version）
    dagVersion: int
    # node→capability 映射版本（§11 plan_revisions.node_capability_map_version）
    nodeCapabilityMapVersion: str
    # stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）
    stageCapabilityMapVersion: str
    # 语义计划哈希，跨修订版本稳定
    semanticPlanHash: str
    # 本修订版本内容摘要
    planRevisionDigest: str
    # DAG 节点列表
    nodes: list[dict[str, Any]]
    # 阶段屏障列表
    barriers: list[dict[str, Any]]
    # target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）
    stageMaps: dict[str, Any]
    createdAt: str

class ControlCommand(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 命令唯一 ID
    commandId: str
    # 命令类型：软暂停、立即停止、取消（不可逆）、恢复
    commandType: Literal["PAUSE", "IMMEDIATE_STOP", "CANCEL", "RESUME"]
    # 目标任务 ID
    taskId: str
    # 目标 Run ID（可选，指定具体 Run）
    runId: Optional[str]
    # 命令签发时间
    issuedAt: str
    # 控制 epoch，用于防重放和顺序保证
    controlEpoch: int
    # 命令签发者标识
    issuedBy: Optional[str]
    # 命令原因说明（可选）
    reason: Optional[str]

class TaskIntakeRequest(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 用户自然语言需求描述
    requirementText: str
    # 用户选择的目标终点阶段（Master Spec §6.1 权威 target_stage 分类）
    targetStage: Literal["DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"]
    repositorySelection: dict[str, Any]
    environmentSelection: dict[str, Any]
    # 用户设定的自主执行预算上限（毫秒）
    budgetCapMs: Optional[int]
    # 用户已确认风险
    userRiskAcknowledged: Literal[True]

class TaskIntakeAccepted(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # Agent 生成的任务唯一 ID
    taskId: str
    # IntentAuthorization 唯一 ID
    intentAuthorizationId: str
    normalizedRepositoryBinding: dict[str, Any]
    # 服务端求交后的允许 capability set 摘要
    allowedCapabilitySetDigest: str
    # 有效自主执行预算（毫秒）
    effectiveBudgetMs: int
    # 风险摘要内容的摘要（不含明文风险详情）
    riskSummaryDigest: str
    # 授权绝对过期时间
    expiresAt: str
    # 接受时间
    acceptedAt: str

class ClaudeSelfReview(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 自审回执唯一 ID
    selfReviewId: str
    # 关联任务 ID
    taskId: Optional[str]
    # 关联 Step ID
    stepId: Optional[str]
    # 被审核的 base commit SHA；漂移后本回执失效
    reviewedBaseSha: str
    # 被审核的候选 commit SHA；漂移后本回执失效
    reviewedCandidateSha: str
    # 所用审核规则集版本
    rubricVersion: str
    # 自审结论：pass 表示通过，needs_fix 表示需要修复
    result: Literal["pass", "needs_fix"]
    # 发现问题列表（result=pass 时可为空数组）
    findings: list[dict[str, Any]]
    # 证据摘要列表（每个证据的内容寻址摘要）
    evidenceDigests: list[str]
    # 自审完成时间
    createdAt: str

class PreparedEventV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # Adapter 分配的摄取 ID，用于幂等去重
    ingestEventId: str
    # 所属任务 ID
    taskId: str
    # 事件类型
    eventType: Literal["stream", "provider-semantic", "tool", "state", "gate", "receipt", "control", "heartbeat"]
    # 来源端序号（用于顺序校验和去重）
    sourceSeq: int
    # 传输 span 摘要（用于 STREAM-002 完整性检查）
    transportSpanDigest: Optional[str]
    # 事件负载（已脱敏）
    payload: dict[str, Any]
    # 负载内容摘要（SHA-256 hex）
    payloadDigest: Optional[str]
    # 预备时间
    preparedAt: str

class PreparedBatchV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 批次唯一稳定 ID。
    preparedBatchId: str
    # claim 时的 writer epoch（用于竞争检测）。
    writerEpoch: int
    # 批次状态机（§11 ingest_batches）。正常转换仅 CLAIMED → PREPARING → PREPARED → COMMITTED；旧 epoch pre-commit 状态只能由 Reconciler CAS 为 ABANDONED。COMMITTED/ABANDONED 均为终态。
    state: Literal["CLAIMED", "PREPARING", "PREPARED", "COMMITTED"]
    # 按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。
    orderedIngestIds: list[str]
    # 每个 Task 唯一的 expectedCommittedHead 锚点 + 批内 ordinal 范围（§11 ingest_batch_task_heads）。
    perTaskExpectedHeads: list[dict[str, Any]]
    # segment digest 列表。
    segmentDigests: list[dict[str, Any]]
    # 总事件数。
    eventCount: int
    # 总 payload 字节数。
    payloadBytes: int
    # 本批首事件 batchOrdinal。
    firstBatchOrdinal: int
    # 本批末事件 batchOrdinal。
    lastBatchOrdinal: int
    # 本批最旧 ingestedAt。
    oldestIngestedAt: str
    # 本批 prepared 完成时间。
    preparedAt: str
    # PreparedBatch schema 版本。
    schemaVersion: Literal[2]
    # coordinator 版本（compatibility manifest 绑定）。
    coordinatorVersion: str

class DurableEventV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。
    schemaVersion: Literal[2]
    # 耐久化等级（§10.4）：authoritative_state=内部权威 state.changed/lease/授权；side_effect_receipt=外部副作用 receipt；provider_source=Provider semantic/public frame；derived=UI summary/指标等可重建派生。
    durabilityClass: Literal["authoritative_state", "side_effect_receipt", "provider_source", "derived"]
    # 物化器分配的全局唯一事件 ID（evt_<64 位小写十六进制>，SHA-256(JCS([factory-event-id-v2, ingestEventId]))）。崩溃重试得到同一身份。
    eventId: str
    # 所属任务 ID。
    taskId: str
    # 任务内单调递增序号（由物化器分配，CAS 持久）。UI cursor 用 (taskId, taskSeq)。
    taskSeq: int
    # 所属 Run ID。
    runId: str
    # 单 Run 内诊断序号（不能跨 Run 唯一）。
    runSeq: int
    # 所属 Step ID。
    stepId: str
    # 所属 Attempt ID。
    attemptId: str
    # 事件来源 actor 身份。
    source: Literal["claude", "codex", "verifier", "release", "orchestrator"]
    # 原始摄取 ID（来自 PreparedEventV2）。
    ingestEventId: str
    # 具体事件类型（§10.1）。model.summary 必带 summaryOrigin=provider_public + providerEventId + sanitizedProviderFrameRef + sanitizedProviderFrameDigest。
    eventType: Literal["process.started", "stream.segment.committed", "stream.terminated", "model.summary", "orchestrator.objective", "tool.call", "tool.result", "state.changed"]
    # Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。
    providerEventId: Optional[str]
    # Provider 来源端序号（用于去重与对齐）。
    sourceSeq: int
    # Provider stream 稳定 ID（stream.* 事件必填；非 stream 事件 null）。
    streamId: Optional[str]
    # 所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。
    preparedBatchId: str
    # 所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。
    batchOrdinal: int
    # 源传输 span：内存中以原始传输字节精确计数。credential/secret/PII 仅存 providerEventId/sourceSeq 与诚实的 mappingPrecision（byte|field|frame|none），精确字节起止不能进普通事件。
    sourceTransportSpan: dict[str, Any]
    # 脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。
    sanitizedStreamSpan: dict[str, Any]
    # RFC3339 接收 wall time（仅用于显示，不能参与排序）。
    wallTime: str
    # 单调时钟纳秒。
    monotonicTimeNs: int
    # 完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。
    ingestedAt: str
    # Provider CLI 上报版本。
    providerVersion: str
    # Adapter 自身 semver 版本。
    adapterVersion: str
    # 受管进程身份（§10.1）：用于 RUNNING 真实性校验（PID/Job Object/WSL/container exec）。
    processIdentity: dict[str, Any]
    # 事件负载（已脱敏，未脱敏原文不得落盘）。
    payload: dict[str, Any]
    # 已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃。model.summary 必填，其它 null。
    sanitizedProviderFrameDigest: Optional[str]
    # 脱敏后 payload 的 JCS 摘要。
    payloadDigest: str
    # 前驱事件 eventDigest（genesis 用 sha256:<64 个 0> 固定 predecessor）。
    previousEventDigest: str
    # 本事件 SHA-256(JCS(去除 eventDigest 字段后完整对象))。previousEventDigest 仍参与计算。
    eventDigest: str
    # 本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。
    redactions: list[dict[str, Any]]
    # redactions 列表内容的 SHA-256 摘要。
    redactionManifestDigest: str

class IpcEnvelope(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 信封唯一 ID（防重放）
    envelopeId: str
    # 消息类型
    messageType: Literal["CONTROL_COMMAND", "STATUS_UPDATE", "EVENT_BATCH", "HEARTBEAT", "ACK", "ERROR", "RUNNER_PROTOCOL"]
    # 发送方进程身份（含 SID）
    senderId: str
    # 接收方进程身份
    recipientId: str
    # 随机 nonce，防重放攻击
    nonce: str
    # 关联 ID，用于请求-响应匹配
    correlationId: Optional[str]
    # 消息负载（类型由 messageType 决定）
    payload: dict[str, Any]
    # 负载内容摘要，用于完整性验证
    payloadDigest: Optional[str]
    # 发送时间
    sentAt: str

class RunnerProtocol(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 协议消息唯一 ID
    messageId: str
    # 协议消息类型
    protocolMessageType: Literal["START", "INSPECT", "INTERRUPT", "KILL", "STATUS", "COMPLETED", "FAILED", "RECONCILING", "HEARTBEAT"]
    # Attempt 唯一 ID（每次派发不可复用）
    attemptId: str
    # 执行者唯一标识
    executorId: str
    # Fencing token，严格单调递增
    fencingToken: int
    # 控制 epoch
    controlEpoch: int
    # 进程启动身份（containerId/execId/PID等）
    processStartIdentity: dict[str, Any]
    # 消息负载（按 protocolMessageType 定义结构）
    payload: dict[str, Any]
    # 消息时间戳
    timestamp: str

class TestReceipt(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 回执唯一 ID（全局唯一，verify_receipts 强制 DUPLICATE_RECEIPT_ID 检查）
    receiptId: str
    # 测试 ID（来自 required-test-catalog.v1.json）
    testId: str
    # 环境 Compatibility Manifest 摘要
    environmentManifestDigest: str
    # 执行动作列表（P0-6：至少 1 条，杜绝手工文字 PASS）
    actions: list[dict[str, Any]]
    # 期望结果描述（机器可读；P0-6：至少 1 个属性以防伪造 PASS）
    expected: dict[str, Any]
    # 实际观察结果（机器可读；P0-6：至少 1 个属性以防伪造 PASS）
    actual: dict[str, Any]
    # 外部副作用计数
    sideEffectCount: int
    # 相关 Artifact 摘要列表
    artifactDigests: list[str]
    # 测试结论（不能通过手工文字改为 PASS）
    result: Literal["PASS", "FAIL", "BLOCKED_UNCERTIFIED"]
    # 失败原因（result=FAIL 时必填）
    failureReason: Optional[str]
    # 场景合同内容摘要（来自 required-test-catalog；P0-6：必填以防伪造 PASS）
    scenarioContractDigest: str
    # 实现贡献者列表
    implementationContributors: list[str]
    # 最终通过责任人（P0-6：必填；verify_receipts 强制匹配 catalog.owner + 白名单）
    finalPassOwner: str
    # 执行资质等级（GPT 第二轮审核限定）：PARTIAL=阶段/中间通过；FINAL=最终/收口通过。verify_receipts 强制 PARTIAL 不能冒充 FINAL 用于收口。
    qualification: Literal["PARTIAL", "FINAL"]
    # 要求重放次数（P0-6：>= 1，verify_receipts 按 catalog.requiredReplays 强制覆盖门禁）
    requiredReplays: int
    # 回执创建时间
    createdAt: str
