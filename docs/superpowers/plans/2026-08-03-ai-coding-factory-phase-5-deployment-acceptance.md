# AI Coding Factory Phase 5 Deployment and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现云厂商无关的 ServerProfile、凭据 Broker、OCI 构建、fenced SSH/Compose 发布、Nginx 切流、数据库备份迁移、真实验收、自动回滚和跨任务 target guard，使 staging/production 目标可真实完成。

**Architecture:** Release Runner 是独立信任区；控制平面签发绑定 release/resource/token 的短期授权，远端每个写入口都校验 fencing token 并追加 fsync journal。发布、验收、回滚以不可变 Plan 和真实远端 identity 为依据，响应丢失一律先 reconcile。

**Tech Stack:** Python、OpenSSH、Docker Engine/Compose/buildx、OCI Registry、Nginx、数据库原生工具、DPAPI Vault、pytest release E2E、可销毁标准 Linux 主机。

**Command contract:** 本文件所有开发、测试和门禁命令都通过总计划 §8 的 `scripts/dev.ps1 --` 运行；正文短命令只表示 wrapper 参数。

---

## Task 1: 实现 Server/Database Profile 和 DPAPI Vault

**Files:**

- Create: `apps/agent/src/factory_agent/domain/profiles.py`
- Modify: `apps/agent/src/factory_agent/security/dpapi_vault.py`
- Modify: `apps/agent/src/factory_agent/security/credential_broker.py`
- Create: `apps/agent/src/factory_agent/storage/sqlite/profile_repository.py`
- Create: `contracts/schemas/server-profile.v1.schema.json`
- Create: `contracts/schemas/database-profile.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `apps/desktop/src/features/settings/ServerProfiles.tsx`
- Create: `apps/desktop/src/features/settings/DatabaseProfiles.tsx`
- Create: `apps/desktop/src/features/settings/CredentialRefs.tsx`
- Test: `tests/security/test_dpapi_vault.py`
- Test: `tests/agent/integration/profiles/test_profile_revisions.py`
- Test: `tests/agent/integration/profiles/test_credential_scope.py`

- [ ] 写 profile revision 不可变、credential 明文入库/日志、scope/TTL 越界、同用户外进程边界说明和 guard 被新 revision 绕过测试。
- [ ] `server-profile.v1` 与 `database-profile.v1` 必须 `$ref` Phase 0 的 `credential-ref.v1`；实现 D 盘 DPAPI 加密 Vault、ACL 和只保存 credentialRef/fingerprint/expiry 的 Profile，Broker 只向受限 Release/Acceptance Runner 短期解封。
- [ ] UI 不显示秘密值；更新秘密创建新 fingerprint/revision，自动撤销不匹配授权。
- [ ] 在同一 Task 登记 Profile schema 分类，运行普通 codegen 和 `--check`；再运行 profile/security 测试，Expected: 三语言 CredentialRef/Profile 合同一致、明文 secret scan 0、权限越界 fail closed。
- [ ] 提交：`feat(security): add scoped DPAPI deployment vault`。

## Task 2: 实现服务器只读发现和授权 bootstrap

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/ssh/client.py`
- Create: `apps/agent/src/factory_agent/integrations/ssh/known_hosts.py`
- Create: `apps/agent/src/factory_agent/integrations/ssh/operations.py`
- Create: `apps/agent/src/factory_agent/application/server_onboarding_service.py`
- Create: `ops/remote/bootstrap.sh`
- Create: `ops/remote/validate-envelope.sh`
- Modify: `runners/images/release/Dockerfile`
- Create: `apps/desktop/src/features/settings/ServerOnboarding.tsx`
- Test: `tests/integration/server/test_onboarding.py`
- Test: `tests/security/test_ssh_operations.py`

- [ ] 写 host-key 漂移、任意 shell、sudo 越界、发现阶段意外写、已有 `.factory` 冲突和授权取消测试。
- [ ] 只读发现 OS/arch/Docker/Compose/Nginx/磁盘/内存/端口/remoteRoot；BootstrapPlan 明确每项写和回滚。
- [ ] 经用户一次性 bootstrap 授权后安装 `ops/remote` 的固定 digest helper、创建 deploy 用户/目录；Release 镜像只 COPY 同一 digest，普通发布不再拥有任意 sudo。
- [ ] 运行 server integration，Expected: 发现零写，bootstrap 可重复核对，host key 固定。
- [ ] 提交：`feat(server): add read-only discovery and authorized bootstrap`。

## Task 3: 实现一次构建、SBOM/provenance 和 OCI digest 链

**Files:**

- Create: `apps/agent/src/factory_agent/application/artifact_build_service.py`
- Modify: `apps/agent/src/factory_agent/ports/build.py`
- Modify: `apps/agent/src/factory_agent/integrations/buildkit/broker.py`
- Modify: `runners/images/buildkit/policy.json`
- Create: `apps/agent/src/factory_agent/integrations/registry/oci.py`
- Create: `apps/agent/src/factory_agent/integrations/registry/observer.py`
- Create: `docker-bake.hcl`
- Create: `contracts/schemas/build-artifact-receipt.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/integration/registry/test_oci_identity.py`
- Test: `tests/integration/registry/test_registry_observer.py`
- Test: `tests/chaos/test_registry_response_loss.py`

- [ ] 写 tag-only、candidate SHA 漂移、push 响应丢失、registry digest 查询不一致、SBOM/provenance 缺失、临时 Docker config 泄漏和绕过 Build Broker 测试。
- [ ] Verifier FINAL gate 通过后，受信 Build Broker 用绑定 candidate SHA/context digest/platform/policy digest 的请求构建一次 OCI 镜像；Verifier 容器始终看不到 Docker socket。后续 staging/production 只使用同一 digest，不远端现场构建。
- [ ] registry push 和 observe 使用不同 capability；push/inspect 是 pause safe point，已发送后必须先 reconcile。完成后删除临时 D 盘 `DOCKER_CONFIG` 并写 receipt。
- [ ] 在同一 Task 登记 build receipt schema 分类，运行普通 codegen 和 `--check`；再运行 registry/side-effect chaos，Expected: digest identity 收敛且无重复 image object。
- [ ] 提交：`feat(oci): add SBOM-backed immutable image chain`。

## Task 4: 实现 DeployPlan、远端 journal 和双层锁

**Files:**

- Create: `apps/agent/src/factory_agent/domain/deployment.py`
- Create: `apps/agent/src/factory_agent/release/deploy_plan.py`
- Create: `apps/agent/src/factory_agent/release/journal.py`
- Create: `apps/agent/src/factory_agent/release/release_lock.py`
- Create: `apps/agent/src/factory_agent/release/migration_lock.py`
- Create: `contracts/schemas/deploy-plan.v1.schema.json`
- Create: `contracts/schemas/release-journal.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Create: `ops/remote/lock.sh`
- Create: `ops/remote/journal.sh`
- Test: `tests/agent/unit/release/test_deploy_plan.py`
- Test: `tests/agent/unit/release/test_release_journal.py`
- Test: `tests/agent/unit/release/test_release_lock.py`
- Test: `tests/chaos/release/test_fencing.py`

- [ ] 写计划 hash 漂移、只读预检前写入、双 Release Runner、旧 token 在上传/pull/备份/启动/切流继续写和 journal 未 fsync 测试。
- [ ] 首次远端业务写前取得 release lock；数据库迁移前再取得 migration lock；每个 `ops/remote` helper 先调用唯一 `validate-envelope.sh`，比较 releaseId/fencingToken/stepSeq/idempotencyKey。
- [ ] STARTED、迁移、切流、回滚记录持久化后才推进；按 node pause policy 标记远端请求边界和不可中断 journal 关键区，暂停必须等待副作用终态或转 UNKNOWN/RECONCILING；重连校验 journal、锁和真实容器/数据库/Nginx。
- [ ] 在同一 Task 登记 deploy/journal schema 分类，运行普通 codegen 和 `--check`；再运行 `DEPLOY-FENCE-001` chaos，Expected: 旧 token 所有写入口拒绝，无双发布。
- [ ] 提交：`feat(release): add fenced deploy plan and remote journal`。

## Task 5: 实现 SSH Compose 发布主链和内存预检

**Files:**

- Create: `apps/agent/src/factory_agent/release/release_service.py`
- Create: `apps/agent/src/factory_agent/integrations/ssh/release_adapter.py`
- Create: `apps/agent/src/factory_agent/integrations/docker/remote_compose.py`
- Create: `ops/remote/upload.sh`
- Create: `ops/remote/pull.sh`
- Create: `ops/remote/compose.sh`
- Create: `runners/images/release/templates/compose.yaml.j2`
- Test: `tests/release-e2e/test_deploy_success.py`
- Test: `tests/release-e2e/test_memory_guard.py`

- [ ] 写磁盘/端口/RAM/swap/container watermark、旧栈健康、候选启动、pull digest 和 Compose config 失败测试。
- [ ] 实现 §15.4 固定发布顺序；只读预检在锁和任何写之前，候选按 release ID 隔离目录。upload/pull/Compose 请求一旦发送，暂停只能等待 receipt settled 或进入 RECONCILING，不能取消后伪造 PAUSED。
- [ ] blue-green 计算宿主保留、旧栈 limit/峰值、候选/migration/backup 峰值和 safety margin；证据不足或余量不足不启动候选，只有已授权 recreate 才降级。
- [ ] 在可销毁 Linux 运行成功路径，Expected: digest/容器/config identity 全一致，`DEPLOY-RAM-001` PASS。
- [ ] 提交：`feat(release): add fenced SSH Compose deployment`。

## Task 6: 实现数据库备份、迁移和隔离恢复演练

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/database/probe.py`
- Create: `apps/agent/src/factory_agent/integrations/database/backup.py`
- Create: `apps/agent/src/factory_agent/integrations/database/migration.py`
- Create: `apps/agent/src/factory_agent/integrations/database/restore_drill.py`
- Create: `apps/agent/src/factory_agent/release/database_service.py`
- Create: `apps/agent/src/factory_agent/release/restore_drill_node.py`
- Create: `contracts/schemas/backup-receipt.v1.schema.json`
- Create: `contracts/schemas/restore-drill-receipt.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/release-e2e/test_backup_restore.py`
- Test: `tests/chaos/release/test_migration_failure.py`
- Test: `tests/security/test_restore_drill_scope.py`

- [ ] 写错误数据库 identity、同故障域 backup、checksum/加密/可读性失败、drill 过期、migration checksum 漂移、恢复覆盖生产库和未知 `RESTORE_DRILL` capability 测试。
- [ ] 每次发布验证真实 backup identity/大小/checksum/可读性；完整 restore drill 只在首次/到期/高风险触发并恢复到隔离实例。
- [ ] `RESTORE_DRILL` 节点只派生 `db.restore/restore.validation.instance/db.check`，resource fingerprint 必须机械证明非生产实例；migration/restore 关键区按 pause policy 完成 journal 后才能暂停。migration job 使用同一镜像 digest、scoped DB 账户且无 Docker socket/模型/其他环境网络。
- [ ] 在同一 Task 登记 backup/restore schema 分类，运行普通 codegen 和 `--check`；再运行 `BACKUP-001`，Expected: 无有效 drill 的 `restore_required` fail closed，生产库从未被演练覆盖。
- [ ] 提交：`feat(database): add backup migration and isolated restore drill`。

## Task 7: 实现 Nginx ownership、切流和漂移核对

**Files:**

- Create: `apps/agent/src/factory_agent/integrations/nginx/existing.py`
- Create: `apps/agent/src/factory_agent/integrations/nginx/external.py`
- Create: `apps/agent/src/factory_agent/ports/traffic.py`
- Create: `ops/remote/nginx.sh`
- Create: `runners/images/release/templates/nginx-upstream.conf.j2`
- Test: `tests/release-e2e/test_nginx_drift.py`

- [ ] 写第三方在 `nginx -t` 前、reload 前、reload 后修改配置和 certbot hook 竞争测试。
- [ ] `existing` 只写独占 include；reload 是不可中断关键区，开始后必须取得终态 receipt；reload 前后比较完整 `nginx -T` hash，预期外漂移 fail closed/RECONCILING。
- [ ] receipt 明示 reload 会激活磁盘全部配置的残余风险；不接受者使用 `external` Traffic Adapter，无 Adapter 时 BLOCKED。
- [ ] 运行 `NGINX-001`，Expected: 冲突不误报成功、不加载我方已知冲突。
- [ ] 提交：`feat(nginx): add ownership-aware traffic switching`。

## Task 8: 实现 AcceptancePlan 和 Codex 发布证据审核

**Files:**

- Create: `apps/agent/src/factory_agent/domain/acceptance.py`
- Create: `apps/agent/src/factory_agent/release/acceptance_service.py`
- Create: `apps/agent/src/factory_agent/integrations/http/acceptance.py`
- Create: `apps/agent/src/factory_agent/integrations/logs/window_checker.py`
- Create: `apps/agent/src/factory_agent/prompts/codex_release_evidence_review.md`
- Create: `contracts/schemas/acceptance-report.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/release-e2e/test_acceptance.py`

- [ ] 写 skipped blocking check、错误 digest/SHA/config、fixture 未清理、日志泄密/错误阈值、重启不允许和 soak 未达时长测试。
- [ ] 实现 Git/release/digest/config/schema、容器健康、端点/TLS/upstream、真实业务、日志、DB 不变量、重启和 soak 检查。
- [ ] 每项产生 COMMITTED evidence Artifact；Codex 只读复核精确证据包，模型不能替代 blocking matcher。
- [ ] 在同一 Task 登记 acceptance schema 分类，运行普通 codegen 和 `--check`；再运行 staging/production acceptance，Expected: 全部 blocking PASS 才更新 achieved stage。
- [ ] 提交：`feat(acceptance): add evidence-backed release acceptance`。

## Task 9: 实现 RollbackPlan、失败语义和 target guard 清除

**Files:**

- Create: `apps/agent/src/factory_agent/domain/rollback.py`
- Create: `apps/agent/src/factory_agent/release/rollback_service.py`
- Create: `apps/agent/src/factory_agent/release/target_reconciliation.py`
- Create: `contracts/schemas/rollback-plan.v1.schema.json`
- Create: `contracts/schemas/reconciliation-plan.v1.schema.json`
- Modify: `contracts/codegen/catalog.v1.json`
- Modify: `packages/factory-contracts/src/generated/contracts.ts`
- Modify: `apps/agent/src/factory_agent/contracts/generated/models.py`
- Modify: `crates/factory-contracts/src/generated/contracts.rs`
- Test: `tests/release-e2e/test_rollback.py`
- Test: `tests/release-e2e/test_guard_reconcile.py`
- Test: `tests/chaos/release/test_rollback_failure.py`

- [ ] 写切流前/后、migration 后未切流、schema 不兼容、回滚再次失败、新 Task/Profile 绕 guard、无证据清 guard，以及 lifecycle/receipt/event/guard/notification 每个事务写点失败测试。
- [ ] 实现五种 DB rollback 类型；`forward_fix_only` 不宣称数据回滚，`restore_required` 验证 drill/capability/RPO/RTO。
- [ ] 回滚再次失败时，在释放 lease 前用一个 SQLite 事务提交 lifecycle=`FAILED_NEEDS_INTERVENTION`、rollback failure receipt、`state.changed`、全部 Server/DB/registry `INTERVENTION_REQUIRED` guard 和唯一 ACTION_REQUIRED Notification Action；任一写失败整体回滚，guard 不随 TTL 消失。
- [ ] 清 guard 只允许专用 ReconciliationPlan、observe capability、`target.guard.clear` 和一次性 CAS；在同一 Task 登记 rollback/reconciliation schema 分类，运行普通 codegen 和 `--check`，生成树漂移则禁止提交。
- [ ] 提交：`feat(rollback): add guarded database-aware recovery`。

## Task 10: Phase 5 完整发布 E2E Gate

**Files:**

- Create: `ops/lab/linux-target/cloud-init.yaml`
- Create: `scripts/test-release-e2e.ps1`
- Create: `docs/operations/server-onboarding.md`
- Create: `docs/operations/release.md`
- Create: `docs/operations/backup-restore.md`
- Create: `docs/operations/rollback.md`
- Create: `docs/operations/failed-needs-intervention.md`
- Modify: `apps/agent/src/factory_agent/bootstrap.py`
- Modify: `apps/agent/src/factory_agent/ipc/handlers.py`
- Modify: `apps/agent/src/factory_agent/scheduler/dispatcher.py`
- Modify: `apps/agent/src/factory_agent/harness/workflows/stage_pipeline.py`
- Modify: `apps/desktop/src/app/router.tsx`
- Modify: `apps/desktop/src/features/settings/SettingsPage.tsx`
- Test: `tests/release-e2e/test_composed_desktop_release.py`

- [ ] 先运行 `generate.py --check` 复核 Tasks 1/3/4/6/8/9 已原子提交的 catalog/generated；随后将 Profile/Vault、Build/Registry、SSH/DB/Nginx、Acceptance/Rollback 注册到 stage pipeline、Agent composition root、IPC/dispatcher 和桌面设置 route，准备可销毁生产等价 Linux ServerProfile，从桌面发起真实 SSH/Compose/Nginx/DB 流程，不用 mock 冒充最终 PASS。
- [ ] 运行 `STAGE-005..006`、`AUTH-002`、`LEASE-001`、`RETRY-001`、`DEPLOY-001..003`、`GUARD-001`、`DEPLOY-FENCE-001`、`NGINX-001`、`BACKUP-001`、`DEPLOY-RAM-001` 和 `SIDEFX-001` 的完整 scenario contract。
- [ ] 验证成功发布、切流前/后失败、回滚成功和回滚再失败；另覆盖权限/scope/TTL/授权失效，双 executor/旧 lease/旧授权，以及 Provider `Retry-After`、quota exhausted、SSH 响应丢失三类机械分流。检查 locks/journal/guards/通知/Artifact 无僵尸或假成功；全部通过后由 Phase 5 唯一 final owner 签发 `AUTH-002`、`LEASE-001`、`RETRY-001` FINAL。
- [ ] Codex 只读审核 source/image/release/config/acceptance/rollback identities；无 blocker/high 才关闭阶段。
- [ ] 提交：`test(release): certify staging production and rollback chain`。

真实云厂商认证只有用户提供临时 ECS/CVM 等 ServerProfile 后才执行；缺少某厂商环境时标记“未认证”，不影响实现代码完成，但不能生成该厂商兼容 PASS。
