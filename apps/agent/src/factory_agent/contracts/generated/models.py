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
    # 批次唯一 ID
    batchId: str
    # 所属任务 ID
    taskId: str
    # 批次序号，用于稳定 identity 生成
    batchOrdinal: int
    # 批次内预备事件列表
    events: list[Any]
    # 前驱批次 head 摘要（genesis 批次为 null）
    previousHead: Optional[str]
    # claim 时的 writer epoch，用于竞争检测
    claimEpoch: Optional[int]
    # 批次预备时间
    preparedAt: str

class DurableEventV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 物化器分配的全局唯一事件 ID
    eventId: str
    # 所属任务 ID
    taskId: str
    # 任务内单调递增序号（由物化器分配）
    taskSeq: int
    # 所属批次序号
    batchOrdinal: int
    # 事件内容摘要（SHA-256 hex）
    eventDigest: str
    # 本批次物化后的 head 摘要（链式验证用）
    head: str
    # 前驱批次 head 摘要（genesis 为 null）
    previousHead: Optional[str]
    # 原始摄取 ID（来自 PreparedEventV2）
    ingestEventId: str
    # 事件类型
    eventType: Literal["stream", "provider-semantic", "tool", "state", "gate", "receipt", "control", "heartbeat"]
    # 来源端序号
    sourceSeq: int
    # 事件负载（已脱敏）
    payload: dict[str, Any]
    # 负载内容摘要
    payloadDigest: Optional[str]
    # 耐久化完成时间
    durableAt: str

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
    # 回执唯一 ID
    receiptId: str
    # 测试 ID（来自 required-test-catalog.v1.json）
    testId: str
    # 环境 Compatibility Manifest 摘要
    environmentManifestDigest: str
    # 执行动作列表
    actions: list[dict[str, Any]]
    # 期望结果描述（机器可读）
    expected: dict[str, Any]
    # 实际观察结果（机器可读）
    actual: dict[str, Any]
    # 外部副作用计数
    sideEffectCount: int
    # 相关 Artifact 摘要列表
    artifactDigests: list[str]
    # 测试结论（不能通过手工文字改为 PASS）
    result: Literal["PASS", "FAIL", "BLOCKED_UNCERTIFIED"]
    # 失败原因（result=FAIL 时必填）
    failureReason: Optional[str]
    # 场景合同内容摘要（来自 required-test-catalog）
    scenarioContractDigest: Optional[str]
    # 实现贡献者列表
    implementationContributors: list[str]
    # 最终通过责任人
    finalPassOwner: Optional[str]
    # 要求重放次数
    requiredReplays: Optional[int]
    # 回执创建时间
    createdAt: str
