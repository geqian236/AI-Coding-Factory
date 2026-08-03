# AI Coding Factory Phase 2 Desktop and IPC Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现常驻 Windows Tauri 桌面应用、全局指挥中心、时间线优先任务详情、真实只读终端弹窗、设置/接入页面和可暂停控制，并确保 UI 崩溃或关闭不影响 Agent。

**Architecture:** React 只消费生成的合同类型并调用 Tauri Rust 白名单 command；Rust 使用两条认证 Named Pipe 与 Agent 通信，一条承载低延迟控制，一条承载 durable event。UI 投影只接受 SQLite COMMIT 后的 taskSeq，断线后以 snapshot + cursor 重建。

**Tech Stack:** Tauri 2、Rust、React、TypeScript、Vite、xterm.js、Vitest、React Testing Library、WebdriverIO/Tauri driver、Playwright 浏览器模式、Windows UI Automation。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文中的短命令只表示 wrapper 参数。

---

## Task 1: 创建 Tauri/React 桌面骨架和独立生命周期

**Files:**

- Create: `apps/desktop/package.json`
- Modify: `pnpm-lock.yaml`
- Modify: `Cargo.lock`
- Create: `apps/desktop/vite.config.ts`
- Create: `apps/desktop/src/main.tsx`
- Create: `apps/desktop/src/app/App.tsx`
- Create: `apps/desktop/src/app/router.tsx`
- Create: `apps/desktop/src-tauri/Cargo.toml`
- Create: `apps/desktop/src-tauri/tauri.conf.json`
- Create: `apps/desktop/src-tauri/src/main.rs`
- Create: `apps/desktop/src-tauri/src/lib.rs`
- Create: `apps/desktop/src-tauri/src/lifecycle.rs`
- Create: `apps/desktop/src-tauri/src/tray.rs`
- Create: `apps/desktop/src-tauri/src/single_instance.rs`
- Create: `apps/desktop/src-tauri/src/autostart.rs`
- Test: `apps/desktop/src-tauri/tests/lifecycle.rs`
- Test: `tests/desktop-e2e/specs/shell-lifecycle.spec.ts`

- [ ] 写失败测试：第二实例聚焦已有窗口；关闭窗口只 hide-to-tray；“退出界面”不停止 Agent；“停止 Factory”走安全停止命令。
- [ ] 运行 `cargo test -p factory-desktop lifecycle` 和 renderer smoke，确认缺实现失败。
- [ ] 实现 single-instance 最先注册、托盘菜单、窗口恢复、自启动入口和 Agent offline 诊断页；Tauri 不直接启动 Claude/Codex 或打开数据库。
- [ ] 使用 Phase 0 的真实 Tauri E2E driver 与 Windows UI Automation 验证窗口/托盘，不以浏览器 DOM 替代原生托盘证据。
- [ ] 提交：`feat(desktop): add persistent Windows shell lifecycle`。

## Task 2: 实现 Rust↔Python 双 Named Pipe 鉴权和事件桥

**Files:**

- Create: `apps/desktop/src-tauri/src/ipc/client.rs`
- Create: `apps/desktop/src-tauri/src/ipc/auth.rs`
- Create: `apps/desktop/src-tauri/src/ipc/framing.rs`
- Create: `apps/desktop/src-tauri/src/ipc/mod.rs`
- Create: `apps/desktop/src-tauri/src/commands.rs`
- Create: `apps/desktop/src-tauri/src/event_bridge.rs`
- Create: `apps/desktop/src/ipc/agentClient.ts`
- Test: `apps/desktop/src-tauri/tests/ipc_auth.rs`
- Test: `tests/integration/windows/test_named_pipe_interop.py`
- Create: `scripts/test-ipc-e2e.ps1`

- [ ] 写错误 SID/PID/executable digest/protocol/nonce、重放 requestId、event pipe 堵塞但 control pipe 仍可用的失败测试。
- [ ] 先用冻结 fixture 完成 Rust/Python 合同测试；真实 interop 明确依赖 Phase 1 Task 9 的集成提交。实现 challenge/nonce、安装会话密钥、当前用户 ACL、客户端身份校验和显式 schema；JS 只能调用 `get_snapshot/subscribe/control/query_artifact` 白名单。
- [ ] event bridge 只转发 committed cursor；连接断开立即丢弃内存投影队列，不影响 Agent。
- [ ] 运行 `powershell -NoProfile -File scripts/test-ipc-e2e.ps1`，Expected: 正常/拒绝/重连场景全部 PASS。
- [ ] 提交：`feat(ipc): add authenticated dual-pipe desktop bridge`。

## Task 3: 实现 durable projection store 和全局指挥中心

**Files:**

- Create: `apps/desktop/src/stores/taskProjectionStore.ts`
- Create: `apps/desktop/src/features/command-center/CommandCenterPage.tsx`
- Create: `apps/desktop/src/features/command-center/TaskSummaryCard.tsx`
- Create: `apps/desktop/src/features/command-center/CommandCenterToolbar.tsx`
- Create: `apps/desktop/src/features/command-center/QueueSummary.tsx`
- Create: `apps/desktop/src/shared/formatters.ts`
- Create: `apps/desktop/src/shared/statusLabels.ts`
- Test: `apps/desktop/src/features/command-center/CommandCenterPage.test.tsx`
- Test: `apps/desktop/src/stores/taskProjectionStore.test.ts`

- [ ] 写 snapshot/cursor、重复/跳号/out-of-order event、RUNNING desired/QUEUED observed 和多任务排队展示失败测试。
- [ ] 实现 snapshot + taskSeq 单调 reducer；检测 gap 时丢弃投影并重取 snapshot，绝不把 prepared event 显示为 durable。
- [ ] 实现全局任务状态、目标阶段、等待原因、耗时/ETA 和最近事件；工具栏预留由 Task 7 提供的“暂停全部/继续全部”组件插槽，不复制控制逻辑。
- [ ] 运行 `pnpm --dir apps/desktop test -- CommandCenterPage taskProjectionStore`，Expected: PASS，无控制台错误。
- [ ] 提交：`feat(desktop): render durable command center projections`。

## Task 4: 实现创建任务和目标终点选择

**Files:**

- Create: `apps/desktop/src/features/task-create/CreateTaskDialog.tsx`
- Create: `apps/desktop/src/features/task-create/TargetStagePicker.tsx`
- Create: `apps/desktop/src/features/task-create/RepositoryPicker.tsx`
- Create: `apps/desktop/src/features/task-create/RiskSummary.tsx`
- Test: `apps/desktop/src/features/task-create/CreateTaskDialog.test.tsx`
- Test: `tests/desktop-e2e/specs/create-task.spec.ts`

- [ ] 写六种 target stage、已有/新仓库、D 路径错误、生产授权摘要和重复提交 requestId 测试。
- [ ] 实现自然语言需求、项目、目标终点、环境/预算/risk summary；不要求用户编写 Claude/Codex Prompt。
- [ ] 生产目标明确展示一次性授权包络和回滚语义，但不得在创建任务时读取秘密。
- [ ] 运行 component + browser E2E，Expected: 六种目标创建请求合同一致，更高 capability 不出现在较低阶段摘要。
- [ ] 提交：`feat(desktop): add target-aware task creation`。

## Task 5: 实现时间线优先任务详情

**Files:**

- Create: `apps/desktop/src/features/task-detail/TaskDetailPage.tsx`
- Create: `apps/desktop/src/features/task-detail/Timeline.tsx`
- Create: `apps/desktop/src/features/task-detail/TimelineFilters.tsx`
- Create: `apps/desktop/src/features/task-detail/MilestoneHeader.tsx`
- Create: `apps/desktop/src/features/task-detail/FindingPanel.tsx`
- Test: `apps/desktop/src/features/task-detail/Timeline.test.tsx`
- Test: `tests/desktop-e2e/specs/task-timeline.spec.ts`

- [ ] 写 phase/barrier/Step/Attempt/receipt 映射、筛选、重连、UNKNOWN_REMOTE_STATE 和 ACTION_REQUIRED 失败测试。
- [ ] 实现时间倒序/顺序切换、按阶段/来源/状态筛选、里程碑、finding 和证据链接；同屏区分 lifecycle、desired、observed、phase、achieved stage。
- [ ] 每条运行节点可以点击打开居中弹窗；没有 durable transcript 的事件只能显示事实摘要。
- [ ] 使用真实 Agent fixture 验证 DOM 与 taskSeq，对断线重放做截图和 trace。
- [ ] 提交：`feat(desktop): add evidence-backed task timeline`。

## Task 6: 实现真实只读终端弹窗

**Files:**

- Create: `apps/desktop/src/features/task-detail/TerminalModal.tsx`
- Create: `apps/desktop/src/features/terminal/DurableTerminal.tsx`
- Create: `apps/desktop/src/features/terminal/ActivitySummary.tsx`
- Create: `apps/desktop/src/features/terminal/ProcessTree.tsx`
- Create: `apps/desktop/src/features/terminal/ProviderFrames.tsx`
- Create: `apps/desktop/src/features/terminal/RuntimeIdentity.tsx`
- Test: `apps/desktop/src/features/terminal/DurableTerminal.test.tsx`
- Test: `tests/desktop-e2e/specs/terminal-modal.spec.ts`

- [ ] 写 sanitized segment、provider public frame、tool event、process identity、cursor replay 和禁止键盘输入测试。
- [ ] 实现 xterm.js 只读重建渲染；明确标注 structured runner 非 TTY 回放，ConPTY 事件另标来源。
- [ ] 五个页签显示脱敏输出、模型公开活动摘要、工具/进程、Provider frame、runtime/container identity；不得推断隐藏思维链。
- [ ] 运行真实 stream fixture 和 Agent E2E，Expected: UI 内容与 committed transcript digest 对应，未脱敏 marker 为 0。
- [ ] 提交：`feat(desktop): add durable terminal evidence modal`。

## Task 7: 实现任务/全局控制和通知收件箱

**Files:**

- Create: `apps/desktop/src/features/control/TaskControls.tsx`
- Create: `apps/desktop/src/features/control/GlobalControls.tsx`
- Create: `apps/desktop/src/features/control/ControlReceipt.tsx`
- Create: `apps/desktop/src/features/notifications/NotificationInbox.tsx`
- Create: `apps/desktop/src/features/notifications/NotificationItem.tsx`
- Create: `crates/factory-toast-helper/Cargo.toml`
- Modify: `Cargo.lock`
- Create: `crates/factory-toast-helper/src/main.rs`
- Create: `apps/agent/src/factory_agent/integrations/windows/toast_helper.py`
- Create: `apps/desktop/src-tauri/src/notification_activation.rs`
- Test: `apps/desktop/src/features/control/TaskControls.test.tsx`
- Test: `tests/desktop-e2e/specs/control-and-notification.spec.ts`
- Test: `crates/factory-toast-helper/tests/notification_contract.rs`
- Test: `tests/agent/integration/notifications/test_toast_helper.py`

- [ ] 写 soft pause、immediate stop、resume、cancel、快速 pause→resume、command CAS conflict、DISPATCH_UNKNOWN 和重启通知去重测试。
- [ ] 控制按钮先展示命令已接受，再按 receipt 展示 PAUSING/STOPPING/QUEUED；不得前端乐观伪造 PAUSED/RUNNING。
- [ ] 应用内 inbox 以 Notification Action 为权威；常驻 Agent 通过签名 Rust toast helper 持有 OS dispatch/reconcile，按 provider tag 查询或未知不重发。Tauri 只负责 inbox 和点击激活，不拥有通知生命周期。
- [ ] 运行 `CTRL-001..002`、`NOTIFY-001` 桌面 E2E，Expected: UI/领域 receipt 一致并生成 `qualification=PARTIAL` subcheck；最终通知场景由 Phase 6 owner 重放。
- [ ] 提交：`feat(desktop): add receipt-driven controls and notifications`。

## Task 8: 实现设置、诊断和首次接入 UI

**Files:**

- Create: `apps/desktop/src/features/settings/SettingsPage.tsx`
- Create: `apps/desktop/src/features/settings/StorageStatus.tsx`
- Create: `apps/desktop/src/features/settings/RuntimeStatus.tsx`
- Create: `apps/desktop/src/features/settings/CompatibilityStatus.tsx`
- Create: `apps/desktop/src/features/onboarding/OnboardingWizard.tsx`
- Create: `apps/desktop/src/features/onboarding/RuntimeProbeStep.tsx`
- Create: `apps/desktop/src/features/onboarding/ProfileLoginStep.tsx`
- Create: `apps/desktop/src/features/onboarding/DockerIntegrationStep.tsx`
- Create: `apps/desktop/src/features/onboarding/ReceiptStep.tsx`
- Test: `apps/desktop/src/features/onboarding/OnboardingWizard.test.tsx`
- Test: `tests/desktop-e2e/specs/onboarding-diagnostics.spec.ts`

- [ ] 写 D 不可用、未知 Docker layout、integration 未启用、profile 未登录、授权取消和诊断-only 模式测试。
- [ ] 实现只读事实展示；迁移、创建 distro、启用 integration、重启 Docker 和 CLI 登录分别要求明确用户动作，不自动执行。
- [ ] UI 只显示 credentialRef/profile status 和 digest，不读取/显示 token/Cookie。
- [ ] 运行 onboarding E2E，Expected: 未认证环境只能诊断，所有按钮的副作用边界与 receipt 明确。
- [ ] 提交：`feat(desktop): add runtime onboarding and diagnostics`。

## Task 9: 验证 Agent 独立生命周期、崩溃重连和 D 盘路径

**Files:**

- Create: `scripts/test-desktop-e2e.ps1`
- Create: `tests/desktop-e2e/specs/agent-survives-ui.spec.ts`
- Create: `tests/desktop-e2e/specs/ui-reconnect.spec.ts`
- Create: `tests/desktop-e2e/specs/global-shutdown.spec.ts`
- Create: `tests/desktop-e2e/support/windows-ui-automation.ps1`
- Modify: `apps/desktop/src/app/App.tsx`
- Modify: `apps/desktop/src/app/router.tsx`
- Modify: `apps/desktop/src-tauri/src/lib.rs`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/ipc/handlers.py`

- [ ] 本任务真实 interop 依赖 Phase 1 Task 9。先注册全部 React route、Tauri command/event bridge 与 Agent handler，并在真实 Agent bootstrap 把 Phase 1 `NotificationPort/notification_service` 绑定到 toast helper，管理 helper 启停、activation 和 provider tag reconcile；组合 E2E 禁止直接实例化 helper。随后启动真实 Agent/fixture task，关闭和杀死 UI，证明 Agent/Broker/任务继续且 taskSeq 增长；完成和 BLOCKED 各只发送一条 OS 通知。
- [ ] 重启 UI，证明只按 committed cursor 重放；重复事件不重复显示、gap 强制 snapshot。
- [ ] 验证退出界面、正常停止 Factory、强制停止产生不同 receipt 和恢复行为。
- [ ] 对 TEMP/TMP/Cargo target/WebView user-data/截图/trace/log 做文件系统快照，所有可控写入必须在 D。
- [ ] 提交：`test(desktop): cover independent lifecycle and reconnect`。

## Task 10: Phase 2 总门禁

**Files:**

- Create: `docs/architecture/desktop-ipc.md`
- Create: `docs/protocols/ipc-v1.md`
- Modify: `scripts/check.ps1`

- [ ] 运行 `pnpm --dir apps/desktop lint`、`typecheck`、`test`，以及 `cargo fmt --check`、`cargo clippy --workspace --all-targets -- -D warnings`、`cargo test --workspace`。
- [ ] 从 Task 9 已冻结的真实组合根操作 UI，穿过双 Named Pipe 到控制平面，再运行浏览器模式和真实 Tauri/托盘完整回归，检查桌面/缩窄窗口、弹窗滚动、文本溢出、控制台错误和失败 IPC；不得在 Gate 临时增加测试专用接线。
- [ ] 审查 React 不直连数据库/CLI，UI 不显示 prepared/raw 事件，所有复杂 Rust/TS 逻辑有中文注释，日志无秘密。
- [ ] 生成带唯一 subcheckId 的阶段 receipt；跨 Runner/完整产品部分全部 `qualification=PARTIAL`，不得用正式 ID 提前生成 FINAL PASS。
- [ ] 提交：`test(desktop): certify shell IPC and durable UI gates`。

Phase 2 完成后形成真实桌面控制界面，但仍使用 fake/控制平面 fixture；只有 Phase 3 接入真实 Runner 后，终端和进程证据才成为完整产品证据。
