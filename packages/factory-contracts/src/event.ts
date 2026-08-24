/**
 * 单 Task MaterializationInput 到 DurableEventV2 的纯函数物化器。
 *
 * MaterializationInput 不是 PreparedBatchV2 wire manifest。它只接受 coordinator
 * 已 claim 的单 Task slice 和显式已提交锚点；本模块不执行 Task 7 的 group commit、
 * CAS、claim 或存储写入。
 */

import { createHash, timingSafeEqual } from "node:crypto";
import Ajv, { type AnySchema, type ValidateFunction } from "ajv";
import { canonicalize } from "./canonical.js";
import {
  DURABLE_EVENT_V2_SCHEMA_JSON,
  DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
  PREPARED_BATCH_V2_SCHEMA_JSON,
  PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
  PREPARED_EVENT_V2_SCHEMA_JSON,
  PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
} from "./generated/contracts.js";

/** 任何会影响输出字节的改动都必须同步更新三语言 golden vectors。 */
export const EVENT_HASH_VERSION = "event-hash-v2";
const EVENT_ID_DOMAIN = "factory-event-id-v2";
const SHA256_PREFIX = "sha256:";
const INVALID_EVENT_INPUT = "INVALID_EVENT_HASH_INPUT";
const DIGEST_RE = /^sha256:[a-f0-9]{64}$/;
const REDACTION_REPLACEMENT_RE = /^\[REDACTED(?::[a-z0-9._-]+)?\]$/;
const DRAFT7_SCHEMA_URI = "http://json-schema.org/draft-07/schema#";
const MAX_SAFE_INTEGER = 9_007_199_254_740_991;
const FACTORY_DATE_TIME = /^([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-5][0-9])(?:\.[0-9]+)?([Zz]|[+-][0-9]{2}:[0-9]{2})$/;

/** genesis 的固定 predecessor，不由 Adapter 或 coordinator 传入。 */
export const GENESIS_PREDECESSOR = SHA256_PREFIX + "0".repeat(64);

/** Adapter wire PreparedEventV2 的精确字段集。 */
const PREPARED_EVENT_FIELDS = [
  "schemaVersion",
  "taskId",
  "runId",
  "stepId",
  "attemptId",
  "source",
  "ingestEventId",
  "eventType",
  "providerEventId",
  "sourceSeq",
  "streamId",
  "sourceTransportSpan",
  "sanitizedStreamSpan",
  "wallTime",
  "monotonicTimeNs",
  "ingestedAt",
  "providerVersion",
  "adapterVersion",
  "processIdentity",
  "payload",
  "sanitizedProviderFrameDigest",
  "redactions",
] as const;

/** coordinator 只能在非 wire MaterializationInput 中附加的三个字段。 */
const COORDINATOR_EVENT_FIELDS = [
  "batchOrdinal",
  "runSeq",
  "durabilityClass",
] as const;
const MATERIALIZATION_INPUT_FIELDS = [
  "preparedBatchId",
  "taskId",
  "expectedTaskSeq",
  "expectedEventDigest",
  "events",
] as const;
const COMMITTED_ANCHOR_FIELDS = [
  "committedTaskSeq",
  "committedEventDigest",
] as const;
const MATERIALIZATION_EVENT_FIELDS = new Set<string>([
  ...PREPARED_EVENT_FIELDS,
  ...COORDINATOR_EVENT_FIELDS,
]);
const DURABILITY_CLASSES = new Set<string>([
  "side_effect_receipt",
  "provider_source",
  "derived",
]);

/** 事件物化边界的稳定错误类型，不回显不可信 payload。 */
export class EventHashError extends Error {
  readonly errorCode = "event-hash-error";

  constructor(detail: string) {
    super("[event-hash-error] " + detail);
    this.name = "EventHashError";
  }
}

/** 任意 JSON 对象别名。 */
type JsonObject = Record<string, unknown>;

/** 已提交的 Task head；genesis 必须为显式 (null, null)。 */
export interface CommittedEventAnchor {
  committedTaskSeq: number | null;
  committedEventDigest: string | null;
}

/** 保留原导出类型名，但其 wire 形态已收紧为 CommittedEventAnchor。 */
export type EventAnchor = CommittedEventAnchor;

/** 统一要求普通对象，拒绝 null、array 和原型形态歧义。 */
function requireObject(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new EventHashError(label + " 必须为对象");
  }
  return value as JsonObject;
}

/** exact-set 让 unknown 字段无法静默混入物化输入或 hash。 */
function requireExactKeys(
  value: JsonObject,
  expectedFields: readonly string[] | Set<string>,
  label: string,
): void {
  const expected = expectedFields instanceof Set
    ? expectedFields
    : new Set<string>(expectedFields);
  const actual = Object.keys(value);
  if (actual.length !== expected.size || actual.some((key) => !expected.has(key))) {
    throw new EventHashError(label + " 字段集合不符合 MaterializationInput 合同");
  }
}

/** 计算 sha256:<lowercase hex>。 */
function sha256Prefixed(bytes: Uint8Array): string {
  return SHA256_PREFIX + createHash("sha256").update(bytes).digest("hex");
}

/** 判断冻结的 SHA-256 wire 形态。 */
function isSha256Digest(value: unknown): value is string {
  return typeof value === "string" && DIGEST_RE.test(value);
}

/** 按 Draft7 数学整数语义校验安全范围，并把 -0 规范化为 +0。 */
function requireSafeNonNegativeInteger(value: unknown): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > MAX_SAFE_INTEGER
  ) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  return Object.is(value, -0) ? 0 : value;
}

/** 深拷贝不可信 JSON-like 输入，规范化时不反向修改调用方对象。 */
function cloneEventValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map((item) => cloneEventValue(item));
  }
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as JsonObject).map(([key, item]) => [key, cloneEventValue(item)]),
    );
  }
  return value;
}

/** taskSeq 等 writer 序号使用 checked arithmetic，禁止越过 I-JSON 上界。 */
function checkedSafeAdd(value: number, increment: number): number {
  const result = value + increment;
  if (!Number.isSafeInteger(result) || result > MAX_SAFE_INTEGER) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  return result;
}

/** canonical 错误在事件边界统一归一，禁止泄漏异常正文或 payload。 */
function canonicalizeEventHash(value: unknown): Uint8Array {
  try {
    return canonicalize(value);
  } catch {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
}

/** 校验显式 genesis/null 或非负整数 taskSeq。 */
function optionalTaskSeq(value: unknown, label: string): number | null {
  if (value === null) {
    return null;
  }
  void label;
  return requireSafeNonNegativeInteger(value);
}

/** 校验显式 genesis/null 或 SHA-256 event digest。 */
function optionalDigest(value: unknown, label: string): string | null {
  if (value === null) {
    return null;
  }
  if (!isSha256Digest(value)) {
    throw new EventHashError(label + " 必须为 sha256 digest 或 null");
  }
  return value;
}

/** 禁止 (null, digest) 或 (taskSeq, null) 半空 head。 */
function requireHeadPair(
  taskSeq: number | null,
  eventDigest: string | null,
  label: string,
): void {
  if ((taskSeq === null) !== (eventDigest === null)) {
    throw new EventHashError(label + " 的 taskSeq 与 eventDigest 必须同时为 null 或同时存在");
  }
}

/** 计算指定年月的天数，避免 Date 的年份兼容性差异。 */
function daysInMonth(year: number, month: number): number {
  if (month === 2) {
    return year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28;
  }
  return [4, 6, 9, 11].includes(month) ? 30 : 31;
}

/** 三端共用的冻结 RFC3339 子集，拒绝 leap second、year 0000 和非法 offset。 */
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
  if (
    year === 0 ||
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > daysInMonth(year, month) ||
    hour > 23 ||
    minute > 59
  ) {
    return false;
  }
  if (zone.toLowerCase() === "z") {
    return true;
  }
  return Number(zone.slice(1, 3)) <= 23 && Number(zone.slice(4, 6)) <= 59;
}

/** 内嵌 schema 在 JSON.parse 前先做 UTF-8 内容身份校验。 */
function verifyEmbeddedSchemaIntegrity(
  schemaJson: unknown,
  expectedSha256: unknown,
): asserts schemaJson is string {
  if (typeof schemaJson !== "string" || typeof expectedSha256 !== "string") {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  const match = /^sha256:([0-9a-f]{64})$/.exec(expectedSha256);
  if (match === null) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  const actual = createHash("sha256").update(schemaJson, "utf8").digest();
  const expected = Buffer.from(match[1], "hex");
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected)) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
}

/** 编译 codegen 的 Draft7 schema；初始化失败也不能泄漏 schema 或 payload 细节。 */
function compileEventValidator(
  schemaJson: unknown,
  expectedSha256: unknown,
): ValidateFunction {
  try {
    verifyEmbeddedSchemaIntegrity(schemaJson, expectedSha256);
    const schema: unknown = JSON.parse(schemaJson);
    if (
      schema === null ||
      typeof schema !== "object" ||
      Array.isArray(schema) ||
      (schema as JsonObject).$schema !== DRAFT7_SCHEMA_URI
    ) {
      throw new Error("invalid draft7 schema");
    }
    const ajv = new Ajv({
      strict: true,
      validateSchema: true,
      validateFormats: true,
      coerceTypes: false,
      useDefaults: false,
      removeAdditional: false,
      ownProperties: true,
    });
    ajv.addFormat("date-time", { type: "string", validate: isFactoryDateTime });
    return ajv.compile(schema as AnySchema);
  } catch (error) {
    if (error instanceof EventHashError) {
      throw error;
    }
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
}

let preparedEventValidator: ValidateFunction | undefined;
let durableEventValidator: ValidateFunction | undefined;
let preparedBatchValidator: ValidateFunction | undefined;

/** 按需编译 PreparedEventV2，避免 import 时无条件运行。 */
function getPreparedEventValidator(): ValidateFunction {
  preparedEventValidator ??= compileEventValidator(
    PREPARED_EVENT_V2_SCHEMA_JSON,
    PREPARED_EVENT_V2_SCHEMA_JSON_SHA256,
  );
  return preparedEventValidator;
}

/** 按需编译 DurableEventV2，确保输出也经过权威 schema。 */
function getDurableEventValidator(): ValidateFunction {
  durableEventValidator ??= compileEventValidator(
    DURABLE_EVENT_V2_SCHEMA_JSON,
    DURABLE_EVENT_V2_SCHEMA_JSON_SHA256,
  );
  return durableEventValidator;
}

/** 按需编译 PreparedBatchV2；运行时语义检查在 Draft7 之后执行。 */
function getPreparedBatchValidator(): ValidateFunction {
  preparedBatchValidator ??= compileEventValidator(
    PREPARED_BATCH_V2_SCHEMA_JSON,
    PREPARED_BATCH_V2_SCHEMA_JSON_SHA256,
  );
  return preparedBatchValidator;
}

/** 对 schema 错误使用稳定错误码，绝不传播 Ajv 的不可信值片段。 */
function validateWithAuthoritativeSchema(
  value: JsonObject,
  validator: ValidateFunction,
): void {
  try {
    if (!validator(value)) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
  } catch (error) {
    if (error instanceof EventHashError) {
      throw error;
    }
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
}

/** 从 Adapter 稳定 ingestEventId 推导幂等 eventId。 */
export function eventId(ingestEventId: string): string {
  if (typeof ingestEventId !== "string" || ingestEventId.length === 0) {
    throw new EventHashError("ingestEventId 必须为非空字符串");
  }
  return "evt_" + createHash("sha256")
    .update(canonicalizeEventHash([EVENT_ID_DOMAIN, ingestEventId]))
    .digest("hex");
}

/** 计算已脱敏 payload 的 JCS SHA-256 摘要。 */
export function payloadDigest(payload: unknown): string {
  return sha256Prefixed(canonicalizeEventHash(payload));
}

/** 计算 redactions 列表的 JCS SHA-256 摘要。 */
export function redactionManifestDigest(redactions: unknown): string {
  return sha256Prefixed(canonicalizeEventHash(redactions));
}

/** 校验 exact 输入/锚点并返回配对后的 committed head。 */
function validateMaterializationInput(
  rawInput: unknown,
  rawAnchor: unknown,
): {
  input: JsonObject;
  committedTaskSeq: number | null;
  committedEventDigest: string | null;
} {
  const input = requireObject(rawInput, "input");
  const anchor = requireObject(rawAnchor, "anchor");
  requireExactKeys(input, MATERIALIZATION_INPUT_FIELDS, "input");
  requireExactKeys(anchor, COMMITTED_ANCHOR_FIELDS, "anchor");

  const expectedTaskSeq = optionalTaskSeq(input.expectedTaskSeq, "input.expectedTaskSeq");
  const expectedEventDigest = optionalDigest(
    input.expectedEventDigest,
    "input.expectedEventDigest",
  );
  const committedTaskSeq = optionalTaskSeq(
    anchor.committedTaskSeq,
    "anchor.committedTaskSeq",
  );
  const committedEventDigest = optionalDigest(
    anchor.committedEventDigest,
    "anchor.committedEventDigest",
  );
  requireHeadPair(expectedTaskSeq, expectedEventDigest, "input");
  requireHeadPair(committedTaskSeq, committedEventDigest, "anchor");
  if (
    expectedTaskSeq !== committedTaskSeq ||
    expectedEventDigest !== committedEventDigest
  ) {
    throw new EventHashError("expected head 与已提交 anchor 不一致");
  }
  return { input, committedTaskSeq, committedEventDigest };
}

/** 校验 Draft7 无法表达的 range 顺序，并统一事件数字为 I-JSON 安全整数。 */
function validatePreparedEventSemantics(preparedEvent: JsonObject): void {
  preparedEvent.schemaVersion = requireSafeNonNegativeInteger(preparedEvent.schemaVersion);
  preparedEvent.sourceSeq = requireSafeNonNegativeInteger(preparedEvent.sourceSeq);
  preparedEvent.monotonicTimeNs = requireSafeNonNegativeInteger(
    preparedEvent.monotonicTimeNs,
  );
  const processIdentity = requireObject(preparedEvent.processIdentity, "processIdentity");
  processIdentity.pid = requireSafeNonNegativeInteger(processIdentity.pid);

  const sourceSpan = requireObject(preparedEvent.sourceTransportSpan, "sourceTransportSpan");
  // 坐标轴描述 Provider 原始传输单位，mappingPrecision 描述映射精度；二者不能按同名字面绑定。
  if (sourceSpan.coordinate === "none") {
    if (
      sourceSpan.mappingPrecision !== "none" ||
      sourceSpan.start !== null ||
      sourceSpan.endExclusive !== null
    ) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
  } else if (
    ["provider_transport_bytes", "provider_transport_chars"].includes(
      String(sourceSpan.coordinate),
    )
  ) {
    if (sourceSpan.mappingPrecision === "byte") {
      const start = requireSafeNonNegativeInteger(sourceSpan.start);
      const endExclusive = requireSafeNonNegativeInteger(sourceSpan.endExclusive);
      if (start >= endExclusive) {
        throw new EventHashError(INVALID_EVENT_INPUT);
      }
      sourceSpan.start = start;
      sourceSpan.endExclusive = endExclusive;
    } else if (["field", "frame", "none"].includes(String(sourceSpan.mappingPrecision))) {
      if (sourceSpan.start !== null || sourceSpan.endExclusive !== null) {
        throw new EventHashError(INVALID_EVENT_INPUT);
      }
    } else {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
  } else {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }

  const sanitizedSpan = requireObject(preparedEvent.sanitizedStreamSpan, "sanitizedStreamSpan");
  const sanitizedStart = requireSafeNonNegativeInteger(sanitizedSpan.start);
  const sanitizedEnd = requireSafeNonNegativeInteger(sanitizedSpan.endExclusive);
  if (sanitizedStart >= sanitizedEnd) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  sanitizedSpan.start = sanitizedStart;
  sanitizedSpan.endExclusive = sanitizedEnd;

  if (!Array.isArray(preparedEvent.redactions)) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  for (const rawRedaction of preparedEvent.redactions) {
    const redaction = requireObject(rawRedaction, "redaction");
    const byteRange = requireObject(redaction.byteRange, "redaction.byteRange");
    const redactionStart = requireSafeNonNegativeInteger(byteRange.start);
    const redactionEnd = requireSafeNonNegativeInteger(byteRange.endExclusive);
    if (
      redactionStart >= redactionEnd ||
      typeof redaction.replacement !== "string" ||
      !REDACTION_REPLACEMENT_RE.test(redaction.replacement)
    ) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    byteRange.start = redactionStart;
    byteRange.endExclusive = redactionEnd;
  }
}

/**
 * 验证 PreparedBatchV2 wire manifest 的 schema 与跨字段不变量。
 *
 * 本函数无 I/O/存储副作用；Task 7 才负责 claim、CAS、segments 与 group commit。
 */
export function validatePreparedBatchManifest(rawManifest: unknown): void {
  const manifest = requireObject(rawManifest, "preparedBatch");
  validateWithAuthoritativeSchema(manifest, getPreparedBatchValidator());
  const eventCount = requireSafeNonNegativeInteger(manifest.eventCount);
  const firstOrdinal = requireSafeNonNegativeInteger(manifest.firstBatchOrdinal);
  const lastOrdinal = requireSafeNonNegativeInteger(manifest.lastBatchOrdinal);
  const orderedIngestIds = manifest.orderedIngestIds;
  if (
    !Array.isArray(orderedIngestIds) ||
    lastOrdinal < firstOrdinal ||
    eventCount !== orderedIngestIds.length ||
    eventCount !== lastOrdinal - firstOrdinal + 1
  ) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  const normalizedIngestIds = new Set<string>();
  for (const ingestId of orderedIngestIds) {
    if (typeof ingestId !== "string" || ingestId.length === 0) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    const normalizedIngestId = ingestId.normalize("NFC");
    if (normalizedIngestIds.has(normalizedIngestId)) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    normalizedIngestIds.add(normalizedIngestId);
  }

  if (!Array.isArray(manifest.perTaskExpectedHeads)) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  const taskIds = new Set<string>();
  for (const rawHead of manifest.perTaskExpectedHeads) {
    const head = requireObject(rawHead, "perTaskExpectedHead");
    const taskId = head.taskId;
    const headFirst = requireSafeNonNegativeInteger(head.firstBatchOrdinal);
    const headLast = requireSafeNonNegativeInteger(head.lastBatchOrdinal);
    const expectedTaskSeq = optionalTaskSeq(head.expectedTaskSeq, "head.expectedTaskSeq");
    const expectedDigest = optionalDigest(head.expectedEventDigest, "head.expectedEventDigest");
    requireHeadPair(expectedTaskSeq, expectedDigest, "head");
    if (
      typeof taskId !== "string" ||
      taskIds.has(taskId) ||
      headFirst > headLast ||
      headFirst < firstOrdinal ||
      headLast > lastOrdinal
    ) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    taskIds.add(taskId);
  }

  if (!Array.isArray(manifest.segmentDigests)) {
    throw new EventHashError(INVALID_EVENT_INPUT);
  }
  for (const rawSegment of manifest.segmentDigests) {
    const segment = requireObject(rawSegment, "segmentDigest");
    const ordinalRange = requireObject(segment.ordinalRange, "segment.ordinalRange");
    const segmentFirst = requireSafeNonNegativeInteger(ordinalRange.first);
    const segmentLast = requireSafeNonNegativeInteger(ordinalRange.lastExclusive);
    const sanitizedSpan = requireObject(segment.sanitizedSpan, "segment.sanitizedSpan");
    const sanitizedStart = requireSafeNonNegativeInteger(sanitizedSpan.start);
    const sanitizedEnd = requireSafeNonNegativeInteger(sanitizedSpan.endExclusive);
    if (segment.mappingPrecision === "byte") {
      const sourceSpan = requireObject(segment.sourceSpan, "segment.sourceSpan");
      const sourceStart = requireSafeNonNegativeInteger(sourceSpan.start);
      const sourceEnd = requireSafeNonNegativeInteger(sourceSpan.endExclusive);
      if (sourceStart >= sourceEnd) {
        throw new EventHashError(INVALID_EVENT_INPUT);
      }
    } else if (["field", "frame", "none"].includes(String(segment.mappingPrecision))) {
      if (segment.sourceSpan !== null) {
        throw new EventHashError(INVALID_EVENT_INPUT);
      }
    } else {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    if (segmentFirst >= segmentLast || sanitizedStart >= sanitizedEnd) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
  }
}

/**
 * 物化单 Task MaterializationInput 为已验证 DurableEventV2 列表。
 *
 * 该函数无日志、无 I/O、无存储副作用；Task 7 才从真实 PreparedBatch manifest/
 * segments 生成每个 Task slice 并负责多 Task 提交。
 */
export function materializeBatch(
  rawInput: unknown,
  rawAnchor: unknown,
): JsonObject[] {
  const { input, committedTaskSeq, committedEventDigest } =
    validateMaterializationInput(rawInput, rawAnchor);
  const taskId = input.taskId;
  const preparedBatchId = input.preparedBatchId;
  const events = input.events;
  if (typeof taskId !== "string" || taskId.length === 0) {
    throw new EventHashError("input.taskId 必须为非空字符串");
  }
  if (typeof preparedBatchId !== "string" || preparedBatchId.length === 0) {
    throw new EventHashError("input.preparedBatchId 必须为非空字符串");
  }
  if (!Array.isArray(events) || events.length === 0) {
    throw new EventHashError("input.events 必须为非空数组");
  }

  const baseTaskSeq = committedTaskSeq === null
    ? 0
    : checkedSafeAdd(committedTaskSeq, 1);
  let previousEventDigest = committedEventDigest ?? GENESIS_PREDECESSOR;
  let previousBatchOrdinal: number | undefined;
  const seenIngestIds = new Set<string>();
  const durableEvents: JsonObject[] = [];

  for (let index = 0; index < events.length; index++) {
    const event = requireObject(events[index], "input.events[" + index + "]");
    requireExactKeys(
      event,
      MATERIALIZATION_EVENT_FIELDS,
      "input.events[" + index + "]",
    );
    if (event.taskId !== taskId) {
      throw new EventHashError("切片内 event.taskId 与 input.taskId 不一致");
    }

    // 去掉 coordinator 三字段后必须恰好是权威 PreparedEventV2。
    const preparedEvent: JsonObject = {};
    for (const field of PREPARED_EVENT_FIELDS) {
      preparedEvent[field] = cloneEventValue(event[field]);
    }
    validateWithAuthoritativeSchema(preparedEvent, getPreparedEventValidator());
    validatePreparedEventSemantics(preparedEvent);

    const ingestEventId = event.ingestEventId;
    if (typeof ingestEventId !== "string" || ingestEventId.length === 0) {
      throw new EventHashError(INVALID_EVENT_INPUT);
    }
    // NFC 等价 ID 会导出相同 eventId，必须在 hash 前拒绝。
    const normalizedIngestId = ingestEventId.normalize("NFC");
    if (seenIngestIds.has(normalizedIngestId)) {
      throw new EventHashError("切片内 ingestEventId 重复");
    }
    seenIngestIds.add(normalizedIngestId);

    const batchOrdinal = event.batchOrdinal;
    const runSeq = event.runSeq;
    const durabilityClass = event.durabilityClass;
    const safeBatchOrdinal = requireSafeNonNegativeInteger(batchOrdinal);
    if (
      previousBatchOrdinal !== undefined &&
      safeBatchOrdinal <= previousBatchOrdinal
    ) {
      throw new EventHashError("单 Task slice 的 batchOrdinal 必须严格递增");
    }
    previousBatchOrdinal = safeBatchOrdinal;
    const safeRunSeq = requireSafeNonNegativeInteger(runSeq);
    if (
      typeof durabilityClass !== "string" ||
      !DURABILITY_CLASSES.has(durabilityClass)
    ) {
      throw new EventHashError("event.durabilityClass 不在 DurableEventV2 枚举内");
    }

    // writer 只分配 head/digest 相关字段；所有 Adapter 字段逐字复制。
    const durable: JsonObject = {
      schemaVersion: preparedEvent.schemaVersion,
      durabilityClass,
      eventId: eventId(ingestEventId),
      taskId,
      taskSeq: checkedSafeAdd(baseTaskSeq, index),
      runId: preparedEvent.runId,
      runSeq: safeRunSeq,
      stepId: preparedEvent.stepId,
      attemptId: preparedEvent.attemptId,
      source: preparedEvent.source,
      ingestEventId,
      eventType: preparedEvent.eventType,
      providerEventId: preparedEvent.providerEventId,
      sourceSeq: preparedEvent.sourceSeq,
      streamId: preparedEvent.streamId,
      preparedBatchId,
      batchOrdinal: safeBatchOrdinal,
      sourceTransportSpan: preparedEvent.sourceTransportSpan,
      sanitizedStreamSpan: preparedEvent.sanitizedStreamSpan,
      wallTime: preparedEvent.wallTime,
      monotonicTimeNs: preparedEvent.monotonicTimeNs,
      ingestedAt: preparedEvent.ingestedAt,
      providerVersion: preparedEvent.providerVersion,
      adapterVersion: preparedEvent.adapterVersion,
      processIdentity: preparedEvent.processIdentity,
      payload: preparedEvent.payload,
      sanitizedProviderFrameDigest: preparedEvent.sanitizedProviderFrameDigest,
      payloadDigest: payloadDigest(preparedEvent.payload),
      previousEventDigest,
      redactions: preparedEvent.redactions,
      redactionManifestDigest: redactionManifestDigest(preparedEvent.redactions),
    };
    durable.eventDigest = sha256Prefixed(canonicalizeEventHash(durable));

    // eventDigest 已加入后验证最终 DurableEventV2，阻断构造/枚举漂移。
    validateWithAuthoritativeSchema(durable, getDurableEventValidator());
    durableEvents.push(durable);
    previousEventDigest = durable.eventDigest as string;
  }

  return durableEvents;
}
