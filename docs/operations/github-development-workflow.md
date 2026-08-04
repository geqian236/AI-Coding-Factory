# GitHub 开发工作流

## 概述

本文档描述 AI Coding Factory 在 GitHub 上的标准开发工作流，包括分支策略、PR 流程、
Required Checks 和 Receipt 验证规则。

## 分支策略

- `main` — 保护分支，只能通过 PR 合并
- `codex/<task>` — 主要开发分支命名规范（例：`codex/phase-0-task-1`）
- 并行写任务使用独立 worktree，避免文件冲突

## Required Checks

以下 GitHub Actions check 必须全部通过才能合并 PR：

| Check 名称           | 说明                              |
|----------------------|-----------------------------------|
| `plan-consistency`   | 计划文件一致性验证                |
| `contracts`          | 合同测试（Python pytest）         |
| `python`             | Python lint/type check            |
| `rust`               | Cargo check/test                  |
| `desktop`            | 桌面兼容性（Windows-only neutral） |
| `security`           | 秘密扫描                          |

**重要：** 以上 check 名称固定，不得通过 path filter 让 required check 消失。
按路径跳过只能在 job 内产生明确 `neutral/success` 结论。

## PR Receipt 规范

每个 PR 必须在描述中包含：

1. **reviewedHeadSha** — Claude self-review 绑定的候选 commit SHA
2. **ciTestMergeSha** — GitHub Actions test-merge SHA（非 `GITHUB_SHA`）
3. **base SHA** — 本次 base commit SHA
4. **Verifier receipt digest** — sha256 摘要
5. **Codex delivery actor** — 执行 agent 标识

## Merge 规则

- 首版不启用 merge queue
- Ruleset 必须启用 required checks strict/up-to-date
- 未来启用 merge queue 前必须另加 `merge_group` 触发器
- `factory/codex-review` check 等 Phase 4 Task 8 完成后才加入 required checks

## 本地验证命令

```powershell
# 运行合同测试
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- python -m pytest tests/contract/ -q

# 运行全部检查
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/check.ps1

# 验证 D 盘环境
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev.ps1 -- powershell -NoProfile -File scripts/bootstrap-dev.ps1 -VerifyOnly
```
