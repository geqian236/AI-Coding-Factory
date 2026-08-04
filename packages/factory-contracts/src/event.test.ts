/**
 * packages/factory-contracts/src/event.test.ts
 *
 * Task 4 跨语言 golden vector 测试（TypeScript 侧）。
 * 从 repo 根 contracts/golden/event-hash.v2.json 与 prepared-batch.v2.json 读取
 * 与 Python/Rust 共享的冻结向量，断言 TypeScript 物化器产生字节级一致的结果。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";

import { CanonicalJsonError, canonicalizeToString } from "./canonical.js";
import { EventHashError, eventId, payloadDigest, materializeBatch } from "./event.js";

// 定位 repo 根：本文件位于 packages/factory-contracts/src/。
const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, "..", "..", "..", "contracts", "golden");

function loadGolden(name: string): any {
  return JSON.parse(readFileSync(resolve(GOLDEN, name), "utf-8"));
}

const eventGolden = loadGolden("event-hash.v2.json");
const batchGolden = loadGolden("prepared-batch.v2.json");

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

describe("materialize vectors", () => {
  for (const c of eventGolden.materialize) {
    it(`materialize: ${c.name}`, () => {
      expect(materializeBatch(c.batch, c.anchor, c.durableAt)).toEqual(c.expected);
    });
  }

  it("多 Task 交错：各自 genesis，taskSeq 与 head 相互独立", () => {
    const inv = eventGolden.invariants.interleavedTasksIndependent;
    const byName = (n: string) =>
      eventGolden.materialize.find((x: any) => x.name === n);
    const a = byName(inv.taskA);
    const b = byName(inv.taskB);
    const ra = materializeBatch(a.batch, a.anchor, a.durableAt);
    const rb = materializeBatch(b.batch, b.anchor, b.durableAt);
    expect(ra[0].taskSeq).toBe(0);
    expect(rb[0].taskSeq).toBe(0);
    expect(ra[0].head).not.toBe(rb[0].head);
  });
});

describe("materialize reject vectors", () => {
  for (const c of eventGolden.reject) {
    it(`reject: ${c.name}`, () => {
      expect(() => materializeBatch(c.batch, c.anchor, c.durableAt)).toThrow(
        EventHashError,
      );
    });
  }

  it("竞争批次：败者（previousHead 已过期）被拒", () => {
    const name = eventGolden.invariants.competingBatchOnlyOneWins;
    const c = eventGolden.reject.find((x: any) => x.name === name);
    expect(() => materializeBatch(c.batch, c.anchor, c.durableAt)).toThrow(
      EventHashError,
    );
  });
});

describe("prepared-batch canonical vectors", () => {
  for (const c of batchGolden.cases) {
    it(`canonical: ${c.name}`, () => {
      expect(canonicalizeToString(c.batch)).toBe(c.expectedCanonical);
    });
  }
});
