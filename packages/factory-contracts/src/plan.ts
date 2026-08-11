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

import { createHash, timingSafeEqual } from "node:crypto";
import Ajv, { type AnySchema, type ValidateFunction } from "ajv";
import { canonicalize } from "./canonical.js";
import {
  PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
  PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
  PLAN_REVISION_SCHEMA_JSON,
  PLAN_REVISION_SCHEMA_JSON_SHA256,
  RUN_SPEC_SCHEMA_JSON,
  RUN_SPEC_SCHEMA_JSON_SHA256,
} from "./generated/contracts.js";

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

/** 摘要输出前缀。 */
const SHA256_PREFIX = "sha256:";

/** 不可信 wire 与 validator 初始化失败共用的稳定脱敏错误明细。 */
const STABLE_VALIDATION_DETAIL = "INVALID_PLAN_HASH_INPUT";
const DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#";
const KNOWN_VALIDATOR_FORMATS = new Set(["date-time"]);
const SINGLE_SCHEMA_KEYWORDS = new Set([
  "additionalItems",
  "additionalProperties",
  "contains",
  "if",
  "then",
  "else",
  "not",
  "propertyNames",
]);
const SCHEMA_MAP_KEYWORDS = new Set(["properties", "patternProperties", "definitions", "$defs"]);
const SCHEMA_ARRAY_KEYWORDS = new Set(["allOf", "anyOf", "oneOf"]);

/** Factory 冻结的可移植 RFC3339 wire 子集；日历和 offset 再由下方函数复核。 */
const FACTORY_DATE_TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-5][0-9])(?:\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$/;

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

/** 返回给定年月的天数，避免 Date 对 0..99 年份的隐式 1900 偏移。 */
function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    return leapYear ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

/** 与 Python/Rust 共享的 date-time format：拒绝 leap second、year 0000、非法日历和 offset。 */
function isFactoryDateTime(value: unknown): boolean {
  if (typeof value !== "string") {
    return true;
  }
  const match = FACTORY_DATE_TIME.exec(value);
  if (match === null) {
    return false;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = Number(match[4]);
  const minute = Number(match[5]);
  const zone = match[7];
  if (year === 0 || month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month) || hour > 23 || minute > 59) {
    return false;
  }
  if (zone.toLowerCase() === "z") {
    return true;
  }
  const offsetHour = Number(zone.slice(1, 3));
  const offsetMinute = Number(zone.slice(4, 6));
  return offsetHour <= 23 && offsetMinute <= 59;
}

/** 按 Draft7 schema keyword 遍历，避免把 properties 内的用户字段名误判成 keyword。 */
function walkValidatorSubschema(value: unknown): void {
  if (typeof value === "boolean" || value === null || typeof value !== "object" || Array.isArray(value)) {
    return;
  }
  const object = value as JsonObject;
  if ("$ref" in object && (typeof object.$ref !== "string" || !object.$ref.startsWith("#"))) {
    throw new Error("external schema reference");
  }
  if ("format" in object && (
    typeof object.format !== "string" || !KNOWN_VALIDATOR_FORMATS.has(object.format)
  )) {
    throw new Error("unknown schema format");
  }

  for (const keyword of SINGLE_SCHEMA_KEYWORDS) {
    if (keyword in object) {
      walkValidatorSubschema(object[keyword]);
    }
  }
  for (const keyword of SCHEMA_MAP_KEYWORDS) {
    const schemaMap = object[keyword];
    if (schemaMap !== null && typeof schemaMap === "object" && !Array.isArray(schemaMap)) {
      for (const childSchema of Object.values(schemaMap)) {
        walkValidatorSubschema(childSchema);
      }
    }
  }
  const items = object.items;
  if (Array.isArray(items)) {
    for (const childSchema of items) {
      walkValidatorSubschema(childSchema);
    }
  } else if (items !== undefined) {
    walkValidatorSubschema(items);
  }
  for (const keyword of SCHEMA_ARRAY_KEYWORDS) {
    const schemas = object[keyword];
    if (Array.isArray(schemas)) {
      for (const childSchema of schemas) {
        walkValidatorSubschema(childSchema);
      }
    }
  }
  const dependencies = object.dependencies;
  if (dependencies !== null && typeof dependencies === "object" && !Array.isArray(dependencies)) {
    for (const dependency of Object.values(dependencies)) {
      if (typeof dependency === "boolean" || (dependency !== null && typeof dependency === "object" && !Array.isArray(dependency))) {
        walkValidatorSubschema(dependency);
      }
    }
  }
}

/** 运行时只能编译完整 Draft7 生成常量；外部 ref、未知 format 和元 schema 失败均 fail closed。 */
function assertValidGeneratedSchema(schema: unknown): asserts schema is AnySchema {
  if (schema === null || typeof schema !== "object" || Array.isArray(schema)) {
    throw new Error("invalid schema root");
  }
  const root = schema as JsonObject;
  if (root.$schema !== DRAFT7_SCHEMA_URI) {
    throw new Error("unsupported schema draft");
  }
  walkValidatorSubschema(schema);
}

/** 在 JSON.parse 前验证嵌入字符串的 UTF-8 内容身份，任意字节漂移均 fail closed。 */
function verifyEmbeddedSchemaIntegrity(schemaJson: unknown, expectedSha256: unknown): asserts schemaJson is string {
  if (typeof schemaJson !== "string" || typeof expectedSha256 !== "string") {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
  const match = /^sha256:([0-9a-f]{64})$/.exec(expectedSha256);
  if (match === null) {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
  const actualDigest = createHash("sha256").update(schemaJson, "utf8").digest();
  const expectedDigest = Buffer.from(match[1], "hex");
  if (actualDigest.length !== expectedDigest.length || !timingSafeEqual(actualDigest, expectedDigest)) {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
}

/** 在 hash 调用路径中严格解析并编译权威 schema；任何初始化细节都归一化为稳定错误。 */
function compileValidator(schemaJson: unknown, expectedSha256: unknown): ValidateFunction {
  try {
    verifyEmbeddedSchemaIntegrity(schemaJson, expectedSha256);
    const schema: unknown = JSON.parse(schemaJson);
    assertValidGeneratedSchema(schema);
    // v1 用 Draft7 tuple 在 allOf 内只约束 index 0，外层通用 items 仍校验后续节点；
    // 因此关闭 Ajv 的“完整 tuple”静态告警，不能把 additionalItems:false 加到该断言而误杀后续计划节点。
    const ajv = new Ajv({
      strict: true,
      strictTuples: false,
      validateSchema: true,
      validateFormats: true,
      coerceTypes: false,
      useDefaults: false,
      removeAdditional: false,
      ownProperties: true,
    });
    ajv.addFormat("date-time", { type: "string", validate: isFactoryDateTime });
    return ajv.compile(schema);
  } catch {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
}

let runSpecValidator: ValidateFunction | undefined;
let planRevisionValidator: ValidateFunction | undefined;
let planRevisionDigestMaterialValidator: ValidateFunction | undefined;

/** 惰性构建 RunSpec validator，避免 generated 常量在 import 时直接影响应用入口。 */
function getRunSpecValidator(): ValidateFunction {
  runSpecValidator ??= compileValidator(RUN_SPEC_SCHEMA_JSON, RUN_SPEC_SCHEMA_JSON_SHA256);
  return runSpecValidator;
}

/** 惰性构建完整 PlanRevision validator，摘要字段剥离前先校验原始 wire。 */
function getPlanRevisionValidator(): ValidateFunction {
  planRevisionValidator ??= compileValidator(
    PLAN_REVISION_SCHEMA_JSON,
    PLAN_REVISION_SCHEMA_JSON_SHA256,
  );
  return planRevisionValidator;
}

/** 惰性构建摘要 material validator，确保只有两项排除字段可绕过摘要输入。 */
function getPlanRevisionDigestMaterialValidator(): ValidateFunction {
  planRevisionDigestMaterialValidator ??= compileValidator(
    PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON,
    PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON_SHA256,
  );
  return planRevisionDigestMaterialValidator;
}

/** 执行完整 schema；Ajv error 或异常均不得把 payload/内部错误带出 hash API。 */
function validateWire(value: unknown, validator: ValidateFunction): void {
  try {
    if (!validator(value)) {
      throw new PlanHashError(STABLE_VALIDATION_DETAIL);
    }
  } catch (error) {
    if (error instanceof PlanHashError) {
      throw error;
    }
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
}

/** 校验 value 为对象，否则 fail closed。 */
function requireObject(value: unknown): JsonObject {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value)
  ) {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
  return value as JsonObject;
}

/** 从 source 提取 fields 指定的字段子集，缺字段即 fail closed。 */
function projectSubset(
    source: JsonObject,
    fields: readonly string[],
): JsonObject {
  const projected: JsonObject = {};
  for (const name of fields) {
    if (!(name in source)) {
      throw new PlanHashError(STABLE_VALIDATION_DETAIL);
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
  validateWire(plan, getRunSpecValidator());
  const obj = requireObject(plan);
  const projection = projectSubset(obj, SEMANTIC_TOP_FIELDS);

  // repository：仅纳入 mode/root/baseBranch/baseCommit。
  const repository = requireObject(obj["repository"]);
  projection["repository"] = projectSubset(repository, REPO_FIELDS);

  // workPlan：仅纳入 dagVersion/nodes/barriers。
  const workPlan = requireObject(obj["workPlan"]);
  projection["workPlan"] = projectSubset(workPlan, WORKPLAN_FIELDS);

  return projection;
}

/** 计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。 */
function sha256Prefixed(bytes: Uint8Array): string {
  const digest = createHash("sha256").update(bytes).digest("hex");
  return `${SHA256_PREFIX}${digest}`;
}

/**
 * 计算 semanticPlanHash（跨修订版本稳定的计划语义身份）。
 * @throws PlanHashError 语义字段缺失、结构非法或字段值无法规范化。
 */
export function semanticPlanHash(plan: unknown): string {
  const projection = buildSemanticProjection(plan);
  try {
    return sha256Prefixed(canonicalize(projection));
  } catch {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
}

/**
 * 计算 planRevisionDigest（完整不可变修订记录身份）。
 * 排除 planRevisionDigest 与 signature，其余字段（含谱系、授权关联、
 * semanticPlanHash、生成时间）全部纳入。
 * @throws PlanHashError revision 非对象、结构非法或字段值无法规范化。
 */
export function planRevisionDigest(revision: unknown): string {
  validateWire(revision, getPlanRevisionValidator());
  const obj = requireObject(revision);
  // full schema 已保证摘要必填、签名可选；精确剥离后再校验机械派生的 material。
  const material: JsonObject = { ...obj };
  delete material.planRevisionDigest;
  if ("signature" in material) {
    delete material.signature;
  }
  validateWire(material, getPlanRevisionDigestMaterialValidator());
  // 纯函数没有日志出口；稳定错误由后续应用入口脱敏记录，本层不声称已实现 runtime 日志。
  try {
    return sha256Prefixed(canonicalize(material));
  } catch {
    throw new PlanHashError(STABLE_VALIDATION_DETAIL);
  }
}

/**
 * 计算 barrierId（稳定 barrier 身份，使用域分离数组）。
 * @throws PlanHashError 参数类型非法或参数值无法规范化。
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
