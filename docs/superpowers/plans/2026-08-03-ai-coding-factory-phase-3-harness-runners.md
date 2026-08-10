# AI Coding Factory Phase 3 Harness and Runners Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入 Factory 专用 WSL2/Docker 执行面、Claude/Codex 结构化 CLI、真实进程监管、流式组帧脱敏、Tool Broker、背压与暂停恢复，使桌面终端展示真实且可复核的运行证据。

**Architecture:** Windows Agent 只控制 Rust WSL Broker；Broker 通过 Docker Engine identity 管理容器/exec/process set。Provider Adapter 只产生 `PreparedEventV2`，经过组帧、脱敏和 Phase 1 durable writer 后才进入 UI。Runner 镜像按 Claude、Codex、Verifier、Release 分离信任区。

**Tech Stack:** Python 3.12、Rust、WSL2、Docker Desktop/Engine API、Dockerfile、JSONL、xterm event model、pytest/chaos、固定 provider fixtures。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文短命令只表示 wrapper 参数。

---

## Task 1: 实现本地 runtime 只读探测和分类

**Files:**

- Create: `apps/agent/src/factory_agent/onboarding/runtime_probe.py`
- Create: `apps/agent/src/factory_agent/onboarding/runtime_classifier.py`
- Create: `apps/agent/src/factory_agent/onboarding/cli_profiles.py`
- Create: `apps/agent/src/factory_agent/onboarding/receipts.py`
- Create: `apps/agent/src/factory_agent/onboarding/wsl_provisioner.py`
- Test: `tests/agent/unit/onboarding/test_runtime_classifier.py`
- Test: `tests/agent/unit/onboarding/test_cli_profiles.py`
- Test: `tests/integration/onboarding/test_runtime_probe.py`

- [ ] 写 WSL2 simplified、legacy dual-distro、Hyper-V、Windows containers、未知布局、integration 关闭和数据路径不合规测试。
- [ ] 实现只读 probe；不得仅凭 Docker 版本或 `docker-desktop-data` 是否存在推断布局，不读取 C 盘 profile 内容。
- [ ] 实现授权后的 Factory distro create/import port；dry-run 生成影响/备份/receipt，取消授权时零变更。
- [ ] 运行 `python -m pytest tests/agent/unit/onboarding tests/integration/onboarding -q`，Expected: `RUNTIME-001` 分类 PASS。
- [ ] 提交：`feat(onboarding): add fail-closed local runtime classification`。

## Task 2: 实现 Rust WSL Broker 和 Docker process identity

**Files:**

- Create: `crates/factory-wsl-broker/Cargo.toml`
- Modify: `Cargo.lock`
- Create: `crates/factory-wsl-broker/src/main.rs`
- Create: `crates/factory-wsl-broker/src/protocol.rs`
- Create: `crates/factory-wsl-broker/src/docker_engine.rs`
- Create: `crates/factory-wsl-broker/src/process_identity.rs`
- Create: `crates/factory-wsl-broker/src/runner_policy.rs`
- Create: `crates/factory-wsl-broker/src/lifecycle.rs`
- Create: `crates/factory-wsl-broker/src/git_objects.rs`
- Create: `apps/agent/src/factory_agent/integrations/wsl/runner_client.py`
- Create: `apps/agent/src/factory_agent/integrations/windows/managed_process.py`
- Modify: `apps/agent/src/factory_agent/application/shutdown_service.py`
- Test: `crates/factory-wsl-broker/tests/runner_identity.rs`
- Test: `tests/integration/wsl/test_broker_reconnect.py`
- Test: `tests/integration/wsl/test_shutdown_fast_broker.py`

- [ ] 写 `containerId + execId + processStartIdentity`、Broker 被杀后 inspect、interrupt/kill 幂等、process set 未清空、identity 漂移和 `SHUTDOWN_FAST` 0–2 秒 Broker 收尾测试。
- [ ] 实现 versioned `capabilities/start/inspect/interrupt/kill/parse/resume` 合同；Job Object 只监管 Windows Broker，不作为容器退出证据。
- [ ] Runner 结束条件必须同时满足 Docker inspect/exec inspect 和受管 process set 清空；无法证明时返回 LOST/RECONCILING。
- [ ] 运行 `powershell -NoProfile -File scripts/spikes/test-runner-identity.ps1`，Expected: PASS。
- [ ] 提交：`feat(runner): add inspectable WSL Docker broker`。

## Task 3: 构建隔离 Runner 镜像和 mount/network policy

**Files:**

- Create: `runners/images/base/Dockerfile`
- Create: `runners/images/claude/Dockerfile`
- Create: `runners/images/claude/entrypoint.sh`
- Create: `runners/images/claude/policy.json`
- Create: `runners/images/codex/Dockerfile`
- Create: `runners/images/codex/entrypoint.sh`
- Create: `runners/images/codex/policy.json`
- Create: `runners/images/verifier/Dockerfile`
- Create: `runners/images/verifier/entrypoint.sh`
- Create: `runners/images/verifier/policy.json`
- Create: `runners/images/release/Dockerfile`
- Create: `runners/images/release/entrypoint.sh`
- Create: `runners/images/release/policy.json`
- Create: `runners/images/buildkit/Dockerfile`
- Create: `runners/images/buildkit/policy.json`
- Create: `runners/shim/Cargo.toml`
- Modify: `Cargo.lock`
- Create: `runners/shim/src/main.rs`
- Create: `runners/protocol/README.md`
- Create: `crates/factory-wsl-broker/src/buildkit.rs`
- Modify: `crates/factory-wsl-broker/src/main.rs`
- Modify: `crates/factory-wsl-broker/src/protocol.rs`
- Modify: `crates/factory-wsl-broker/src/runner_policy.rs`
- Modify: `crates/factory-wsl-broker/src/docker_engine.rs`
- Create: `apps/agent/src/factory_agent/ports/build.py`
- Create: `apps/agent/src/factory_agent/integrations/buildkit/broker.py`
- Create: `contracts/schemas/build-request.v1.schema.json`
- Create: `contracts/policies/build-policy.v1.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `scripts/test-wsl-runner-e2e.ps1`
- Test: `tests/integration/wsl/test_runner_policy.py`
- Test: `tests/security/test_runner_isolation.py`
- Test: `tests/security/test_build_broker.py`

- [ ] 写错误 profile mount、`/mnt/d` worktree、Docker socket、云元数据、跨 Task volume、超 scope 网络、Release secret 进入模型 Runner，以及恶意 host mount/secret/host network Dockerfile 的失败测试。
- [ ] 实现 ext4 worktree、只需 profile、最小网络、CPU/RAM/timeout、非 root 用户和受信进程组 shim；镜像以 digest 固定。受信 Build Broker 只接受绑定 candidate SHA/context digest/platform/network/secret scope 的结构化请求，使用独立 rootless/dedicated BuildKit，cache/volume 位于 D-backed VHDX；把 BuildKit capability 与请求分支接入 Broker `protocol.rs` 和 `main.rs`，未接线或未知版本必须 fail closed。
- [ ] Verifier 无 Docker socket、模型或生产凭据；Broker 在执行前静态扫描不可信 Dockerfile/Compose 并拒绝逃逸字段；Codex 只读独立检出，Release 无源码写权限和模型 profile。安全测试必须启动真实 Broker 二进制并走版本化 BuildKit dispatch/inspect/reconcile，禁止只直接导入 Python 类。
- [ ] 在同一 Task 登记 generated/runtime-only 分类，运行普通 codegen 和 `--check`；再启动真实 Broker 执行 `scripts/test-wsl-runner-e2e.ps1 -Mode Isolation`，Expected: BuildRequest 三语言绑定可编译，`WSL-IO-001` 与安全隔离 PASS。
- [ ] 提交：`feat(runners): add capability-scoped container images`。

## Task 4: 实现 frame assembler、streaming redactor 和 Provider event adapter

**Files:**

- Create: `apps/agent/src/factory_agent/harness/frame_assembler.py`
- Create: `apps/agent/src/factory_agent/harness/streaming_redactor.py`
- Create: `apps/agent/src/factory_agent/harness/provider_events.py`
- Create: `apps/agent/src/factory_agent/harness/adapters/base.py`
- Create: `contracts/schemas/provider-frame.v1.schema.json`
- Create: `contracts/policies/provider-frame-limits.v1.json`
- Create: `contracts/policies/stream-redaction-policy.v1.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/contract/providers/test_frame_assembler.py`
- Test: `tests/security/test_streaming_redactor.py`

- [ ] 写断行、重复、乱序、跨 chunk secret、慢但合法 frame、超限、截断、永不闭合 frame、长度敏感 redaction、lookbehind 上限和 uncertain 终态测试。
- [ ] 分别冻结 `maxFrameBytes/frameAssemblyTimeoutMs` 与 redactor 的 500 ms 决策/lookbehind 参数。已证安全前缀立即输出；不确定后缀只继续保留、终态保守 `[REDACTED:uncertain]` 或 fail closed，不能向未闭合 JSON 注入 marker 后继续解析，超时不得原样释放。
- [ ] 同时维护内存 `sourceTransportSpan` 与持久化 `sanitizedStreamSpan`；credential 长度默认只到 field/frame boundary。
- [ ] 在同一 Task 登记合同分类，运行普通 codegen 和 `--check`；再运行 Provider/redactor 测试，Expected: 三语言生成树一致，未脱敏原文落盘为 0。
- [ ] 提交：`feat(stream): add bounded fail-closed provider redaction`。

## Task 5: 实现 Claude Developer Adapter

**Files:**

- Create: `apps/agent/src/factory_agent/harness/adapters/claude.py`
- Create: `contracts/schemas/providers/claude/v1/event.schema.json`
- Create: `contracts/schemas/providers/claude/v1/termination.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `tests/fixtures/providers/claude/valid.jsonl`
- Create: `tests/fixtures/providers/claude/truncated.jsonl`
- Create: `tests/fixtures/providers/claude/exit-zero-missing-termination.jsonl`
- Create: `scripts/certify-provider.ps1`
- Test: `tests/contract/providers/test_claude_adapter.py`

- [ ] 录制脱敏 fixture：正常结束、工具调用、公开 summary、无 summary、截断、exit 0 缺 termination、source sequence gap、版本漂移。
- [ ] 实现锁定 CLI structured non-interactive 模式和 D-backed `CLAUDE_CONFIG_DIR`；未登录只返回 onboarding blocker，不读取默认 profile。
- [ ] exit 0 只有 schema 完整、termination frame、source watermark 和必需 Artifact 全部满足才成功。
- [ ] 在同一 Task 登记 Provider schema 分类，运行普通 codegen 和 `--check`；再运行 fake fixtures 与最小真实任务认证，Expected: 生成 `STREAM-001..002`、`CLI-PROFILE-001` 的 Claude Adapter PARTIAL subcheck，最终由 Phase 3 Gate 组合签发。
- [ ] 提交：`feat(harness): add certified Claude developer adapter`。

## Task 6: 实现 Codex 只读 Reviewer Adapter

**Files:**

- Create: `apps/agent/src/factory_agent/harness/adapters/codex.py`
- Create: `contracts/schemas/providers/codex/v1/event.schema.json`
- Create: `contracts/schemas/providers/codex/v1/termination.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `tests/fixtures/providers/codex/valid.jsonl`
- Create: `tests/fixtures/providers/codex/truncated.jsonl`
- Create: `tests/fixtures/providers/codex/exit-zero-missing-termination.jsonl`
- Test: `tests/contract/providers/test_codex_adapter.py`

- [ ] 写只读检出、结构化 finding、公开摘要、无摘要、schema 漂移、缺 termination 和审核后 SHA 漂移测试。
- [ ] 实现锁定 Codex JSON 模式和 D-backed `CODEX_HOME`；Runner 只读，不能修改 candidate 或调用写 tool。
- [ ] finding 必须有 rule/severity/evidence/location/fingerprint/fixability；不可复核主观意见不得单独 reject。
- [ ] 在同一 Task 登记 Provider schema 分类，运行普通 codegen 和 `--check`；再运行 fixture + 最小真实只读审核，Expected: Adapter 合同 PASS，完整 `REVIEW-001` 留到 Phase 4。
- [ ] 提交：`feat(harness): add certified Codex reviewer adapter`。

## Task 7: 实现 Tool Broker 和 MCP 注册策略

**Files:**

- Create: `apps/agent/src/factory_agent/harness/tool_broker.py`
- Create: `apps/agent/src/factory_agent/harness/tool_registry.py`
- Create: `apps/agent/src/factory_agent/harness/mcp_policy.py`
- Test: `tests/agent/unit/harness/test_tool_broker.py`
- Test: `tests/security/test_mcp_registration.py`

- [ ] 写无限制 shell、缺 schema/capability/timeout/retry/side-effect/redaction、越 scope 网络和秘密输出测试。
- [ ] 实现内置 Git/Docker/Browser/SSH/File Adapter 元数据注册；任何 tool call 先经过 ExecutionAuthorization 和稳定 action identity。
- [ ] MCP 拒绝必须生成 audit/receipt，注册失败的工具不能出现在模型可调用清单。
- [ ] 运行 `MCP-001`，Expected: 所有非法工具 fail closed。
- [ ] 提交：`feat(harness): add capability-scoped tool broker`。

## Task 8: 实现暂停/停止、backpressure 和隐藏 checkpoint

**Files:**

- Create: `apps/agent/src/factory_agent/harness/runner_orchestrator.py`
- Create: `apps/agent/src/factory_agent/integrations/git_bridge/checkpoint.py`
- Test: `tests/integration/wsl/test_runner_control.py`
- Test: `tests/integration/git_bridge/test_hidden_checkpoint.py`
- Test: `tests/chaos/test_stream_backpressure.py`

- [ ] 写 pause safe point、immediate kill、快速 resume、旧 Attempt DRAIN、未跟踪文件 checkpoint、hook 禁用和 hard limit 测试。
- [ ] checkpoint 只在 tool call 终态静止点：Broker 为每个 Attempt 设置 D-backed ext4 `GIT_INDEX_FILE=/var/lib/ai-coding-factory/tmp/indexes/<attempt-id>.index`，每次 Git 调用使用镜像内 root-owned 只读空目录 `-c core.hooksPath=/opt/factory/empty-hooks`，按 `read-tree`→`add -A -- <schema 校验后的 authorized pathspecs>`→`write-tree`→`commit-tree`→CAS `refs/factory/checkpoints/{attempt_id}` 执行。范围外 dirty、不可控 clean filter 或 submodule 均 fail closed；真实 index/branch 不变。
- [ ] 背压达到硬上限时安全 interrupt 并排空 termination；不能完整收尾则 Attempt LOST/RECONCILING，Gate 拒绝。
- [ ] 运行 `CTRL-001..002`、`STREAM-BP-001` 真实 Runner chaos，Expected: PASS。
- [ ] 提交：`feat(harness): add attempt-safe controls and checkpoints`。

## Task 9: 实现真实 stream/scheduler 性能基准

**Files:**

- Create: `tools/loadgen/Cargo.toml`
- Modify: `Cargo.lock`
- Create: `tools/loadgen/src/main.rs`
- Create: `tools/loadgen/src/profile.rs`
- Create: `tools/loadgen/src/generator.rs`
- Create: `tools/loadgen/src/arrival.rs`
- Create: `contracts/benchmarks/scheduler-profile.v1.json`
- Create: `tools/loadgen/src/scheduler.rs`
- Create: `tests/performance/test_stream_profiles.py`
- Create: `tests/performance/test_scheduler.py`
- Create: `tests/performance/metrics.py`
- Create: `tests/performance/assertions.py`
- Create: `scripts/run-performance.ps1`

- [ ] 实现固定 generator ID/digest、seed `20260803`、Task/事件/payload 分布和 paced/burst/sparse arrival；冻结 scheduler profile 的三个代表任务、Claude=1/Codex=1 槽位、等待原因、阶段计时与 ETA 误差字段。
- [ ] 接入真实 redactor→object flush→SQLite→IPC→React/xterm 完成时间；记录各分段指标和 queue slope/working set。
- [ ] 运行 sustained 预热 10s + 60s、burst 12,000/100 ms、sparse 1/5/10 events/s；存在其他 Factory 任务或 manifest 漂移则结果 INVALID。
- [ ] 验证三项规范 `STREAM-PERF-*` 可生成 Phase 3 FINAL；scheduler 断言槽位不超过 1、等待原因准确和三个 fixture workflow 完成，但只生成 PARTIAL，`SCHED-PERF-001` 最终由 Phase 6 用真实端到端任务签发。
- [ ] 分两次提交：`test(perf): add deterministic event load generator`、`test(perf): enforce stream and scheduler gates`。

## Task 10: Phase 3 真实运行 Gate

**Files:**

- Create: `docs/architecture/execution-plane.md`
- Create: `docs/protocols/runner-v1.md`
- Create: `docs/operations/local-runtime-onboarding.md`
- Modify: `scripts/test.ps1`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/ipc/handlers.py`
- Modify: `apps/agent/src/factory_agent/scheduler/dispatcher.py`
- Test: `tests/integration/wsl/test_composed_runner_path.py`

- [ ] 先运行 `generate.py --check` 复核 Tasks 3–6 已原子提交的 catalog/generated 无漂移；随后将 runtime probe、WSL/Build Broker、Provider Adapter、Tool Broker 和 Runner orchestrator 注册到 Agent composition root/IPC/dispatcher，从桌面创建 fixture task 穿过完整路径，再运行 Rust/Python lint、contract、security、Runner integration、chaos 和性能命令。
- [ ] 执行 `PROC-001`：Claude 真实运行时杀 UI，确认 Agent/Broker/container 继续且 UI 重连只回放 durable cursor。
- [ ] 执行 `STREAM-001..002`、`STREAM-BP-001`、三项性能、`CLI-PROFILE-001`、`RUNTIME-001`、`WSL-IO-001` 的 owner 场景；`STREAM-DUR-001` 只签 PARTIAL，等待 Phase 6 的 WSL/Windows 硬重启重放。
- [ ] 独立审查所有模型公开摘要、terminal、日志、fixture 和 receipt 无隐藏思维链或秘密原文。
- [ ] 提交：`test(harness): certify real runner and provider evidence chain`。

Phase 3 完成后，桌面能够观察和控制真实 Claude/Codex Runner；尚未具备完整规划、Verifier/Codex 返工和 PR/merge，必须继续 Phase 4。
