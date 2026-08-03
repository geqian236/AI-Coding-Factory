# AI Coding Factory Phase 1 Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现独立常驻 Agent 的权威控制平面，包括五维状态机、单写 SQLite、Artifact/Event Store、授权、lease/fencing、控制命令、预算、调度、通知和崩溃恢复。

**Architecture:** 纯领域层不依赖 SQLite、IPC、UI 或 Runner；所有写事务经过唯一 `write_coordinator.py`。权威状态事件与业务状态同事务，Provider stream 通过 PreparedBatch/durable object/group commit 进入 timeline；所有外部动作通过 port 和追加 receipt 建模。

**Tech Stack:** Python 3.12、Pydantic、stdlib `sqlite3` + 显式 SQL、asyncio/线程隔离 writer、pytest/Hypothesis、structlog、Win32 clock/mutex adapter。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文中的短命令只表示 wrapper 的参数，不能裸跑。

---

## Task 1: 实现纯领域模型和 SQLite 原子底座

**Files:**

- Create: `apps/agent/src/factory_agent/domain/plans.py`
- Create: `apps/agent/src/factory_agent/domain/workflow.py`
- Create: `apps/agent/src/factory_agent/domain/artifacts.py`
- Modify: `apps/agent/src/factory_agent/domain/events.py`
- Create: `apps/agent/src/factory_agent/domain/authorization.py`
- Create: `apps/agent/src/factory_agent/domain/control.py`
- Create: `apps/agent/src/factory_agent/domain/resources.py`
- Create: `apps/agent/src/factory_agent/domain/budgets.py`
- Create: `apps/agent/src/factory_agent/ports/unit_of_work.py`
- Create: `apps/agent/src/factory_agent/ports/workflow_store.py`
- Create: `apps/agent/src/factory_agent/ports/event_store.py`
- Create: `apps/agent/src/factory_agent/ports/authorization_store.py`
- Create: `apps/agent/src/factory_agent/ports/resource_store.py`
- Create: `apps/agent/src/factory_agent/ports/object_store.py`
- Create: `apps/agent/src/factory_agent/ports/inspectors.py`
- Create: `apps/agent/src/factory_agent/ports/clock.py`
- Create: `apps/agent/src/factory_agent/ports/timeline_sink.py`
- Create: `apps/agent/src/factory_agent/scheduler/write_coordinator.py`
- Create: `apps/agent/src/factory_agent/integrations/windows/named_mutex.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/database.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/unit_of_work.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/migrations/0001_workflow.sql`
- Create: `apps/agent/src/factory_agent/storage/sqlite/migrations/0002_event_store.sql`
- Create: `apps/agent/src/factory_agent/storage/sqlite/migrations/0003_auth_resources.sql`
- Create: `apps/agent/src/factory_agent/storage/sqlite/migrations/0004_control_budget.sql`
- Test: `tests/agent/unit/domain/test_plan_revision.py`
- Test: `tests/agent/unit/domain/test_workflow_invariants.py`
- Test: `tests/agent/integration/sqlite/test_schema_constraints.py`
- Test: `tests/agent/integration/sqlite/test_unit_of_work.py`
- Test: `tests/agent/unit/scheduler/test_write_coordinator.py`
- Test: `tests/agent/integration/windows/test_named_mutex.py`

- [ ] 写失败测试：PlanRevision 不可变、Attempt 不可复用、`achieved_stage` 不倒退、FK/CHECK/UNIQUE 与陈旧 `state_version` CAS 生效。
- [ ] 运行 `python -m pytest tests/agent/unit/domain tests/agent/integration/sqlite -q`，确认缺实现失败。
- [ ] 实现领域对象、repository port、唯一 write coordinator、Windows named mutex、迁移和单写 UnitOfWork；启动时强制核对 `journal_mode=WAL`、`synchronous=FULL`、`foreign_keys=ON`，禁止 ORM 隐藏事务边界。
- [ ] 注入状态或 `state.changed` 任一写失败，证明事务整体回滚；第二 writer 必须排队。未取得 mutex 的第二 Agent 不得打开 SQLite、推进 epoch 或扫描对象。
- [ ] 精确暂存并提交：`feat(control-plane): add immutable domain and SQLite unit of work`。

## Task 2: 实现状态机、barrier、里程碑和控制命令

**Files:**

- Create: `apps/agent/src/factory_agent/state_machine/transitions.py`
- Create: `apps/agent/src/factory_agent/state_machine/barriers.py`
- Create: `apps/agent/src/factory_agent/state_machine/milestones.py`
- Create: `apps/agent/src/factory_agent/state_machine/write_guards.py`
- Create: `apps/agent/src/factory_agent/state_machine/predicates.py`
- Create: `apps/agent/src/factory_agent/application/transition_service.py`
- Create: `apps/agent/src/factory_agent/application/control_service.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/workflow_repository.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/control_repository.py`
- Test: `tests/agent/unit/state_machine/test_transitions.py`
- Test: `tests/agent/unit/state_machine/test_barriers.py`
- Test: `tests/agent/integration/control/test_control_commands.py`
- Test: `tests/agent/integration/control/test_drain_write.py`

- [ ] 写参数化失败测试，枚举 §7.3 每条合法/非法转换、barrier settled/passed、UNKNOWN settle timeout、缺 Artifact、活动 Attempt 和 CAS 冲突。
- [ ] 写快速 `PAUSE→RESUME` 测试，断言旧 Attempt 仍为 `DRAINING`，只允许 stream/checkpoint/termination 收尾，新运行必须从 `QUEUED` 创建 Attempt。
- [ ] 实现五维状态、`NORMAL_WRITE`/`DRAIN_WRITE`、command requestId 幂等和追加 receipt；`RESUME` 只能改 desired state，不能直接写 observed RUNNING。
- [ ] 运行 `STATE-001` 完整 scenario contract：枚举五维合法/非法转换、settled/passed barrier、UNKNOWN settle timeout、里程碑证据、快速暂停恢复与 CAS 竞争；全部通过后由 Phase 1 唯一 final owner 签发 `qualification=FINAL`。
- [ ] 提交：`feat(state): enforce deterministic barriers and control semantics`。

## Task 3: 实现预算时钟和重试分类

**Files:**

- Create: `apps/agent/src/factory_agent/policy/retry_policy.py`
- Create: `apps/agent/src/factory_agent/application/budget_service.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/budget_repository.py`
- Test: `tests/agent/unit/policy/test_retry_policy.py`
- Test: `tests/agent/integration/control/test_budget_clock.py`

- [ ] 写失败测试覆盖同 boot 睡眠、wall 前跳/回拨、跨 boot、合法关机、用户暂停、requires-user-action、Provider Retry-After、quota exhausted 和预算耗尽。
- [ ] 实现 append-only `budget_clock_events` 与投影同事务；只有 `USER_PAUSED/REQUIRES_USER_ACTION` 暂停计时，重启/重规划不重置。
- [ ] 实现 `TRANSIENT/PROVIDER_RATE_LIMIT/PROVIDER_QUOTA_EXHAUSTED/FIXABLE/CAPACITY/AUTH_POLICY/UNKNOWN_STATE/IRREVERSIBLE_RISK/INTERNAL_BUG` 的机械分流，禁止按异常类盲重试。
- [ ] 运行定向测试，Expected: 生成 `BUDGET-001`、`RETRY-001` 的唯一 `subcheckId` 与 `qualification=PARTIAL`，不得生成正式 FINAL receipt。
- [ ] 提交：`feat(policy): add reconstructable budget and retry decisions`。

## Task 4: 实现 capability catalog 和三层授权

**Files:**

- Create: `apps/agent/src/factory_agent/policy/capability_catalog.py`
- Create: `apps/agent/src/factory_agent/policy/scope.py`
- Create: `apps/agent/src/factory_agent/policy/authorizer.py`
- Create: `apps/agent/src/factory_agent/application/authorization_service.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/authorization_repository.py`
- Test: `tests/agent/unit/policy/test_capability_catalog.py`
- Test: `tests/agent/unit/policy/test_scope.py`
- Test: `tests/agent/integration/authorization/test_authorizer.py`
- Test: `tests/agent/integration/authorization/test_authorization_fencing.py`

- [ ] 写六种 target stage 展开测试；未知 nodeType、catalog digest 漂移、越权 capability、资源/SHA/revision 漂移均失败。
- [ ] 写 lease/token/control sequence 改变后旧 ExecutionAuthorization 即使 TTL 未到也不可消费的测试。
- [ ] 实现 IntentAuthorization → PlanRevision → ExecutionAuthorization 的机械求交；降低 stage/cancel 撤销未消费授权，提高权限/预算/风险进入 ACTION_REQUIRED。
- [ ] 运行 `python -m pytest tests/agent/unit/policy tests/agent/integration/authorization -q`，Expected: 只生成 `STAGE-001..006`、`AUTH-001..002`、`PLAN-001` 的领域 subcheck，全部 `qualification=PARTIAL`。
- [ ] 提交：`feat(auth): derive scoped fenced execution authorizations`。

## Task 5: 实现 lease、调度、Action receipt、通知和 target guard

**Files:**

- Create: `apps/agent/src/factory_agent/scheduler/lease_service.py`
- Create: `apps/agent/src/factory_agent/scheduler/dispatcher.py`
- Create: `apps/agent/src/factory_agent/scheduler/resource_slots.py`
- Create: `apps/agent/src/factory_agent/scheduler/wakeups.py`
- Create: `apps/agent/src/factory_agent/application/side_effect_service.py`
- Create: `apps/agent/src/factory_agent/application/notification_service.py`
- Create: `apps/agent/src/factory_agent/application/global_control_service.py`
- Create: `apps/agent/src/factory_agent/domain/credentials.py`
- Create: `apps/agent/src/factory_agent/ports/notifications.py`
- Create: `apps/agent/src/factory_agent/ports/credential_broker.py`
- Create: `apps/agent/src/factory_agent/security/dpapi_vault.py`
- Create: `apps/agent/src/factory_agent/security/credential_broker.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/resource_repository.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/action_repository.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/notification_repository.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/credential_repository.py`
- Test: `tests/agent/unit/scheduler/test_leases.py`
- Test: `tests/agent/unit/scheduler/test_resource_slots.py`
- Test: `tests/agent/integration/side_effects/test_action_receipts.py`
- Test: `tests/agent/integration/side_effects/test_notifications.py`
- Test: `tests/agent/integration/side_effects/test_target_guards.py`
- Test: `tests/agent/integration/control/test_global_pause.py`
- Test: `tests/security/test_credential_broker.py`

- [ ] 写双 dispatcher、陈旧 token、授权消费与 STARTED 非原子、响应丢失、跨 Task guard 绕过、通知未知结果重发、全局暂停后仍派发和 credential scope/TTL/明文泄漏失败测试。
- [ ] 实现 token 单调 lease；Authorization 消费、Action identity 和 STARTED receipt 同事务；完成只追加后继 receipt。
- [ ] 实现持久化全局暂停并阻止新任务派发；`notification_actions` 去重与 `DISPATCH_UNKNOWN` 不重发；通用 DPAPI Vault/Broker 只接受 Phase 0 codegen 生成的 `CredentialRef` 类型并保存 scope/fingerprint/expiry；guard 绑定稳定物理 fingerprint。
- [ ] 运行 scheduler/side-effect/global-control/security 测试，Expected: 只生成 `LEASE/SIDEFX/GUARD/NOTIFY` 的 `qualification=PARTIAL` subcheck，外部 Adapter 留待后续最终重放。
- [ ] 分三次提交：`feat(scheduler): add fenced resource leases`、`feat(actions): add append-only side-effect receipts`、`feat(notifications): add durable notification actions`。

## Task 6: 实现 durable object store

**Files:**

- Create: `apps/agent/src/factory_agent/storage/objects/layout.py`
- Create: `apps/agent/src/factory_agent/storage/objects/durable_io.py`
- Create: `apps/agent/src/factory_agent/storage/objects/batch_manifest.py`
- Create: `apps/agent/src/factory_agent/storage/objects/segment.py`
- Create: `apps/agent/src/factory_agent/storage/objects/artifact_store.py`
- Create: `apps/agent/src/factory_agent/storage/objects/quarantine.py`
- Test: `tests/agent/integration/objects/test_durable_io.py`
- Test: `tests/agent/integration/objects/test_artifact_store.py`
- Test: `tests/agent/integration/objects/test_quarantine.py`
- Test: `tests/support/fault_injection.py`

- [ ] 写 flush、record、rename、父目录耐久、digest、SQLite reference 和 disk-full 每个切点的失败测试。
- [ ] 实现 `tmp → flush → cross-check → content-addressed final rename → parent durability → SQLite reference`；COMMIT 后禁止移动对象。
- [ ] 实现自描述 header/record/footer、完整对象补录、部分对象 quarantine 和已引用对象缺失 fail closed。
- [ ] 在真实 NTFS/WSL ext4 probe 上运行 `python -m pytest tests/agent/integration/objects -q`，Expected: PASS；无法由 Python 证明的 Windows durable 操作调用 Phase 0 冻结的 Rust helper。
- [ ] 提交：`feat(storage): add durable content-addressed objects`。

## Task 7: 实现 PreparedBatch、事件链、group commit 和背压

**Files:**

- Create: `apps/agent/src/factory_agent/storage/events/ingest_queue.py`
- Create: `apps/agent/src/factory_agent/storage/events/prepared_batch.py`
- Create: `apps/agent/src/factory_agent/storage/events/coordinator.py`
- Create: `apps/agent/src/factory_agent/storage/events/materializer.py`
- Create: `apps/agent/src/factory_agent/storage/events/replay.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/event_repository.py`
- Test: `tests/agent/integration/events/test_atomic_claim.py`
- Test: `tests/agent/integration/events/test_materializer.py`
- Test: `tests/agent/integration/events/test_backpressure.py`
- Test: `tests/agent/chaos/test_stream_durability.py`

- [ ] 写多 Task claim 全有或全无、同 Task 唯一 pending、批内连续链、竞争 head、COMMIT 前不可见和队列水位测试。
- [ ] 实现单 writer group commit；阈值来自 manifest，`synchronous=NORMAL` 配置必须拒绝。
- [ ] 实现 75%/50% 高低水位、硬上限、安全 interrupt 和每 stream 8 MiB 应急排空；不得持久化未脱敏字节或静默丢事件。
- [ ] 运行事件/chaos 测试，Expected: `EVENT-HASH-001` 可由其 Phase 1 final owner 生成 FINAL；`STREAM-DUR-001`、`STREAM-BP-001` 只生成控制平面 `qualification=PARTIAL` subcheck。
- [ ] 分三次提交：`feat(events): add atomic prepared batches`、`feat(events): materialize durable event chains`、`feat(events): enforce bounded ingest backpressure`。

## Task 8: 实现启动恢复和四分支 reconciliation

**Files:**

- Create: `apps/agent/src/factory_agent/recovery/startup.py`
- Create: `apps/agent/src/factory_agent/recovery/decisions.py`
- Create: `apps/agent/src/factory_agent/recovery/batch_reconciler.py`
- Create: `apps/agent/src/factory_agent/recovery/attempt_reconciler.py`
- Create: `apps/agent/src/factory_agent/recovery/action_reconciler.py`
- Create: `apps/agent/src/factory_agent/recovery/queue_rebuilder.py`
- Create: `apps/agent/src/factory_agent/recovery/recovery_report.py`
- Test: `tests/agent/integration/recovery/test_batch_reconciler.py`
- Test: `tests/agent/integration/recovery/test_attempt_reconciler.py`
- Test: `tests/agent/integration/recovery/test_action_reconciler.py`
- Test: `tests/agent/chaos/test_startup_recovery.py`

- [ ] 写无对象、部分对象、完整对象、事实矛盾四分支和全 Task claim 清理 CAS 测试。
- [ ] 写旧 Attempt/Action 未知不得重派、恢复必须 `RECONCILING→QUEUED→新 lease/token/Attempt/auth→RUNNING` 的测试。
- [ ] 实现固定启动顺序：StorageLocation/manifest → Windows named mutex → SQLite/WAL → writer epoch → pending object → attempt/action → queue → IPC；mutex 失败实例在 SQLite 之前退出。
- [ ] 运行 recovery/chaos，Expected: 无永久 pending、双提交、假成功或未知状态洗白。
- [ ] 分两次提交：`feat(recovery): reconcile durable event batches`、`feat(recovery): reconcile attempts and actions`。

## Task 9: 组装常驻 Agent、IPC port 和结构化可观察性

**Files:**

- Create: `apps/agent/src/factory_agent/__main__.py`
- Create: `apps/agent/src/factory_agent/bootstrap.py`
- Create: `apps/agent/src/factory_agent/ipc/protocol.py`
- Create: `apps/agent/src/factory_agent/ipc/authentication.py`
- Create: `apps/agent/src/factory_agent/ipc/windows_named_pipe.py`
- Create: `apps/agent/src/factory_agent/ipc/handlers.py`
- Create: `apps/agent/src/factory_agent/ipc/subscriptions.py`
- Create: `apps/agent/src/factory_agent/observability/metrics.py`
- Create: `apps/agent/src/factory_agent/observability/audit.py`
- Create: `apps/agent/src/factory_agent/application/task_service.py`
- Create: `apps/agent/src/factory_agent/application/replay_service.py`
- Create: `apps/agent/src/factory_agent/application/shutdown_service.py`
- Create: `apps/agent/src/factory_agent/integrations/windows/session_events.py`
- Test: `tests/agent/integration/ipc/test_authentication.py`
- Test: `tests/agent/integration/ipc/test_subscriptions.py`
- Test: `tests/agent/e2e/test_agent_process.py`
- Test: `tests/agent/integration/windows/test_shutdown_fast.py`

- [ ] 写第二实例、错误 SID/PID/digest/version/nonce、慢订阅者、UI 断开、启动中途失败，以及 Windows logoff/shutdown 的 0–2 秒各强杀切点测试。
- [ ] 组合 Task 1–8 的真实 control plane；实现独立 command/event Named Pipe。`SHUTDOWN_FAST` 在 2 秒预算内优先同事务持久化控制命令、writer/control epoch、durable cursor、活动 Attempt/receipt identity；超时写最小 receipt，重启后禁止宣称 PAUSED并直接 reconciliation。
- [ ] 实现统一结构化日志/指标，包含关联 ID、操作、耗时、状态和脱敏错误码；secret fixture 扫描必须为 0。
- [ ] 运行 IPC/process E2E，Expected: `PROC-001` 只生成 Agent 侧 `qualification=PARTIAL` subcheck，UI 关闭不影响 Agent；关机任一切点都无假 PAUSED。
- [ ] 提交：`feat(agent): compose persistent authenticated control plane`。

## Task 10: Phase 1 chaos、性能和阶段 Gate

**Files:**

- Create: `benchmarks/event_ingest/run.py`
- Create: `benchmarks/scheduler/run.py`
- Create: `tests/agent/performance/test_event_ingest.py`
- Create: `tests/agent/performance/test_scheduler.py`
- Create: `tests/support/receipt_plugin.py`
- Create: `docs/architecture/control-plane.md`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/ipc/handlers.py`

- [ ] 实现冻结 workload 的 fake Adapter 基准：burst 12,000、sustained 2,000/s×60s、sparse 1/5/10/s；此阶段只认证 writer/control plane，最终 UI/Runner 指标留待 Phase 6 重放。
- [ ] 运行 `python -m pytest tests/agent -q`、`python -m ruff check apps/agent/src tests`、`python -m mypy apps/agent/src`。
- [ ] 运行 `powershell -NoProfile -File scripts/test.ps1 -Suite control-plane-chaos,control-plane-performance -EmitReceipts`。
- [ ] 从真实 `__main__ → bootstrap → IPC handler → application service → repository` 运行跨层 smoke；独立复核状态、事务、日志、中文注释和恢复，fake inspector 结果只能是 PARTIAL。
- [ ] 提交：`test(control-plane): certify deterministic state and durability gates`。

Phase 1 完成后，控制平面可以在没有真实模型/部署的情况下可靠运行 fake workflow；它不是可交付 MVP，必须继续接入 Phase 2–6。
