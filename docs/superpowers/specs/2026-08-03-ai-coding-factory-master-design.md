# AI Coding Factory Master Design Spec

- 文档版本：`1.0`
- 日期：`2026-08-03`
- 文档类型：Reference / Conceptual Design
- 状态：七节交互设计已获用户确认；本文待用户进行书面规格复核
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
- **Receipt**：记录已执行外部动作、输入身份、结果和证据 hash 的不可变回执
- **Lease**：带所有者、有效期和心跳的资源使用权
- **Reconciler**：崩溃或断线后核对真实进程、Git 和远端状态的恢复组件
- **IPC**：Inter-Process Communication，进程间通信；本产品使用受限 Windows Named Pipe
- **WSL2**：Windows Subsystem for Linux 2，Windows 上的 Linux 执行环境
- **OCI**：Open Container Initiative，镜像与运行时标准
- **PTY/ConPTY**：伪终端及 Windows Pseudo Console，用于承载真实终端流

## 3. 产品目标与非目标

### 3.1 产品目标

- 用户只描述“做什么”，系统负责生成和维护内部 Prompt、RunSpec 与审核材料。
- 支持已有仓库和新项目；所有写任务使用独立 Git worktree、分支和容器。
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
- 首个完整版本只支持本机已登录的 Claude Code CLI 与 Codex CLI；不实现模型 API Adapter，也不在产品内管理模型 API key。
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
- 可选的时间、成本和自动修复循环上限覆盖值。

系统负责将输入转化为内部 RunSpec；用户不需要编辑模型 Prompt。

### 4.3 时间线优先的任务详情

任务详情默认按单调事件序号展示时间线。事件可筛选来源、阶段、严重级和运行尝试。点击事件或“查看终端”打开居中弹窗。

终端弹窗包含：

- 实时输出。
- 活动摘要。
- 进程树。
- Provider JSONL（强制脱敏）。
- 环境与身份。

受编排终端默认只读。用户可以暂停、继续或立即停止，但不能向 Claude/Codex 受管会话直接键入内容，以免破坏状态机和证据链。

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

通知由常驻控制平面发送，而不是由可关闭的 UI 发送。规划完成、Codex 打回、测试失败、自动重试和范围内重规划都不触发系统通知。每次允许通知的状态变化先持久化 `notification_receipt`，以 `taskId + outcomeVersion + notificationKind` 唯一去重；发送失败采用有限重试，Agent 重启后补发尚未成功的通知。`BLOCKED`、`FAILED_NEEDS_INTERVENTION` 和凭据/授权问题统一归为 `ACTION_REQUIRED` 通知；同一 `outcomeVersion` 只通知一次。

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
  - Managed Process Helper、ConPTY/pipe、Job Object
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

`factory-agent.exe` 是独立用户级后台进程。它持有任务状态机和调度器，使用命名互斥锁保证单实例。由 Agent 内部的 Managed Process Helper 独占受管 CLI、ConPTY/pipe、Windows Job Object 和容器执行句柄；输出先进入事件日志，再供 UI 订阅。Tauri UI 可以崩溃和重启而不终止任务；UI 重连后按任务事件 cursor 回放。

每个执行 Adapter 必须实现版本化合同：`capabilities`、`start`、`inspect`、`interrupt`、`kill`、`parse` 和可选的 `resume`。`start` 返回不可复用的 Attempt 身份和进程身份；`interrupt/kill` 必须幂等；不支持原会话恢复时，`resume` 明确返回 `unsupported`，由状态机创建新 Attempt。CLI exit code 为 0 但终止事件、JSONL 完整性或必需 Artifact 缺失时仍判定失败。

### 5.3 执行平面

每个任务使用唯一 Task ID、Git worktree、分支、运行容器和日志范围。Claude、Codex、验证器和发布器在不同信任区运行，不能共享同一个可写工作区或秘密集合。

Claude Adapter 调用本机已登录的 Claude Code CLI 结构化非交互模式。Codex Adapter 调用本机已登录的 Codex CLI JSON 模式。核心流程不依赖自行管理的模型 API key；Adapter 必须检测 CLI 版本、解析 JSONL，并在事件 schema 不兼容时 fail closed。

Tool Broker 优先使用内置 Git、Docker、Browser、SSH 和文件 Adapter，也允许接入 Model Context Protocol (MCP) 工具。每个 MCP 工具必须声明 schema、capability、超时、重试、副作用和脱敏规则；系统不允许注册无限制 shell 或无权限清单的 MCP 工具。

### 5.4 IPC

桌面 UI 与控制平面使用仅当前 Windows 用户可访问的认证 Named Pipe。默认不监听公共 HTTP 端口或 localhost Web 服务。所有 IPC 命令使用显式 schema、版本、权限和 request ID。首版执行位置只有 `local-windows` 与 `wsl-docker`；数据模型预留 `executorId`、`hostId`、`runtime` 和 `protocolVersion` 仅用于本机恢复，不构成云 Worker 协议。云 Worker 必须另立包含双向认证、远程租约和事件传输的新规格。

## 6. 任务目标阶段与授权

### 6.1 可选目标阶段

| target_stage | 完成条件 | 隐含的最高操作权限 |
| --- | --- | --- |
| `DESIGN_APPROVED` | RunSpec 和技术方案通过 Codex 方案审核 | `repo.read`；新空目录另需 `repo.bootstrap` |
| `CODEX_APPROVED` | Claude 实现、确定性门禁和 Codex 代码审核通过 | `worktree.write`、`git.local_commit` |
| `PR_READY` | 精确候选提交已 push，并创建 PR | 上述权限 + `git.push`、`pr.create` |
| `MERGED` | CI、review 和分支保护通过，提交已合并 | 上述权限 + `repo.merge` |
| `STAGING_ACCEPTED` | 同一镜像 digest 已在测试环境部署并验收 | 上述权限 + `registry.push`、测试环境 scoped SSH/远端写入/验收权限 |
| `PRODUCTION_ACCEPTED` | 生产发布、真实验收和 Codex 证据复核通过 | 上述权限 + 生产 scoped SSH/切流/验收权限及计划内 `rollback` |

若包含数据库迁移，`db.migrate` 作为独立 capability 写入授权快照，并明确迁移风险分类。

### 6.2 任务级授权

授权与可变计划分成三个对象，避免自动重规划与无人值守执行互相冲突：

1. **IntentAuthorization：**用户创建任务时形成，绑定需求 digest、项目/仓库、基准、目标阶段、目标环境、ServerProfile/数据库身份、capability 上限、风险上限、成本/时间/修复次数上限、有效期和用户身份。它不包含尚未生成的计划 hash。
2. **PlanRevision：**规划完成后生成的不可变 RunSpec 版本，包含 `specRevision`、`parentRevisionId`、假设、DAG 与 `planHash`。`planHash` 对规范化 RunSpec 计算，明确排除授权 ID、事件 ID、生成时间等非语义字段，避免循环引用。
3. **ExecutionAuthorization：**Policy Engine 在具体 Step 输入冻结时，机械地从有效 IntentAuthorization 与当前 PlanRevision 派生；绑定精确 plan hash、该步骤已经存在的 base/candidate SHA 或 digest、资源身份、capability scope、TTL 和消费状态。开发前的 worktree 授权只绑定 base SHA；push、merge 和部署授权必须绑定已经产生并审核的 candidate SHA/digest。它不是新的人工确认；每次副作用执行前必须校验并原子消费或续期。

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
| `git.push`、`pr.create`、`repo.merge` | remote URL、source ref、target branch、reviewed SHA、分支保护结果 |
| `registry.push` | registry/repository、image digest、架构 |
| `ssh.exec.scoped`、`remote.write.scoped` | ServerProfile revision、host key、允许命令族、remoteRoot |
| `nginx.switch` | server、release ID、upstream config hash、允许回切目标 |
| `acceptance.fixture.write` | 环境、fixture namespace、清理期限和最大数据量 |
| `db.read`、`db.check`、`db.migrate`、`db.restore` | database profile revision、migration checksum、备份 ID、风险类 |
| `rollback` | 原 release、候选 release、回滚步骤、RPO/RTO 与有效窗口 |

`target_stage` 只决定 capability 上限；具体任务只获得 PlanRevision 实际需要的子集。任何未列入 scope 的 shell、网络、文件、Git、数据库或发布动作一律拒绝。

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

当且仅当 `achieved_stage == target_stage`、必需 Artifact 已提交且没有未解决 blocking finding 时，`Task.lifecycle` 从 `ACTIVE` 转为 `SUCCEEDED`。达到较早里程碑不代表完成更晚目标；`achieved_stage` 不因失败或回滚而倒退。

### 7.2 Step 与 Attempt

- `Step.phase`：`PENDING`、`READY`、`DISPATCHED`、`RUNNING`、`RECONCILING`、`TERMINAL`。
- `Step.outcome`：`NONE`、`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`SKIPPED`、`CANCELLED`、`UNKNOWN_REMOTE_STATE`。
- `Attempt.phase`：`CREATED`、`STARTING`、`RUNNING`、`INTERRUPTING`、`RECONCILING`、`TERMINATED`。
- `Attempt.outcome`：`NONE`、`SUCCEEDED`、`FAILED`、`INTERRUPTED`、`KILLED`、`LOST`、`UNKNOWN_REMOTE_STATE`。

一次派发创建一个全新 Attempt ID；Attempt 永不复用。立即停止、进程丢失或 Agent 崩溃后恢复执行时必须创建新 Attempt，并通过 `supersedesAttemptId` 关联旧尝试。

### 7.3 合法转换

| 当前 `Run.observed_state` | 允许的下一状态 |
| --- | --- |
| `QUEUED` | `RUNNING`、`PAUSED`、`BLOCKED`、`TERMINATED` |
| `RUNNING` | `QUEUED`、`PAUSING`、`STOPPING`、`RECONCILING`、`BLOCKED`、`TERMINATED` |
| `PAUSING` | `PAUSED`、`STOPPING`、`RECONCILING`、`BLOCKED` |
| `PAUSED` | `QUEUED`、`STOPPING`、`TERMINATED` |
| `STOPPING` | `INTERRUPTED`、`RECONCILING`、`TERMINATED` |
| `INTERRUPTED` | `RECONCILING`、`QUEUED`、`TERMINATED` |
| `RECONCILING` | `QUEUED`、`RUNNING`、`PAUSED`、`BLOCKED`、`TERMINATED` |
| `BLOCKED` | `QUEUED`、`PAUSED`、`RECONCILING`、`TERMINATED` |
| `TERMINATED` | 无 |

`desired_state` 只允许 `RUNNING ↔ PAUSED` 和 `RUNNING/PAUSED → CANCELLED`；`CANCELLED` 不可逆。立即停止是将当前 Attempt 终止并把 desired state 设为 `PAUSED` 的控制命令，不等于取消，因此事实核对后仍可由用户继续。任务终局只允许：

- `ACTIVE → SUCCEEDED`：达到所选 target_stage。
- `ACTIVE → FAILED`：没有自动恢复路径且没有需要保留的生产未知状态。
- `ACTIVE → CANCELLED`：用户取消，远端事实已核对，清理/保留 receipt 完成。
- `ACTIVE → ROLLED_BACK`：目标未达成，但旧良好版本已恢复并通过回滚验收。
- `ACTIVE → FAILED_NEEDS_INTERVENTION`：发布与回滚都无法建立安全事实。

所有转换都使用 `state_version` CAS，并与状态事件、outcomeVersion 和当前 `fencing_token` 校验在同一数据库事务提交。非法转换、旧 CAS、旧 fencing token 或缺少必需 Artifact 一律拒绝并生成审计事件。

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
  nodes: []
riskProfile:
  level: low|medium|high|critical
  reasons: []
intentAuthorizationId: intent-authorization-uuid
planHash: sha256:canonical-runspec-digest
```

`planHash` 的规范化算法、字段排序、Unicode 编码和排除字段固定在 schema 版本中；PlanRevision 写入后不可修改，修复只能新建子版本。当前执行游标始终指向唯一 active PlanRevision。

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

验证结果必须形成机器可读 Artifact，包含命令、环境、开始/结束时间、exit code、关键摘要和内容 hash。

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

默认最多执行 6 个 Claude↔Codex 修复循环；相同失败签名连续出现 3 次时先在 IntentAuthorization 包络内自动生成新 PlanRevision。仍未收敛或重规划越界时进入 `Run.observed_state=BLOCKED`，向用户交付根因、证据和明确处理选项。

发布后 Codex 只审核证据：证据不完整先进入 `RECONCILING` 补证；真实运行缺陷阻止验收；安全缺陷或已满足 RollbackPlan 触发条件时触发回滚。Codex 不能单凭无法复核的主观意见直接执行生产动作。

## 10. 可观察性与真实终端契约

### 10.1 事件信封

所有 Adapter 将真实源事件归一化为：

```json
{
  "schemaVersion": 1,
  "taskSeq": 1842,
  "runSeq": 219,
  "eventId": "event-uuid",
  "taskId": "task-uuid",
  "runId": "run-uuid",
  "stepId": "step-uuid",
  "attemptId": "attempt-uuid",
  "source": "claude | codex | verifier | release | orchestrator",
  "type": "process.started | stream.chunk | stream.terminated | model.summary | orchestrator.objective | tool.call | tool.result | state.changed",
  "sourceSeq": 271,
  "sourceByteOffset": 8192,
  "streamId": "stream-uuid",
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
  "payloadDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "previousEventDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "eventDigest": "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "redactions": [],
  "redactionManifestDigest": "sha256:full-digest"
}
```

数据库事务为每个 Task 分配全局单调且唯一的 `taskSeq`；`runSeq` 只用于单个 Run 内诊断。UI cursor 使用 `(taskId, taskSeq)`，`wallTime` 只用于显示，不能参与排序。每个 Task 的事件 digest 形成链；断线后从最后确认的 `taskSeq` 重放。该链能发现本地事件被意外删除或改写，但不宣称能够抵抗拥有本机管理员权限的恶意篡改。

Adapter 必须对 Provider stream 记录来源序号或字节偏移、接收时间、CLI/Adapter 版本和显式 `stream.terminated`。断行只能在完整 frame 后解析；重复或乱序 frame 按 `(attemptId, streamId, sourceSeq/sourceByteOffset)` 去重与核对。缺终止 frame、offset 空洞、解析错误或 exit 0 但 schema 不完整都不能生成成功事实。

### 10.2 运行状态真实性

只有以下条件同时满足才显示 `RUNNING`：

- 有效资源 lease 归当前 Agent 所有。
- PID、Windows Job Object、WSL process 或 container exec 身份匹配。
- 心跳在允许窗口内。
- 最近事件没有终止或失联证据。

完成状态还必须具备 exit code、阶段门禁结果和对应 Artifact。缺少事实时显示“正在核对”或“未知”，不能推断成功。

### 10.3 模型活动边界

可以展示：

- 模型主动公开的 reasoning summary。
- 当前 RunSpec 目标和编排器当前步骤。
- 工具调用、工具结果、文件变化、测试状态和 Codex finding。

每条 `model.summary` 必须带 `summaryOrigin=provider_public`、`providerEventId` 和 `rawEventDigest`。`rawEventDigest` 使用 Vault 内审计密钥对原始 frame 做 HMAC，只用于证明来源关联，避免对低熵秘密暴露可离线猜测的裸 hash；原始 frame 完成脱敏后立即丢弃。不能展示或伪造模型未公开的隐藏思维链。若 Provider/CLI 未提供可显示摘要，界面明确显示“无可展示推理摘要”。编排器目标使用独立的 `orchestrator.objective` 事件，不能归入 `model.summary` 或冒充模型思考。

### 10.4 脱敏

真实输出先通过跨 chunk 流式脱敏器，再允许写入事件库、Artifact 或 UI。命中已知凭据或秘密模式时显示 `[REDACTED:credential]` 并记录 redaction manifest；未脱敏原文不得落盘或展示。系统不能为了“逐字原样”泄露秘密，也不能静默删除造成误解。

大对象先写入同卷临时文件，校验长度与 digest，执行 `fsync` 后原子 rename；随后才在同一 SQLite 事务提交 Artifact 元数据和引用 Event。磁盘满、rename 失败或数据库提交失败时不得暴露半提交 Artifact，清理动作必须可重试。

## 11. 持久化数据模型

SQLite WAL 是单机权威状态库，由控制平面单写。主要实体：

| 实体 | 职责 |
| --- | --- |
| `projects` | 仓库、默认分支、Provider 和项目规则 |
| `tasks` | 用户需求、lifecycle、target/achieved stage、active revision、outcome/CAS version |
| `plan_revisions` | 规范化 RunSpec、父版本、DAG 版本和 plan hash |
| `runs` | desired/observed state、phase、cursor、执行位置及恢复边界 |
| `steps` | DAG 节点、phase/outcome、依赖、超时、幂等键和重试策略 |
| `attempts` | 不可复用的具体尝试、进程/容器身份、退出和终止事实 |
| `events` | 追加事件、任务全局序号、来源 offset 和 digest 链 |
| `artifacts` | producer、confidentiality、commit state、路径、大小和 digest |
| `review_findings` | Codex finding 生命周期 |
| `intent_authorizations` | 用户意图、风险/capability 包络、有效期和撤销状态 |
| `execution_authorizations` | 计划/资源绑定、scope、TTL、消费和撤销状态 |
| `resource_leases` | owner、单调 fencing token、control epoch、TTL 和心跳 |
| `server_profiles` | 云无关服务器能力与凭据引用 |
| `deployments` | 发布身份、前后版本和状态 |
| `acceptance_checks` | blocking/advisory 检查及证据 |
| `action_receipts` | Git、registry、SSH、迁移和切流等外部动作完成事实 |
| `rollback_receipts` | 回滚触发、动作、结果和恢复身份 |
| `notification_receipts` | 通知去重键、发送尝试和最终结果 |
| `credential_refs` | 凭据元数据，不包含秘密值 |
| `audit_records` | 权限、工具和外部动作审计 |

关键字段与约束：

- `tasks(task_id PK, project_id FK, lifecycle, target_stage, achieved_stage, active_plan_revision_id FK, active_run_id FK, state_version, outcome_version)`；`state_version` 每次 CAS 更新递增。
- `runs(run_id PK, task_id FK, desired_state, observed_state, phase, dag_version, run_cursor, executor_id, host_id, runtime, protocol_version, recovery_target_phase, state_version)`。
- `steps(step_id PK, run_id FK, plan_revision_id FK, phase, outcome, dependency_hash, idempotency_key, state_version)`；同一 PlanRevision 内 `(run_id, logical_node_id)` 唯一。
- `attempts(attempt_id PK, step_id FK, supersedes_attempt_id FK, phase, outcome, executor_id, process_session_id, pid, process_start_time, job_object_id, wsl_distro, container_id, image_digest, exit_code, termination_reason, fencing_token, started_at, ended_at)`。
- `events(event_id PK, task_id FK, run_id FK, task_seq, run_seq, source_seq, source_byte_offset, schema_version, event_digest, payload_digest)`；`(task_id, task_seq)`、`event_digest` 唯一，事件不可原位更新。
- `artifacts(artifact_id PK, producer_attempt_id FK, media_type, confidentiality, commit_state, storage_path, size_bytes, digest, created_at)`；`commit_state` 只允许 `STAGING → COMMITTED`，只有 COMMITTED Artifact 能满足 Gate。
- `intent_authorizations` 与 `execution_authorizations` 都记录 `issued_at`、`expires_at`、`revoked_at`、`revoke_reason`；ExecutionAuthorization 另有 `consumed_at`、`plan_hash`、`resource_fingerprint` 和 `capability_scope_digest`。
- `resource_leases(resource_key PK, owner_executor_id, fencing_token, control_epoch, acquired_at, heartbeat_at, expires_at, state_version)`；同一资源只允许一个未过期 owner。
- `action_receipts(action_type, idempotency_key, request_digest, remote_identity, result_digest, started_at, completed_at)` 的 `(action_type, idempotency_key)` 唯一。

PlanRevision、Event、已消费 Authorization 和 Receipt 追加后不可修改，只能通过后继记录更正。Task 默认软删除；删除内容对象前必须确认无其他 Artifact 引用并写审计 tombstone。所有外键启用并做 migration 合同测试；schema 迁移必须先备份、在事务中执行并通过完整性检查。

大体积 transcript、测试报告、截图、SBOM 和发布证据存入 D 盘内容寻址对象目录，数据库保存路径、大小、类型和 hash。

默认数据目录结构：

```text
D:\codex项目\AI-Coding-Factory-Data\
  state\factory.sqlite3
  artifacts\sha256\
  transcripts\
  logs\
  worktrees\
  cli-profiles\
  vault\
  backups\
  tmp\
```

默认保留策略：

- 任务元数据、最终审核和发布回执保留到用户主动删除。
- 脱敏后的 Provider/终端 transcript 保留 30 天，允许配置 7–365 天。
- worktree 在合并或回滚窗口结束、提交已持久化并完成清理检查后删除。
- 状态库每日备份 7 份、每周备份 4 份；schema 升级前强制备份。

## 12. 暂停、停止与恢复

### 12.1 软暂停

软暂停先将 `desired_state=PAUSED`，立即停止派发新 Step，并在当前 Step 类型定义的安全点写入检查点。控制命令应在 1 秒内持久化确认；进入 `PAUSED` 的实际时间由下表的安全点决定。

| Step 类型 | 安全点 | 默认 grace | 超时/立即停止后的事实 |
| --- | --- | --- | --- |
| Claude/Codex 生成或文件编辑 | 当前完整 Provider/tool frame 已落盘，文件操作已原子完成 | 30 秒 | 终止旧 Attempt；脏工作区隔离，恢复创建新 Attempt |
| Verifier 本地命令 | 子进程正常退出并收集 exit/报告 | 10 秒 | Job Object 强杀；Attempt=`KILLED`，从干净环境重跑 |
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

软暂停完成后释放执行 lease；可以保留无副作用的逻辑排队保留，但不得持续占用模型、CPU、服务器或数据库锁。远端关键区锁只由带 TTL/heartbeat 的 release journal 持有，不能因桌面暂停无限续期。

### 12.2 全局暂停

全局暂停对所有任务发送软暂停请求并停止新任务派发。手动暂停状态跨重启保留，登录自启动不会自动解除。

### 12.3 立即停止

立即停止将 `desired_state` 设为 `PAUSED`，并通过 Windows Job Object、WSL/Docker process tree 终止当前 Attempt。旧 Attempt 永久结束为 `INTERRUPTED/KILLED`，任何继续操作都创建新 Attempt。恢复时不继续使用无法证明一致的脏工作区；将其隔离为诊断 Artifact，并从可信提交重建。

部署中立即停止只证明本地 Release Runner 已停止，不能证明远端动作已停止。当前 Step 记为 `UNKNOWN_REMOTE_STATE`，Run 进入 `RECONCILING`；必须先检查容器、Nginx、数据库和远端 receipt。

### 12.4 取消

取消将 `desired_state=CANCELLED`，立即撤销未消费的授权和 capability，且不可恢复为 RUNNING。系统仍必须核对已派发的远端副作用、执行预声明清理并生成保留说明；只有远端状态已知后，`Task.lifecycle` 才能转为 `CANCELLED`。若无法建立安全事实，则保持 `ACTIVE + BLOCKED` 或转为 `FAILED_NEEDS_INTERVENTION`，不能用“已取消”掩盖未知生产状态。

### 12.5 启动恢复

控制平面启动后按固定顺序执行：

1. 获取单实例互斥锁。
2. 打开并校验 SQLite/WAL。
3. 将陈旧 `RUNNING` lease 标记为待核对并推进 `control_epoch`。
4. 发现 PID、容器、Git、远端 receipt 和数据库锁。
5. 分类为仍存活、停机期间完成、孤儿或未知。
6. 从可信检查点重建队列。
7. 恢复 UI 事件订阅。

UI 崩溃时 Agent 继续运行；Agent 崩溃时用户级启动任务按失败策略重启。电脑睡眠、断网或重启后先核对事实；关机或用户未登录时不保证继续执行。默认本地心跳间隔 2 秒、10 秒未见心跳视为失联并进入核对；登录后控制平面恢复目标为 2 分钟内可用、5 分钟内分类所有活动任务。事件从 Agent 入库到已连接 UI 展示的本机 p95 目标小于 2 秒，断线后必须从最后确认 taskSeq 无缺口重放。

## 13. 并发与资源调度

默认使用资源感知调度：

- Claude 槽位：1。
- Codex 槽位：1。
- Verifier：根据 CPU/RAM 自动，初始上限 2。
- 每仓库合并锁：1。
- 每目标 ServerProfile 发布锁：1。
- 每数据库迁移锁：1。

不同任务可以处于不同阶段并行，但共享风险点必须串行。队列必须显示具体等待原因。生产自动回滚具有最高恢复优先级，但不能抢占数据库迁移关键区。

### 13.1 Lease 与 fencing

调度采用 at-least-once 派发，因此正确性不能依赖“消息只执行一次”。每次取得资源 lease 时，数据库为 `resource_key` 分配严格单调的 `fencing_token`；Agent 每次重建控制权时推进 `control_epoch`。Lease 至少包含 owner executor、token、epoch、TTL、heartbeat、acquired/expires 时间。

所有 Step 状态写入、Artifact commit、Authorization 消费和外部动作必须同时校验当前 `fencing_token + control_epoch + state_version`。旧 worker 即使稍后恢复，也不能提交状态或发布 Artifact。远端 Release Runner 把 token 写入只追加 release journal，并在迁移、切流和回滚前通过原子锁脚本比较当前 token；无法验证 fencing 的目标不允许生产自动化。

本地 lease 过期不等于可以立刻并发接管远端资源。服务器、数据库或 registry Step 在心跳丢失后先进入 `RECONCILING`；只有远端锁、journal 和实际动作均已核对，或原锁已按目标侧规则明确释放，新 executor 才能取得更高 token 并继续。长迁移还必须持有数据库原生 advisory lock，防止仅靠本机 TTL 产生双执行。

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

外部动作在本地先写 `STARTED` receipt，再派发，再以查询到的远端身份写 `COMPLETED`。双派发、成功响应丢失、Agent 重启或 stale worker 都必须收敛到一个外部事实和一个唯一 completed receipt。

## 14. 安全与信任边界

### 14.1 信任区

1. **桌面 UI：**无任意 shell、无数据库直连、无秘密读取接口。
2. **控制平面：**验证策略、路径和 capability，仅保存 credentialRef。
3. **Claude Runner：**当前 worktree 可写，无生产凭据；shell 请求受 Tool Broker 约束。
4. **Codex Runner：**独立只读检出，无候选写权限和生产凭据。
5. **Verifier：**运行仓库脚本，无模型和部署凭据；网络默认关闭，按依赖源临时放行。
6. **Release Runner：**无模型凭据，只在部署阶段获取绑定目标和 TTL 的 capability。

Claude/Codex/Verifier 容器视为不可信执行区。不得挂载 Docker socket、WSL 管理接口、凭据 Vault、用户原工作区、其他任务目录或宿主机根目录；不得使用 `privileged`、host PID/network、任意 device 或新增 Linux capability。默认非 root、`cap-drop=ALL`、只读 rootfs，只把当前任务 worktree 和临时输出目录按所需最小权限挂载。若项目门禁确需放宽其中一项，必须在 RunSpec 中单独列为高风险 capability。

### 14.2 凭据

秘密值保存在 `D:\codex项目` 下的 DPAPI 加密 Vault，并使用当前 Windows 用户 ACL。SQLite、RunSpec、日志和 UI 只保存 credentialRef、scope、fingerprint 和 expiry。DPAPI/ACL 只保护静态数据免受其他普通用户或离线读取，不能抵御同一 Windows 用户下的恶意进程或本机管理员；因此生产凭据只由 Credential Broker 在受限 Release Runner 启动时短期解封，不进入开发/审核/验证进程。

- 模型 CLI 使用隔离的 D 盘 profile。
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

应用对所有可控写入维护机器可读位置清单，并在每次启动和任务预检时解析真实路径、junction/symlink 与剩余空间。以下项目必须位于 `D:\codex项目`：源码、worktree、SQLite/WAL、Artifact、transcript、日志、Vault、备份、临时目录、crash dump、updater staging、WebView2 user-data、Claude/Codex CLI profile、WSL distro VHDX、Docker Desktop disk image/data-root、BuildKit cache 和任务容器卷。

Agent 为所有子进程显式设置 D 盘 `TEMP/TMP` 和工具缓存目录。发现任一可控路径解析到 C 盘、未知卷或空间低于任务预算时拒绝启动写任务并给出迁移步骤。干净机器验收必须用文件系统快照证明位置清单内无 C 盘写入；Windows 注册表、事件/通知数据库、系统日志、系统组件更新和操作系统自身临时元数据列入不可控白名单，并在验收报告中逐项披露，不能笼统宣称“C 盘零写入”。

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
  ownership: existing|managed|external
  publicEndpoints: [https://app.example.com]
dataStores: []
policy:
  supportedStrategies: [recreate, blue-green]
  maxDowntimeSeconds: 30
  retainGoodReleases: 2
```

ServerProfile 不允许保存任意 shell 模板或秘密值。

普通应用发布使用专用非 root deploy 用户和固定 `remoteRoot`，不得挂载 Docker socket 到应用容器。若 deploy 用户属于 `docker` 组，威胁模型按远端 root 等级处理并限制账户只用于发布；更严格环境可使用受限 rootless Docker Adapter。`sudo` 只允许首次接入时调用明确列出的 Bootstrap 操作，普通发布阶段不得获得通用 sudo shell。

`remoteRoot` 必须是规范化绝对 POSIX 路径并位于 ServerProfile 允许前缀；release ID、镜像名、域名、端口和文件名都按独立 schema 校验。SSH Adapter 只能调用版本化的固定操作或上传已校验 digest 的受控脚本，并以结构化参数传值；不得把仓库文本、ServerProfile 字段或模型输出拼进远端 shell 字符串。

### 15.2 首次服务器接入

首次接入是与应用发布分离的一次性流程：

1. 用户填写主机、SSH、域名和环境。
2. 系统只读发现 OS、架构、Docker/Compose、Nginx、端口、磁盘和现有 ownership。
3. 若缺少前置组件，生成明确的 BootstrapPlan。
4. 用户对安装 Docker、创建用户、修改 Nginx/防火墙等系统变更进行一次性授权。
5. 完成后验证并保存新的 ServerProfile revision。

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

每次发布在 `remoteRoot/releases/<releaseId>/` 建立只追加 journal。每条 journal 记录 `stepSeq`、fencing token、输入 hash、开始/完成状态、远端身份、结果 hash 和前一条 digest；迁移、切流和回滚记录必须 `fsync` 后才能推进。Release Runner 重连时先校验完整 journal、活动锁与真实容器/数据库/Nginx 状态，不能仅相信本地缓存。

### 15.4 发布主链

1. 在 Verifier 中构建一次 OCI 镜像并生成 SBOM/manifest。
2. 推送 registry，解析并冻结 digest。
3. SSH 预检 host key、版本、架构、磁盘、端口、当前 release、锁和配置漂移。
4. 创建唯一远端 release 目录，上传带 hash 的 Compose/Nginx/迁移材料。
5. `docker pull` by digest，并用 inspect 核对实际身份。
6. 获取带 TTL、heartbeat 与 fencing token 的 release lock 和 migration lock，执行备份并验证位置、加密、checksum、保留期和一次可恢复性演练；与数据库同故障域、未验证或不可读取的副本不能算有效备份。
7. 用同一镜像 digest 运行一次性 migration job；checksum/release ID 写入数据库 migration journal，重复执行只允许核对同一结果。Migration Runner 只获得目标数据库的 scoped 账户和目标网络访问，无模型凭据、Docker socket、云元数据或其他环境网络权限。
8. 校验 Compose 配置并启动候选栈。
9. 候选内部验收通过后执行 `nginx -t`，校验当前 active config hash，再原子切换 upstream 并 reload；`edge.ownership=external` 时不得自行切流，必须由显式 Traffic Adapter 完成或进入 `BLOCKED`。
10. 执行公网、真实业务、日志、数据库、重启和 soak 验收。
11. Codex 只读复核证据包。
12. 写不可变 release receipt、核对 journal 尾摘要、释放锁并保留回滚版本。

测试环境默认 `recreate`。生产环境在状态外置、迁移向后兼容且资源足够时默认 blue-green 0/100；资源不足时仅在授权的停机阈值内使用 recreate，否则进入 `BLOCKED`。复杂百分比 Canary 延后到具备可靠指标后实现。

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

控制平面本地恢复目标为 RTO 2 分钟可用、5 分钟内完成活动任务分类；RPO 为 0 个已提交 SQLite 事务，最多丢失当前尚未形成完整 frame 的脱敏 stream chunk。每个远端 RollbackPlan 另行声明业务 RTO/RPO，系统只能承诺并验证该任务冻结的数值，不能用产品默认值替代业务承诺。

### 16.2 RollbackPlan

发布前必须记录旧良好镜像 digest、Compose/Nginx/config hash、原 upstream、数据库版本、备份 ID、RPO/RTO、触发条件、回切顺序和回滚后验收。

数据库回滚类型必须是以下之一：

- `none`
- `schema_backward_compatible`
- `reversible_down`
- `restore_required`
- `forward_fix_only`

默认采用 expand-contract。破坏性 contract migration 在回滚窗口结束后作为独立任务执行。`restore_required` 只有在备份恢复已演练、写入可冻结、RPO/RTO 已授权且 `db.restore` capability 存在时才可自动执行；`forward_fix_only` 不得宣称支持自动数据回滚，发布前必须把该限制作为风险门禁。若计划不能建立安全恢复路径，生产 ExecutionAuthorization 不得签发。

失败语义：

- 数据库写入与切流前失败：删除候选；只有确认没有远端副作用后才能写“生产无影响”。
- migration 已执行但切流前失败：生产应用流量虽未切换，schema 或数据已经可能变化；必须核对 migration journal 和旧版本兼容性，并按 `schema_backward_compatible`、`reversible_down`、`restore_required` 或 `forward_fix_only` 的预声明路径处置，不能标记“无影响”。
- 切流后失败：切回旧 upstream/digest，并执行回滚验收。
- schema 不兼容：停止写入，执行预声明的数据处置路径。
- 回滚也失败：进入 `FAILED_NEEDS_INTERVENTION`，保留容器、日志、journal 和证据；按预声明策略冻结或隔离流量/写入。资源 lease 记录 intervention 状态后按 TTL 安全释放，不得留下无限期僵尸锁，也不得做破坏性清理。

回滚成功必须同时证明旧 digest/upstream 已恢复、数据库处于 RollbackPlan 允许的版本、blocking 回滚验收通过且新增错误未超阈值；否则不能标记 `ROLLED_BACK`。

## 17. 错误分类与重试

| 分类 | 示例 | 默认动作 |
| --- | --- | --- |
| `TRANSIENT` | 网络、限流、短暂 SSH 中断 | 最多 3 次，jitter，遵守 Retry-After |
| `FIXABLE` | 测试失败、Codex finding | 结构化返回 Claude 修复 |
| `CAPACITY` | 模型、CPU、仓库或服务器锁 | 排队并显示原因 |
| `AUTH_POLICY` | 凭据失效、授权不匹配 | Fail closed，进入 BLOCKED |
| `UNKNOWN_STATE` | 崩溃后副作用不明 | 先 reconcile，禁止盲重试 |
| `IRREVERSIBLE_RISK` | 不可逆迁移、备份无效 | 停止并保留证据 |
| `INTERNAL_BUG` | 控制平面异常 | 只暂停受影响任务，生成脱敏诊断包 |

任何可能产生外部副作用的步骤只有在具备幂等键或完成事实核对后才能重试。

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
- Release E2E：Compose、Nginx、迁移、blue-green、真实验收和回滚。
- Packaging：D 盘安装、升级、备份恢复、卸载和数据保留。

### 20.2 必须通过的代表流程

| 测试 ID | 场景 | 必须证明的结果 |
| --- | --- | --- |
| `STAGE-001..006` | 分别选择六个 target_stage | 精确停在目标里程碑；未取得更高 capability；最终 receipt 与 achieved_stage 一致 |
| `AUTH-001` | finding 触发范围内重规划 | 新 PlanRevision 自动继续，父版本/差异可审计且不通知用户 |
| `AUTH-002` | 服务器、数据库、不可逆风险或权限越界；授权过期/撤销 | 未派发副作用，进入 BLOCKED/ACTION_REQUIRED，旧授权不能消费 |
| `STATE-001` | 枚举全部合法/非法转换及 CAS 冲突 | 合法转换原子提交；非法、旧 CAS 和缺 Artifact 转换被拒绝 |
| `LEASE-001` | stale worker、双派发、旧 fencing token | 只有新 owner 能提交状态/Artifact；外部副作用最多形成一个事实 |
| `PROC-001` | Claude 运行时杀死并重启 UI | CLI 与 Agent 继续；重连无 taskSeq 缺口；进程身份一致 |
| `CTRL-001` | 各 Step 类型软暂停、立即停止、取消和恢复 | 符合 safe point/grace；旧 Attempt 不复用；取消撤销授权且不可恢复 |
| `STREAM-001` | JSONL 断行、重复、乱序、跨 chunk 秘密、重连 | 正确拼帧/去重/脱敏/重放；未脱敏原文不落盘 |
| `STREAM-002` | CLI exit 0 但终止 frame、offset 或必需 Artifact 缺失 | fail closed，不能生成成功 Gate |
| `REVIEW-001` | Codex reject 后 Claude 修复；审核后 SHA 漂移 | finding 闭环；漂移使 receipt 失效并重跑 Verifier/Codex |
| `GIT-001` | PR、CI、merge 完整流程 | base/candidate/reviewed/remote/merge SHA 一致，用户工作区不受污染 |
| `SIDEFX-001` | push/PR/registry 已成功但响应丢失 | reconcile 查到完成事实，不重复产生外部对象 |
| `DEPLOY-001` | 可销毁 Linux 上生产等价成功发布 | digest 部署、迁移、切流、业务验收、soak、Codex 证据审核全部通过 |
| `DEPLOY-002` | 切流前/后故障 | 按 RollbackPlan 恢复旧版本，验收通过，lifecycle=`ROLLED_BACK` |
| `DEPLOY-003` | 回滚再次失败 | lifecycle=`FAILED_NEEDS_INTERVENTION`，现场可诊断、锁无僵死且发一次 ACTION_REQUIRED |
| `STORE-001` | SQLite/WAL 损坏、D 盘满、Artifact 原子 rename 失败 | 从有效备份恢复或 fail closed；无半提交 Artifact/假成功 |
| `NOTIFY-001` | 自动修复、完成、BLOCKED、发送失败和 Agent 重启 | 中间过程零通知；允许结果按去重键恰好成功一次或留明确失败 receipt |
| `PATH-001` | 干净 Windows 安装与全流程文件系统快照 | 所有 StorageLocationContract 可控写入位于 D 盘；OS 白名单逐项披露 |

每个测试必须产生机器可读报告，至少包含测试 ID、环境 manifest digest、动作、期望、实际状态、外部副作用计数、Artifact digest 和最终 receipt。失败测试不能通过手工文字改为成功。

### 20.3 Compatibility Manifest

每个可交付版本必须附 `compatibility-manifest.json`，冻结并签名实际验证过的 Windows build、WebView2、WSL 版本、Linux distro、Docker Desktop/Engine/Compose、Tauri、Agent/Python、Claude Code CLI、Codex CLI 和基础镜像 digest。发布支持范围严格等于 manifest 中通过合同测试的组合；未知 CLI/schema 或运行时版本先进入只读诊断并 `BLOCKED`，不能猜测兼容。

首个完整版本的发布门禁至少覆盖：一个仍受厂商支持的 Windows 11 x64 build、一个 WSL2 下的 Ubuntu LTS、一个标准 x86_64 Linux SSH/Compose 环境，以及当前锁定的 Claude/Codex CLI 版本。确切版本在实现发布时写入 manifest，而不是在概念规格中漂移。

## 21. 实施拆分

本产品范围较大，因此使用一个 Master Spec 管理端到端契约，并拆成六个可独立验收的实现工作流。拆分是为了降低集成风险，不表示交付单功能 MVP。

1. **Desktop Shell：**Tauri、React、托盘、自启动、单实例、更新、Command Center。
2. **Control Plane：**状态机、SQLite/Event Store、资源租约、恢复和并发。
3. **AI Harness：**Claude/Codex Adapter、Tool Broker、进程管理和真实终端。
4. **Verification：**隔离 Verifier、门禁、review loop 和 Artifact。
5. **Deployment：**ServerProfile、SSH Compose、验收、回滚和证据复核。
6. **Hardening：**Vault、安全、备份、安装包、文档和整体 E2E。

每个节点完成条件均为：实现、必要中文注释、结构化日志、相关测试、需求审查、代码审查和本地原子提交全部通过。

## 22. Definition of Done

只有下表无条件项全部 PASS，AI Coding Factory 才能宣称首个完整版本交付：

| DoD ID | 环境与操作 | 机器判定 | 必需回执 |
| --- | --- | --- | --- |
| `DOD-APP-001` | Compatibility Manifest 中的干净 Windows；安装、启动、托盘、升级 | 主界面不依赖外部浏览器；关闭 UI 后 Agent 继续；签名/版本一致 | packaging/E2E report |
| `DOD-STAGE-001` | 执行 `STAGE-001..006` | 六个目标均真实止步且 capability 不越界 | 6 份 final task receipt |
| `DOD-OBS-001` | 执行 `PROC-001`、`STREAM-001..002` | 真实进程、连续事件、脱敏来源和回放全部匹配 | event-chain verification receipt |
| `DOD-AI-001` | Claude→Verifier→Codex 闭环及返工 | 所有 blocking/high finding 关闭；reviewed SHA 精确一致 | verifier + review receipts |
| `DOD-CTRL-001` | 执行 `STATE/LEASE/CTRL/STORE` 测试 | 暂停、停止、取消、崩溃、重启和未知状态无假成功 | recovery/chaos report |
| `DOD-GIT-001` | 新/旧项目、worktree、PR、merge | 用户工作区零污染；Git 身份链一致 | Git identity receipt |
| `DOD-DEPLOY-001` | 可销毁、生产等价 Linux；成功与故障流程 | 发布、真实业务验收、soak、回滚和回滚失败语义全部通过 | release/acceptance/rollback receipts |
| `DOD-SEC-001` | 威胁测试、secret scan、StorageLocationContract 快照 | 无明文凭据、无跨任务访问、可控写入都在 D 盘 | security + path report |
| `DOD-OPS-001` | 备份恢复、升级失败、诊断演练 | 达到本地 RTO/RPO；Runbook 步骤可复现 | operations drill receipt |
| `DOD-REVIEW-001` | Codex 对完整产品和上述证据做最终只读审核 | 冻结 rubric 下无 open blocker/high | final product review receipt |

云厂商实机认证是条件式交付项：用户提供临时 ECS/CVM 或其他真实 ServerProfile 后，分别运行 `DEPLOY-001..003` 并生成 `cloud-certification-receipt`。缺少某厂商凭据不阻塞通用产品的本地完整交付，但该厂商必须显示“未认证”，不得宣传为已验证兼容。

## 23. 交付物

- Windows 安装包或便携包，以及 D 盘数据根配置。
- 完整源码、测试、CI、部署适配器和版本信息。
- 架构文档、事件协议、威胁模型和服务器接入手册。
- 备份恢复、故障排查、发布回滚和升级 Runbook。
- Codex 最终审核回执和全链路验收报告。

## 24. 外部依赖与诚实边界

以下内容不是规格占位符，而是实施和验收时的外部前置条件：

- 用户需要在设置中完成 Claude Code 和 Codex CLI 登录。
- 真实云服务器验收需要用户以后提供 ServerProfile、域名/网络和相应凭据引用。
- 生产级签名安装包需要可用的 Windows 代码签名证书；没有证书时只能交付未签名测试包或便携包，并会有系统警告。
- 应用可保证所有可控项目、日志、缓存、运行数据、WebView 数据目录、WSL/Docker 数据位置配置在 D 盘。Windows 注册表、通知数据库、系统日志、WebView2/系统运行时更新等操作系统元数据无法由应用承诺绝对不写 C 盘。
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
