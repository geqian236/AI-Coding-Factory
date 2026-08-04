## PR 概要

<!-- 简短说明本 PR 的目标和改动范围 -->

## Plan Task

- Plan Task: <!-- e.g. Phase 0 Task 1 -->
- Reviewed Head SHA: <!-- reviewedHeadSha — Claude self-review 绑定的候选 SHA -->
- CI Test Merge SHA: <!-- ciTestMergeSha — GitHub Actions test-merge SHA -->
- Base SHA: <!-- 本次 base commit SHA -->

## 改动内容

<!-- 列出主要改动文件和逻辑 -->

## 测试验证

- [ ] 合同测试通过：`python -m pytest tests/contract/ -q`
- [ ] CI checks 全部绿色
- [ ] 无 C 盘路径写入（scripts/dev.ps1 验证）

## Claude Self-Review

- Self-review result: <!-- pass | needs_fix -->
- Rubric version: <!-- e.g. v1.0 -->
- Evidence digests: <!-- sha256 摘要列表 -->

## Codex Receipt

- Verifier receipt digest: <!-- sha256 -->
- Codex delivery actor: <!-- actor 标识 -->

## 风险与恢复说明

<!-- 说明潜在风险和回滚方案 -->

## Checklist

- [ ] 公共符号有中文注释
- [ ] 后端/agent 改动使用正式 logger（无裸 print）
- [ ] 不含密钥、token 或敏感信息
- [ ] 只暂存本次相关路径
