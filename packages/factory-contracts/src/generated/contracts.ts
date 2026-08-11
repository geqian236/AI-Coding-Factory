// 此文件由 contracts/codegen/generate.py 自动生成，禁止手动修改。
// 源 schema: contracts/schemas/*.schema.json
// 算法版本: v1

/* eslint-disable */
// @ts-nocheck

// 运行时 validator 由权威 schema 机械嵌入；禁止手写字段表。
export const RUN_SPEC_SCHEMA_SOURCE_SHA256 = "sha256:8fa7cf095a24de2f1c823385a34a3786b2dc2c7ce409de732009f5f6e5e50883";
export const RUN_SPEC_SCHEMA_JSON = "{\"$id\":\"https://factory.local/contracts/schemas/run-spec.v1.schema.json\",\"$schema\":\"http://json-schema.org/draft-07/schema#\",\"additionalProperties\":false,\"description\":\"规划层生成的不可变、版本化 RunSpec：完整语义文档（Master Spec §8）。semanticPlanHash 从本文档的语义字段投影派生；写入后不可修改，修复只能新建子版本。\",\"properties\":{\"acceptanceCriteria\":{\"description\":\"验收条件列表\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"assumptions\":{\"description\":\"规划假设列表\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"constraints\":{\"description\":\"约束条件列表\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"createdAt\":{\"description\":\"RunSpec 创建时间\",\"format\":\"date-time\",\"pattern\":\"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$\",\"type\":\"string\"},\"goal\":{\"description\":\"用户目标的结构化表达\",\"minLength\":1,\"type\":\"string\"},\"intentAuthorizationId\":{\"description\":\"关联 IntentAuthorization ID\",\"minLength\":1,\"type\":\"string\"},\"nodeCapabilityMapVersion\":{\"description\":\"node-capability-map 版本标识\",\"minLength\":1,\"type\":\"string\"},\"parentRevisionId\":{\"description\":\"父 PlanRevision ID；genesis 修订为 null\",\"type\":[\"string\",\"null\"]},\"planRevisionDigest\":{\"description\":\"完整不可变修订记录摘要（写入 PlanRevision 后填入）\",\"pattern\":\"^sha256:[0-9a-f]{64}$\",\"type\":\"string\"},\"repository\":{\"additionalProperties\":false,\"description\":\"仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit）\",\"properties\":{\"baseBranch\":{\"description\":\"基础分支名\",\"minLength\":1,\"type\":\"string\"},\"baseCommit\":{\"allOf\":[{\"not\":{\"const\":\"0000000000000000000000000000000000000000\"}}],\"description\":\"bootstrap 完成后可信 base commit 的 40 位小写完整 SHA\",\"pattern\":\"^[0-9a-f]{40}$\",\"type\":\"string\"},\"mode\":{\"description\":\"仓库模式\",\"enum\":[\"existing\",\"new\"],\"type\":\"string\"},\"root\":{\"description\":\"规范化后的 D 盘仓库根路径\",\"minLength\":1,\"type\":\"string\"}},\"required\":[\"mode\",\"root\",\"baseBranch\",\"baseCommit\"],\"type\":\"object\"},\"riskProfile\":{\"additionalProperties\":false,\"description\":\"风险画像（§8：level/reasons）\",\"properties\":{\"level\":{\"description\":\"风险等级\",\"enum\":[\"low\",\"medium\",\"high\",\"critical\"],\"type\":\"string\"},\"reasons\":{\"description\":\"风险原因列表\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"level\",\"reasons\"],\"type\":\"object\"},\"schemaVersion\":{\"description\":\"RunSpec schema 版本号\",\"minimum\":1,\"type\":\"integer\"},\"scope\":{\"additionalProperties\":false,\"description\":\"范围声明（§8：include/exclude）\",\"properties\":{\"exclude\":{\"description\":\"排除范围的路径/模块\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"include\":{\"description\":\"纳入范围的路径/模块\",\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"include\",\"exclude\"],\"type\":\"object\"},\"semanticPlanHash\":{\"description\":\"语义计划哈希（跨修订版本稳定，从语义字段投影派生）\",\"pattern\":\"^sha256:[0-9a-f]{64}$\",\"type\":\"string\"},\"specRevision\":{\"description\":\"同一 taskId 下的规格修订序号\",\"minimum\":1,\"type\":\"integer\"},\"stageCapabilityMapVersion\":{\"description\":\"stage-capability-map 版本标识\",\"minLength\":1,\"type\":\"string\"},\"targetStage\":{\"description\":\"目标终点阶段（Master Spec §6.1 权威 6 阶段）\",\"enum\":[\"DESIGN_APPROVED\",\"CODEX_APPROVED\",\"PR_READY\",\"MERGED\",\"STAGING_ACCEPTED\",\"PRODUCTION_ACCEPTED\"],\"type\":\"string\"},\"taskId\":{\"description\":\"关联任务 ID\",\"minLength\":1,\"type\":\"string\"},\"workPlan\":{\"additionalProperties\":false,\"description\":\"工作计划 DAG（§8：dagVersion/nodes/barriers）\",\"properties\":{\"barriers\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"}},\"required\":[\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"阶段屏障列表\",\"items\":{\"additionalProperties\":false,\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]},\"properties\":{\"barrierOrdinal\":{\"minimum\":0,\"type\":\"integer\"},\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"passPredicateId\":{\"minLength\":1,\"type\":\"string\"},\"requiredNodeIds\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"settleTimeoutMs\":{\"minimum\":1,\"type\":\"integer\"}},\"required\":[\"businessPhase\",\"barrierOrdinal\",\"requiredNodeIds\",\"settleTimeoutMs\",\"passPredicateId\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"},\"dagVersion\":{\"description\":\"DAG 结构版本\",\"minimum\":1,\"type\":\"integer\"},\"nodes\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"},\"nodeType\":{\"const\":\"PLAN\"}},\"required\":[\"nodeType\",\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"DAG 工作节点列表\",\"items\":{\"additionalProperties\":false,\"allOf\":[{\"not\":{\"properties\":{\"nodeType\":{\"const\":\"BOOTSTRAP_REPOSITORY\"}},\"required\":[\"nodeType\"]}},{\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]}}],\"properties\":{\"barrierOrdinal\":{\"minimum\":0,\"type\":\"integer\"},\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"dependsOn\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"logicalNodeId\":{\"minLength\":1,\"type\":\"string\"},\"nodeType\":{\"enum\":[\"PLAN\",\"DESIGN_REVIEW\",\"BOOTSTRAP_REPOSITORY\",\"IMPLEMENT\",\"VERIFY\",\"CODE_REVIEW\",\"ATTEST_REVIEW\",\"PUBLISH_PR\",\"MERGE\",\"BUILD_ARTIFACT\",\"DEPLOY_STAGING\",\"ACCEPT_STAGING\",\"DEPLOY_PRODUCTION\",\"ACCEPT_PRODUCTION\",\"ROLLBACK\",\"RECONCILE_TARGET\",\"RESTORE_DRILL\"],\"type\":\"string\"},\"required\":{\"type\":\"boolean\"},\"requiredArtifacts\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"retryPolicyId\":{\"minLength\":1,\"type\":\"string\"},\"sideEffectClass\":{\"minLength\":1,\"type\":\"string\"},\"successPredicateId\":{\"minLength\":1,\"type\":\"string\"},\"timeoutMs\":{\"minimum\":1,\"type\":\"integer\"}},\"required\":[\"logicalNodeId\",\"businessPhase\",\"barrierOrdinal\",\"nodeType\",\"required\",\"dependsOn\",\"sideEffectClass\",\"requiredArtifacts\",\"successPredicateId\",\"timeoutMs\",\"retryPolicyId\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"}},\"required\":[\"dagVersion\",\"nodes\",\"barriers\"],\"type\":\"object\"}},\"required\":[\"schemaVersion\",\"taskId\",\"goal\",\"assumptions\",\"scope\",\"constraints\",\"acceptanceCriteria\",\"targetStage\",\"repository\",\"workPlan\",\"riskProfile\",\"nodeCapabilityMapVersion\",\"stageCapabilityMapVersion\",\"intentAuthorizationId\",\"semanticPlanHash\"],\"title\":\"RunSpec\",\"type\":\"object\"}";
export const RUN_SPEC_SCHEMA_JSON_SHA256 = "sha256:0a63d2dae80c298d85bdb5f2ea119669d6a7c6aca8e954f07c6cf2a8261f7841";
export const PLAN_REVISION_SCHEMA_SOURCE_SHA256 = "sha256:fc9acfd58b9e41b12dee1baaa63d05b0ce08227afd99419bf987cac1abdbe035";
export const PLAN_REVISION_SCHEMA_JSON = "{\"$defs\":{\"sha256Digest\":{\"pattern\":\"^sha256:[0-9a-f]{64}$\",\"type\":\"string\"}},\"$id\":\"https://factory.local/contracts/schemas/plan-revision.v1.schema.json\",\"$schema\":\"http://json-schema.org/draft-07/schema#\",\"additionalProperties\":false,\"description\":\"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。\",\"properties\":{\"barriers\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"}},\"required\":[\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"阶段屏障列表\",\"items\":{\"additionalProperties\":false,\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]},\"properties\":{\"barrierId\":{\"minLength\":1,\"type\":\"string\"},\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"nodeIds\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"barrierId\",\"businessPhase\",\"nodeIds\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"},\"createdAt\":{\"format\":\"date-time\",\"pattern\":\"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$\",\"type\":\"string\"},\"dagVersion\":{\"description\":\"DAG 版本号（§11 plan_revisions.dag_version）\",\"minimum\":1,\"type\":\"integer\"},\"intentAuthorizationId\":{\"description\":\"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）\",\"minLength\":1,\"type\":\"string\"},\"nodeCapabilityMapVersion\":{\"description\":\"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）\",\"minLength\":1,\"type\":\"string\"},\"nodes\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"},\"nodeType\":{\"const\":\"PLAN\"}},\"required\":[\"nodeType\",\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"DAG 节点列表\",\"items\":{\"additionalProperties\":false,\"allOf\":[{\"not\":{\"properties\":{\"nodeType\":{\"const\":\"BOOTSTRAP_REPOSITORY\"}},\"required\":[\"nodeType\"]}},{\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]}}],\"properties\":{\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"dependencies\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"gate\":{\"minLength\":1,\"type\":\"string\"},\"hasSideEffect\":{\"type\":\"boolean\"},\"logicalNodeId\":{\"minLength\":1,\"type\":\"string\"},\"nodeType\":{\"enum\":[\"PLAN\",\"DESIGN_REVIEW\",\"BOOTSTRAP_REPOSITORY\",\"IMPLEMENT\",\"VERIFY\",\"CODE_REVIEW\",\"ATTEST_REVIEW\",\"PUBLISH_PR\",\"MERGE\",\"BUILD_ARTIFACT\",\"DEPLOY_STAGING\",\"ACCEPT_STAGING\",\"DEPLOY_PRODUCTION\",\"ACCEPT_PRODUCTION\",\"ROLLBACK\",\"RECONCILE_TARGET\",\"RESTORE_DRILL\"],\"type\":\"string\"},\"requiredArtifacts\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"logicalNodeId\",\"nodeType\",\"businessPhase\",\"dependencies\",\"hasSideEffect\",\"requiredArtifacts\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"},\"parentRevisionId\":{\"description\":\"父修订版本 ID（范围内重规划时设置）\",\"minLength\":1,\"type\":\"string\"},\"planRevisionDigest\":{\"allOf\":[{\"$ref\":\"#/$defs/sha256Digest\"}],\"description\":\"本修订版本内容摘要\",\"type\":\"string\"},\"planRevisionId\":{\"description\":\"本修订版本唯一 ID\",\"minLength\":1,\"type\":\"string\"},\"semanticPlanHash\":{\"allOf\":[{\"$ref\":\"#/$defs/sha256Digest\"}],\"description\":\"语义计划哈希，跨修订版本稳定\",\"type\":\"string\"},\"signature\":{\"minLength\":1,\"type\":\"string\"},\"specRevision\":{\"description\":\"规格修订号，单调递增（§11 plan_revisions.spec_revision）\",\"minimum\":1,\"type\":\"integer\"},\"stageCapabilityMapVersion\":{\"description\":\"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）\",\"minLength\":1,\"type\":\"string\"},\"stageMaps\":{\"additionalProperties\":false,\"description\":\"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）\",\"properties\":{\"CODEX_APPROVED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"DESIGN_APPROVED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"MERGED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"PRODUCTION_ACCEPTED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"PR_READY\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"STAGING_ACCEPTED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"}},\"type\":\"object\"},\"taskId\":{\"description\":\"所属任务 ID（§11 plan_revisions.task_id）\",\"minLength\":1,\"type\":\"string\"}},\"required\":[\"planRevisionId\",\"taskId\",\"specRevision\",\"intentAuthorizationId\",\"semanticPlanHash\",\"planRevisionDigest\",\"dagVersion\",\"nodeCapabilityMapVersion\",\"stageCapabilityMapVersion\",\"nodes\",\"barriers\",\"stageMaps\",\"createdAt\"],\"title\":\"PlanRevision\",\"type\":\"object\"}";
export const PLAN_REVISION_SCHEMA_JSON_SHA256 = "sha256:859b4d8f5f18b5b388ff836789f526deb2358a81c9a4baeeef3f04ea063b8a45";
// 仅 planRevisionDigest/signature 从摘要 material 排除，其他字段仍由权威 schema 约束。
export const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON = "{\"$defs\":{\"sha256Digest\":{\"pattern\":\"^sha256:[0-9a-f]{64}$\",\"type\":\"string\"}},\"$id\":\"https://factory.local/contracts/schemas/plan-revision.v1.schema.json\",\"$schema\":\"http://json-schema.org/draft-07/schema#\",\"additionalProperties\":false,\"description\":\"计划修订版本，包含 DAG 节点、barrier、stage 映射和谱系信息。\",\"properties\":{\"barriers\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"}},\"required\":[\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"阶段屏障列表\",\"items\":{\"additionalProperties\":false,\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]},\"properties\":{\"barrierId\":{\"minLength\":1,\"type\":\"string\"},\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"nodeIds\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"barrierId\",\"businessPhase\",\"nodeIds\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"},\"createdAt\":{\"format\":\"date-time\",\"pattern\":\"^[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-5][0-9](?:\\\\.[0-9]+)?(?:[Zz]|[+-][0-9]{2}:[0-9]{2})$\",\"type\":\"string\"},\"dagVersion\":{\"description\":\"DAG 版本号（§11 plan_revisions.dag_version）\",\"minimum\":1,\"type\":\"integer\"},\"intentAuthorizationId\":{\"description\":\"关联 IntentAuthorization ID（§11 plan_revisions.intent_authorization_id）\",\"minLength\":1,\"type\":\"string\"},\"nodeCapabilityMapVersion\":{\"description\":\"node→capability 映射版本（§11 plan_revisions.node_capability_map_version）\",\"minLength\":1,\"type\":\"string\"},\"nodes\":{\"allOf\":[{\"items\":[{\"properties\":{\"businessPhase\":{\"const\":\"PLANNING\"},\"nodeType\":{\"const\":\"PLAN\"}},\"required\":[\"nodeType\",\"businessPhase\"],\"type\":\"object\"}]}],\"description\":\"DAG 节点列表\",\"items\":{\"additionalProperties\":false,\"allOf\":[{\"not\":{\"properties\":{\"nodeType\":{\"const\":\"BOOTSTRAP_REPOSITORY\"}},\"required\":[\"nodeType\"]}},{\"not\":{\"properties\":{\"businessPhase\":{\"enum\":[\"BOOTSTRAPPING\",\"BOOTSTRAPPING_REPOSITORY\"]}},\"required\":[\"businessPhase\"]}}],\"properties\":{\"businessPhase\":{\"enum\":[\"PLANNING\",\"DESIGN_REVIEWING\",\"PREPARING_WORKSPACE\",\"IMPLEMENTING\",\"VERIFYING\",\"CODE_REVIEWING\",\"PUBLISHING_PR\",\"MERGING\",\"BUILDING_ARTIFACT\",\"DEPLOYING_STAGING\",\"ACCEPTING_STAGING\",\"DEPLOYING_PRODUCTION\",\"ACCEPTING_PRODUCTION\",\"ROLLING_BACK\",\"FINALIZING\"],\"type\":\"string\"},\"dependencies\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"},\"gate\":{\"minLength\":1,\"type\":\"string\"},\"hasSideEffect\":{\"type\":\"boolean\"},\"logicalNodeId\":{\"minLength\":1,\"type\":\"string\"},\"nodeType\":{\"enum\":[\"PLAN\",\"DESIGN_REVIEW\",\"BOOTSTRAP_REPOSITORY\",\"IMPLEMENT\",\"VERIFY\",\"CODE_REVIEW\",\"ATTEST_REVIEW\",\"PUBLISH_PR\",\"MERGE\",\"BUILD_ARTIFACT\",\"DEPLOY_STAGING\",\"ACCEPT_STAGING\",\"DEPLOY_PRODUCTION\",\"ACCEPT_PRODUCTION\",\"ROLLBACK\",\"RECONCILE_TARGET\",\"RESTORE_DRILL\"],\"type\":\"string\"},\"requiredArtifacts\":{\"items\":{\"minLength\":1,\"type\":\"string\"},\"type\":\"array\"}},\"required\":[\"logicalNodeId\",\"nodeType\",\"businessPhase\",\"dependencies\",\"hasSideEffect\",\"requiredArtifacts\"],\"type\":\"object\"},\"minItems\":1,\"type\":\"array\"},\"parentRevisionId\":{\"description\":\"父修订版本 ID（范围内重规划时设置）\",\"minLength\":1,\"type\":\"string\"},\"planRevisionId\":{\"description\":\"本修订版本唯一 ID\",\"minLength\":1,\"type\":\"string\"},\"semanticPlanHash\":{\"allOf\":[{\"$ref\":\"#/$defs/sha256Digest\"}],\"description\":\"语义计划哈希，跨修订版本稳定\",\"type\":\"string\"},\"specRevision\":{\"description\":\"规格修订号，单调递增（§11 plan_revisions.spec_revision）\",\"minimum\":1,\"type\":\"integer\"},\"stageCapabilityMapVersion\":{\"description\":\"stage→capability 映射版本（§11 plan_revisions.stage_capability_map_version）\",\"minLength\":1,\"type\":\"string\"},\"stageMaps\":{\"additionalProperties\":false,\"description\":\"target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类）\",\"properties\":{\"CODEX_APPROVED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"DESIGN_APPROVED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"MERGED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"PRODUCTION_ACCEPTED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"PR_READY\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"},\"STAGING_ACCEPTED\":{\"items\":{\"type\":\"string\"},\"type\":\"array\"}},\"type\":\"object\"},\"taskId\":{\"description\":\"所属任务 ID（§11 plan_revisions.task_id）\",\"minLength\":1,\"type\":\"string\"}},\"required\":[\"planRevisionId\",\"taskId\",\"specRevision\",\"intentAuthorizationId\",\"semanticPlanHash\",\"dagVersion\",\"nodeCapabilityMapVersion\",\"stageCapabilityMapVersion\",\"nodes\",\"barriers\",\"stageMaps\",\"createdAt\"],\"title\":\"PlanRevision\",\"type\":\"object\"}";
export const PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256 = "sha256:b0ec793126618633ddde4986c388a66d5db345d9c5dbd13ce3384689174b3a28";

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
  /** 仓库绑定（§8：mode 保留用户来源；RunSpec 只在 bootstrap 后生成，两个 mode 都绑定可信 40 位小写 baseCommit） */
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
  signature?: string;
  /** DAG 节点列表 */
  nodes: Record<string, unknown>[];
  /** 阶段屏障列表 */
  barriers: Record<string, unknown>[];
  /** target_stage 到节点集合的映射（Master Spec §6.1 权威 target_stage 分类） */
  stageMaps: Record<string, unknown>;
  createdAt: string;
}

/** IntentAuthorization — 由 generate.py 自动生成，禁止手动修改 */
export interface IntentAuthorization {
  /** IntentAuthorization 的稳定身份。 */
  intentAuthorizationId: string;
  /** 被授权任务的稳定身份。 */
  taskId: string;
  /** 做出一次性授权的用户身份。 */
  userId: string;
  /** 规范化用户需求的闭合 JCS/NFC 快照引用。 */
  requirementDigest: Record<string, unknown>;
  /** 项目身份，禁止跨项目复用授权。 */
  projectId: string;
  /** 规范化后的仓库身份。 */
  repositoryId: string;
  /** 可解析的仓库/物理路径/模式绑定快照引用。 */
  repositoryBindingDigest: Record<string, unknown>;
  /** 可解析的基础分支、base SHA 或 bootstrap 前置事实快照引用。 */
  baselineDigest: Record<string, unknown>;
  /** 用户选择的目标终点阶段；只给出 capability 上限。 */
  targetStage: "DESIGN_APPROVED" | "CODEX_APPROVED" | "PR_READY" | "MERGED" | "STAGING_ACCEPTED" | "PRODUCTION_ACCEPTED";
  /** 派生授权时使用的 stage-capability-map 版本。 */
  stageCapabilityMapVersion: string;
  /** targetStage 展开后的排序 capability 集合快照引用。 */
  allowedCapabilitySetDigest: Record<string, unknown>;
  /** 可解析的环境、ServerProfile、数据库和资源目标绑定快照引用。 */
  targetBindingDigest: Record<string, unknown>;
  /** 用户接受的最高风险等级。 */
  riskCeiling: "low" | "medium" | "high" | "critical";
  /** 可解析的估算成本告警策略快照引用；不把订阅 CLI 估算伪装成真实账单。 */
  estimatedCostAlertDigest: Record<string, unknown>;
  /** 端到端自主执行预算（毫秒）；禁止无限值。 */
  autonomousExecutionBudgetMs: number;
  /** 自动修复轮数上限。 */
  repairLoopLimit: number;
  /** 自动重规划次数上限。 */
  autoReplanLimit: number;
  /** Attempt 总数上限。 */
  attemptLimit: number;
  /** 授权签发时间。 */
  issuedAt: string;
  /** 绝对有效期；暂停不会延长它。 */
  expiresAt: string;
  /** 撤销时间；未撤销时显式为 null。 */
  revokedAt: string | null;
  /** 撤销原因；未撤销时显式为 null。 */
  revokeReason: string | null;
}

/** ExecutionAuthorization — 由 generate.py 自动生成，禁止手动修改 */
export interface ExecutionAuthorization {
  /** 派生授权稳定身份。 */
  executionAuthorizationId: string;
  /** 来源 IntentAuthorization 身份。 */
  intentAuthorizationId: string;
  /** 绑定的不可变 PlanRevision 身份。 */
  planRevisionId: string;
  /** 计划语义的闭合快照引用。 */
  semanticPlanHash: Record<string, unknown>;
  /** 完整 PlanRevision 的闭合快照引用。 */
  planRevisionDigest: Record<string, unknown>;
  /** stage capability map 版本。 */
  stageCapabilityMapVersion: string;
  /** stage capability map 的闭合快照引用。 */
  stageCapabilityMapDigest: Record<string, unknown>;
  /** node capability map 版本。 */
  nodeCapabilityMapVersion: string;
  /** node capability map raw-file 内容的闭合快照引用。 */
  nodeCapabilityMapDigest: Record<string, unknown>;
  /** 绑定的 Run 身份。 */
  runId: string;
  /** 绑定的 Step 身份。 */
  stepId: string;
  /** 绑定的不可复用 Attempt 身份。 */
  attemptId: string;
  /** 冻结 node-capability-map 的节点类型。 */
  nodeType: "PLAN" | "DESIGN_REVIEW" | "BOOTSTRAP_REPOSITORY" | "IMPLEMENT" | "VERIFY" | "CODE_REVIEW" | "ATTEST_REVIEW" | "PUBLISH_PR" | "MERGE" | "BUILD_ARTIFACT" | "DEPLOY_STAGING" | "ACCEPT_STAGING" | "DEPLOY_PRODUCTION" | "ACCEPT_PRODUCTION" | "ROLLBACK" | "RECONCILE_TARGET" | "RESTORE_DRILL";
  /** 当前 lease owner 的执行器身份。 */
  executorId: string;
  /** 资源指纹的闭合快照引用；其 payload 必须按 nodeType 使用 node map 内嵌 resourceFingerprintSchema 校验。 */
  resourceFingerprint: Record<string, unknown>;
  /** 可解析 capability/resource scope 的闭合快照引用。 */
  capabilityScopeDigest: Record<string, unknown>;
  /** 按 factory-action-v1 的 H=sha256(JCS/NFC(...)) 计算的稳定 action key 快照引用。 */
  idempotencyKey: Record<string, unknown>;
  /** 闭合且无歧义的输入身份对象；同一种 SHA/digest 至多一个字段，禁止数组重复绑定或自由文本。 */
  inputBindings: Record<string, unknown>;
  /** 本 ExecutionAuthorization 唯一对应的 capability action。 */
  actionCapability: "acceptance.fixture.write" | "build.exec.isolated" | "check.publish" | "container.inspect.scoped" | "db.backup" | "db.check" | "db.migrate" | "db.read" | "db.restore" | "forge.observe.scoped" | "git.local_commit" | "git.push" | "http.check.scoped" | "log.read.scoped" | "network.egress.scoped" | "nginx.switch" | "pr.create" | "pr.update" | "registry.observe.scoped" | "registry.push" | "remote.observe.scoped" | "remote.write.scoped" | "repo.bootstrap" | "repo.merge" | "repo.read" | "restore.validation.instance" | "rollback" | "service.restart.scoped" | "ssh.exec.scoped" | "target.guard.clear" | "test.exec.isolated" | "traffic.switch.scoped" | "worktree.write";
  /** selected action 的 key template、completion fact 与消费点确定性投影的闭合快照引用；payload 必须绑定 nodeCapabilityMapDigest、nodeType 和 actionCapability。 */
  actionPolicySnapshotDigest: Record<string, unknown>;
  /** 严格单调 lease fencing token。 */
  fencingToken: number;
  /** 控制权 epoch。 */
  controlEpoch: number;
  /** 派生时接受的控制命令序号。 */
  acceptedControlCommandSeq: number;
  /** 剩余可消费次数；不会使用第二个 ttlMs 真源。 */
  maxUses: number;
  /** 仅表示消费投影；撤销/过期分别由 revokedAt/expiresAt 表达，未知状态不能视为成功。 */
  consumptionState: "AVAILABLE" | "CONSUMED";
  /** 派生授权签发时间。 */
  issuedAt: string;
  /** 派生授权绝对 TTL。 */
  expiresAt: string;
  /** 撤销时间；未撤销时显式为 null。 */
  revokedAt: string | null;
  /** 撤销原因；未撤销时显式为 null。 */
  revokeReason: string | null;
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
  /** 批次唯一稳定 ID。 */
  preparedBatchId: string;
  /** claim 时的 writer epoch（用于竞争检测）。 */
  writerEpoch: number;
  /** 批次状态机（§11 ingest_batches）。正常转换仅 CLAIMED → PREPARING → PREPARED → COMMITTED；旧 epoch pre-commit 状态只能由 Reconciler CAS 为 ABANDONED。COMMITTED/ABANDONED 均为终态。 */
  state: "CLAIMED" | "PREPARING" | "PREPARED" | "COMMITTED";
  /** 按 batchOrdinal 排列的全量稳定 ingestEventId 列表（权威顺序）。 */
  orderedIngestIds: string[];
  /** 每个 Task 唯一的 expectedCommittedHead 锚点 + 批内 ordinal 范围（§11 ingest_batch_task_heads）。 */
  perTaskExpectedHeads: Record<string, unknown>[];
  /** segment digest 列表。 */
  segmentDigests: Record<string, unknown>[];
  /** 总事件数。 */
  eventCount: number;
  /** 总 payload 字节数。 */
  payloadBytes: number;
  /** 本批首事件 batchOrdinal。 */
  firstBatchOrdinal: number;
  /** 本批末事件 batchOrdinal。 */
  lastBatchOrdinal: number;
  /** 本批最旧 ingestedAt。 */
  oldestIngestedAt: string;
  /** 本批 prepared 完成时间。 */
  preparedAt: string;
  /** PreparedBatch schema 版本。 */
  schemaVersion: 2;
  /** coordinator 版本（compatibility manifest 绑定）。 */
  coordinatorVersion: string;
}

/** DurableEventV2 — 由 generate.py 自动生成，禁止手动修改 */
export interface DurableEventV2 {
  /** 事件 schema 版本（v2）。Phase 1+ 字段集演进时升级。 */
  schemaVersion: 2;
  /** 耐久化等级（§10.4）：authoritative_state=内部权威 state.changed/lease/授权；side_effect_receipt=外部副作用 receipt；provider_source=Provider semantic/public frame；derived=UI summary/指标等可重建派生。 */
  durabilityClass: "authoritative_state" | "side_effect_receipt" | "provider_source" | "derived";
  /** 物化器分配的全局唯一事件 ID（evt_<64 位小写十六进制>，SHA-256(JCS([factory-event-id-v2, ingestEventId]))）。崩溃重试得到同一身份。 */
  eventId: string;
  /** 所属任务 ID。 */
  taskId: string;
  /** 任务内单调递增序号（由物化器分配，CAS 持久）。UI cursor 用 (taskId, taskSeq)。 */
  taskSeq: number;
  /** 所属 Run ID。 */
  runId: string;
  /** 单 Run 内诊断序号（不能跨 Run 唯一）。 */
  runSeq: number;
  /** 所属 Step ID。 */
  stepId: string;
  /** 所属 Attempt ID。 */
  attemptId: string;
  /** 事件来源 actor 身份。 */
  source: "claude" | "codex" | "verifier" | "release" | "orchestrator";
  /** 原始摄取 ID（来自 PreparedEventV2）。 */
  ingestEventId: string;
  /** 具体事件类型（§10.1）。model.summary 必带 summaryOrigin=provider_public + providerEventId + sanitizedProviderFrameRef + sanitizedProviderFrameDigest。 */
  eventType: "process.started" | "stream.segment.committed" | "stream.terminated" | "model.summary" | "orchestrator.objective" | "tool.call" | "tool.result" | "state.changed";
  /** Provider 事件 ID（model.summary 等 provider 源事件必填，普通事件 null）。 */
  providerEventId: string | null;
  /** Provider 来源端序号（用于去重与对齐）。 */
  sourceSeq: number;
  /** Provider stream 稳定 ID（stream.* 事件必填；非 stream 事件 null）。 */
  streamId: string | null;
  /** 所属 PreparedBatchV2 的 batchId。物化器先 claim 批次，再组装 PreparedBatch，最后逐条物化 DurableEvent。 */
  preparedBatchId: string;
  /** 所属 PreparedBatch 内的事件序号（与 manifest 的 orderedIngestIds 一致）。 */
  batchOrdinal: number;
  /** 源传输 span：内存中以原始传输字节精确计数。credential/secret/PII 仅存 providerEventId/sourceSeq 与诚实的 mappingPrecision（byte|field|frame|none），精确字节起止不能进普通事件。 */
  sourceTransportSpan: Record<string, unknown>;
  /** 脱敏后 span：精确指向持久化脱敏字节。脱敏改变长度时不得与 sourceTransportSpan 冒充同一坐标。 */
  sanitizedStreamSpan: Record<string, unknown>;
  /** RFC3339 接收 wall time（仅用于显示，不能参与排序）。 */
  wallTime: string;
  /** 单调时钟纳秒。 */
  monotonicTimeNs: number;
  /** 完整 frame 闭合后写入脱敏器的时刻（首字节到闭合仅记录 frame_assembly_ms 不写入）。 */
  ingestedAt: string;
  /** Provider CLI 上报版本。 */
  providerVersion: string;
  /** Adapter 自身 semver 版本。 */
  adapterVersion: string;
  /** 受管进程身份（§10.1）：用于 RUNNING 真实性校验（PID/Job Object/WSL/container exec）。 */
  processIdentity: Record<string, unknown>;
  /** 事件负载（已脱敏，未脱敏原文不得落盘）。 */
  payload: Record<string, unknown>;
  /** 已脱敏 Provider frame 的 SHA-256。原始 frame 在脱敏后立即丢弃。model.summary 必填，其它 null。 */
  sanitizedProviderFrameDigest: string | null;
  /** 脱敏后 payload 的 JCS 摘要。 */
  payloadDigest: string;
  /** 前驱事件 eventDigest（genesis 用 sha256:<64 个 0> 固定 predecessor）。 */
  previousEventDigest: string;
  /** 本事件 SHA-256(JCS(去除 eventDigest 字段后完整对象))。previousEventDigest 仍参与计算。 */
  eventDigest: string;
  /** 本事件命中脱敏点列表（不含原文、低熵 hash 或可推断秘密长度的 redaction manifest）。 */
  redactions: Record<string, unknown>[];
  /** redactions 列表内容的 SHA-256 摘要。 */
  redactionManifestDigest: string;
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
