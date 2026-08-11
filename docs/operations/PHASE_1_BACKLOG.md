# Phase 1 Backlog（Phase 0 审核中提出、按各项独立理由分级到 Phase 1）

> **用途**：记录审核过程中提出的、**不命中 [PHASE_0_ACCEPTANCE.md](PHASE_0_ACCEPTANCE.md) 冻结验收集**
> 的项。这些项真实存在、有价值，但属于**有状态运行时**（并发 CAS、SQLite 事务、
> writer epoch、授权消费状态机等），应在 Phase 1+ 实现。
>
> **分级依据（更正）**：Master Spec 中"纯输入/输出与拒绝规则"仅约束 Phase 0 Task 4
> 的事件物化器，**不是 §11 全局规则**。因此下方每一项**各自给出独立的 Phase 1 归属
> 理由**，不以"§11 只冻结纯输入/输出"作笼统依据。
>
> 记录在此 = 不丢弃、可追踪、不阻断 Phase 0 收口。

---

## 来自审核的深层项（真实缺口，Phase 1 实现）

### B1. PASS 从确定性断言派生（第三轮 REVISE item 4 要求拆分）
- **拆分后 Phase 0 部分（✅ 已满足）**：**probe/spike 的顶层 status 由真实可观测断言派生**，
  不接受 self-report。sqlite bench.py 的 status 由 `core_pass = all(assertions passed) and
  no required BLOCKED/FAIL subresult` 计算；各 spike receipt 的 status 来自跨进程硬杀后
  独立进程读回、容器孤儿存活 inspect、pack roundtrip 校验等**实测事实**，非硬编码。
  receipt 聚合层强制 owner 白名单 + catalog 冻结值匹配 + qualification=FINAL + replay 去重
  + digest 绑定 + receiptId 唯一 + expected/actual 非空（6 负例 fixture fail-closed）。
- **总计划归属裁决**：Phase 1 只实现 receipt plumbing 与本阶段的确定性测试（字段绑定、
  catalog/owner/digest/qualification 机械校验），不提前实现隔离执行器。完整隔离容器
  **Deterministic Verifier**——由它实际执行测试并写入/交叉核验
  `expected`/`actual`/`artifactDigests`、使调用方不能自报 `observed:true`——明确保留至
  **Phase 4 Task 5**。
- **审核出处**：第二/三轮 P0-6 深层点。

### B2. TestReceipt 单一真源 — ✅ 已在 Phase 0 消除（第四轮 REVISE item 2）
- **已解决**：消除"生成 TypedDict + 手写 dataclass"双源。做法：
  1. `contracts/codegen/catalog.v1.json` 的 test-receipt 改 `codegen=false` +
     `category=runtime-only`——三语言生成树不再产出 `TestReceipt` TypedDict/interface/struct
     （已重生成 + codegen `--check` 零漂移验证）。**schema 为字段/枚举单一真源**，
     `factory_agent.testing.receipts.TestReceipt` dataclass 为唯一 Python 表示（承载 `validate()`
     行为，codegen 无法生成）。
  2. 新增机械一致性测试 `test_receipt_dataclass_matches_schema_*`：断言 dataclass 字段集 ==
     schema properties 集、schema required ⊆ dataclass 字段、`KNOWN_RUNTIME_IDS` == schema
     `runtimeId` enum。任一漂移即 fail-closed。
  3. 修正真实漂移：dataclass 的 `runtimeId` 原不在 schema（schema `additionalProperties:false`
     会拒绝），现作为可选属性 + enum 加入 schema。
- **不再属 Phase 1**：双源已消除、漂移由测试机械防护，从 backlog 移除。

### B3. 真实 ENOSPC 认证
- **状态：未完成**。不得通过模拟磁盘满、合成 PASS 或结构性门禁把它标为已认证。
- **现状（Phase 0）**：sqlite spike disk-full 子项诚实标 `BLOCKED_UNCERTIFIED`；
  bench.py 支持 `--probe-dir` 指向 VHD；`scripts/spikes/create_enospc_vhd.ps1` 提供
  admin 挂载 ≤16MiB VHD 的完整脚本。
- **Phase 1 目标**：在有 admin/SeManageVolumePrivilege 的 CI Windows runner 上跑
  `create_enospc_vhd.ps1` + bench `--probe-dir <VHD>`，认证 WAL 在真实 ENOSPC 下
  不暴露半提交批次。届时 sqlite spike 顶层转 PASS。

### B4. Rust 合同运行时断言执行 — ✅ 已拉回 Phase 0（第三轮 REVISE item 3）
- **已解决**：`cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked`
  → **14 passed, 0 failed**（event_vectors 7 + plan_vectors 7）。GPT 第三轮独立跑出同一
  结果，本地用 `+stable-...-gnu` 显式工具链复现一致。已记入 PHASE_0_ACCEPTANCE A-3b。
- **前几轮误判更正**：之前记"MSVC 环境硬限制"是错的。真根因是仓库根 `rust-toolchain.toml`
  写 `channel = "stable"`（无 host 后缀），rustup 在本机（default host = msvc）把它解析成
  `stable-x86_64-pc-windows-msvc`，build-script 为 msvc host 编译才找不到 `link.exe`。
  显式 `+stable-x86_64-pc-windows-gnu` 强制 gnu host → build-script 走 ld.lld → 全过。
- **不再属 Phase 1**：此项已在 Phase 0 验收集 A-3b 认证，从 backlog 移除。

### B5. 并发实现（Master Spec §11 明确移交）
- **Phase 0 冻结**：PreparedBatchV2 / DurableEventV2 的**纯函数物化算法** + schema
  （previousEventDigest 单链、跨批链接续、多 Task manifest 结构、eventDigest 排除自身）。
- **Phase 1 目标**：SQLite 单写者 coordinator、writer_epoch CAS、多 Task 原子 claim 事务、
  `ingest_batches` 状态机（CLAIMED→PREPARING→PREPARED→COMMITTED）、竞争批次
  CAS 落败重建。这些是**有状态运行时**，Phase 0 不实现。

### B6. 授权合同 + 运行时状态机（整体 Phase 1）
- **本节点已完成的合同前置**：新增 `intent-authorization.v1.schema.json` 与
  `execution-authorization.v1.schema.json`，以 camelCase 冻结 Intent 的阶段/预算/撤销快照和
  Execution 的 plan/map digest、action policy snapshot、资源指纹、fencing/control/输入绑定及
  消费投影；所有 digest-backed 字段均改为闭合 `{artifactId,schemaId,schemaVersion,digest}` ref，
  `contracts/policies/authorization-snapshot-registry.v1.json` 固定 NFC+RFC8785/JCS 域分离摘要、
  每字段 schemaId/version、真实 validator 来源与闭合 payload。资源指纹使用 node map 内嵌 schema，
  action 快照使用 selected action 投影并绑定 node map digest/node/action。输入绑定仍为闭合
  `baseSha` / `candidateSha` / `contentDigest` 对象（其中 contentDigest 为快照 ref），按节点强制最小
  SHA/content；且 `nodeType → actionCapability` 与 17-node policy map 精确求交。registry 是 policy，
  不增加 schema/catalog 计数；既有两份授权 schema 继续由生成器确定性生成 Python/TypeScript/Rust，
  Python TypedDict 仅将 schema required 字段标为 `Required[T]`，并有 schema、catalog、codegen
  漂移和 fail-closed 合同测试。CompatibilityManifest v1 已有 `nodeCapabilityMapDigest`，唯一真源为
  `emit_manifest.py` raw-file SHA-256，禁止换成 compact/sorted JSON 私有 hash。该工作不把 RunSpec
  的关联字符串误称为早期 `$ref`，也不实现授权运行时。
  `semanticPlanHash` 快照现在直接承接运行时 `build_semantic_projection` 的完整、post-bootstrap RunSpec
  语义字段，不再使用缩小样本（`schemaVersion` 为整数、`constraints` 为数组，assumptions/scope/
  acceptanceCriteria/repository/workPlan/riskProfile 全部冻结）。bootstrap 必须先完成并取得可信 full SHA，
  之后才生成首个 v1 RunSpec；`repository.baseCommit` 键在 `existing|new` 两种用户来源模式下都必须是
  40 位小写完整 SHA。不得伪造 `new/null` RunSpec，也不得把 mode 改写为 existing；RunSpec/PlanRevision
  只在结构化 `nodeType` 字段拒绝 `BOOTSTRAP_REPOSITORY`，并在结构化 `businessPhase` 字段拒绝真实
  `BOOTSTRAPPING_REPOSITORY`（同时兼容拒绝旧误拼 `BOOTSTRAPPING`），不递归限制 logicalNodeId/gate
  等自由 ID 字符串。post-bootstrap 的首个计划从 `PLAN`/`PLANNING` 开始。`planRevisionDigest` 快照逐项承接
  `plan-revision.v1.schema.json`，仅排除自身 digest 与签名，仍包含 DAG、map 版本、nodes、barriers、
  stageMaps 与 createdAt。合同测试从权威投影/schema 派生 exact-set 断言，并对每个纳入字段的变更
  重算摘要，防止静态 registry 与后续运行时实现分叉。
  本轮收紧为机械闭集：`semanticPlanHash`/`planRevisionDigest` 共同采用 `^sha256:[0-9a-f]{64}$`，摘要全零
  仍格式合法；Git 全零 sentinel 只在 baseline/RunSpec 路径拒绝。`businessPhase` 从 Master Spec §7.1 去除
  `CREATED`/`PREFLIGHT`/`BOOTSTRAPPING_REPOSITORY` 后精确保留 15 个 post-bootstrap 值。**v1 设计决定**：
  所有 RunSpec/PlanRevision（含 child replan）均有非空 nodes/barriers，且首 node 为 `PLAN`/`PLANNING`、首
  barrier 为 `PLANNING`；自由 logicalNodeId/gate 文本不递归禁词。registry baseline metadata 固定为
  `required=[baseBranch,bootstrapState]`、`optional=[baseSha,bootstrapReceiptId]`：`EXISTS` 必须 non-zero full
  `baseSha`（receipt 可选），`BOOTSTRAP_REQUIRED` 禁止 sha/receipt。新仓库 baseBranch 默认策略和
  `bootstrapPending` 显式策略未冻结，不宣称当前合同已唯一规定。生成器写三语言文件时采用同目录
  stage/rollback 的进程内补偿批次，稳定脱敏错误码优先级为 rollback > cleanup > write；不宣称断电或并发事务。
- **后续 Task 4/5**：pre-plan bootstrap 目前仅冻结 Task intake、Intent `repo.bootstrap` 包络、目录合同与
  action receipt 边界，不伪造 PlanRevision-bound ExecutionAuthorization；bootstrap 专用 permit、消费/事务
  尚未实现且必须 fail closed。授权签发、从 `COMMITTED` 不可变 artifact 解析 ref、核验 artifact ID、
  schema/version、validator 与重算 digest、Execution/Manifest 实际 node map 值比对、撤销、`maxUses`
  消费、fencing token/control epoch 校验、CAS、动态 scope/lease/时间比较及真实执行消费仍是有状态
  运行时；本节点没有伪测数据库 COMMITTED 逻辑，也不是这些流程已完成的声明。

### B7. durable-event.v2 §10.1 全字段的**运行时填充**（第三轮 REVISE item 4 要求拆分）
- **Phase 0 冻结（✅）**：schema 含 §10.1 全 32 字段（执行身份、双 span、脱敏证据、
  processIdentity 等）；纯函数物化器填充 identity/链/摘要字段，golden vector 字节锚定。
- **拆分后 Phase 1 部分（durable writer）**：SQLite 单写者把物化后的 DurableEventV2 落库、
  推进 head、group-commit 事务——事件耐久写入路径。
- **拆分后 Phase 3 部分（adapter / process identity 采集）**：Adapter 运行时真实采集
  processIdentity（pid/jobObjectId/containerId/imageDigest）、sourceTransportSpan 实际字节
  水位、sanitizedStreamSpan 脱敏字节、redactionManifest 实际脱敏证据。属真实 Provider
  数据源接入，依赖 Phase 3 harness/runner。

### B8. node-capability-map 5 字段的**策略引擎消费**
- **本节点已完成的合同前置**：17 种 nodeType 均冻结既有 `requiredCapabilities`/
  `sideEffectClass` 与五个新增字段：本文件本地 `$defs` 可解析的闭合 resource fingerprint
  schema、按 capability 独立的 `factory-action-v1` 幂等键输入、外部完成事实、授权消费点及
  静态 retry envelope。合同测试逐 node/action 精确冻结 required/optional capability、side effect、
  授权消费点、retry class、key 输入、predicate 与 evidence；并自动穷举每个 selection 的
  true-缺-overlay/false-携-overlay，以及资源指纹根对象和嵌套对象的未知字段注入。
  DEPLOY/ACCEPT/ROLLBACK 每个 action key 均绑定 `environment + resourceFingerprintDigest`；
  `target.guard.clear` 只属于 RECONCILE_TARGET optional，PUBLISH_PR 四个 action 保持 action-local，
  同步改 key/fact 或换另一个合法 retry class 也会被精确 mutation 门禁拒绝。
- **后续 Task 4/5**：Policy Engine 按 nodeType/action 派生 capability、验证资源指纹/完成事实、
  记录 started transaction、处理 `UNKNOWN_STATE/RECONCILING` 并执行 CAS/重试包络，仍属于
  运行时消费；本节点不提前实现该引擎，也不把静态 retry class 误作 §17 的动态错误类别。

---

## 分级规则（引自 PHASE_0_ACCEPTANCE §1.3）

- 命中 PHASE_0_ACCEPTANCE 某检查项 → Phase 0，必须修。
- 不命中任何检查项 → 记入本文件，Phase 1 实现，**不阻断 Phase 0 收口**。
