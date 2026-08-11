"""
此模块由 contracts/codegen/generate.py 自动生成，禁止手动修改。
源 schema: contracts/schemas/*.schema.json
算法版本: v1
"""
from typing import Any, Final, Literal, Optional, Required, TypedDict, Union

__all__ = [
    "RUN_SPEC_SCHEMA_SOURCE_SHA256",
    "RUN_SPEC_SCHEMA_JSON",
    "RUN_SPEC_SCHEMA_JSON_SHA256",
    "PLAN_REVISION_SCHEMA_SOURCE_SHA256",
    "PLAN_REVISION_SCHEMA_JSON",
    "PLAN_REVISION_SCHEMA_JSON_SHA256",
    "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON",
    "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256",
    "CredentialRef",
    "RunSpec",
    "PlanRevision",
    "IntentAuthorization",
    "ExecutionAuthorization",
    "ControlCommand",
    "TaskIntakeRequest",
    "TaskIntakeAccepted",
    "ClaudeSelfReview",
    "PreparedEventV2",
    "PreparedBatchV2",
    "DurableEventV2",
    "IpcEnvelope",
    "RunnerProtocol",
]

# 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。
RUN_SPEC_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:185d4067779d4830d03b861f2e6b61f512675aa32e02bc54326351a79d99087e"
RUN_SPEC_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/run-spec.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"规划层生成的不可变、版本化 RunSpec：完整语义文档（Master Spec §8）。semanticPlanHash 从本文档的语义字段投影派生；写入后不可修改，修复只能新建子版本。","properties":{"acceptanceCriteria":{"description":"验收条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"assumptions":{"description":"规划假设列表","items":{"minLength":1,"type":"string"},"type":"array"},"constraints":{"description":"约束条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"createdAt":{"description":"RunSpec 创建时间","format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"goal":{"description":"用户目标的结构化表达","minLength":1,"type":"string"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node-capability-map 版本标识","minLength":1,"type":"string"},"parentRevisionId":{"description":"父 PlanRevision ID；genesis 修订为 null","type":["string","null"]},"planRevisionDigest":{"description":"完整不可变修订记录摘要（写入 PlanRevision 后填入）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"repository":{"additionalProperties":false,"description":"仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）","properties":{"baseBranch":{"description":"基础分支名","minLength":1,"type":"string"},"baseCommit":{"description":"bootstrap 完成后可信 base commit 的 40 位小写完整 SHA","pattern":"^[0-9a-f]{40}$","type":"string"},"mode":{"description":"仓库模式","enum":["existing","new"],"type":"string"},"root":{"description":"规范化后的 D 盘仓库根路径","minLength":1,"type":"string"}},"required":["mode","root","baseBranch","baseCommit"],"type":"object"},"riskProfile":{"additionalProperties":false,"description":"风险画像（§8：level/reasons）","properties":{"level":{"description":"风险等级","enum":["low","medium","high","critical"],"type":"string"},"reasons":{"description":"风险原因列表","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["level","reasons"],"type":"object"},"schemaVersion":{"description":"RunSpec schema 版本号","minimum":1,"type":"integer"},"scope":{"additionalProperties":false,"description":"范围声明（§8：include/exclude）","properties":{"exclude":{"description":"排除范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"},"include":{"description":"纳入范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["include","exclude"],"type":"object"},"semanticPlanHash":{"description":"语义计划哈希（跨修订版本稳定，从语义字段投影派生）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"specRevision":{"description":"同一 taskId 下的规格修订序号","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage-capability-map 版本标识","minLength":1,"type":"string"},"targetStage":{"description":"目标终点阶段（Master Spec §6.1 权威 6 阶段）","enum":["DESIGN_APPROVED","CODEX_APPROVED","PR_READY","MERGED","STAGING_ACCEPTED","PRODUCTION_ACCEPTED"],"type":"string"},"taskId":{"description":"关联任务 ID","minLength":1,"type":"string"},"workPlan":{"additionalProperties":false,"description":"工作计划 DAG（§8：dagVersion/nodes/barriers）","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"minLength":1,"type":"string"},"passPredicateId":{"minLength":1,"type":"string"},"requiredNodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"},"settleTimeoutMs":{"minimum":1,"type":"integer"}},"required":["businessPhase","barrierOrdinal","requiredNodeIds","settleTimeoutMs","passPredicateId"],"type":"object"},"type":"array"},"dagVersion":{"description":"DAG 结构版本","minimum":1,"type":"integer"},"nodes":{"description":"DAG 工作节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"minLength":1,"type":"string"},"dependsOn":{"items":{"minLength":1,"type":"string"},"type":"array"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"required":{"type":"boolean"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"},"retryPolicyId":{"minLength":1,"type":"string"},"sideEffectClass":{"minLength":1,"type":"string"},"successPredicateId":{"minLength":1,"type":"string"},"timeoutMs":{"minimum":1,"type":"integer"}},"required":["logicalNodeId","businessPhase","barrierOrdinal","nodeType","required","dependsOn","sideEffectClass","requiredArtifacts","successPredicateId","timeoutMs","retryPolicyId"],"type":"object"},"type":"array"}},"required":["dagVersion","nodes","barriers"],"type":"object"}},"required":["schemaVersion","taskId","goal","assumptions","scope","constraints","acceptanceCriteria","targetStage","repository","workPlan","riskProfile","nodeCapabilityMapVersion","stageCapabilityMapVersion","intentAuthorizationId","semanticPlanHash"],"title":"RunSpec","type":"object"}'
RUN_SPEC_SCHEMA_JSON_SHA256: Final[str] = "sha256:f5f68bff70e3f688f08b33f682533cde9616fdcc19d88090409f112b8fef16a3"
PLAN_REVISION_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:1095c85161c56edaf4fda8e344b0d813106b695eacfaf517ce698134a1a4d0cc"
PLAN_REVISION_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"minLength":1,"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"minLength":1,"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionDigest":{"description":"本修订版本内容摘要","minLength":1,"type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"description":"语义计划哈希，跨修订版本稳定","minLength":1,"type":"string"},"signature":{"minLength":1,"type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","planRevisionDigest","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}'
PLAN_REVISION_SCHEMA_JSON_SHA256: Final[str] = "sha256:2491645a45eae4fd3ba6272c887213056a16a75832129078df51f81fbf327c3f"
# 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。
PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"minLength":1,"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"minLength":1,"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"description":"语义计划哈希，跨修订版本稳定","minLength":1,"type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}'
PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256: Final[str] = "sha256:748d62a28b85afcfc01d42a028833a7c2f0bfdd88979eb1f33071dccaea35c48"

class CredentialRef(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 凭据唯一 ID（不含明文值）
    credentialId: Required[str]
    # 凭据类型
    credentialType: Required[Literal["SSH_KEY", "REGISTRY_TOKEN", "API_KEY", "GITHUB_APP", "FACTORY_PROFILE_TOKEN"]]
    # 凭据所属 D-backed 隔离 profile 作用域
    profileScope: Required[str]
    # 绑定凭据的资源指纹（如 host-key、repository ID）
    resourceFingerprint: str
    # 凭据绝对过期时间（ISO 8601）
    expiresAt: str

class RunSpec(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # RunSpec schema 版本号
    schemaVersion: Required[int]
    # 同一 taskId 下的规格修订序号
    specRevision: int
    # 父 PlanRevision ID；genesis 修订为 null
    parentRevisionId: Optional[str]
    # 关联任务 ID
    taskId: Required[str]
    # 用户目标的结构化表达
    goal: Required[str]
    # 规划假设列表
    assumptions: Required[list[str]]
    # 范围声明（§8：include/exclude）
    scope: Required[dict[str, Any]]
    # 约束条件列表
    constraints: Required[list[str]]
    # 验收条件列表
    acceptanceCriteria: Required[list[str]]
    # 目标终点阶段（Master Spec §6.1 权威 6 阶段）
    targetStage: Required[Literal["DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"]]
    # 仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）
    repository: Required[dict[str, Any]]
    # 工作计划 DAG（§8：dagVersion/nodes/barriers）
    workPlan: Required[dict[str, Any]]
    # 风险画像（§8：level/reasons）
    riskProfile: Required[dict[str, Any]]
    # node-capability-map 版本标识
    nodeCapabilityMapVersion: Required[str]
    # stage-capability-map 版本标识
    stageCapabilityMapVersion: Required[str]
    # 关联 IntentAuthorization ID
    intentAuthorizationId: Required[str]
    # 语义计划哈希（跨修订版本稳定，从语义字段投影派生）
    semanticPlanHash: Required[str]
    # 完整不可变修订记录摘要（写入 PlanRevision 后填入）
    planRevisionDigest: str
    # RunSpec 创建时间
    createdAt: str

class PlanRevision(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 本修订版本唯一 ID
    planRevisionId: Required[str]
    # 父修订版本 ID（范围内重规划时设置）
    parentRevisionId: str
    # 所属任务 ID（§11 plan_revisions.task_id）
    taskId: Required[str]
    # 规格修订号，单调递增（§11 plan_revisions.spec_revision）
    specRevision: Required[int]
    # 关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）
    intentAuthorizationId: Required[str]
    # DAG 版本号（§11 plan_revisions.dag_version）
    dagVersion: Required[int]
    # node→capability 映射版本（§11 plan_revisions.node_capability_map_version）
    nodeCapabilityMapVersion: Required[str]
    # stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）
    stageCapabilityMapVersion: Required[str]
    # 语义计划哈希，跨修订版本稳定
    semanticPlanHash: Required[str]
    # 本修订版本内容摘要
    planRevisionDigest: Required[str]
    signature: str
    # DAG 节点列表
    nodes: Required[list[dict[str, Any]]]
    # 阶段屏障列表
    barriers: Required[list[dict[str, Any]]]
    # target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）
    stageMaps: Required[dict[str, Any]]
    createdAt: Required[str]

class IntentAuthorization(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # IntentAuthorization 的稳定身份。
    intentAuthorizationId: Required[str]
    # 被授权任务的稳定身份。
    taskId: Required[str]
    # 做出一次性授权的用户身份。
    userId: Required[str]
    # 规范化用户需求的闭合 JCS/NFC 快照引用。
    requirementDigest: Required[dict[str, Any]]
    # 项目身份，禁止跨项目复用授权。
    projectId: Required[str]
    # 规范化后的仓库身份。
    repositoryId: Required[str]
    # 可解析的仓库/物理路径/模式绑定快照引用。
    repositoryBindingDigest: Required[dict[str, Any]]
    # 可解析的基础分支、base SHA 或 bootstrap 前置事实快照引用。
    baselineDigest: Required[dict[str, Any]]
    # 用户选择的目标终点阶段；只给出 capability 上限。
    targetStage: Required[Literal["DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"]]
    # 派生授权时使用的 stage-capability-map 版本。
    stageCapabilityMapVersion: Required[str]
    # targetStage 展开后的排序 capability 集合快照引用。
    allowedCapabilitySetDigest: Required[dict[str, Any]]
    # 可解析的环境、ServerProfile、数据库和资源目标绑定快照引用。
    targetBindingDigest: Required[dict[str, Any]]
    # 用户接受的最高风险等级。
    riskCeiling: Required[Literal["low", "medium", "high", "critical"]]
    # 可解析的估算成本告警策略快照引用；不把订阅 CLI 估算伪装成真实账单。
    estimatedCostAlertDigest: Required[dict[str, Any]]
    # 端到端自主执行预算（毫秒）；禁止无限值。
    autonomousExecutionBudgetMs: Required[int]
    # 自动修复轮数上限。
    repairLoopLimit: Required[int]
    # 自动重规划次数上限。
    autoReplanLimit: Required[int]
    # Attempt 总数上限。
    attemptLimit: Required[int]
    # 授权签发时间。
    issuedAt: Required[str]
    # 绝对有效期；暂停不会延长它。
    expiresAt: Required[str]
    # 撤销时间；未撤销时显式为 null。
    revokedAt: Required[Optional[str]]
    # 撤销原因；未撤销时显式为 null。
    revokeReason: Required[Optional[str]]

class ExecutionAuthorization(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 派生授权稳定身份。
    executionAuthorizationId: Required[str]
    # 来源 IntentAuthorization 身份。
    intentAuthorizationId: Required[str]
    # 绑定的不可变 PlanRevision 身份。
    planRevisionId: Required[str]
    # 计划语义的闭合快照引用。
    semanticPlanHash: Required[dict[str, Any]]
    # 完整 PlanRevision 的闭合快照引用。
    planRevisionDigest: Required[dict[str, Any]]
    # stage capability map 版本。
    stageCapabilityMapVersion: Required[str]
    # stage capability map 的闭合快照引用。
    stageCapabilityMapDigest: Required[dict[str, Any]]
    # node capability map 版本。
    nodeCapabilityMapVersion: Required[str]
    # node capability map raw-file 内容的闭合快照引用。
    nodeCapabilityMapDigest: Required[dict[str, Any]]
    # 绑定的 Run 身份。
    runId: Required[str]
    # 绑定的 Step 身份。
    stepId: Required[str]
    # 绑定的不可复用 Attempt 身份。
    attemptId: Required[str]
    # 冻结 node-capability-map 的节点类型。
    nodeType: Required[Literal["PLAN", "DESIGN_REVIEW", "BOOTSTRAP_REPOSITORY", "IMPLEMENT", "VERIFY", "CODE_REVIEW", "ATTEST_REVIEW", "PUBLISH_PR", "MERGE", "BUILD_ARTIFACT", "DEPLOY_STAGING", "ACCEPT_STAGING", "DEPLOY_PRODUCTION", "ACCEPT_PRODUCTION", "ROLLBACK", "RECONCILE_TARGET", "RESTORE_DRILL"]]
    # 当前 lease owner 的执行器身份。
    executorId: Required[str]
    # 资源指纹的闭合快照引用；其 payload 必须按 nodeType 使用 node map 内嵌 resourceFingerprintSchema 校验。
    resourceFingerprint: Required[dict[str, Any]]
    # 可解析 capability/resource scope 的闭合快照引用。
    capabilityScopeDigest: Required[dict[str, Any]]
    # 按 factory-action-v1 的 H=sha256(JCS/NFC(...)) 计算的稳定 action key 快照引用。
    idempotencyKey: Required[dict[str, Any]]
    # 闭合且无歧义的输入身份对象；同一种 SHA/digest 至多一个字段，禁止数组重复绑定或自由文本。
    inputBindings: Required[dict[str, Any]]
    # 本 ExecutionAuthorization 唯一对应的 capability action。
    actionCapability: Required[Literal["acceptance.fixture.write", "build.exec.isolated", "check.publish", "container.inspect.scoped", "db.backup", "db.check", "db.migrate", "db.read", "db.restore", "forge.observe.scoped", "git.local_commit", "git.push", "http.check.scoped", "log.read.scoped", "network.egress.scoped", "nginx.switch", "pr.create", "pr.update", "registry.observe.scoped", "registry.push", "remote.observe.scoped", "remote.write.scoped", "repo.bootstrap", "repo.merge", "repo.read", "restore.validation.instance", "rollback", "service.restart.scoped", "ssh.exec.scoped", "target.guard.clear", "test.exec.isolated", "traffic.switch.scoped", "worktree.write"]]
    # selected action 的 key template、completion fact 与消费点确定性投影的闭合快照引用；payload 必须绑定 nodeCapabilityMapDigest、nodeType 和 actionCapability。
    actionPolicySnapshotDigest: Required[dict[str, Any]]
    # 严格单调 lease fencing token。
    fencingToken: Required[int]
    # 控制权 epoch。
    controlEpoch: Required[int]
    # 派生时接受的控制命令序号。
    acceptedControlCommandSeq: Required[int]
    # 剩余可消费次数；不会使用第二个 ttlMs 真源。
    maxUses: Required[int]
    # 仅表示消费投影；撤销/过期分别由 revokedAt/expiresAt 表达，未知状态不能视为成功。
    consumptionState: Required[Literal["AVAILABLE", "CONSUMED"]]
    # 派生授权签发时间。
    issuedAt: Required[str]
    # 派生授权绝对 TTL。
    expiresAt: Required[str]
    # 撤销时间；未撤销时显式为 null。
    revokedAt: Required[Optional[str]]
    # 撤销原因；未撤销时显式为 null。
    revokeReason: Required[Optional[str]]

class ControlCommand(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 命令唯一 ID
    commandId: Required[str]
    # 命令类型：软暂停、立即停止、取消（不可逆）、恢复
    commandType: Required[Literal["PAUSE", "IMMEDIATE_STOP", "CANCEL", "RESUME"]]
    # 目标任务 ID
    taskId: Required[str]
    # 目标 Run ID（可选，指定具体 Run）
    runId: str
    # 命令签发时间
    issuedAt: Required[str]
    # 控制 epoch，用于防重放和顺序保证
    controlEpoch: Required[int]
    # 命令签发者标识
    issuedBy: str
    # 命令原因说明（可选）
    reason: str

class TaskIntakeRequest(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 用户自然语言需求描述
    requirementText: Required[str]
    # 用户选择的目标终点阶段（Master Spec §6.1 权威 target_stage 分类）
    targetStage: Required[Literal["DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY", "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED"]]
    repositorySelection: Required[dict[str, Any]]
    environmentSelection: dict[str, Any]
    # 用户设定的自主执行预算上限（毫秒）
    budgetCapMs: int
    # 用户已确认风险
    userRiskAcknowledged: Required[Literal[True]]

class TaskIntakeAccepted(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # Agent 生成的任务唯一 ID
    taskId: Required[str]
    # IntentAuthorization 唯一 ID
    intentAuthorizationId: Required[str]
    normalizedRepositoryBinding: Required[dict[str, Any]]
    # 服务端求交后的允许 capability set 摘要
    allowedCapabilitySetDigest: Required[str]
    # 有效自主执行预算（毫秒）
    effectiveBudgetMs: Required[int]
    # 风险摘要内容的摘要（不含明文风险详情）
    riskSummaryDigest: Required[str]
    # 授权绝对过期时间
    expiresAt: Required[str]
    # 接受时间
    acceptedAt: Required[str]

class ClaudeSelfReview(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 自审回执唯一 ID
    selfReviewId: Required[str]
    # 关联任务 ID
    taskId: str
    # 关联 Step ID
    stepId: str
    # 被审核的 base commit SHA；漂移后本回执失效
    reviewedBaseSha: Required[str]
    # 被审核的候选 commit SHA；漂移后本回执失效
    reviewedCandidateSha: Required[str]
    # 所用审核规则集版本
    rubricVersion: Required[str]
    # 自审结论：pass 表示通过，needs_fix 表示需要修复
    result: Required[Literal["pass", "needs_fix"]]
    # 发现问题列表（result=pass 时可为空数组）
    findings: Required[list[dict[str, Any]]]
    # 证据摘要列表（每个证据的内容寻址摘要）
    evidenceDigests: Required[list[str]]
    # 自审完成时间
    createdAt: Required[str]

class PreparedEventV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # Adapter 分配的摄取 ID，用于幂等去重
    ingestEventId: Required[str]
    # 所属任务 ID
    taskId: Required[str]
    # 事件类型
    eventType: Required[Literal["stream", "provider-semantic", "tool", "state", "gate", "receipt", "control", "heartbeat"]]
    # 来源端序号（用于顺序校验和去重）
    sourceSeq: Required[int]
    # 传输 span 摘要（用于 STREAM-002 完整性检查）
    transportSpanDigest: str
    # 事件负载（已脱敏）
    payload: Required[dict[str, Any]]
    # 负载内容摘要（SHA-256 hex）
    payloadDigest: str
    # 预备时间
    preparedAt: Required[str]

class PreparedBatchV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 批次唯一稳定 ID。
    preparedBatchId: Required[str]
    # claim 时的 writer epoch（用于竞争检测）。
    writerEpoch: Required[int]
    # 批次状态机（§11 ingest_batches）。正常转换仅 CLAIMED → PREPARING → PREPARED → COMMITTED；旧 epoch pre-commit 状态只能由 Reconciler CAS 为 ABANDONED。COMMITTED/ABANDONED 均为终态。
    state: Required[Literal["CLAIMED", "PREPARING", "PREPARED", "COMMITTED"]]
    # 按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。
    orderedIngestIds: Required[list[str]]
    # 每个 Task 唯一的 expectedCommittedHead 锚点 + 批内 ordinal 范围（§11 ingest_batch_task_heads）。
    perTaskExpectedHeads: Required[list[dict[str, Any]]]
    # segment digest 列表。
    segmentDigests: Required[list[dict[str, Any]]]
    # 总事件数。
    eventCount: Required[int]
    # 总 payload 字节数。
    payloadBytes: Required[int]
    # 本批首事件 batchOrdinal。
    firstBatchOrdinal: Required[int]
    # 本批末事件 batchOrdinal。
    lastBatchOrdinal: Required[int]
    # 本批最旧 ingestedAt。
    oldestIngestedAt: Required[str]
    # 本批 prepared 完成时间。
    preparedAt: Required[str]
    # PreparedBatch schema 版本。
    schemaVersion: Required[Literal[2]]
    # coordinator 版本（compatibility manifest 绑定）。
    coordinatorVersion: Required[str]

class DurableEventV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。
    schemaVersion: Required[Literal[2]]
    # 耐久化等级（§10.4）：authoritative_state=内部权威 state.changed/lease/授权；side_effect_receipt=外部副作用 receipt；provider_source=Provider semantic/public frame；derived=UI summary/指标等可重建派生。
    durabilityClass: Required[Literal["authoritative_state", "side_effect_receipt", "provider_source", "derived"]]
    # 物化器分配的全局唯一事件 ID（evt_<64 位小写十六进制>，SHA-256(JCS([factory-event-id-v2, ingestEventId]))）。崩溃重试得到同一身份。
    eventId: Required[str]
    # 所属任务 ID。
    taskId: Required[str]
    # 任务内单调递增序号（由物化器分配，CAS 持久）。UI cursor 用 (taskId, taskSeq)。
    taskSeq: Required[int]
    # 所属 Run ID。
    runId: Required[str]
    # 单 Run 内诊断序号（不能跨 Run 唯一）。
    runSeq: Required[int]
    # 所属 Step ID。
    stepId: Required[str]
    # 所属 Attempt ID。
    attemptId: Required[str]
    # 事件来源 actor 身份。
    source: Required[Literal["claude", "codex", "verifier", "release", "orchestrator"]]
    # 原始摄取 ID（来自 PreparedEventV2）。
    ingestEventId: Required[str]
    # 具体事件类型（§10.1）。model.summary 必带 summaryOrigin=provider_public + providerEventId + sanitizedProviderFrameRef + sanitizedProviderFrameDigest。
    eventType: Required[Literal["process.started", "stream.segment.committed", "stream.terminated", "model.summary", "orchestrator.objective", "tool.call", "tool.result", "state.changed"]]
    # Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。
    providerEventId: Required[Optional[str]]
    # Provider 来源端序号（用于去重与对齐）。
    sourceSeq: Required[int]
    # Provider stream 稳定 ID（stream.* 事件必填；非 stream 事件 null）。
    streamId: Required[Optional[str]]
    # 所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。
    preparedBatchId: Required[str]
    # 所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。
    batchOrdinal: Required[int]
    # 源传输 span：内存中以原始传输字节精确计数。credential/secret/PII 仅存 providerEventId/sourceSeq 与诚实的 mappingPrecision（byte|field|frame|none），精确字节起止不能进普通事件。
    sourceTransportSpan: Required[dict[str, Any]]
    # 脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。
    sanitizedStreamSpan: Required[dict[str, Any]]
    # RFC3339 接收 wall time（仅用于显示，不能参与排序）。
    wallTime: Required[str]
    # 单调时钟纳秒。
    monotonicTimeNs: Required[int]
    # 完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。
    ingestedAt: Required[str]
    # Provider CLI 上报版本。
    providerVersion: Required[str]
    # Adapter 自身 semver 版本。
    adapterVersion: Required[str]
    # 受管进程身份（§10.1）：用于 RUNNING 真实性校验（PID/Job Object/WSL/container exec）。
    processIdentity: Required[dict[str, Any]]
    # 事件负载（已脱敏，未脱敏原文不得落盘）。
    payload: Required[dict[str, Any]]
    # 已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃。model.summary 必填，其它 null。
    sanitizedProviderFrameDigest: Required[Optional[str]]
    # 脱敏后 payload 的 JCS 摘要。
    payloadDigest: Required[str]
    # 前驱事件 eventDigest（genesis 用 sha256:<64 个 0> 固定 predecessor）。
    previousEventDigest: Required[str]
    # 本事件 SHA-256(JCS(去除 eventDigest 字段后完整对象))。previousEventDigest 仍参与计算。
    eventDigest: Required[str]
    # 本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。
    redactions: Required[list[dict[str, Any]]]
    # redactions 列表内容的 SHA-256 摘要。
    redactionManifestDigest: Required[str]

class IpcEnvelope(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 信封唯一 ID（防重放）
    envelopeId: Required[str]
    # 消息类型
    messageType: Required[Literal["CONTROL_COMMAND", "STATUS_UPDATE", "EVENT_BATCH", "HEARTBEAT", "ACK", "ERROR", "RUNNER_PROTOCOL"]]
    # 发送方进程身份（含 SID）
    senderId: Required[str]
    # 接收方进程身份
    recipientId: Required[str]
    # 随机 nonce，防重放攻击
    nonce: Required[str]
    # 关联 ID，用于请求-响应匹配
    correlationId: str
    # 消息负载（类型由 messageType 决定）
    payload: Required[dict[str, Any]]
    # 负载内容摘要，用于完整性验证
    payloadDigest: str
    # 发送时间
    sentAt: Required[str]

class RunnerProtocol(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 协议消息唯一 ID
    messageId: Required[str]
    # 协议消息类型
    protocolMessageType: Required[Literal["START", "INSPECT", "INTERRUPT", "KILL", "STATUS", "COMPLETED", "FAILED", "RECONCILING", "HEARTBEAT"]]
    # Attempt 唯一 ID（每次派发不可复用）
    attemptId: Required[str]
    # 执行者唯一标识
    executorId: Required[str]
    # Fencing token，严格单调递增
    fencingToken: Required[int]
    # 控制 epoch
    controlEpoch: Required[int]
    # 进程启动身份（containerId/execId/PID等）
    processStartIdentity: dict[str, Any]
    # 消息负载（按 protocolMessageType 定义结构）
    payload: Required[dict[str, Any]]
    # 消息时间戳
    timestamp: Required[str]
