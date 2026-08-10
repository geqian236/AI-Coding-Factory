# AI Coding Factory Complete Program Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付一个常驻 Windows、可观察、可暂停恢复、可按任务选择目标终点，并能从需求一直执行到 Linux 部署验收的完整 AI 软件研发流水线。

**Architecture:** 使用 Tauri 2 + React/TypeScript 实现桌面壳，独立 Python 3.12 `factory-agent.exe` 作为权威控制平面，通过认证 Windows Named Pipe 通信；Factory 专用 WSL2 distro + Docker Desktop 承载隔离 Runner。SQLite WAL/FULL、内容寻址对象存储、版本化合同、lease/fencing、追加 receipt 和 Reconciler 共同保证状态、事件与外部副作用可恢复且不可伪造。

**Tech Stack:** Tauri 2、Rust、React、TypeScript、Vite、xterm.js、Python 3.12、Pydantic、pytest/Hypothesis、SQLite、Windows Named Pipe/Job Object、WSL2、Docker Desktop/Compose、Git bundle/pack、GitHub App/Actions/Rulesets、SSH、Nginx、OCI Registry、Playwright/Vitest/Cargo test。

---

## 1. 范围和交付原则

### 1.1 本计划包含

- 六种可选任务终点：`DESIGN_APPROVED`、`CODEX_APPROVED`、`PR_READY`、`MERGED`、`STAGING_ACCEPTED`、`PRODUCTION_ACCEPTED`。
- Windows 桌面应用、托盘、登录自启动、暂停/停止/恢复和真实时间线/终端弹窗。
- 独立常驻 Agent、确定性状态机、授权、资源调度、事件链、Artifact、恢复和通知。
- Factory 专用 WSL2/Docker Runner、Claude Developer、Codex Reviewer、Verifier 和 Release Runner。
- 新旧仓库、Git Object Bridge、PR/CI/merge、镜像构建与 digest 身份链。
- 本项目自身使用 GitHub 公开仓库和逐 Task 分支/PR：当前 owner 为 GitHub Free，用户已明确选择公开仓库以启用 Ruleset；Claude 实现并结构化自审，Verifier 做确定性检查，Codex 独立审核、创建 PR 并在门禁通过后合并，不能因为是在开发 Factory 自身就绕过产品要求的证据链。
- Linux SSH/Compose/Nginx/数据库迁移、验收、自动回滚和失败隔离。
- 严格 D 盘存储合同、Vault、兼容性清单、安装升级、备份恢复和全链路 DoD。

### 1.2 本计划不包含

- Kubernetes、百分比 Canary、多用户控制平面或云端常驻 Worker。
- 模型 API key 管理或绕过本机 Claude/Codex CLI 登录。
- 未经实机认证的云厂商兼容性宣传。
- 展示模型隐藏思维链；界面只展示公开结构化活动、摘要和真实工具/进程事件。

### 1.3 完整交付与内部里程碑

实施分阶段是为了控制集成风险，不改变最终范围。Phase 0–4 达到 `SELF_HOSTING_CODEX_APPROVED` 后允许 Factory 用自身闭环继续开发后半程，但只有 Phase 0–6、全部 47 个代表流程和 11 项 DoD 全部通过，才允许宣称首个完整版本交付。

### 1.4 本项目自身的 Claude、Codex 与 GitHub 协作合同

实施启动先由 Codex 通过用户已连接的 GitHub plugin 做只读预检：核对 owner/套餐、公开仓库 Ruleset 可用性、admin 权限、基础/Attester App 可安装性、API 能力，以及一个与 Publisher/Merger App 分离且能对 App-authored PR 提交可计数 approval 的 bootstrap reviewer actor；任一不满足即 `BLOCKED`，不得先创建或推送半保护远端。当前 owner `geqian236` 使用 GitHub Free，用户已明确选择公开仓库；预检通过后，Codex 在该 owner 下创建名为 `AI-Coding-Factory` 的公开仓库。一次性 bootstrap 只允许 Codex 推送已核对的本地基准 `main`，随后立即启用“必须经 PR、禁止 force-push/删除、要求 squash 线性历史、会话解决、required checks strict/up-to-date”的基础 Ruleset，并记录 repository/ruleset ID、版本、bypass actor 和撤销条件。当前 Master Spec 与完整计划作为第一张文档 PR，由 Codex 创建；Codex 通过预授权的 bootstrap reviewer 身份自动提交原生 approval，再通过 Publisher/Merger App 合并，不要求用户逐 PR 点击。

每个实施 Task 默认对应一个远端 `factory/phase-<n>-task-<n>-<slug>` 分支和一张由 Codex 创建的 PR。Claude Developer 是任务 worktree 的唯一源码写者，并必须先对精确 candidate SHA 结构化自审、自行修复到 PASS；Verifier 再运行机械门禁；Codex Reviewer 只在独立只读检出中审核精确 candidate SHA、Claude self-review、测试证据、中文注释和日志。任何 Codex finding 仍交回 Claude，Claude 修复并重新自审后，旧 self-review/Verifier/Codex receipt 和远端 check 自动失效。只有当前 receipts 全部有效且 `target_stage >= PR_READY` 时，Codex Delivery Orchestrator 才通过 GitHub plugin（建设期）或产品内 GitHub Adapter（Phase 4 后）push 并创建/更新 PR。Phase 4 Task 8 合并前使用“签名本地 Codex receipt + Codex 自动调用独立 bootstrap reviewer 原生 approval”的过渡门禁；Task 8 合并后安装只读源码/Checks 写的 Review Attester App、发布首个 `factory/codex-review` 并把 check name + expected App ID 加入 Ruleset，以该 required check 取代并撤销 bootstrap reviewer credential。达到 `MERGED` 且全部门禁有效时由 Codex 执行 squash merge。正常进展不打扰用户，只在所选终点完成、权限越界、不可恢复 blocker 或需要新的外部授权时通知。

## 2. 冻结的工程结构

```text
AI-Coding-Factory/
  README.md
  .editorconfig
  .gitattributes
  .gitignore
  package.json
  pnpm-lock.yaml
  pnpm-workspace.yaml
  Cargo.toml
  Cargo.lock
  rust-toolchain.toml
  pyproject.toml
  uv.lock
  .node-version
  .python-version
  .env.example
  .github/
    workflows/
      plan-validation.yml
      ci.yml
    pull_request_template.md
  contracts/
    schemas/
    policies/
    golden/
    fixtures/
    benchmarks/
    testing/
    codegen/
  packages/
    factory-contracts/
  crates/
    factory-contracts/
    factory-wsl-broker/
    factory-toast-helper/
  apps/
    desktop/
      package.json
      vite.config.ts
      src/
        app/
        features/
        ipc/
        shared/
        stores/
        styles/
        telemetry/
      src-tauri/
        Cargo.toml
        src/
        capabilities/
        tauri.conf.json
    agent/
      src/factory_agent/
        __main__.py
        bootstrap.py
        config.py
        errors.py
        application/
        compatibility/
        contracts/
        domain/
        harness/
        integrations/
        ipc/
        observability/
        onboarding/
        ops/
        policy/
        ports/
        prompts/
        recovery/
        release/
        scheduler/
        security/
        state_machine/
        storage/
        testing/
      factory-agent.spec
  runners/
    images/
      base/
      claude/
      codex/
      verifier/
      release/
      buildkit/
    shim/
    protocol/
  benchmarks/
    event_ingest/
    scheduler/
  tests/
    agent/
    contract/
    integration/
    chaos/
    desktop-e2e/
    release-e2e/
    security/
    packaging/
    fixtures/
    performance/
    e2e/
    dod/
    support/
  ops/
    installer/
    remote/
    lab/
    runbooks/
    windows/
  scripts/
    bootstrap-dev.ps1
    dev.ps1
    check.ps1
    test.ps1
    package.ps1
    verify-storage.ps1
    spikes/
  tools/
    chaos/
    compat-probes/
    loadgen/
    dod/
  docs/
    architecture/
    protocols/
    security/
    operations/
    release/
    superpowers/plans/
    superpowers/specs/
```

边界规则：

- `contracts/` 是跨 Python/Rust/TypeScript 的唯一协议源；运行代码不得维护第四份手写枚举。
- `apps/agent` 独占 SQLite、Vault、Runner 句柄、调度和副作用；桌面端只持认证 IPC 客户端。
- `runners/` 只通过冻结协议接收任务，不直接写控制库。
- `ops/remote` 是远端 helper 的唯一源码；Release Runner 镜像只复制其固定 digest。每个写入口必须显式校验 release ID、fencing token、stepSeq 和幂等键。
- 所有运行数据都由 `StorageLocationContract` 解析到 `D:\codex项目`，仓库不得写入本地数据库、日志、凭据或运行 transcript。

## 3. 实施依赖图

```text
Phase 0  工程基础、合同与兼容性骨架
   ├── Phase 1  控制平面、状态、存储、授权与恢复
   └── Phase 2  桌面壳、Named Pipe、时间线与终端
          \       /
           Phase 3  WSL/Docker Runner、CLI Adapter、流式脱敏
                    |
           Phase 4  规划、Claude→Verifier→Codex、Git/PR/CI
                    |
             SELF_HOSTING_CODEX_APPROVED
                    |
           Phase 5  Linux 发布、验收、回滚和 target guard
                    |
           Phase 6  安全加固、性能、打包升级与完整 DoD
```

Phase 1 与 Phase 2 的纯 UI/fixture 工作可在 Phase 0 合同冻结后并行。Phase 2 Task 2 的真实 Rust↔Python interop、Task 9 和 Task 10 必须等 Phase 1 Task 9 的集成提交；Phase 3 只有在 Phase 1 与 Phase 2 总 Gate 都通过后才进入 ready。其他阶段按依赖推进，任何阶段都不得绕过前置 Gate 直接实现后续副作用。

## 4. 分阶段实施文件

| 阶段 | 详细计划 | 可验收输出 | 原子集成 Gate |
| --- | --- | --- | --- |
| Phase 0 | `2026-08-03-ai-coding-factory-phase-0-foundation-contracts.md` | Monorepo、版本化 schema、JCS/golden vectors、统一检查脚本 | Python/Rust/TS 合同测试和无 C 盘写入基线通过 |
| Phase 1 | `2026-08-03-ai-coding-factory-phase-1-control-plane.md` | 状态机、SQLite/Event Store、Artifact、授权、lease、scheduler、recovery | 状态/耐久/控制/预算 chaos 门禁通过 |
| Phase 2 | `2026-08-03-ai-coding-factory-phase-2-desktop-ipc.md` | Tauri 桌面、托盘、IPC、Command Center、时间线、终端、设置 | UI 崩溃重连、真实 durable cursor、暂停控制 E2E 通过 |
| Phase 3 | `2026-08-03-ai-coding-factory-phase-3-harness-runners.md` | onboarding、WSL/Docker、Claude/Codex Adapter、脱敏、背压、Tool Broker | CLI contract、真实进程、stream durability/performance 通过 |
| Phase 4 | `2026-08-03-ai-coding-factory-phase-4-verification-git.md` | PM/Architect、设计审核、实现/返工、Verifier、Codex review、Git/PR/CI | `CODEX_APPROVED` 自开发闭环及 PR/MERGED 身份链通过 |
| Phase 5 | `2026-08-03-ai-coding-factory-phase-5-deployment-acceptance.md` | ServerProfile、Vault Broker、OCI、SSH/Compose/Nginx/DB、验收和回滚 | staging/production、双 runner fencing、回滚/guard E2E 通过 |
| Phase 6 | `2026-08-03-ai-coding-factory-phase-6-hardening-delivery.md` | 性能、威胁测试、安装升级、备份恢复、runbook、DoD 聚合 | 47/47 流程、11/11 DoD、Codex 最终审核全部 PASS |

## 5. 工作批次与提交纪律

每个详细计划中的任务执行相同步骤：

1. Codex 编排器从最新受保护 `main` 创建独立 task worktree、`factory/phase-<n>-task-<n>-<slug>` 分支和追踪记录；只有 Claude Developer 获得该 worktree 的源码写 capability，分支尚无差异时不得伪造 PR identity。
2. Claude 先写失败测试或可机械失败的合同检查，运行并保存预期失败证据，再实现最小完整行为，补齐中文注释和结构化日志，运行模块测试、受影响集成测试与本地等价门禁，并只精确暂存本 Task 文件形成候选原子提交。
3. Claude 对精确 `base SHA + candidate SHA` 执行冻结 rubric 自审，输出 `claude-self-review.v1` receipt；发现问题由 Claude 自行修复、产生新 SHA 并重新自审，直到 current SHA 为 PASS。自审只能作为作者质量证据，不能替代 Verifier 或 Codex approval。
4. Verifier 在无模型凭据的干净环境检查精确 candidate SHA，运行 `scripts/check.ps1`、秘密/路径扫描和 GitHub Actions 本地等价门禁；失败证据回传 Claude并重新执行步骤 2–4，不能由 Claude/Codex 主观意见替代机械门禁。
5. Codex Reviewer 在独立只读检出审核 RunSpec/验收条件、精确 diff、Claude self-review、Verifier Artifact、中文注释和日志；任何 blocker/high 原样打回 Claude，修复后重新执行步骤 2–5。当前 SHA 通过后才成立 `CODEX_APPROVED`；目标止于此处时不 push、不创建 PR。
6. 当 `target_stage >= PR_READY`，Codex Delivery Orchestrator 才通过 Publisher App push 精确 reviewed SHA 并创建或更新 draft PR；PR body 记录 Task、base/candidate/reviewed SHA、测试命令、Claude self-review/Verifier/Codex receipt digest、风险和恢复说明。Claude 不创建 PR，Publisher App 也不能自行决定交付。
7. Actions receipt 分别绑定 `ciTestMergeSha`、`reviewedHeadSha` 和 base SHA；Codex 审核始终绑定 `reviewedHeadSha`。Phase 4 Task 8 前，Codex 只在签名本地 review receipt 有效后，通过 GitHub plugin 使用预检冻结的 `bootstrapReviewerActorId + credentialRef` 自动提交原生 approval；Task 8 后由独立 Attester App 签发绑定同一 head 的 required `factory/codex-review`，并撤销临时 reviewer credential。check pending/stale、SHA 漂移、未解决会话或 Ruleset 拒绝都不能把 draft PR 晋升为 `PR_READY/MERGED`。
8. 当 `target_stage >= MERGED`，Codex Delivery Orchestrator 才通过 Merger App 发起 `squash` merge，并在请求原子携带 expected reviewed head SHA；expected base SHA 保留为授权/receipt identity，GitHub 服务端 strict/up-to-date Ruleset 必须在 merge 事务中拒绝 base 前移，禁止用客户端先读后写冒充 base CAS。并行 PR 导致 base 前移时，先更新任务分支，再重跑 Claude 自审与无冲突确认、Verifier、Codex、Actions/Check。合并后由 Codex 核对 merge commit parent/tree/SHA，下一依赖 Task 才从新的受保护 `main` 启动；目标止于 `PR_READY` 时绝不合并。
9. 集成台账记录 Claude self-review digest、source/reviewed SHA、Codex delivery actor、PR number/head SHA、Check Run ID、merge SHA 和验证结果；正常过程静默，只向用户报告所选终点完成结果或 blocker。

Claude 必须自审自己的改动，但该 self-review 永远不是独立 approval，也不得创建 PR 或合并；Codex 不得在审核检出中写源码，Codex Delivery Orchestrator 也不得覆盖 Claude 源码或绕过 Reviewer/Verifier。GitHub PR/Actions 不得替代本地确定性验证。禁止把一个阶段的全部改动积压成一次提交，也禁止为了通过集成而跳过失败测试、日志、中文注释、恢复路径或 Ruleset。

## 6. 阶段验收与代表流程归属

`contracts/testing/required-test-catalog.v1.json` 必须逐项保存下表四列。`implementationContributors` 可以跨阶段；`finalPassOwner` 恰好一个；`requiredReplays` 必须全部通过。局部单测、fake Adapter 或未完成跨层场景只能生成唯一 `subcheckId` 和 `qualification=PARTIAL`，不得使用正式测试 ID 生成 PASS。最终 receipt 必须同时包含 `qualification=FINAL`、`finalPassOwner` 和 `scenarioContractDigest`，Phase 6 聚合器按精确集合拒绝别名、缺项、额外项或错误 owner。

| 测试 ID | implementationContributors | finalPassOwner | requiredReplays |
| --- | --- | --- | --- |
| `STAGE-001` | Phase 0/1/2/3/4 | Phase 4 | Phase 6 完整产品 |
| `STAGE-002` | Phase 0/1/2/3/4 | Phase 4 | Phase 6 完整产品 |
| `STAGE-003` | Phase 0/1/2/3/4 | Phase 4 | Phase 6 完整产品 |
| `STAGE-004` | Phase 0/1/2/3/4 | Phase 4 | Phase 6 完整产品 |
| `STAGE-005` | Phase 0/1/2/3/4/5 | Phase 5 | Phase 6 完整产品 |
| `STAGE-006` | Phase 0/1/2/3/4/5 | Phase 5 | Phase 6 完整产品 |
| `AUTH-001` | Phase 0/1/4 | Phase 4 | Phase 6 完整产品 |
| `AUTH-002` | Phase 0/1/4/5 | Phase 5 | Phase 6 完整产品 |
| `PLAN-001` | Phase 0/1/4 | Phase 4 | Phase 6 完整产品 |
| `PLAN-HASH-001` | Phase 0 | Phase 0 | Phase 6 跨语言重放 |
| `BOOT-001` | Phase 4 | Phase 4 | Phase 6 完整产品 |
| `MCP-001` | Phase 0/3 | Phase 3 | Phase 6 完整产品 |
| `STATE-001` | Phase 0/1 | Phase 1 | Phase 6 chaos 重放 |
| `LEASE-001` | Phase 1/4/5 | Phase 5 | Phase 6 双 executor 重放 |
| `PROC-001` | Phase 1/2/3 | Phase 3 | Phase 6 UI 退出重放 |
| `CTRL-001` | Phase 0/1/2/3/4/5/6 | Phase 6 | 全 nodeType 真实重放 |
| `CTRL-002` | Phase 1/2/3/4 | Phase 4 | Phase 6 快速恢复重放 |
| `EVENT-HASH-001` | Phase 0/1 | Phase 1 | Phase 6 跨语言竞争重放 |
| `STREAM-001` | Phase 0/1/3 | Phase 3 | Phase 6 真实 UI 重放 |
| `STREAM-002` | Phase 0/1/3 | Phase 3 | Phase 6 真实 UI 重放 |
| `STREAM-DUR-001` | Phase 0/1/3/6 | Phase 6 | Agent 在全部 claim/object/SQLite 切点崩溃，并重放 WSL/Windows 硬重启 |
| `STREAM-BP-001` | Phase 1/3 | Phase 3 | Phase 6 slow-disk 重放 |
| `STREAM-PERF-BURST` | Phase 0/1/2/3 | Phase 3 | Phase 6 完整产品基准 |
| `STREAM-PERF-SUSTAINED` | Phase 0/1/2/3 | Phase 3 | Phase 6 完整产品基准 |
| `STREAM-PERF-SPARSE` | Phase 0/1/2/3 | Phase 3 | Phase 6 完整产品基准 |
| `REVIEW-001` | Phase 0/3/4 | Phase 4 | Phase 6 完整产品 |
| `GIT-001` | Phase 3/4 | Phase 4 | Phase 6 隔离仓库重放 |
| `SIDEFX-001` | Phase 1/4/5 | Phase 5 | Phase 6 push/PR/registry 重放 |
| `DEPLOY-001` | Phase 5 | Phase 5 | Phase 6 生产等价 Linux |
| `DEPLOY-002` | Phase 5 | Phase 5 | Phase 6 生产等价 Linux |
| `DEPLOY-003` | Phase 1/5 | Phase 5 | Phase 6 生产等价 Linux |
| `GUARD-001` | Phase 1/5 | Phase 5 | Phase 6 三类目标重放 |
| `DEPLOY-FENCE-001` | Phase 1/5 | Phase 5 | Phase 6 双 Release Runner |
| `STORE-001` | Phase 1/6 | Phase 6 | crash 与介质损坏分域 |
| `BUDGET-001` | Phase 1/3/6 | Phase 6 | 跨 boot/睡眠/时钟重放 |
| `RETRY-001` | Phase 1/3/5 | Phase 5 | Phase 6 Provider/SSH 重放 |
| `NOTIFY-001` | Phase 1/2/6 | Phase 6 | UI 已退出的 Agent 通知 |
| `PATH-001` | Phase 0/2/3/6 | Phase 6 | 干净 Windows 文件快照 |
| `CLI-PROFILE-001` | Phase 2/3 | Phase 3 | Phase 6 干净 profile 重放 |
| `PATH-002` | Phase 0/2/6 | Phase 6 | 伪装卷与缺 D 重放 |
| `RUNTIME-001` | Phase 0/2/3 | Phase 3 | Phase 6 兼容矩阵重放 |
| `WSL-IO-001` | Phase 0/3 | Phase 3 | Phase 6 构建/Verifier 重放 |
| `NGINX-001` | Phase 5 | Phase 5 | Phase 6 第三方竞争重放 |
| `BACKUP-001` | Phase 5 | Phase 5 | Phase 6 到期/高风险重放 |
| `DEPLOY-RAM-001` | Phase 5 | Phase 5 | Phase 6 小规格 Linux 重放 |
| `SCHED-PERF-001` | Phase 1/3/4/6 | Phase 6 | 三真实任务单槽基准 |
| `COMPAT-001` | Phase 0/3/6 | Phase 6 | 漂移、回归、晋升演练 |

## 7. 目标终点实现顺序

同一个 `target_stage` 机制从 Phase 1 起建立，不为每个终点复制工作流：

| target_stage | 首次具备真实可用能力的阶段 | 停止谓词 |
| --- | --- | --- |
| `DESIGN_APPROVED` | Phase 4 | 设计 barrier passed、RunSpec/Codex design receipt committed |
| `CODEX_APPROVED` | Phase 4 | 候选 SHA、Claude self-review、Verifier、Codex review 和无 blocking finding 全部一致；没有发生 push/PR |
| `PR_READY` | Phase 4 | Codex 已 push reviewed SHA 并创建 PR；GitHub PR identity、当前 head SHA、Actions 与 `factory/codex-review` Check Run 可核对，尚未 merge |
| `MERGED` | Phase 4 | GitHub Ruleset、required checks、会话解决和审核 SHA 均通过，Codex 发起的远端 merge SHA 可核对 |
| `STAGING_ACCEPTED` | Phase 5 | 同一镜像 digest 在 staging 通过全部 blocking AcceptancePlan |
| `PRODUCTION_ACCEPTED` | Phase 5 | 生产验收、soak、Codex evidence review 和 release receipt committed |

用户创建任务时选择做到哪一步；产品本身仍按 Phase 0–6 完整实现，不能因默认选择较早阶段而删除后半程代码或门禁。

## 8. 统一测试命令

`scripts/dev.ps1` 在同一子进程内设置并复核全部 D 盘缓存、临时、构建、WebView 和测试 Artifact 路径，然后执行 `--` 后的命令；子进程路径快照发现可控 C 盘写入时返回非零。除 Task 1 创建该 wrapper 的安全引导步骤外，本套计划出现的任何 `python`、`pnpm`、`cargo` 或子 PowerShell 命令都必须通过它执行，不能依赖父终端已有环境。详细计划可以缩小测试范围，但每个阶段集成提交必须运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Suite unit,contract,integration
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/agent -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- pnpm --dir apps/desktop test
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- cargo test --workspace
```

Phase 2 起增加：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- pnpm --dir apps/desktop test:e2e:web
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- pnpm --dir apps/desktop test:e2e:tauri
```

Phase 3 起增加：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Suite cli-contract,runner,stream,performance
```

Phase 5 起增加：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Suite release-e2e,rollback,chaos
```

Phase 6 最终门禁：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1 -Suite all -EmitReceipts
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -ExecutionPolicy Bypass -File scripts/package.ps1 -Configuration Release
```

每条命令必须把机器可读结果写到 D 盘测试 Artifact 根；测试报告只保存脱敏内容并绑定 Compatibility Manifest digest。

## 9. 日志、注释和证据门禁

- Python 使用统一结构化 logger；Rust 使用 `tracing`；TypeScript 只通过桌面日志适配器，不散落 `console.log`。
- 入口、状态转换、外部调用、tool、模型、Git、部署、授权拒绝、重试和恢复都记录 request/task/run/step/attempt/trace 身份及耗时。
- 所有公共模块、类、服务入口、IPC 命令、状态机、Adapter、存储、恢复和发布逻辑必须有中文注释，解释职责、副作用、边界与失败语义。
- 成功状态只能由领域事务、远端事实或 committed Artifact/receipt 推导；UI 文案、模型摘要和普通日志不能满足 Gate。
- 每个阶段的最后一个任务都必须运行“中文注释扫描 + 日志事件清单 + test ID coverage”审查。

## 10. 安全与外部依赖处理

- 本计划不读取或复制 `%USERPROFILE%\.claude`、`%USERPROFILE%\.codex` 凭据；首次接入只引导用户在 D-backed profile 登录。
- GitHub 使用分离身份：Codex Delivery Orchestrator 只能短期调用 Publisher/Merger App，该 App 只持 metadata 只读与按阶段求交的 Contents/PR 写权限，明确没有 Checks 写；Review Attester App 只持 Contents 只读和 Checks 写，明确没有 Contents/PR/merge 写。Ruleset 把 `factory/codex-review` 同时绑定固定 check name 与 expected Attester App ID。仓库创建/Ruleset 管理使用第三份独立、一次性 bootstrap admin 授权，完成后撤销。PAT/SSH/device flow 仅作显式兼容路径；未安装 Attester App 时最高只能停在过渡门禁允许的阶段或 fail closed，SSH/PAT 不能伪装成 Checks 发布能力。任何凭据都不能从默认 C 盘读取或写入仓库、Actions 日志、PR 评论和 receipt。
- Docker backend/integration、WSL distro 导入、删除或迁移属于用户级全局变更；实现向导和 dry-run receipt，但只有用户明确授权后才执行。
- 真实生产等价 Linux 验收需要用户提供可销毁 ServerProfile 或临时云服务器；未提供时该环境显示“未认证”，不能伪造 `DEPLOY-001..003` PASS。
- Windows 代码签名证书不可用时只生成未签名测试包/便携包并明确警告；不能把它登记为签名发布。
- 所有 destructive/release/database 操作在详细计划中必须包含身份解析、备份/回滚、授权和 receipt 步骤。

## 11. 完成判定

计划实施完成必须同时满足：

- [ ] Phase 0–6 的详细计划任务全部有本地原子提交和集成映射。
- [ ] 六种 target stage 均能在真实工作流中精确停止，权限不越界。
- [ ] 47 个展开后的 §20.2 测试 ID 均有 PASS receipt，且环境 manifest digest 一致。
- [ ] 11 项 Definition of Done 全部 PASS；无 skipped 必检项、冲突或手工改成功。
- [ ] Codex 对完整源码、安装包和证据做最终只读审核，无 open blocker/high。
- [ ] GitHub 公开仓库、基础/强化 Ruleset、Claude self-review、由 Codex 创建的逐 Task PR、固定 required checks、`factory/codex-review` SHA 绑定和由 Codex 发起的远端 merge identity 均有可复核 receipt；不存在直接推送受保护 `main` 的未关闭例外。
- [ ] Windows 安装、关闭 UI 后 Agent 继续、暂停恢复、Linux 发布、验收和回滚均有真实运行证据。
- [ ] 源码、运行数据、日志、缓存、profile、WSL/Docker 数据和备份的可控写入均位于 `D:\codex项目`。
- [ ] 文档、威胁模型、事件协议、接入手册、备份恢复、故障排查、发布回滚和升级 Runbook 与实现一致。

## 12. 执行入口

正式实施前先由 Codex 执行 §1.4 的 GitHub 只读能力预检；当前已冻结 owner `geqian236`、仓库名 `AI-Coding-Factory` 和公开可见性，由 Codex 创建空远端、推送已核对基准、启用基础 Ruleset，并创建第一张文档 PR 合并 Master Spec 与本计划。该步骤需要实施当次的明确外部变更授权，预检或保护失败不得留下已推送但未保护的仓库。之后从 Phase 0 开始，按详细计划逐 Task 由 Claude 实现并自审、Verifier 检查、Codex 独立审核、创建 PR 并在目标阶段允许时合并。Phase 1 与 Phase 2 只按 §3 的任务级依赖并行；其余阶段只有前置集成 Gate 通过后才进入 ready 状态。任何实现发现需要改变 target stage、生产授权、信任边界、凭据、发布或回滚保证时，先回到 Master Spec 做变更审查，不能只在代码或本计划中悄悄放宽。
