# AI Coding Factory Master Design Spec

- 文档版本：`1.1`
- 日期：`2026-08-03`
- 文档类型：Reference / Conceptual Design
- 状态：七节交互设计已获用户确认；技术规格已通过 Codex 独立终审，作为实施前冻结基线
- 产品形态：常驻 Windows 桌面应用
- 源码项目根目录：`D:\codex项目\AI-Coding-Factory`
- 默认应用数据根目录：`D:\codex项目\AI-Coding-Factory-Data`

## 1. 执行摘要

AI Coding Factory 是一个单用户、Windows 常驻、可观察、可暂停、可恢复的 AI 软件研发流水线。用户只输入自然语言需求、选择项目和目标终点，不需要编写 Claude/Codex Prompt。系统自动完成需求结构化、方案设计、Claude Code 开发、确定性验证、Codex 独立审核、自动返工、Git/PR/合并，以及按任务选择执行的 Linux 部署、验收与回滚。

系统不是“调用两个 CLI 的脚本”，也不是播放预设日志的演示页面。它必须满足以下真实性要求：

1. Claude Code 是主要开发执行者。
2. Codex 使用独立只读检出进行方案、代码和发布证据审核。
3. 每个运行状态、终端输出和完成结论都能追溯到真实进程、事件、Git 身份、镜像 digest 或验收证据。
4. 正常执行期间不主动打扰用户；只有达到所选终点或遇到系统无法自行解决的阻塞才通知。
5. 关闭桌面窗口不停止任务；暂停、崩溃、重启和网络中断均有明确恢复语义。
6. 任务可以分别选择做到设计审核、代码审核、PR、合并、测试环境验收或生产环境验收。

## 2. 对原始方案的继承与升级

原始方案正确识别了 Harness Engineering、SWE Agent、AI Software Factory 和多 Agent 审核闭环。本设计保留其核心角色：项目经理/规划、架构设计、Claude 开发、Codex 审核、自动修复、测试和发布。

本设计在用户确认后做出五项关键升级：

1. **桌面应用代替浏览器控制台。** 使用 Tauri 2 构建 Windows App，并提供托盘、登录自启动、单实例和暂停控制。
2. **确定性状态机代替 LangGraph 作为权威调度器。** LangGraph 可以在规划模块内部使用，但任务状态、租约、授权和副作用由可持久化的确定性状态机负责。
3. **真实证据终端代替日志模拟。** React/xterm.js 只消费真实 stdout、stderr、Provider JSONL、进程和工具事件，并能回放经过强制脱敏的来源事件。
4. **生产部署不是固定步骤，也不是被删除的能力。** 它是可选择的 `target_stage`；选择生产验收即形成任务级计划授权，同时保留安全门禁和自动回滚。
5. **创建任务时的目标授权代替最终“Approve Merge”点击。** 系统在用户选择的风险与 capability 包络内自动推进，只在越权、不可逆风险或无法自行恢复时请求处理。

单机首版不引入 Redis 或本地 PostgreSQL。SQLite WAL、文件对象存储和资源租约已经足以支撑单用户桌面调度，减少无必要的常驻依赖。

### 2.1 术语

- **RunSpec**：一次任务的不可变需求、范围、计划、验收和授权输入
- **Gate**：必须以确定性证据通过的阶段门禁
- **Artifact**：提交、测试报告、审核回执、镜像或部署证据等内容寻址产物
- **Receipt event**：记录动作或控制的输入身份、阶段、结果和证据 hash 的不可变追加事件；可变当前状态只能是由事件重建的投影
- **Lease**：带所有者、有效期和心跳的资源使用权
- **Reconciler**：崩溃或断线后核对真实进程、Git 和远端状态的恢复组件
- **IPC**：Inter-Process Communication，进程间通信；本产品使用受限 Windows Named Pipe
- **WSL2**：Windows Subsystem for Linux 2，Windows 上的 Linux 执行环境
- **OCI**：Open Container Initiative，镜像与运行时标准
- **PTY/ConPTY**：伪终端及 Windows Pseudo Console，只用于确需 TTY 语义的命令；Claude/Codex 结构化 Provider 流以 pipe 为权威来源

## 3. 产品目标与非目标

### 3.1 产品目标

- 用户只描述“做什么”，系统负责生成和维护内部 Prompt、RunSpec 与审核材料。
- 支持已有仓库和新项目；所有写任务使用独立 Git worktree、分支和容器。执行 worktree 必须位于专用 Factory WSL2 distro 的原生 ext4 内，该 distro 的 VHDX 物理文件必须位于 `D:\codex项目` 下。
- Claude Code 完成主要编码和结构化返工。
- Codex 对设计、代码和部署证据做独立审核，不能修改候选代码。
- 支持真实时间线、真实终端、Provider JSONL（强制脱敏）、进程身份和可展示的模型活动摘要。
- 支持任务级及全局软暂停、立即停止、恢复和崩溃重建。
- 支持按任务选择目标终点，并将目标终点映射为最小权限。
- 使用统一 SSH + Docker Compose Adapter 支持标准 Linux 主机；阿里云 ECS、腾讯云 CVM 等厂商兼容性必须按认证矩阵实机验证后再声明。
- 支持真实业务验收、数据库检查、稳定观察和计划内自动回滚。
- 所有可控源码、运行数据、日志、缓存、工作区、WSL/Docker 数据和备份位于 `D:\codex项目` 下。

### 3.2 明确非目标

- 不自研基础代码模型。
- 首个完整版本只支持 Factory 专用 WSL2 distro 内由 Compatibility Manifest 锁定的 Claude Code CLI 与 Codex CLI；首次接入必须由用户在 D-backed 隔离 profile 中分别完成登录，不复用或静默复制 C 盘默认 profile。产品不实现模型 API Adapter，也不在产品内管理模型 API key。
- 不自研完整 IDE；只提供任务、证据、终端和控制界面。
- 不展示或编造模型未公开的隐藏思维链。
- 不允许模型直接持有生产 SSH、数据库或 registry 秘密值。
- 不绕过 Git 分支保护、CI、安全门禁或数据库回滚限制。
- 不在首个完整版本中实现 Kubernetes、复杂百分比 Canary 或所有云厂商基础设施 API；这些能力通过适配器扩展。
- 不承诺 Windows 关机或用户未登录时仍在本机 24×7 执行。云端 Worker 需要另立协议迁移规格，不属于首个完整版本。

### 3.3 需求追踪矩阵

| 已确认需求 | 规格落点 | 可验收证据 |
| --- | --- | --- |
| 用户只写需求，不写 Prompt | 4.2、8 | 创建任务不提供 Prompt 输入框；RunSpec Artifact 可查看 |
| Claude Code 主要开发 | 9.1 | Claude CLI session、候选提交和文件变更事件 |
| Codex 独立审核并自动打回 | 9.3 | 独立 reviewer SHA、review receipt、修复循环事件 |
| 正常过程只通知最终结果 | 4.5 | 通知审计中没有阶段性打扰；完成/阻塞通知符合策略 |
| 每个任务选择做到哪一步 | 6 | 六个 target_stage 的端到端验收报告 |
| 时间线优先并能打开真实终端 | 4.3、10 | taskSeq cursor、PID/容器身份、脱敏 Provider JSONL 和 transcript digest |
| 可以查看真实模型活动 | 10.3 | 来源明确的 summary/objective/tool 事件，无伪造隐藏思维链 |
| Windows 桌面常驻和托盘 | 4.1、5、12 | 关闭窗口后任务继续，托盘和重连 E2E |
| 用户可以暂停和停止 | 12 | 任务/全局暂停、立即停止及恢复 E2E |
| WSL2 + Docker 隔离 | 5.3、14 | 独立 worktree/container、无密钥 Verifier 安全测试 |
| 可部署到阿里云、腾讯云等标准 Linux 云服务器 | 15、16、20 | 通用 Adapter 合同测试；厂商名称只在对应实机认证 receipt 存在后展示为“已认证” |

## 4. 用户体验与核心页面

### 4.1 全局指挥中心

桌面应用默认打开全局指挥中心，展示：

- 任务名称、项目、目标终点、当前阶段和真实状态。
- 当前执行者、最近事件、开始时间、持续时间和队列等待原因。
- 运行中、暂停、阻塞、失败、已回滚和已完成计数。
- 全局暂停/继续，以及打开任务详情入口。

关闭窗口只隐藏到系统托盘。托盘菜单包含打开指挥中心、暂停全部、继续全部、退出界面和停止 Factory。退出界面只终止 Tauri UI，后台 Agent 和系统通知继续；停止 Factory 会先全局软暂停、持久化检查点并停止 Agent。存在不可中断的远端关键区时，正常停止必须等待或拒绝；用户另选“强制停止”才按全局立即停止处理并在下次启动核对。

### 4.2 创建任务

用户只填写或选择：

- 自然语言需求。
- 本地项目或新项目位置。
- 基准分支。
- `target_stage`。
- 当目标包含部署时选择测试/生产 `ServerProfile`。
- 可选的端到端时间、估算成本告警和自动修复/重规划/Attempt 上限覆盖值；未覆盖时使用已签名产品策略中的具体有限值，不允许隐式“无限运行”。

系统负责将输入转化为内部 RunSpec；用户不需要编辑模型 Prompt。

### 4.3 时间线优先的任务详情

任务详情默认按单调事件序号展示时间线。事件可筛选来源、阶段、严重级和运行尝试。点击事件或“查看终端”打开居中弹窗。

终端弹窗包含：

- 实时输出。
- 活动摘要。
- 进程树。
- Provider JSONL（强制脱敏）。
- 环境与身份。

受编排终端默认只读。用户可以暂停、继续或立即停止，但不能向 Claude/Codex 受管会话直接键入内容，以免破坏状态机和证据链。Claude/Codex 结构化 runner 的终端是脱敏 Provider/工具流的重建渲染，不承诺真实 TTY 的光标寻址、进度条原地刷新或窗口宽度回放；确需交互式 TTY 的非模型命令才使用 PTY/ConPTY，并在 UI 中明确标记来源类型。

### 4.4 配置与运维页面

- 项目与仓库管理。
- ServerProfile 与一次性服务器接入。
- 凭据引用和登录状态，不显示秘密值。
- 并发、保留期、备份和日志级别设置。
- 审计记录、诊断包和应用版本。

### 4.5 通知策略

系统默认不为规划、编码、测试或审核阶段发送主动通知。用户可以随时打开指挥中心查看中间状态。

系统只在以下情况通知：

- 任务达到所选 target_stage，并附最终结果和证据摘要
- 授权失效、凭据缺失或不可恢复风险导致 `BLOCKED`
- 自动修复循环无法收敛
- 发布失败并完成回滚，或发布与回滚均失败

通知由常驻控制平面发送，而不是由可关闭的 UI 发送。规划完成、Codex 打回、测试失败、自动重试和范围内重规划都不触发系统通知。每次允许通知的状态变化先在状态事务中创建稳定 `notification_actions`，以 `taskId + outcomeVersion + notificationKind` 唯一，并追加 `PREPARED` receipt event；应用内通知收件箱以该 Action 为权威且恰好一条。

操作系统通知不笼统承诺 exactly-once。Adapter 调用前追加 `DISPATCH_STARTED`；若底层支持稳定 tag/idempotency key 和状态查询，重启后按同一 tag 核对或重试；否则调用结果未知时追加 `DISPATCH_UNKNOWN` 且不自动重发，保持应用内收件箱可见，避免潜在重复。只有能证明请求尚未交给 OS 的 `FAILED_KNOWN` 才有限重试；成功只表示 `ACCEPTED_BY_ADAPTER`，不冒充用户已经看到。`BLOCKED`、`FAILED_NEEDS_INTERVENTION` 和凭据/授权问题统一归为 `ACTION_REQUIRED` 通知。

## 5. 总体架构

```text
Windows Desktop
  Tauri 2 / Rust
    - 窗口、托盘、自启动、单实例、更新
    - 认证 IPC 客户端与只读终端渲染
  React / TypeScript
    - Command Center、时间线、终端、设置
            │ 认证 Windows Named Pipe
            ▼
factory-agent.exe / Python 3.12
  - Task API 与确定性 Workflow Engine
  - Scheduler、Lease、Process Supervisor
  - Managed Process Helper、structured pipe / ConPTY、Job Object
  - Claude/Codex Adapters、Tool Broker
  - Policy Engine、Credential Broker
  - Event Journal、Artifact Store、Reconciler
            │
            ▼
WSL2 + Docker Desktop Execution Plane
  - Claude Developer Runner（任务 worktree 可写）
  - Deterministic Verifier（无模型/生产凭据）
  - Codex Reviewer（独立只读检出）
  - Release Runner（短期部署 capability）
            │
            ├── Git / PR / CI
            ├── OCI Registry
            └── Linux Server / SSH / Compose / Nginx / DB
```

### 5.1 桌面壳

Tauri Rust 层只负责 Windows UI 生命周期、认证 IPC 客户端、签名更新和终端渲染。React/Tauri 不直接启动 Claude/Codex，不持有受管进程、ConPTY、Job Object、容器句柄，也不能接触凭据或数据库文件。

自动更新只能在没有 `Run.observed_state=RUNNING/RECONCILING` 且没有 `Step.outcome=UNKNOWN_REMOTE_STATE` 时应用。更新前必须软暂停可暂停任务并备份状态库。停止 Factory 时执行同一安全退出流程；强制停止等同全局立即停止，并在下次启动时执行恢复核对。Windows 注销/关机事件会尽力触发软暂停，但系统强制结束进程时只保证下次启动 reconcile，不保证任务在未登录期间继续。

### 5.2 常驻控制平面

`factory-agent.exe` 是独立用户级后台进程。它持有任务状态机和调度器，使用命名互斥锁保证单实例。由 Agent 内部的 Managed Process Helper 独占 Windows 侧 WSL Broker、structured pipe/ConPTY、Windows Job Object 和容器执行句柄；Broker 再按结构化合同控制 Factory WSL2 内的容器 Runner。Windows Job Object 只证明 Broker 进程树的状态，不能被当作 Linux 容器内进程已经退出的证据。输出必须先完成组帧、脱敏和 durable commit，才能进入可重放时间线并供 UI 订阅。Tauri UI 可以崩溃和重启而不终止任务；UI 重连后按任务事件 cursor 回放。

每个执行 Adapter 必须实现版本化合同：`capabilities`、`start`、`inspect`、`interrupt`、`kill`、`parse` 和可选的 `resume`。`start` 返回不可复用的 Attempt 身份和进程身份；`interrupt/kill` 必须幂等；不支持原会话恢复时，`resume` 明确返回 `unsupported`，由状态机创建新 Attempt。首版容器 Adapter 以 `containerId + execId + processStartIdentity` 为终止目标，完成条件是 Docker inspect/exec inspect 证明目标已退出且受管 process set 为空；连接中断、身份漂移或无法证明清空时只能进入 `LOST/RECONCILING`。CLI exit code 为 0 但终止事件、JSONL 完整性或必需 Artifact 缺失时仍判定失败。

### 5.3 执行平面

每个任务使用唯一 Task ID、Git worktree、分支、运行容器和日志范围。Claude、Codex、验证器和发布器在不同信任区运行，不能共享同一个可写工作区或秘密集合。执行 worktree 在 Factory WSL2 distro 的原生 ext4 文件系统内创建，Git、依赖安装和 Verifier 文件 I/O 都在 WSL 侧完成；桌面 UI 只通过 Agent 读取事件和 Artifact。该 distro 的 VHDX 位于 D 盘数据根，因此满足物理落盘合同，同时避免在 `/mnt/d`/DrvFs 上承担大型依赖树的小文件 I/O。

首版冻结 CLI 的唯一权威执行位置为 Factory 专用 WSL2 distro 通过 Docker Desktop WSL integration 启动的受限容器 Runner：Claude Adapter 调用 manifest 锁定的 Claude Code CLI 结构化非交互模式，Codex Adapter 调用 manifest 锁定的 Codex CLI JSON 模式。控制平面为每个受管容器显式挂载该 distro D 盘 VHDX 内的隔离 profile，并设置 `CLAUDE_CONFIG_DIR` 或 `CODEX_HOME`；profile 未登录、版本未知或 schema 不兼容时只允许诊断并 fail closed。首版不支持直接 WSL 进程型 CLI Runner；Windows Agent 只控制 WSL Broker、容器 identity 和事件，不通过 `\\wsl$` 直接运行 Windows CLI，也不把杀死 Broker 视为容器退出。同一 Attempt 不得跨 Windows/WSL/容器执行位置复用会话。核心流程不依赖自行管理的模型 API key，也不得读取 C 盘默认 profile 来绕过接入向导。

Windows 源仓库与 WSL ext4 worktree 之间使用版本化 Git Object Bridge。Windows 只读 Git Adapter 以 `GIT_OPTIONAL_LOCKS=0` 解析已授权的精确 base SHA，从用户仓库生成位于 D 盘临时区、带 manifest/digest 的 bundle 或 pack；它不得写用户 index、worktree、branch、ref 或 Git 配置。WSL 侧先把对象导入 Factory 管理的 bare mirror，验证对象可达性、base SHA 和 bundle digest，再从 mirror 创建任务 worktree。开发完成后，候选对象通过反向 bundle/pack 导入 D 盘 Factory bare repo；只有已授权交付时，才允许以 CAS 更新用户仓库的 `refs/factory/tasks/<taskId>/candidate`，全过程不得触碰用户 index/worktree。`workspaces` 记录两端 repo identity、bundle digest、base/candidate/checkpoint ref、writer lease 和清理状态；崩溃恢复必须先核对对象与 ref，禁止按目录存在性猜测导入或清理完成。

Tool Broker 优先使用内置 Git、Docker、Browser、SSH 和文件 Adapter，也允许接入 Model Context Protocol (MCP) 工具。每个 MCP 工具必须声明 schema、capability、超时、重试、副作用和脱敏规则；系统不允许注册无限制 shell 或无权限清单的 MCP 工具。

### 5.4 IPC

桌面 UI 与控制平面使用仅当前 Windows 用户可访问的认证 Named Pipe。默认不监听公共 HTTP 端口或 localhost Web 服务。所有 IPC 命令使用显式 schema、版本、权限和 request ID。首版执行位置只有 `local-windows` 与 `wsl-docker`；数据模型预留 `executorId`、`hostId`、`runtime` 和 `protocolVersion` 仅用于本机恢复，不构成云 Worker 协议。云 Worker 必须另立包含双向认证、远程租约和事件传输的新规格。

### 5.5 本地运行时首次接入

首次接入向导与普通任务分离，并按以下顺序执行：

1. 只读探测 Windows/WSL/Docker/CLI 版本、executable digest、backend、磁盘实体路径和登录状态元数据；不得读取凭据文件内容，也不得复制 C 盘默认 profile。
2. 在用户授权后创建或导入 Factory 专用 WSL2 distro，使其 VHDX 位于 `D:\codex项目\AI-Coding-Factory-Data\wsl`；安装 Compatibility Manifest 锁定的 Claude/Codex CLI。迁移、注销、factory reset 或删除任何现有 distro/Docker 数据都属于破坏性动作，必须另行备份和显式授权，不能由向导默认执行。
3. 引导用户在 Factory 隔离 profile 内分别完成一次 CLI 登录；登录完成后只做身份/版本探测和前后文件系统快照，不读取 token、Cookie 或认证原文。
4. 探测 Docker Desktop 的安装模式、活动 backend、container mode、实际 distro 集合、disk image/data-root、BuildKit cache、volume 实体位置，以及 Factory distro 的 Docker Desktop WSL integration 是否启用，分类为 `WSL2_SIMPLIFIED`、`WSL2_LEGACY_DUAL_DISTRO`、`HYPERV_LINUX`、`WINDOWS_CONTAINERS` 或 `UNSUPPORTED_OR_UNKNOWN`。不得只凭版本号或是否存在 `docker-desktop-data` 推断布局。启用 integration 或重启 Docker Desktop 是用户级全局变更，必须展示影响、取得显式授权并生成独立变更 receipt；未启用时不能启动 Runner。
5. 生成 `local-runtime-onboarding-receipt`，绑定 CLI executable digest/版本、环境变量合同、解析后的 profile 路径、Factory distro identity/VHDX/WSL 版本、Docker 布局/backend/data paths、Factory distro integration 状态和 Compatibility Manifest digest，不记录秘密值。

首版只支持 manifest 已认证的 WSL2 Linux-container 布局；simplified 模式不要求存在 `docker-desktop-data`，legacy 模式必须分别核对运行与 data distro。检测到 Hyper-V Linux backend、Windows containers、未知布局或任何 StorageLocationContract 不合规时，App 可以进入只读诊断/迁移向导，但不得创建 worktree、拉取镜像、启动 Runner 或执行写任务，也不得自行切换全局 backend。

## 6. 任务目标阶段与授权

### 6.1 可选目标阶段

| target_stage | 完成条件 | `allowedCapabilitySetId` |
| --- | --- | --- |
| `DESIGN_APPROVED` | RunSpec 和技术方案通过 Codex 方案审核 | `stage-design-v1` |
| `CODEX_APPROVED` | Claude 实现、确定性门禁和 Codex 代码审核通过 | `stage-codex-approved-v1` |
| `PR_READY` | 精确候选提交已 push，并创建 PR | `stage-pr-ready-v1` |
| `MERGED` | CI、review 和分支保护通过，提交已合并 | `stage-merged-v1` |
| `STAGING_ACCEPTED` | 同一镜像 digest 已在测试环境部署并验收 | `stage-staging-accepted-v1` |
| `PRODUCTION_ACCEPTED` | 生产发布、真实验收和 Codex 证据复核通过 | `stage-production-accepted-v1` |

首版签名 `stage-capability-map.v1` 存储下列**完全展开、排序后的数组**；下面的集合表达式只用于便于阅读，Policy Engine 禁止在运行时解释“上述权限”等自然语言：

- `stage-design-v1 = {repo.read, repo.bootstrap, git.local_commit}`；后两项只在 `repository.mode=new` 且目录合同通过时允许进入实际授权。
- `stage-codex-approved-v1 = stage-design-v1 ∪ {worktree.write, test.exec.isolated, build.exec.isolated, network.egress.scoped}`。
- `stage-pr-ready-v1 = stage-codex-approved-v1 ∪ {git.push, pr.create}`。
- `stage-merged-v1 = stage-pr-ready-v1 ∪ {repo.merge}`。
- `stage-staging-accepted-v1 = stage-merged-v1 ∪ {registry.push, registry.observe.scoped, ssh.exec.scoped, remote.write.scoped, remote.observe.scoped, container.inspect.scoped, log.read.scoped, http.check.scoped, service.restart.scoped, nginx.switch, traffic.switch.scoped, acceptance.fixture.write, db.read, db.check, db.backup, db.migrate, db.restore, restore.validation.instance, rollback}`，所有远端 scope 强制绑定 staging 资源。
- `stage-production-accepted-v1` 使用与 staging 相同的 capability 名称全集，但所有远端 scope 强制绑定 production 资源、生产风险上限和预声明 RollbackPlan，不能复用 staging 授权。

目标阶段只给出机械上限；IntentAuthorization 还要与用户实际选择的仓库/环境/数据库/流量策略求交集，ExecutionAuthorization 再取 PlanRevision 节点实际所需子集。未被实际计划使用的 capability 不签发。若包含数据库，`db.backup`、`db.migrate`、`db.restore` 按实际计划分别写入授权快照；迁移必须声明风险分类，恢复演练还必须声明隔离验证实例 capability。

### 6.2 任务级授权

授权与可变计划分成三个对象，避免自动重规划与无人值守执行互相冲突：

1. **IntentAuthorization：**用户创建任务时形成，绑定需求 digest、项目/仓库、基准、目标阶段、`stageCapabilityMapVersion + allowedCapabilitySetDigest`、目标环境、ServerProfile/数据库身份、capability 上限、风险上限、估算成本告警、端到端时间/修复/重规划/Attempt 上限、绝对有效期和用户身份。它不包含尚未生成的计划 digest，也不允许任何预算取无限值。
2. **PlanRevision：**规划完成后生成的不可变 RunSpec 版本，同时保存 `semanticPlanHash` 与 `planRevisionDigest`。前者只标识计划语义，后者标识包含修订谱系和授权关联的完整不可变记录；二者不得混用。
3. **ExecutionAuthorization：**Policy Engine 在具体 Step 输入冻结时，先验证 stage capability 全量上限，再依据版本化 `nodeType → requiredCapabilities` 映射，从有效 IntentAuthorization 与当前 PlanRevision 机械派生实际子集。授权绑定 `(semanticPlanHash, planRevisionId, planRevisionDigest, intentAuthorizationId)`、节点类型、stage/node 映射版本与 digest、该步骤已经存在的 base/candidate SHA 或 digest、资源身份、`executorId + fencing_token + control_epoch`、capability scope、TTL 和消费状态。开发前的 worktree 授权只绑定 base SHA；push、merge 和部署授权必须绑定已经产生并审核的 candidate SHA/digest。它不是新的人工确认；每次副作用执行前必须校验并原子消费或续期。Lease 被更高 token 接管后，旧 executor 的未消费授权即使 TTL 尚未到期也不得转移使用，新 owner 必须重新派生授权。

`semanticPlanHash` 与 `planRevisionDigest` 共用 schema 版本固定的字节 canonicalizer：所有字符串先递归做 Unicode NFC，再按 RFC 8785 JSON Canonicalization Scheme (JCS) 生成 UTF-8；schema 拒绝 NaN、Infinity、超出 I-JSON/JCS 可互操作范围的数字和重复 object key。纳入 `semanticPlanHash` 的完整字段清单为 `schemaVersion`、`goal`、`assumptions`、`scope`、`constraints`、`acceptanceCriteria`、`targetStage`、`repository` 的 mode/root/baseBranch/baseCommit、`workPlan` 的 dagVersion/nodes/barriers、`riskProfile`、`nodeCapabilityMapVersion` 和 `stageCapabilityMapVersion`；排除 `taskId`、`specRevision`、`parentRevisionId`、`intentAuthorizationId`、生成时间、事件/授权/receipt ID、hash/signature 字段。`planRevisionDigest` 对除自身值与签名外的完整不可变 PlanRevision 使用同一 canonicalizer，因此必须包含 task/revision/parent/intent authorization 身份、`semanticPlanHash` 与生成时间。Rust/Python/TypeScript 实现必须通过同一组版本化 golden vectors，包含 `1/1.0`、`-0`、指数、Unicode 组合字符、转义、空值和非法数字拒绝案例。相同语义可共享 `semanticPlanHash`，但不同谱系必须得到不同 `planRevisionDigest`。

选择 `PRODUCTION_ACCEPTED` 表示用户一次性授权在上述包络内执行 push、PR、合并、部署、验收及计划内自动回滚，不再逐步索要形式化确认。范围内的自动修复和重规划可以生成新的 PlanRevision 与 ExecutionAuthorization，并留下父版本和差异审计，不打扰用户。

以下实质变化超出 IntentAuthorization 包络，必须撤销未消费的 ExecutionAuthorization，进入 `BLOCKED/ACTION_REQUIRED`：

- 更换仓库、目标分支、服务器、数据库或环境。
- 提高 target_stage、capability、风险、成本、时间或修复次数上限。
- 引入未授权的不可逆迁移、扩大恢复数据范围或降低既定 RPO/RTO。
- 修改生产域名、DNS、证书、安全组、远端根目录或流量策略的所有权边界。
- 计划实际所需权限超出授权 scope，或执行时的 SHA、digest、ServerProfile revision 与 ExecutionAuthorization 不一致。

降低目标阶段会立即撤销超出新上限的未消费 capability，并在最近安全里程碑收口；已经完成的外部操作不会被自动撤销。用户取消任务会永久撤销该任务所有未消费授权。用户需求存在歧义时，系统先使用仓库事实和项目默认值形成显式假设；只有验收目标无法确定、存在不可逆选择或必须扩大权限时才进入 `ACTION_REQUIRED`。

### 6.3 Capability scope

权限不是布尔值。每个 capability 都绑定资源、操作、TTL、幂等键模板和最大调用次数：

| capability | 最小资源范围 |
| --- | --- |
| `repo.read`、`repo.bootstrap`、`worktree.write`、`git.local_commit` | repo ID、base/candidate SHA、允许路径；bootstrap 仅限选定 D 盘空目录 |
| `test.exec.isolated`、`build.exec.isolated` | image digest、命令 ID、worktree 只读/写范围、CPU/RAM/timeout |
| `git.push`、`pr.create`、`repo.merge` | remote URL、source ref、target branch、reviewed SHA、分支保护结果 |
| `registry.push` | registry/repository、image digest、架构 |
| `registry.observe.scoped` | registry/repository、manifest digest/ref、只读查询操作、credentialRef、响应大小和脱敏规则 |
| `ssh.exec.scoped`、`remote.write.scoped` | ServerProfile revision、host key、允许命令族、remoteRoot |
| `remote.observe.scoped`、`container.inspect.scoped`、`log.read.scoped` | ServerProfile/release/container identity、只读命令 ID、日志时间窗/字段和脱敏规则 |
| `http.check.scoped`、`network.egress.scoped` | 固定 scheme/host/port/path/method、TLS matcher、请求/响应大小和脱敏上限 |
| `service.restart.scoped` | ServerProfile/release/service、最大重启次数、冗余前置条件和恢复检查 |
| `nginx.switch` | server、release ID、upstream config hash、允许回切目标 |
| `traffic.switch.scoped` | Traffic Adapter、目标 revision、候选/旧后端、切换和回切 identity |
| `acceptance.fixture.write` | 环境、fixture namespace、清理期限和最大数据量 |
| `db.read`、`db.check`、`db.backup`、`db.migrate`、`db.restore` | database profile revision、migration checksum、backup ID/目标、风险类、RPO/RTO |
| `restore.validation.instance` | 隔离实例 identity、网络、容量、有效期和强制销毁/清理 receipt |
| `rollback` | 原 release、候选 release、回滚步骤、RPO/RTO 与有效窗口 |
| `target.guard.clear` | resource fingerprint、原 reason/evidence digest、ReconciliationPlan digest、用户身份和一次性 CAS |

`target_stage` 只决定 capability 上限；具体任务只获得 PlanRevision 实际需要的子集。任何未列入 scope 的 shell、网络、文件、Git、数据库或发布动作一律拒绝。

首版冻结 `node-capability-map.v1`，每个 DAG node 必须声明 `nodeType`，Policy Engine 不接受模型自由填写 capability：

| nodeType | requiredCapabilities 上界 |
| --- | --- |
| `PLAN`、`DESIGN_REVIEW`、`CODE_REVIEW` | `repo.read` |
| `BOOTSTRAP_REPOSITORY` | `repo.bootstrap`、`git.local_commit` |
| `IMPLEMENT` | `repo.read`、`worktree.write`、`git.local_commit` |
| `VERIFY` | `repo.read`、`test.exec.isolated`，按依赖计划可附加 `network.egress.scoped`；无 Git/部署写权限 |
| `PUBLISH_PR` | `git.push`、`pr.create` |
| `MERGE` | `repo.merge` |
| `BUILD_ARTIFACT` | `build.exec.isolated`；推送时另需 `registry.push` 与 `registry.observe.scoped` 核对 digest |
| `DEPLOY_STAGING`、`DEPLOY_PRODUCTION` | 对应环境的 `ssh.exec.scoped`、`remote.write.scoped`，按计划可附加 `db.backup`、`db.migrate`、`nginx.switch` 或 `traffic.switch.scoped` |
| `ACCEPT_STAGING`、`ACCEPT_PRODUCTION` | `http.check.scoped`、`network.egress.scoped`、`remote.observe.scoped`、`container.inspect.scoped`、`log.read.scoped`，按计划附加 `db.read`、`db.check`、`acceptance.fixture.write` 或 `service.restart.scoped` |
| `ROLLBACK` | `rollback`、`ssh.exec.scoped`、`remote.write.scoped`，仅在预声明路径附加 `db.restore`、`nginx.switch` 或 `traffic.switch.scoped` |
| `RESTORE_DRILL` | `db.restore`、`restore.validation.instance`、`db.check`；禁止指向生产实例 |
| `RECONCILE_TARGET` | 按目标附加 `remote.observe.scoped`、`container.inspect.scoped`、`log.read.scoped`、`db.read`、`db.check` 或 `registry.observe.scoped`；只有证据齐全且取得专用用户授权后才附加一次性 `target.guard.clear` |

node capability 与 stage capability 两份映射文件都随产品签名并写入 Compatibility Manifest；每项同时冻结 `requiredCapabilities`、`sideEffectClass`、resource fingerprint schema、幂等键模板、完成事实、授权消费点和 retry class。`target.guard.clear` 不属于任何普通 target_stage 上限，只能由独立、用户授权的 ReconciliationPlan 派生。新增 nodeType、扩大任一映射或实际工具 scope 超出表项都需要规格/策略版本升级，不能由 PlanRevision 自行放权。

## 7. 确定性状态机

状态分为五个正交维度，任何实现不得用一个枚举同时表达业务进度、用户意图和执行事实：

| 维度 | 权威字段 | 枚举 |
| --- | --- | --- |
| 任务终局 | `Task.lifecycle` | `ACTIVE`、`SUCCEEDED`、`FAILED`、`CANCELLED`、`ROLLED_BACK`、`FAILED_NEEDS_INTERVENTION` |
| 用户控制意图 | `Run.desired_state` | `RUNNING`、`PAUSED`、`CANCELLED` |
| 实际运行状态 | `Run.observed_state` | `QUEUED`、`RUNNING`、`PAUSING`、`PAUSED`、`STOPPING`、`INTERRUPTED`、`RECONCILING`、`BLOCKED`、`TERMINATED` |
| 当前业务进度 | `Run.phase` | 见 7.1 |
| 已完成里程碑 | `Task.achieved_stage` | `NONE` 加六个 `target_stage` |

`BLOCKED` 是可恢复的实际运行状态，任务仍为 `ACTIVE`；`FAILED_NEEDS_INTERVENTION` 是不可由系统自动收口的任务终态。UI 的“需要处理”是展示类别 `ACTION_REQUIRED`，不是额外状态。

### 7.1 业务阶段与里程碑

`Run.phase` 使用以下业务枚举：

```text
CREATED
PREFLIGHT
BOOTSTRAPPING_REPOSITORY
PLANNING
DESIGN_REVIEWING
PREPARING_WORKSPACE
IMPLEMENTING
VERIFYING
CODE_REVIEWING
PUBLISHING_PR
MERGING
BUILDING_ARTIFACT
DEPLOYING_STAGING
ACCEPTING_STAGING
DEPLOYING_PRODUCTION
ACCEPTING_PRODUCTION
ROLLING_BACK
FINALIZING
```

正常路径按该顺序推进；修复循环允许 `CODE_REVIEWING → IMPLEMENTING`，范围内重规划允许当前开发阶段回到 `PLANNING`，回滚允许任何发布/验收阶段进入 `ROLLING_BACK`。每次非顺序跳转必须记录原因、来源 gate 和新 PlanRevision。

`workPlan` 的每个 node 必须冻结 `businessPhase`、`nodeType`、依赖、是否包含外部副作用、必需 Artifact 和 gate。一个 Run 同时只允许派发当前 phase barrier 内的节点；barrier 内可并行，但不得跨 barrier 提前派发后续业务阶段。`Run.phase` 是 active PlanRevision 中“当前尚未通过的 barrier”的物化投影，只能由 barrier 结果或显式重规划、修复、回滚转换更新，不能按“最靠前/最靠后活动 Step”临时猜测。

令 `knownTerminalOutcome = {SUCCEEDED, FAILED, INTERRUPTED, SKIPPED, CANCELLED}`：

- `barrierSettled(B)` 当且仅当 B 内每个 Step 都是 `phase=TERMINAL` 且 outcome 属于 `knownTerminalOutcome`，不存在活动 Attempt，所有已写 `STARTED` 的外部副作用 receipt 都已取得“完成、权威证明未发生或已核对失败”的事实，并且不存在 `UNKNOWN_REMOTE_STATE`。
- `barrierPassed(B)` 当且仅当 `barrierSettled(B)`，所有 required Step 满足冻结的成功谓词，必需 Artifact 均为 `COMMITTED`，且没有未关闭的 blocking finding。
- 同一 barrier 出现 blocking failure 后停止派发尚未开始的兄弟节点，并以明确原因终结为 `SKIPPED/CANCELLED`；已经派发的副作用节点不得因兄弟失败被假定取消，必须取得完成事实或进入 `RECONCILING`。副作用节点默认排在无副作用前置节点通过之后；确需并行时必须冻结独立资源 fencing 和独立完成事实。
- `UNKNOWN_REMOTE_STATE` 永远不能令 barrier settled。settle deadline 到期只能保持对应 Step 为 `RECONCILING/UNKNOWN_REMOTE_STATE`，将 Run 置为 `BLOCKED` 并显示 `ACTION_REQUIRED`；不得把未知洗成失败、取消、跳过或成功，也不得推进 phase 或派发后续副作用。

每个 barrier 使用 `barrierId = "bar_" + lowercaseHex(SHA-256(JCS(["factory-barrier-v1", runId, planRevisionDigest, businessPhase, barrierOrdinal])))` 作为稳定身份，并持久化到 `phase_barriers`；该编码不得用无长度边界的字符串拼接代替。`barrierPassed`、`Run.phase/active_barrier_id`、`Task.achieved_stage`、可能发生的最终 lifecycle 和对应 `state.changed` 必须在同一 SQLite 事务更新；事务成功谓词还要求目标 barrier 已 passed、无活动 Attempt、无 `UNKNOWN_REMOTE_STATE` 且所有必需 evidence 已 COMMITTED。

通过门禁后，`Task.achieved_stage` 在同一事务中单调更新：

```text
NONE
→ DESIGN_APPROVED
→ CODEX_APPROVED
→ PR_READY
→ MERGED
→ STAGING_ACCEPTED
→ PRODUCTION_ACCEPTED
```

当且仅当 `achieved_stage == target_stage`、对应目标 barrier 已 passed、无活动 Attempt/未知远端状态、必需 Artifact 已提交且没有未解决 blocking finding 时，`Task.lifecycle` 从 `ACTIVE` 转为 `SUCCEEDED`。达到较早里程碑不代表完成更晚目标；`achieved_stage` 不因失败或回滚而倒退。

### 7.2 Step 与 Attempt

- `Step.phase`：`PENDING`、`READY`、`DISPATCHED`、`RUNNING`、`RECONCILING`、`TERMINAL`。
- `Step.outcome`：`NONE`、`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`SKIPPED`、`CANCELLED`、`UNKNOWN_REMOTE_STATE`。
- `Attempt.phase`：`CREATED`、`STARTING`、`RUNNING`、`INTERRUPTING`、`RECONCILING`、`TERMINATED`。
- `Attempt.outcome`：`NONE`、`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`KILLED`、`LOST`、`UNKNOWN_REMOTE_STATE`。

一次派发创建一个全新 Attempt ID；Attempt 永不复用。立即停止、进程丢失或 Agent 崩溃后恢复执行时必须创建新 Attempt，并通过 `supersedesAttemptId` 关联旧尝试。

### 7.3 合法转换

| 当前 `Run.observed_state` | 允许的下一状态 |
| --- | --- |
| `QUEUED` | `RUNNING`、`PAUSED`、`RECONCILING`、`BLOCKED`、`TERMINATED` |
| `RUNNING` | `QUEUED`、`PAUSING`、`STOPPING`、`RECONCILING`、`BLOCKED`、`TERMINATED` |
| `PAUSING` | `PAUSED`、`STOPPING`、`RECONCILING`、`BLOCKED` |
| `PAUSED` | `QUEUED`、`RECONCILING`、`TERMINATED` |
| `STOPPING` | `INTERRUPTED`、`RECONCILING`、`TERMINATED` |
| `INTERRUPTED` | `RECONCILING`、`PAUSED`、`QUEUED`、`TERMINATED` |
| `RECONCILING` | `QUEUED`、`PAUSED`、`STOPPING`、`BLOCKED`、`TERMINATED` |
| `BLOCKED` | `QUEUED`、`PAUSED`、`RECONCILING`、`TERMINATED` |
| `TERMINATED` | 无 |

`desired_state` 只允许 `RUNNING ↔ PAUSED` 和 `RUNNING/PAUSED → CANCELLED`；`CANCELLED` 不可逆。立即停止是将当前 Attempt 终止并把 desired state 设为 `PAUSED` 的控制命令，不等于取消，因此事实核对后仍可由用户继续。对已经满足 PAUSED 谓词的 Run 再点立即停止是幂等 no-op，只追加完成 receipt；若发现残留进程，必须先进入 RECONCILING，确认身份后才转 STOPPING。任务终局只允许：

- `ACTIVE → SUCCEEDED`：达到所选 target_stage。
- `ACTIVE → FAILED`：没有自动恢复路径且没有需要保留的生产未知状态。
- `ACTIVE → CANCELLED`：用户取消，远端事实已核对，清理/保留 receipt 完成。
- `ACTIVE → ROLLED_BACK`：目标未达成，但旧良好版本已恢复并通过回滚验收。
- `ACTIVE → FAILED_NEEDS_INTERVENTION`：发布与回滚都无法建立安全事实。

用户控制命令经认证 Named Pipe 接收，以 `requestId` 幂等并用 expected `state_version` 做 CAS；接受事务只追加不可变 `control_commands`、修改 `desired_state` 并写初始确认 event，后续完成/失败只追加 `control_command_receipt_events`，不能直接修改 Attempt/Step outcome 或伪造 `observed_state`，因此不要求持有 executor fencing token。每个 Run 的 `controlCommandSeq` 严格单调；Attempt 和 ExecutionAuthorization 都绑定其启动时接受的序号。接受 `SOFT_PAUSE`、`IMMEDIATE_STOP` 或 `CANCEL` 时，命令以 `acknowledged_attempt_id` 锁定当前 Attempt，并把该 Attempt 的 `drain_state` 原子置为 `DRAINING`；即使紧接着收到 `RESUME`，drain latch 也不会解除。

Executor 写入分成两个互斥谓词。**NORMAL_WRITE** 要求 `attempt.accepted_control_command_seq == run.control_command_seq`、`desired_state=RUNNING`，并校验当前 `fencing_token + control_epoch + state_version`；只有该模式能开始 tool call、消费 Authorization、执行副作用或提交候选 Artifact。**DRAIN_WRITE** 要求最新阻断命令的 `acknowledged_attempt_id` 匹配 Attempt、该 Attempt 的 token/epoch 仍有效且 `drain_state=DRAINING`；它只允许提交已读到的脱敏 stream/semantic tail、隐藏 checkpoint、Attempt 终止和 reconciliation receipt，严禁新工具调用、副作用、Authorization 消费和候选 Artifact。DRAIN 完成后把 latch 终结为 `DRAINED`，任何后续运行必须创建新 Attempt。

Executor 产生的 `observed_state`、Step/Attempt、Artifact、Authorization 消费和外部副作用写入必须使用上述对应谓词，并与状态事件和 outcomeVersion 在同一数据库事务校验。无资源 lease 的纯调度投影由单实例控制平面使用 `control_epoch + state_version` CAS 更新。非法转换、旧 CAS、旧 fencing token、不满足 NORMAL/DRAIN 白名单、旧控制序号或缺少必需 Artifact 一律拒绝并生成审计事件。

`observed_state=RUNNING` 还必须满足 `desired_state=RUNNING`、没有更新的 pause/stop/cancel 命令、当前 lease 与活动 Attempt/Authorization 绑定同一 executor/token/epoch/control sequence、心跳有效且没有未知远端事实。`PAUSED` 必须满足 desired 为 PAUSED、无活动 Attempt、执行 lease 已释放且不存在未核对 `STARTED` receipt/`UNKNOWN_REMOTE_STATE`。`QUEUED` 表示 desired 为 RUNNING，但尚未取得新 lease/Attempt/授权且不存在待核对远端事实；`RECONCILING` 不得直接跳到 RUNNING，必须先收敛到 QUEUED 再重新派发。

## 8. RunSpec 与规划审核

规划模块根据用户需求、仓库事实和项目规则生成不可变、版本化 RunSpec。RunSpec 至少包含：

```yaml
schemaVersion: 1
specRevision: 3
parentRevisionId: plan-revision-uuid
taskId: task-uuid
goal: 用户目标的结构化表达
assumptions: []
scope:
  include: []
  exclude: []
constraints: []
acceptanceCriteria: []
targetStage: CODEX_APPROVED
repository:
  mode: existing|new
  root: D:\codex项目\example
  baseBranch: main
  baseCommit: full-git-sha
workPlan:
  dagVersion: 1
  nodes:
    - logicalNodeId: implement-core
      businessPhase: IMPLEMENTING
      barrierOrdinal: 1
      nodeType: IMPLEMENT
      required: true
      dependsOn: []
      sideEffectClass: none
      requiredArtifacts: [candidate-commit]
      successPredicateId: predicate://implement-commit/v1
      timeoutMs: 3600000
      retryPolicyId: retry://fixable/v1
  barriers:
    - businessPhase: IMPLEMENTING
      barrierOrdinal: 1
      requiredNodeIds: [implement-core]
      settleTimeoutMs: 300000
      passPredicateId: predicate://barrier-all-required/v1
riskProfile:
  level: low|medium|high|critical
  reasons: []
nodeCapabilityMapVersion: node-capability-map.v1
stageCapabilityMapVersion: stage-capability-map.v1
intentAuthorizationId: intent-authorization-uuid
semanticPlanHash: sha256:canonical-plan-semantics
planRevisionDigest: sha256:complete-immutable-plan-revision
```

PlanRevision 写入后不可修改，修复只能新建子版本；当前执行游标始终指向唯一 active PlanRevision。读取时必须同时重算 `semanticPlanHash` 与 `planRevisionDigest`，任一不匹配均 fail closed。

新项目在规划前进入 `BOOTSTRAPPING_REPOSITORY`：只允许在用户选定的 D 盘空目录创建 Git 仓库、默认分支、基础 `.gitignore` 和可审计的初始空提交；该提交成为可信 `baseCommit`。目录非空、已有 Git 元数据或路径归属不明确时 fail closed，不覆盖内容。

规划层包含两个逻辑角色：Project Manager 将自然语言需求转换为目标、范围、约束、假设和验收条件；Architect 根据仓库事实生成技术设计、文件范围、工作节点和风险计划。两个角色可以共享同一规划模型资源，但必须输出不同 schema，并分别留下事件和 Artifact。

规划 Agent 默认可以使用 Claude 生成候选方案，但 Codex 必须基于仓库事实、RunSpec schema 和冻结 rubric 做独立方案审核。非法 schema、缺失证据和仓库基准漂移一律 fail closed；`DESIGN_APPROVED` 只有在 Policy Engine 判定所有 blocking finding 关闭后才成立。

## 9. Claude 开发、确定性验证与 Codex 审核闭环

### 9.1 Claude Developer Runner

Claude 接收：

- 已批准 RunSpec。
- 当前仓库的最小必要上下文。
- 项目 AGENTS/CLAUDE 等规则。
- 上一轮结构化 finding（如有）。
- 允许的工具和文件范围。

Claude 只写当前任务 worktree。项目脚本、依赖安装、测试和构建不在持有模型登录信息的环境中执行；这些动作由 Tool Broker 转交给无模型凭据的 Verifier。

### 9.2 Deterministic Verifier

Verifier 在干净容器中执行仓库已有命令或 RunSpec 中冻结的等价门禁：

- 单元、服务、API、集成和回归测试。
- lint、格式、类型检查和构建。
- 数据库迁移检查。
- 安全扫描、密钥扫描和依赖检查。
- 浏览器或真实业务 smoke 流程。
- 中文注释和日志覆盖检查。

中文注释与日志不是不可复核的主观门禁。Factory 提供版本化 `CommentPolicyChecker` 与 `LoggingPolicyChecker`：按语言 AST/语法树识别本次变更的公共模块、类、函数、API/IPC、状态机和关键业务入口，并机械检查紧邻中文职责/边界注释与结构化 logger；语言暂不支持 AST 时使用 RunSpec 冻结的确定性文件/符号规则。Codex 只能引用具体文件、符号、规则 ID 和机械 pass condition 提出 blocker，不能仅凭风格偏好 reject。

验证结果必须形成机器可读 Artifact，包含命令、环境、开始/结束时间、exit code、关键摘要、策略检查器版本和内容 hash。

### 9.3 Codex Reviewer

Codex 使用与 Claude 不同的只读检出和运行会话，输入包括：

- RunSpec 与审核 rubric。
- base/candidate 完整 Git SHA。
- 精确 diff 和变更文件列表。
- Verifier 原始证据和报告。
- 相关架构、日志、中文注释和安全规则。

Codex 输出结构化 review receipt：

```json
{
  "result": "pass | reject",
  "reviewedBaseSha": "full-sha",
  "reviewedCandidateSha": "full-sha",
  "findings": [
    {
      "id": "finding-id",
      "severity": "blocker | high | medium | low",
      "file": "path",
      "line": 1,
      "evidence": "可复核事实",
      "problem": "问题说明",
      "passCondition": "修复后可确定验证的条件"
    }
  ],
  "evidenceDigests": []
}
```

Codex 的 `result` 是审核意见，最终 Gate 由模型外的 Policy Engine 计算：

- `blocker` 或 `high` finding 仍为 open 时必须 reject。
- `medium` 是否阻断由 RunSpec 中冻结的 rubric 明确声明；`low` 默认不阻断，但必须进入债务清单。
- `pass` 与 findings、reviewed SHA、schema 或证据不一致时 fail closed。
- finding 只能由新的、引用原 finding ID 的复审 receipt 关闭，不能被原执行者自行删除。
- Codex 审核后 candidate SHA 发生任何漂移，原 receipt 立即失效并重新验证、重新审核。

Codex 不能修改候选代码。审核失败后，系统将 finding 原样结构化返回 Claude，修复后重新运行受影响门禁并再次审核。

`repairLoopUsed` 在失败 gate 触发新的 Claude 自动修复 Attempt 时递增；`autoReplanUsed` 在自动激活子 PlanRevision 时递增。二者在 PlanRevision 变化、暂停/恢复、Agent 重启、Provider 重试或相同失败签名变化后都不重置。默认 `repairLoopLimit=6`、`autoReplanLimit=2`；相同失败签名连续出现 3 次时，若仍有重规划预算，先在 IntentAuthorization 包络内生成新 PlanRevision。任一预算耗尽、仍未收敛或重规划越界时，停止自动派发并撤销未消费副作用授权，保持 `Task.lifecycle=ACTIVE`、设置 `Run.observed_state=BLOCKED`，向用户交付根因、已消耗预算、证据和明确处理选项。提高预算需要新的用户授权，已消耗计数不得清零。

发布后 Codex 只审核证据：证据不完整先进入 `RECONCILING` 补证；真实运行缺陷阻止验收；安全缺陷或已满足 RollbackPlan 触发条件时触发回滚。Codex 不能单凭无法复核的主观意见直接执行生产动作。

## 10. 可观察性与真实终端契约

### 10.1 事件信封

所有 Adapter 先把真实源事件归一化为 `PreparedEventV2`：包含稳定 `ingestEventId`、Task/Run/Step/Attempt 与来源身份、Provider/source 序号、双坐标跨度、接收时间、受管进程身份、已脱敏 payload/digest 和 redaction manifest；它不得读取或预填 Task head，也不得预填尚未分配的 `batchOrdinal/eventId/taskSeq/runSeq/previousEventDigest/eventDigest`。单写者 coordinator 在**一个 SQLite claim 事务**中创建 `ingest_batches(state=CLAIMED, writer_epoch)`、写入所有 per-Task expected head，并按 Task ID 排序原子 CAS 取得该批次涉及的全部 `pendingBatchId`；任一 Task 已有 claim 时整笔事务回滚，禁止留下部分 claim。随后才组装 `PreparedBatchV2` 并给批内事件分配稳定、连续的 `batchOrdinal`；manifest 中按 `batchOrdinal` 排列的 `ingestEventId` 列表是权威顺序。最终 SQLite 引用事务先验证 `pendingBatchId + writerEpoch` 并对每个 Task 的 committed head 做 CAS，再按该顺序逐条物化 `DurableEventV2`：第一条连接批次锚点，后续条目连接事务内刚计算出的前一 event digest；成功时推进 head、标记 batch COMMITTED 并清空全部 pending claim。以下是已提交的 `DurableEventV2` 示例，不是 Adapter 的 prepared 输入：

```json
{
  "schemaVersion": 2,
  "durabilityClass": "authoritative_state | side_effect_receipt | provider_source | derived",
  "ingestEventId": "stable-ingest-uuid",
  "taskSeq": 1842,
  "runSeq": 219,
  "eventId": "event-uuid",
  "taskId": "task-uuid",
  "runId": "run-uuid",
  "stepId": "step-uuid",
  "attemptId": "attempt-uuid",
  "source": "claude | codex | verifier | release | orchestrator",
  "type": "process.started | stream.segment.committed | stream.terminated | model.summary | orchestrator.objective | tool.call | tool.result | state.changed",
  "providerEventId": "optional-provider-event-id",
  "sourceSeq": 271,
  "streamId": "stream-uuid",
  "preparedBatchId": "prepared-batch-uuid",
  "batchOrdinal": 17,
  "sourceTransportSpan": {
    "coordinate": "provider_transport_bytes",
    "start": null,
    "endExclusive": null,
    "mappingPrecision": "frame"
  },
  "sanitizedStreamSpan": {
    "segmentId": "segment-uuid",
    "start": 8192,
    "endExclusive": 9216
  },
  "wallTime": "RFC3339",
  "monotonicTimeNs": 0,
  "ingestedAt": "RFC3339",
  "providerVersion": "cli-reported-version",
  "adapterVersion": "semver",
  "processIdentity": {
    "executorId": "executor-uuid",
    "hostId": "host-fingerprint",
    "runtime": "local-windows | wsl-docker",
    "executableDigest": "sha256:full-digest",
    "pid": 1234,
    "processStartTime": "RFC3339",
    "jobObjectId": "job-uuid",
    "wslDistro": "optional",
    "containerId": "optional",
    "imageDigest": "optional"
  },
  "payload": {},
  "sanitizedProviderFrameDigest": "sha256:full-digest",
  "payloadDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "previousEventDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "eventDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "redactions": [],
  "redactionManifestDigest": "sha256:full-digest"
}
```

事件 schema v2 复用 §6.2 的 NFC + RFC 8785/JCS canonicalizer。`eventId = "evt_" + lowercaseHex(SHA-256(JCS(["factory-event-id-v2", ingestEventId])))`，使崩溃重试仍得到同一身份；`payloadDigest = SHA-256(JCS(sanitized payload))`；`eventDigest = SHA-256(JCS(DurableEventV2 去除 eventDigest 字段后的完整对象))`，因此计算输入仍包含 `previousEventDigest`、payload、redaction manifest 和所有 identity。Task 的 genesis event 使用固定 32 字节全零 predecessor。字段缺省与显式 `null` 不等价，schema 未声明的扩展字段不得混入 v2 hash；事件实现同样必须通过跨语言 golden vectors。

数据库事务为每个 Task 分配全局单调且唯一的 `taskSeq`；`runSeq` 只用于单个 Run 内诊断。UI cursor 使用 `(taskId, taskSeq)`，`wallTime` 只用于显示，不能参与排序。每个 Task 的已提交 `DurableEventV2` digest 形成链；断线后从最后确认的 `taskSeq` 重放。该链只能证明已提交 durable prefix 的顺序和未改写性，不能证明未提交尾部从未存在，也不能单独检测 group commit 整批回滚；它同样不宣称能抵抗拥有本机管理员权限的恶意篡改。

Adapter 必须对 Provider stream 记录来源序号、传输跨度、接收时间、CLI/Adapter 版本和显式 `stream.terminated`。`sourceTransportSpan` 在摄取内存中以原始传输字节精确计数，`sanitizedStreamSpan` 精确指向持久化脱敏字节；脱敏改变长度时两者不得冒充同一坐标。对于 credential/secret/PII，普通事件、UI 和导出不得持久化可泄露原始长度的精确起止，只保存 `providerEventId/sourceSeq` 与诚实的 `field | frame | none` 精度；确需的精确来源水位只能进入受限完整性元数据。重复或乱序 frame 按 `(attemptId, streamId, providerEventId/sourceSeq, ingestEventId)` 去重与核对。

Claude/Codex structured runner 必须通过 pipe 接收 JSONL。只有完整 frame 才能解析和进入持久化链；frame 未完整时 UI 只显示不包含原始内容的活动心跳。xterm.js 展示脱敏流的重建渲染，不宣称 TTY 回放。ConPTY 只用于确需 TTY 的命令，其观察字节受终端宽度和 VT 处理影响，不能作为 Provider semantic event、source transport span 或 digest 的权威来源。缺终止 frame、来源水位空洞、解析错误或 exit 0 但 schema 不完整都不能生成成功事实。

### 10.2 运行状态真实性

只有以下条件同时满足才显示 `RUNNING`：

- `desired_state=RUNNING`，且没有序号更新的 pause/stop/cancel 控制命令。
- 有效资源 lease 归当前 Agent 所有。
- PID、Windows Job Object、WSL process 或 container exec 身份匹配。
- 活动 Attempt、ExecutionAuthorization、`fencing_token`、`control_epoch` 和 `controlCommandSeq` 匹配。
- 心跳在允许窗口内。
- 最近事件没有终止或失联证据。

完成状态还必须具备 exit code、阶段门禁结果和对应 Artifact。缺少事实时显示“正在核对”或“未知”，不能推断成功。

### 10.3 模型活动边界

可以展示：

- 模型主动公开的 reasoning summary。
- 当前 RunSpec 目标和编排器当前步骤。
- 工具调用、工具结果、文件变化、测试状态和 Codex finding。

每条 `model.summary` 必须带 `summaryOrigin=provider_public`、`providerEventId`、`sanitizedProviderFrameRef` 和可重算的 `sanitizedProviderFrameDigest`，并引用已 durable 的脱敏 Provider frame。原始 frame 在脱敏后立即丢弃；对已丢弃原文计算的 HMAC 既不可事后重算，也不得表述为原文审计证明。不能展示或伪造模型未公开的隐藏思维链。若 Provider/CLI 未提供可显示摘要，界面明确显示“无可展示推理摘要”。编排器目标使用独立的 `orchestrator.objective` 事件，不能归入 `model.summary` 或冒充模型思考。

### 10.4 脱敏

真实输出先通过跨 chunk 流式脱敏器，再允许写入 segment、事件库、Artifact 或 UI。脱敏规则集冻结 `maxLookbehindBytes`、`maxPendingBytes` 和非结构化流的 `redactionDecisionTimeoutMs`，首版参考上限分别为 8 KiB、64 KiB 和 500 ms；脱敏器立即输出已证安全前缀，只保留最短不确定后缀。需要无界 lookbehind 的规则不得进入流式模式，只能整帧扫描或 fail closed。空闲或 decision timeout 不会让不确定字节自动变安全；达到时间/容量上限时只能写不可逆终态标记 `[REDACTED:uncertain]` 或 fail closed，绝不能原样释放。终态标记一经提交不可恢复成原文，raw pending bytes 必须清除，UI 与 transcript 必须展示同一 durable 结果。

Structured JSONL 的 `maxFrameBytes` 与 `frameAssemblyTimeoutMs` 属于 Provider/Adapter 合同，必须由 Compatibility Manifest 的慢帧、截断和 burst 样本独立认证，不能使用 500 ms 脱敏等待参数，也不能为满足 UI SLO 而缩短。首字节到完整 frame 闭合只记录 `frame_assembly_ms` 并向 UI 发无内容心跳；完整 frame 闭合并进入脱敏器后才启动 semantic event 的 durable UI SLO。frame 未闭合、超时或超限必须终止该解析路径并生成独立错误事件，不能把脱敏标记注入 JSON 后继续解析。

命中凭据、秘密或隐私模式时显示类型化标记并记录不含原文、低熵 hash 或可推断秘密长度的 redaction manifest。`mappingPrecision` 必须真实标为 `byte | field | frame | none`；credential 默认不高于 field/frame。未脱敏原文不得落盘或展示，系统不能为了“逐字原样”泄露秘密，也不能静默删除造成误解。

每个 `PreparedBatchV2` 由一个不可变 batch manifest 和一个或多个自描述脱敏 stream segment 组成。manifest 至少包含 `preparedBatchId`、按 `batchOrdinal` 排列的全量稳定 `ingestEventId`、每个 Task 唯一的 `expectedCommittedHead` 与 ordinal 范围、segment digest 列表、总事件数/长度、schema/coordinator 版本和完整尾标；segment header/footer 包含 batch ID/manifest digest、Task/Run/Attempt/stream 身份、所含 ordinal 范围、按 mappingPrecision 最小化的来源跨度与精确脱敏跨度、内容/redaction manifest digest、adapter 版本、来源水位和完整尾标。每条 record 还必须有 `batchOrdinal`、长度、类型和 digest，才允许恢复时定位最后一个有效 record。coordinator 对同一 Task 最多保留一个未提交 PreparedBatch；跨 Task 的事件可以交错，但各 Task 的相对顺序必须稳定。

durable prepare 的固定顺序是：把 manifest 与全部 segment 写入同卷 `transcripts\tmp` 临时文件；逐个 flush 内容并交叉校验；逐个原子 rename 到最终不可变内容寻址路径 `transcripts\objects\sha256\<digest>`；再持久化所有目标父目录；最后才允许 SQLite 在一个事务中引用完整对象集合并 COMMIT。Linux 使用文件 `fsync`、rename 后父目录 `fsync`；Windows 使用 Compatibility Manifest 已验证的 write-through/`FlushFileBuffers` 与目录耐久方案。SQLite COMMIT 后不得再次移动对象，逻辑可见性只由数据库中的 COMMITTED 引用决定。比较文件长度与数据库 offset 不能替代 record/header/footer、manifest、digest、前驱和幂等校验。

大对象 Artifact 使用同一“durable prepare → SQLite reference”上位合同。磁盘满、flush/rename/目录持久化/COMMIT 失败时不得暴露半提交对象；COMMIT 前崩溃留下的完整 prepared object 由 Reconciler 幂等补录或隔离。只有逐 record 长度和 digest 均有效时才允许截断到最后一个有效 record 后重新生成新对象；缺少该证据、对象 digest 错误或前驱不一致时必须整体 quarantine。SQLite 已引用但对象缺失/损坏时 fail closed，所有补录、隔离和清理动作必须可重试。

## 11. 持久化数据模型

SQLite 是单机权威状态库，固定 `journal_mode=WAL`、`synchronous=FULL` 和单写者 group commit；权威状态库禁止用 `synchronous=NORMAL` 换取吞吐。Writer 在 `maxBatchEvents`、`maxBatchBytes`、`maxBatchAgeMs` 任一阈值先到时提交，首版参考默认值分别为 200 events、256 KiB 脱敏 payload 和 500 ms；实际参数组合和基准 receipt 写入 Compatibility Manifest。该 age 只约束最老未提交批次的健康等待时间，不是端到端 p95 下界，也不能用“p95 目标减一次 COMMIT 时长”简单推导。进程内只有一个 SQLite writer coordinator 和一个正在组装/提交的全局事务；允许不同 Task 同时存在已 durable 的 prepared segment，但同一 Task 只能有一个未提交批次。若恢复时发现两个批次竞争同一旧 head，CAS 只允许一个获胜；失败批次不得盲提交，必须从其已脱敏 records 按稳定顺序重建带新锚点的新对象，或在无法证明顺序时 quarantine。

事件只有在 SQLite COMMIT 成功后才进入 durable timeline、断线重放和 Gate 证据。`FULL` 的选择基于持久性合同；本机微基准只能证明目标机器上的可行性，不能泛化为所有硬件上 `FULL` 与 `NORMAL` 等速。主要实体：

Writer 前的摄取队列同样必须有界。首版参考硬上限为 `maxIngestQueueEvents=20000`、`maxIngestQueueBytes=32 MiB`、`maxOldestIngestAgeMs=5000`，高/低水位为 75%/50%，并为每个活动 stream 预留最多 8 MiB 的终止排空空间；最终参数和 slow-disk receipt 写入 Compatibility Manifest。达到高水位时停止派发新 Attempt，并通过暂停 pipe 读取对 Provider 施加 OS 背压，降到低水位后才恢复。达到任一硬上限时请求受管 Attempt 在安全点 interrupt，并继续在应急空间内排空到完整 frame/termination；不得把未脱敏字节落盘、静默丢事件或假定成功。若无法得到完整终止事实，必须显式标记 stream incomplete，使本地 Attempt `INTERRUPTED/LOST`，存在外部副作用时进入 `RECONCILING`；任何成功 Gate 都被拒绝。

| 实体 | 职责 |
| --- | --- |
| `projects` | 仓库、默认分支、Provider 和项目规则 |
| `tasks` | 用户需求、lifecycle、target/achieved stage、active revision、outcome/CAS version |
| `plan_revisions` | 规范化 RunSpec、父版本、DAG 版本、semantic plan hash 和完整 revision digest |
| `phase_barriers` | PlanRevision 内稳定 barrier 身份、settled/passed 投影和 gate 证据 |
| `runs` | desired/observed state、phase barrier、cursor、控制序号、预算、执行位置及恢复边界 |
| `steps` | DAG 节点、phase/outcome、依赖、超时、幂等键和重试策略 |
| `attempts` | 不可复用的具体尝试、进程/容器身份、退出和终止事实 |
| `workspaces` | Windows↔WSL Git bridge、bare mirror、refs、writer lease 和清理状态 |
| `event_chain_heads` | 每个 Task 的已提交事件 head、唯一待提交批次和 CAS 版本 |
| `ingest_batches` | group commit 批次、prepared 高水位、阈值和恢复状态 |
| `ingest_batch_task_heads` | PreparedBatch 内每个 Task 的 committed-head 锚点和批内 ordinal 范围 |
| `stream_segments` | 自描述脱敏 segment、来源/脱敏跨度、prepare/commit/quarantine 状态和 digest |
| `ingest_queue_metrics` | writer epoch、当前/峰值 events/bytes/age、高低水位转换和 backpressure 结果 |
| `events` | 追加事件、耐久类别、稳定摄取 ID、任务全局序号、segment 引用和 digest 链 |
| `artifacts` | producer、confidentiality、commit state、路径、大小和 digest |
| `review_findings` | Codex finding 生命周期 |
| `intent_authorizations` | 用户意图、风险/capability 包络、有效期和撤销状态 |
| `execution_authorizations` | 计划/资源绑定、scope、TTL、消费和撤销状态 |
| `resource_leases` | owner、单调 fencing token、control epoch、TTL 和心跳 |
| `server_profiles` | 云无关服务器能力与凭据引用 |
| `deployments` | 发布身份、前后版本和状态 |
| `acceptance_checks` | blocking/advisory 检查及证据 |
| `actions` | 外部动作的稳定 identity、幂等键、资源和 CAS 当前投影 |
| `action_receipt_events` | STARTED/COMPLETED/核对结果的只追加 fencing 证据链 |
| `rollback_receipts` | 回滚触发、动作、结果和恢复身份 |
| `target_guards` | ServerProfile/数据库/registry 等跨任务资源的 intervention 隔离门禁 |
| `notification_actions` | 通知稳定 identity、去重键、payload digest 和 CAS 当前投影 |
| `notification_receipt_events` | 通知准备、派发、接受、已知失败或未知结果的只追加证据链 |
| `control_commands` | pause/stop/resume/cancel 的追加命令账本、request 幂等与确认状态 |
| `control_command_receipt_events` | 控制命令确认、完成或失败的只追加证据链 |
| `budget_clock_events` | 自主执行预算开始/暂停的只追加时钟转换链及权威暂停原因 |
| `credential_refs` | 凭据元数据，不包含秘密值 |
| `audit_records` | 权限、工具和外部动作审计 |

关键字段与约束：

- `tasks(task_id PK, project_id FK, lifecycle, target_stage, achieved_stage, active_plan_revision_id FK, active_run_id FK, state_version, outcome_version)`；`state_version` 每次 CAS 更新递增。
- `plan_revisions(plan_revision_id PK, task_id FK, spec_revision, parent_revision_id FK, intent_authorization_id FK, semantic_plan_hash, plan_revision_digest, dag_version, node_capability_map_version, stage_capability_map_version, created_at)`；`plan_revision_digest` 唯一。
- `phase_barriers(barrier_id PK, run_id FK, plan_revision_id FK, business_phase, barrier_ordinal, required_node_set_digest, settle_timeout_ms, settle_deadline_at, pass_predicate_id, settled, passed, gate_digest, state_version)`；`barrier_id` 使用 §7.1 的 JCS 域分离算法，且 `(run_id, plan_revision_id, business_phase, barrier_ordinal)` 唯一。
- `runs(run_id PK, task_id FK, desired_state, observed_state, phase, active_barrier_id, dag_version, run_cursor, durable_cursor, control_command_seq, repair_loop_used, auto_replan_used, requires_user_action, block_reason_code, budget_accumulated_ms, budget_clock_state, budget_clock_boot_id, budget_clock_monotonic_ns, budget_clock_wall_time, budget_suspension_reason, executor_id, host_id, runtime, protocol_version, recovery_target_phase, state_version)`；预算字段是可从事件链重建的 CAS 投影。
- `steps(step_id PK, run_id FK, plan_revision_id FK, barrier_id FK, logical_node_id, business_phase, node_type, required, side_effect_class, phase, outcome, dependency_hash, required_artifacts_digest, success_predicate_id, timeout_ms, retry_policy_id, idempotency_key, state_version)`；同一 Run/PlanRevision 内 `(run_id, plan_revision_id, logical_node_id)` 唯一。
- `attempts(attempt_id PK, step_id FK, supersedes_attempt_id FK, phase, outcome, executor_id, process_session_id, pid, process_start_time, job_object_id, wsl_distro, container_id, image_digest, exit_code, termination_reason, fencing_token, control_epoch, accepted_control_command_seq, interrupt_command_id FK, drain_state, started_at, ended_at)`；`drain_state` 只允许 `NONE/DRAINING/DRAINED`。
- `workspaces(workspace_id PK, task_id FK, project_id FK, distro_identity, linux_worktree_path, linux_git_common_dir, windows_source_repo, factory_bare_repo, base_sha, candidate_sha, candidate_ref, checkpoint_namespace, writer_lease_key, import_bundle_digest, export_bundle_digest, cleanup_state, state_version)`。
- `event_chain_heads(task_id PK/FK, committed_task_seq, committed_event_digest, pending_batch_id, pending_writer_epoch, pending_claimed_at, state_version)`；claim 事务按 Task ID 排序，将所有涉及 Task 从空 claim 原子 CAS 为同一 batch/epoch；已有 pending batch 时整笔失败。
- `ingest_batches(prepared_batch_id PK, writer_epoch, state, manifest_storage_path, manifest_digest, ordered_ingest_ids_digest, per_task_expected_heads_digest, first_batch_ordinal, last_batch_ordinal, event_count, payload_bytes, oldest_ingested_at, claimed_at, prepared_at, committed_at, recovery_state)`；正常转换只允许 `CLAIMED → PREPARING → PREPARED → COMMITTED`，旧 epoch 的 pre-commit 状态只能由 Reconciler CAS 为 `ABANDONED`；COMMITTED/ABANDONED 均为终态。同一 Task 至多关联一个未提交批次，COMMITTED 引用必须覆盖 manifest 声明的完整 segment 集合。
- `ingest_batch_task_heads(prepared_batch_id FK, task_id FK, expected_task_seq, expected_event_digest, first_batch_ordinal, last_batch_ordinal)`；`(prepared_batch_id, task_id)` 唯一，字段必须与最终对象 header 一致。
- `stream_segments(segment_id PK, prepared_batch_id FK, task_id FK, run_id FK, attempt_id FK, stream_id, storage_path, source_span_summary, sanitized_start, sanitized_end, event_count, size_bytes, content_digest, redaction_manifest_digest, predecessor_digest, complete_footer_digest, commit_state)`；`storage_path` 必须从首次数据库引用起就指向最终不可变内容寻址对象，SQLite 行只在引用事务中以 `COMMITTED` 写入；恢复发现的冲突对象可以追加 `QUARANTINED` 行，不能把临时或未 durable 文件预先登记成可用 segment。
- `events(event_id PK, ingest_event_id, prepared_batch_id FK, batch_ordinal, task_id FK, run_id FK, task_seq, run_seq, source_seq, source_mapping_precision, sanitized_segment_id FK, sanitized_start, sanitized_end, durability_class, schema_version, previous_event_digest, event_digest, payload_digest)`；`ingest_event_id`、`(prepared_batch_id, batch_ordinal)`、`(task_id, task_seq)`、`event_digest` 唯一，事件不可原位更新。
- `artifacts(artifact_id PK, producer_attempt_id FK, media_type, confidentiality, commit_state, storage_path, size_bytes, digest, created_at)`；`commit_state` 只允许 `STAGING → COMMITTED`，只有 COMMITTED Artifact 能满足 Gate。
- `intent_authorizations` 与 `execution_authorizations` 都记录 `issued_at`、`expires_at`、`revoked_at`、`revoke_reason`；IntentAuthorization 另有 `stage_capability_map_version`、`allowed_capability_set_digest`、`autonomous_execution_budget_ms` 和修复/重规划/Attempt 上限。ExecutionAuthorization 绑定 `intent_authorization_id`、`plan_revision_id`、`semantic_plan_hash`、`plan_revision_digest`、stage/node capability map version/digest、run/step/attempt、owner executor、resource fingerprint、capability scope digest、幂等键、fencing token、control epoch、accepted control command sequence、输入 SHA/digest、`max_uses` 和消费状态。
- `resource_leases(resource_key PK, owner_executor_id, fencing_token, control_epoch, acquired_at, heartbeat_at, expires_at, state_version)`；同一资源只允许一个未过期 owner。
- `actions(action_id PK, action_type, idempotency_key, request_digest, resource_fingerprint, current_phase, state_version)` 的 `(action_type, idempotency_key)` 唯一；`current_phase` 只是可 CAS 重建投影。
- `action_receipt_events(receipt_event_id PK, action_id FK, receipt_seq, phase, attempt_id FK, execution_authorization_id FK, executor_id, fencing_token, control_epoch, control_command_seq, remote_identity, result_digest, evidence_digest, previous_receipt_digest, created_at)`；`phase` 只允许 `STARTED/COMPLETED/ABSENT_CONFIRMED/FAILED_RECONCILED`，`(action_id, receipt_seq)` 唯一，记录永不更新。
- `target_guards(resource_fingerprint PK, guard_state, reason_code, evidence_digest, created_by_release_id, created_at, cleared_by, cleared_at, clear_receipt_digest, state_version)`；`guard_state` 只允许 `OPEN/INTERVENTION_REQUIRED`。
- `notification_actions(notification_action_id PK, task_id FK, outcome_version, notification_kind, channel, provider_tag, payload_digest, current_phase, state_version)`；`(task_id, outcome_version, notification_kind)` 唯一，`current_phase` 仅为 CAS 投影。
- `notification_receipt_events(notification_receipt_event_id PK, notification_action_id FK, receipt_seq, phase, attempt_no, provider_identity, result_digest, previous_receipt_digest, created_at)`；`phase` 只允许 `PREPARED/DISPATCH_STARTED/ACCEPTED_BY_ADAPTER/FAILED_KNOWN/DISPATCH_UNKNOWN`，`(notification_action_id, receipt_seq)` 唯一且事件永不更新。
- `control_commands(command_id PK, run_id FK, command_seq, request_id, command_type, actor_id, expected_state_version, accepted_state_version, issued_at, acknowledged_attempt_id, reason_digest)`；`(run_id, command_seq)` 和 `request_id` 唯一，`command_type` 至少包含 `SOFT_PAUSE`、`IMMEDIATE_STOP`、`RESUME`、`CANCEL`，命令接受后不可原位修改。
- `control_command_receipt_events(command_receipt_event_id PK, command_id FK, receipt_seq, phase, attempt_id FK, state_event_id FK, evidence_digest, previous_receipt_digest, created_at)`；`phase` 只允许 `ACKNOWLEDGED/COMPLETED/FAILED`，`(command_id, receipt_seq)` 唯一且记录永不更新。
- `budget_clock_events(run_id FK, clock_seq, transition, suspension_reason, boot_id, monotonic_ns, wall_time, state_event_id FK, previous_clock_digest, clock_digest)`；`transition` 只允许 `RUNNING_STARTED/SUSPENSION_STARTED`，暂停原因只允许 `USER_PAUSED/REQUIRES_USER_ACTION`，`(run_id, clock_seq)` 唯一且事件永不更新。每次转换与 `runs` 预算投影、业务状态和 `state.changed` 同事务提交。

事件耐久按权威来源分级，而不是把所有 semantic event 一概视为普通日志：

| 类别 | 权威持久化与恢复语义 |
| --- | --- |
| 内部权威 `state.changed`、lease、授权 | 与对应领域状态在同一 SQLite 事务提交；状态与事件不得一真一假 |
| 外部副作用 `tool.call/result` | Authorization 消费与 `STARTED` action receipt event 同事务；完成以远端事实和追加的 `COMPLETED/ABSENT_CONFIRMED/FAILED_RECONCILED` event 为权威 |
| Provider semantic/public frame | 引用已 durable 的脱敏 Provider segment；恢复时按 ingest ID 与前驱幂等补录 |
| UI summary、指标等派生事件 | 可重建、不得单独满足 Gate；丢失不把 Attempt 直接判为 `LOST` |

Attempt 进入暂停检查点或终态前，必须先提交截至该安全点的 stream、Provider semantic 事件和 durable cursor。只有成功 Gate 必需且无法从领域状态、receipt、prepared segment 或确定性派生重建的证据缺失，才进入 `RECONCILING` 或 `LOST`；不得因普通派生事件丢失宣告运行失败。

PlanRevision、Event、已消费 Authorization 和 receipt event 追加后不可修改，只能通过后继记录更正；`actions` 等当前投影用 CAS 更新并可从事件重建。Task 默认软删除；删除内容对象前必须确认无其他 Artifact 引用并写审计 tombstone。所有外键启用并做 migration 合同测试；schema 迁移必须先备份、在事务中执行并通过完整性检查。

大体积 transcript、测试报告、截图、SBOM 和发布证据存入 D 盘内容寻址对象目录，数据库保存路径、大小、类型和 hash。

默认数据目录结构：

```text
D:\codex项目\AI-Coding-Factory-Data\
  state\factory.sqlite3
  artifacts\sha256\
  transcripts\tmp\
  transcripts\objects\sha256\
  quarantine\
  logs\
  wsl\factory-runtime.vhdx
    /var/lib/factory/worktrees/
    /var/lib/factory/cli-profiles/
    /var/cache/factory/
  docker\
  vault\
  backups\
  tmp\
```

默认保留策略：

- 任务元数据、最终审核和发布回执保留到用户主动删除。
- 脱敏后的 Provider/终端 transcript 保留 30 天，允许配置 7–365 天。
- `target_stage=CODEX_APPROVED` 的最终候选 commit/ref 作为交付物保留到用户主动删除任务；临时隐藏 checkpoint 和 worktree 默认保留 7 天，最终 receipt 必须给出候选 ref/commit。保留期结束且临时对象无运行/审核引用后才能清理；更晚目标的 worktree 在合并或回滚窗口结束、提交已持久化并完成同样清理检查后删除。
- 被 active/final Gate、review receipt 或 action receipt 引用的 transcript segment 必须提升为相应证据 Artifact 或延长保留期；30 天 transcript 清理不得留下 dangling evidence reference。
- 状态库每日备份 7 份、每周备份 4 份；schema 升级前强制备份。

## 12. 暂停、停止与恢复

### 12.1 软暂停

软暂停先通过 `control_commands` 追加 `SOFT_PAUSE`、将 `desired_state=PAUSED`，立即停止派发新 Step，并在当前 Step 类型定义的安全点写入检查点。控制命令应在 1 秒内持久化确认；进入 `PAUSED` 的实际时间由下表的安全点决定。安全点必须同时提交当前 Attempt 的 stream、Provider semantic event 和 durable cursor，不能只证明文件写过。

| Step 类型 | 安全点 | 默认 grace | 超时/立即停止后的事实 |
| --- | --- | --- | --- |
| Claude/Codex 生成或文件编辑 | 当前完整 Provider/tool frame、脱敏 segment、semantic event 和 durable cursor 已提交；文件操作原子完成并生成隐藏 checkpoint | 30 秒 | 终止旧 Attempt；尽力保留未验证 checkpoint，恢复创建新 Attempt |
| Verifier 本地命令 | 容器 exec 正常退出并收集 exit/报告 | 10 秒 | 按 container/exec identity 终止并 inspect；Broker Job Object 只作补充，Attempt=`KILLED` 后从干净环境重跑 |
| Git push、PR、merge 请求 | 请求发送前；发送后不可强行假定未执行 | 60 秒核对 | 进入 `RECONCILING`，按远端 ref/PR/merge SHA 查完成事实 |
| registry push | manifest 提交前；提交后按 digest 查询 | 120 秒核对 | 不盲重推，先查 manifest digest |
| migration、Nginx 切流 | 远端 journal 的预声明检查点 | 不允许软暂停穿越关键区 | 本地强停后为 `UNKNOWN_REMOTE_STATE`，先核对 journal/真实状态 |

检查点至少包含：

- RunSpec 和授权 hash。
- 当前状态、步骤、attempt 和事件 cursor。
- Git base/candidate SHA、worktree 和脏状态摘要。
- 模型 session ID（若可恢复）。
- Artifact digest、上一 fencing token、资源释放结果和下一步。
- 部署时的 release ID、远端 journal cursor 和活动锁。

可信 Git checkpoint 只能在两次工具调用之间的 Adapter 可观测静止点创建：当前 tool receipt 已终态并 durable、没有活动的受管可写子进程、持有 worktree writer lease，且检查点前后文件稳定性检查一致。该约束是 Adapter 协议，不要求 OS 级全目录锁；发现后台子进程仍写、范围外脏文件、dirty submodule 或文件继续变化时重试或 fail closed。

checkpoint 禁止使用普通 `git stash`，也不在候选分支创建 WIP commit。实现使用 Factory WSL2 中 D 盘 VHDX 承载的唯一临时索引，并对每条 Git 命令通过 `-c core.hooksPath=<受控空目录>` 禁用用户 hooks：设置 `GIT_INDEX_FILE`，执行 `read-tree <previous-checkpoint-or-HEAD>`、`add -A -- <authorized pathspecs>`、`write-tree`、`commit-tree <tree> -p <parent>`，最后用 `update-ref refs/factory/checkpoints/<attemptId> <new> <expected-old>` 做 CAS。这里的 `add -A` 只作用于临时 index 和授权 pathspec，用于保留未跟踪文件；不得修改真实 index、工作树或候选分支。无法受控的 clean filter、范围外脏文件或 submodule 状态不得被静默遗漏。

软暂停完成后释放执行 lease；可以保留无副作用的逻辑排队保留，但不得持续占用模型、CPU、服务器或数据库锁。远端关键区锁只由带 TTL/heartbeat 的 release journal 持有，不能因桌面暂停无限续期。

### 12.2 全局暂停

全局暂停对所有任务发送软暂停请求并停止新任务派发。手动暂停状态跨重启保留，登录自启动不会自动解除。Windows 注销/关机使用独立 `SHUTDOWN_FAST` 控制原因：在系统允许的极短窗口内优先于完整 checkpoint 持久化控制命令、writer/control epoch、durable cursor 和活动 Attempt/receipt 身份，目标预算 2 秒；超过窗口不能宣称软暂停完成，下次启动必须直接进入 reconcile。

### 12.3 立即停止

立即停止追加 `IMMEDIATE_STOP` 命令、将 `desired_state` 设为 `PAUSED`，并按记录的 `containerId + execId + processStartIdentity` 终止当前 Attempt；Windows Job Object 仅用于终止 Broker，不能代替 Docker inspect 的 Linux 侧退出证明。只有 inspect 证明容器 exec 已退出且受管 process set 为空，旧 Attempt 才能永久结束为 `INTERRUPTED/KILLED`；身份漂移或无法核对时进入 `LOST/RECONCILING`，任何继续操作都创建新 Attempt。强杀并确认受管进程退出后，Factory 尽力使用临时 index 创建 `refs/factory/checkpoints/<attemptId>` 下的 `UNVERIFIED_STOP_CHECKPOINT`，保存包括未跟踪文件在内的当前改动；它只作为诊断/人工恢复材料，不能直接成为候选提交或审核基准。自动恢复从最后可信 checkpoint 重建，但不得静默丢弃该未验证引用。

部署中立即停止只证明本地 Release Runner 已停止，不能证明远端动作已停止。当前 Step 记为 `UNKNOWN_REMOTE_STATE`，Run 进入 `RECONCILING`；必须先检查容器、Nginx、数据库和远端 receipt。

### 12.4 取消

取消将 `desired_state=CANCELLED`，立即撤销未消费的授权和 capability，且不可恢复为 RUNNING。系统仍必须核对已派发的远端副作用、执行预声明清理并生成保留说明；只有远端状态已知后，`Task.lifecycle` 才能转为 `CANCELLED`。若无法建立安全事实，则保持 `ACTIVE + BLOCKED` 或转为 `FAILED_NEEDS_INTERVENTION`，不能用“已取消”掩盖未知生产状态。

### 12.5 启动恢复

控制平面启动后按固定顺序执行：

1. 获取单实例互斥锁。
2. 打开并校验 SQLite/WAL。
3. 推进 writer/control epoch，fence 所有旧 writer，并将陈旧 `RUNNING` lease 标记为待核对。
4. 先枚举数据库中的全部 pending batch，再联合扫描 `transcripts\tmp`、最终内容寻址对象和 COMMITTED 引用。旧 writer 已被更高 epoch fence 后逐批分类：完整最终 manifest/object 集合存在时按稳定 ingest ID 与 head CAS 幂等补录；只有临时/部分对象时先整体 quarantine，再写恢复审计并清 claim；完全无对象且没有任何 COMMITTED Event/segment 引用时写 `ORPHAN_CLAIM_NO_OBJECT` 审计后清 claim；存在部分 COMMITTED 引用、batch/claim/epoch 不一致或旧 writer 未被证明失效时保持 `RECONCILING`，禁止清除。清除必须在一个 SQLite 事务中以每个 Task 的 `pending_batch_id + pending_writer_epoch + state_version` 做 CAS，同时把 batch 标为 `ABANDONED` 并处理该 batch 的全部 Task，任一 CAS 失败整笔回滚。竞争同一旧 head 的完整失败对象只能按稳定 records 重建新对象或 quarantine；SQLite 已引用但对象缺失/损坏时 fail closed。
5. 发现 PID、容器、Git、远端 Action identity/receipt event 和数据库锁。
6. 分类为仍存活、停机期间完成、孤儿或未知；未知副作用不得被恢复流程改写成失败或未发生。
7. 从可信 checkpoint 和 committed durable cursor 重建队列。
8. 恢复 UI 事件订阅，只重放已提交事件。

`RESUME` 只追加控制命令并把 `desired_state` CAS 为 RUNNING，不等同于开始执行。若旧 Attempt 仍在 `PAUSING/STOPPING/RECONCILING`，observed state 保持真实事实；只有旧 Attempt 已终结、所有副作用已核对、取得严格更高 fencing token、创建新 Attempt 并签发绑定新 Attempt/token/control sequence 的 ExecutionAuthorization 后，才能 `RECONCILING/PAUSED → QUEUED → RUNNING`。即使 Provider session ID 可复用，也不得复用 Attempt ID。

UI 崩溃时 Agent 继续运行；Agent 崩溃时用户级启动任务按失败策略重启。电脑睡眠、断网或重启后先核对事实；关机或用户未登录时不保证继续执行。默认本地心跳间隔 2 秒、10 秒未见心跳视为失联并进入核对；登录后控制平面恢复目标为 2 分钟内可用、5 分钟内分类所有活动任务。

durable UI 延迟按事件类型使用可实现的起点：structured semantic event 从完整 frame 闭合并进入脱敏器时起算，非结构化 stream 从一个可作安全判定的 record/chunk 进入脱敏器时起算，到对应 SQLite COMMIT 完成并由已连接 UI 完成 DOM/xterm 渲染为止；本机健康状态稳态 p95 目标小于 2 秒。首字节到 frame 闭合单独记录 `frame_assembly_ms`，期间只要求无内容心跳；瞬时 burst 使用“队列有界且 5 秒内排空”的 drain SLO，不混入稳态 p95。系统必须分别记录 `frame_assembly_ms`、`redaction_hold_ms`、`writer_queue_ms`、`oldest_uncommitted_age_ms`、`segment_flush_ms`、`sqlite_commit_ms`、`ipc_dispatch_ms`、`ui_render_ms`、`redaction_pending_bytes` 和保守脱敏计数。断线后从最后确认的 `taskSeq` 重放已提交 durable prefix；不能把序号连续误表述为“未提交尾部从未存在”。

## 13. 并发与资源调度

默认使用资源感知调度：

- Claude 槽位：1。
- Codex 槽位：1。
- Verifier：根据 CPU/RAM 自动，初始上限 2。
- 每仓库合并锁：1。
- 每目标 ServerProfile 发布锁：1。
- 每数据库迁移锁：1。

不同任务可以处于不同阶段并行，但共享风险点必须串行。队列必须显示具体等待原因。生产自动回滚具有最高恢复优先级，但不能抢占数据库迁移关键区。

Claude/Codex 单槽会使多个任务在 IMPLEMENTING/CODE_REVIEWING 阶段排队；UI 不得把资源等待显示成“卡死”。每个 Compatibility Manifest 必须包含三任务并发端到端基准，记录各阶段服务时间、排队时间、资源利用率和完成吞吐；ETA 只能由同一 manifest/任务类别的实测分布推导并显示置信范围，不能把未经测量的“4–8 小时”等估计写成产品承诺。

### 13.1 Lease 与 fencing

调度采用 at-least-once 派发，因此正确性不能依赖“消息只执行一次”。每次取得资源 lease 时，数据库为 `resource_key` 分配严格单调的 `fencing_token`；Agent 每次重建控制权时推进 `control_epoch`。Lease 至少包含 owner executor、token、epoch、TTL、heartbeat、acquired/expires 时间。

普通 Step 状态写入、Artifact commit、Authorization 消费和外部动作必须满足 §7.3 的 `NORMAL_WRITE`，并同时校验当前 `fencing_token + control_epoch + acceptedControlCommandSeq + state_version`；drain 期间只有白名单收尾能使用 `DRAIN_WRITE`。旧 worker 即使稍后恢复，也不能开始工具、提交状态或发布 Artifact。远端 Release Runner 把 token 写入只追加 release journal；创建目录、上传、pull、备份、迁移、启动/停止容器、切流、验收副作用和回滚等每个远端写操作都必须从受控脚本入口读取并原子比较当前 release lock token，不能只在少数关键步骤检查。无法验证 fencing 的目标不允许生产自动化。

本地 lease 过期不等于可以立刻并发接管远端资源。服务器、数据库或 registry Step 在心跳丢失后先进入 `RECONCILING`；只有远端锁、journal 和实际动作均已核对，或原锁已按目标侧规则明确释放，新 executor 才能取得更高 token 并继续。长迁移还必须持有数据库原生 advisory lock，防止仅靠本机 TTL 产生双执行。ServerProfile、数据库、registry 和其他生产目标必须先存在对应 `target_guards` 行且 `guard_state=OPEN`，Policy Engine 才能签发 Deploy/Rollback Authorization，调度器才可取得或续期相关 lease；缺行、状态未知或 `INTERVENTION_REQUIRED` 一律 fail closed，不能由新 Task 绕过。guard 的 resource fingerprint 绑定稳定物理目标（例如 host-key + remoteRoot、数据库集群/库 identity、registry repository），不能因创建新的 Profile revision 或 Task 而变化；revision 另行进入授权 scope。

`INTERVENTION_REQUIRED` 不按 TTL 自动清除。只有显式 ReconciliationPlan 在新 lease/token 下重新读取远端容器、流量、数据库和 journal 事实，形成完整证据包并取得对应用户授权后，才能用 CAS 把 guard 清回 `OPEN`；清除事务必须记录 actor、原 reason/evidence digest、核对结果和 `clear_receipt_digest`。单纯重启 Agent、删除失败 Task、释放 release lock 或创建新 Task 都不得清除 guard。

副作用 Authorization 的消费与稳定 `actions` identity 的创建/核对、追加 `STARTED` action receipt event 必须在同一 SQLite 事务完成；该事务只能由 `NORMAL_WRITE` 进入。消费时同时验证：授权未撤销/未过期且尚有次数，active PlanRevision 与输入身份未漂移，`desired_state=RUNNING`，没有更新的阻断控制命令，以及 Attempt、owner executor、lease、resource、target guard、token、epoch、control sequence 全部匹配。Lease 失效会立即使授权不可消费，即使授权 TTL 尚未到期；TTL 到期也禁止开始新副作用，即使 lease 仍有效。副作用已经 STARTED 后 TTL 到期，只允许核对/安全收口，不允许续期后盲重派。接管必须先核对旧 receipt event，再为新 Attempt 和新 token 派生新授权；授权续期创建追加的后继记录且只能发生在 STARTED 之前，不得原位延长或移植旧授权。

### 13.2 外部副作用幂等

发送方没有收到成功响应时，必须先查询真实完成事实，再决定是否重试：

| 动作 | 幂等键 | 完成事实 |
| --- | --- | --- |
| Git push | repo ID + remote ref + candidate SHA | 远端 ref 精确指向 candidate SHA |
| 创建 PR | repo ID + head SHA + target branch | 同一 head/base 的现有 PR ID 与状态 |
| 合并 | PR ID + reviewed head SHA + strategy | merge receipt 与目标分支 merge SHA |
| Registry push | repository + image digest | registry manifest 可按 digest 读取且内容一致 |
| 数据库迁移 | database profile revision + migration checksum + release ID | 远端 migration journal 与数据库版本/checksum |
| Nginx 切流 | ServerProfile revision + release ID + upstream config hash | active upstream、配置 hash、reload receipt |
| 回滚 | failed release ID + good release digest + rollback plan hash | 活动 upstream/digest 和回滚验收 receipt |

外部动作在本地追加 `STARTED` action receipt event 后才允许派发；完成、确认未发生或失败核对分别追加 `COMPLETED`、`ABSENT_CONFIRMED` 或 `FAILED_RECONCILED`，禁止回填或修改 STARTED。每次追加都要校验当前 token/epoch、Action 前一 receipt digest、remote identity 和结果证据；数据库约束同一 Action 最多一个可接受的 `COMPLETED` 终态投影，`actions.current_phase` 只用 CAS 从追加事件重建。双派发、成功响应丢失、Agent 重启或 stale worker 都必须收敛到一个外部事实、一个稳定 Action identity 和一条可审计 receipt event 链。

## 14. 安全与信任边界

### 14.1 信任区

1. **桌面 UI：**无任意 shell、无数据库直连、无秘密读取接口。
2. **控制平面：**验证策略、路径和 capability，仅保存 credentialRef。
3. **Claude Runner：**当前 worktree 可写，无生产凭据；shell 请求受 Tool Broker 约束。
4. **Codex Runner：**独立只读检出，无候选写权限和生产凭据。
5. **Verifier：**运行仓库脚本，无模型和部署凭据；网络默认关闭，按依赖源临时放行。
6. **Release Runner：**无模型凭据，只在部署阶段获取绑定目标和 TTL 的 capability。

Claude/Codex/Verifier 容器视为不可信执行区。不得挂载 Docker socket、WSL 管理接口、Factory 生产凭据 Vault、用户原工作区、其他任务目录或宿主机根目录；不得使用 `privileged`、host PID/network、任意 device 或新增 Linux capability。默认非 root、`cap-drop=ALL`、只读 rootfs，只把当前任务 worktree 和临时输出目录按所需最小权限挂载。Claude/Codex Runner 只能额外挂载各自的 Provider CLI profile，Verifier/Release Runner 禁止挂载任何模型 profile。若项目门禁确需放宽其中一项，必须在 RunSpec 中单独列为高风险 capability。

### 14.2 凭据

Factory 自管的 SSH、registry、database 和其他部署秘密保存在 `D:\codex项目` 下的 DPAPI 加密 Vault，并使用当前 Windows 用户 ACL。SQLite、RunSpec、日志和 UI 只保存 credentialRef、scope、fingerprint 和 expiry。DPAPI/ACL 只保护静态数据免受其他普通用户或离线读取，不能抵御同一 Windows 用户下的恶意进程或本机管理员；因此生产凭据只由 Credential Broker 在受限 Release Runner 启动时短期解封，不进入开发/审核/验证进程。

Claude/Codex 登录 token、Cookie 等材料由第三方 CLI 自行管理，位于 D-backed Factory WSL profile，不进入 DPAPI Vault，也不宣称具备 DPAPI 保护。profile 目录/文件权限至少为 Linux `0700/0600`，各 Runner 只挂载自己的 profile；标准备份、诊断包、Artifact 导出、日志和项目容器必须排除 profile。丢失 profile 时要求用户重新登录，不能把认证原文复制到 Windows 或普通备份。该边界只能降低误读和跨任务暴露，不能抵御取得当前 Windows 用户、Factory distro 或本机管理员控制权的恶意进程。

- 模型 CLI 使用隔离的 D-backed WSL profile，并按 secret-bearing artifact 管理。
- SSH 优先使用受控 agent/内存句柄；必要临时文件只写 D 盘受限目录。
- Registry 密码通过 stdin 和临时 `DOCKER_CONFIG` 注入，步骤结束后 logout 并销毁。
- 秘密不得进入 argv、Prompt、Compose、普通环境诊断或发布 receipt。
- 部署使用固定 host key、非 root 用户、受限 sudo 和显式 remoteRoot。
- 优先使用短期、可撤销的 SSH/registry/database 凭据；长期凭据必须有独立 scope、轮换和吊销流程。

### 14.3 文件与 Git

- 所有路径先规范化，拒绝 `..`、symlink/junction 越界和非授权卷。
- 一个任务一个 worktree、分支、容器和审计范围。
- 精确暂存文件或代码块；禁止功能提交使用 `git add .` 和 `git add -A`。
- Codex 审核 SHA 与最终候选 SHA 必须完全一致。
- 用户原工作目录和已有改动不被回滚、覆盖或混入任务提交。

仓库源码、Issue、README、测试输出和网页内容都按不可信输入处理。只有明确发现并登记的项目策略文件可以进入指令层；普通代码注释不能提升权限或改变 RunSpec。Tool Broker 和 Policy Engine 必须在模型之外执行权限判断，因此即使模型受到 Prompt Injection，也不能获得额外文件、网络、Git 或部署 capability。

Codex Reviewer 使用只读 sandbox，默认禁止执行仓库脚本。若审核需要验证命令，必须由无模型凭据的 Verifier 执行并返回 Artifact。

### 14.4 容器边界的限制

容器不是绝对安全沙箱。系统仍必须固定基础镜像和 digest、限制 CPU/RAM/PID、限制网络、扫描依赖并及时更新主机。未知高风险仓库默认不能获得生产部署 capability。

Docker Desktop/WSL 管理权限在本机上接近高权限边界，不能被当成普通沙箱。Agent 不向模型暴露 Docker daemon；受信的本地 Helper 只接受结构化容器操作并拒绝宿主挂载、任意 Compose 字段和逃逸型配置。来自仓库的 Dockerfile/Compose 在执行前做静态策略扫描，发布 Compose 由系统模板与冻结配置生成，不直接执行仓库提供的任意生产 Compose。

Named Pipe 除当前用户 ACL 外，还校验协议版本、每次安装生成的会话密钥、客户端 PID、允许的签名/可执行文件 digest 和短期 nonce；命令包含 request ID 并防重放。该机制降低误接入和普通进程冒充风险，但不宣称能抵御已完全控制当前用户会话的恶意软件。更新包必须验证发布签名和 manifest digest，验证失败不得安装。

### 14.5 StorageLocationContract

首版 `storageRoot` 固定为规范化后的 `D:\codex项目`，不是“任意非系统盘”。普通设置不得改成 C/E/F、网络、可移动或未知卷；改变根目录属于需要用户明确批准的规格变更和独立迁移流程。D 盘不存在、不是 Compatibility Manifest 支持的本地固定卷或空间不足时，App 只能进入只读诊断/迁移向导，不得回退到其他卷。

应用对所有可控写入维护机器可读位置清单，并在每次启动和任务预检时核对最终路径、卷 identity、每级 reparse point、junction、symlink、mount point、`SUBST` 映射与剩余空间；预检后写入仍须使用防跟随/句柄约束降低 TOCTOU 路径替换风险。未知、网络、可移动或无法证明实体位置的卷 fail closed。以下项目必须位于 `D:\codex项目`：源码、应用安装目录、Factory Agent/Helper/更新后二进制、安装器缓存、SQLite/WAL、Artifact、transcript、日志、Vault、备份、临时目录、crash dump、updater staging、WebView2 user-data、Claude/Codex CLI profile、Factory WSL distro VHDX、Docker Desktop disk image/data-root、BuildKit cache 和任务容器卷。

Linux 执行任务的权威 worktree 位于 Factory 专用 WSL2 distro 的 ext4，禁止把 `/mnt/d` NTFS 目录作为 Verifier、依赖安装或构建 worktree。该 distro 不能复用 Docker 管理的 `docker-desktop` distro；其 VHDX 实体必须解析到 `D:\codex项目\AI-Coding-Factory-Data\wsl`。Windows 仓库与 WSL worktree 只按 Git base/candidate SHA 和 Git 对象传递，不做双向文件同步、不共享 Windows Git index；receipt 记录 distro identity、WSL/version、VHDX 规范路径和卷 identity。

Agent 为所有子进程显式设置 D 盘或 D-backed Factory WSL 内的 `TEMP/TMP` 和工具缓存目录。Docker/WSL 数据位置不合规时允许 App 展示诊断和迁移步骤，但禁止写任务；Docker/WSL 不能因是全局组件而加入普通白名单。Windows 注册表、事件/通知数据库、系统日志、系统组件二进制更新和操作系统自身临时元数据可以列入不可控白名单，但 Factory profile、VHDX、镜像、缓存和 volume 仍属可控数据。干净机器验收必须用文件系统快照逐项证明位置合同，并披露白名单，不能笼统宣称“C 盘零写入”。

## 15. Linux 云服务器发布

### 15.1 云无关模型

首个完整版本实现 `SshComposeReleaseAdapter`，面向符合 Compatibility Manifest 的标准 Linux + OpenSSH + Docker Engine/Compose 主机。阿里云 ECS、腾讯云 CVM 和其他主机使用同一个 ServerProfile schema，主流程中不能按厂商复制发布分支；只有在该厂商真实临时主机完成部署、验收、故障注入和回滚并生成认证 receipt 后，产品界面和发布说明才能标记“该厂商已认证”。没有实机 receipt 时只声明“通用 SSH/Compose Adapter 预期兼容”。

ServerProfile 只描述能力和引用：

```yaml
id: prod-cn-01
revision: 7
environment: production
providerLabel: aliyun|tencent|other
transport:
  driver: ssh
  host: example.com
  port: 22
  user: deploy
  credentialRef: cred://ssh/prod-cn-01
  hostKeyFingerprints: [SHA256:base64_fingerprint_here]
runtime:
  driver: docker-compose
  osArch: linux/amd64
  remoteRoot: /opt/apps/myapp
artifact:
  driver: oci
  registry: registry.example.com
  repository: team/myapp
  credentialRef: cred://registry/prod-pull
edge:
  driver: nginx
  ownership: existing|external
  publicEndpoints: [https://app.example.com]
dataStores: []
policy:
  supportedStrategies: [recreate, blue-green]
  maxDowntimeSeconds: 30
  retainGoodReleases: 2
```

ServerProfile 不允许保存任意 shell 模板或秘密值。

首版只实现 `existing` 与 `external`；历史配置、导入文件或模型计划出现 `managed` 及其他值时必须 schema 校验失败并进入接入修订，不能静默按 `existing` 执行。

普通应用发布使用专用非 root deploy 用户和固定 `remoteRoot`，不得挂载 Docker socket 到应用容器。若 deploy 用户属于 `docker` 组，威胁模型按远端 root 等级处理并限制账户只用于发布；更严格环境可使用受限 rootless Docker Adapter。`sudo` 只允许首次接入时调用明确列出的 Bootstrap 操作，普通发布阶段不得获得通用 sudo shell。

`remoteRoot` 必须是规范化绝对 POSIX 路径并位于 ServerProfile 允许前缀；release ID、镜像名、域名、端口和文件名都按独立 schema 校验。SSH Adapter 只能调用版本化的固定操作或上传已校验 digest 的受控脚本，并以结构化参数传值；不得把仓库文本、ServerProfile 字段或模型输出拼进远端 shell 字符串。

`edge.ownership=existing` 时，Factory 只允许写一个经首次接入授权的独占 include 文件；主配置增加 include 只能在 BootstrapPlan 中执行一次，Factory 不修改证书、其他站点、主配置或第三方文件。reload 前必须保存并比较规范化的完整 `nginx -T` digest、Factory include digest 和预期差异，发现意外漂移即 fail closed 且不 reload；reload 后再次核对并生成 receipt，后验漂移进入 `UNKNOWN_REMOTE_STATE/RECONCILING`，不能标记“无影响”或成功。

`existing` 模式无法阻止 certbot hook、人工编辑或其他工具在最终检查与 reload 之间写入；Factory 发起的 reload 会重新加载磁盘上全部配置，可能把第三方未完成编辑一并激活。该 TOCTOU 残余风险必须绑定 ServerProfile revision 由用户显式接受，Factory 的发布锁不得宣称能约束外部写者；不能接受时必须使用 `edge.ownership=external` 和独立 Traffic Adapter。

### 15.2 首次服务器接入

首次接入是与应用发布分离的一次性流程：

1. 用户填写主机、SSH、域名和环境。
2. 系统只读发现 OS、架构、Docker/Compose、Nginx、端口、磁盘、`MemAvailable`/swap、容器资源水位和现有 ownership。
3. 若缺少前置组件，生成明确的 BootstrapPlan。
4. 用户对安装 Docker、创建用户、修改 Nginx/防火墙，以及在 `remoteRoot/.factory/` 下创建受控 `locks/`、`journals/` 目录等系统变更进行一次性授权。
5. 完成后验证并保存新的 ServerProfile revision，并为该 revision 引用的 ServerProfile、数据库和 registry resource fingerprint 原子初始化 `target_guards=OPEN`；已有 guard 只能按 CAS 读取，绝不能用接入流程覆盖 `INTERVENTION_REQUIRED`。

普通应用任务不能隐式扩大为服务器初始化。

### 15.3 不可变 DeployPlan

部署前冻结：

- Git SHA、目标分支、PR/merge/CI 证据。
- `repo@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef` OCI digest、目标架构、SBOM 和来源证明。
- ServerProfile ID、revision 和 fingerprint。
- 非密配置 hash、Compose/Nginx/迁移清单 hash。
- 有序步骤、超时、幂等键、锁和失败策略。
- AcceptancePlan 与 RollbackPlan。
- 当前 ExecutionAuthorization、release fencing token 与远端 journal schema 版本。

生产执行器拒绝 tag-only 镜像和远端现场构建。

每次发布在首次接入预建的 `remoteRoot/.factory/journals/<releaseId>.jsonl` 建立只追加 journal，发布材料仍放在 `remoteRoot/releases/<releaseId>/`。每条 journal 记录 `stepSeq`、fencing token、输入 hash、开始/完成状态、远端身份、结果 hash 和前一条 digest；任何远端写步骤的 STARTED、迁移、切流和回滚记录必须 `fsync` 后才能推进。Release Runner 重连时先校验完整 journal、活动锁与真实容器/数据库/Nginx 状态，不能仅相信本地缓存。

### 15.4 发布主链

1. 在 Verifier 中构建一次 OCI 镜像并生成 SBOM/manifest。
2. 推送 registry，解析并冻结 digest。
3. 只读 SSH 预检 host key、版本、架构、磁盘、端口、内存/swap/容器水位、当前 release、锁、`target_guard=OPEN` 和配置漂移；此步不得创建目录、上传或 pull。
4. 通过首次接入安装的固定原子锁 helper，在 `remoteRoot/.factory/locks/` 获取带 TTL、heartbeat 与 fencing token 的 release lock，并在固定 journal 追加、`fsync` STARTED；获取失败不得产生其他远端写入。
5. 在持锁脚本内创建唯一远端 release 目录，上传带 hash 的 Compose/Nginx/迁移材料。
6. `docker pull` by digest，并用 inspect 核对实际身份。
7. 在备份/迁移前再取得数据库原生 migration lock，创建/绑定精确 backup ID，并验证数据库身份、时间、大小、checksum、加密、可读性、保留期和异故障域位置；这一步不是每次发布都执行完整恢复演练。未验证、不可读取或与数据库同故障域的副本不能算有效备份。
8. 用同一镜像 digest 运行一次性 migration job；checksum/release ID 写入数据库 migration journal，重复执行只允许核对同一结果。Migration Runner 只获得目标数据库的 scoped 账户和目标网络访问，无模型凭据、Docker socket、云元数据或其他环境网络权限。
9. 校验 Compose 配置并启动候选栈。
10. 候选内部验收通过后执行 `nginx -t`，按 ownership 合同校验完整 `nginx -T` 与 active config hash，再原子切换 upstream 并 reload；`existing` 模式执行前后漂移核对，`external` 模式不得自行切流，必须由显式 Traffic Adapter 完成或进入 `BLOCKED`。
11. 执行公网、真实业务、日志、数据库、重启和 soak 验收。
12. Codex 只读复核证据包。
13. 写不可变 release receipt、核对 journal 尾摘要、释放 migration/release lock 并保留回滚版本。

测试环境默认 `recreate`。生产环境在状态外置、迁移向后兼容且资源足够时默认 blue-green 0/100。blue-green 预检必须把 `MemAvailable`、swap、宿主保留量、旧栈 container limit/当前值/观测峰值、候选 limit、migration/backup 峰值和安全余量公式冻结进 DeployPlan；缺 limit/峰值证据或余量不足时不得启动候选栈。重叠运行期间持续监测宿主内存、OOM/restart 和旧栈健康，越阈值立即停止候选并核对旧栈；只有任务已授权 recreate 且停机预算允许时才能降级，否则进入 `BLOCKED`。单机 blue-green 只是并行版本切换，不构成故障域冗余或高可用。复杂百分比 Canary 延后到具备可靠指标后实现。

## 16. 验收与回滚

### 16.1 AcceptancePlan

每个检查包含 `checkId`、类型、阶段、blocking/advisory、timeout、有限重试、期望 matcher、证据采集、脱敏、测试副作用清理和失败回滚策略。

至少覆盖：

1. Git SHA、release ID、运行 digest、配置 hash 和 schema version。
2. 容器 running/healthy、restart delta 和资源水位。
3. 内网/公网端点、TLS 和 Nginx 活动 upstream。
4. 专用测试账号或 fixture 的真实业务流程，记录 trace ID 并清理副作用。
5. 发布窗口新增 error/exception、请求链路和敏感信息泄漏。
6. 迁移 checksum、schema、连接、FK/完整性和关键业务不变量。
7. 候选冷启动；只有生产有冗余时才执行滚动重启。
8. 明确时长和错误率、延迟、重启阈值的 soak。

所有 blocking 检查必须通过，必检项不能以 `skipped` 冒充成功。

生产验收只有在 Codex 证据复核 receipt 与活动 release digest、Git SHA、配置 hash 完全一致时才能更新 `PRODUCTION_ACCEPTED`。证据缺失先补证；运行缺陷阻止验收；安全缺陷或命中预声明回滚阈值立即执行 RollbackPlan。真实业务 fixture 由 Acceptance Runner 通过专用 credentialRef 使用，不能传给模型，执行后必须以独立检查证明副作用已清理。

控制平面本地恢复目标为 RTO 2 分钟可用、5 分钟内完成活动任务分类。在 Compatibility Manifest 支持且底层存储兑现 durable flush 的前提下，本地 RPO 为 0 个已提交 SQLite 事务；完整且校验通过的 prepared stream 可在恢复时幂等补录，尚未形成完整 frame/prepare 的内存尾部允许丢失。`taskSeq`/digest 连续不能证明尾部从未存在；任何成功 Gate 必需的关键证据缺失都进入 `RECONCILING`，不能推断成功。每个远端 RollbackPlan 另行声明业务 RTO/RPO，系统只能承诺并验证该任务冻结的数值，不能用产品默认值替代业务承诺。

### 16.2 RollbackPlan

发布前必须记录旧良好镜像 digest、Compose/Nginx/config hash、原 upstream、数据库版本、备份 ID、RPO/RTO、触发条件、回切顺序和回滚后验收。

数据库回滚类型必须是以下之一：

- `none`
- `schema_backward_compatible`
- `reversible_down`
- `restore_required`
- `forward_fix_only`

默认采用 expand-contract。破坏性 contract migration 在回滚窗口结束后作为独立任务执行。完整恢复演练在首次数据库接入、周期 receipt 到期、`restore_required`、数据库/备份工具大版本变化、备份格式/密钥/存储目标变化以及破坏性迁移前强制执行；演练只能恢复到隔离验证实例，并检查 schema、关键行数、业务不变量和实际 RTO/RPO，禁止覆盖生产库。

Restore drill receipt 必须绑定 DatabaseProfile revision、引擎版本、备份配置 hash、密钥 fingerprint、backup ID 或等价样本、隔离验证实例、验证器版本、结果和 `expiresAt`；任一绑定项变化或 receipt 过期即失效。`restore_required` 只有在写入可冻结、RPO/RTO 已授权、持有有效 drill receipt、`db.restore` capability 和隔离验证资源 capability 时才可自动执行；`forward_fix_only` 不得宣称支持自动数据回滚，发布前必须把该限制作为风险门禁。若计划不能建立安全恢复路径，生产 ExecutionAuthorization 不得签发。

失败语义：

- 数据库写入与切流前失败：删除候选；只有确认没有远端副作用后才能写“生产无影响”。
- migration 已执行但切流前失败：生产应用流量虽未切换，schema 或数据已经可能变化；必须核对 migration journal 和旧版本兼容性，并按 `schema_backward_compatible`、`reversible_down`、`restore_required` 或 `forward_fix_only` 的预声明路径处置，不能标记“无影响”。
- 切流后失败：切回旧 upstream/digest，并执行回滚验收。
- schema 不兼容：停止写入，执行预声明的数据处置路径。
- 回滚也失败：进入 `FAILED_NEEDS_INTERVENTION`，保留容器、日志、journal 和证据；按预声明策略冻结或隔离流量/写入。在释放临时 lease 前，必须与 lifecycle/receipt event 同一 SQLite 事务把涉及的 ServerProfile、数据库、registry 等 `target_guards` 原子写成 `INTERVENTION_REQUIRED` 并绑定 release/evidence digest；guard 不随 lease TTL 释放，后续任何 Task 都不能取得这些目标的 Deploy/Rollback Authorization 或 lease。只能按 §13.1 的显式核对流程清除 guard，不得留下无限期僵尸锁，也不得做破坏性清理。

回滚成功必须同时证明旧 digest/upstream 已恢复、数据库处于 RollbackPlan 允许的版本、blocking 回滚验收通过且新增错误未超阈值；否则不能标记 `ROLLED_BACK`。

## 17. 错误分类与重试

IntentAuthorization 必须冻结绝对 `expiresAt` 与 `autonomousExecutionBudgetMs`。`expiresAt` 是安全有效期，不因暂停而延长；自主执行预算从 PLANNING 到 target stage，包含容量排队、Provider 退避、自动 reconciliation、系统睡眠、Agent 停机和 soak。只有 `desired_state=PAUSED`，或持久化 blocker 明确写入 `requires_user_action=true` 时，预算时钟才能以 `USER_PAUSED/REQUIRES_USER_ACTION` 原因暂停；`ACTION_REQUIRED` 只是 UI 类别，不能作为计时依据。

每次运行/暂停转换都追加 `budget_clock_events`，并与累计投影和状态事件同事务提交。同一 boot 内使用 Compatibility Manifest 认证的、包含系统睡眠时间的 monotonic source 作为预算增量权威值；事件同时记录 `boot_id + monotonic_ns + UTC wall_time`，并比较 wall delta 与 monotonic delta，二者偏差超过冻结的 `maxClockForwardJumpMs` 或 wall 回拨超过默认 1 秒的 `maxClockRollbackMs` 时进入 `BLOCKED/CLOCK_ANOMALY`，不能赠送预算。跨 boot 无连续 monotonic 证据：若上一状态为 RUNNING，则按两个 wall anchor 的非负差保守计入停机时间；合法长时间关机本身不算 forward jump，但 wall anchor 回拨超过阈值仍 fail closed，向前差会计入预算并受绝对 `expiresAt` 限制。重启/重规划不重置累计值。每次派发前必须证明剩余预算能覆盖节点 timeout、最大退避和必需 soak；预算耗尽时停止新派发，保持 `ACTIVE + BLOCKED/ACTION_REQUIRED`，已进入外部关键区的动作只能安全收口或 reconcile。

| 分类 | 示例 | 默认动作 |
| --- | --- | --- |
| `TRANSIENT` | 能证明尚未越过副作用派发边界的短暂网络错误 | 在剩余预算内最多 3 次，jitter |
| `PROVIDER_RATE_LIMIT` | 有明确 Retry-After 的短时限流 | 等待不超过自动等待上限且预算足够时进入带唤醒时间的 QUEUED；不忙轮询 |
| `PROVIDER_QUOTA_EXHAUSTED` | 订阅额度耗尽、重置未知或超出预算 | `ACTIVE + BLOCKED/ACTION_REQUIRED`，创建一个去重 Notification Action，不做三次快速重试 |
| `FIXABLE` | 测试失败、Codex finding | 结构化返回 Claude 修复 |
| `CAPACITY` | 模型、CPU、仓库或服务器锁 | 排队并显示原因 |
| `AUTH_POLICY` | 凭据失效、授权不匹配 | Fail closed，进入 BLOCKED |
| `UNKNOWN_STATE` | 崩溃、请求发送后的 SSH/网络中断或副作用不明 | 保持 RECONCILING，先查完成事实，禁止盲重试 |
| `IRREVERSIBLE_RISK` | 不可逆迁移、备份无效 | 停止并保留证据 |
| `INTERNAL_BUG` | 控制平面异常 | 只暂停受影响任务，生成脱敏诊断包 |

错误分类取决于副作用派发边界，不取决于异常名称；同一个 SSH/network 错误在请求发送前可为 TRANSIENT，请求可能已被远端接收后必须为 UNKNOWN_STATE。`UNKNOWN_REMOTE_STATE` 只有在完成事实已知，或能权威证明未发生且目标支持同一幂等键重放时，才允许新 Attempt + 新授权重试。任何可能产生外部副作用的步骤只有在具备幂等键或完成事实核对后才能重试。

在只使用订阅制本机 CLI 的首版中，Provider 报告的 `total_cost_usd` 等字段只能作为等价 API 估算和告警，不能宣称是真实账单或硬成本门禁。可机械执行的硬门禁是绝对授权有效期、端到端 wall-clock、Attempt、修复和重规划预算。

## 18. 日志与审计

正式实现必须使用结构化 logger，禁止使用零散 `print`/`console.log` 代替日志。日志至少包含：

- request/task/run/step/attempt/trace ID。
- 模块、操作、目标、参数摘要、开始/结束、耗时和状态。
- 模型/CLI 名称和版本、session ID、工具调用及 exit code。
- Git base/candidate SHA、镜像 digest、ServerProfile revision。
- 资源 lease、重试、暂停、恢复、回滚和授权拒绝。
- 脱敏后的错误类型、错误码和失败原因。

日志不能包含 Prompt 中的秘密原文、token、密码、私钥或完整敏感 payload。日志文件、transcript 和 crash dump 位于 D 盘数据根并按保留策略轮转。

## 19. 中文注释标准

实现阶段新增或修改的模块、类、公共函数、API/IPC 命令、状态机、调度器、工具、模型 Adapter、存储、恢复、授权、部署和回滚逻辑必须有中文注释。注释解释职责、输入输出、副作用、边界、重试和“为什么这样设计”；简单 import、赋值和显而易见 getter 可以不注释。

中文注释和日志覆盖是 Codex 审核 blocker，不是交付后补做的文档工作。

## 20. 产品验证体系

### 20.1 测试层次

- Unit：状态转换、授权、路径、脱敏、计划 hash 和回滚判定。
- CLI Contract：Claude stream-json、Codex JSON 事件和版本 schema 漂移。
- Integration：进程树、Named Pipe、SQLite/WAL、worktree、Artifact 和恢复。
- Desktop E2E：托盘、时间线、真实终端、暂停、重连和设置。
- Security：路径越界、symlink/junction、命令注入、恶意仓库、密钥泄漏和 IPC 鉴权。
- Chaos：杀 UI/Agent/Runner、断网、陈旧锁、截断 JSONL、电脑重启和 SSH 中断。
- Performance：burst、持续速率、稀疏流、队列斜率、脱敏等待、segment flush、SQLite COMMIT、IPC 和真实 UI 渲染。
- Release E2E：Compose、Nginx、迁移、blue-green、真实验收和回滚。
- Packaging：D 盘安装、升级、备份恢复、卸载和数据保留。

### 20.2 必须通过的代表流程

| 测试 ID | 场景 | 必须证明的结果 |
| --- | --- | --- |
| `STAGE-001..006` | 分别选择六个 target_stage，并枚举该路径全部 required node | stage set digest 精确匹配；路径内必需 capability 均可机械派生、更高 capability 被拒绝；精确停在目标里程碑且 receipt 与 achieved_stage 一致 |
| `AUTH-001` | finding 触发范围内重规划 | 新 PlanRevision 自动继续，父版本/差异可审计且不通知用户 |
| `AUTH-002` | 服务器、数据库、不可逆风险或权限越界；授权过期/撤销 | 未派发副作用，进入 BLOCKED/ACTION_REQUIRED，旧授权不能消费 |
| `PLAN-001` | PM/Architect 双角色规划与 capability 派生 | 两份独立 schema/Artifact/事件齐全；未知 nodeType 或越权映射 fail closed |
| `PLAN-HASH-001` | Rust/Python/TypeScript 对含完整 nodes/barriers/stage map 的 JCS/NFC golden vectors、非法数字和不同 revision 谱系计算双 hash/barrier ID | 三端字节结果与 barrier identity 一致；非法输入拒绝；同语义 hash 可同而不同 Run/revision 谱系 identity 必须区分 |
| `BOOT-001` | 新项目目录非空、已有 `.git`、路径归属不明 | 不覆盖、不初始化，进入 BLOCKED 并保留事实 |
| `MCP-001` | 注册无限制 shell、缺 capability/脱敏/副作用声明的 MCP | 注册被拒绝且无工具可调用，拒绝 receipt 可审计 |
| `STATE-001` | 枚举合法/非法转换、同一 PlanRevision 多 Run、重规划复用 logical node、barrier settled/passed、UNKNOWN settle timeout 与 CAS 冲突 | barrier/node identity 无碰撞；完整成功谓词和 Artifact 原子提交；未知状态不被洗成终态；非法、旧 CAS 和缺 Artifact 转换被拒绝 |
| `LEASE-001` | stale worker、双派发、旧 fencing token、继任者尝试消费前任授权 | 只有新 owner 能提交；旧授权不可移植；外部副作用最多形成一个事实 |
| `PROC-001` | Claude 运行时只杀死并重启 UI | CLI 与 Agent 继续；重连只按 committed taskSeq 重放；进程身份一致且 UI 不伪造 prepared 状态 |
| `CTRL-001` | 各 Step 类型软暂停、立即停止、取消和恢复 | 符合 safe point/grace；旧 Attempt 不复用；取消撤销授权且不可恢复 |
| `CTRL-002` | 快速 PAUSE→RESUME、旧 executor 仍活；checkpoint 含未跟踪文件 | 旧 Attempt 的 DRAIN stream/checkpoint/终止收尾可提交，但新 tool/Authorization/副作用被拒绝；新 token/Attempt/授权后才 RUNNING；隐藏 ref 保留文件且真实 index/branch 不变 |
| `EVENT-HASH-001` | Rust/Python/TypeScript 物化同一 PreparedBatchV2：同 Task 同批至少两事件、多 Task 交错、两个批次竞争同一旧 head，并测试 genesis、null/缺省、未知字段和前驱漂移 | 三端按冻结 event ID allocator/batchOrdinal 得到一致 DurableEventV2/taskSeq/digest；竞争批次仅一方 CAS 成功，非法或前驱不符输入拒绝 |
| `STREAM-001` | JSONL 断行、重复、乱序、跨 chunk 秘密、重连 | 正确拼帧/去重/脱敏/重放；未脱敏原文不落盘 |
| `STREAM-002` | CLI exit 0 但终止 frame、sourceSeq/transport span 或必需 Artifact 缺失 | fail closed，不能生成成功 Gate |
| `STREAM-DUR-001` | 在多 Task claim 事务前/中/后、claim COMMIT 后首个 manifest byte 前、record write、文件 flush、最终 rename、父目录持久化、SQLite 引用 COMMIT 前后逐点杀 Agent，并覆盖 WSL VM/主机硬重启 | claim 全有或全无；无对象/部分对象/完整对象/事实冲突分别正确清除、quarantine、补录或 RECONCILING；无永久 pending、双批次、错误清除、缺对象引用或假成功 |
| `STREAM-BP-001` | 注入 slow disk/flush stall 直到摄取队列跨过高水位和硬上限 | 停止新派发并产生 OS 背压；内存/应急排空有界；未脱敏字节不落盘、不丢事件、不生成成功 Gate；无法收尾时明确 LOST/RECONCILING |
| `STREAM-PERF-BURST` | 签名 load generator 按固定三任务/事件/大小分布在 100 ms 内输入 12,000 events | 全链路无丢失/泄密，峰值/working-set 不越 `benchmark-profile.v1` 且从最后输入起 5 秒内清空；记录各段 p50/p95/p99 |
| `STREAM-PERF-SUSTAINED` | 同一固定分布以三任务聚合 2,000 events/s（±0.5%）持续 60 秒 | SQLite commit p95 <1 秒、durable UI p95 <2 秒，queue slope/结束差/峰值均过 profile 阈值，覆盖脱敏/flush/SQLite/IPC/真实渲染 |
| `STREAM-PERF-SPARSE` | 1–10 events/s、跨 chunk 不确定后缀和低流量 batch | 按 profile 算 nearest-rank p95/最大延迟与心跳间隔；不确定字节只保守脱敏或 fail closed |
| `REVIEW-001` | Codex reject 后 Claude 修复；审核后 SHA 漂移 | finding 闭环；漂移使 receipt 失效并重跑 Verifier/Codex |
| `GIT-001` | 含本地未 push base commit 的 Windows 仓库，经 Git Object Bridge 完成开发、PR、CI、merge | bundle/pack digest、base/candidate/reviewed/remote/merge SHA 一致；崩溃可恢复；用户 index/worktree/branch 不受污染 |
| `SIDEFX-001` | push/PR/registry 已成功但响应丢失 | reconcile 查到完成事实，不重复产生外部对象 |
| `DEPLOY-001` | 可销毁 Linux 上生产等价成功发布 | digest 部署、迁移、切流、业务验收、soak、Codex 证据审核全部通过 |
| `DEPLOY-002` | 切流前/后故障 | 按 RollbackPlan 恢复旧版本，验收通过，lifecycle=`ROLLED_BACK` |
| `DEPLOY-003` | 回滚再次失败后，以新 Task 再次请求相同目标 | lifecycle=`FAILED_NEEDS_INTERVENTION`，现场可诊断、临时锁无僵死；target guard 持续阻止新授权/lease，且只创建一个 ACTION_REQUIRED Notification Action |
| `GUARD-001` | ServerProfile、数据库、registry 分别进入 `INTERVENTION_REQUIRED` 后创建新 Profile revision/Task，并尝试无证据清除与专用 ReconciliationPlan 清除 | 三类稳定物理 fingerprint 都不被 revision 绕过；无对应 observe/`target.guard.clear` 授权时拒绝；完整核对后一次性 CAS 清为 OPEN 且 receipt 可审计 |
| `DEPLOY-FENCE-001` | 只读预检后制造双 Release Runner，并在上传/pull/备份/启动/切流各点令旧 token 继续写 | release lock 在首次远端业务写前取得；每个写入口拒绝旧 token；journal 与远端事实收敛且无双发布 |
| `STORE-001` | SQLite/WAL 损坏、D 盘满、Artifact 原子 rename 失败 | 从有效备份恢复或 fail closed；无半提交 Artifact/假成功 |
| `BUDGET-001` | 修复、重规划、重启、合法长时间关机、系统睡眠、用户暂停、Provider 重试和同 boot 时钟前跳/回拨 | 只追加时钟链可重建；非授权暂停/停机均计费，合法关机不误报、回拨/异常跳变 fail closed；耗尽后 ACTIVE+BLOCKED，扩大预算需要新授权 |
| `RETRY-001` | 短 Retry-After、订阅额度耗尽、请求发送后 SSH 断线 | 分别进入定时 QUEUED、ACTION_REQUIRED、RECONCILING；无忙轮询或盲重试 |
| `NOTIFY-001` | 自动修复、完成、BLOCKED、已知发送失败、调用后结果未知和 Agent 重启 | 中间过程零 OS 通知；应用内 Action 恰好一条；支持幂等查询的 Adapter 收敛到同一 tag，不支持者对未知结果不重发并留下 `DISPATCH_UNKNOWN` receipt event |
| `PATH-001` | 干净 Windows 安装与全流程文件系统快照 | 所有 StorageLocationContract 可控写入位于 D 盘；OS 白名单逐项披露 |
| `CLI-PROFILE-001` | C 盘默认 profile 存在、Factory profile 为空 | 只引导在 D-backed Factory profile 登录，不读取/复制 C 凭据；生成无秘密 onboarding receipt |
| `PATH-002` | D 缺失、E/F 回退、junction/`SUBST`/网络卷伪装 | 全部拒绝写任务，无自动 fallback；App 仅进入诊断/迁移向导 |
| `RUNTIME-001` | WSL2 simplified 单 distro、legacy 双 distro、Factory distro integration 未启用、Hyper-V、Windows containers、未知布局 | 已认证 WSL2 分支和 integration 正确识别；启用/重启须显式授权；其他布局只读诊断并 BLOCKED，迁移不破坏既有数据 |
| `WSL-IO-001` | 执行依赖安装、Verifier 和构建 | worktree 位于专用 distro ext4、VHDX 实体在 storageRoot，未用 `/mnt/d` 作为执行 worktree |
| `NGINX-001` | 第三方在 `nginx -t`/reload 前后修改配置 | 预期外漂移不误报成功；reload 前冲突不 reload，后验冲突进入 RECONCILING；残余风险 receipt 存在 |
| `BACKUP-001` | 每次发布轻量验证、drill receipt 过期、`restore_required` | 每次绑定真实 backup；到期/高风险路径强制隔离恢复演练，绝不覆盖生产库 |
| `DEPLOY-RAM-001` | 候选栈会耗尽内存余量 | 不启动候选、不 OOM 旧栈；只有已授权且停机预算允许才降级 recreate |
| `SCHED-PERF-001` | 单 Claude/Codex 槽位下并发执行三个代表任务 | 等待原因准确、无资源超卖；产出阶段吞吐/排队分布并据此校准 ETA |
| `COMPAT-001` | CLI/Docker/WSL 自动更新造成 executable digest/schema/layout 漂移 | 停止新派发并诊断；录制样本和最小真实任务回归通过、签发新 manifest 后才晋升 |

首版签名 `benchmark-profile.v1` 使负载和 PASS 均可机械重现。它固定 `generatorId=factory-event-loadgen-v1`、可执行文件 digest 和 PRNG seed `20260803`；三 Task 输入占比为 40%/35%/25%，每个 1 秒窗口允许舍入误差 1 event；事件类型为 stream/provider-semantic/tool/state = 70%/15%/10%/5%；Provider 输入 payload 大小为 512/704/1024 bytes = 25%/50%/25%，内容由 seed 生成，其中 1% stream records 含跨 chunk 的合成 secret 测试标记。sustained 精确目标为 2,000 events/s（±0.5%）并采用 paced arrival；burst 使用同一分布在不超过 100 ms 内提交 12,000 events。任何分布、seed、arrival model 或 generator digest 漂移都使 receipt 无效。

采样使用 monotonic clock，`sampleIntervalMs=100`；sustained 测试先预热 10 秒、再测量 60 秒，使用最后 50 秒的全部样本分别对 queue events/bytes 做普通最小二乘斜率，要求 `slopeEventsPerSecond <= 1`、`slopeBytesPerSecond <= 4096`，且结束值不高于测量起点加 200 events/256 KiB。全过程峰值不得超过 `20000 events/32 MiB` 摄取硬上限，每个 stream 的应急排空不得超过 8 MiB，Agent ingest 路径相对预热基线的 working-set 增量不得超过 256 MiB。burst 的 5 秒从最后一个输入 event 被 Adapter 接受时开始；sparse 分别以 1、5、10 events/s 各运行 60 秒，按固定 seed 循环同一类型/大小分布，并覆盖良性跨 chunk、秘密跨 chunk 和永不闭合 frame。对已闭合 frame/安全 record 使用 nearest-rank p95 <2 秒且单事件最大 <5 秒，未闭合 structured frame 至少每 1 秒提供一次无内容心跳。benchmark receipt 必须冻结 CPU/RAM、Windows/WSL/Docker 版本、D 盘卷 identity/介质/剩余空间、后台负载、generator/profile digest 和样本原始 Artifact；存在其他 Factory 任务或未披露竞争负载时结果无效。新 manifest 可以收紧阈值，不能放宽这些首版产品门禁。

每个测试必须产生机器可读报告，至少包含测试 ID、环境 manifest digest、动作、期望、实际状态、外部副作用计数、Artifact digest 和最终 receipt。失败测试不能通过手工文字改为成功。

### 20.3 Compatibility Manifest

每个可交付版本必须附 `compatibility-manifest.json`，冻结并签名实际验证过的 Windows build、WebView2、WSL kernel/distro/VHDX layout、Linux distro、Docker 安装模式/backend/provisioning/container mode/data-root、Factory distro WSL integration、Desktop/Engine/Compose/buildx/BuildKit、容器终止/inspect 合同、Tauri、Agent/Python、Claude/Codex CLI executable/package digest、CLI schema/profile 环境变量语义、基础镜像 digest、stage/node capability map digest、frame assembly/redaction/事件批量/摄取队列参数、`benchmark-profile.v1` digest、预算 monotonic/boot identity 与 clock-jump 阈值、Windows/Linux 文件与父目录耐久方法和对应测试 receipt digest。发布支持范围严格等于 manifest 中通过合同测试的组合；未知 CLI/schema 或运行时版本先进入只读诊断并 `BLOCKED`，不能猜测兼容。

每次 Agent 启动、每次 Attempt 派发前及 Runner 启动后都核对实际 executable/runtime digest 与 manifest。外部 CLI、Docker、WSL 或 schema 自动更新不能视为已兼容：检测漂移后停止新派发，运行中受影响 Attempt 在安全点停止并 reconcile；候选版本先在隔离环境回放脱敏录制 JSONL，执行登录/profile 路径、异常/截断/终止 frame、CLI Contract 和最小真实任务测试，全部通过并签发新 manifest 后才晋升。失败保留旧 manifest，不用未知 parser 继续生成成功事实。

首个完整版本的发布门禁至少覆盖：一个仍受厂商支持的 Windows 11 x64 build、一个 WSL2 下的 Ubuntu LTS、一个标准 x86_64 Linux SSH/Compose 环境，以及当前锁定的 Claude/Codex CLI 版本。确切版本在实现发布时写入 manifest，而不是在概念规格中漂移。

## 21. 实施拆分

本产品范围较大，因此使用一个 Master Spec 管理端到端契约，并拆成六个可独立验收的实现工作流。拆分是为了降低集成风险，不表示交付单功能 MVP。

1. **Desktop Shell：**Tauri、React、托盘、自启动、单实例、更新、Command Center。
2. **Control Plane：**状态机、SQLite/Event Store、资源租约、恢复和并发。
3. **AI Harness：**Claude/Codex Adapter、Tool Broker、进程管理和真实终端。
4. **Verification：**隔离 Verifier、门禁、review loop 和 Artifact。
5. **Deployment：**ServerProfile、SSH Compose、验收、回滚和证据复核。
6. **Hardening：**Vault、安全、备份、安装包、文档和整体 E2E。

工作流 1–4 集成后设置内部自用里程碑 `SELF_HOSTING_CODEX_APPROVED`：桌面壳、控制平面、状态/事件链、Claude→Verifier→Codex 闭环必须能用来开发本产品自身。该里程碑不包含 push/merge/部署，不等于首个完整版本交付，也不削减工作流 5–6 和最终 DoD；后半程发布链以该版本的真实使用证据继续验证。

每个节点完成条件均为：实现、必要中文注释、结构化日志、相关测试、需求审查、代码审查和本地原子提交全部通过。

## 22. Definition of Done

只有下表无条件项全部 PASS，AI Coding Factory 才能宣称首个完整版本交付：

| DoD ID | 环境与操作 | 机器判定 | 必需回执 |
| --- | --- | --- | --- |
| `DOD-APP-001` | Compatibility Manifest 中的干净 Windows；安装、启动、托盘、升级并执行 `NOTIFY-001` | 主界面不依赖外部浏览器；关闭 UI 后 Agent 继续；通知投递边界和签名/版本一致 | packaging/E2E + notification receipt report |
| `DOD-STAGE-001` | 执行 `STAGE-001..006` | 六个目标均真实止步且 capability 不越界 | 6 份 final task receipt |
| `DOD-OBS-001` | 执行 `PROC-001`、`EVENT-HASH-001`、`STREAM-001..002`、`STREAM-DUR-001`、`STREAM-BP-001` | 真实进程、跨语言事件链、durable prefix、脱敏来源、背压、崩溃补录和回放全部匹配 | event-chain + durability receipt |
| `DOD-PERF-001` | 执行三项 `STREAM-PERF-*` 与 `SCHED-PERF-001` | 持续/稀疏 SLO、burst 有界清空、队列斜率和三任务 ETA 基准全部通过 | performance benchmark receipt |
| `DOD-AI-001` | 执行 `AUTH-001..002`、`PLAN-001`、`PLAN-HASH-001`、`BOOT-001`、`MCP-001`、`REVIEW-001` 和完整返工 | 授权/重规划、规划证据、跨语言 hash、工具拒绝、finding 闭环和 reviewed SHA 全部一致 | authorization + planner + verifier + review receipts |
| `DOD-CTRL-001` | 执行 `STATE-001`、`LEASE-001`、`CTRL-001..002`、`SIDEFX-001`、`STORE-001`、`BUDGET-001`、`RETRY-001` | 暂停、停止、取消、副作用核对、预算、重试、崩溃和未知状态无假成功 | recovery/chaos report |
| `DOD-GIT-001` | 新/旧项目、worktree、PR、merge；执行 `GIT-001` | 用户工作区零污染；Git Object Bridge 与身份链一致 | Git identity receipt |
| `DOD-DEPLOY-001` | 可销毁、生产等价 Linux；执行 `DEPLOY-001..003`、`GUARD-001`、`DEPLOY-FENCE-001`、`NGINX-001`、`BACKUP-001`、`DEPLOY-RAM-001` | 发布、逐写 fencing、真实验收、soak、内存预检、备份、回滚和跨任务隔离/清除全部通过 | release/acceptance/rollback receipts |
| `DOD-SEC-001` | 威胁测试、secret scan、`CLI-PROFILE-001`、`PATH-001..002`、`RUNTIME-001`、`WSL-IO-001` | 无明文凭据、无跨任务访问、可控写入都在严格 D 根、未知运行时 fail closed | security + path/runtime report |
| `DOD-OPS-001` | 备份恢复、升级失败、诊断与 `COMPAT-001` 演练 | 达到本地 RTO/RPO；版本漂移安全阻断；Runbook 步骤可复现 | operations + compatibility drill receipt |
| `DOD-REVIEW-001` | Codex 对完整产品和上述证据做最终只读审核 | 冻结 rubric 下无 open blocker/high | final product review receipt |

最终 DoD 聚合器必须从 §20.2 机器读取测试 ID，展开 `001..NNN` 范围，并证明每个“必须通过”ID 至少被一个 DoD 行引用且存在 PASS receipt；出现未映射、重复冲突、缺报告或环境 manifest digest 不一致时整体 fail closed。

云厂商实机认证是条件式交付项：用户提供临时 ECS/CVM 或其他真实 ServerProfile 后，分别运行 `DEPLOY-001..003` 并生成 `cloud-certification-receipt`。缺少某厂商凭据不阻塞通用产品的本地完整交付，但该厂商必须显示“未认证”，不得宣传为已验证兼容。

## 23. 交付物

- Windows 安装包或便携包，以及 D 盘数据根配置。
- 完整源码、测试、CI、部署适配器和版本信息。
- 架构文档、事件协议、威胁模型和服务器接入手册。
- 备份恢复、故障排查、发布回滚和升级 Runbook。
- Codex 最终审核回执和全链路验收报告。

## 24. 外部依赖与诚实边界

以下内容不是规格占位符，而是实施和验收时的外部前置条件：

- 用户需要在首次接入向导中，于 Factory 专用 WSL2 distro 的 D-backed 隔离 profile 内完成 Claude Code 和 Codex CLI 登录；现有 C 盘默认登录态不会被复用或复制。
- 真实云服务器验收需要用户以后提供 ServerProfile、域名/网络和相应凭据引用。
- 生产级签名安装包需要可用的 Windows 代码签名证书；没有证书时只能交付未签名测试包或便携包，并会有系统警告。
- 应用只在 StorageLocationContract 全部通过后启动写任务，并保证所有可控项目、日志、缓存、运行数据、WebView 数据目录、WSL/Docker 数据位置解析到 `D:\codex项目`。Windows 注册表、通知数据库、系统日志、WebView2/系统组件二进制更新等操作系统元数据无法由应用承诺绝对不写 C 盘，必须逐项披露；这不构成把 Factory 数据放到其他目录的许可。
- 电脑关机或用户未登录时，本机版不能继续执行。若以后需要真正 24×7，必须先完成独立的云 Worker 协议、认证、租约、事件传输和迁移规格，不能把首版本机 IPC 直接暴露到网络。

## 25. 隐私与遥测

默认不发送产品遥测。源码摘要、运行日志、终端 transcript、模型活动和指标只保存在本机 D 盘。未来若增加遥测，必须默认关闭、显式开启、先脱敏，并允许用户查看发送字段。

## 26. 规格变更规则

本文经用户书面复核后成为实施计划的需求基线。以下变化需要更新本文并重新做影响审查：

- 新增或删除目标阶段。
- 改变生产授权语义。
- 放宽 Claude/Codex/Verifier/Release Runner 信任边界。
- 改变凭据存储、远端发布策略或数据库回滚保证。
- 将本机单用户架构扩展为多用户或云端控制平面。

不影响上述契约的内部实现细节可以在实施计划中细化，但不能削弱真实性、审核、日志、安全和验收门禁。
