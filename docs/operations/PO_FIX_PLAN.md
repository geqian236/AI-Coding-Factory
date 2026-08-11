# Phase 0 P0 阻断项修复任务分配表

> **来源**：GPT 对 `80d9c64` 的审核裁决 + 7 项 recon gap 图（详见 `.claude/workflows/p0-recon.js`）
>
> **状态**：Wave 1 修复开始前冻结
>
> **生效决策**（已与用户确认）：
> 1. P0-1 事件链模型：全量重建，主刀串行做（不并行、不走 worktree 隔离）
> 2. P0-4 disk-full：顶层判 `BLOCKED_UNCERTIFIED`，manifest 改用合成 PASS fixture

---

## 1. 修复波次与依赖图

```
Wave 0 (工具链预检)         ← 当前在跑
   ↓
Wave 1 (真正并行)            ← 4 个 cluster，文件集不相交
   ├─ 1a: P0-7 存储/日志脱敏      (high confidence, 纯逻辑, 不触 schema)
   ├─ 1b: P0-4 → P0-3 → P0-5      (串行捆绑：共享 bench.py + receipt.json)
   │     · 1b-i  bench.py 真阈值 + PRAGMA 读回 + 顶层 BLOCKED
   │     · 1b-ii emit_manifest 命名冲突修复 + 强制绑定 digest
   │     · 1b-iii spike wrapper stale-PASS + UTF-8/BOM
   └─ 1c: P0-6 receipt 聚合        (high confidence, 独立 cluster)
   ↓
Wave 2 (critical path)      ← 我主刀串行
   └─ P0-1 合同脊柱重建：
         · durable-event.v2 head/previousHead → previousEventDigest 单链
         · 执行身份/双 span/脱敏证据 字段补齐
         · prepared-batch.v2 多 Task manifest 重构
         · 新建 intent-authorization / execution-authorization schema
         · node-capability-map.v1 补 5 字段
         · plan-revision.v1 nodes 补齐 6 字段
         · 三语言 codegen 重生成
         · event-hash/plan-hash golden vectors 重算
         · events.py/event.ts/event.rs 物化器同步
   ↓
Wave 3 (收尾)
   └─ P0-2 门禁 fail-open（2026-08-05 完成）：
         · 删除 plan-validation.yml（与 ci.yml 重复且含占位）
         · ci.yml 6 个 job 全替换为真实 fail-closed 命令
           （plan-consistency 跑 plan_hash/event_hash pytest；
            python 跑 ruff+mypy；desktop 跑 corepack pnpm frozen install + vitest；
            security 跑 _gate_checks.py secret-scan + c-drive-paths；
            新增 windows-probes 跑 Windows runner 上的契约测试）
         · 生成完整 pnpm-lock.yaml（1388 行，含 148 个包）
         · 验证 `corepack pnpm install --frozen-lockfile` 退出 0
         · check.ps1 golden-vectors SKIP-PASS 分支改为 fail closed
         · _gate_checks.py 4 项门禁（chinese-coverage/no-bare-print/secret-scan/
           c-drive-paths）目录缺失时由静默 PASS 改为返回非零
   ↓
Wave 4 (最终收口)
   └─ 重新原子提交，绑定最终干净 SHA
```

---

## 2. 各 P0 修复要点（不变量 + 验收标准）

### 2.1 P0-7 存储/日志脱敏

**不变量**：

- `storage_contract.py`：
  - 新增模块常量 `_REQUIRED_ROOT = Path('D:/codex项目')`（**Windows 大小写不敏感**）
  - 规则 3.5（新增）：解析后绝对路径必须等于 `_REQUIRED_ROOT` 或为其严格父级，**用分段 normcase 比较**，禁止裸 `startswith`（防 `D:\codex项目-evil` 混淆）
  - 规则 7 扩展：`realpath` 后 resolved 也必须通过同一根前缀检查（防 reparse 链跨根重定向）
  - `config.py` 的 `FACTORY_D_ROOT` 覆盖值必须经 `validate_storage_path` 通过后才采纳
- `logging.py`：
  - 新增 `_SENSITIVE_VALUE_PATTERN` 覆盖：`sk-`、`ghp_/gho_/ghs_/ghu_/github_pat_`、`Bearer <token>`、`AKIA[0-9A-Z]{16}`、`xox[baprs]-`、`-----BEGIN [A-Z ]*PRIVATE KEY-----`、`eyJ`(JWT)
  - 改造 `_redact_value`：key 命中 → `[REDACTED]`；否则若 value 是 str 走 `_scan_str`
  - 改造 `_redact_dict`：list/tuple 逐元素递归（dict→_redact_dict，str→_scan_str，嵌套 list 递归）
  - `entry['message']` 在 `format()` 里显式过 `_scan_str`（不能依赖 key 名脱敏）

**验收**：新增负例测试全红后转绿，旧用例不回归。

---

### 2.2 P0-4 SQLite spike：真阈值 + PRAGMA 读回 + 顶层 BLOCKED

**不变量**：

- `bench.py` 重写 batcher：
  - 维护 accumulator（events 计数、bytes 累计、首事件时间戳）
  - 三阈值任一先到即 COMMIT，记录 `triggerReason ∈ {events, bytes, age}`
  - 三种负载各触发一次：高速小事件触发 events；单条大 payload 触发 bytes；低速+sleep 触发 age
- PRAGMA 读回断言：
  - `con.execute("PRAGMA journal_mode").fetchone()[0]` 断言 `=='wal'`
  - `con.execute("PRAGMA synchronous").fetchone()[0]` 断言 `==2` (FULL)
  - 两条断言加入 `assertions` 与 `observable_facts`
- 顶层 status 计算：
  - **disk-full 必选**（§10.4 + catalog STORE-001）
  - `core_pass = all(assertions) AND not any(sub.required and sub.status in (BLOCKED_UNCERTIFIED, FAIL))`
  - 当 disk-full BLOCKED 时顶层判 `BLOCKED_UNCERTIFIED`，**非 PASS**

**receipt 字段**：新增 `perBatchTriggers: [{ordinal, triggerReason, eventCount, bytes}]` 与 `pragmaReadback: {journalMode, synchronous}`。保持 `eventBatchParameters`/`parameterTupleDigest` 键与算法不变，避免触发 codegen 漂移。

**验收**：`tests/contract/test_sqlite_wal_full_spike.py` 覆盖三触发原因 + PRAGMA 实返值 + 顶层 BLOCKED；本地重跑 bench.py 重生成 receipt.json（顶层 BLOCKED）。

---

### 2.3 P0-3 emit_manifest：命名冲突修复 + 强制绑定

**根因**：`receipt.benchmarkProfileDigest`（cc4421bc）是 bench 内部小结字典摘要，**不是**冻结 `contracts/benchmarks/benchmark-profile.v1.json` 文件摘要（03df1bc4）。emitter 写 manifest 用的是后者，两者永不相等。

**不变量**：

- `bench.py`：读取冻结文件并 `_sha256_file` 计算**真正的** `benchmarkProfileDigest` 写入 receipt；原内部小结改名为 `sqliteBenchSummaryDigest`（消除命名冲突）。`env_digest = _sha256_dict(environment)` 不变；`parameterTupleDigest` 算法不变。
- `emit_manifest.py`：
  - 步骤 5 之前插入 `_verify_receipt_digests(receipt)`：
    - 读 `receipt['benchmarkProfileDigest']` 与 `_sha256_file(v1.json)` 比对，不符 `raise ValueError("BENCHMARK_PROFILE_DIGEST_MISMATCH ...")`；缺字段也 fail closed
    - 对 `receipt['environment']` 做 `_sha256_dict` 重算并与 `receipt['env_digest']` 比对，不符 `raise ValueError("ENV_DIGEST_MISMATCH ...")`
    - 对 `receipt['parameterTupleDigest']`（或顶层 `param_digest`）重算比对，去掉软守卫
  - 统一错误码前缀（`BENCHMARK_PROFILE_DIGEST_MISMATCH` / `ENV_DIGEST_MISMATCH` / `PARAM_TUPLE_MISMATCH`）
- `tests/contract/test_compatibility_manifest.py`：
  - 新增 3 个负例（benchmark/env/param digest mismatch）
  - 新增正例 `test_emit_manifest_binds_real_receipt`：但因 P0-4 顶层 BLOCKED，**真实 receipt 不再走 emit_manifest PASS 路径**——改用合成 PASS fixture

**manifest fixture 调整**（重要）：`emit_manifest.py` 仍要求 `spike.status == PASS`，但 P0-4 后真实 SQLite receipt 顶层是 BLOCKED。`test_compatibility_manifest.py` 必须改用合成 PASS receipt fixture（复用 `emit_manifest.py:188-199` 默认值路径），并显式记录「Phase 0 沙盒内无独立小卷 → 真实 SQLite spike 顶层 BLOCKED → manifest 测试走合成 fixture」。

**验收**：新增 4 个负例 fail closed；用合成 fixture 的正例 passed；CLI smoke 验证。

---

### 2.4 P0-5 spike wrapper：stale-PASS + UTF-8/BOM

**不变量**（对 `test-sqlite-wal.ps1`、`test-git-bridge.ps1`、`test-clock-source.ps1`、`test-tauri-e2e.ps1`）：

- 跑子进程**前**强制 `Remove-Item $receiptOut -Force`
- 子进程退出码 `if ($ec -ne 0) { 写 FAIL receipt; exit 1 }`，再校验本轮 receipt 是否存在
- 读 receipt：`Get-Content $path -Raw -Encoding utf8 | ConvertFrom-Json`
- 写 receipt：复用 `test-durable-io.ps1:37-40` 的 `[System.IO.File]::WriteAllText($path, $json, (New-Object System.Text.UTF8Encoding($false)))`
- 加 nonce：本轮生成 nonce 传子进程，读回时断言 `receipt.run_nonce == $nonce`

**验收**：抽出公共 helper；预置旧 PASS receipt 令本轮子进程崩，断言 wrapper exit ≠ 0；用 Pester/pytest 驱动回归测试。

---

### 2.5 P0-6 receipt 聚合 fail-closed

**不变量**：

- `contracts/schemas/test-receipt.v1.schema.json`：
  - `required` 追加 `finalPassOwner`、`requiredReplays`、`scenarioContractDigest`
  - `expected` / `actual` 加 `minProperties: 1`
  - `actions` 加 `minItems: 1`
  - `requiredReplays` minimum 改为 1
  - 新增 `qualification` 字段（codex-reviewer/claude-implementer/...）
- `apps/agent/src/factory_agent/testing/required_test_catalog.py`：增补 `owner_by_test_id` / `replays_by_test_id` helper
- `receipts.py validate()`：
  - `result==PASS` 时 expected 空 → `EMPTY_EXPECTED`；actual 空 → `EMPTY_ACTUAL`
  - `finalPassOwner` 缺失 → `MISSING_OWNER`；`scenarioContractDigest` 缺失 → `MISSING_DIGEST`；`requiredReplays` None → `MISSING_REPLAYS`
- `verify_receipts.py verify()`：
  - `OWNER_MISMATCH`：receipt.owner == catalog owner 映射值
  - `UNAUTHORIZED_OWNER`：owner 不在授权白名单（初版 `{'codex-reviewer'}`）
  - `MISSING_DIGEST` 强制：PASS 且 declared None 即报错
  - `DUPLICATE_RECEIPT_ID`：全局 receiptId 唯一
  - 覆盖门禁按 `catalog.requiredReplays` 计数：对每个 testId 统计 `result==PASS` 且校验通过的去重 receiptId 数，`< requiredReplays` → `INSUFFICIENT_REPLAYS`

**验收**：6 个负例 fixture（空 PASS、缺 owner、owner 不匹配、replay 不足、缺 digest、duplicate）全 fail closed；1 个 `full_coverage_valid.json` 正例 passed。

---

### 2.6 P0-1 合同脊柱（我主刀）

**冻结决策**（先定，避免后续漂移）：

1. **事件链**：`durable-event.v2` 的 `head` / `previousHead` 双字段 **整体删除**，改用 §10.1 的 `previousEventDigest` 单链。`eventDigest = SHA-256(JCS(去除 eventDigest 字段后完整对象))`，genesis 用固定 32 字节全零 predecessor。
2. **类型枚举**：把 `eventType` 的 8 个抽象值改成 §10.1 枚举：`process.started | stream.segment.committed | stream.terminated | model.summary | orchestrator.objective | tool.call | tool.result | state.changed`。
3. **执行身份**：补齐 `schemaVersion`、`durabilityClass`、`runId/stepId/attemptId/source`、`processIdentity{executorId,hostId,runtime,executableDigest,pid,processStartTime,jobObjectId,wslDistro?,containerId?,imageDigest?}`。
4. **双 span**：补齐 `sourceTransportSpan{coordinate,start,endExclusive,mappingPrecision}` + `sanitizedStreamSpan{segmentId,start,endExclusive}`。
5. **脱敏证据**：补齐 `redactions`、`redactionManifestDigest`、`sanitizedProviderFrameDigest`、`mappingPrecision`。
6. **时间/版本**：补齐 `wallTime`、`monotonicTimeNs`、`ingestedAt`、`providerVersion`、`adapterVersion`、`providerEventId`、`streamId`、`preparedBatchId`。
7. **prepared-batch.v2**：从单 Task 改成多 Task manifest，含 `perTaskHeads: [{taskId, expectedCommittedHead, firstOrdinal, lastOrdinal}]`、`orderedIngestIds`、`segmentDigests`、`eventCount`、`payloadBytes`、`schemaVersion/coordinatorVersion`、`state` (CLAIMED→PREPARING→PREPARED→COMMITTED)、`writerEpoch`、完整尾标。
8. **authorization schema**：新建 `intent-authorization.v1.schema.json` + `execution-authorization.v1.schema.json`；承载 §11 L739 详列字段（issued_at/expires_at/revoked_at/stage_capability_map_version/allowed_capability_set_digest/autonomous_execution_budget_ms/...）。
9. **node-capability-map.v1**：每 nodeType 补 5 字段：`resourceFingerprintSchema`、`idempotencyKeyTemplate`、`completionFact`、`authorizationConsumptionPoint`、`retryClass`。
10. **plan-revision.v1**：`nodes` 精确复用 RunSpec `workPlan.nodes` 的 11 字段
    `logicalNodeId/businessPhase/barrierOrdinal/nodeType/required/dependsOn/sideEffectClass/requiredArtifacts/successPredicateId/timeoutMs/retryPolicyId`；
    `barriers` 精确复用 `businessPhase/barrierOrdinal/requiredNodeIds/settleTimeoutMs/passPredicateId`。
    删除有损旧别名 `dependencies/hasSideEffect/gate/barrierId/nodeIds`；运行期 `barrierId` 仍按 Master §7.1 域分离算法派生，不进入不可变计划 wire。

**验收**：
- `generate.py` 重新生成三语言类型
- 重算 `contracts/golden/event-hash.v2.json` + `plan-hash.v1.json` + `prepared-batch.v2.json`
- 同步 `events.py/event.ts/event.rs` 物化器实现
- 跑 `test_codegen_no_drift`（test_schema_catalog.py L387）+ `test_event_hash_vectors` + `test_plan_hash_vectors` 零漂移

---

### 2.7 P0-2 门禁 fail-open（已完成 2026-08-05）

**验收**（全部已达成）：
- `pnpm install --frozen-lockfile` 退出 0
- `pnpm --filter @factory/contracts test`: **69 passed**（event.test.ts 23 + plan.test.ts 46）
- `python -m pytest tests/contract/`: **187 passed**
- `cargo check --target x86_64-pc-windows-gnu -p factory-contracts --tests`: **通过**
- `scripts/check.ps1`: **9/9 PASS**
- `generate.py --check`: **零漂移**

**未涉及/已知约束**：
- Rust test binary 链接（cargo test）在 worktree 当前环境因 `rust-lld` 跨 flavor 识别问题失败，属 dev.ps1 工具链配置错位；cargo check 已通过保证 Rust 代码无编译错误。
- vitest 测的 event.test.ts / plan.test.ts 是单源 golden vectors，跨 Rust/Python/TS 共享同一 contracts/golden/*.json，三语言字节一致性由 Python pytest 端断言驱动（test_event_hash_vectors.py 25/25 + event.test.ts 23/23 + Rust event_vectors.rs 同源）。
- CI 的 windows-probes job 仅跑合同测试（Ubuntu runner 上的 contracts job 也覆盖）；probe 实物认证（spike 脚本）在本地与 §2.2-2.4 任务中已固化在 receipts/。

---

## 3. 本机工具链使用约束（修复期间执行须知）

### 3.1 dev.ps1 双 `--` 调用陷阱（历史 memory 记录过）

**正确协议**：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- cargo check -p factory-contracts`

**错误示例**：`... dev.ps1 -- -- cargo check ...`（两个 `--` → 第一个被 dev.ps1 当分隔符，第二个 `--` 被当成子进程名 → `CommandNotFoundException`）

### 3.2 Rust 工具链位置不一致（关键）

dev.ps1 把 `RUSTUP_HOME` 重定向到 `D:\codex项目\...\dev\rustup-home`，但**真实 Rust 三件套在 ASCII 根 `D:\acf-dev`**（因 CJK 路径链接约束）。直接走 dev.ps1 跑 cargo 会因找不到 `ld.lld.exe` 失败。

**当前已验证可行协议**（不通过 dev.ps1，直接调 cargo；手动注入 `CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER`）：

```bash
export CARGO_HOME=/d/acf-dev/cargo-home
export RUSTUP_HOME=/d/acf-dev/rustup-home
export PATH="/d/acf-dev/cargo-home/bin:$PATH"
export CARGO_TARGET_DIR="D:/codex项目/AI-Coding-Factory-Data/dev/cargo-target"
SYSROOT=$(rustc --print sysroot 2>/dev/null | head -1)
export CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER="$SYSROOT/lib/rustlib/x86_64-pc-windows-gnu/bin/gcc-ld/ld.lld.exe"
cd "D:/codex项目/.codex-worktrees/factory-phase-0"
cargo check -p factory-contracts
```

预检已通过：`cargo metadata --no-deps` 成功列出 5 个 crate，target_directory 自动落在 `D:\codex项目\AI-Coding-Factory-Data\dev\cargo-target`，与 `dev.ps1` 写入路径一致。P0-1 完成后 Rust codegen 必须在本机真验证通过。

### 3.3 GBK 代码页解码

PowerShell stderr/console 输出含 CJK 时被 GBK 破坏。读取后台输出时统一用：

```python
python -c "import sys; data=open(sys.argv[1],'rb').read(); print(data.decode('utf-8',errors='replace'))" <file>
```

---

## 4. 风险与回退

- **P0-1 重算 golden vectors** 若三语言字节不一致，需查 codegen 是否真的确定（JSON 序列化 key 顺序、浮点精度等）。回退：保留旧 vectors + 新 vectors 并存，标注 deprecated。
- **P0-4 disk-full 顶层 BLOCKED** 会让现有 `tests/contract/test_compatibility_manifest.py` 现有正例全红（依赖 sqlite PASS receipt）。回退：所有 `test_compatibility_manifest` 正例改用合成 fixture，并显式标注「Phase 0 沙盒无独立小卷」。
- **P0-2 pnpm 离线安装** 若 corepack 不可联网，需手工生成完整 lockfile；先尝试 ASCII 临时目录跑 `pnpm install`。

---

*本文档由 Claude 在 Phase 0 P0 修复启动前冻结，与 7 份 recon gap 图（`.claude/workflows/p0-recon.js`）配合使用。任何破坏以上不变量的改动必须在本文档更新后再落地。*
