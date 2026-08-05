# AI Coding Factory — Development Environment Setup

> **版本**：v1（Phase 0 基线，2026-08-04）
>
> **适用平台**：Windows 11 + WSL2 + Docker Desktop

---

## 1. 前提条件

| 工具 | 要求 | 验证命令 |
|------|------|---------|
| Python | 3.12+ | `python --version` |
| Node.js | 见 `.node-version` | `node --version` |
| pnpm | 9+ | `pnpm --version` |
| Rust toolchain | 见 `rust-toolchain.toml` | `rustc --version` |
| WSL2 | Ubuntu 22.04+ | `wsl --version` |
| Git | 2.40+ | `git --version` |

所有开发工具的数据目录（缓存、包、编译产物）必须位于 D 盘，由 `scripts/dev.ps1` 强制绑定。

---

## 2. 快速启动

### 2.1 克隆仓库

```powershell
cd D:\codex项目
git clone <repo-url> AI-Coding-Factory
cd AI-Coding-Factory
```

### 2.2 引导开发环境

```powershell
# 以 D 盘 wrapper 执行引导脚本
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap-dev.ps1
```

引导脚本完成后：
- Python 虚拟环境位于 `D:\codex项目\AI-Coding-Factory-Data\dev\venv\`
- pnpm store 位于 `D:\codex项目\AI-Coding-Factory-Data\dev\pnpm-store\`
- Rust target 目录位于 `D:\codex项目\AI-Coding-Factory-Data\dev\cargo-target\`

### 2.3 验证引导结果

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap-dev.ps1 -VerifyOnly
```

期望输出：D 盘路径摘要（JSON），无 C 盘写入报告。

---

## 3. D 盘路径规则

### 3.1 scripts/dev.ps1 包装器

所有开发工具命令必须通过 `scripts/dev.ps1 -- <command>` 运行，该包装器：

1. 解析环境变量 `TEMP/TMP/CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME/PNPM_HOME/NPM_CONFIG_CACHE/UV_CACHE_DIR/PIP_CACHE_DIR/PLAYWRIGHT_BROWSERS_PATH`。
2. 将所有路径绑定到 `D:\codex项目\AI-Coding-Factory-Data\dev\`。
3. 执行前后做文件系统快照，如发现可控 C 盘写入则以非零退出。

### 3.2 禁止的 C 盘操作

以下操作在源码中禁止（由 check.ps1 检查 7 执行）：
- 硬编码 `C:\` 或 `C:/` 路径
- 使用 `%APPDATA%`、`%LOCALAPPDATA%`、`%USERPROFILE%` 而未经 D 盘覆盖
- 直接写入 `os.path.expanduser("~")` 或等价路径

### 3.3 允许例外

以下 C 盘路径允许只读访问：
- `C:\Windows\System32\` — 系统工具调用
- `C:\Program Files\` — 已安装程序的只读引用

---

## 4. 日常开发工作流

### 4.1 运行测试

```powershell
# Python 合同测试
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    python -m pytest tests/contract -q

# Python 单元测试（Phase 1 起生效）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    python -m pytest tests/agent/unit -q

# TypeScript 合同包测试
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    pnpm --filter @factory/contracts test

# Rust 合同测试
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    cargo test -p factory-contracts
```

### 4.2 运行完整测试套件

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
```

### 4.3 运行门禁检查（提交前必须通过）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

9 项检查全部通过才允许提交，详见 [contracts-v1.md](../protocols/contracts-v1.md) 第 11 节。

### 4.4 代码生成

```powershell
# 重新生成三语言类型文件
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    python contracts/codegen/generate.py

# 验证无漂移
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- `
    python contracts/codegen/generate.py --check
```

---

## 5. 分支与提交规则

- 非琐碎改动使用聚焦分支，命名为 `codex/<task>`。
- 并行写任务使用独立 worktree，防止文件冲突。
- 只暂存本次相关路径，禁止 `git add .` 混入无关改动。
- 提交前运行 `scripts/check.ps1`；Phase 0 不推送到远端。

### 提交消息格式

```
<type>(<scope>): <subject>

<body>（可选，说明背景和技术决策）
```

类型前缀：`feat`、`fix`、`chore`、`test`、`docs`、`refactor`。

---

## 6. 代码质量规范

### 6.1 中文注释要求

以下位置必须添加中文注释（由 check.ps1 检查 4 执行）：
- 新增模块的模块级 docstring
- 新增或修改的公共类的类级 docstring
- 新增或修改的公共函数的函数级 docstring
- 复杂算法、状态流转、异常处理的行内中文注释

不需要对简单赋值、显然的 getter/setter 额外添加注释。

### 6.2 禁止裸输出

以下在非测试源码中禁止（由 check.ps1 检查 5 执行）：
- Python: `print(...)` — 使用 `logger.info(...)` 代替
- TypeScript: `console.log(...)` — 使用结构化 logger 代替

### 6.3 结构化日志

所有后端 / agent / 外部集成代码使用正式 logger，日志必须包含：
- `request_id` / `task_id` / `run_id` / `step_id` / `attempt_id` / `trace_id`
- 脱敏后的上下文（不得记录 token、password、private_key 原文）

---

## 7. 安全规范

### 7.1 秘密扫描

以下模式不得出现在非测试源码中（由 check.ps1 检查 6 执行）：
- `token = "..."` / `password = "..."` / `private_key = "..."`
- 任何包含实际凭据值的字符串字面量

凭据引用必须使用 `credential-ref.v1` schema 中的 `credentialId`，不含明文值。

### 7.2 存储位置合同

所有数据目录必须通过 `StorageLocationContract` 验证（Phase 0 为 dry-run 模式）：
- 句柄验证：确认物理卷是 D 盘
- reparse point 检测：拒绝 junction / symlink 指向 C 盘
- SUBST / 网络卷拒绝

---

## 8. Compatibility Spike 状态

| Spike | 状态 | 认证文件 |
|-------|------|---------|
| Windows Durable IO | 已认证 (PASS) | `tools/compat-probes/windows_durable_io/receipt.json` |
| SQLite WAL FULL | 已认证 (PASS, disk-full BLOCKED_UNCERTIFIED) | `tools/compat-probes/sqlite_wal_full/receipt.json` |
| Clock Source | 已认证 (PASS) | `tools/compat-probes/clock_source/receipt.json` |
| Named Pipe | 已认证 (PASS) | `tools/compat-probes/named_pipe/receipt.json` |
| Runner Identity | 已认证 (PASS) | `tools/compat-probes/runner_identity/receipt.json` |
| Git Object Bridge | 已认证 (PASS) | `tools/compat-probes/git_object_bridge/receipt.json` |
| Tauri E2E | 待 Phase 2 | `tools/compat-probes/tauri_e2e/` |

所有 Phase 0 receipt 均包含 `assertions` 数组（每条有 `name/passed/detail`）和
`observable_facts`，status 来自真实可观测行为；disk-full 子结果诚实标记
`BLOCKED_UNCERTIFIED` 并附解封步骤，不伪造兼容。

---

## 9. 常见问题

### Q: 运行 check.ps1 时报告 C 盘路径？

检查源码中是否有硬编码路径字符串。使用 `REPO_ROOT = Path(__file__).resolve().parents[N]` 代替绝对路径。

### Q: codegen --check 报告漂移？

重新运行 `python contracts/codegen/generate.py` 更新生成文件，然后提交更新后的 `generated/` 文件。

### Q: 如何添加新 schema？

1. 在 `contracts/schemas/` 创建 `<name>.v<N>.schema.json`。
2. 在 `contracts/codegen/catalog.v1.json` 的 `schemas` 数组中添加条目。
3. 运行 `python contracts/codegen/generate.py` 重新生成。
4. 更新相关测试（`test_schema_catalog.py` 中的 `EXPECTED_SCHEMA_FILES`）。

---

*本文档由 Task 8 生成，Phase 0 全部 spike 认证完毕（2026-08-05，HEAD: ef9a33f）。*
