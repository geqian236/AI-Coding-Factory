# AI Coding Factory

多语言 monorepo，实现 AI 辅助编码流水线。所有开发数据绑定到 D 盘，避免污染系统盘。

## 快速开始

### 前置条件

- Windows 11 + WSL2
- Python 3.12（通过 pyenv-win 或官方安装包）
- Node.js 22（通过 nvm-windows）
- pnpm 9
- Rust stable（通过 rustup）
- uv（Python 包管理）

### 环境自举

```powershell
# 以 D 盘安全模式验证环境
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/bootstrap-dev.ps1 -VerifyOnly
```

### 运行合同测试

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/test_repository_layout.py -q
```

### 运行全部检查

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/check.ps1
```

## 目录结构

```
contracts/        语言中立合同（schema、policy、golden vectors）
packages/         TypeScript/Node 包
apps/             应用（agent 等）
crates/           Rust crate
tools/            工具脚本与兼容性探针
scripts/          开发脚本
tests/            合同与集成测试
docs/             文档
```

## D 盘安全规则

所有运行时缓存（Cargo、pnpm、uv、npm、Playwright）均由 `scripts/dev.ps1` 绑定到
`D:\codex项目\AI-Coding-Factory-Data\dev`，不写入 C 盘。
