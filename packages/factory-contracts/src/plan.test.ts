/**
 * packages/factory-contracts/src/plan.test.ts
 *
 * Task 3 跨语言 golden vector 测试（TypeScript 侧）。
 * 从 repo 根 contracts/golden/*.json 读取与 Python/Rust 共享的冻结向量，
 * 断言 TypeScript 实现产生字节级一致的结果。
 */

import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it, vi } from "vitest";

import {
  CanonicalJsonError,
  canonicalizeJsonTextToString,
} from "./canonical.js";
import {
  PlanHashError,
  barrierId,
  buildSemanticProjection,
  planRevisionDigest,
  semanticPlanHash,
} from "./plan.js";

// 定位 repo 根：本文件位于 packages/factory-contracts/src/。
const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = resolve(HERE, "..", "..", "..", "contracts", "golden");

function loadGolden(name: string): any {
  return JSON.parse(readFileSync(resolve(GOLDEN, name), "utf-8"));
}

const canonicalGolden = loadGolden("canonical-json.v1.json");
const planGolden = loadGolden("plan-hash.v1.json");
const barrierGolden = loadGolden("barrier-id.v1.json");

function findNamedCase(cases: any[], name: string): any {
  const found = cases.find((item: any) => item.name === name);
  if (found === undefined) {
    throw new Error(`missing golden base case: ${name}`);
  }
  return found;
}

function applyGoldenMutation(payload: unknown, mutation: any): unknown {
  // 单条变异只操作独立 payload 副本，避免同组负例之间相互污染。
  const path = mutation.path as string[];
  if (!Array.isArray(path) || path.length === 0) {
    throw new Error("golden mutation path must be non-empty");
  }

  // path 同时允许对象键和数组十进制下标，三语言均从同一 shared golden 物化 DAG 反例。
  let target: Record<string, unknown> | unknown[] = payload as Record<string, unknown> | unknown[];
  for (const segment of path.slice(0, -1)) {
    const child = Array.isArray(target) ? target[Number(segment)] : target[segment];
    if (child === null || typeof child !== "object") {
      throw new Error(`golden mutation path is not a container: ${segment}`);
    }
    target = child as Record<string, unknown> | unknown[];
  }

  const leaf = path.at(-1)!;
  if (mutation.op === "remove") {
    if (Array.isArray(target)) {
      target.splice(Number(leaf), 1);
    } else {
      delete target[leaf];
    }
  } else if (mutation.op === "replace") {
    if (Array.isArray(target)) {
      target[Number(leaf)] = structuredClone(mutation.value);
    } else {
      target[leaf] = structuredClone(mutation.value);
    }
  } else {
    throw new Error(`unsupported golden mutation: ${mutation.op}`);
  }
  return payload;
}

function materializeMutationCases(
  caseItem: any,
  sourceCases: any[],
  payloadKey: string,
): Array<{ name: string; payload: unknown }> {
  // 共享 golden 只描述变异；同一 pytest/Vitest 节点内逐项执行多变异，冻结计数不漂移。
  if (Object.hasOwn(caseItem, "input")) {
    return [{ name: caseItem.name, payload: structuredClone(caseItem.input) }];
  }

  const mutations = caseItem.mutations ?? [caseItem.mutation];
  if (!Array.isArray(mutations) || mutations.length === 0) {
    throw new Error("golden mutations must be a non-empty array");
  }
  return mutations.map((mutation: any, index: number) => {
    // 仅“字段缺失”可回退到序号；显式 null、非字符串或空字符串都是损坏的 golden 元数据。
    const label = Object.hasOwn(mutation, "name") ? mutation.name : String(index);
    if (typeof label !== "string" || label.length === 0) {
      throw new Error("golden mutation name must be non-empty");
    }
    return {
      name: `${caseItem.name}:${label}`,
      payload: applyGoldenMutation(
        structuredClone(findNamedCase(sourceCases, caseItem.base)[payloadKey]),
        mutation,
      ),
    };
  });
}

function expectStablePlanHashError(action: () => unknown, expectedErrorCode: string): void {
  try {
    action();
  } catch (error) {
    expect(error).toBeInstanceOf(PlanHashError);
    expect((error as PlanHashError).errorCode).toBe(expectedErrorCode);
    return;
  }
  throw new Error("expected plan hash validation to reject input");
}

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

describe("RunSpec semantic validation", () => {
  for (const c of planGolden.invalidRunSpec) {
    it(`rejects before projection and hash: ${c.name}`, async () => {
      const materialized = materializeMutationCases(c, planGolden.semanticPlanHash, "plan");
      if (c.name === "missing-base-commit") {
        expect(materialized.map(({ name }) => name)).toEqual(["missing-base-commit:0"]);
      }
      if (c.name === "invalid-base-commit-or-zero-sentinel") {
        const emptyNameCase = structuredClone(c);
        emptyNameCase.mutations[0].name = "";
        expect(() => materializeMutationCases(emptyNameCase, planGolden.semanticPlanHash, "plan"))
          .toThrow("golden mutation name must be non-empty");
      }
      for (const { name, payload } of materialized) {
        expectStablePlanHashError(() => buildSemanticProjection(payload), c.expectedErrorCode);
        expectStablePlanHashError(() => semanticPlanHash(payload), c.expectedErrorCode);
        expect(name).toContain(c.name);
      }

      // 只在已有首个反例节点篡改 module cache：生成常量损坏不得泄露 Ajv 异常或产出 hash。
      if (c.name !== "non-object") {
        return;
      }
      const schemaCases = [
        ["RUN_SPEC_SCHEMA_JSON", "RUN_SPEC_SCHEMA", "semanticPlanHash"],
        ["PLAN_REVISION_SCHEMA_JSON", "PLAN_REVISION_SCHEMA", "planRevisionDigest"],
        [
          "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA_JSON",
          "PLAN_REVISION_DIGEST_MATERIAL_SCHEMA",
          "planRevisionDigest",
        ],
      ] as const;
      for (const [jsonName, legacyName, functionName] of schemaCases) {
        const actual = await vi.importActual<Record<string, unknown>>("./generated/contracts.js");
        const raw =
          typeof actual[jsonName] === "string"
            ? actual[jsonName]
            : JSON.stringify(actual[legacyName]);
        expect(typeof raw).toBe("string");
        const originalJson = raw as string;
        const expectedSha = `sha256:${createHash("sha256").update(originalJson, "utf8").digest("hex")}`;
        const corruptions = [
          `${originalJson.slice(0, -1)},"required":[]}`,
          originalJson.replace('"title":"', '"title":"X'),
        ];
        for (const corrupted of corruptions) {
          expect(corrupted).not.toBe(originalJson);
          vi.resetModules();
          vi.doMock("./generated/contracts.js", () => ({
            ...actual,
            [jsonName]: corrupted,
            [`${jsonName}_SHA256`]: expectedSha,
          }));
          try {
            const dynamicPlan = await import("./plan.js");
            const invoke =
              functionName === "semanticPlanHash"
                ? () => dynamicPlan.semanticPlanHash(structuredClone(planGolden.semanticPlanHash[0].plan))
                : () => dynamicPlan.planRevisionDigest(structuredClone(planGolden.planRevisionDigest[0].revision));
            try {
              invoke();
              throw new Error("corrupt generated schema unexpectedly produced a hash");
            } catch (error) {
              expect(error).toBeInstanceOf(dynamicPlan.PlanHashError);
              expect((error as { errorCode?: unknown }).errorCode).toBe("plan-hash-error");
            }
          } finally {
            vi.doUnmock("./generated/contracts.js");
            vi.resetModules();
          }
        }
      }
    });
  }
});

describe("shared RFC3339 date-time subset", () => {
  for (const c of planGolden.rfc3339DateTime.valid) {
    it(`accepts portable date-time for both contracts: ${c.name}`, () => {
      const plan = structuredClone(planGolden.semanticPlanHash[0].plan);
      plan.createdAt = c.value;
      const revision = structuredClone(planGolden.planRevisionDigest[0].revision);
      revision.createdAt = c.value;

      expect(semanticPlanHash(plan)).toMatch(/^sha256:/);
      expect(planRevisionDigest(revision)).toMatch(/^sha256:/);
    });
  }

  for (const c of planGolden.rfc3339DateTime.invalid) {
    it(`rejects non-portable date-time before projection/hash: ${c.name}`, () => {
      const plan = structuredClone(planGolden.semanticPlanHash[0].plan);
      plan.createdAt = c.value;
      const revision = structuredClone(planGolden.planRevisionDigest[0].revision);
      revision.createdAt = c.value;

      expectStablePlanHashError(() => buildSemanticProjection(plan), "plan-hash-error");
      expectStablePlanHashError(() => semanticPlanHash(plan), "plan-hash-error");
      expectStablePlanHashError(() => planRevisionDigest(revision), "plan-hash-error");
    });
  }
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

describe("PlanRevision digest-material validation", () => {
  for (const c of planGolden.invalidPlanRevision) {
    it(`rejects non-excluded malformed material: ${c.name}`, () => {
      for (const { name, payload } of materializeMutationCases(c, planGolden.planRevisionDigest, "revision")) {
        expectStablePlanHashError(() => planRevisionDigest(payload), c.expectedErrorCode);
        expect(name).toContain(c.name);
      }
    });
  }
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
