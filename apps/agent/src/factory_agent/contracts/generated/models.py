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
    "PREPARED_EVENT_V2_SCHEMA_SOURCE_SHA256",
    "PREPARED_EVENT_V2_SCHEMA_JSON",
    "PREPARED_EVENT_V2_SCHEMA_JSON_SHA256",
    "DURABLE_EVENT_V2_SCHEMA_SOURCE_SHA256",
    "DURABLE_EVENT_V2_SCHEMA_JSON",
    "DURABLE_EVENT_V2_SCHEMA_JSON_SHA256",
    "PREPARED_BATCH_V2_SCHEMA_SOURCE_SHA256",
    "PREPARED_BATCH_V2_SCHEMA_JSON",
    "PREPARED_BATCH_V2_SCHEMA_JSON_SHA256",
    "AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_SOURCE_SHA256",
    "AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON",
    "AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON_SHA256",
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
    "AuthoritativeStateEventV1",
    "IpcEnvelope",
    "RunnerProtocol",
]

# 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。
RUN_SPEC_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:8fa7cf095a24de2f1c823385a34a3786b2dc2c7ce409de732009f5f6e5e50883"
RUN_SPEC_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/run-spec.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"规划层生成的不可变、版本化 RunSpec：完整语义文档（Master Spec §8）。semanticPlanHash 从本文档的语义字段投影派生；写入后不可修改，修复只能新建子版本。","properties":{"acceptanceCriteria":{"description":"验收条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"assumptions":{"description":"规划假设列表","items":{"minLength":1,"type":"string"},"type":"array"},"constraints":{"description":"约束条件列表","items":{"minLength":1,"type":"string"},"type":"array"},"createdAt":{"description":"RunSpec 创建时间","format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"goal":{"description":"用户目标的结构化表达","minLength":1,"type":"string"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node-capability-map 版本标识","minLength":1,"type":"string"},"parentRevisionId":{"description":"父 PlanRevision ID；genesis 修订为 null","type":["string","null"]},"planRevisionDigest":{"description":"完整不可变修订记录摘要（写入 PlanRevision 后填入）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"repository":{"additionalProperties":false,"description":"仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）","properties":{"baseBranch":{"description":"基础分支名","minLength":1,"type":"string"},"baseCommit":{"allOf":[{"not":{"const":"0000000000000000000000000000000000000000"}}],"description":"bootstrap 完成后可信 base commit 的 40 位小写完整 SHA","pattern":"^[0-9a-f]{40}$","type":"string"},"mode":{"description":"仓库模式","enum":["existing","new"],"type":"string"},"root":{"description":"规范化后的 D 盘仓库根路径","minLength":1,"type":"string"}},"required":["mode","root","baseBranch","baseCommit"],"type":"object"},"riskProfile":{"additionalProperties":false,"description":"风险画像（§8：level/reasons）","properties":{"level":{"description":"风险等级","enum":["low","medium","high","critical"],"type":"string"},"reasons":{"description":"风险原因列表","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["level","reasons"],"type":"object"},"schemaVersion":{"description":"RunSpec schema 版本号","minimum":1,"type":"integer"},"scope":{"additionalProperties":false,"description":"范围声明（§8：include/exclude）","properties":{"exclude":{"description":"排除范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"},"include":{"description":"纳入范围的路径/模块","items":{"minLength":1,"type":"string"},"type":"array"}},"required":["include","exclude"],"type":"object"},"semanticPlanHash":{"description":"语义计划哈希（跨修订版本稳定，从语义字段投影派生）","pattern":"^sha256:[0-9a-f]{64}$","type":"string"},"specRevision":{"description":"同一 taskId 下的规格修订序号","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage-capability-map 版本标识","minLength":1,"type":"string"},"targetStage":{"description":"目标终点阶段（Master Spec §6.1 权威 6 阶段）","enum":["DESIGN_APPROVED","CODEX_APPROVED","PR_READY","MERGED","STAGING_ACCEPTED","PRODUCTION_ACCEPTED"],"type":"string"},"taskId":{"description":"关联任务 ID","minLength":1,"type":"string"},"workPlan":{"additionalProperties":false,"description":"工作计划 DAG（§8：dagVersion/nodes/barriers）","properties":{"barriers":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"}},"required":["businessPhase"],"type":"object"}]}],"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"passPredicateId":{"minLength":1,"type":"string"},"requiredNodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"},"settleTimeoutMs":{"minimum":1,"type":"integer"}},"required":["businessPhase","barrierOrdinal","requiredNodeIds","settleTimeoutMs","passPredicateId"],"type":"object"},"minItems":1,"type":"array"},"dagVersion":{"description":"DAG 结构版本","minimum":1,"type":"integer"},"nodes":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"},"nodeType":{"const":"PLAN"}},"required":["nodeType","businessPhase"],"type":"object"}]}],"description":"DAG 工作节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"barrierOrdinal":{"minimum":0,"type":"integer"},"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"dependsOn":{"items":{"minLength":1,"type":"string"},"type":"array"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"required":{"type":"boolean"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"},"retryPolicyId":{"minLength":1,"type":"string"},"sideEffectClass":{"minLength":1,"type":"string"},"successPredicateId":{"minLength":1,"type":"string"},"timeoutMs":{"minimum":1,"type":"integer"}},"required":["logicalNodeId","businessPhase","barrierOrdinal","nodeType","required","dependsOn","sideEffectClass","requiredArtifacts","successPredicateId","timeoutMs","retryPolicyId"],"type":"object"},"minItems":1,"type":"array"}},"required":["dagVersion","nodes","barriers"],"type":"object"}},"required":["schemaVersion","taskId","goal","assumptions","scope","constraints","acceptanceCriteria","targetStage","repository","workPlan","riskProfile","nodeCapabilityMapVersion","stageCapabilityMapVersion","intentAuthorizationId","semanticPlanHash"],"title":"RunSpec","type":"object"}'
RUN_SPEC_SCHEMA_JSON_SHA256: Final[str] = "sha256:0a63d2dae80c298d85bdb5f2ea119669d6a7c6aca8e954f07c6cf2a8261f7841"
PLAN_REVISION_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:fc9acfd58b9e41b12dee1baaa63d05b0ce08227afd99419bf987cac1abdbe035"
PLAN_REVISION_SCHEMA_JSON: Final[str] = '{"$defs":{"sha256Digest":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"}},"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"}},"required":["businessPhase"],"type":"object"}]}],"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"minItems":1,"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"},"nodeType":{"const":"PLAN"}},"required":["nodeType","businessPhase"],"type":"object"}]}],"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"minItems":1,"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionDigest":{"allOf":[{"$ref":"#/$defs/sha256Digest"}],"description":"本修订版本内容摘要","type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"allOf":[{"$ref":"#/$defs/sha256Digest"}],"description":"语义计划哈希，跨修订版本稳定","type":"string"},"signature":{"minLength":1,"type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","planRevisionDigest","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}'
PLAN_REVISION_SCHEMA_JSON_SHA256: Final[str] = "sha256:859b4d8f5f18b5b388ff836789f526deb2358a81c9a4baeeef3f04ea063b8a45"
# 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。
PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON: Final[str] = '{"$defs":{"sha256Digest":{"pattern":"^sha256:[0-9a-f]{64}$","type":"string"}},"$id":"https://factory.local/contracts/schemas/plan-revision.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。","properties":{"barriers":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"}},"required":["businessPhase"],"type":"object"}]}],"description":"阶段屏障列表","items":{"additionalProperties":false,"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]},"properties":{"barrierId":{"minLength":1,"type":"string"},"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"nodeIds":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["barrierId","businessPhase","nodeIds"],"type":"object"},"minItems":1,"type":"array"},"createdAt":{"format":"date-time","pattern":"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$","type":"string"},"dagVersion":{"description":"DAG 版本号（§11 plan_revisions.dag_version）","minimum":1,"type":"integer"},"intentAuthorizationId":{"description":"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）","minLength":1,"type":"string"},"nodeCapabilityMapVersion":{"description":"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）","minLength":1,"type":"string"},"nodes":{"allOf":[{"items":[{"properties":{"businessPhase":{"const":"PLANNING"},"nodeType":{"const":"PLAN"}},"required":["nodeType","businessPhase"],"type":"object"}]}],"description":"DAG 节点列表","items":{"additionalProperties":false,"allOf":[{"not":{"properties":{"nodeType":{"const":"BOOTSTRAP_REPOSITORY"}},"required":["nodeType"]}},{"not":{"properties":{"businessPhase":{"enum":["BOOTSTRAPPING","BOOTSTRAPPING_REPOSITORY"]}},"required":["businessPhase"]}}],"properties":{"businessPhase":{"enum":["PLANNING","DESIGN_REVIEWING","PREPARING_WORKSPACE","IMPLEMENTING","VERIFYING","CODE_REVIEWING","PUBLISHING_PR","MERGING","BUILDING_ARTIFACT","DEPLOYING_STAGING","ACCEPTING_STAGING","DEPLOYING_PRODUCTION","ACCEPTING_PRODUCTION","ROLLING_BACK","FINALIZING"],"type":"string"},"dependencies":{"items":{"minLength":1,"type":"string"},"type":"array"},"gate":{"minLength":1,"type":"string"},"hasSideEffect":{"type":"boolean"},"logicalNodeId":{"minLength":1,"type":"string"},"nodeType":{"enum":["PLAN","DESIGN_REVIEW","BOOTSTRAP_REPOSITORY","IMPLEMENT","VERIFY","CODE_REVIEW","ATTEST_REVIEW","PUBLISH_PR","MERGE","BUILD_ARTIFACT","DEPLOY_STAGING","ACCEPT_STAGING","DEPLOY_PRODUCTION","ACCEPT_PRODUCTION","ROLLBACK","RECONCILE_TARGET","RESTORE_DRILL"],"type":"string"},"requiredArtifacts":{"items":{"minLength":1,"type":"string"},"type":"array"}},"required":["logicalNodeId","nodeType","businessPhase","dependencies","hasSideEffect","requiredArtifacts"],"type":"object"},"minItems":1,"type":"array"},"parentRevisionId":{"description":"父修订版本 ID（范围内重规划时设置）","minLength":1,"type":"string"},"planRevisionId":{"description":"本修订版本唯一 ID","minLength":1,"type":"string"},"semanticPlanHash":{"allOf":[{"$ref":"#/$defs/sha256Digest"}],"description":"语义计划哈希，跨修订版本稳定","type":"string"},"specRevision":{"description":"规格修订号，单调递增（§11 plan_revisions.spec_revision）","minimum":1,"type":"integer"},"stageCapabilityMapVersion":{"description":"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）","minLength":1,"type":"string"},"stageMaps":{"additionalProperties":false,"description":"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）","properties":{"CODEX_APPROVED":{"items":{"type":"string"},"type":"array"},"DESIGN_APPROVED":{"items":{"type":"string"},"type":"array"},"MERGED":{"items":{"type":"string"},"type":"array"},"PRODUCTION_ACCEPTED":{"items":{"type":"string"},"type":"array"},"PR_READY":{"items":{"type":"string"},"type":"array"},"STAGING_ACCEPTED":{"items":{"type":"string"},"type":"array"}},"type":"object"},"taskId":{"description":"所属任务 ID（§11 plan_revisions.task_id）","minLength":1,"type":"string"}},"required":["planRevisionId","taskId","specRevision","intentAuthorizationId","semanticPlanHash","dagVersion","nodeCapabilityMapVersion","stageCapabilityMapVersion","nodes","barriers","stageMaps","createdAt"],"title":"PlanRevision","type":"object"}'
PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256: Final[str] = "sha256:b0ec793126618633ddde4986c388a66d5db345d9c5dbd13ce3384689174b3a28"
# 事件物化器输入/输出均使用本组权威 schema，禁止以手写字段表替代。
PREPARED_EVENT_V2_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:2a353f3a87e9c8262b584e3925a0d1055cf0b37a386b06c8ded98924e2eb22fd"
PREPARED_EVENT_V2_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/prepared-event.v2.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"allOf":[{"if":{"properties":{"eventType":{"enum":["stream.segment.committed","stream.terminated"]}},"required":["eventType"]},"then":{"properties":{"streamId":{"minLength":1,"type":"string"}},"required":["streamId"]}}],"description":"Adapter 自描述的预备事件。它只携带 DurableEventV2 的共享输入字段，不含任何单写者/协调器分配字段；非 wire 的 MaterializationInput 另行补入 preparedBatchId、batchOrdinal、runSeq 与 durabilityClass。","properties":{"adapterVersion":{"description":"Adapter 自身 semver 版本。","type":"string"},"attemptId":{"description":"所属 Attempt ID。","minLength":1,"type":"string"},"eventType":{"description":"Adapter 只能准备已明确支持的具体事件类型。state.changed 仅由 AuthoritativeStateEventV1 产生；model.summary 的 Provider frameRef 形状尚未冻结，本轮 Adapter/物化器 fail-closed。","enum":["process.started","stream.segment.committed","stream.terminated","orchestrator.objective","tool.call","tool.result"],"type":"string"},"ingestEventId":{"description":"原始摄取 ID（来自 PreparedEventV2）。","minLength":1,"type":"string"},"ingestedAt":{"description":"完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。","format":"date-time","type":"string"},"monotonicTimeNs":{"description":"单调时钟纳秒。","maximum":9007199254740991,"minimum":0,"type":"integer"},"payload":{"description":"事件负载（已脱敏，未脱敏原文不得落盘）。","type":"object"},"processIdentity":{"additionalProperties":false,"allOf":[{"if":{"properties":{"runtime":{"const":"local-windows"}},"required":["runtime"]},"then":{"properties":{"containerId":{"type":"null"},"imageDigest":{"type":"null"},"jobObjectId":{"minLength":1,"type":"string"},"wslDistro":{"type":"null"}}}},{"if":{"properties":{"runtime":{"const":"wsl-docker"}},"required":["runtime"]},"then":{"properties":{"containerId":{"minLength":1,"type":"string"},"imageDigest":{"minLength":1,"type":"string"},"jobObjectId":{"type":"null"},"wslDistro":{"minLength":1,"type":"string"}}}}],"description":"受管进程身份（§10.1）：runtime 仅为 local-windows 或 wsl-docker，并按分支提供完整进程证据。","properties":{"containerId":{"description":"容器 ID（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]},"executableDigest":{"description":"可执行文件 SHA-256（完整）。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"executorId":{"description":"执行者稳定 ID。","minLength":1,"type":"string"},"hostId":{"description":"主机指纹。","minLength":1,"type":"string"},"imageDigest":{"description":"容器镜像 digest（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]},"jobObjectId":{"description":"Windows Job Object ID（local-windows 必填；wsl-docker 必须为 null）。","minLength":1,"type":["string","null"]},"pid":{"description":"进程 PID。","maximum":9007199254740991,"minimum":0,"type":"integer"},"processStartTime":{"description":"进程启动 wall time。","format":"date-time","type":"string"},"runtime":{"description":"运行时类型。","enum":["local-windows","wsl-docker"],"type":"string"},"wslDistro":{"description":"WSL distro 名（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]}},"required":["executorId","hostId","runtime","executableDigest","pid","processStartTime","jobObjectId","wslDistro","containerId","imageDigest"],"type":"object"},"providerEventId":{"description":"Provider 事件 ID；未关联 Provider 事件时为 null。","type":["string","null"]},"providerVersion":{"description":"Provider CLI 上报版本。","type":"string"},"redactions":{"description":"本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。","items":{"additionalProperties":false,"properties":{"byteRange":{"additionalProperties":false,"properties":{"endExclusive":{"maximum":9007199254740991,"minimum":0,"type":"integer"},"start":{"maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["start","endExclusive"],"type":"object"},"patternId":{"description":"命中的脱敏规则 ID。","minLength":1,"type":"string"},"replacement":{"description":"替换值（[REDACTED] / [REDACTED:uncertain] / 类型化标记）。","pattern":"^\\\\[REDACTED(?::[a-z0-9._-]+)?\\\\]$","type":"string"}},"required":["patternId","byteRange","replacement"],"type":"object"},"type":"array"},"runId":{"description":"所属 Run ID。","minLength":1,"type":"string"},"sanitizedProviderFrameDigest":{"description":"已脱敏 Provider frame 的 SHA-256；原始 frame 在脱敏后立即丢弃。","pattern":"^sha256:[a-f0-9]{64}$","type":["string","null"]},"sanitizedStreamSpan":{"additionalProperties":false,"description":"脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。","properties":{"endExclusive":{"description":"脱敏 segment 内字节偏移（不含）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"segmentId":{"description":"自描述脱敏 stream segment ID（指向 stream_segments 表）。","minLength":1,"type":"string"},"start":{"description":"脱敏 segment 内字节偏移（不含）。","maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["segmentId","start","endExclusive"],"type":"object"},"schemaVersion":{"const":2,"description":"事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。","type":"integer"},"source":{"description":"事件来源 actor 身份。","enum":["claude","codex","verifier","release","orchestrator"],"type":"string"},"sourceSeq":{"description":"Provider 来源端序号（用于去重与对齐）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"sourceTransportSpan":{"additionalProperties":false,"allOf":[{"if":{"properties":{"coordinate":{"enum":["provider_transport_bytes","provider_transport_chars"]},"mappingPrecision":{"const":"byte"}},"required":["coordinate","mappingPrecision"]},"then":{"properties":{"endExclusive":{"type":"integer"},"start":{"type":"integer"}}}},{"if":{"properties":{"coordinate":{"enum":["provider_transport_bytes","provider_transport_chars"]},"mappingPrecision":{"enum":["field","frame","none"]}},"required":["coordinate","mappingPrecision"]},"then":{"properties":{"endExclusive":{"type":"null"},"start":{"type":"null"}}}},{"if":{"properties":{"coordinate":{"const":"none"}},"required":["coordinate"]},"then":{"properties":{"endExclusive":{"type":"null"},"mappingPrecision":{"const":"none"},"start":{"type":"null"}}}}],"description":"源传输 span：coordinate 表示 Provider 传输坐标轴，mappingPrecision 表示映射精度，两者相互独立。transport 坐标仅在 byte 精度下携带非空范围；field/frame/none 精度不携带 offset。","properties":{"coordinate":{"description":"Provider 原始传输坐标轴；不得使用 mappingPrecision 的 byte/field/frame 名称冒充坐标。","enum":["provider_transport_bytes","provider_transport_chars","none"],"type":"string"},"endExclusive":{"description":"transport 坐标且 mappingPrecision=byte 时的结束偏移（不含）；其他组合必须为 null。","maximum":9007199254740991,"minimum":0,"type":["integer","null"]},"mappingPrecision":{"description":"精度（§10.4）：credential 默认不高于 field/frame；stream.segment.* 一般为 byte 或 frame。","enum":["byte","field","frame","none"],"type":"string"},"start":{"description":"transport 坐标且 mappingPrecision=byte 时的起始偏移；其他组合必须为 null。","maximum":9007199254740991,"minimum":0,"type":["integer","null"]}},"required":["coordinate","start","endExclusive","mappingPrecision"],"type":"object"},"stepId":{"description":"所属 Step ID。","minLength":1,"type":"string"},"streamId":{"description":"Provider stream 稳定 ID；stream.segment.committed/stream.terminated 必须为非空字符串。","type":["string","null"]},"taskId":{"description":"所属任务 ID。","minLength":1,"type":"string"},"wallTime":{"description":"RFC3339 接收 wall time（仅用于显示，不能参与排序）。","format":"date-time","type":"string"}},"required":["schemaVersion","taskId","runId","stepId","attemptId","source","ingestEventId","eventType","providerEventId","sourceSeq","streamId","sourceTransportSpan","sanitizedStreamSpan","wallTime","monotonicTimeNs","ingestedAt","providerVersion","adapterVersion","processIdentity","payload","sanitizedProviderFrameDigest","redactions"],"title":"PreparedEventV2","type":"object"}'
PREPARED_EVENT_V2_SCHEMA_JSON_SHA256: Final[str] = "sha256:a4485f9dd57423a13435c8f57be74f50ec2dfdf6eeb6982d53e8f348c8719f52"
DURABLE_EVENT_V2_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:1a7294781ae69c646866a928dac6a8301de0f3693050e3d1908e4574709c1348"
DURABLE_EVENT_V2_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/durable-event.v2.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"allOf":[{"if":{"properties":{"eventType":{"enum":["stream.segment.committed","stream.terminated"]}},"required":["eventType"]},"then":{"properties":{"streamId":{"minLength":1,"type":"string"}},"required":["streamId"]}},{"if":{"properties":{"eventType":{"const":"state.changed"}},"required":["eventType"]},"then":{"properties":{"durabilityClass":{"const":"authoritative_state"},"source":{"const":"orchestrator"}}}},{"if":{"properties":{"durabilityClass":{"const":"authoritative_state"}},"required":["durabilityClass"]},"then":{"properties":{"eventType":{"const":"state.changed"},"source":{"const":"orchestrator"}}}}],"description":"耐久化后的事件（Master Spec §10.1 字段集完整版）。事件链使用 previousEventDigest 单链：genesis 事件 previousEventDigest = sha256:<64 个 0>；eventDigest = SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后的完整对象))，previousEventDigest 仍参与计算。additionalProperties:false 拒绝 schema 未声明字段混入 v2 hash。","properties":{"adapterVersion":{"description":"Adapter 自身 semver 版本。","type":"string"},"attemptId":{"description":"所属 Attempt ID。","minLength":1,"type":"string"},"batchOrdinal":{"description":"所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"durabilityClass":{"description":"耐久化等级（§10.4）：authoritative_state=内部权威 state.changed/lease/授权；side_effect_receipt=外部副作用 receipt；provider_source=Provider semantic/public frame；derived=UI summary/指标等可重建派生。","enum":["authoritative_state","side_effect_receipt","provider_source","derived"],"type":"string"},"eventDigest":{"description":"本事件 SHA-256(JCS(去除 eventDigest 字段后完整对象))。previousEventDigest 仍参与计算。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"eventId":{"description":"物化器分配的全局唯一事件 ID（evt_<64 位小写十六进制>，SHA-256(JCS([factory-event-id-v2, ingestEventId]))）。崩溃重试得到同一身份。","minLength":1,"type":"string"},"eventType":{"description":"具体事件类型（§10.1）。model.summary 的 Provider frameRef 形状尚未冻结，Adapter/物化器本轮不会接受该输入；本 schema 不臆造 ref 字段。","enum":["process.started","stream.segment.committed","stream.terminated","model.summary","orchestrator.objective","tool.call","tool.result","state.changed"],"type":"string"},"ingestEventId":{"description":"原始摄取 ID（来自 PreparedEventV2）。","minLength":1,"type":"string"},"ingestedAt":{"description":"完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。","format":"date-time","type":"string"},"monotonicTimeNs":{"description":"单调时钟纳秒。","maximum":9007199254740991,"minimum":0,"type":"integer"},"payload":{"description":"事件负载（已脱敏，未脱敏原文不得落盘）。","type":"object"},"payloadDigest":{"description":"脱敏后 payload 的 JCS 摘要。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"preparedBatchId":{"description":"所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。","minLength":1,"type":"string"},"previousEventDigest":{"description":"前驱事件 eventDigest（genesis 用 sha256:<64 个 0> 固定 predecessor）。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"processIdentity":{"additionalProperties":false,"allOf":[{"if":{"properties":{"runtime":{"const":"local-windows"}},"required":["runtime"]},"then":{"properties":{"containerId":{"type":"null"},"imageDigest":{"type":"null"},"jobObjectId":{"minLength":1,"type":"string"},"wslDistro":{"type":"null"}}}},{"if":{"properties":{"runtime":{"const":"wsl-docker"}},"required":["runtime"]},"then":{"properties":{"containerId":{"minLength":1,"type":"string"},"imageDigest":{"minLength":1,"type":"string"},"jobObjectId":{"type":"null"},"wslDistro":{"minLength":1,"type":"string"}}}}],"description":"受管进程身份（§10.1）：runtime 仅为 local-windows 或 wsl-docker，并按分支提供完整进程证据。","properties":{"containerId":{"description":"容器 ID（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]},"executableDigest":{"description":"可执行文件 SHA-256（完整）。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"executorId":{"description":"执行者稳定 ID。","minLength":1,"type":"string"},"hostId":{"description":"主机指纹。","minLength":1,"type":"string"},"imageDigest":{"description":"容器镜像 digest（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]},"jobObjectId":{"description":"Windows Job Object ID（local-windows 必填；wsl-docker 必须为 null）。","minLength":1,"type":["string","null"]},"pid":{"description":"进程 PID。","maximum":9007199254740991,"minimum":0,"type":"integer"},"processStartTime":{"description":"进程启动 wall time。","format":"date-time","type":"string"},"runtime":{"description":"运行时类型。","enum":["local-windows","wsl-docker"],"type":"string"},"wslDistro":{"description":"WSL distro 名（wsl-docker 必填；local-windows 必须为 null）。","minLength":1,"type":["string","null"]}},"required":["executorId","hostId","runtime","executableDigest","pid","processStartTime","jobObjectId","wslDistro","containerId","imageDigest"],"type":"object"},"providerEventId":{"description":"Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。","type":["string","null"]},"providerVersion":{"description":"Provider CLI 上报版本。","type":"string"},"redactionManifestDigest":{"description":"redactions 列表内容的 SHA-256 摘要。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"redactions":{"description":"本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。","items":{"additionalProperties":false,"properties":{"byteRange":{"additionalProperties":false,"properties":{"endExclusive":{"maximum":9007199254740991,"minimum":0,"type":"integer"},"start":{"maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["start","endExclusive"],"type":"object"},"patternId":{"description":"命中的脱敏规则 ID。","minLength":1,"type":"string"},"replacement":{"description":"替换值（[REDACTED] / [REDACTED:uncertain] / 类型化标记）。","pattern":"^\\\\[REDACTED(?::[a-z0-9._-]+)?\\\\]$","type":"string"}},"required":["patternId","byteRange","replacement"],"type":"object"},"type":"array"},"runId":{"description":"所属 Run ID。","minLength":1,"type":"string"},"runSeq":{"description":"单 Run 内诊断序号（不能跨 Run 唯一）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"sanitizedProviderFrameDigest":{"description":"已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃；model.summary 的完整引用合同尚未冻结。","pattern":"^sha256:[a-f0-9]{64}$","type":["string","null"]},"sanitizedStreamSpan":{"additionalProperties":false,"description":"脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。","properties":{"endExclusive":{"description":"脱敏 segment 内字节偏移（不含）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"segmentId":{"description":"自描述脱敏 stream segment ID（指向 stream_segments 表）。","minLength":1,"type":"string"},"start":{"description":"脱敏 segment 内字节偏移（不含）。","maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["segmentId","start","endExclusive"],"type":"object"},"schemaVersion":{"const":2,"description":"事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。","type":"integer"},"source":{"description":"事件来源 actor 身份。","enum":["claude","codex","verifier","release","orchestrator"],"type":"string"},"sourceSeq":{"description":"Provider 来源端序号（用于去重与对齐）。","maximum":9007199254740991,"minimum":0,"type":"integer"},"sourceTransportSpan":{"additionalProperties":false,"allOf":[{"if":{"properties":{"coordinate":{"enum":["provider_transport_bytes","provider_transport_chars"]},"mappingPrecision":{"const":"byte"}},"required":["coordinate","mappingPrecision"]},"then":{"properties":{"endExclusive":{"type":"integer"},"start":{"type":"integer"}}}},{"if":{"properties":{"coordinate":{"enum":["provider_transport_bytes","provider_transport_chars"]},"mappingPrecision":{"enum":["field","frame","none"]}},"required":["coordinate","mappingPrecision"]},"then":{"properties":{"endExclusive":{"type":"null"},"start":{"type":"null"}}}},{"if":{"properties":{"coordinate":{"const":"none"}},"required":["coordinate"]},"then":{"properties":{"endExclusive":{"type":"null"},"mappingPrecision":{"const":"none"},"start":{"type":"null"}}}}],"description":"源传输 span：coordinate 表示 Provider 传输坐标轴，mappingPrecision 表示映射精度，两者相互独立。transport 坐标仅在 byte 精度下携带非空范围；field/frame/none 精度不携带 offset。","properties":{"coordinate":{"description":"Provider 原始传输坐标轴；不得使用 mappingPrecision 的 byte/field/frame 名称冒充坐标。","enum":["provider_transport_bytes","provider_transport_chars","none"],"type":"string"},"endExclusive":{"description":"transport 坐标且 mappingPrecision=byte 时的结束偏移（不含）；其他组合必须为 null。","maximum":9007199254740991,"minimum":0,"type":["integer","null"]},"mappingPrecision":{"description":"精度（§10.4）：credential 默认不高于 field/frame；stream.segment.* 一般为 byte 或 frame。","enum":["byte","field","frame","none"],"type":"string"},"start":{"description":"transport 坐标且 mappingPrecision=byte 时的起始偏移；其他组合必须为 null。","maximum":9007199254740991,"minimum":0,"type":["integer","null"]}},"required":["coordinate","start","endExclusive","mappingPrecision"],"type":"object"},"stepId":{"description":"所属 Step ID。","minLength":1,"type":"string"},"streamId":{"description":"Provider stream 稳定 ID；stream.segment.committed/stream.terminated 必须为非空字符串。","type":["string","null"]},"taskId":{"description":"所属任务 ID。","minLength":1,"type":"string"},"taskSeq":{"description":"任务内单调递增序号（由物化器分配，CAS 持久）。UI cursor 用 (taskId, taskSeq)。","maximum":9007199254740991,"minimum":0,"type":"integer"},"wallTime":{"description":"RFC3339 接收 wall time（仅用于显示，不能参与排序）。","format":"date-time","type":"string"}},"required":["schemaVersion","durabilityClass","eventId","taskId","taskSeq","runId","runSeq","stepId","attemptId","source","ingestEventId","eventType","providerEventId","sourceSeq","streamId","preparedBatchId","batchOrdinal","sourceTransportSpan","sanitizedStreamSpan","wallTime","monotonicTimeNs","ingestedAt","providerVersion","adapterVersion","processIdentity","payload","sanitizedProviderFrameDigest","payloadDigest","previousEventDigest","eventDigest","redactions","redactionManifestDigest"],"title":"DurableEventV2","type":"object"}'
DURABLE_EVENT_V2_SCHEMA_JSON_SHA256: Final[str] = "sha256:b2d33b08f497017e2b19a4208b73b60ab010eaff89e97d0100ef9335d07c0053"
PREPARED_BATCH_V2_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:4297c7f9854e80ac8e2419856565ec75e32e401485a6705d56d39742e916bc68"
PREPARED_BATCH_V2_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/prepared-batch.v2.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"description":"不可变预备事件批次 manifest（§10.1 + §11）。由不可变 manifest + 一个或多个自描述脱敏 stream segment 组成；不携带可变数据库状态。manifest 至少含 preparedBatchId、按 batchOrdinal 排列的全量稳定 ingestEventId、每个 Task 唯一的提交锚点与 ordinal 范围、segment digest 列表、总事件数/长度、schema/coordinator 版本、完整尾标。跨 Task 事件可交错；同一 Task 至多一个未提交批次。","properties":{"coordinatorVersion":{"description":"coordinator 版本（compatibility manifest 绑定）。","type":"string"},"eventCount":{"description":"总事件数。","maximum":9007199254740991,"minimum":1,"type":"integer"},"firstBatchOrdinal":{"description":"本批首事件 batchOrdinal。","maximum":9007199254740991,"minimum":0,"type":"integer"},"lastBatchOrdinal":{"description":"本批末事件 batchOrdinal。","maximum":9007199254740991,"minimum":0,"type":"integer"},"oldestIngestedAt":{"description":"本批最旧 ingestedAt。","format":"date-time","type":"string"},"orderedIngestIds":{"description":"按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。","items":{"minLength":1,"type":"string"},"minItems":1,"type":"array","uniqueItems":true},"payloadBytes":{"description":"总 payload 字节数。","maximum":9007199254740991,"minimum":0,"type":"integer"},"perTaskExpectedHeads":{"description":"每个 Task 唯一的 `expectedTaskSeq` / `expectedEventDigest` 锚点与批内 ordinal 范围（§11 ingest_batch_task_heads）；`expectedEventDigest` 显式可空。","items":{"additionalProperties":false,"allOf":[{"if":{"properties":{"expectedTaskSeq":{"type":"null"}},"required":["expectedTaskSeq"]},"then":{"properties":{"expectedEventDigest":{"type":"null"}}}},{"if":{"properties":{"expectedEventDigest":{"type":"null"}},"required":["expectedEventDigest"]},"then":{"properties":{"expectedTaskSeq":{"type":"null"}}}}],"properties":{"expectedEventDigest":{"description":"`expectedEventDigest` 显式可空：genesis 为 null，否则仅允许该 Task 锚点的 sha256；不接受空字符串。","pattern":"^sha256:[a-f0-9]{64}$","type":["string","null"]},"expectedTaskSeq":{"description":"`expectedTaskSeq` 是该 Task 的 CAS 重放基线；genesis 显式为 null。","maximum":9007199254740991,"minimum":0,"type":["integer","null"]},"firstBatchOrdinal":{"description":"该 Task 在本批内的首事件 batchOrdinal。","maximum":9007199254740991,"minimum":0,"type":"integer"},"lastBatchOrdinal":{"description":"该 Task 在本批内的末事件 batchOrdinal。","maximum":9007199254740991,"minimum":0,"type":"integer"},"taskId":{"minLength":1,"type":"string"}},"required":["taskId","expectedTaskSeq","expectedEventDigest","firstBatchOrdinal","lastBatchOrdinal"],"type":"object"},"minItems":1,"type":"array"},"preparedAt":{"description":"本批 prepared 完成时间。","format":"date-time","type":"string"},"preparedBatchId":{"description":"批次唯一稳定 ID。","minLength":1,"type":"string"},"schemaVersion":{"const":2,"description":"PreparedBatch schema 版本。","type":"integer"},"segmentDigests":{"description":"segment digest 列表。","items":{"additionalProperties":false,"allOf":[{"if":{"properties":{"mappingPrecision":{"const":"byte"}},"required":["mappingPrecision"]},"then":{"properties":{"sourceSpan":{"type":"object"}}}},{"if":{"properties":{"mappingPrecision":{"enum":["field","frame","none"]}},"required":["mappingPrecision"]},"then":{"properties":{"sourceSpan":{"type":"null"}}}}],"description":"stream segment 自描述结构（§10.4）。header/footer 包含 batch ID/manifest digest、Task/Run/Attempt/stream 身份、所含 ordinal 范围、按 mappingPrecision 最小化的来源跨度与精确脱敏跨度、内容/redaction manifest digest、adapter 版本、来源水位和完整尾标。","properties":{"adapterVersion":{"type":"string"},"attemptId":{"minLength":1,"type":"string"},"completeFooterDigest":{"pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"contentDigest":{"pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"mappingPrecision":{"enum":["byte","field","frame","none"],"type":"string"},"ordinalRange":{"additionalProperties":false,"properties":{"first":{"maximum":9007199254740991,"minimum":0,"type":"integer"},"lastExclusive":{"maximum":9007199254740991,"minimum":1,"type":"integer"}},"required":["first","lastExclusive"],"type":"object"},"predecessorDigest":{"pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"redactionManifestDigest":{"pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"runId":{"minLength":1,"type":"string"},"sanitizedSpan":{"additionalProperties":false,"properties":{"endExclusive":{"maximum":9007199254740991,"minimum":0,"type":"integer"},"start":{"maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["start","endExclusive"],"type":"object"},"segmentId":{"minLength":1,"type":"string"},"sourceSpan":{"anyOf":[{"additionalProperties":false,"properties":{"endExclusive":{"maximum":9007199254740991,"minimum":0,"type":"integer"},"start":{"maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["start","endExclusive"],"type":"object"},{"type":"null"}]},"streamId":{"type":["string","null"]},"taskId":{"minLength":1,"type":"string"}},"required":["segmentId","taskId","runId","attemptId","streamId","ordinalRange","mappingPrecision","sourceSpan","sanitizedSpan","contentDigest","redactionManifestDigest","predecessorDigest","completeFooterDigest","adapterVersion"],"type":"object"},"minItems":1,"type":"array"},"writerEpoch":{"description":"claim 时的 writer epoch（用于竞争检测）。","maximum":9007199254740991,"minimum":0,"type":"integer"}},"required":["preparedBatchId","writerEpoch","orderedIngestIds","perTaskExpectedHeads","segmentDigests","eventCount","payloadBytes","firstBatchOrdinal","lastBatchOrdinal","oldestIngestedAt","preparedAt","schemaVersion","coordinatorVersion"],"title":"PreparedBatchV2","type":"object"}'
PREPARED_BATCH_V2_SCHEMA_JSON_SHA256: Final[str] = "sha256:fd278e7ade3ee125396ccbc4f4097c7a6f47209a096da4a15c40cf907ac4e7eb"
AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_SOURCE_SHA256: Final[str] = "sha256:4738c50f0f8d8d3b22bab22dbdf8d591a5dbc019ca75f0eaec3c925ae453df9f"
AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON: Final[str] = '{"$id":"https://factory.local/contracts/schemas/authoritative-state-event.v1.schema.json","$schema":"http://json-schema.org/draft-07/schema#","additionalProperties":false,"allOf":[{"if":{"properties":{"aggregateType":{"const":"TASK"}},"required":["aggregateType"]},"then":{"properties":{"attemptId":{"type":"null"},"runId":{"type":"null"},"scope":{"const":"TASK"},"stepId":{"type":"null"}}}},{"if":{"properties":{"aggregateType":{"const":"RUN"}},"required":["aggregateType"]},"then":{"properties":{"attemptId":{"type":"null"},"runId":{"minLength":1,"type":"string"},"scope":{"const":"RUN"},"stepId":{"type":"null"}}}},{"if":{"properties":{"aggregateType":{"const":"PHASE_BARRIER"}},"required":["aggregateType"]},"then":{"properties":{"attemptId":{"type":"null"},"runId":{"minLength":1,"type":"string"},"scope":{"const":"RUN"},"stepId":{"type":"null"}}}},{"if":{"properties":{"aggregateType":{"const":"STEP"}},"required":["aggregateType"]},"then":{"properties":{"attemptId":{"type":"null"},"runId":{"minLength":1,"type":"string"},"scope":{"const":"STEP"},"stepId":{"minLength":1,"type":"string"}}}},{"if":{"properties":{"aggregateType":{"const":"ATTEMPT"}},"required":["aggregateType"]},"then":{"properties":{"attemptId":{"minLength":1,"type":"string"},"runId":{"minLength":1,"type":"string"},"scope":{"const":"ATTEMPT"},"stepId":{"minLength":1,"type":"string"}}}}],"description":"内部权威 state.changed 事件。它绑定真实工作流 aggregate 与显式可空执行身份；不得由 Adapter PreparedEventV2 伪造。跨字段身份、外表 FK、跨记录 CAS 与唯一键由 Task 1 SQLite 同一 UoW 实施。","properties":{"aggregateId":{"description":"Task 1 UoW 的五路身份合同：TASK -> aggregateId == taskId；RUN -> aggregateId == runId；PHASE_BARRIER -> aggregateId == phase_barriers.barrier_id，且 phase_barriers.run_id == runId；STEP -> aggregateId == stepId；ATTEMPT -> aggregateId == attemptId。Draft7 不能验证该外表 FK 与跨字段相等关系。","minLength":1,"type":"string"},"aggregateType":{"description":"被更新的领域 aggregate 类型。","enum":["TASK","RUN","PHASE_BARRIER","STEP","ATTEMPT"],"type":"string"},"attemptId":{"description":"真实 Attempt 身份；非 Attempt aggregate 时为 null。","minLength":1,"type":["string","null"]},"durabilityClass":{"const":"authoritative_state","description":"必须与领域投影在同一事务提交。","type":"string"},"eventType":{"const":"state.changed","description":"仅承载内部权威状态变化。","type":"string"},"payload":{"description":"已脱敏的权威状态变化载荷。","type":"object"},"payloadDigest":{"description":"payload 的规范化 SHA-256 摘要。","pattern":"^sha256:[a-f0-9]{64}$","type":"string"},"previousStateVersion":{"description":"本次转换读取到的旧版本。","maximum":9007199254740991,"minimum":0,"type":"integer"},"runId":{"description":"真实 Run 身份；Task aggregate 时为 null。","minLength":1,"type":["string","null"]},"schemaVersion":{"const":1,"description":"权威状态事件 schema 版本。","type":"integer"},"scope":{"description":"工作流身份范围；PHASE_BARRIER 使用 RUN scope。","enum":["TASK","RUN","STEP","ATTEMPT"],"type":"string"},"stateEventId":{"description":"追加型状态事件的稳定身份。","minLength":1,"type":"string"},"stateVersion":{"description":"本次转换写入的新版本；必须由 Task 1 同一 UoW 验证为 previousStateVersion + 1。","maximum":9007199254740991,"minimum":1,"type":"integer"},"stepId":{"description":"真实 Step 身份；Task/Run/Barrier aggregate 时为 null。","minLength":1,"type":["string","null"]},"taskId":{"description":"所有 aggregate 均归属一个真实 Task。","minLength":1,"type":"string"}},"required":["schemaVersion","stateEventId","eventType","durabilityClass","taskId","scope","aggregateType","aggregateId","runId","stepId","attemptId","previousStateVersion","stateVersion","payload","payloadDigest"],"title":"AuthoritativeStateEventV1","type":"object","x-persistence":{"enforcedBy":"Task 1 0002_event_store.sql UoW","uniqueKey":["aggregateType","aggregateId","stateVersion"]}}'
AUTHORITATIVE_STATE_EVENT_V1_SCHEMA_JSON_SHA256: Final[str] = "sha256:05c1b7453b41a501464ee6481b018738173fd39b44ccf20d3480f375426b8067"

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
    # 事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。
    schemaVersion: Required[Literal[2]]
    # 所属任务 ID。
    taskId: Required[str]
    # 所属 Run ID。
    runId: Required[str]
    # 所属 Step ID。
    stepId: Required[str]
    # 所属 Attempt ID。
    attemptId: Required[str]
    # 事件来源 actor 身份。
    source: Required[Literal["claude", "codex", "verifier", "release", "orchestrator"]]
    # 原始摄取 ID（来自 PreparedEventV2）。
    ingestEventId: Required[str]
    # Adapter 只能准备已明确支持的具体事件类型。state.changed 仅由 AuthoritativeStateEventV1 产生；model.summary 的 Provider frameRef 形状尚未冻结，本轮 Adapter/物化器 fail-closed。
    eventType: Required[Literal["process.started", "stream.segment.committed", "stream.terminated", "orchestrator.objective", "tool.call", "tool.result"]]
    # Provider 事件 ID；未关联 Provider 事件时为 null。
    providerEventId: Required[Optional[str]]
    # Provider 来源端序号（用于去重与对齐）。
    sourceSeq: Required[int]
    # Provider stream 稳定 ID；stream.segment.committed/stream.terminated 必须为非空字符串。
    streamId: Required[Optional[str]]
    # 源传输 span：coordinate 表示 Provider 传输坐标轴，mappingPrecision 表示映射精度，两者相互独立。transport 坐标仅在 byte 精度下携带非空范围；field/frame/none 精度不携带 offset。
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
    # 受管进程身份（§10.1）：runtime 仅为 local-windows 或 wsl-docker，并按分支提供完整进程证据。
    processIdentity: Required[dict[str, Any]]
    # 事件负载（已脱敏，未脱敏原文不得落盘）。
    payload: Required[dict[str, Any]]
    # 已脱敏 Provider frame 的 SHA-256；原始 frame 在脱敏后立即丢弃。
    sanitizedProviderFrameDigest: Required[Optional[str]]
    # 本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。
    redactions: Required[list[dict[str, Any]]]

class PreparedBatchV2(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 批次唯一稳定 ID。
    preparedBatchId: Required[str]
    # claim 时的 writer epoch（用于竞争检测）。
    writerEpoch: Required[int]
    # 按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。
    orderedIngestIds: Required[list[str]]
    # 每个 Task 唯一的 `expectedTaskSeq` / `expectedEventDigest` 锚点与批内 ordinal 范围（§11 ingest_batch_task_heads）；`expectedEventDigest` 显式可空。
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
    # 具体事件类型（§10.1）。model.summary 的 Provider frameRef 形状尚未冻结，Adapter/物化器本轮不会接受该输入；本 schema 不臆造 ref 字段。
    eventType: Required[Literal["process.started", "stream.segment.committed", "stream.terminated", "model.summary", "orchestrator.objective", "tool.call", "tool.result", "state.changed"]]
    # Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。
    providerEventId: Required[Optional[str]]
    # Provider 来源端序号（用于去重与对齐）。
    sourceSeq: Required[int]
    # Provider stream 稳定 ID；stream.segment.committed/stream.terminated 必须为非空字符串。
    streamId: Required[Optional[str]]
    # 所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。
    preparedBatchId: Required[str]
    # 所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。
    batchOrdinal: Required[int]
    # 源传输 span：coordinate 表示 Provider 传输坐标轴，mappingPrecision 表示映射精度，两者相互独立。transport 坐标仅在 byte 精度下携带非空范围；field/frame/none 精度不携带 offset。
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
    # 受管进程身份（§10.1）：runtime 仅为 local-windows 或 wsl-docker，并按分支提供完整进程证据。
    processIdentity: Required[dict[str, Any]]
    # 事件负载（已脱敏，未脱敏原文不得落盘）。
    payload: Required[dict[str, Any]]
    # 已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃；model.summary 的完整引用合同尚未冻结。
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

class AuthoritativeStateEventV1(TypedDict, total=False):
    """由 generate.py 自动生成，禁止手动修改。"""
    # 权威状态事件 schema 版本。
    schemaVersion: Required[Literal[1]]
    # 追加型状态事件的稳定身份。
    stateEventId: Required[str]
    # 仅承载内部权威状态变化。
    eventType: Required[Literal["state.changed"]]
    # 必须与领域投影在同一事务提交。
    durabilityClass: Required[Literal["authoritative_state"]]
    # 所有 aggregate 均归属一个真实 Task。
    taskId: Required[str]
    # 工作流身份范围；PHASE_BARRIER 使用 RUN scope。
    scope: Required[Literal["TASK", "RUN", "STEP", "ATTEMPT"]]
    # 被更新的领域 aggregate 类型。
    aggregateType: Required[Literal["TASK", "RUN", "PHASE_BARRIER", "STEP", "ATTEMPT"]]
    # Task 1 UoW 的五路身份合同：TASK -> aggregateId == taskId；RUN -> aggregateId == runId；PHASE_BARRIER -> aggregateId == phase_barriers.barrier_id，且 phase_barriers.run_id == runId；STEP -> aggregateId == stepId；ATTEMPT -> aggregateId == attemptId。Draft7 不能验证该外表 FK 与跨字段相等关系。
    aggregateId: Required[str]
    # 真实 Run 身份；Task aggregate 时为 null。
    runId: Required[Optional[str]]
    # 真实 Step 身份；Task/Run/Barrier aggregate 时为 null。
    stepId: Required[Optional[str]]
    # 真实 Attempt 身份；非 Attempt aggregate 时为 null。
    attemptId: Required[Optional[str]]
    # 本次转换读取到的旧版本。
    previousStateVersion: Required[int]
    # 本次转换写入的新版本；必须由 Task 1 同一 UoW 验证为 previousStateVersion + 1。
    stateVersion: Required[int]
    # 已脱敏的权威状态变化载荷。
    payload: Required[dict[str, Any]]
    # payload 的规范化 SHA-256 摘要。
    payloadDigest: Required[str]

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
