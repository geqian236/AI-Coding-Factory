/**
 * packages/factory-contracts/src/event.ts
 *
 * PreparedBatchV2 → DurableEventV2 纯函数物化器 —— TypeScript 实现。
 * 与 Python (factory_agent.domain.events) 及 Rust (factory_contracts::event)
 * 字节级一致，共用同一 canonical.ts 底座（NFC + RFC 8785 JCS）。
 *
 * GPT 第二轮审核指出早期实现只放了 §10.1 子集字段；本修订补齐：
 * 执行身份（runId/runSeq/stepId/attemptId/source/processIdentity）、
 * 双 span（sourceTransportSpan/sanitizedStreamSpan）、
 * 脱敏证据（redactions/redactionManifestDigest/sanitizedProviderFrameDigest/
 * mappingPrecision）、schemaVersion、durabilityClass、providerEventId、
 * streamId、preparedBatchId、wallTime、monotonicTimeNs、ingestedAt、
 * providerVersion、adapterVersion、eventType 改为 §10.1 具体枚举。
 *
 * 算法（严格来自 Master Spec §10.1）：
 *
 *   eventId
 *       "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))
 *       仅由 ingestEventId 决定，使崩溃重试得到同一身份（幂等）。
 *
 *   payloadDigest
 *       "sha256:" + lowercaseHex(SHA-256(JCS(payload)))
 *
 *   redactionManifestDigest
 *       "sha256:" + lowercaseHex(SHA-256(JCS(redactions 列表)))
 *
 *   eventDigest
 *       "sha256:" + lowercaseHex(SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后的
 *       完整对象)))；previousEventDigest 仍参与计算。previousEventDigest 单链模型：
 *       本事件无 head 字段，下一事件的 previousEventDigest 指向前一事件的 eventDigest；
 *       genesis 事件 previousEventDigest = "sha256:" + 64 个 0。
 *
 * 物化规则（fail-closed）：
 *   - batch.previousHead 必须与 anchor.committedHead 相等（同为 null 或同字符串）。
 *   - 批内所有事件的 taskId 必须等于 batch.taskId。
 *   - 批至少含一个事件；ingestEventId 批内不得重复。
 *   - 每个事件必须含 §10.1 完整字段集（required by durable-event.v2 schema）。
 *   - processIdentity 必填子字段不能缺失。
 *   - 双 span 必填子字段不能缺失。
 *   - taskSeq 由 anchor 单调延续：genesis 从 0 开始，否则从 committedTaskSeq+1 开始。
 *   - batchOrdinal 为事件在批内序号，原样从 event 拷贝。
 *   - redactions 列表空表示本事件未脱敏。
 *
 * 失败错误码：event-hash-error。
 */

import { createHash } from "node:crypto";
import { canonicalize } from "./canonical.js";

/** 算法版本：影响输出字节的任何改动都必须同步升级并更新 golden vectors。 */
export const EVENT_HASH_VERSION = "event-hash-v2";

/** eventId 域分离标签（数组首元素）。 */
const EVENT_ID_DOMAIN = "factory-event-id-v2";

/** 摘要输出前缀。 */
const SHA256_PREFIX = "sha256:";

/** genesis 事件的固定前驱：sha256: + 64 个 0（Master Spec §10.1 全零 predecessor）。 */
export const GENESIS_PREDECESSOR = `${SHA256_PREFIX}${"0".repeat(64)}`;

/** eventDigest 计算时必须排除的字段（仅排除自身摘要，避免自指循环）。
 *  previousEventDigest 仍参与计算（Master Spec §10.1）。 */
const DIGEST_EXCLUDED_FIELDS = new Set(["eventDigest"]);

/** PreparedEventV2 必需字段（每个 batch.events[i] 必须含）。
 *  全字段集按 §10.1：执行身份、双 span、脱敏、时间、版本。 */
const REQUIRED_PREPARED_EVENT_FIELDS = [
  // 身份
  "ingestEventId", "taskId", "runId", "stepId", "attemptId", "source",
  // 类型与 Provider 引用
  "eventType", "providerEventId", "sourceSeq", "streamId",
  // Prepared 关联（preparedBatchId 在 batch 顶层；events 每项含 batchOrdinal）
  "batchOrdinal",
  // 双 span
  "sourceTransportSpan", "sanitizedStreamSpan",
  // 时间 / 版本
  "wallTime", "monotonicTimeNs", "ingestedAt", "providerVersion", "adapterVersion",
  // 受管进程身份
  "processIdentity",
  // Payload / 脱敏
  "payload", "sanitizedProviderFrameDigest", "redactions",
] as const;

/** PreparedEventV2 允许出现的字段集（对齐 schema 的 additionalProperties:false）。
 *  出现集合外字段即 fail closed。 */
const ALLOWED_PREPARED_EVENT_FIELDS = new Set<unknown>([
  ...REQUIRED_PREPARED_EVENT_FIELDS,
  "payloadDigest",
  "redactionManifestDigest",
  "previousEventDigest",
  "eventDigest",
  "runSeq",
  "durabilityClass",
]);

/** 事件物化输入非法（缺字段、类型错误、前驱漂移、重复摄取 ID 等）时抛出。 */
export class EventHashError extends Error {
  readonly errorCode = "event-hash-error";
  constructor(detail: string) {
    super(`[event-hash-error] ${detail}`);
    this.name = "EventHashError";
  }
}

/** 任意 JSON 对象别名。 */
type JsonObject = Record<string, unknown>;

/** 该 Task 当前已提交锚点（genesis 时两字段均为 null）。 */
export interface EventAnchor {
  /** 已提交的最后一个 taskSeq；genesis 为 null。 */
  committedTaskSeq: number | null;
  /** 已提交链的 head；genesis 为 null。 */
  committedHead: string | null;
}

/** 校验 value 为对象，否则 fail closed。 */
function requireObject(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new EventHashError(`字段 '${label}' 必须为对象`);
  }
  return value as JsonObject;
}

/** 计算 SHA-256 并返回 "sha256:<64 位小写十六进制>"。 */
function sha256Prefixed(bytes: Uint8Array): string {
  const digest = createHash("sha256").update(bytes).digest("hex");
  return `${SHA256_PREFIX}${digest}`;
}

/**
 * 计算幂等 eventId（仅由 ingestEventId 决定）。
 * @throws EventHashError ingestEventId 非非空字符串。
 */
export function eventId(ingestEventId: string): string {
  if (typeof ingestEventId !== "string" || ingestEventId.length === 0) {
    throw new EventHashError("ingestEventId 必须为非空字符串");
  }
  const domainArray: unknown[] = [EVENT_ID_DOMAIN, ingestEventId];
  const digest = createHash("sha256").update(canonicalize(domainArray)).digest("hex");
  return `evt_${digest}`;
}

/**
 * 计算 payloadDigest（脱敏后 payload 的 JCS 摘要）。
 * @throws CanonicalJsonError payload 含非法数字/类型/重复键。
 */
export function payloadDigest(payload: unknown): string {
  return sha256Prefixed(canonicalize(payload));
}

/**
 * 计算 redactionManifestDigest（redactions 列表的 JCS 摘要）。
 * @throws CanonicalJsonError redactions 含非法数字/类型/重复键。
 */
export function redactionManifestDigest(redactions: unknown): string {
  return sha256Prefixed(canonicalize(redactions));
}

/**
 * 由 anchor 推导本批次首事件的 taskSeq。
 * genesis（committedTaskSeq 为 null）从 0 开始，否则从 committedTaskSeq+1 开始。
 * @throws EventHashError committedTaskSeq 存在但非整数或为负。
 */
function baseTaskSeq(anchor: EventAnchor): number {
  const committed = anchor.committedTaskSeq;
  if (committed === null || committed === undefined) {
    return 0;
  }
  if (typeof committed !== "number" || !Number.isInteger(committed)) {
    throw new EventHashError("anchor.committedTaskSeq 必须为整数或 null");
  }
  if (committed < 0) {
    throw new EventHashError("anchor.committedTaskSeq 不得为负");
  }
  return committed + 1;
}

/**
 * 把一个 PreparedBatchV2 物化为按序连接的 DurableEventV2 列表（纯函数）。
 * 每个 PreparedEventV2 必须含 §10.1 全字段集；物化器直通到 DurableEventV2。
 * @throws EventHashError 缺字段 / 未知字段 / 前驱漂移 / taskId 不一致 /
 *                        ingestEventId 重复 / processIdentity 不合规。
 * @throws CanonicalJsonError 任一字段值无法规范化。
 */
export function materializeBatch(
  batch: unknown,
  anchor: EventAnchor,
  durableAt: string,
): JsonObject[] {
  const batchObj = requireObject(batch, "batch");

  const taskId = batchObj["taskId"];
  if (typeof taskId !== "string" || taskId.length === 0) {
    throw new EventHashError("batch.taskId 必须为非空字符串");
  }

  const preparedBatchId = batchObj["preparedBatchId"];
  if (typeof preparedBatchId !== "string" || preparedBatchId.length === 0) {
    throw new EventHashError("batch.preparedBatchId 必须为非空字符串");
  }

  const events = batchObj["events"];
  if (!Array.isArray(events) || events.length === 0) {
    throw new EventHashError("batch.events 必须为非空数组");
  }

  // 前驱校验：batch.previousHead 必须与 anchor.committedHead 完全一致。
  const batchPrev = batchObj["previousHead"] ?? null;
  const committedHead = anchor.committedHead ?? null;
  if (batchPrev !== committedHead) {
    throw new EventHashError(
      "前驱漂移：batch.previousHead 与 anchor.committedHead 不一致，拒绝物化",
    );
  }

  const baseSeq = baseTaskSeq(anchor);

  // genesis（无已提交 head）首事件前驱为全零 predecessor，否则接已提交 head。
  let prevEventDigest: string =
    committedHead === null ? GENESIS_PREDECESSOR : committedHead;

  const durableEvents: JsonObject[] = [];
  const seenIngest = new Set<string>();

  for (let index = 0; index < events.length; index++) {
    const event = requireObject(events[index], `events[${index}]`);

    // 未知字段拒绝（对齐 schema additionalProperties:false）。
    for (const key of Object.keys(event)) {
      if (!ALLOWED_PREPARED_EVENT_FIELDS.has(key)) {
        throw new EventHashError(
          `events[${index}] 含未声明字段 '${key}'（${EVENT_HASH_VERSION}）`,
        );
      }
    }
    // 必需字段校验。
    for (const field of REQUIRED_PREPARED_EVENT_FIELDS) {
      if (!(field in event)) {
        throw new EventHashError(
          `events[${index}] 缺少必需字段 '${field}'（${EVENT_HASH_VERSION}）`,
        );
      }
    }

    const ingestId = event["ingestEventId"];
    if (typeof ingestId !== "string" || ingestId.length === 0) {
      throw new EventHashError(`events[${index}].ingestEventId 必须为非空字符串`);
    }
    if (seenIngest.has(ingestId)) {
      throw new EventHashError(`批内 ingestEventId 重复：'${ingestId}'`);
    }
    seenIngest.add(ingestId);

    // 显式类型注解：避免 TS7022（隐式 any 因为三元表达式一侧是 unknown）。
    const eventTask: string = (event["taskId"] as string);
    if (eventTask !== taskId) {
      throw new EventHashError(
        `events[${index}].taskId 与 batch.taskId 不一致，拒绝物化`,
      );
    }

    // processIdentity 必填子字段校验（schema required 重复保险）
    const pi = event["processIdentity"];
    if (pi === null || typeof pi !== "object" || Array.isArray(pi)) {
      throw new EventHashError(`events[${index}].processIdentity 必须为对象`);
    }
    for (const sub of [
      "executorId", "hostId", "runtime", "executableDigest",
      "pid", "processStartTime", "jobObjectId",
    ]) {
      if (!(sub in (pi as JsonObject))) {
        throw new EventHashError(
          `events[${index}].processIdentity 缺子字段 '${sub}'`,
        );
      }
    }

    // 双 span 必填子字段校验
    const sts = event["sourceTransportSpan"];
    const sss = event["sanitizedStreamSpan"];
    if (
      sts === null || typeof sts !== "object" || Array.isArray(sts) ||
      sss === null || typeof sss !== "object" || Array.isArray(sss)
    ) {
      throw new EventHashError(
        `events[${index}].sourceTransportSpan/sanitizedStreamSpan 必须为对象`,
      );
    }
    for (const sub of ["coordinate", "start", "endExclusive", "mappingPrecision"]) {
      if (!(sub in (sts as JsonObject))) {
        throw new EventHashError(
          `events[${index}].sourceTransportSpan 缺子字段 '${sub}'`,
        );
      }
    }
    for (const sub of ["segmentId", "start", "endExclusive"]) {
      if (!(sub in (sss as JsonObject))) {
        throw new EventHashError(
          `events[${index}].sanitizedStreamSpan 缺子字段 '${sub}'`,
        );
      }
    }

    const taskSeq = baseSeq + index;

    // 派生字段
    const redactions = event["redactions"];
    if (!Array.isArray(redactions)) {
      throw new EventHashError(`events[${index}].redactions 必须为数组`);
    }

    const durable: JsonObject = {
      // 版本 / 等级
      schemaVersion: 2,
      durabilityClass: event["durabilityClass"] ?? "derived",
      // 身份（执行身份链）
      eventId: eventId(ingestId as string),
      taskId: taskId as string,
      taskSeq,
      runId: event["runId"],
      runSeq: event["runSeq"] ?? 0,
      stepId: event["stepId"],
      attemptId: event["attemptId"],
      source: event["source"],
      // Provider 引用
      ingestEventId: ingestId,
      eventType: event["eventType"],
      providerEventId: event["providerEventId"],
      sourceSeq: event["sourceSeq"],
      streamId: event["streamId"],
      // Prepared 关联
      preparedBatchId: preparedBatchId as string,
      batchOrdinal: event["batchOrdinal"],
      // 双 span
      sourceTransportSpan: sts,
      sanitizedStreamSpan: sss,
      // 时间 / 版本
      wallTime: event["wallTime"],
      monotonicTimeNs: event["monotonicTimeNs"],
      ingestedAt: event["ingestedAt"] ?? durableAt,
      providerVersion: event["providerVersion"],
      adapterVersion: event["adapterVersion"],
      // 受管进程身份
      processIdentity: pi,
      // Payload / 脱敏
      payload: event["payload"],
      sanitizedProviderFrameDigest: event["sanitizedProviderFrameDigest"],
      payloadDigest: payloadDigest(event["payload"]),
      // 链
      previousEventDigest: prevEventDigest,
      redactions,
      redactionManifestDigest: redactionManifestDigest(redactions),
    };

    // eventDigest = JCS(去除 eventDigest 字段后完整对象) 的 SHA-256。
    const digestInput: JsonObject = {};
    for (const [k, v] of Object.entries(durable)) {
      if (!DIGEST_EXCLUDED_FIELDS.has(k)) {
        digestInput[k] = v;
      }
    }
    const eventDigest = sha256Prefixed(canonicalize(digestInput));
    durable["eventDigest"] = eventDigest;

    durableEvents.push(durable);
    prevEventDigest = eventDigest; // 下一事件 previousEventDigest 指向前一事件 eventDigest。
  }

  return durableEvents;
}