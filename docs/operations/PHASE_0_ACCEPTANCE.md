# Phase 0 冻结验收集（Acceptance Frozen Set）

> **目的**：把"Phase 0 完成"定义成一个**有限、客观、可机器判定**的集合，
> 让验收有明确终点，避免开放式审核循环无限追加新 P0。
>
> **acceptanceSetVersion**：`phase0-acceptance-v1`（本文件的稳定版本标识；内容 digest 由
> `scripts/phase0-acceptance.ps1` 在回执生成时实算并写入 `acceptanceSetDigest`）
> **候选 SHA**：**不在此硬编码**——由 `phase0-acceptance.ps1` 运行时抓取当前 40 位
> `testedCandidateSha` 写入总回执，确保验收证据绑定的是实际被测 commit（GPT item 5）。
> **分支**：`codex/phase-0-foundation`；**冻结日期**：2026-08-05

---

## 1. 冻结原则（双方约定）

1. **本文件列出的检查项 = Phase 0 完成的完整定义**。全部通过即 Phase 0 达成，
   可进入 Phase 1；不得以"spec 里还有更多字段/功能"为由追加新的 Phase 0 阻断项。
2. **Phase 0 的定位是"合同地基 + 平台兼容性认证"，不是完整平台实现**。
   "纯输入/输出与拒绝规则"这一措辞在 Master Spec 中**仅约束 Phase 0 Task 4 的事件
   物化器**（PreparedBatchV2 → DurableEventV2 纯函数），不是 §11 的全局规则。
   本文件对每个移交 Phase 1 的项**各自给出独立理由**（见 §4 / PHASE_1_BACKLOG），
   不再以"§11 只冻结纯输入/输出"作为笼统依据。有状态运行时（并发 CAS、SQLite
   事务、writer epoch、授权消费状态机）按各自定位属于 Phase 1+。
3. **新发现的缺陷分级**：
   - 若命中本文件某检查项 → 属 Phase 0，必须修。
   - 若不命中任何检查项 → 记入 `docs/operations/PHASE_1_BACKLOG.md`，不阻断 Phase 0。
4. **BLOCKED allowlist（冻结，精确）**：验收集分两类检查，判定规则互斥、无歧义：
   - **核心合同检查**（§2.1 A-1/A-2/A-3/A-3b、§2.2 全部 15 门禁、§2.3、§2.4）：
     **必须 PASS**，任一 FAIL 或 BLOCKED 即 Phase 0 未达成。
   - **环境兼容 spike**（§3）：**只有下方冻结 allowlist 中列明的 node ID** 允许
     `BLOCKED_UNCERTIFIED`，且必须附解封脚本；未列明的 spike 必须 PASS。
   - **冻结 BLOCKED allowlist（仅此两项，其余全部必须 PASS）**：
     - `spike:sqlite_wal_full/disk_full_enospc` —— 真实 ENOSPC 需 admin 挂载 VHD，
       解封脚本 `scripts/spikes/create_enospc_vhd.ps1`。该子项 BLOCKED 使 sqlite spike
       **顶层判 BLOCKED_UNCERTIFIED**（按 §3 优先级 `FAIL > BLOCKED > PASS`）。
     - `spike:tauri_e2e/webview_runtime` —— WebView2 E2E 需 Tauri 运行时 + 显示环境，
       本 sandbox 无头。保留 Phase 0 spike ownership，正式记为 `BLOCKED_UNCERTIFIED`
       （非"待 Phase 2"）。该 subcheckId 与 tauri wrapper 发出的 receipt `subcheckId`
       及 §3 allowlist key 三处统一。
   - 顶层状态优先级冻结为 `FAIL > BLOCKED_UNCERTIFIED > PASS`：任一必选子项 FAIL
     顶层即 FAIL；无 FAIL 但有 allowlist 内 BLOCKED 顶层即 BLOCKED_UNCERTIFIED。
   - **不伪造 PASS**：allowlist 内项在解封环境（admin CI Windows runner）跑通后转 PASS。

---

## 2. 机器可判定验收集

> **判定规则（引 §1 原则 4 的冻结 allowlist）**：核心合同项（A-1/A-2/A-3/A-3b、
> §2.2 全部 15 门禁、§2.3 可复现构建、§2.4 CI fail-closed）**必须 PASS**；
> 只有 §1.4 allowlist 明列的环境兼容 spike 子项允许 `BLOCKED_UNCERTIFIED`，
> 其余任何项 BLOCKED 或 FAIL 即 Phase 0 不达成。

### 2.1 测试套件（确定性，可复现）

| 编号 | 检查 | 命令 | 冻结基线 |
|------|------|------|---------|
| A-1 | Python 三目录测试 | `python -m pytest tests/contract tests/agent tests/security -q` | 330 passed, 1 skipped（contract 199 + agent 78 + security 53）；唯一冻结 skip node = `tests/agent/unit/test_config.py::TestValidateDRoot::test_d_root_passes_on_d_drive`（D 盘固定卷检测，环境受限非失败；runner 精确校验此 node，出现其它 skip 即 FAIL）。contract 199 含第六轮 item 3 新增 `test_receipt_schema_conformance.py`（3 项：反例锁定 + stdlib 回退校验器与真 jsonschema 逐例等价 + 基准），证明运行时校验语义与 schema 单一真源一致 |
| A-2 | TypeScript 合同向量 | `pnpm --filter @factory/contracts test` | 70 passed |
| A-3 | Rust 合同编译 + 类型检查 | `cargo +stable-x86_64-pc-windows-gnu check -p factory-contracts --tests` | exit 0（gate #14） |
| A-3b | Rust 合同**运行时断言** | `cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked` | **14 passed, 0 failed**（event_vectors 7 + plan_vectors 7） |

> 1 skipped = 环境受限项（记录在 receipt 中，非失败）。
>
> **A-3b 调用说明（B4 已拉回 Phase 0，本地实测通过）**：`cargo test` 的运行时断言
> （`event_vectors.rs` / `plan_vectors.rs` 对 `contracts/golden/*.json` 做字节相等）
> **必须显式指定 `+stable-x86_64-pc-windows-gnu` 工具链**。根因：仓库根
> `rust-toolchain.toml` 写 `channel = "stable"`（无 host 后缀），rustup 在本机
> （`Default host: x86_64-pc-windows-msvc`）把它解析成 `stable-x86_64-pc-windows-msvc`，
> 导致 build-script（proc-macro2 / serde / serde_json）为 msvc host 编译、要求
> `link.exe`（本机未装 MSVC C++ build tools）而失败。显式 `+stable-x86_64-pc-windows-gnu`
> 强制 gnu host → build-script 走 gnu 工具链 + rust-lld → 全过。
> **实测（Claude 本地 + GPT 独立复现均为 14 passed, 0 failed）**：
> - event_vectors.rs：7 passed（event_id/payload_digest/materialize/reject/interleaved）
> - plan_vectors.rs：7 passed（canonical/barrier_id/plan_revision_digest/semantic_plan_hash）
>
> 三语言字节一致性由**共享 `contracts/golden/*.json` 锚定**：Python(330) + TS(70) +
> Rust(14) 均对同一份 golden 做 RFC 8785 字节相等断言，三侧一致即跨语言认证。

### 2.2 门禁（scripts/check.ps1，15 项全 PASS）

| # | 门禁 | 语义 |
|---|------|------|
| 1 | codegen-drift | 三语言生成树与 schema 零漂移 |
| 2 | schema-validity | 13 JSON schema 有效 |
| 3 | golden-vectors | plan_hash + event_hash 向量字节一致 |
| 4 | chinese-coverage | 公共函数/类中文注释覆盖（目录缺失 fail-closed） |
| 5 | no-bare-print | 无裸 print/console.log（目录缺失 fail-closed） |
| 6 | secret-scan | 0 命中（目录缺失 fail-closed） |
| 7 | c-drive-paths | 无可控 C 盘路径（目录缺失 fail-closed） |
| 8 | catalog-47-ids | required-test-catalog 精确 47 个唯一 ID |
| 9 | no-doc-placeholders | docs/ 无未完成占位标记（四类待办/待定关键词） |
| 10 | ruff-lint | ruff check 0 error |
| 11 | mypy-strict | mypy 0 error |
| 12 | tsc-noemit | tsc --noEmit 干净 |
| 13 | vitest | TS 合同向量通过 |
| 14 | rust-check | cargo check 通过 |
| 15 | bootstrap-verify | bootstrap-dev.ps1 -VerifyOnly 通过 |

### 2.3 可复现构建

| 编号 | 检查 | 命令 | 期望 |
|------|------|------|------|
| B-1 | pnpm frozen lockfile | `pnpm install --frozen-lockfile` | exit 0 |
| B-2 | Python 锁定版本 | check.ps1 锁 `.python-version`=3.12 | Python 3.12.x |

### 2.4 CI fail-closed（无固定绿色）

| 编号 | 检查 | 判定 |
|------|------|------|
| C-1 | `.github/workflows/ci.yml` 无 `echo OK/neutral` 占位 | 每个 job 调真实命令 |
| C-2 | 无重复 workflow | plan-validation.yml 已删 |

---

## 3. Phase 0 兼容性 spike 认证状态（诚实标注）

| Spike | 状态 | 说明 |
|-------|------|------|
| clock_source | PASS | 多轮 monotonic + wall clock |
| windows_durable_io | PASS | 跨进程硬杀 + 独立进程读回 |
| named_pipe | PASS | 双进程 nonce + 重放拒绝 |
| git_object_bridge | PASS | 双进程 pack roundtrip + 零污染 |
| runner_identity | **PASS 或 BLOCKED_UNCERTIFIED** | Docker 可用时 broker 硬杀后容器孤儿存活 → PASS；Docker 不可用 → BLOCKED_UNCERTIFIED（allowlist ID `spike:runner_identity/docker_daemon`） |
| sqlite_wal_full | **BLOCKED_UNCERTIFIED** | 核心（process-kill 恢复 / group-commit 原子 / events/bytes/age 三阈值 / PRAGMA 读回）全 PASS；必选 disk-full ENOSPC 子项无 admin VHD 无法认证 → 按优先级顶层 = BLOCKED_UNCERTIFIED（allowlist ID `spike:sqlite_wal_full/disk_full_enospc`） |
| tauri_e2e | **BLOCKED_UNCERTIFIED**（Phase 0 spike ownership 保留） | Phase 0 拥有该 spike；本机无 WebView2/Tauri 运行时 → 正式 BLOCKED（allowlist ID `spike:tauri_e2e/webview_runtime`），非"待 Phase 2"移交 |

**两级"顶层"区分（消除 §3 与 §5 的表面矛盾）**：

1. **单个 spike 的顶层状态**（由该 spike 的子检查派生，优先级 `FAIL > BLOCKED_UNCERTIFIED > PASS`）：
   任一子检查 FAIL → 该 spike = FAIL；无 FAIL 但有子检查 BLOCKED → 该 spike =
   BLOCKED_UNCERTIFIED；全部子检查 PASS → 该 spike = PASS。据此 sqlite_wal_full 与
   tauri_e2e 两个 spike 的**自身顶层** = BLOCKED_UNCERTIFIED。
2. **验收运行的 topStatus**（`phase0-acceptance.ps1` 的总判定）：把每个 spike 的自身顶层
   **按 §1.4 allowlist 归约成一个通过/不通过的 check**——核心 spike 必须自身 PASS 才算
   check 通过；allowlist 内 spike（sqlite disk_full_enospc、tauri webview_runtime）自身
   为 BLOCKED_UNCERTIFIED **即算该 check 通过**。所有 check 通过 → 验收运行 topStatus = PASS。

> 因此"sqlite/tauri spike 自身顶层 = BLOCKED_UNCERTIFIED"与"验收运行 topStatus = PASS"
> **不矛盾**——前者是 spike 级事实标注，后者是 allowlist 归约后的运行级判定。

**冻结 BLOCKED allowlist（只有以下环境兼容 ID 允许 BLOCKED_UNCERTIFIED，其余必须 PASS）**：

| allowlist ID | 解封条件 |
|---|---|
| `spike:sqlite_wal_full/disk_full_enospc` | admin 挂载 ≤16MiB VHD（`create_enospc_vhd.ps1`）后 bench `--probe-dir <VHD>` |
| `spike:tauri_e2e/webview_runtime` | 装 WebView2 Runtime + Tauri CLI 的环境跑 E2E spike |
| `spike:runner_identity/docker_daemon` | 安装 Docker Desktop（WSL2 后端）并启动 daemon；验证容器孤儿存活（broker 硬杀后容器仍 Running） |

> **核心合同检查（codegen/schema/golden/三语言测试/receipt/lint/type）必须 PASS，不在 allowlist、不接受 BLOCKED。**

---

## 4. 明确移交 Phase 1 的项（不阻断 Phase 0）

以下属 Master Spec 定义、但本 Phase 0 不实现的项，逐项给出独立移交理由（不援引
「纯输入/输出」作全局规则——该措辞在 Phase 0 计划中仅约束 Task 4 事件物化器，
不泛化到并发/授权/Verifier 等其它子系统）：

1. **并发实现**：SQLite 单写者 coordinator、writer epoch CAS、多 Task 原子 claim 事务
   （Phase 0 只冻结 PreparedBatchV2/DurableEventV2 的**纯函数物化算法**与 schema）。
2. **授权合同 + 运行时**：Phase 0 **未定义** IntentAuthorization / ExecutionAuthorization
   的 JSON Schema——`contracts/schemas/` 下无此文件。RunSpec 仅有一个普通 string 字段
   `intentAuthorizationId`（关联 ID，非 `$ref`，无编译期悬空引用）。完整授权合同
   （schema 字段 + 拒绝规则）与消费/撤销状态机（`max_uses` 递减、fencing token 递增、
   control epoch 校验）整体移交 Phase 1；本文件不主张 Phase 0 已冻结授权 schema。
3. **PASS 从确定性断言派生**：receipt 聚合当前强制 owner/qualification/replay/digest 门禁；
   "PASS 完全由确定性断言自动派生、不接受任何 self-report" 属 Phase 1 Verifier 实现。
4. **真实 ENOSPC 认证**：需 admin 挂载 VHD，在 CI Windows runner 执行。
5. ~~TestReceipt 单一生成源~~ —— **已在 Phase 0 消除**（本轮 W6）：test-receipt 已从
   codegen catalog 移除（`codegen=false`），schema 是唯一结构真源，手写 dataclass 为唯一
   Python 表示；`validate()` 运行时读取 schema 的 `required`/`minItems`/`minProperties`
   并**无条件强制**（不再仅 `result==PASS` 时查），机械一致性测试绑定 dataclass 字段集
   ↔ schema 属性集。不再属 Phase 1。

> 这些项若在 Phase 0 审核中被提出，按第 1.3 条记入 `PHASE_1_BACKLOG.md`，不阻断收口。

---

## 5. 复现验收（唯一命令跑完全部机器可判定项）

**唯一验收入口 `scripts/phase0-acceptance.ps1`** 覆盖完整 A/B/C/spike 集合，
不再只跑 `check.ps1`（GPT 第三轮 item 1：`check.ps1` 不含 A-1 完整三目录 /
A-3b cargo test / B-1 frozen install / C-1/C-2 / spike receipt 验证）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/phase0-acceptance.ps1
```

该脚本按顺序执行并逐条记录退出码：

- **A-1** `pytest tests/contract tests/agent tests/security`
- **A-2** `pnpm --filter @factory/contracts test`（vitest）
- **A-3b** `cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked`
- **B-1** `pnpm install --frozen-lockfile`
- **B-2** Python 3.12 锁定校验
- **C-1** ci.yml 无 `echo OK/neutral` 占位（机器判定）
- **C-2** 无重复 workflow（plan-validation.yml 已删）
- **GATES** `check.ps1` 15 项门禁
- **spike:** 7 个 spike receipt 状态按 §3 allowlist 判定

完成后写出**唯一总回执** `.phase0-acceptance-receipt.json`，绑定：
`testedCandidateSha`（40 位）、`acceptanceSetVersion`、`cleanTree` + `dirtyFiles`、
每条 check 的 `id/exitCode/passed/tail`、`spikeStatuses`、`blockedAllowlist`、
`scriptDigests`（脚本 + 验收文档）、`schemaDigests`（全 schema）、环境版本。

**顶层 PASS 判定**：所有 check `passed=true`（spike 按 allowlist：核心必 PASS，
仅 allowlist ID 允许 BLOCKED_UNCERTIFIED）→ `topStatus=PASS` = Phase 0 达成。

> **锚点 SHA 说明**：本文档不硬编码候选 SHA；`testedCandidateSha` 由验收脚本
> 在**运行时**从 `git rev-parse HEAD` 绑定进总回执，并同时记录 `cleanTree` 状态，
> 确保回执证据与被测 commit 一一对应（GPT 第三轮 item 5）。
