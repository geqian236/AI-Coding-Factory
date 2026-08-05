# Phase 1 Backlog（Phase 0 审核中提出、按 Master Spec §11 正确分级到 Phase 1）

> **用途**：记录审核过程中提出的、**不命中 [PHASE_0_ACCEPTANCE.md](PHASE_0_ACCEPTANCE.md) 冻结验收集**
> 的项。这些项真实存在、有价值，但按 Master Spec §11「Phase 0 只冻结纯输入/输出与
> 拒绝规则；并发 CAS、SQLite 事务、writer epoch 竞争留到 Phase 1」应在 Phase 1+ 实现。
>
> 记录在此 = 不丢弃、可追踪、不阻断 Phase 0 收口。

---

## 来自审核的深层项（真实缺口，Phase 1 实现）

### B1. PASS 完全从确定性断言派生（不接受任何 self-report）
- **现状（Phase 0）**：`verify_receipts.py` 已强制 owner 白名单 + owner 与 catalog 冻结值匹配
  + qualification=FINAL + requiredReplays 去重计数 + scenarioContractDigest 与 catalog 绑定
  + receiptId 全局唯一 + expected/actual 非空 + PASS 必带 actions。伪造空 PASS / 缺 owner /
  owner 不匹配 / replay 不足 / 缺 digest 均 fail-closed（6 负例 fixture 覆盖）。
- **Phase 1 目标**：receipt 的 `expected`/`actual`/`artifactDigests` 内容本身由 Verifier
  在隔离容器中**实际执行测试**后写入并交叉核验，调用方完全无法自报 `observed:true`。
  这需要 §9.2 Deterministic Verifier 的容器化执行实现，属 Phase 1。
- **审核出处**：第二/三轮 P0-6 深层点。

### B2. TestReceipt schema / dataclass / loader 单一生成真源
- **现状（Phase 0）**：三者语义一致且互相校验（schema 是 required 权威、dataclass `validate()`
  逐字段查、loader 反序列化）。测试覆盖三者一致性。
- **Phase 1 目标**：dataclass 由 `contracts/codegen/generate.py` 从 schema 单一生成，
  消除手写 dataclass 与 schema 漂移的可能。属 codegen 扩展。

### B3. 真实 ENOSPC 认证
- **现状（Phase 0）**：sqlite spike disk-full 子项诚实标 `BLOCKED_UNCERTIFIED`；
  bench.py 支持 `--probe-dir` 指向 VHD；`scripts/spikes/create_enospc_vhd.ps1` 提供
  admin 挂载 ≤16MiB VHD 的完整脚本。
- **Phase 1 目标**：在有 admin/SeManageVolumePrivilege 的 CI Windows runner 上跑
  `create_enospc_vhd.ps1` + bench `--probe-dir <VHD>`，认证 WAL 在真实 ENOSPC 下
  不暴露半提交批次。届时 sqlite spike 顶层转 PASS。

### B4. Rust 合同运行时断言执行
- **现状（Phase 0）**：`cargo check --target gnu --tests` 编译 + 类型检查通过；
  golden 字节一致性由 Python(320)+TS(70) 对**同一份** golden 锚定。
- **环境硬限制**：本 sandbox 无 MSVC C++ build tools，proc-macro2/serde build-script
  host 链接失败，`cargo test` 无法从零执行（非代码缺陷）。
- **Phase 1 目标**：CI Windows runner（含 MSVC build tools）跑 `cargo test -p factory-contracts`，
  认证 Rust 运行时字节断言，填入 PHASE_0_ACCEPTANCE A-3b 真实计数。

### B5. 并发实现（Master Spec §11 明确移交）
- **Phase 0 冻结**：PreparedBatchV2 / DurableEventV2 的**纯函数物化算法** + schema
  （previousEventDigest 单链、跨批链接续、多 Task manifest 结构、eventDigest 排除自身）。
- **Phase 1 目标**：SQLite 单写者 coordinator、writer_epoch CAS、多 Task 原子 claim 事务、
  `ingest_batches` 状态机（CLAIMED→PREPARING→PREPARED→COMMITTED）、竞争批次
  CAS 落败重建。这些是**有状态运行时**，Phase 0 不实现。

### B6. 授权运行时状态机
- **Phase 0 冻结**：IntentAuthorization / ExecutionAuthorization schema 字段 + 拒绝规则
  + RunSpec.intentAuthorizationId 绑定。
- **Phase 1 目标**：授权的**消费/撤销有状态机**（`max_uses` 递减、fencing token 递增、
  control epoch 校验、consumed 状态 CAS），属 §11 运行时。

### B7. durable-event.v2 §10.1 全字段的**运行时填充**
- **Phase 0 冻结**：schema 含 §10.1 全 32 字段（执行身份、双 span、脱敏证据、
  processIdentity 等）；纯函数物化器填充 identity/链/摘要字段。
- **Phase 1 目标**：Adapter 运行时真实采集 processIdentity（pid/jobObjectId/containerId）、
  sourceTransportSpan 实际字节水位、redactionManifest 实际脱敏证据。Phase 0 是 schema +
  算法，Phase 1 是真实数据源接入。

### B8. node-capability-map 5 字段的**策略引擎消费**
- **Phase 0 冻结**：node-capability-map schema 含 requiredCapabilities/sideEffectClass 等。
- **Phase 1 目标**：resource fingerprint schema、幂等键模板、完成事实、授权消费点、
  retry class 的**策略引擎运行时消费**（Policy Engine 按 nodeType 派生 capability 并校验），
  属 §6.3 运行时。

---

## 分级规则（引自 PHASE_0_ACCEPTANCE §1.3）

- 命中 PHASE_0_ACCEPTANCE 某检查项 → Phase 0，必须修。
- 不命中任何检查项 → 记入本文件，Phase 1 实现，**不阻断 Phase 0 收口**。
