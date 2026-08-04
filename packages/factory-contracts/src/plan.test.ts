/**
 * packages/factory-contracts/src/plan.test.ts
 *
 * Task 3 跨语言 golden vector 测试（TypeScript 侧）。
 * 从 repo 根 contracts/golden/*.json 读取与 Python/Rust 共享的冻结向量，
 * 断言 TypeScript 实现产生字节级一致的结果。
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, it, expect } from "vitest";

import {
  CanonicalJsonError,
  canonicalizeJsonTextToString,
} from "./canonical.js";
import { semanticPlanHash, planRevisionDigest, barrierId } from "./plan.js";

// 定位 repo 根：本文件位于 packages/factory-contracts/src/。
const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, "..", "..", "..", "contracts", "golden");

function loadGolden(name: string): any {
  return JSON.parse(readFileSync(resolve(GOLDEN, name), "utf-8"));
}

const canonicalGolden = loadGolden("canonical-json.v1.json");
const planGolden = loadGolden("plan-hash.v1.json");
const barrierGolden = loadGolden("barrier-id.v1.json");

describe("canonical-json accept vectors", () => {
  for (const c of canonicalGolden.accept) {
    it(`accept: ${c.name}`, () => {
      const out = canonicalizeJsonTextToString(c.inputJsonText);
      expect(out).toBe(c.expectedCanonical);
    });
  }
});

describe("canonical-json reject vectors", () => {
  for (const c of canonicalGolden.reject) {
    it(`reject: ${c.name}`, () => {
      expect(() => canonicalizeJsonTextToString(c.inputJsonText)).toThrow(
        CanonicalJsonError,
      );
    });
  }
});

describe("semanticPlanHash vectors", () => {
  for (const c of planGolden.semanticPlanHash) {
    it(`semantic: ${c.name}`, () => {
      expect(semanticPlanHash(c.plan)).toBe(c.expected);
    });
  }

  it("语义相同、排除字段不同 → 共享 semanticPlanHash", () => {
    const inv = planGolden.invariants.sameSemanticsShareHash;
    const byName = (n: string) =>
      planGolden.semanticPlanHash.find((x: any) => x.name === n);
    expect(semanticPlanHash(byName(inv.a).plan)).toBe(
      semanticPlanHash(byName(inv.b).plan),
    );
  });
});

describe("planRevisionDigest vectors", () => {
  for (const c of planGolden.planRevisionDigest) {
    it(`digest: ${c.name}`, () => {
      expect(planRevisionDigest(c.revision)).toBe(c.expected);
    });
  }

  it("不同谱系 → 不同 planRevisionDigest", () => {
    const inv = planGolden.invariants.differentLineageDiffersDigest;
    const byName = (n: string) =>
      planGolden.planRevisionDigest.find((x: any) => x.name === n);
    expect(planRevisionDigest(byName(inv.a).revision)).not.toBe(
      planRevisionDigest(byName(inv.b).revision),
    );
  });
});

describe("barrierId vectors", () => {
  for (const c of barrierGolden.cases) {
    it(`barrier: ${c.name}`, () => {
      expect(
        barrierId(c.runId, c.planRevisionDigest, c.businessPhase, c.barrierOrdinal),
      ).toBe(c.expected);
    });
  }

  it("四个不同输入 → 四个不同 barrierId", () => {
    const ids = new Set(
      barrierGolden.cases.map((c: any) =>
        barrierId(c.runId, c.planRevisionDigest, c.businessPhase, c.barrierOrdinal),
      ),
    );
    expect(ids.size).toBe(barrierGolden.cases.length);
  });
});
