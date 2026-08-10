# AI Coding Factory Phase 4 Verification and Git Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 PM/Architect 规划、Codex 设计审核、Claude 开发、确定性 Verifier、Codex 代码审核、自动修复/重规划、Git Object Bridge、PR/CI/merge，并让四个前半程 target stage 精确停止。

**Architecture:** RunSpec/PlanRevision 是不可变计划源；Claude 只写任务 worktree，Verifier 不使用模型，Codex 在独立只读检出审核。Windows 用户仓库与 WSL worktree 只通过带 digest 的 Git object bundle/pack 交换，候选/ref 更新和远端副作用均走授权、CAS 与 receipt。

**Tech Stack:** Python 3.12、Git plumbing/bundle/pack、Claude/Codex CLI、Docker Verifier、GitHub Forge Adapter（首个认证实现）、pytest/chaos、OCI buildx 前置接口。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文短命令只表示 wrapper 参数。

---

## Task 1: 实现 Windows→WSL Git Object Bridge 入站

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/git_bridge/manifest.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/windows_export.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/wsl_import.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/commands.py`
- Create: `contracts/schemas/git-object-manifest.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/integration/git_bridge/test_inbound_bridge.py`
- Test: `tests/security/test_git_source_pollution.py`

- [ ] 写含未 push base commit、dirty worktree/index、linked worktree、可达性失败、bundle digest 漂移、source repo 锁，以及当前 repo physical identity/规范路径/base SHA 与 `TaskIntakeAccepted` binding 漂移测试。
- [ ] Windows adapter 使用 `GIT_OPTIONAL_LOCKS=0` 和只读 plumbing；导出前重新核对当前 repo physical identity、规范路径和 base SHA 与 `TaskIntakeAccepted` binding 完全一致，再导出精确 base SHA，不写用户 index/worktree/branch/ref/config，任何 TOCTOU 漂移 fail closed。
- [ ] WSL 导入 Factory bare mirror，验证 repo identity、base 可达性和 bundle digest，再从 ext4 mirror 创建任务 worktree。
- [ ] 在同一 Task 登记 Git object schema 分类，运行普通 codegen 和 `--check`；比较操作前后 status/index/HEAD/refs/config，Expected: 生成合同一致且用户仓库零污染。
- [ ] 提交：`feat(git): import exact base through object bridge`。

## Task 2: 实现新仓库安全 bootstrap 和 workspace identity

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/git_bridge/bootstrap.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/workspace_service.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/workspace_repository.py`
- Test: `tests/integration/git_bridge/test_bootstrap.py`
- Test: `tests/integration/git_bridge/test_workspace_recovery.py`

- [ ] 写非空目录、已有 `.git`、路径身份未知、与 `TaskIntakeAccepted` 的规范路径/physical identity 漂移、并发 bootstrap 和崩溃中途测试。
- [ ] 只允许选定 D 盘空目录，创建 `.gitignore`、默认分支和可审计空提交；任何不确定情况 BLOCKED，不覆盖。
- [ ] `workspaces` 持久化两端 repo identity、bundle/base/candidate/checkpoint ref、lease 和清理状态，恢复不按目录存在性猜测。
- [ ] 运行 `BOOT-001`，Expected: fail-closed 场景均无用户文件变化。
- [ ] 提交：`feat(git): add safe repository bootstrap and workspace identity`。

## Task 3: 实现 PM/Architect 双角色规划和 RunSpec 构建

**Files:**

- Create: `apps/agent/src/factory_agent/harness/planning/pm.py`
- Create: `apps/agent/src/factory_agent/harness/planning/architect.py`
- Create: `apps/agent/src/factory_agent/harness/planning/run_spec_builder.py`
- Create: `apps/agent/src/factory_agent/harness/planning/repository_facts.py`
- Create: `apps/agent/src/factory_agent/prompts/project_manager.md`
- Create: `apps/agent/src/factory_agent/prompts/architect.md`
- Create: `contracts/schemas/pm-plan.v1.schema.json`
- Create: `contracts/schemas/architect-plan.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/agent/unit/harness/planning/test_pm.py`
- Test: `tests/agent/unit/harness/planning/test_architect.py`
- Test: `tests/agent/unit/harness/planning/test_run_spec_builder.py`
- Test: `tests/integration/planning/test_run_spec.py`

- [ ] 写两个角色 artifact 缺失、仓库事实漂移、未知 nodeType、DAG/barrier 不完整、capability 越界和 hash 漂移测试。
- [ ] PM 输出目标/范围/假设/验收，Architect 输出文件/节点/barrier/风险；两者可共享模型资源但 schema、Artifact、事件独立。
- [ ] RunSpec builder 机械生成 node capability、barrier identity、semantic/revision digest；PlanRevision 写入后不可修改。
- [ ] 在同一 Task 登记规划 schema 分类，运行普通 codegen 和 `--check`；再运行 `PLAN-001` 和 `PLAN-HASH-001` 实际规划路径，Expected: PASS。
- [ ] 提交：`feat(planning): add evidence-backed PM and architect plans`。

## Task 4: 实现 Codex 设计审核 Gate

**Files:**

- Create: `apps/agent/src/factory_agent/harness/review/design_reviewer.py`
- Create: `apps/agent/src/factory_agent/harness/review/findings.py`
- Create: `apps/agent/src/factory_agent/harness/review/rubrics.py`
- Create: `apps/agent/src/factory_agent/prompts/codex_design_review.md`
- Create: `contracts/fixtures/rubrics/design-review.v1.json`
- Test: `tests/integration/review/test_design_gate.py`

- [ ] 写 illegal schema、base 漂移、无证据 finding、blocking finding 未关闭和 review digest/SHA 漂移测试。
- [ ] Codex 只读审核仓库事实、RunSpec 和冻结 rubric；finding 必须可复核，Policy Engine 决定 Gate，不由模型直接写成功状态。
- [ ] `DESIGN_APPROVED` 与 PlanRevision/review receipt/Artifact 同事务推进；达到该 target 时无活动 Attempt 后终结。
- [ ] 运行设计正反例，Expected: 达到 `STAGE-001` 与 `AUTH-001` 的完整 scenario contract 时，由 Phase 4 final owner 签发 `qualification=FINAL`；缺任一跨层证据只能签 PARTIAL。
- [ ] 提交：`feat(review): add deterministic Codex design gate`。

## Task 5: 实现 Deterministic Verifier 和 Artifact 证据

**Files:**

- Create: `apps/agent/src/factory_agent/harness/verification/command_catalog.py`
- Create: `apps/agent/src/factory_agent/harness/verification/verifier.py`
- Create: `apps/agent/src/factory_agent/harness/verification/gate_evaluator.py`
- Create: `apps/agent/src/factory_agent/harness/verification/artifacts.py`
- Create: `apps/agent/src/factory_agent/harness/verification/comment_policy.py`
- Create: `apps/agent/src/factory_agent/harness/verification/logging_policy.py`
- Create: `apps/agent/src/factory_agent/harness/verification/languages/python_ast.py`
- Create: `apps/agent/src/factory_agent/harness/verification/languages/typescript_ast.py`
- Create: `apps/agent/src/factory_agent/harness/verification/languages/rust_ast.py`
- Create: `apps/agent/src/factory_agent/harness/verification/languages/fallback.py`
- Modify: `runners/images/verifier/Dockerfile`
- Create: `contracts/schemas/verifier-report.v1.schema.json`
- Create: `contracts/policies/comment-policy.v1.json`
- Create: `contracts/policies/logging-policy.v1.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/integration/verifier/test_command_catalog.py`
- Test: `tests/integration/verifier/test_gate_evaluator.py`
- Test: `tests/integration/verifier/test_artifacts.py`
- Test: `tests/security/test_verifier_isolation.py`
- Test: `tests/integration/verifier/test_comment_policy.py`
- Test: `tests/integration/verifier/test_logging_policy.py`

- [ ] 写未知命令、命令注入、超时、缺报告、exit 0 但 Artifact 缺失、候选 SHA 漂移、网络越界，以及公共 API/IPC/状态机缺中文注释或结构化日志的失败测试。
- [ ] Verifier 只执行仓库已有命令或 RunSpec 冻结的等价 gate；版本化 AST checker 输出 rule ID、symbol、evidence 和 pass condition。未知语言只能执行 RunSpec 冻结 fallback，禁止 grep 或 Codex 主观意见冒充机械结论。
- [ ] Verifier 无模型/生产凭据；Artifact durable COMMIT 后才允许 Step 成功。
- [ ] 在同一 Task 登记 verifier schema/policy 分类，运行普通 codegen 和 `--check`；再运行 verifier integration/security，Expected: 三语言合同一致且确定性 gate 可复现。
- [ ] 提交：`feat(verification): add isolated deterministic verifier`。

## Task 6: 实现 Claude 编码与自审→Verifier→Codex 独立审核及自动修复

**Files:**

- Create: `apps/agent/src/factory_agent/harness/workflows/implementation.py`
- Create: `apps/agent/src/factory_agent/harness/workflows/code_review.py`
- Create: `apps/agent/src/factory_agent/harness/workflows/repair_loop.py`
- Create: `apps/agent/src/factory_agent/prompts/claude_implement.md`
- Create: `apps/agent/src/factory_agent/prompts/claude_self_review.md`
- Create: `apps/agent/src/factory_agent/prompts/claude_repair.md`
- Create: `apps/agent/src/factory_agent/prompts/codex_code_review.md`
- Create: `contracts/fixtures/rubrics/code-review.v1.json`
- Test: `tests/integration/workflows/test_code_review_loop.py`
- Test: `tests/chaos/test_review_sha_drift.py`

- [ ] 写 Claude 实现失败、自审发现问题后自修复、自审 receipt 缺失/伪造/绑定旧 SHA、Verifier finding、Codex reject、Claude 自审企图关闭 Codex finding、相同失败签名三次、六轮预算、审核后 SHA 漂移和 provider quota 测试。
- [ ] Claude 是任务 worktree 的唯一源码写者并产生候选 hidden ref/commit；它必须按 `claude-self-review.v1` 对精确 candidate SHA 自审并修复到 PASS，随后 Verifier 运行机械门禁，最后 Codex 在独立只读检出审核精确 candidate SHA、self-review 与 Verifier 证据。Claude self-review 不是独立 approval，不得关闭 Codex finding；Codex 不得直接修改源码。中文注释/日志 finding 必须引用 checker 的 rule ID、symbol 和 pass condition，任何修改使旧 self-review/Verifier/Codex receipt 及对应 GitHub Check Run 失效并回到 Claude 修复。
- [ ] 修复预算按 Run 累计不重置，PlanRevision 上限独立；范围内重规划自动继续，越授权则 ACTION_REQUIRED。
- [ ] 运行 `REVIEW-001`，Expected: finding 闭环、漂移重跑、预算耗尽状态正确。
- [ ] 提交：`feat(workflow): add Claude verifier Codex repair loop`。

## Task 7: 实现候选回传、CAS ref 和 checkpoint 清理

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/git_bridge/candidate_export.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/ref_publish.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/cleanup.py`
- Modify: `apps/agent/src/factory_agent/integrations/git_bridge/checkpoint.py`
- Test: `tests/integration/git_bridge/test_candidate_roundtrip.py`
- Test: `tests/chaos/test_git_bridge_recovery.py`

- [ ] 写候选 digest/base mismatch、source ref CAS 失败、重复导入、崩溃补录、checkpoint 未跟踪文件和保留期测试。
- [ ] WSL candidate 通过 bundle/pack 导入 Factory bare repo；仅授权交付时 CAS 更新 `refs/factory/tasks/{task_id}/candidate`，不触碰用户 index/worktree。每个 Git 请求边界按 `node-pause-policy.v1` 作为 safe point；请求已发送则先 settle/reconcile receipt，再响应暂停。
- [ ] 保留最终 candidate ref；临时 checkpoint/worktree 清理先证明无运行/Artifact/receipt 引用并写 tombstone。
- [ ] 完成完整对象往返 identity 检查，Expected: 生成 `GIT-001` 本地桥接 subcheck，`qualification=PARTIAL`；真实远端 final 在 Task 8 签发。
- [ ] 提交：`feat(git): publish candidate refs without workspace pollution`。

## Task 8: 实现 GitHub PR、CI、merge 和副作用核对

**Files:**

- Create: `apps/agent/src/factory_agent/ports/forge.py`
- Create: `apps/agent/src/factory_agent/integrations/forge/github.py`
- Create: `apps/agent/src/factory_agent/integrations/forge/github_review_attester.py`
- Create: `apps/agent/src/factory_agent/integrations/forge/credential_session.py`
- Create: `apps/agent/src/factory_agent/application/publication_service.py`
- Create: `apps/agent/src/factory_agent/domain/forge_profiles.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/forge_profile_repository.py`
- Create: `apps/desktop/src/features/settings/ForgeProfiles.tsx`
- Create: `contracts/schemas/forge-receipt.v1.schema.json`
- Create: `contracts/schemas/forge-profile.v1.schema.json`
- Create: `contracts/schemas/github-repository-policy.v1.schema.json`
- Create: `contracts/schemas/review-attestation.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/integration/forge/test_github_adapter.py`
- Test: `tests/integration/forge/test_github_ruleset.py`
- Test: `tests/integration/forge/test_github_review_attester.py`
- Test: `tests/chaos/test_forge_response_loss.py`
- Test: `tests/security/test_forge_credentials.py`

- [ ] 写未经 Codex Delivery Orchestrator 授权的 push/PR/merge、Claude 直接创建 PR/合并、缺失/过期/撤销 credential、默认 C 盘凭据误用、PR 响应丢失、CI pending/fail、`reviewedHeadSha` 与 `ciTestMergeSha` 混淆、required workflow 未触发、`factory/codex-review` 缺失/错误 source App/绑定旧 SHA、reviewed head/base 漂移、Ruleset/会话解决拒绝、merge expected-head CAS 失败、merge 响应丢失和错误 parent/tree/merge SHA 测试。
- [ ] 首版只认证 GitHub Adapter；核对 Phase 0 已冻结的 stage/node capability maps 包含 `pr.update/forge.observe.scoped/check.publish` 与 `ATTEST_REVIEW`，并运行三语言 codegen `--check`；缺失时 fail closed，禁止在本 Task 临时补合同。Publisher/Merger App 只有 metadata 只读与按阶段求交的 Contents/PR 写，Review Attester App 只有 Contents 只读与 Checks 写，Ruleset 将 check 绑定 expected Attester App ID，并冻结 required checks strict/up-to-date，使 base 前移在 GitHub merge 事务中被拒绝；两者 credentialRef、scope、TTL、fingerprint 分开保存。仓库创建/Ruleset 管理由总计划 §1.4 的一次性 bootstrap admin 承担。PAT/SSH/device flow 不能冒充 Checks 能力；其他 Forge 返回 `UNSUPPORTED_OR_UNCERTIFIED`。
- [ ] push/PR/Check Run/merge 使用短期 Broker session、稳定 idempotency identity 和 inspect；`PUBLISH_PR/MERGE` 的 actor 必须是 Codex Delivery Orchestrator，Claude/Verifier/Reviewer Runner 均无该 capability。完成事实绑定 GitHub repository ID、PR number、base/candidate/reviewed/current-head/remote/merge SHA、`ciTestMergeSha`、Actions run/check IDs、expected source App ID、Claude self-review、Verifier 与 Codex receipt/rubric digest。只有 current receipts 有效且 `target_stage >= PR_READY` 时 Codex 才创建 draft PR；独立 Attester 发布 head-bound check，再由 Codex 用 `pr.update` 标 ready。只有 `target_stage >= MERGED` 时 Codex 才发起固定 `squash` merge，请求原子携带 expected reviewed head，服务端 strict/up-to-date Ruleset 负责拒绝 base 前移；base 漂移后必须更新分支并重跑 Claude 自审/Verifier/Codex/Actions/Attestation，成功后由 Codex 验证 merge parent/tree/SHA。每个外部请求遵守 pause policy，已发送请求必须 settle/reconcile 后才暂停。
- [ ] Phase 4 Task 8 自身及之前 PR 由 Codex 创建；预检冻结与 Publisher/Merger 分离、可对 App-authored PR 提交有效 approval 的 `bootstrapReviewerActorId + credentialRef`，Codex 只在签名本地 review receipt 有效后通过 GitHub plugin 自动提交 approval。本 Task 合并后安装 Attester、发布探测 check、把 `factory/codex-review + expected App ID` 加入 Ruleset并生成升级 receipt，随后撤销 bootstrap reviewer credential；此后 required check 取代过渡 approval且禁止回退。Actions `pull_request` receipt 记录 test-merge SHA 与 head/base SHA；required 聚合 job 始终产出，首版不启用 merge queue。
- [ ] 在同一 Task 登记 Forge schema 分类，运行普通 codegen 和 `--check`；再在真实隔离仓库运行 `GIT-001`、`STAGE-003..004` FINAL。`SIDEFX-001` 只生成 Forge subcheck，待 Phase 5 签发；secret scan 必须为 0。
- [ ] 分两次提交：`feat(forge): add GitHub PR and CI identity adapter`、`feat(forge): add protected merge reconciliation`。

## Task 9: 实现 target-stage 精确停止和自用纵切

**Files:**

- Create: `apps/agent/src/factory_agent/harness/workflows/stage_pipeline.py`
- Create: `tests/e2e/test_target_stages.py`
- Create: `scripts/test-self-hosting-code-review.ps1`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/ipc/handlers.py`
- Modify: `apps/agent/src/factory_agent/scheduler/dispatcher.py`
- Modify: `apps/desktop/src/app/router.tsx`
- Modify: `apps/desktop/src/features/settings/SettingsPage.tsx`

- [ ] 为 `DESIGN_APPROVED/CODEX_APPROVED/PR_READY/MERGED` 写正例和高一级 capability 拒绝测试。
- [ ] stage pipeline 由 barrier 和 capability catalog 决定 required nodes，不用条件散落在 Adapter。
- [ ] 先运行 `generate.py --check` 复核 Tasks 1/3/5/8 已原子提交的 catalog/generated；随后将 planning/review/Git/Forge/stage pipeline 注册到 Agent composition root、IPC、dispatcher 和桌面设置 route，使用该真实组合根开发隔离示例仓库，关闭/重连 UI 后证据仍一致，禁止测试专用装配。
- [ ] 执行快速 `PAUSE→RESUME` 全场景：旧 executor 只可完成 tool/checkpoint/termination 收尾，新派发必须取得新 lease/fencing token/Attempt/授权；checkpoint 保留与陈旧 executor 拒绝全部通过后由 Phase 4 owner 签 `CTRL-002` FINAL。达到 `SELF_HOSTING_CODEX_APPROVED` 时另生成内部 milestone receipt，明确它不是首版交付。
- [ ] 提交：`test(e2e): certify self-hosting code review milestone`。

## Task 10: Phase 4 总门禁

**Files:**

- Create: `docs/architecture/ai-review-loop.md`
- Create: `docs/protocols/git-object-bridge-v1.md`
- Create: `docs/operations/git-pr-ci.md`
- Modify: `scripts/test.ps1`
- Test: `tests/e2e/test_composed_ai_delivery.py`

- [ ] 对 Task 9 的 codegen 结果运行 `generate.py --check`；从已冻结的真实组合根创建任务穿过 Claude 实现与自审→Verifier→Codex 独立审核→Codex 创建 PR/合并，再运行 contracts、Agent、Runner、Verifier、Git Bridge、Forge、chaos 和 desktop E2E，不得在 Gate 改业务接线。
- [ ] 冻结 candidate/reviewed SHA、Claude self-review、Verifier、Codex review、Codex PR/merge actor 和全部 receipts；secret scan、中文注释和日志覆盖无 blocker。
- [ ] 独立需求审查验证 `PLAN-001`、`BOOT-001`、`REVIEW-001`、`GIT-001`、`SIDEFX-001` 及 `STAGE-001..004`，并核验 Task 9 的 `CTRL-002` FINAL 与当前 source/codegen digest 一致。
- [ ] Codex 对完整前半程做只读审核，无 blocker/high 后才承认内部自用里程碑。
- [ ] 提交：`test(workflow): certify local AI development and Git delivery`。

Phase 4 结束后系统已经可日常用于开发自身，但完整交付仍必须继续实现 staging/production 发布、验收、回滚、打包和最终 DoD。
