# AI Coding Factory Phase 6 Hardening and Complete Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成严格 D 盘安全、Compatibility Manifest、全链路性能、Windows 安装升级、备份恢复、威胁/Chaos 测试、47 项代表流程和 11 项 DoD 聚合，形成首个完整可交付版本。

**Architecture:** Phase 6 不新增旁路业务逻辑；它把 Phase 0–5 的真实实现放进干净 Windows、Factory WSL2、标准 Linux 和故障注入环境，生成同一 manifest digest 下的机器回执。安装、升级、测试和运维脚本都调用产品的冻结合同，不能另写一套“测试专用成功路径”。

**Tech Stack:** Tauri bundler/Windows installer、PyInstaller、PowerShell、DPAPI、Windows/WSL/Docker probes、Rust load generator、pytest/Vitest/Cargo/WebDriver、secret scan、SBOM/provenance、Codex final review。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文短命令只表示 wrapper 参数。真实 Windows reboot 只允许专用实验环境在显式 `-AllowHostReboot` 下执行。

---

## Task 1: 完成严格 StorageLocationContract 和威胁加固

**Files:**

- Modify: `apps/agent/src/factory_agent/security/storage_contract.py`
- Modify: `apps/agent/src/factory_agent/security/dpapi_vault.py`
- Modify: `apps/agent/src/factory_agent/security/credential_broker.py`
- Create: `apps/agent/src/factory_agent/security/volume_identity.py`
- Create: `apps/agent/src/factory_agent/security/write_guard.py`
- Create: `apps/agent/src/factory_agent/security/ipc_identity.py`
- Create: `ops/windows/controlled-write-locations.v1.json`
- Create: `scripts/audit-storage-location.ps1`
- Create: `tests/security/test_path_attacks.py`
- Create: `tests/security/test_ipc_attacks.py`
- Create: `tests/security/test_malicious_repo.py`
- Create: `tests/security/test_secret_leakage.py`
- Create: `docs/security/threat-model.md`

- [ ] 写 C/E/F fallback、junction/symlink/mount/`SUBST`/网络/可移动卷、TOCTOU 替换、恶意 Git hook、IPC 重放/冒充和秘密 exfiltration 测试。
- [ ] 每次启动/任务预检解析 volume identity 和每级 reparse；每次打开关键文件使用防跟随/句柄二次确认。
- [ ] 把安装、Agent/Helper、cache、WebView user-data、crash dump、profile、WSL/Docker、BuildKit、volume 和 backup 全列入机器清单；OS 不可控写入逐项披露。
- [ ] 运行 `PATH-001..002`、`CLI-PROFILE-001`、`RUNTIME-001`、`WSL-IO-001` 全流程，Expected: 可控 C 盘写入 0。
- [ ] 提交：`feat(security): harden storage IPC and repository boundaries`。

## Task 2: 实现 Compatibility Manifest 候选认证机制

**Files:**

- Create: `apps/agent/src/factory_agent/compatibility/probe.py`
- Create: `apps/agent/src/factory_agent/compatibility/validator.py`
- Create: `apps/agent/src/factory_agent/compatibility/signer.py`
- Create: `apps/agent/src/factory_agent/compatibility/upgrade_candidate.py`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/scheduler/dispatcher.py`
- Create: `scripts/certify-compatibility.ps1`
- Create: `tests/contract/test_manifest_drift.py`
- Create: `tests/chaos/test_runtime_upgrade_drift.py`

- [ ] 写 Windows/WebView2/WSL/Docker/CLI/schema/image/policy/profile/queue/clock/fsync/loadgen，以及 frame limits/redaction policy 任一 digest 漂移测试。
- [ ] 把 Compatibility validator 注册到 bootstrap/dispatcher，实现启动、Attempt 派发前、Runner 启动后三级核对；未知版本停止新派发，运行 Attempt 安全停止/reconcile。
- [ ] 候选升级先回放脱敏 JSONL、profile path、异常/截断/termination 和最小真实任务；全部通过才签 `qualification=CANDIDATE` manifest，失败保留旧版。Tasks 1–7 尚未全部提交前禁止签 FINAL manifest。
- [ ] 运行 `COMPAT-001` 候选场景，Expected: 漂移不会产生成功 Gate，本任务只生成 PARTIAL subcheck；FINAL 必须在 Task 9 冻结全部实现后重放。
- [ ] 提交：`feat(compat): enforce signed runtime compatibility`。

## Task 3: 执行跨 Agent、WSL 和 Windows 重启耐久性演练

**Files:**

- Create: `tools/chaos/durability_controller.py`
- Create: `contracts/testing/stream-durability-crash-points.v1.json`
- Create: `scripts/run-durability-chaos.ps1`
- Create: `ops/windows/durability-resume-task.xml`
- Modify: `scripts/test.ps1`
- Modify: `apps/agent/src/factory_agent/recovery/startup.py`
- Modify: `apps/agent/src/factory_agent/application/shutdown_service.py`
- Test: `tests/chaos/durability/test_claim_crash_points.py`
- Test: `tests/chaos/durability/test_reboot_resume.py`
- Test: `tests/chaos/durability/test_cleanup.py`

- [ ] 写 controller 崩溃、runToken 篡改、重复 resume、错误 expectedBranch、未传 `-AllowHostReboot`、最终状态轮询超时，以及每个 claim/object/SQLite crash point与成功/失败/崩溃后的 Scheduled Task/token 残留测试。
- [ ] 在 D-backed state 中持久化签名 `runToken/crashPoint/expectedBranch/manifestDigest`；恢复入口与 controller 都以 `try/finally` 注销本次 Scheduled Task、撤销 token、写 cleanup receipt。重启后把最终状态/exitCode 写入 `durability-runs/{runToken}/result.json`，原 PowerShell 进程或恢复 watcher 轮询该 Artifact 并返回同一退出码。
- [ ] 先运行不带 `-AllowHostReboot` 的负例并确认零主机副作用；再在专用实验机运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/run-durability-chaos.ps1 -Modes AgentKill,WslTerminate,WindowsReboot -AllowHostReboot -EmitReceipts`。Expected: 按签名 token 逐切点自动续跑并最终返回 0，不用人工改状态。
- [ ] 核对四分支、全 Task claim、对象引用、durable cursor、Attempt/Action reconciliation 和无永久 pending；`scripts/test.ps1 -Suite all` 必须机械调用或核验当前 qualification 对应的 durability receipt。本任务只生成候选 manifest PARTIAL，`STREAM-DUR-001` FINAL 必须在 Task 9 的最终 manifest 下重跑，并证明 Scheduled Task/token/临时状态全清理。
- [ ] 提交：`test(durability): certify process WSL and Windows crash recovery`。

## Task 4: 在完整产品上重跑性能与资源基准

**Files:**

- Modify: `tools/loadgen/Cargo.toml`
- Modify: `tools/loadgen/src/main.rs`
- Modify: `tools/loadgen/src/profile.rs`
- Modify: `tools/loadgen/src/generator.rs`
- Modify: `tools/loadgen/src/arrival.rs`
- Modify: `tools/loadgen/src/scheduler.rs`
- Modify: `contracts/benchmarks/scheduler-profile.v1.json`
- Modify: `tests/performance/test_stream_profiles.py`
- Modify: `tests/performance/test_scheduler.py`
- Modify: `tests/performance/metrics.py`
- Modify: `tests/performance/assertions.py`
- Create: `apps/desktop/src/telemetry/performance.ts`
- Create: `scripts/run-full-performance.ps1`

- [ ] 验证 generator digest、seed、40/35/25 Task 比例、70/15/10/5 事件比例、512/704/1024 payload 分布和 1% 跨 chunk secret。
- [ ] sustained：预热 10s、测量 60s，最后 50s queue slope events≤1/s、bytes≤4096/s，SQLite commit p95<1s、durable UI p95<2s。
- [ ] burst：12,000 events ≤ 100 ms，最后输入起 5s 清空；sparse：1/5/10 events/s，各 60s，安全 record p95 < 2s/max < 5s，未闭合 frame 每秒无内容 heartbeat。
- [ ] 在 Claude=1/Codex=1 槽位下并发执行 scheduler profile 冻结的三个真实端到端任务；断言占用不超过 1、等待原因准确、任务全部完成，并输出阶段服务时间、排队分布、吞吐与 ETA 误差。检查 working-set 增量 ≤ 256 MiB；环境有其他任务或 manifest 不一致则 INVALID。
- [ ] 提交：`test(perf): certify full durable UI and scheduler SLOs`。

## Task 5: 实现 D 盘 Windows 安装、独立 Agent 自启动和安全升级

**Files:**

- Modify: `apps/desktop/src-tauri/tauri.conf.json`
- Create: `apps/desktop/src-tauri/src/updater.rs`
- Create: `apps/desktop/src-tauri/src/path_bootstrap.rs`
- Create: `apps/desktop/src-tauri/src/shutdown.rs`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Create: `apps/agent/factory-agent.spec`
- Create: `apps/agent/src/factory_agent/ops/upgrade_guard.py`
- Create: `apps/agent/src/factory_agent/ops/state_backup.py`
- Create: `apps/agent/src/factory_agent/ops/state_restore.py`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Create: `ops/installer/nsis-hooks.nsh`
- Create: `ops/installer/install-manifest.json`
- Create: `scripts/package-windows.ps1`
- Create: `scripts/package.ps1`
- Create: `scripts/sign-windows.ps1`
- Create: `scripts/test-shutdown-fast.ps1`
- Create: `tests/packaging/windows/install.Tests.ps1`
- Create: `tests/packaging/windows/upgrade.Tests.ps1`
- Create: `tests/packaging/windows/uninstall.Tests.ps1`
- Create: `tests/packaging/windows/storage-snapshot.Tests.ps1`
- Create: `tests/packaging/windows/shutdown-fast.Tests.ps1`

- [ ] 写默认 C 安装拒绝、Agent 未自启动、关闭 UI 杀 Agent、真实 logoff/shutdown 0–2 秒强杀、RUNNING/RECONCILING 更新、签名/manifest 失败、升级中断和卸载误删数据测试。
- [ ] 把 updater/path bootstrap/shutdown 与 Agent upgrade guard 注册到 Tauri/Agent composition root；生成 Tauri 桌面包与 PyInstaller Agent，安装器要求 D 根，注册 per-user Agent 登录自启动，UI/Agent 可独立更新和恢复。
- [ ] 更新前证明无 RUNNING/RECONCILING/UNKNOWN，软暂停、备份 SQLite、验证签名/manifest；失败回旧版本并 reconcile。真实关机 probe 必须证明 2 秒最小事务、无假 PAUSED、启动直接 reconciliation 和预算跨 boot 正确。
- [ ] 卸载默认保留数据；删除数据是独立明确操作。没有代码签名证书时仅产出标记清楚的测试包，不冒充正式签名发布。
- [ ] 提交：`feat(packaging): add D-backed Windows install and safe update`。

## Task 6: 完成本地备份恢复、保留和运维 Runbook

**Files:**

- Modify: `apps/agent/src/factory_agent/ops/state_backup.py`
- Modify: `apps/agent/src/factory_agent/ops/state_restore.py`
- Create: `apps/agent/src/factory_agent/ops/retention.py`
- Create: `apps/agent/src/factory_agent/ops/diagnostics.py`
- Create: `docs/operations/install-upgrade.md`
- Modify: `docs/operations/backup-restore.md`
- Create: `docs/operations/diagnostics.md`
- Create: `docs/operations/uninstall-data-retention.md`
- Create: `tests/packaging/test_state_restore.py`
- Create: `tests/chaos/test_upgrade_recovery.py`

- [ ] 分开写两类失败测试：primary/WAL 仍完整的进程或主机崩溃，以及 SQLite/WAL/卷损坏后的 backup restore；另覆盖 checksum/版本错误、Artifact dangling reference、30 天 transcript 清理破坏证据和升级崩溃。
- [ ] 实现每日 7/每周 4、schema 升级前备份；恢复先到隔离路径校验再切换，绝不覆盖唯一有效库。
- [ ] retention 只清理无 Task/Gate/Artifact/receipt/ref 引用对象并写 tombstone；诊断包强制脱敏。
- [ ] 对 primary/WAL 完整的 crash recovery 验证已提交事务 RPO=0；介质损坏只能按最后有效备份计算并报告真实 backup RPO（默认每日策略上限 24h）或 fail closed，不得宣称 RPO=0。两类场景分别验证 2 分钟可用和 5 分钟活动任务分类目标。
- [ ] 提交：`feat(ops): add verified backup retention and diagnostics`。

## Task 7: 实现机器可判定的 47 项测试和 DoD 聚合器

**Files:**

- Create: `tools/dod/parser.py`
- Create: `tools/dod/coverage.py`
- Create: `tools/dod/receipts.py`
- Create: `tools/dod/main.py`
- Create: `contracts/testing/dod-map.v1.json`
- Create: `tests/dod/test_spec_coverage.py`
- Create: `tests/dod/test_receipt_validation.py`
- Create: `tests/dod/test_manifest_identity.py`
- Create: `scripts/run-full-dod.ps1`

- [ ] 同时解析 Master Spec §20.2 与 §22 首列：展开流程数值范围得到精确 47 个 test ID；DoD 必须精确为 `DOD-APP-001`、`DOD-STAGE-001`、`DOD-OBS-001`、`DOD-PERF-001`、`DOD-AI-001`、`DOD-CTRL-001`、`DOD-GIT-001`、`DOD-DEPLOY-001`、`DOD-SEC-001`、`DOD-OPS-001`、`DOD-REVIEW-001`。缺项、额外项、别名、重复或错误范围展开都失败。
- [ ] 从 catalog 和 `dod-map.v1.json` 验证每个 test ID 恰有一个 finalPassOwner、全部 requiredReplays、至少一个 DoD，并强制 `dodMapIds == executedDoDIds == specDoDIds`；任一集合不等、非 PASS、Artifact 未 committed 或 manifest/scenario contract digest 不一致均失败。
- [ ] 聚合器只接受 `qualification=FINAL + finalPassOwner + scenarioContractDigest` 的机器 receipt；PARTIAL/subcheck、手工 Markdown 或数据库状态均不能计入 47 项。
- [ ] 运行 `python -m pytest tests/dod -q` 和负面 fixtures，Expected: 正例 PASS、每种篡改均 fail closed。
- [ ] 提交：`test(dod): add complete receipt coverage aggregator`。

## Task 8: 候选全流程预演和最终证据脚本冻结

**Files:**

- Create: `docs/operations/final-acceptance.md`
- Create: `scripts/run-clean-windows-e2e.ps1`
- Create: `scripts/run-full-product-e2e.ps1`

- [ ] Tasks 1–7 的实现提交全部完成后，使用 candidate manifest 在干净 Windows 11 x64、Factory Ubuntu LTS WSL2 和标准 x86_64 Linux SSH/Compose 环境预演完整产品；冻结 `run-clean-windows-e2e.ps1`、`run-full-product-e2e.ps1` 和验收说明，但本任务禁止签 FINAL manifest 或 FINAL receipt。
- [ ] 按下表为每个 intended test ID 生成唯一 `subcheckId` 和同一 candidate manifest digest 的 `qualification=PARTIAL` rehearsal receipt；不得用正式 test ID 冒充 FINAL：

| 流程组 | 展开 ID 数 | 测试 ID |
| --- | ---: | --- |
| 目标阶段 | 6 | `STAGE-001`、`STAGE-002`、`STAGE-003`、`STAGE-004`、`STAGE-005`、`STAGE-006` |
| 授权/规划/状态 | 9 | `AUTH-001`、`AUTH-002`、`PLAN-001`、`PLAN-HASH-001`、`BOOT-001`、`MCP-001`、`STATE-001`、`LEASE-001`、`EVENT-HASH-001` |
| 进程/控制/流 | 7 | `PROC-001`、`CTRL-001`、`CTRL-002`、`STREAM-001`、`STREAM-002`、`STREAM-DUR-001`、`STREAM-BP-001` |
| 性能 | 4 | `STREAM-PERF-BURST`、`STREAM-PERF-SUSTAINED`、`STREAM-PERF-SPARSE`、`SCHED-PERF-001` |
| 审核/Git/副作用 | 3 | `REVIEW-001`、`GIT-001`、`SIDEFX-001` |
| 发布/回滚 | 10 | `DEPLOY-001`、`DEPLOY-002`、`DEPLOY-003`、`GUARD-001`、`DEPLOY-FENCE-001`、`NGINX-001`、`BACKUP-001`、`DEPLOY-RAM-001`、`STORE-001`、`RETRY-001` |
| 预算/通知/路径/兼容 | 8 | `BUDGET-001`、`NOTIFY-001`、`PATH-001`、`PATH-002`、`CLI-PROFILE-001`、`RUNTIME-001`、`WSL-IO-001`、`COMPAT-001` |
| **合计** | **47** | 无遗漏 |

- [ ] 机器执行清单直接消费 Task 7 的 canonical catalog，并断言 `executedIds == specIds` 以及 `dodMapIds == executedDoDIds == specDoDIds`；人工表只用于阅读。`CTRL-001` 枚举全部 nodeType；`NOTIFY-001` 在 UI 已退出时验证 Agent helper 恰好通知一次；预演上述精确 11 项 DoD，blocking 检查不得 skipped，发现任何问题必须先修代码再进入 Task 9。
- [ ] 真实云厂商 ECS/CVM 仅在用户提供临时 ServerProfile 后另签 `cloud-certification-receipt`；未提供显示“未认证”。
- [ ] 提交：`test(e2e): certify complete AI Coding Factory delivery`。

## Task 9: 冻结最终 manifest、重跑完整 DoD 并完成 Codex 审核

**Files:**

- Create: `docs/release/first-complete-release.md`
- Create: `docs/release/evidence-index.json`
- Create: `docs/release/known-boundaries.md`
- Test: `tests/e2e/test_composed_complete_product.py`

- [ ] 先完成本任务列出的测试、release 文档和 evidence slot index，运行全仓 Gate 并创建产品源码冻结 commit；基于该 commit 构建 Desktop/Agent/Runner/BuildKit/installer，再签覆盖 source/schema/policy/profile/loadgen/artifact digest 的 FINAL Compatibility Manifest。签名之后禁止再修改运行代码、测试或冻结输入。
- [ ] 在 FINAL manifest 认证环境重新执行 Task 8 表中精确 47 个 ID 与 Task 7 冻结的精确 11 个 DoD ID，强制 `executedIds == specIds` 与 `dodMapIds == executedDoDIds == specDoDIds`；必须包含真实 Windows reboot durability、完整 UI/stream/scheduler 性能和 Linux 发布验收，只接受同一 manifest digest、正确 finalPassOwner 和 `qualification=FINAL` 的 committed receipt。
- [ ] 从桌面创建六种目标任务并穿过当前组合根；Codex 在只读检出按冻结 rubric 审核完整源码、中文注释、结构化日志、测试、威胁模型、打包、47 项 receipt、11 项 DoD 和发布证据，无 blocker/high 才通过。
- [ ] 有 open blocker/high 或任一代码/Artifact/digest 变化时，立即作废 FINAL manifest 与受影响 receipt，回到所属 Phase 修复，并从本任务第一步重新构建、重签、重跑；不得修改 rubric 或降低阈值。
- [ ] 审核通过后只生成 D-backed 运行时交接 Artifact 和本地 tag 建议，不再改冻结 commit；验证 Git 工作树干净、所有子 agent 关闭、无秘密/本地数据库/log/大型生成物误入提交。push、merge、代码签名发布或真实部署仍需用户当次明确授权。

只有本任务完成，才能把产品称为“首个完整版本”；`SELF_HOSTING_CODEX_APPROVED`、测试包或单次 staging 成功均不能替代本 Gate。
