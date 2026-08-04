# AI Coding Factory Phase 0 Foundation and Contracts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立可在 D 盘安全开发的多语言 monorepo，冻结 Python/Rust/TypeScript 共用合同、策略映射、golden vectors、测试回执和关键平台 spike，使后续实现不再依赖猜测。

**Architecture:** 根目录 `contracts/` 是语言中立的唯一协议源，生成或适配到 Python、TypeScript 和 Rust；所有运行代码只依赖版本化合同。Phase 0 不实现业务流程，但必须用真实 Windows/WSL/Docker 探针证明 Named Pipe、文件耐久、SQLite、时钟、Runner identity、桌面 E2E 和 Git Object Bridge 的关键假设。

**Tech Stack:** Python 3.12、pytest/Hypothesis、ruff/mypy、Rust/Cargo、TypeScript/pnpm/Vitest、JSON Schema、RFC 8785/JCS、PowerShell、SQLite、Win32 API、WSL2/Docker Desktop。

**Command contract:** Task 1 先创建最小、无业务逻辑的 `scripts/dev.ps1` 安全包装器；此后本文件所有 `python`、`pnpm`、`cargo` 和子 PowerShell 命令都必须作为 `scripts/dev.ps1 --` 后的参数执行。wrapper 在同一子进程设置 D 盘环境并做执行前后路径快照，不能依赖父终端环境。

---

## Task 1: 建立只在 D 盘工作的 monorepo 基线

**Files:**

- Create: `.editorconfig`
- Create: `.gitattributes`
- Create: `.env.example`
- Modify: `.gitignore`
- Create: `README.md`
- Create: `package.json`
- Create: `pnpm-lock.yaml`
- Create: `pnpm-workspace.yaml`
- Create: `Cargo.toml`
- Create: `Cargo.lock`
- Create: `rust-toolchain.toml`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `.node-version`
- Create: `.python-version`
- Create: `.github/workflows/plan-validation.yml`
- Create: `.github/workflows/ci.yml`
- Create: `.github/pull_request_template.md`
- Create: `docs/operations/github-development-workflow.md`
- Create: `scripts/bootstrap-dev.ps1`
- Create: `scripts/dev.ps1`
- Create: `scripts/check.ps1`
- Create: `scripts/test.ps1`
- Test: `tests/contract/test_repository_layout.py`

- [ ] **Step 1: 先创建最小 D 盘命令包装器，再写失败的仓库布局测试**

`scripts/dev.ps1` 在执行任何开发工具前解析 `TEMP/TMP/CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME/PNPM_HOME/NPM_CONFIG_CACHE/UV_CACHE_DIR/PIP_CACHE_DIR/PLAYWRIGHT_BROWSERS_PATH` 的最终物理路径，全部绑定到 `D:\codex项目\AI-Coding-Factory-Data\dev`，然后以参数数组启动 `--` 后的子进程；路径不可信、子进程产生可控 C 盘写入或退出非零时，wrapper 返回非零。该安全 wrapper 不创建业务实现，因此允许作为首个失败测试的前置 harness。

```python
from pathlib import Path


def test_required_workspace_files_exist() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    required = {
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "pyproject.toml",
        "uv.lock",
        ".node-version",
        ".github/workflows/plan-validation.yml",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md",
        "scripts/bootstrap-dev.ps1",
        "scripts/dev.ps1",
        "scripts/check.ps1",
        "scripts/test.ps1",
    }
    assert required <= {str(path.relative_to(repo_root)).replace("\\", "/") for path in repo_root.rglob("*")}
```

- [ ] **Step 2: 运行测试并确认因缺少工程文件失败**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_repository_layout.py -q`

Expected: FAIL，报告缺少 workspace 文件。

- [ ] **Step 3: 创建工程文件和 D 盘开发启动器**

`scripts/bootstrap-dev.ps1` 复用 `scripts/dev.ps1` 的路径解析，不单独维护第二套环境规则。首次 compatibility probe 选定版本后，bootstrap 生成并校验 `.node-version`、`.python-version`、`rust-toolchain.toml`、`pnpm-lock.yaml`、`Cargo.lock` 和 `uv.lock`；计划不预猜会漂移的具体版本号。`plan-validation.yml` 与 `ci.yml` 使用 `pull_request` 触发；固定、始终产出的聚合 check 名称为 `plan-consistency/contracts/python/rust/desktop/security`，不得用 workflow/job 级 path filter 让 required check 消失，按路径跳过只能在 job 内产生明确 neutral/success 结论。receipt 同时记录 `reviewedHeadSha`、GitHub test-merge `ciTestMergeSha` 和 base SHA，不能把 Actions 的 `GITHUB_SHA` 当作审核 head；首版不启用 merge queue，基础 Ruleset 必须启用 required checks strict/up-to-date，未来启用 merge queue 前必须另加 `merge_group` 合同。PR 模板记录 Plan Task、上述 SHA、Claude self-review/Verifier/Codex receipt digest、Codex delivery actor、风险和恢复说明。仓库创建与基础 Ruleset 由总计划 §1.4 的一次性 bootstrap 完成，本 Task 合并后只把这些 Actions checks 加入 required checks；`factory/codex-review` 要等 Phase 4 Task 8 的独立 Attester 安装并完成升级 receipt 后才加入。

- [ ] **Step 4: 运行仓库基线检查**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap-dev.ps1 -VerifyOnly`

Expected: PASS，并输出机器可读的 D 盘路径摘要；不得创建 C 盘文件。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_repository_layout.py -q`

Expected: PASS。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- .editorconfig .gitattributes .env.example .gitignore README.md package.json pnpm-lock.yaml pnpm-workspace.yaml Cargo.toml Cargo.lock rust-toolchain.toml pyproject.toml uv.lock .node-version .python-version .github/workflows/plan-validation.yml .github/workflows/ci.yml .github/pull_request_template.md docs/operations/github-development-workflow.md scripts/bootstrap-dev.ps1 scripts/dev.ps1 scripts/check.ps1 scripts/test.ps1 tests/contract/test_repository_layout.py
git commit -m "chore: bootstrap multi-runtime workspace"
```

## Task 2: 冻结语言中立 schema、策略和测试目录

**Files:**

- Create: `contracts/schemas/run-spec.v1.schema.json`
- Create: `contracts/schemas/plan-revision.v1.schema.json`
- Create: `contracts/schemas/control-command.v1.schema.json`
- Create: `contracts/schemas/credential-ref.v1.schema.json`
- Create: `contracts/schemas/task-intake.v1.schema.json`
- Create: `contracts/schemas/claude-self-review.v1.schema.json`
- Create: `contracts/schemas/prepared-event.v2.schema.json`
- Create: `contracts/schemas/prepared-batch.v2.schema.json`
- Create: `contracts/schemas/durable-event.v2.schema.json`
- Create: `contracts/schemas/ipc-envelope.v1.schema.json`
- Create: `contracts/schemas/runner-protocol.v1.schema.json`
- Create: `contracts/schemas/test-receipt.v1.schema.json`
- Create: `contracts/schemas/compatibility-manifest.v1.schema.json`
- Create: `contracts/policies/stage-capability-map.v1.json`
- Create: `contracts/policies/node-capability-map.v1.json`
- Create: `contracts/policies/node-pause-policy.v1.json`
- Create: `contracts/testing/required-test-catalog.v1.json`
- Create: `contracts/codegen/catalog.v1.json`
- Create: `contracts/codegen/generate.py`
- Create: `packages/factory-contracts/package.json`
- Modify: `pnpm-lock.yaml`
- Create: `packages/factory-contracts/tsconfig.json`
- Create: `packages/factory-contracts/src/index.ts`
- Create: `packages/factory-contracts/src/generated/contracts.ts`
- Create: `apps/agent/src/factory_agent/contracts/generated/__init__.py`
- Create: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Create: `crates/factory-contracts/Cargo.toml`
- Modify: `Cargo.lock`
- Create: `crates/factory-contracts/src/lib.rs`
- Create: `crates/factory-contracts/src/generated/mod.rs`
- Create: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `tests/contract/test_schema_catalog.py`

- [ ] **Step 1: 写 schema catalog 失败测试**

```python
def test_unknown_node_type_is_not_present_in_capability_catalog(catalog) -> None:
    assert set(catalog.node_types) == {
        "PLAN", "DESIGN_REVIEW", "BOOTSTRAP_REPOSITORY", "IMPLEMENT", "VERIFY",
        "CODE_REVIEW", "ATTEST_REVIEW", "PUBLISH_PR", "MERGE", "BUILD_ARTIFACT",
        "DEPLOY_STAGING", "ACCEPT_STAGING", "DEPLOY_PRODUCTION",
        "ACCEPT_PRODUCTION", "ROLLBACK", "RECONCILE_TARGET",
        "RESTORE_DRILL",
    }
```

测试还必须证明 stage capability 数组已完全展开并排序、未知字段拒绝、required test catalog 从 Master Spec 展开为 47 个唯一 ID。通用 `credential-ref.v1` 必须在此阶段冻结并生成三语言类型，后续 Forge/Server/Database Profile 只能 `$ref` 或导入该类型，不得另建不兼容副本。

`task-intake.v1` 必须严格区分客户端与服务端字段：`TaskIntakeRequest` 只允许自然语言需求、target stage、repository selection `mode/root/baseBranch`、环境选择、预算上限和用户风险确认；不得接受客户端自报的 capability digest、规范化 binding、风险判定或 IntentAuthorization 身份。`TaskIntakeAccepted` 只由 Agent 生成，包含 task/IntentAuthorization ID、规范化 repository binding、existing 模式的完整 base SHA 或 new 模式的 bootstrap pending 状态、服务端求交后的 `allowedCapabilitySetDigest`、有效预算/风险摘要和 `expiresAt`；两者都不得暴露 workspace lease、checkpoint namespace 或 bundle 内部路径。

`claude-self-review.v1` 必须绑定完整 `reviewedBaseSha/reviewedCandidateSha`、`rubricVersion`、`result=pass|needs_fix`、稳定 finding ID、severity、file/symbol/evidence/passCondition、finding 状态和 `evidenceDigests`，拒绝隐藏思维链/自由文本冒充证据。schema 明确 self-review 的 actor 是候选源码作者，因此它只能作为 `IMPLEMENT` 的作者质量 Artifact，不能满足 `CODE_REVIEW/ATTEST_REVIEW`、不能关闭 Codex finding，也不能授权 `PUBLISH_PR/MERGE`；candidate SHA 漂移后 receipt 必须失效。

`compatibility-manifest.v1` 必须冻结单一路径 `eventBatchParameters.{maxBatchEvents,maxBatchBytes,maxBatchAgeMs,synchronous,parameterTupleDigest,sqliteSpikeReceiptDigest}`，其中 `synchronous` 只能为 `FULL`。SQLite spike receipt 必须绑定同一参数 tuple、manifest schema digest、`benchmarkProfileDigest` 和环境 digest；emitter、Phase 1 consumer 与 Phase 6 validator 都重算 tuple digest 并核对 receipt 输入，禁止只比较 receipt 文件名。`RESTORE_DRILL` 固定需要 `db.restore`、`restore.validation.instance`、`db.check`，resource fingerprint 必须证明目标不是生产实例。测试 catalog 的每行还必须含总计划 §6 冻结的 `implementationContributors/finalPassOwner/requiredReplays/scenarioContractDigest`；`STREAM-DUR-001` 的场景摘要必须包含稳定 crashPointId `record_prefix_mid_batch/record_torn_next`。node pause policy 必须覆盖每种 nodeType 的 safe point、grace、关键区和 UNKNOWN 处置。

- [ ] **Step 2: 运行并确认 catalog 尚不存在**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_schema_catalog.py -q`

Expected: FAIL，原因是 schema/catalog 未创建。

- [ ] **Step 3: 实现严格 schema 和确定性 codegen**

`catalog.v1.json` 逐项冻结 codegen 输入、生成目标和 `generated/runtime-only` 分类；`generate.py` 只读取 catalog 标记为 generated 的合同，确定性输出到 `packages/factory-contracts/src/generated/contracts.ts`、`apps/agent/src/factory_agent/contracts/generated/models.py` 和 `crates/factory-contracts/src/generated/contracts.rs`。三个 generated 文件不维护手写副本；包级 `index.ts`、Python `__init__.py` 与 Rust `lib.rs` 只能 re-export generated 类型和 Task 3/4 明确列出的 canonical/plan/event 手写算法模块，不得复制 DTO 定义。runtime-only schema 必须在 catalog 说明 Adapter 动态验证边界。`--check` 模式在输入遗漏或输出漂移时失败且不改文件。schema 对未知字段使用 fail-closed 规则，数值范围满足 I-JSON/JCS。

- [ ] **Step 4: 验证 schema、生成结果和测试目录**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python contracts/codegen/generate.py`

Expected: PASS，首次生成三个语言的确定性输出。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python contracts/codegen/generate.py --check`

Expected: PASS，三语言生成树无漂移。

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_schema_catalog.py -q`

Expected: PASS，root pnpm/Cargo workspace 能发现两个合同包，47 个测试 ID 全部唯一且可追踪。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- pnpm-lock.yaml Cargo.lock contracts/schemas/run-spec.v1.schema.json contracts/schemas/plan-revision.v1.schema.json contracts/schemas/control-command.v1.schema.json contracts/schemas/credential-ref.v1.schema.json contracts/schemas/task-intake.v1.schema.json contracts/schemas/claude-self-review.v1.schema.json contracts/schemas/prepared-event.v2.schema.json contracts/schemas/prepared-batch.v2.schema.json contracts/schemas/durable-event.v2.schema.json contracts/schemas/ipc-envelope.v1.schema.json contracts/schemas/runner-protocol.v1.schema.json contracts/schemas/test-receipt.v1.schema.json contracts/schemas/compatibility-manifest.v1.schema.json contracts/policies/stage-capability-map.v1.json contracts/policies/node-capability-map.v1.json contracts/policies/node-pause-policy.v1.json contracts/testing/required-test-catalog.v1.json contracts/codegen/catalog.v1.json contracts/codegen/generate.py packages/factory-contracts/package.json packages/factory-contracts/tsconfig.json packages/factory-contracts/src/index.ts packages/factory-contracts/src/generated/contracts.ts apps/agent/src/factory_agent/contracts/generated/__init__.py apps/agent/src/factory_agent/contracts/generated/models.py crates/factory-contracts/Cargo.toml crates/factory-contracts/src/lib.rs crates/factory-contracts/src/generated/mod.rs crates/factory-contracts/src/generated/contracts.rs tests/contract/test_schema_catalog.py
git commit -m "feat(contracts): freeze schemas and policy catalogs"
```

## Task 3: 实现跨语言 canonical JSON、计划 hash 和 barrier identity

**Files:**

- Create: `contracts/golden/canonical-json.v1.json`
- Create: `contracts/golden/plan-hash.v1.json`
- Create: `contracts/golden/barrier-id.v1.json`
- Create: `apps/agent/src/factory_agent/policy/canonical_json.py`
- Create: `apps/agent/src/factory_agent/policy/plan_hash.py`
- Create: `packages/factory-contracts/src/canonical.ts`
- Create: `packages/factory-contracts/src/plan.ts`
- Modify: `packages/factory-contracts/src/index.ts`
- Create: `crates/factory-contracts/src/canonical.rs`
- Create: `crates/factory-contracts/src/plan.rs`
- Modify: `crates/factory-contracts/src/lib.rs`
- Test: `tests/contract/test_plan_hash_vectors.py`
- Test: `packages/factory-contracts/src/plan.test.ts`
- Test: `crates/factory-contracts/tests/plan_vectors.rs`

- [ ] **Step 1: 写覆盖 Unicode、数字和谱系差异的失败向量**

向量必须包含 NFC 组合字符、`1/1.0`、`-0`、指数、null/缺省、重复 key、NaN/Infinity/超范围数字，以及“semantic 相同但 revision 谱系不同”的用例。

- [ ] **Step 2: 运行三语言测试并确认失败**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_plan_hash_vectors.py -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- pnpm --filter @factory/contracts test -- plan.test.ts
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- cargo test -p factory-contracts --test plan_vectors
```

Expected: 三项均因 canonicalizer/hash 未实现失败。

- [ ] **Step 3: 分别实现同一域分离算法**

实现 `semanticPlanHash`、`planRevisionDigest` 和 `barrierId`；字段 in/out 清单严格来自 Master Spec §6.2，任何非法输入必须在 hash 前拒绝。公共函数添加中文注释，记录算法版本和失败错误码但不记录完整计划正文。

- [ ] **Step 4: 运行三语言 byte-for-byte 比较**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite contract-plan-hash`

Expected: PASS，Python/Rust/TypeScript 输出完全相同，`PLAN-HASH-001` 产生前置 receipt。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- contracts/golden/canonical-json.v1.json contracts/golden/plan-hash.v1.json contracts/golden/barrier-id.v1.json apps/agent/src/factory_agent/policy/canonical_json.py apps/agent/src/factory_agent/policy/plan_hash.py packages/factory-contracts/src/canonical.ts packages/factory-contracts/src/plan.ts packages/factory-contracts/src/index.ts packages/factory-contracts/src/plan.test.ts crates/factory-contracts/src/canonical.rs crates/factory-contracts/src/plan.rs crates/factory-contracts/src/lib.rs crates/factory-contracts/tests/plan_vectors.rs tests/contract/test_plan_hash_vectors.py
git commit -m "feat(contracts): add cross-language plan identities"
```

## Task 4: 实现 PreparedBatchV2 和 DurableEventV2 golden vectors

**Files:**

- Create: `contracts/golden/event-hash.v2.json`
- Create: `contracts/golden/prepared-batch.v2.json`
- Create: `apps/agent/src/factory_agent/domain/events.py`
- Create: `packages/factory-contracts/src/event.ts`
- Modify: `packages/factory-contracts/src/index.ts`
- Create: `crates/factory-contracts/src/event.rs`
- Modify: `crates/factory-contracts/src/lib.rs`
- Test: `tests/contract/test_event_hash_vectors.py`
- Test: `packages/factory-contracts/src/event.test.ts`
- Test: `crates/factory-contracts/tests/event_vectors.rs`

- [ ] **Step 1: 写批内链、跨 Task 和竞争 head 的失败向量**

固定同 Task 至少两事件、多 Task 交错、genesis、null/缺省、未知字段、前驱漂移和两个批次竞争同一旧 head；Adapter 输入含 `ingestEventId`，但不得预填 `taskSeq/eventId/eventDigest/head`。

- [ ] **Step 2: 运行失败测试**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite contract-event-hash`

Expected: FAIL，报告物化器未实现。

- [ ] **Step 3: 实现纯函数物化器**

物化器按 `batchOrdinal` 生成稳定 identity，逐 Task 连接 predecessor；并发 CAS 和数据库事务留到 Phase 1，Phase 0 只冻结纯输入/输出与拒绝规则。

- [ ] **Step 4: 运行跨语言向量**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite contract-event-hash`

Expected: PASS，`EVENT-HASH-001` 的合同层结果一致。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- contracts/golden/event-hash.v2.json contracts/golden/prepared-batch.v2.json apps/agent/src/factory_agent/domain/events.py packages/factory-contracts/src/event.ts packages/factory-contracts/src/index.ts packages/factory-contracts/src/event.test.ts crates/factory-contracts/src/event.rs crates/factory-contracts/src/lib.rs crates/factory-contracts/tests/event_vectors.rs tests/contract/test_event_hash_vectors.py
git commit -m "feat(contracts): freeze durable event identities"
```

## Task 5: 建立配置、关联 ID、结构化日志和存储位置合同骨架

**Files:**

- Create: `apps/agent/src/factory_agent/config.py`
- Create: `apps/agent/src/factory_agent/errors.py`
- Create: `apps/agent/src/factory_agent/domain/identities.py`
- Create: `apps/agent/src/factory_agent/observability/logging.py`
- Create: `apps/agent/src/factory_agent/security/storage_contract.py`
- Test: `tests/agent/unit/test_config.py`
- Test: `tests/agent/unit/test_logging_redaction.py`
- Test: `tests/security/test_storage_contract.py`

- [ ] **Step 1: 写 D 缺失、路径伪装和日志泄密失败测试**

测试覆盖 C/E/F fallback、junction、symlink、`SUBST`、网络/可移动卷、未知 volume identity；日志 fixture 包含 token/password/private key，输出必须只含稳定错误码和脱敏摘要。

- [ ] **Step 2: 运行并确认 fail-closed 行为尚未实现**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/agent/unit/test_config.py tests/agent/unit/test_logging_redaction.py tests/security/test_storage_contract.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现只读验证器和统一 logger**

Phase 0 的 `StorageLocationContract` 只做句柄/卷/reparse 链验证和 dry-run receipt，不迁移 WSL/Docker、不创建业务目录。所有公共函数使用中文注释；logger 强制 request/task/run/step/attempt/trace 字段并拒绝原始敏感 payload。

- [ ] **Step 4: 运行安全测试和日志快照**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/agent/unit tests/security/test_storage_contract.py -q`

Expected: PASS；日志 secret scan 为 0 命中。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- apps/agent/src/factory_agent/config.py apps/agent/src/factory_agent/errors.py apps/agent/src/factory_agent/domain/identities.py apps/agent/src/factory_agent/observability/logging.py apps/agent/src/factory_agent/security/storage_contract.py tests/agent/unit/test_config.py tests/agent/unit/test_logging_redaction.py tests/security/test_storage_contract.py
git commit -m "feat(core): add D-root configuration and safe logging"
```

## Task 6: 建立 Compatibility Manifest 和机器测试回执

**Files:**

- Create: `contracts/benchmarks/benchmark-profile.v1.json`
- Create: `apps/agent/src/factory_agent/testing/receipts.py`
- Create: `apps/agent/src/factory_agent/testing/required_test_catalog.py`
- Create: `apps/agent/src/factory_agent/testing/verify_receipts.py`
- Create: `tools/compat-probes/emit_manifest.py`
- Test: `tests/contract/test_compatibility_manifest.py`
- Test: `tests/contract/test_receipt_coverage.py`
- Create: `tests/fixtures/receipts/valid.json`
- Create: `tests/fixtures/receipts/missing.json`
- Create: `tests/fixtures/receipts/conflict.json`

- [ ] **Step 1: 写 manifest 漂移与 receipt 缺失失败测试**

测试必须拒绝 schema/catalog/profile digest 不一致、未知 CLI/runtime、同一测试 ID 冲突结果、未映射 ID、缺环境 digest、缺失/越界 `eventBatchParameters`、`synchronous != FULL`、参数 tuple/SQLite spike receipt 输入或 digest 不一致和手工文本 PASS。

- [ ] **Step 2: 运行并确认聚合器尚未实现**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_compatibility_manifest.py tests/contract/test_receipt_coverage.py -q`

Expected: FAIL。

- [ ] **Step 3: 实现签名输入模型和 fail-closed 聚合器**

Phase 0 生成开发用未签名 manifest payload 和 digest；`emit_manifest.py` 只从已验证 SQLite spike receipt 读取实际参数，重算 `parameterTupleDigest`，并写入完整 `eventBatchParameters`、`sqliteSpikeReceiptDigest` 和 `benchmarkProfileDigest`。receipt 的 tuple/schema/profile/environment 任一输入不匹配、字段缺失或组合未认证均 fail closed；正式签名在 Phase 6。聚合器从 required test catalog 读取 47 个 ID，不从 Markdown 人工复制成功状态。

- [ ] **Step 4: 验证 manifest 与覆盖聚合**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m factory_agent.testing.verify_receipts --catalog contracts/testing/required-test-catalog.v1.json --receipts tests/fixtures/receipts`

Expected: 正例 PASS，缺失/冲突 fixture 全部失败。

- [ ] **Step 5: 创建原子提交**

```powershell
git add -- contracts/benchmarks/benchmark-profile.v1.json apps/agent/src/factory_agent/testing/receipts.py apps/agent/src/factory_agent/testing/required_test_catalog.py apps/agent/src/factory_agent/testing/verify_receipts.py tools/compat-probes/emit_manifest.py tests/contract/test_compatibility_manifest.py tests/contract/test_receipt_coverage.py tests/fixtures/receipts/valid.json tests/fixtures/receipts/missing.json tests/fixtures/receipts/conflict.json
git commit -m "test(core): add compatibility and receipt contracts"
```

## Task 7: 完成六项平台 compatibility spike

**Files:**

- Modify: `Cargo.lock`
- Modify: `pnpm-lock.yaml`
- Create: `tools/compat-probes/windows_durable_io/Cargo.toml`
- Create: `tools/compat-probes/windows_durable_io/src/main.rs`
- Create: `tools/compat-probes/sqlite_wal_full/bench.py`
- Create: `tools/compat-probes/clock_source/Cargo.toml`
- Create: `tools/compat-probes/clock_source/src/main.rs`
- Create: `tools/compat-probes/named_pipe/python_server.py`
- Create: `tools/compat-probes/named_pipe/Cargo.toml`
- Create: `tools/compat-probes/named_pipe/src/main.rs`
- Create: `tools/compat-probes/runner_identity/Cargo.toml`
- Create: `tools/compat-probes/runner_identity/src/main.rs`
- Create: `tools/compat-probes/git_object_bridge/probe.py`
- Create: `tools/compat-probes/tauri_e2e/package.json`
- Create: `tools/compat-probes/tauri_e2e/specs/shell.spec.ts`
- Create: `scripts/spikes/test-durable-io.ps1`
- Create: `scripts/spikes/test-sqlite-wal.ps1`
- Create: `scripts/spikes/test-clock-source.ps1`
- Create: `scripts/spikes/test-named-pipe.ps1`
- Create: `scripts/spikes/test-runner-identity.ps1`
- Create: `scripts/spikes/test-git-bridge.ps1`
- Create: `scripts/spikes/test-tauri-e2e.ps1`

- [ ] **Step 1: 为每项 spike 定义机器 PASS/FAIL receipt schema**

每个 probe 都记录输入环境、实际操作、可观察事实、断言、产物 digest 和 manifest digest；不得把“命令返回 0”单独当作 PASS。

- [ ] **Step 2: 先运行空实现并确认每项失败**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite compatibility-spikes`

Expected: FAIL，并列出尚未认证的 durable I/O、SQLite、clock、pipe、runner、Git 和 Tauri 项。

- [ ] **Step 3: 逐项实现并分别提交**

固定验证内容：Windows/WSL flush→rename→父目录耐久；WAL/FULL/group commit/disk-full/kill，并用 `eventBatchParameters` 的实际组合生成机器 receipt。该 receipt 明确保存参数 tuple 与 digest、manifest schema digest、`benchmarkProfileDigest`、环境 digest、WAL/FULL 事实和性能样本 digest，Task 6 emitter 验证后才回写开发 manifest；含睡眠 monotonic 与 boot identity；当前 SID ACL + Rust/Python 双 pipe + nonce 防重放；Broker 被杀后 Docker inspect/process set；Git bundle/pack 往返且用户 workspace 零污染；真实 WebView/Tauri 与原生托盘 E2E。Python 无法证明 Windows 目录耐久时，立即采用 Rust helper 并冻结合同。

- [ ] **Step 4: 运行全部 spike**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite compatibility-spikes -EmitReceipts`

Expected: 已支持环境全部 PASS；外部未满足项明确 `BLOCKED_UNCERTIFIED`，不得伪造兼容。

- [ ] **Step 5: 为每个 spike 建立独立原子提交**

```powershell
git add -- tools/compat-probes/windows_durable_io/Cargo.toml tools/compat-probes/windows_durable_io/src/main.rs scripts/spikes/test-durable-io.ps1
git commit -m "test(spike): certify Windows durable IO"
```

其余六项使用同样的精确路径方式分别提交，提交消息依次为 `certify SQLite WAL FULL`、`certify execution clock`、`certify authenticated named pipe`、`certify WSL runner identity`、`certify Git object bridge`、`certify Tauri E2E`；禁止合并成一次提交。

## Task 8: 完成 Phase 0 总门禁

**Files:**

- Modify: `scripts/check.ps1`
- Modify: `scripts/test.ps1`
- Create: `docs/protocols/contracts-v1.md`
- Create: `docs/operations/development-environment.md`
- Create: `tests/contract/test_no_placeholders.py`

- [ ] **Step 1: 写总门禁失败测试**

门禁检查生成代码漂移、schema 无效、golden vector 不一致、中文注释缺失、正式 logger 之外的 `print/console.log`、秘密模式、C 盘可控路径、计划测试 catalog 漏项和文档占位符。

- [ ] **Step 2: 运行总门禁并修复所有真实失败**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1`

Expected: 所有检查 PASS；任何未认证平台事实作为显式 blocked receipt，而不是跳过。

- [ ] **Step 3: 运行完整 Phase 0 测试**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract tests/agent/unit tests/security -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- pnpm --filter @factory/contracts test
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- cargo test -p factory-contracts
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/test.ps1 -Suite compatibility-spikes
```

Expected: 0 failures；`PLAN-HASH-001`、`EVENT-HASH-001` 合同层和 compatibility spike receipt 均存在。

- [ ] **Step 4: 做需求审查和代码质量审查**

审查必须确认后续 Phase 1/2 只能依赖冻结 port/schema；任何“先实现再补合同”、多 writer、C 盘 fallback 或 fake probe 均为 blocker。

- [ ] **Step 5: 创建阶段收口提交**

```powershell
git add -- scripts/check.ps1 scripts/test.ps1 docs/protocols/contracts-v1.md docs/operations/development-environment.md tests/contract/test_no_placeholders.py
git commit -m "docs: certify foundation and contract baseline"
```

Phase 0 完成后，Phase 1 Control Plane 与 Phase 2 Desktop/IPC 才进入 ready 状态。
