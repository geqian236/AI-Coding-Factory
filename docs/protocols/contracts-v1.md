# AI Coding Factory — Contracts v1 Protocol Reference

> **状态**：Phase 0 基线已冻结；本次补入 Phase 1 授权合同前置（schema、catalog、策略映射与确定性测试），不实现运行时服务。SQLite 真实 ENOSPC 仍是 Phase 1 待认证实证项，故不得声称全部 spike 已认证完毕。本文档描述 `contracts/` 目录下所有语言中立合同的结构、生成规则和使用约定。
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
    *.schema.json               — 15 个 JSON Schema 定义（见第 3 节）
  testing/
    required-test-catalog.v1.json — Phase 0 冻结的 47 个必须通过的测试 ID
```

---

## 3. 15 个 JSON Schema

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
| `test-receipt.v1.schema.json` | 机器可读测试回执 | 否（runtime-only） |
| `compatibility-manifest.v1.schema.json` | 兼容性 Manifest（`synchronous` 固定为 `FULL`） | 否（runtime-only） |
| `intent-authorization.v1.schema.json` | Intent 授权的不可变预算、阶段、绑定与撤销快照 | 是 |
| `execution-authorization.v1.schema.json` | 单 capability action 的派生执行授权、fencing 与消费投影 | 是 |

### Schema 通用规则

- 所有顶层对象设置 `"additionalProperties": false`（fail-closed）。
- 数值范围满足 I-JSON/JCS（无 NaN、无 Infinity、无超范围整数）。
- `enum` 字段使用固定字符串值，不允许自由文本。
- `contracts/policies/authorization-snapshot-registry.v1.json` 是授权快照的 policy 真源，不是新增 schema/catalog 条目。所有 digest-backed 授权字段均为闭合 `{artifactId,schemaId,schemaVersion,digest}` 引用；`artifactId` 只定位对象，摘要固定为 `sha256(JCS/NFC([schemaId,schemaVersion,payload]))`，其中字符串先做 Unicode NFC，再按 RFC 8785/JCS 编码。
- 每个 registry binding 固定 schemaId/schemaVersion、真实 validator 来源、闭合 payload schema 以及 required/optional 键。资源指纹 payload 按 `node-capability-map.v1.json` 对应 `nodeType` 的内嵌 `resourceFingerprintSchema` 校验；action policy payload 必须是 selected action 的确定性投影，并绑定 `nodeCapabilityMapDigest + nodeType + actionCapability`。
- `ExecutionAuthorization.semanticPlanHash` 的 payload 不是简化摘要样本，而是 `factory_agent.policy.plan_hash.build_semantic_projection` 对完整 RunSpec 的精确投影：`schemaVersion` 保持整数，`constraints` 保持字符串数组，并同时冻结 assumptions、scope、acceptanceCriteria、repository（mode/root/baseBranch/baseCommit）、workPlan（dagVersion/nodes/barriers）与 riskProfile。`repository.baseCommit` 键始终存在且类型为 `string|null`：`existing` 必须是 40 位小写完整 SHA，bootstrap 前的 `new` 必须为 `null`。`BOOTSTRAP_REPOSITORY` 成功后不得原位回填原 RunSpec；必须创建 `repository.mode=existing` 且带 bootstrap SHA 的子 RunSpec/PlanRevision，新的 `semanticPlanHash` 与闭合快照 ref 都必须变化。顶层及嵌套对象均 fail-closed；合同测试从运行时投影直接取得字段集合并逐字段验证删改不能复用旧摘要。
- `ExecutionAuthorization.planRevisionDigest` 的 payload 是完整不可变 PlanRevision 的 digest material：字段/类型和 required 集合逐项跟随 `plan-revision.v1.schema.json`，仅排除 `planRevisionDigest` 自身值与 signature。`parentRevisionId` 保留权威 schema 的可选性，其余必填字段（含 DAG、映射、nodes、barriers、stageMaps、createdAt）缺失即拒绝；合同测试从 schema 和运行时排除集机械比较，防止新增或遗漏字段静默漂移。
- Task 4 消费边界：只能解析状态为 `COMMITTED` 且不可变的 artifact；`artifactId` 解析、schema/version、validator 来源和重算 digest 必须全部匹配，否则拒绝。此处只冻结合同，尚未实现数据库状态或运行时消费逻辑。
- `ExecutionAuthorization` 使用正确拼写 `stageCapabilityMapDigest`；历史 v1 的 `stageCapeabilityMapDigest` 不在本 schema 双拼写放行范围，后续版本必须经显式 adapter/migration 映射。
- `ExecutionAuthorization.consumptionState` 只表示 `AVAILABLE`（`maxUses >= 1`）或 `CONSUMED`（`maxUses == 0`）；撤销和过期分别由 `revokedAt` 与 `expiresAt` 表达，不能混为消费状态。
- 两种 Authorization 的 `revokedAt` 与 `revokeReason` 必须同时为 `null`，或同时为合法 RFC3339 时间和非空原因；未知状态、格式错误时间和半撤销对象一律拒绝。
- `ExecutionAuthorization.inputBindings` 是闭合对象，只允许完整 `baseSha`、`candidateSha`、`contentDigest` 三个具名键；数组和同类重复绑定不属于 v1 wire format。`contentDigest` 本身是 input-content 快照引用而非裸 hash。`IMPLEMENT` 必须有 `baseSha`，`VERIFY`、`CODE_REVIEW`、`PUBLISH_PR`、`MERGE` 必须有 `candidateSha`，staging/production deploy 与 acceptance 还必须同时有 `candidateSha + contentDigest`。
- `nodeType → actionCapability` 是 17 个分支的精确闭集，不采用“全局 action 枚举通过即放行”的弱校验；每个 action 仍须由后续 Policy Engine 做动态 scope、lease、时间和 snapshot 重算。

`TestReceipt` 在 catalog 中为 `category: "runtime-only"`、`codegen: false`：它由运行时 `TestReceipt` 表示和 schema 一致性测试消费，三语言生成树不生成第四份静态类型。`CompatibilityManifest` 同样保持 runtime-only。

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

Python 生成目标采用 `TypedDict(total=False)`：仅 JSON Schema `required` 中的字段生成
`Required[T]`，未列入 `required` 的字段保持可省略；schema 已声明 `null` 时才额外生成
`Optional[T]`。生成文件不启用 postponed annotations，以便 Python 运行时的
`__required_keys__` / `__optional_keys__` 继续成为可机械核验的合同证据。

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

每种 nodeType 必须声明并冻结下列单一真源字段：

- `requiredCapabilities` / `optionalCapabilities`：闭集 capability 列表；运行时不得接收模型自由填写的 capability。
- `sideEffectClass`：实际使用的类别为 `read-only`、`local-write`、`isolated-exec`、`external-write-limited`、`external-write`、`remote-write`、`remote-observe`、`isolated-restore`。
- `resourceFingerprintSchema`：同一 node map 内本地 `$defs` 可解析的 JSON Schema 子对象；顶层及嵌套对象 fail-closed，稳定物理身份与可变 SHA/revision/release/digest 分离。
- `idempotencyKeyTemplate`：版本化的 `factory-action-v1` / `sha256-jcs-nfc` `actions.byCapability` 闭集。每项为 `H=sha256(JCS/NFC(["factory-action-v1", …]))` 的派发前已知输入，绝不含 attempt、token、epoch、TTL 或时间。
- `completionFact`：版本化 `actions.byCapability` 的外部可观察 `predicateId` 与 `requiredEvidence`，不是模型自报成功；同一节点的不同外部 action 也必须有 action-local 事实，不能由一次泛化 PR 事实互相满足。
- `authorizationConsumptionPoint`：仅 `before-capability-dispatch` 或 `with-action-started-transaction`；写能力一律采用后者，未知送达进入运行时 `UNKNOWN_STATE/RECONCILING`。
- `retryClass`：静态重试包络仅为 `bounded-no-external-side-effect`、`local-fact-before-retry`、`external-fact-before-retry`、`one-shot-cas-reconcile-only`，与 Master Spec §17 的动态错误类别分离。

合同测试逐 node/action 精确冻结 required/optional capability、side effect、授权消费点、retry class、`jcsInputFields`、`predicateId` 与 `requiredEvidence`。所有 `*Selected` 条件都必须同时具备 true-需-overlay 与 false-禁-overlay 分支；测试会自动枚举两支，并在资源指纹的根对象和每个嵌套对象注入未知字段。`target.guard.clear` 只允许作为 `RECONCILE_TARGET` 的 optional capability；`PUBLISH_PR` 的四个 action 事实保持 action-local。

`DEPLOY_STAGING`、`DEPLOY_PRODUCTION`、`ACCEPT_STAGING`、`ACCEPT_PRODUCTION` 与 `ROLLBACK` 的每个 action key 都显式包含 `environment + resourceFingerprintDigest`，因此相同业务输入不能跨环境或物理目标复用 identity。若 action 无法满足精确 key/fact 合同，后续 Policy Engine 必须拒绝签发，不得退回通用自由文本。

v1 `CompatibilityManifest` 已要求 `nodeCapabilityMapDigest`，唯一算法真源是 `emit_manifest.py` 的 raw-file SHA-256；语义或纯格式字节变化都会改变摘要。ExecutionAuthorization 与 Manifest 实际值的运行时核对仍属于 Task 4，本合同节点不伪装已经实现消费逻辑。

`RESTORE_DRILL` 固定需要 `db.restore`、`restore.validation.instance`、`db.check`，且 resource fingerprint 必须证明目标不是生产实例。

### 5.2 stage-capability-map.v1.json

6 个 target_stage 的 capabilities 数组满足渐进式超集关系：

```
DESIGN_APPROVED ⊂ CODEX_APPROVED ⊂ PR_READY ⊂ MERGED ⊂ STAGING_ACCEPTED ⊂ PRODUCTION_ACCEPTED
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
- v1 `CompatibilityManifest` 已经要求 `nodeCapabilityMapDigest`。其唯一算法真源是 `tools/compat-probes/emit_manifest.py` 的 raw-file SHA-256：语义内容或纯格式字节任一变化都会改变摘要，不能用 compact/sorted JSON 私有 hash 替代。ExecutionAuthorization 与 Manifest 的实际 node map 值比对仍由 Task 4 在消费时完成。

---

## 10. Required Test Catalog（47 个 ID）

`contracts/testing/required-test-catalog.v1.json` 包含精确 47 个必须通过的测试 ID，覆盖：

`STAGE-001..006, AUTH-001..002, PLAN-001, PLAN-HASH-001, BOOT-001, MCP-001, STATE-001, LEASE-001, PROC-001, CTRL-001..002, EVENT-HASH-001, STREAM-001..002, STREAM-DUR-001, STREAM-BP-001, STREAM-PERF-BURST/SUSTAINED/SPARSE, REVIEW-001, GIT-001, SIDEFX-001, DEPLOY-001..003, GUARD-001, DEPLOY-FENCE-001, STORE-001, BUDGET-001, RETRY-001, NOTIFY-001, PATH-001..002, CLI-PROFILE-001, RUNTIME-001, WSL-IO-001, NGINX-001, BACKUP-001, DEPLOY-RAM-001, SCHED-PERF-001, COMPAT-001`

每个条目包含：`implementationContributors`、`finalPassOwner`、`requiredReplays`、`scenarioContractDigest`。

`STREAM-DUR-001` 必须包含稳定 `crashPointIds`：`record_prefix_mid_batch`、`record_torn_next`。

---

## 11. 门禁检查

提交前必须通过 `scripts/check.ps1` 的 15 项检查：

1. Codegen drift 检测
2. 15 个 JSON Schema 有效性
3. Golden vectors（plan_hash + event_hash）
4. 中文注释覆盖
5. 无裸 print/console.log
6. Secret scan（0 命中）
7. 无可控 C 盘路径
8. Catalog 精确 47 个 ID
9. docs/ 无占位符
10. Ruff lint
11. mypy strict（`apps/agent/src`）
12. TypeScript `tsc --noEmit`
13. TypeScript Vitest
14. Rust contracts test
15. bootstrap-dev `-VerifyOnly`

真实 ENOSPC 认证不因上述结构性门禁通过而变为 PASS：在具备管理员卷管理权限的 Windows CI runner 上完成真实 VHD/ENOSPC 实验之前，该 Phase 1 实证项保持未完成和 `BLOCKED_UNCERTIFIED`。

---

*本文档保留 Phase 0 冻结合同，并记录本节点新增的 Phase 1 授权合同前置。授权签发、撤销、消费、动态 scope/lease/时间比较及策略引擎执行仍属于后续 Task 4/5；如需修改合同，请通过标准评审流程并更新相关测试与兼容性绑定。*
