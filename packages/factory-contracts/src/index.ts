/**
 * packages/factory-contracts/src/index.ts
 *
 * AI Coding Factory TypeScript 合同包入口。
 * 只 re-export 由 generate.py 生成的类型；
 * Task 3/4 的 canonical/plan/event 算法模块在各自 Task 中单独添加。
 */

export * from "./generated/contracts.js";

// Task 3：跨语言 canonical JSON 与计划身份算法。
export * from "./canonical.js";
export * from "./plan.js";
