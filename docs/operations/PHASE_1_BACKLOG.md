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
- **后移 Phase 1 部分**：完整隔离容器 **Deterministic Verifier**——receipt 的
  `expected`/`actual`/`artifactDigests` 内容本身由 Verifier 在隔离容器中**实际执行测试**
  后写入并交叉核验，调用方无法自报 `observed:true`。属 §9.2 容器化执行实现。
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
- **Phase 0 实际状态（诚实更正）**：`contracts/schemas/` 下**不存在** intent-authorization /
  execution-authorization schema 文件。RunSpec 仅含一个普通字符串字段
  `intentAuthorizationId`（`type: string`，非 `$ref`），用于关联 ID——不导入任何授权合同，
  故无编译期悬空引用，但**也不构成"Phase 0 已冻结授权 schema"**。第三轮审核 item 4 指出
  原主张（"Phase 0 冻结 IntentAuthorization/ExecutionAuthorization schema 字段"）无对应
  schema，属夸大，此处删除该主张。
- **Phase 1 目标（合同 + 运行时一并实现）**：
  1. 按 Master Spec §11 L739 新建 `intent-authorization.v1.schema.json`（issued_at/expires_at/
     revoked_at/revoke_reason/stage_capability_map_version/allowed_capability_set_digest/
     autonomous_execution_budget_ms/修复·重规划·Attempt 上限）与
     `execution-authorization.v1.schema.json`（绑定 intent_authorization_id/plan_revision_id/
     semantic_plan_hash/plan_revision_digest/capability map version+digest/run·step·attempt/
     owner executor/resource fingerprint/capability scope digest/幂等键/fencing token/
     control epoch/accepted control command sequence/输入 SHA/max_uses/消费状态），
     注册 codegen 并加合同测试。
  2. 授权的**消费/撤销有状态机**（`max_uses` 递减、fencing token 递增、control epoch 校验、
     consumed 状态 CAS），属 §11 运行时。

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
- **Phase 0 冻结**：node-capability-map schema 含 requiredCapabilities/sideEffectClass 等。
- **Phase 1 目标**：resource fingerprint schema、幂等键模板、完成事实、授权消费点、
  retry class 的**策略引擎运行时消费**（Policy Engine 按 nodeType 派生 capability 并校验），
  属 §6.3 运行时。

---

## 分级规则（引自 PHASE_0_ACCEPTANCE §1.3）

- 命中 PHASE_0_ACCEPTANCE 某检查项 → Phase 0，必须修。
- 不命中任何检查项 → 记入本文件，Phase 1 实现，**不阻断 Phase 0 收口**。
