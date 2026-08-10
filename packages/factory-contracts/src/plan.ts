/**
 * packages/factory-contracts/src/plan.ts
 *
 * 计划语义哈希、修订摘要与 barrier 身份 —— TypeScript 实现。
 * 三个标识使用同一 canonical.ts 底座（NFC + RFC 8785 JCS），
 * 必须与 Python (factory_agent.policy.plan_hash) 及 Rust
 * (factory_contracts::plan) 字节级一致。
 *
 *   semanticPlanHash
 *       只标识计划语义，跨修订版本稳定；纳入字段严格来自 Master Spec §6.2：
 *         schemaVersion, goal, assumptions, scope, constraints,
 *         acceptanceCriteria, targetStage,
 *         repository{mode, root, baseBranch, baseCommit},
 *         workPlan{dagVersion, nodes, barriers},
 *         riskProfile, nodeCapabilityMapVersion, stageCapabilityMapVersion
 *       排除 taskId / specRevision / parentRevisionId / intentAuthorizationId /
 *       生成时间 / 事件·授权·receipt ID / hash·signature 字段。
 *
 *   planRevisionDigest
 *       对「除 planRevisionDigest 与 signature 外」的完整不可变 PlanRevision
 *       做同一 canonicalizer；语义相同可共享 semanticPlanHash，
 *       但不同谱系必须得到不同 planRevisionDigest。
 *
 *   barrierId
 *       "bar_" + lowercaseHex(SHA-256(JCS(
 *         ["factory-barrier-v1", runId, planRevisionDigest,
 *          businessPhase, barrierOrdinal])))
 *
 * 失败错误码：plan-hash-error（不记录完整计划正文）。
 */

import { createHash } from "node:crypto";
import { canonicalize } from "./canonical.js";

/** 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。 */
export const PLAN_HASH_VERSION = "plan-hash-v1";

/** barrier 身份的域分离标签（数组首元素）。 */
const BARRIER_DOMAIN = "factory-barrier-v1";

/** semanticPlanHash 纳入的顶层字段（严格来自 Master Spec §6.2）。 */
const SEMANTIC_TOP_FIELDS = [
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
] as const;

/** repository 子对象纳入的字段。 */
const REPO_FIELDS = ["mode", "root", "baseBranch", "baseCommit"] as const;

/** workPlan 子对象纳入的字段。 */
const WORKPLAN_FIELDS = ["dagVersion", "nodes", "barriers"] as const;

/** planRevisionDigest 必须排除的字段（自身摘要值与签名）。 */
const DIGEST_EXCLUDED_FIELDS = new Set(["planRevisionDigest", "signature"]);

/** 摘要输出前缀。 */
const SHA256_PREFIX = "sha256:";

/** 计划哈希输入非法（缺字段、类型错误等）时抛出。 */
export class PlanHashError extends Error {
  readonly errorCode = "plan-hash-error";
  constructor(detail: string) {
    super(`[plan-hash-error] ${detail}`);
    this.name = "PlanHashError";
  }
}

/** 任意 JSON 对象别名。 */
type JsonObject = Record<string, unknown>;

/** 校验 value 为对象，否则 fail closed。 */
function requireObject(value: unknown, label: string): JsonObject {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value)
  ) {
    throw new PlanHashError(`字段 '${label}' 必须为对象`);
  }
  return value as JsonObject;
}

/** 从 source 提取 fields 指定的字段子集，缺字段即 fail closed。 */
function projectSubset(
  source: JsonObject,
  fields: readonly string[],
  label: string,
): JsonObject {
  const projected: JsonObject = {};
  for (const name of fields) {
    if (!(name in source)) {
      throw new PlanHashError(`${label} 缺少语义字段 '${name}'（${PLAN_HASH_VERSION}）`);
    }
    projected[name] = source[name];
  }
  return projected;
}

/**
 * 构造 semanticPlanHash 的字段投影（不含任何谱系/时间/ID/hash 字段）。
 * @throws PlanHashError 缺少必需语义字段或子对象结构非法。
 */
export function buildSemanticProjection(plan: unknown): JsonObject {
  const obj = requireObject(plan, "plan");
  const projection = projectSubset(obj, SEMANTIC_TOP_FIELDS, "plan");

  // repository：仅纳入 mode/root/baseBranch/baseCommit。
  const repository = requireObject(obj["repository"], "repository");
  projection["repository"] = projectSubset(repository, REPO_FIELDS, "repository");

  // workPlan：仅纳入 dagVersion/nodes/barriers。
  const workPlan = requireObject(obj["workPlan"], "workPlan");
  projection["workPlan"] = projectSubset(workPlan, WORKPLAN_FIELDS, "workPlan");

  return projection;
}

/** 计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。 */
function sha256Prefixed(bytes: Uint8Array): string {
  const digest = createHash("sha256").update(bytes).digest("hex");
  return `${SHA256_PREFIX}${digest}`;
}

/**
 * 计算 semanticPlanHash（跨修订版本稳定的计划语义身份）。
 * @throws PlanHashError 语义字段缺失或结构非法。
 * @throws CanonicalJsonError 字段值含非法数字/类型/重复键。
 */
export function semanticPlanHash(plan: unknown): string {
  const projection = buildSemanticProjection(plan);
  return sha256Prefixed(canonicalize(projection));
}

/**
 * 计算 planRevisionDigest（完整不可变修订记录身份）。
 * 排除 planRevisionDigest 与 signature，其余字段（含谱系、授权关联、
 * semanticPlanHash、生成时间）全部纳入。
 * @throws PlanHashError revision 非对象。
 * @throws CanonicalJsonError 字段值含非法数字/类型/重复键。
 */
export function planRevisionDigest(revision: unknown): string {
  const obj = requireObject(revision, "revision");
  const material: JsonObject = {};
  for (const key of Object.keys(obj)) {
    if (!DIGEST_EXCLUDED_FIELDS.has(key)) {
      material[key] = obj[key];
    }
  }
  return sha256Prefixed(canonicalize(material));
}

/**
 * 计算 barrierId（稳定 barrier 身份，使用域分离数组）。
 * @throws PlanHashError 参数类型非法。
 * @throws CanonicalJsonError 参数值无法规范化。
 */
export function barrierId(
  runId: string,
  planRevisionDigestValue: string,
  businessPhase: string,
  barrierOrdinal: number,
): string {
  if (typeof runId !== "string" || runId.length === 0) {
    throw new PlanHashError("runId 必须为非空字符串");
  }
  if (typeof planRevisionDigestValue !== "string" || planRevisionDigestValue.length === 0) {
    throw new PlanHashError("planRevisionDigest 必须为非空字符串");
  }
  if (typeof businessPhase !== "string" || businessPhase.length === 0) {
    throw new PlanHashError("businessPhase 必须为非空字符串");
  }
  if (typeof barrierOrdinal !== "number" || !Number.isInteger(barrierOrdinal)) {
    throw new PlanHashError("barrierOrdinal 必须为整数");
  }

  const domainArray: unknown[] = [
    BARRIER_DOMAIN,
    runId,
    planRevisionDigestValue,
    businessPhase,
    barrierOrdinal,
  ];
  const digest = createHash("sha256").update(canonicalize(domainArray)).digest("hex");
  return `bar_${digest}`;
}
