/**
 * packages/factory-contracts/src/event.test.ts
 *
 * Task 4 跨语言 golden vector 测试（TypeScript 侧）。
 * 从 repo 根 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json 读取
 * 与 Python/Rust 共享的冻结向量，断言 TypeScript 物化器产生字节级一致的结果。
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";

import { canonicalizeToString } from "./canonical.js";
import * as eventContracts from "./event.js";
import { EventHashError, eventId, payloadDigest, materializeBatch } from "./event.js";

// 定位 repo 根：本文件位于 packages/factory-contracts/src/。
const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, "..", "..", "..", "contracts", "golden");

function loadGolden(name: string): any {
  return JSON.parse(readFileSync(resolve(GOLDEN, name), "utf-8"));
}

const eventGolden = loadGolden("event-hash.v2.json");
const batchGolden = loadGolden("prepared-batch.v2.json");
const EXPECTED_REJECT_MUTATION_NAMES = [
  "input-root-unknown",
  "event-missing-run-id",
  "event-writer-field",
  "event-coarse-type",
  "event-nested-process-unknown",
  "event-digest-uppercase",
  "event-frame-digest-uppercase",
  "process-runtime-wsl-process",
  "process-runtime-container",
  "process-wsl-docker-missing-container",
  "process-local-missing-job",
  "process-local-unexpected-wsl",
  "source-span-equal-range",
  "source-span-reversed-range",
  "source-span-none-with-offset",
  "source-span-bool",
  "source-span-unsafe-integer",
  "sanitized-span-equal-range",
  "sanitized-span-reversed-range",
  "sanitized-span-bool",
  "redaction-range-equal",
  "redaction-range-reversed",
  "redaction-range-bool",
  "redaction-replacement-nonfrozen",
  "redaction-replacement-uppercase-tag",
  "redaction-nested-unknown",
  "wall-time-invalid",
  "ingested-at-invalid",
  "process-start-time-invalid",
  "head-task-seq-bool",
  "head-task-seq-unsafe-integer",
  "source-seq-bool",
  "source-seq-unsafe-integer",
  "batch-ordinal-bool",
  "batch-ordinal-unsafe-integer",
  "run-seq-bool",
  "run-seq-unsafe-integer",
  "monotonic-time-bool",
  "monotonic-time-unsafe-integer",
  "pid-bool",
  "pid-unsafe-integer",
  "checked-task-seq-overflow",
  "anchor-event-digest-drift",
  "anchor-task-seq-drift",
  "empty-events",
  "duplicate-ingest-id",
  "nfc-equivalent-ingest-id",
  "multi-task-slice",
  "model-summary-fail-closed",
  "state-changed-adapter-lane",
  "event-authoritative-durability-class",
  "missing-source-seq",
  "missing-source-span",
  "missing-process-identity",
  "stream-event-null-stream-id",
  "anchor-unknown-field",
  "batch-ordinal-negative",
  "batch-ordinal-nonmonotonic",
  "missing-coordinator-field",
  "half-null-input-head",
  "digest-pattern-short",
  "digest-pattern-wrong-prefix",
  "sanitized-span-unsafe-integer",
  "redaction-range-unsafe-integer",
] as const;
const EXPECTED_INTEGER_CASE_NAMES = [
  "integer-one",
  "decimal-integral-one",
  "exponent-integral-one",
  "negative-zero",
  "fractional-one-point-five",
  "nan",
  "positive-infinity",
  "negative-infinity",
  "unsafe-positive-integer",
  "boolean-true",
] as const;
const EXPECTED_SOURCE_SPAN_CASE_NAMES = [
  "provider-bytes-byte-range",
  "master-provider-bytes-frame-null",
  "provider-chars-byte-range",
  "provider-chars-field-null",
  "provider-bytes-none-null",
  "none-null",
  "provider-bytes-byte-start-null",
  "provider-bytes-byte-equal-range",
  "provider-bytes-frame-start-present",
  "provider-chars-field-end-present",
  "provider-bytes-none-end-present",
  "none-frame-precision",
  "none-start-present",
  "legacy-coordinate-byte",
] as const;

type PointerMutation = {
  name?: string;
  operation: "add" | "remove" | "replace";
  path: string;
  value?: unknown;
};

/** 复制共享 JSON 向量，保证每个 mutation 从同一合法基座独立执行。 */
function cloneJson<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

/** 解析 RFC 6901 pointer，并解码 ~0/~1。 */
function pointerParts(pointer: string): string[] {
  if (!pointer.startsWith("/")) {
    throw new Error("mutation pointer 必须为绝对路径");
  }
  return pointer.slice(1).split("/").map((part) =>
    part.replaceAll("~1", "/").replaceAll("~0", "~")
  );
}

/** 执行恰好一个 pointer mutation，并返回可逆操作。 */
function applyPointerMutation(
  document: Record<string, any>,
  mutation: PointerMutation,
): PointerMutation {
  const parts = pointerParts(mutation.path);
  let parent: any = document;
  for (const part of parts.slice(0, -1)) {
    parent = Array.isArray(parent) ? parent[Number(part)] : parent[part];
  }
  const leaf = parts.at(-1)!;
  if (mutation.operation === "add") {
    const value = cloneJson(mutation.value);
    if (Array.isArray(parent)) {
      const index = leaf === "-" ? parent.length : Number(leaf);
      parent.splice(index, 0, value);
      const inversePath = "/" + [...parts.slice(0, -1), String(index)].join("/");
      return { operation: "remove", path: inversePath };
    }
    if (Object.hasOwn(parent, leaf)) {
      throw new Error("add mutation 不得覆盖既有字段");
    }
    parent[leaf] = value;
    return { operation: "remove", path: mutation.path };
  }

  const index = Array.isArray(parent) ? Number(leaf) : leaf;
  const previous = cloneJson(parent[index]);
  if (mutation.operation === "remove") {
    if (Array.isArray(parent)) {
      parent.splice(index as number, 1);
    } else {
      delete parent[index];
    }
    return { operation: "add", path: mutation.path, value: previous };
  }
  parent[index] = cloneJson(mutation.value);
  return { operation: "replace", path: mutation.path, value: previous };
}

/** 使用共享真实 input/anchor envelope 调用物化器。 */
function materializeFixture(fixture: any): Record<string, any>[] {
  return materializeBatch(fixture.input, fixture.anchor) as Record<string, any>[];
}

/** 从共享文本构造语言运行时数字；NaN/Infinity 不能写成合法 JSON token。 */
function integerCaseValue(encoded: string): unknown {
  if (encoded === "NaN") return Number.NaN;
  if (encoded === "Infinity") return Number.POSITIVE_INFINITY;
  if (encoded === "-Infinity") return Number.NEGATIVE_INFINITY;
  return JSON.parse(encoded);
}

/** 独立用 canonical bytes 重算 SHA-256，不信任输出中的 digest 字段。 */
function independentDigest(value: unknown): string {
  return "sha256:" + createHash("sha256")
    .update(canonicalizeToString(value))
    .digest("hex");
}

describe("eventId vectors", () => {
  for (const c of eventGolden.eventId) {
    it(`eventId: ${c.name}`, () => {
      expect(eventId(c.ingestEventId)).toBe(c.expected);
    });
  }

  it("同一 ingestEventId → 幂等 eventId", () => {
    const inv = eventGolden.invariants.idempotentEventId;
    const byName = (n: string) =>
      eventGolden.eventId.find((x: any) => x.name === n);
    const ingest = byName(inv.a).ingestEventId;
    expect(eventId(ingest)).toBe(eventId(ingest));
  });
});

describe("payloadDigest vectors", () => {
  for (const c of eventGolden.payloadDigest) {
    it(`payloadDigest: ${c.name}`, () => {
      expect(payloadDigest(c.payload)).toBe(c.expected);
    });
  }
});

describe("Draft7 mathematical integer semantics", () => {
  it("共享 integer case-name exact-set 无重复", () => {
    const names = eventGolden.integerSemantics.map((item: any) => item.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names].sort()).toEqual([...EXPECTED_INTEGER_CASE_NAMES].sort());
  });

  for (const c of eventGolden.integerSemantics) {
    it(`integer semantics: ${c.name}`, () => {
      const fixture = cloneJson(eventGolden.rejectBase);
      fixture.input.events[0].sourceSeq = integerCaseValue(c.json);
      if (c.accepted) {
        const output = materializeFixture(fixture)[0].sourceSeq;
        expect(Number.isInteger(output)).toBe(true);
        expect(Object.is(output, -0)).toBe(false);
        expect(output).toBe(c.expected);
      } else {
        expect(() => materializeFixture(fixture)).toThrow(EventHashError);
      }
    });
  }
});

describe("conservative v1 source span semantics", () => {
  it("共享 source-span case-name exact-set 无重复", () => {
    const names = eventGolden.sourceSpanSemantics.map((item: any) => item.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names].sort()).toEqual([...EXPECTED_SOURCE_SPAN_CASE_NAMES].sort());
  });

  for (const c of eventGolden.sourceSpanSemantics) {
    it(`source span semantics: ${c.name}`, () => {
      const accepted = new Map<string, any>(
        eventGolden.sourceSpanSemantics
          .filter((item: any) => item.accepted)
          .map((item: any) => [item.name, item.span]),
      );
      if (c.accepted) {
        const fixture = cloneJson(eventGolden.rejectBase);
        fixture.input.events[0].sourceTransportSpan = cloneJson(c.span);
        expect(materializeFixture(fixture)[0].sourceTransportSpan).toEqual(c.span);
      } else {
        const baseSpan = cloneJson(accepted.get(c.baseCase));
        const baseFixture = cloneJson(eventGolden.rejectBase);
        baseFixture.input.events[0].sourceTransportSpan = cloneJson(baseSpan);
        expect(materializeFixture(baseFixture)[0].sourceTransportSpan).toEqual(baseSpan);

        const mutatedSpan = cloneJson(baseSpan);
        const inverse = applyPointerMutation(mutatedSpan, c.mutation);
        const fixture = cloneJson(eventGolden.rejectBase);
        fixture.input.events[0].sourceTransportSpan = mutatedSpan;
        expect(() => materializeFixture(fixture)).toThrow(EventHashError);
        applyPointerMutation(mutatedSpan, inverse);
        expect(mutatedSpan).toEqual(baseSpan);
      }
    });
  }
});

describe("materialize vectors", () => {
  for (const c of eventGolden.materialize) {
    it(`materialize: ${c.name}`, () => {
      expect(materializeBatch(c.input, c.anchor)).toEqual(c.expected);
    });
  }

  it("多 Task 交错：各自 genesis，taskSeq 与 eventDigest 相互独立", () => {
    const inv = eventGolden.invariants.interleavedTasksIndependent;
    const byName = (n: string) =>
      eventGolden.materialize.find((x: any) => x.name === n);
    const a = byName(inv.taskA);
    const b = byName(inv.taskB);
    const ra = materializeBatch(a.input, a.anchor);
    const rb = materializeBatch(b.input, b.anchor);
    expect(ra[0].taskSeq).toBe(0);
    expect(rb[0].taskSeq).toBe(0);
    // previousEventDigest 单链：两条独立链的 genesis eventDigest 必须不同（payload 不同）
    expect(ra[0].eventDigest).not.toBe(rb[0].eventDigest);
    // 两者 previousEventDigest 都是固定全零 predecessor（genesis）
    expect(ra[0].previousEventDigest).toBe(
      "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    );
    expect(rb[0].previousEventDigest).toBe(
      "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    );
    // 两个单 Task slice 来自同一真实 manifest，ordinal 在批内全局交错。
    expect(a.input.preparedBatchId).toBe("batch-multi-0");
    expect(b.input.preparedBatchId).toBe("batch-multi-0");
    expect(ra.map((event) => event.batchOrdinal)).toEqual([0, 2]);
    expect(rb.map((event) => event.batchOrdinal)).toEqual([1]);
  });
});

describe("single JSON Pointer mutation vectors", () => {
  it("合法 base 与完整 wsl-docker variant 均通过，case-name exact-set 一致", () => {
    const base = cloneJson(eventGolden.rejectBase);
    expect(() => materializeFixture(base)).not.toThrow();
    const names = eventGolden.rejectMutations.map((item: any) => item.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names].sort()).toEqual([...EXPECTED_REJECT_MUTATION_NAMES].sort());
    for (const mutation of eventGolden.validVariants) {
      const candidate = cloneJson(base);
      const inverse = applyPointerMutation(candidate, mutation);
      expect(() => materializeFixture(candidate)).not.toThrow();
      applyPointerMutation(candidate, inverse);
      expect(candidate).toEqual(base);
      expect(() => materializeFixture(candidate)).not.toThrow();
    }
  });

  for (const mutation of eventGolden.rejectMutations) {
    it(`single mutation: ${mutation.name}`, () => {
      const base = cloneJson(eventGolden.rejectBase);
      expect(() => materializeFixture(base)).not.toThrow();
      const candidate = cloneJson(base);
      const inverse = applyPointerMutation(candidate, mutation);
      let raised: unknown;
      try {
        materializeFixture(candidate);
      } catch (error) {
        raised = error;
      }
      expect(raised).toBeInstanceOf(EventHashError);
      expect((raised as EventHashError).errorCode).toBe("event-hash-error");
      expect((raised as Error).message).not.toContain("must-not-appear");
      applyPointerMutation(candidate, inverse);
      expect(candidate).toEqual(base);
      expect(() => materializeFixture(candidate)).not.toThrow();
    });
  }
});

describe("independent digest invariants", () => {
  function observable(events: Record<string, any>[], label: string): string {
    return label === "successor.previousEventDigest"
      ? events[1].previousEventDigest
      : events[0][label];
  }

  it("独立重算 payload/redaction/event digest 与 predecessor chain", () => {
    const base = cloneJson(eventGolden.rejectBase);
    const events = materializeFixture(base);
    expect(events[0].payloadDigest).toBe(independentDigest(events[0].payload));
    expect(events[0].redactionManifestDigest).toBe(
      independentDigest(events[0].redactions),
    );
    const digestMaterial = { ...events[0] };
    delete digestMaterial.eventDigest;
    expect(events[0].eventDigest).toBe(independentDigest(digestMaterial));
    expect(events[0].previousEventDigest).toBe(base.anchor.committedEventDigest);
    expect(events[1].previousEventDigest).toBe(events[0].eventDigest);
  });

  for (const mutation of eventGolden.digestInvariants) {
    it(`digest mutation: ${mutation.name}`, () => {
      const fixture = cloneJson(eventGolden.rejectBase);
      const baseline = materializeFixture(fixture);
      const candidate = cloneJson(fixture);
      const inverse = applyPointerMutation(candidate, mutation);
      const changed = materializeFixture(candidate);
      for (const label of mutation.changes) {
        expect(observable(changed, label)).not.toBe(observable(baseline, label));
      }
      for (const label of mutation.preserves) {
        expect(observable(changed, label)).toBe(observable(baseline, label));
      }
      expect(changed[1].previousEventDigest).toBe(changed[0].eventDigest);
      applyPointerMutation(candidate, inverse);
      expect(materializeFixture(candidate)).toEqual(baseline);
    });
  }
});

describe("prepared-batch canonical vectors", () => {
  for (const c of batchGolden.cases) {
    it(`canonical: ${c.name}`, () => {
      expect(canonicalizeToString(c.batch)).toBe(c.expectedCanonical);
    });
  }

  it("single manifest 使用连续 0..1 ordinal", () => {
    const single = batchGolden.cases.find(
      (item: any) => item.name === "genesis-single-task-manifest",
    ).batch;
    expect(single.firstBatchOrdinal).toBe(0);
    expect(single.lastBatchOrdinal).toBe(1);
    expect(single.eventCount).toBe(2);
    expect(single.orderedIngestIds).toHaveLength(2);
  });

  it("同 preparedBatchId 的多 Task slices union 精确等于 manifest", () => {
    const manifest = batchGolden.cases.find(
      (item: any) => item.name === "multi-task-interleaved-manifest",
    ).batch;
    const ordered = eventGolden.materialize
      .filter((item: any) => item.input.preparedBatchId === manifest.preparedBatchId)
      .flatMap((item: any) => item.input.events)
      .sort((left: any, right: any) => left.batchOrdinal - right.batchOrdinal);
    expect(ordered.map((event: any) => event.batchOrdinal)).toEqual([0, 1, 2]);
    expect(ordered.map((event: any) => event.ingestEventId)).toEqual(
      manifest.orderedIngestIds,
    );
    const normalizedIds = ordered.map((event: any) =>
      event.ingestEventId.normalize("NFC")
    );
    expect(new Set(normalizedIds).size).toBe(normalizedIds.length);
    expect(normalizedIds).toEqual(
      manifest.orderedIngestIds.map((ingestId: string) => ingestId.normalize("NFC")),
    );
    expect(new Set(ordered.map((event: any) => event.taskId))).toEqual(
      new Set(manifest.perTaskExpectedHeads.map((head: any) => head.taskId)),
    );
  });

  it("可执行 validator 拒绝每个单 pointer 语义 mutation，逆操作恢复", () => {
    const expectedNames = [
      "prepared-batch-event-count-mismatch",
      "prepared-batch-ordered-ingest-length-mismatch",
      "prepared-batch-global-ordinal-gap",
      "prepared-batch-half-null-head",
      "prepared-batch-duplicate-task-head",
      "prepared-batch-reversed-task-range",
      "prepared-batch-event-count-bool",
      "prepared-batch-writer-epoch-unsafe-integer",
      "prepared-batch-nfc-equivalent-ingest-id",
    ];
    const validate = (eventContracts as Record<string, unknown>)
      .validatePreparedBatchManifest;
    expect(typeof validate).toBe("function");
    const semanticValidate = validate as (value: unknown) => void;
    const base = cloneJson(batchGolden.rejectBase);
    expect(() => semanticValidate(base)).not.toThrow();
    const names = batchGolden.rejectMutations.map((item: any) => item.name);
    expect(new Set(names).size).toBe(names.length);
    expect([...names].sort()).toEqual(expectedNames.sort());
    for (const mutation of batchGolden.rejectMutations) {
      const candidate = cloneJson(base);
      const inverse = applyPointerMutation(candidate, mutation);
      expect(() => semanticValidate(candidate)).toThrow(EventHashError);
      applyPointerMutation(candidate, inverse);
      expect(candidate).toEqual(base);
      expect(() => semanticValidate(candidate)).not.toThrow();
    }
  });

  it("segment 仅 byte 精度允许非空递增 sourceSpan", () => {
    const semanticValidate = eventContracts.validatePreparedBatchManifest;
    const names = batchGolden.segmentSpanSemantics.map((item: any) => item.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toHaveLength(9);
    for (const c of batchGolden.segmentSpanSemantics) {
      const candidate = cloneJson(batchGolden.rejectBase);
      candidate.segmentDigests[0].mappingPrecision = c.mappingPrecision;
      candidate.segmentDigests[0].sourceSpan = cloneJson(c.sourceSpan);
      if (c.accepted) {
        expect(() => semanticValidate(candidate)).not.toThrow();
      } else {
        expect(() => semanticValidate(candidate)).toThrow(EventHashError);
      }
    }
  });
});
