# AI Coding Factory — Contracts v1 Protocol Reference

> **状态**：Phase 0 基线已冻结，全部 spike 认证完毕（HEAD: ef9a33f）。本文档描述 `contracts/` 目录下所有语言中立合同的结构、生成规则和使用约定。
>
> **版本**：v1（2026-08-05）

---

## 1. 概述

`contracts/` 是本 monorepo 的唯一协议源。Python、TypeScript 和 Rust 的类型定义均由 `contracts/codegen/generate.py` 从 JSON Schema 确定性生成，不允许手动维护副本。

所有后续 Phase（Phase 1 Control Plane、Phase 2 Desktop/IPC 等）只能：

- `$ref` 或 `import` 本目录中冻结的类型，不得另建不兼容副本。
- 在 `catalog.v1.json` 中添加新 schema，由 `generate.py` 统一生成三语言适配。

---

## 2. 目录结构

```
contracts/
  benchmarks/
    benchmark-profile.v1.json   — 性能负载生成器参数（固定 prngSeed=20260803）
  codegen/
    catalog.v1.json             — codegen 目录：声明哪些 schema 生成代码
    generate.py                 — 确定性三语言代码生成器（支持 --check 漂移检测）
  golden/                       — golden vectors（Task 3/4 填充）
  policies/
    node-capability-map.v1.json — 17 种 nodeType 的 capability 与副作用分类
    node-pause-policy.v1.json   — 每种 nodeType 的暂停安全点策略
    stage-capability-map.v1.json — 6 个 target_stage 的渐进式 capability 集合
  schemas/
    *.schema.json               — 13 个 JSON Schema 定义（见第 3 节）
  testing/
    required-test-catalog.v1.json — Phase 0 冻结的 47 个必须通过的测试 ID
```

---

## 3. 13 个 JSON Schema

| 文件 | 用途 | 生成代码 |
|------|------|---------|
| `credential-ref.v1.schema.json` | 凭据引用（不含明文值） | 是 |
| `run-spec.v1.schema.json` | 单次 Run 的完整规格 | 是 |
| `plan-revision.v1.schema.json` | 计划修订版本（DAG + barriers） | 是 |
| `control-command.v1.schema.json` | 软暂停/立即停止/取消/恢复 | 是 |
| `task-intake.v1.schema.json` | 任务接入请求与接受回执 | 是 |
| `claude-self-review.v1.schema.json` | Claude 自审回执 | 是 |
| `prepared-event.v2.schema.json` | Adapter 预备事件 | 是 |
| `prepared-batch.v2.schema.json` | 事件批次（含 batchOrdinal） | 是 |
| `durable-event.v2.schema.json` | 物化后的耐久事件（含 head 链） | 是 |
| `ipc-envelope.v1.schema.json` | Named Pipe IPC 信封（含 nonce） | 是 |
| `runner-protocol.v1.schema.json` | Runner 生命周期协议消息 | 是 |
| `test-receipt.v1.schema.json` | 机器可读测试回执 | 是 |
| `compatibility-manifest.v1.schema.json` | 兼容性 Manifest（`synchronous` 固定为 `FULL`） | 否（runtime-only） |

### Schema 通用规则

- 所有顶层对象设置 `"additionalProperties": false`（fail-closed）。
- 数值范围满足 I-JSON/JCS（无 NaN、无 Infinity、无超范围整数）。
- `enum` 字段使用固定字符串值，不允许自由文本。

---

## 4. 代码生成规则

### 4.1 运行生成器

```powershell
# 首次生成（写入三语言文件）
python contracts/codegen/generate.py

# 漂移检测（不修改文件，非零退出表示漂移）
python contracts/codegen/generate.py --check
```

### 4.2 生成目标

| 语言 | 输出文件 |
|------|---------|
| TypeScript | `packages/factory-contracts/src/generated/contracts.ts` |
| Python | `apps/agent/src/factory_agent/contracts/generated/models.py` |
| Rust | `crates/factory-contracts/src/generated/contracts.rs` |

### 4.3 catalog.v1.json 字段说明

- `category: "generated"` — 需要生成代码的 schema
- `category: "runtime-only"` — Adapter 动态验证用，不生成静态类型
- `codegen: true` — 实际触发代码生成的开关

---

## 5. 策略目录

### 5.1 node-capability-map.v1.json

精确包含 17 种 nodeType（来自 Master Spec §6.3）：

```
PLAN, DESIGN_REVIEW, BOOTSTRAP_REPOSITORY, IMPLEMENT, VERIFY,
CODE_REVIEW, ATTEST_REVIEW, PUBLISH_PR, MERGE, BUILD_ARTIFACT,
DEPLOY_STAGING, ACCEPT_STAGING, DEPLOY_PRODUCTION,
ACCEPT_PRODUCTION, ROLLBACK, RECONCILE_TARGET, RESTORE_DRILL
```

每种 nodeType 必须声明：
- `requiredCapabilities`: 执行所需能力列表
- `sideEffectClass`: 副作用分类（`read-only` / `local-write` / `external-write` / `destructive`）

`RESTORE_DRILL` 固定需要 `db.restore`、`restore.validation.instance`、`db.check`，且 resource fingerprint 必须证明目标不是生产实例。

### 5.2 stage-capability-map.v1.json

6 个 target_stage 的 capabilities 数组满足渐进式超集关系：

```
DESIGN_REVIEW ⊂ CODE_REVIEW ⊂ PUBLISH_PR ⊂ MERGE ⊂ ACCEPT_STAGING ⊂ ACCEPT_PRODUCTION
```

每个数组已排序且无重复。

### 5.3 node-pause-policy.v1.json

覆盖所有 17 种 nodeType 的暂停策略，每项包含：
- `safePoint`: 安全暂停点描述
- `graceMs`: 宽限时间（毫秒）
- `criticalSections`: 关键区列表（暂停期间不得中断）
- `unknownDisposition`: UNKNOWN 处置动作

顶层 `unknownNodeTypeDisposition` 描述遇到未知 nodeType 时的处置规则。

---

## 6. TaskIntakeRequest vs TaskIntakeAccepted 边界

| 字段类别 | TaskIntakeRequest | TaskIntakeAccepted |
|---------|:-----------------:|:-----------------:|
| 自然语言需求 | 是 | 否 |
| target_stage | 是 | 否 |
| repository selection | 是 | 否 |
| capability digest | **否** | 是 |
| intentAuthorizationId | **否** | 是 |
| normalizedRepositoryBinding | **否** | 是 |
| workspaceLease/checkpointNamespace | **否** | **否** |

> 客户端不得自报 capability digest、规范化 binding 或 IntentAuthorization 身份。

---

## 7. ClaudeSelfReview 限制

`claude-self-review.v1` 的 actor 是候选源码作者，因此：

- 只能作为 `IMPLEMENT` 的作者质量 Artifact。
- **不能**满足 `CODE_REVIEW` / `ATTEST_REVIEW`。
- **不能**关闭 Codex finding 或授权 `PUBLISH_PR` / `MERGE`。
- candidate SHA 漂移后 receipt 必须失效（通过 `reviewedCandidateSha` 绑定）。

---

## 8. PreparedBatchV2 / DurableEventV2 身份链

- 物化器按 `batchOrdinal` 生成稳定 identity，逐 Task 连接 predecessor。
- `head` 字段链式验证批次完整性。
- 并发 CAS 和数据库事务留到 Phase 1，Phase 0 只冻结纯输入/输出与拒绝规则。
- Adapter 输入含 `ingestEventId`，但不得预填 `taskSeq/eventId/eventDigest/head`。

---

## 9. CompatibilityManifest 参数路径

单一路径：

```
eventBatchParameters.{maxBatchEvents, maxBatchBytes, maxBatchAgeMs,
                       synchronous, parameterTupleDigest, sqliteSpikeReceiptDigest}
```

- `synchronous` 固定为 `"FULL"`。
- SQLite spike receipt 必须绑定参数 tuple、manifest schema digest、`benchmarkProfileDigest` 和环境 digest。
- emitter、Phase 1 consumer 与 Phase 6 validator 都重算 tuple digest 并核对 receipt 输入，禁止只比较 receipt 文件名。

---

## 10. Required Test Catalog（47 个 ID）

`contracts/testing/required-test-catalog.v1.json` 包含精确 47 个必须通过的测试 ID，覆盖：

`STAGE-001..006, AUTH-001..002, PLAN-001, PLAN-HASH-001, BOOT-001, MCP-001, STATE-001, LEASE-001, PROC-001, CTRL-001..002, EVENT-HASH-001, STREAM-001..002, STREAM-DUR-001, STREAM-BP-001, STREAM-PERF-BURST/SUSTAINED/SPARSE, REVIEW-001, GIT-001, SIDEFX-001, DEPLOY-001..003, GUARD-001, DEPLOY-FENCE-001, STORE-001, BUDGET-001, RETRY-001, NOTIFY-001, PATH-001..002, CLI-PROFILE-001, RUNTIME-001, WSL-IO-001, NGINX-001, BACKUP-001, DEPLOY-RAM-001, SCHED-PERF-001, COMPAT-001`

每个条目包含：`implementationContributors`、`finalPassOwner`、`requiredReplays`、`scenarioContractDigest`。

`STREAM-DUR-001` 必须包含稳定 `crashPointIds`：`record_prefix_mid_batch`、`record_torn_next`。

---

## 11. 门禁检查（Phase 0 总门禁）

提交前必须通过 `scripts/check.ps1` 的 9 项检查：

1. Codegen drift 检测
2. 13 个 JSON Schema 有效性
3. Golden vectors（plan_hash + event_hash）
4. 中文注释覆盖
5. 无裸 print/console.log
6. Secret scan（0 命中）
7. 无可控 C 盘路径
8. Catalog 精确 47 个 ID
9. docs/ 无占位符

---

*本文档由 Task 8 生成，属于 Phase 0 基线（收口于 2026-08-05，HEAD: ef9a33f）。如需修改合同，请通过标准 PR 流程并更新相关 golden vector。*
