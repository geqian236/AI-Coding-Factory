"""Phase 1 授权合同测试：冻结 schema、策略字段和三语言生成物的 fail-closed 边界。"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
import pytest
from factory_agent.policy import plan_hash
from factory_agent.policy.canonical_json import canonicalize

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "contracts" / "schemas"
CATALOG_PATH = REPO_ROOT / "contracts" / "codegen" / "catalog.v1.json"
POLICY_PATH = REPO_ROOT / "contracts" / "policies" / "node-capability-map.v1.json"
# raw-file golden 与 CompatibilityManifest v1 使用同一算法；精确策略表负责定位字段漂移。
EXPECTED_NODE_POLICY_RAW_FILE_SHA256 = (
    "sha256:25da2cbe791024fbe31475f45c2302d84c5da3b53cafeae2f3c791551d8abff5"
)
SNAPSHOT_REGISTRY_PATH = REPO_ROOT / "contracts" / "policies" / "authorization-snapshot-registry.v1.json"
RUN_SPEC_SCHEMA_PATH = SCHEMAS_DIR / "run-spec.v1.schema.json"
PLAN_REVISION_SCHEMA_PATH = SCHEMAS_DIR / "plan-revision.v1.schema.json"
MANIFEST_SCHEMA_PATH = REPO_ROOT / "contracts" / "schemas" / "compatibility-manifest.v1.schema.json"
EMIT_MANIFEST_PATH = REPO_ROOT / "tools" / "compat-probes" / "emit_manifest.py"
CODEGEN_PATH = REPO_ROOT / "contracts" / "codegen" / "generate.py"
GENERATED_PYTHON_MODELS_PATH = (
    REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py"
)

AUTHORIZATION_SCHEMA_FILES = {
    "IntentAuthorization": "intent-authorization.v1.schema.json",
    "ExecutionAuthorization": "execution-authorization.v1.schema.json",
}

REQUIRED_INTENT_FIELDS = {
    "intentAuthorizationId",
    "taskId",
    "userId",
    "requirementDigest",
    "projectId",
    "repositoryId",
    "repositoryBindingDigest",
    "baselineDigest",
    "targetStage",
    "stageCapabilityMapVersion",
    "allowedCapabilitySetDigest",
    "targetBindingDigest",
    "riskCeiling",
    "estimatedCostAlertDigest",
    "autonomousExecutionBudgetMs",
    "repairLoopLimit",
    "autoReplanLimit",
    "attemptLimit",
    "issuedAt",
    "expiresAt",
    "revokedAt",
    "revokeReason",
}

REQUIRED_EXECUTION_FIELDS = {
    "executionAuthorizationId",
    "intentAuthorizationId",
    "planRevisionId",
    "semanticPlanHash",
    "planRevisionDigest",
    "stageCapabilityMapVersion",
    "stageCapabilityMapDigest",
    "nodeCapabilityMapVersion",
    "nodeCapabilityMapDigest",
    "runId",
    "stepId",
    "attemptId",
    "nodeType",
    "executorId",
    "resourceFingerprint",
    "capabilityScopeDigest",
    "idempotencyKey",
    "inputBindings",
    "actionCapability",
    "actionPolicySnapshotDigest",
    "fencingToken",
    "controlEpoch",
    "acceptedControlCommandSeq",
    "maxUses",
    "consumptionState",
    "issuedAt",
    "expiresAt",
    "revokedAt",
    "revokeReason",
}

INTENT_SNAPSHOT_FIELDS = (
    "requirementDigest",
    "repositoryBindingDigest",
    "baselineDigest",
    "allowedCapabilitySetDigest",
    "targetBindingDigest",
    "estimatedCostAlertDigest",
)

EXECUTION_SNAPSHOT_FIELDS = (
    "semanticPlanHash",
    "planRevisionDigest",
    "stageCapabilityMapDigest",
    "nodeCapabilityMapDigest",
    "resourceFingerprint",
    "capabilityScopeDigest",
    "idempotencyKey",
    "actionPolicySnapshotDigest",
)

# 授权 wire format 的每个快照字段都固定到 registry 中唯一的 schemaId/version。
# artifactId 仅是不可变对象定位符，不能替代 payload 内容摘要，也不进入摘要输入。
SNAPSHOT_SCHEMA_IDS = {
    "IntentAuthorization.requirementDigest": "factory.authorization.intent.requirement.v1",
    "IntentAuthorization.repositoryBindingDigest": "factory.authorization.intent.repository-binding.v1",
    "IntentAuthorization.baselineDigest": "factory.authorization.intent.baseline.v1",
    "IntentAuthorization.allowedCapabilitySetDigest": "factory.authorization.intent.allowed-capability-set.v1",
    "IntentAuthorization.targetBindingDigest": "factory.authorization.intent.target-binding.v1",
    "IntentAuthorization.estimatedCostAlertDigest": "factory.authorization.intent.estimated-cost-alert.v1",
    "ExecutionAuthorization.semanticPlanHash": "factory.authorization.execution.semantic-plan.v1",
    "ExecutionAuthorization.planRevisionDigest": "factory.authorization.execution.plan-revision.v1",
    "ExecutionAuthorization.stageCapabilityMapDigest": "factory.authorization.execution.stage-capability-map.v1",
    "ExecutionAuthorization.nodeCapabilityMapDigest": "factory.authorization.execution.node-capability-map.v1",
    "ExecutionAuthorization.resourceFingerprint": "factory.authorization.execution.resource-fingerprint.v1",
    "ExecutionAuthorization.capabilityScopeDigest": "factory.authorization.execution.capability-scope.v1",
    "ExecutionAuthorization.idempotencyKey": "factory.authorization.execution.idempotency-key.v1",
    "ExecutionAuthorization.actionPolicySnapshotDigest": "factory.authorization.execution.action-policy.v1",
    "ExecutionAuthorization.inputBindings.contentDigest": "factory.authorization.execution.input-content.v1",
}
SNAPSHOT_SCHEMA_VERSION = "1"

FROZEN_POLICY_FIELDS = {
    "resourceFingerprintSchema",
    "idempotencyKeyTemplate",
    "completionFact",
    "authorizationConsumptionPoint",
    "retryClass",
}

RETRY_CLASSES = {
    "bounded-no-external-side-effect",
    "local-fact-before-retry",
    "external-fact-before-retry",
    "one-shot-cas-reconcile-only",
}

AUTHORIZATION_CONSUMPTION_POINTS = {
    "before-capability-dispatch",
    "with-action-started-transaction",
}

# 17 种 node 的五个派发字段是独立审计基线；逐字段比较可直接定位同步漂移，
# 不能只断言值属于一个宽泛合法集合。
EXPECTED_NODE_POLICY_ROWS = (
    ("PLAN", ("repo.read",), (), "read-only", "before-capability-dispatch", "bounded-no-external-side-effect"),
    ("DESIGN_REVIEW", ("repo.read",), (), "read-only", "before-capability-dispatch", "bounded-no-external-side-effect"),
    (
        "BOOTSTRAP_REPOSITORY",
        ("repo.bootstrap", "git.local_commit"),
        (),
        "local-write",
        "with-action-started-transaction",
        "local-fact-before-retry",
    ),
    (
        "IMPLEMENT",
        ("repo.read", "worktree.write", "git.local_commit"),
        (),
        "local-write",
        "with-action-started-transaction",
        "local-fact-before-retry",
    ),
    (
        "VERIFY",
        ("repo.read", "test.exec.isolated"),
        ("network.egress.scoped",),
        "isolated-exec",
        "before-capability-dispatch",
        "bounded-no-external-side-effect",
    ),
    ("CODE_REVIEW", ("repo.read",), (), "read-only", "before-capability-dispatch", "bounded-no-external-side-effect"),
    (
        "ATTEST_REVIEW",
        ("repo.read", "check.publish"),
        (),
        "external-write-limited",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "PUBLISH_PR",
        ("git.push", "pr.create", "pr.update", "forge.observe.scoped"),
        (),
        "external-write",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "MERGE",
        ("forge.observe.scoped", "repo.merge"),
        (),
        "external-write",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "BUILD_ARTIFACT",
        ("build.exec.isolated",),
        ("registry.push", "registry.observe.scoped"),
        "isolated-exec",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "DEPLOY_STAGING",
        ("ssh.exec.scoped", "remote.write.scoped"),
        ("db.backup", "db.migrate", "nginx.switch", "traffic.switch.scoped"),
        "remote-write",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "ACCEPT_STAGING",
        (
            "http.check.scoped",
            "network.egress.scoped",
            "remote.observe.scoped",
            "container.inspect.scoped",
            "log.read.scoped",
        ),
        ("db.read", "db.check", "acceptance.fixture.write", "service.restart.scoped"),
        "remote-observe",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "DEPLOY_PRODUCTION",
        ("ssh.exec.scoped", "remote.write.scoped"),
        ("db.backup", "db.migrate", "nginx.switch", "traffic.switch.scoped"),
        "remote-write",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "ACCEPT_PRODUCTION",
        (
            "http.check.scoped",
            "network.egress.scoped",
            "remote.observe.scoped",
            "container.inspect.scoped",
            "log.read.scoped",
        ),
        ("db.read", "db.check", "acceptance.fixture.write", "service.restart.scoped"),
        "remote-observe",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "ROLLBACK",
        ("rollback", "ssh.exec.scoped", "remote.write.scoped"),
        ("db.restore", "nginx.switch", "traffic.switch.scoped"),
        "remote-write",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "RESTORE_DRILL",
        ("db.restore", "restore.validation.instance", "db.check"),
        (),
        "isolated-restore",
        "with-action-started-transaction",
        "external-fact-before-retry",
    ),
    (
        "RECONCILE_TARGET",
        (),
        (
            "remote.observe.scoped",
            "container.inspect.scoped",
            "log.read.scoped",
            "db.read",
            "db.check",
            "registry.observe.scoped",
            "target.guard.clear",
        ),
        "remote-observe",
        "with-action-started-transaction",
        "one-shot-cas-reconcile-only",
    ),
)
EXPECTED_NODE_POLICY = {
    node_type: {
        "requiredCapabilities": required,
        "optionalCapabilities": optional,
        "sideEffectClass": side_effect,
        "authorizationConsumptionPoint": consumption_point,
        "retryClass": retry_class,
    }
    for (
        node_type,
        required,
        optional,
        side_effect,
        consumption_point,
        retry_class,
    ) in EXPECTED_NODE_POLICY_ROWS
}

# ExecutionAuthorization 与 node map 共用上面的 capability 真源，避免单独维护第二份集合。
EXPECTED_ACTIONS_BY_NODE = {
    node_type: set(expected["requiredCapabilities"]) | set(expected["optionalCapabilities"])
    for node_type, expected in EXPECTED_NODE_POLICY.items()
}

# 有环境语义的 action 必须同时绑定环境与完整资源指纹摘要，防止相同业务输入在
# staging/production 或不同物理目标间得到同一 action identity。
ENVIRONMENT_ACTION_PREFIX = ("environment", "resourceFingerprintDigest")

# 每个 action 的三个安全字段逐项冻结。重复项按语义共享 tuple，但 capability 键仍显式
# 列出，因此失败信息能够精确落到 node/action/field，而不是只报告整文件哈希变化。
EXPECTED_ACTION_POLICY = {
    "PLAN": {
        "repo.read": (
            ("nodeType", "stepId", "requirementDigest", "parentRevisionOrGenesis", "plannerContractDigest"),
            "plan-artifacts-committed-v1",
            (
                "run-spec-schema-valid",
                "plan-revision-schema-valid",
                "dual-hash-recomputed",
                "required-artifacts-committed",
            ),
        )
    },
    "DESIGN_REVIEW": {
        "repo.read": (
            ("repositoryId", "planRevisionDigest", "rubricDigest", "reviewerVersion"),
            "design-review-passed-v1",
            ("review-receipt-binds-plan-revision", "no-blocking-finding", "review-artifact-committed"),
        )
    },
    "BOOTSTRAP_REPOSITORY": {
        capability: (
            ("targetResourceFingerprintDigest", "bootstrapPlanDigest"),
            "bootstrap-commit-matches-v1",
            ("d-volume-empty-no-git-precondition", "repository-identity", "head-and-tree-match", "local-commit-exact"),
        )
        for capability in ("repo.bootstrap", "git.local_commit")
    },
    "IMPLEMENT": {
        capability: (
            ("repoId", "baseSha", "logicalNodeId", "implementationInputDigest"),
            "candidate-commit-matches-v1",
            ("candidate-sha", "candidate-tree", "allowed-paths-only", "artifacts-committed"),
        )
        for capability in ("repo.read", "worktree.write", "git.local_commit")
    },
    "VERIFY": {
        "repo.read": (
            ("repoId", "candidateSha", "testPlanDigest", "commandIdsDigest", "verifierImageDigest"),
            "verification-receipts-pass-v1",
            ("candidate-input-bound", "container-exit-observed", "required-checks-pass"),
        ),
        "test.exec.isolated": (
            ("repoId", "candidateSha", "testPlanDigest", "commandIdsDigest", "verifierImageDigest"),
            "verification-receipts-pass-v1",
            ("candidate-input-bound", "container-exit-observed", "required-checks-pass"),
        ),
        "network.egress.scoped": (
            (
                "repoId",
                "candidateSha",
                "testPlanDigest",
                "commandIdsDigest",
                "verifierImageDigest",
                "egressPolicyDigest",
            ),
            "verification-receipts-pass-v1",
            ("candidate-input-bound", "egress-policy-bound", "container-exit-observed", "required-checks-pass"),
        ),
    },
    "CODE_REVIEW": {
        "repo.read": (
            ("repositoryId", "candidateSha", "rubricDigest", "reviewerVersion"),
            "codex-review-passed-v1",
            (
                "receipt-binds-candidate-sha",
                "receipt-binds-rubric",
                "no-open-blocker-or-high",
                "review-artifact-committed",
            ),
        )
    },
    "ATTEST_REVIEW": {
        capability: (
            (
                "repositoryId",
                "fixedCheckName",
                "expectedAppId",
                "reviewedHeadSha",
                "codexReceiptDigest",
                "rubricDigest",
            ),
            "attestation-check-matches-v1",
            (
                "remote-check-name",
                "source-app-id",
                "reviewed-head-sha",
                "codex-receipt-digest",
                "rubric-digest",
                "check-conclusion",
            ),
        )
        for capability in ("repo.read", "check.publish")
    },
    "PUBLISH_PR": {
        "git.push": (
            ("repositoryId", "remoteRef", "candidateSha"),
            "pushed-ref-matches-candidate-v1",
            ("remote-ref-equals-candidate", "remote-ref-name", "push-receipt-binds-candidate"),
        ),
        "pr.create": (
            ("repositoryId", "headSha", "targetBranch"),
            "created-pr-matches-candidate-v1",
            ("head-base-pr-identity", "created-pr-id", "created-pr-head-equals-candidate"),
        ),
        "pr.update": (
            ("repositoryId", "prId", "expectedHeadSha", "desiredDraftReadyState", "metadataDigest"),
            "updated-pr-state-matches-v1",
            ("pr-id", "expected-head-matches-candidate", "ready-state-no-drift", "metadata-digest-matches"),
        ),
        "forge.observe.scoped": (
            ("repositoryId", "prId", "expectedHeadSha", "evidenceQuerySetDigest"),
            "published-pr-observation-matches-v1",
            (
                "head-base-pr-identity",
                "required-actions-checks-attestation-pass",
                "ready-state-no-drift",
                "observation-query-binds-pr",
            ),
        ),
    },
    "MERGE": {
        capability: (
            ("prId", "expectedReviewedHeadSha", "expectedBaseSha", "mergeMethod"),
            "protected-squash-merge-matches-v1",
            (
                "strict-up-to-date-transaction",
                "unique-parent-equals-base",
                "tree-equals-reviewed-tree",
                "receipt-and-remote-sha-match",
            ),
        )
        for capability in ("forge.observe.scoped", "repo.merge")
    },
    "BUILD_ARTIFACT": {
        "build.exec.isolated": (
            ("repoId", "candidateSha", "buildPlanDigest", "builderImageDigest", "platform"),
            "build-artifact-matches-v1",
            ("oci-digest", "platform-match", "sbom-committed", "provenance-committed"),
        ),
        **{
            capability: (
                ("registryRepository", "imageDigest"),
                "build-artifact-matches-v1",
                (
                    "oci-digest",
                    "platform-match",
                    "sbom-committed",
                    "provenance-committed",
                    "registry-manifest-readable-by-digest",
                ),
            )
            for capability in ("registry.push", "registry.observe.scoped")
        },
    },
    "DEPLOY_STAGING": {
        "ssh.exec.scoped": (
            ENVIRONMENT_ACTION_PREFIX
            + ("serverPhysicalFingerprintDigest", "releaseId", "deployPlanDigest", "stepSeq", "inputHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "direct-target-fact"),
        ),
        "remote.write.scoped": (
            ENVIRONMENT_ACTION_PREFIX
            + ("serverPhysicalFingerprintDigest", "releaseId", "deployPlanDigest", "stepSeq", "inputHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "direct-target-fact"),
        ),
        "db.backup": (
            ENVIRONMENT_ACTION_PREFIX + ("databaseProfileRevision", "releaseId", "backupPlanDigest"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "backup-target-fact"),
        ),
        "db.migrate": (
            ENVIRONMENT_ACTION_PREFIX + ("databaseProfileRevision", "migrationChecksum", "releaseId"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "migration-target-fact"),
        ),
        "nginx.switch": (
            ENVIRONMENT_ACTION_PREFIX + ("serverProfileRevision", "releaseId", "upstreamConfigHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "upstream-target-fact"),
        ),
        "traffic.switch.scoped": (
            ENVIRONMENT_ACTION_PREFIX + ("trafficAdapterFingerprintDigest", "releaseId", "trafficPlanDigest"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "traffic-target-fact"),
        ),
    },
    "ACCEPT_STAGING": {
        **{
            capability: (
                ENVIRONMENT_ACTION_PREFIX + ("releaseId", "acceptancePlanDigest", "checkId"),
                "acceptance-blocking-pass-v1",
                ("target-identity-match", "all-blocking-non-skipped", "evidence-committed", "fixture-cleanup-proof"),
            )
            for capability in (
                "http.check.scoped",
                "network.egress.scoped",
                "remote.observe.scoped",
                "container.inspect.scoped",
                "log.read.scoped",
            )
        },
        **{
            capability: (
                ENVIRONMENT_ACTION_PREFIX
                + ("databaseFingerprintDigest", "releaseId", "acceptancePlanDigest", "checkId"),
                "acceptance-blocking-pass-v1",
                ("target-identity-match", "all-blocking-non-skipped", "database-evidence", "fixture-cleanup-proof"),
            )
            for capability in ("db.read", "db.check")
        },
        "acceptance.fixture.write": (
            ENVIRONMENT_ACTION_PREFIX + ("releaseId", "fixtureDigest"),
            "acceptance-blocking-pass-v1",
            ("target-identity-match", "all-blocking-non-skipped", "fixture-cleanup-proof", "evidence-committed"),
        ),
        "service.restart.scoped": (
            ENVIRONMENT_ACTION_PREFIX + ("releaseId", "restartPlanDigest"),
            "acceptance-blocking-pass-v1",
            ("target-identity-match", "all-blocking-non-skipped", "restart-observed", "evidence-committed"),
        ),
    },
    "DEPLOY_PRODUCTION": {
        "ssh.exec.scoped": (
            ENVIRONMENT_ACTION_PREFIX
            + ("serverPhysicalFingerprintDigest", "releaseId", "deployPlanDigest", "stepSeq", "inputHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "direct-production-target-fact"),
        ),
        "remote.write.scoped": (
            ENVIRONMENT_ACTION_PREFIX
            + ("serverPhysicalFingerprintDigest", "releaseId", "deployPlanDigest", "stepSeq", "inputHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "direct-production-target-fact"),
        ),
        "db.backup": (
            ENVIRONMENT_ACTION_PREFIX + ("databaseProfileRevision", "releaseId", "backupPlanDigest"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "backup-production-target-fact"),
        ),
        "db.migrate": (
            ENVIRONMENT_ACTION_PREFIX + ("databaseProfileRevision", "migrationChecksum", "releaseId"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "migration-production-target-fact"),
        ),
        "nginx.switch": (
            ENVIRONMENT_ACTION_PREFIX + ("serverProfileRevision", "releaseId", "upstreamConfigHash"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "upstream-production-target-fact"),
        ),
        "traffic.switch.scoped": (
            ENVIRONMENT_ACTION_PREFIX + ("trafficAdapterFingerprintDigest", "releaseId", "trafficPlanDigest"),
            "deploy-plan-observed-complete-v1",
            ("planned-step-journal-completed-fsync", "traffic-production-target-fact"),
        ),
    },
    "ACCEPT_PRODUCTION": {
        **{
            capability: (
                ENVIRONMENT_ACTION_PREFIX + ("releaseId", "acceptancePlanDigest", "checkId"),
                "production-acceptance-blocking-pass-v1",
                (
                    "target-identity-match",
                    "active-release-digest-match",
                    "git-sha-and-config-hash-match",
                    "codex-evidence-receipt",
                    "all-blocking-non-skipped",
                    "fixture-cleanup-proof",
                ),
            )
            for capability in (
                "http.check.scoped",
                "network.egress.scoped",
                "remote.observe.scoped",
                "container.inspect.scoped",
                "log.read.scoped",
            )
        },
        **{
            capability: (
                ENVIRONMENT_ACTION_PREFIX
                + ("databaseFingerprintDigest", "releaseId", "acceptancePlanDigest", "checkId"),
                "production-acceptance-blocking-pass-v1",
                (
                    "target-identity-match",
                    "active-release-digest-match",
                    "git-sha-and-config-hash-match",
                    "codex-evidence-receipt",
                    "database-evidence",
                    "fixture-cleanup-proof",
                ),
            )
            for capability in ("db.read", "db.check")
        },
        "acceptance.fixture.write": (
            ENVIRONMENT_ACTION_PREFIX + ("releaseId", "fixtureDigest"),
            "production-acceptance-blocking-pass-v1",
            (
                "target-identity-match",
                "active-release-digest-match",
                "git-sha-and-config-hash-match",
                "codex-evidence-receipt",
                "fixture-cleanup-proof",
            ),
        ),
        "service.restart.scoped": (
            ENVIRONMENT_ACTION_PREFIX + ("releaseId", "restartPlanDigest"),
            "production-acceptance-blocking-pass-v1",
            (
                "target-identity-match",
                "active-release-digest-match",
                "git-sha-and-config-hash-match",
                "codex-evidence-receipt",
                "restart-observed",
            ),
        ),
    },
    "ROLLBACK": {
        **{
            capability: (
                ENVIRONMENT_ACTION_PREFIX + ("failedReleaseId", "goodReleaseDigest", "rollbackPlanHash"),
                "rollback-observed-accepted-v1",
                (
                    "old-release-digest-or-upstream-restored",
                    "database-allowed-version",
                    "blocking-rollback-acceptance-pass",
                    "receipt-match",
                ),
            )
            for capability in ("rollback", "ssh.exec.scoped", "remote.write.scoped")
        },
        "db.restore": (
            ENVIRONMENT_ACTION_PREFIX + ("databaseProfileRevision", "goodReleaseDigest", "rollbackPlanHash"),
            "rollback-observed-accepted-v1",
            (
                "old-release-digest-or-upstream-restored",
                "database-allowed-version",
                "blocking-rollback-acceptance-pass",
                "receipt-match",
            ),
        ),
        "nginx.switch": (
            ENVIRONMENT_ACTION_PREFIX + ("serverProfileRevision", "goodReleaseDigest", "upstreamConfigHash"),
            "rollback-observed-accepted-v1",
            (
                "old-release-digest-or-upstream-restored",
                "database-allowed-version",
                "blocking-rollback-acceptance-pass",
                "receipt-match",
            ),
        ),
        "traffic.switch.scoped": (
            ENVIRONMENT_ACTION_PREFIX + ("trafficAdapterFingerprintDigest", "goodReleaseDigest", "rollbackPlanHash"),
            "rollback-observed-accepted-v1",
            (
                "old-release-digest-or-upstream-restored",
                "database-allowed-version",
                "blocking-rollback-acceptance-pass",
                "receipt-match",
            ),
        ),
    },
    "RESTORE_DRILL": {
        capability: (
            ("databaseProfileRevision", "backupIdOrSampleDigest", "validationInstanceIdentity", "drillPlanDigest"),
            "restore-drill-receipt-and-destroy-v1",
            ("section-16-2-binding", "rto-rpo-evidence", "expiry-evidence", "forced-destroy-receipt"),
        )
        for capability in ("db.restore", "restore.validation.instance", "db.check")
    },
    "RECONCILE_TARGET": {
        **{
            capability: (
                ("resourceFingerprintDigest", "reconciliationPlanDigest", "evidenceQuerySetDigest"),
                "reconciliation-known-state-v1",
                ("stable-resource-fingerprint-match", "target-observation-evidence", "known-state-classification"),
            )
            for capability in (
                "remote.observe.scoped",
                "container.inspect.scoped",
                "log.read.scoped",
                "db.read",
                "db.check",
                "registry.observe.scoped",
            )
        },
        "target.guard.clear": (
            (
                "resourceFingerprintDigest",
                "originalReasonEvidenceDigest",
                "reconciliationPlanDigest",
                "userIdentity",
                "expectedGuardVersion",
            ),
            "reconciliation-known-state-v1",
            (
                "stable-resource-fingerprint-match",
                "intervention-required-to-open-cas",
                "clear-receipt-digest",
                "known-state-classification",
            ),
        ),
    },
}

EXPECTED_CANDIDATE_SHA_NODES = {"VERIFY", "CODE_REVIEW", "PUBLISH_PR", "MERGE"}
EXPECTED_CANDIDATE_AND_CONTENT_NODES = {
    "DEPLOY_STAGING",
    "ACCEPT_STAGING",
    "DEPLOY_PRODUCTION",
    "ACCEPT_PRODUCTION",
}

# 幂等键只能由派发前已知的稳定输入构造；这些运行时可变值会破坏重试/接管收敛。
FORBIDDEN_IDEMPOTENCY_INPUT_FIELDS = {
    "attemptId",
    "fencingToken",
    "controlEpoch",
    "acceptedControlCommandSeq",
    "acceptedControlCommandSequence",
    "issuedAt",
    "expiresAt",
    "ttl",
    "time",
    "timestamp",
}


# 每个 nodeType 都使用可通过本文件本地 $defs 解析的最小资源指纹样本，避免仅凭
# 字符串或注释宣称策略可执行。样本中的可选 overlay 均选择最危险的已启用分支。
FINGERPRINT_SAMPLES = {
    "PLAN": {"repositoryId": "repo-1", "physicalRootIdentity": "volume-d:repo-root"},
    "DESIGN_REVIEW": {"repositoryId": "repo-1", "physicalRootIdentity": "volume-d:repo-root"},
    "BOOTSTRAP_REPOSITORY": {
        "volumeIdentity": "volume-d",
        "canonicalRoot": "D:/factory/new-repository",
        "mode": "new",
    },
    "IMPLEMENT": {"repoId": "repo-1", "worktreePhysicalIdentity": "volume-d:worktree-1"},
    "VERIFY": {
        "repoId": "repo-1",
        "checkoutIdentity": "isolated-checkout-1",
        "verifierImageDigest": "sha256:" + "a" * 64,
        "networkEgressSelected": True,
        "egressPolicyDigest": "sha256:" + "b" * 64,
    },
    "CODE_REVIEW": {"repositoryId": "repo-1", "physicalRootIdentity": "volume-d:repo-root"},
    "ATTEST_REVIEW": {"forge": "github", "repositoryId": "repo-1"},
    "PUBLISH_PR": {"forge": "github", "repositoryId": "repo-1"},
    "MERGE": {"forge": "github", "repositoryId": "repo-1"},
    "BUILD_ARTIFACT": {
        "repoId": "repo-1",
        "builderImageDigest": "sha256:" + "c" * 64,
        "platform": "linux/amd64",
        "registrySelected": True,
        "registry": {"host": "registry.example", "repository": "factory/app"},
    },
    "DEPLOY_STAGING": {
        "environment": "STAGING",
        "serverPhysical": {"hostKey": "staging-host", "remoteRoot": "/srv/factory"},
        "databaseSelected": True,
        "trafficSelected": True,
        "databasePhysical": {"clusterIdentity": "staging-cluster", "databaseIdentity": "factory"},
        "trafficAdapter": {"adapterIdentity": "staging-nginx"},
    },
    "ACCEPT_STAGING": {
        "environment": "STAGING",
        "serverPhysical": {"hostKey": "staging-host", "remoteRoot": "/srv/factory"},
        "releaseIdentity": {"releaseId": "release-1", "containerIdentity": "container-1"},
        "endpointPolicyDigest": "sha256:" + "d" * 64,
        "databaseSelected": True,
        "databasePhysical": {"clusterIdentity": "staging-cluster", "databaseIdentity": "factory"},
    },
    "DEPLOY_PRODUCTION": {
        "environment": "PRODUCTION",
        "serverPhysical": {"hostKey": "production-host", "remoteRoot": "/srv/factory"},
        "databaseSelected": True,
        "trafficSelected": True,
        "databasePhysical": {"clusterIdentity": "production-cluster", "databaseIdentity": "factory"},
        "trafficAdapter": {"adapterIdentity": "production-nginx"},
    },
    "ACCEPT_PRODUCTION": {
        "environment": "PRODUCTION",
        "serverPhysical": {"hostKey": "production-host", "remoteRoot": "/srv/factory"},
        "releaseIdentity": {"releaseId": "release-1", "containerIdentity": "container-1"},
        "endpointPolicyDigest": "sha256:" + "e" * 64,
        "databaseSelected": True,
        "databasePhysical": {"clusterIdentity": "production-cluster", "databaseIdentity": "factory"},
    },
    "ROLLBACK": {
        "environment": "STAGING",
        "serverPhysical": {"hostKey": "staging-host", "remoteRoot": "/srv/factory"},
        "databaseSelected": True,
        "trafficSelected": True,
        "databasePhysical": {"clusterIdentity": "staging-cluster", "databaseIdentity": "factory"},
        "trafficAdapter": {"adapterIdentity": "staging-nginx"},
    },
    "RESTORE_DRILL": {
        "sourceDatabasePhysical": {"clusterIdentity": "prod-cluster", "databaseIdentity": "factory"},
        "validationInstanceIdentity": "restore-drill-1",
        "validationEnvironment": "NON_PRODUCTION",
        "sourceAndValidationDistinct": True,
    },
    "RECONCILE_TARGET": {
        "targetKind": "server",
        "server": {"hostKey": "staging-host", "remoteRoot": "/srv/factory"},
        "guardFingerprint": {"hostKey": "staging-host", "remoteRoot": "/srv/factory"},
    },
}


def _load_json(path: Path) -> dict[str, Any]:
    """以 UTF-8 读取单份合同；测试失败必须定位到真实文件。"""
    with path.open(encoding="utf-8") as source:
        return json.load(source)


_RFC3339_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-]\d{2}:\d{2})$"
)


def _is_rfc3339_date_time(value: object) -> bool:
    """用标准库确定性校验 RFC3339，避免依赖 jsonschema 未锁定的 format extras。"""
    if not isinstance(value, str):
        return True
    if _RFC3339_DATE_TIME.fullmatch(value) is None:
        return False
    normalized = value[:-1] + "+00:00" if value[-1] in {"Z", "z"} else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.utcoffset() is not None


def _authorization_format_checker() -> jsonschema.FormatChecker:
    """为授权合同显式注册 date-time checker；锁定环境未安装可选 RFC3339 包。"""
    checker = jsonschema.FormatChecker()
    checker.checks("date-time")(_is_rfc3339_date_time)
    return checker


def _snapshot_ref(binding_key: str, digest_character: str) -> dict[str, str]:
    """构造闭合快照引用；artifactId 是定位符，摘要域只绑定 schema 与 payload。"""
    return {
        "artifactId": "artifact-" + binding_key.rsplit(".", maxsplit=1)[-1],
        "schemaId": SNAPSHOT_SCHEMA_IDS[binding_key],
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "digest": "sha256:" + digest_character * 64,
    }


def _raw_file_sha256(path: Path) -> str:
    """复用 CompatibilityManifest v1 的文件字节摘要语义，不重造 JSON 私有哈希。"""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_payload_digest(schema_id: str, schema_version: str, payload: object) -> str:
    """按冻结 NFC+RFC8785/JCS 域分离规则重算快照 payload 摘要。"""
    return "sha256:" + hashlib.sha256(canonicalize([schema_id, schema_version, payload])).hexdigest()


def _assert_snapshot_ref_matches_payload(ref: dict[str, str], payload: object) -> None:
    """模拟 Task 4 的纯合同不变量：ref 声明必须等于可重算 payload 摘要。"""
    expected = _snapshot_payload_digest(ref["schemaId"], ref["schemaVersion"], payload)
    assert ref["digest"] == expected, "snapshot ref digest 与 schema/version/payload 重算值不一致"


def _assert_raw_file_digest_matches(actual_digest: str, path: Path) -> None:
    """模拟 Task 4 的纯合同不变量：v1 Manifest 只能接受目标文件的原始字节摘要。"""
    assert actual_digest == _raw_file_sha256(path), "raw-file SHA-256 digest 与冻结文件不一致"


def _load_emit_manifest_module() -> ModuleType:
    """加载既有 emitter，以真实 v1 实现作为 raw-file SHA-256 的唯一算法真源。"""
    spec = importlib.util.spec_from_file_location("phase1_emit_manifest", EMIT_MANIFEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_generated_python_models() -> ModuleType:
    """从生成物原路径加载模块，直接检查 TypedDict 的真实 required/optional 语义。"""
    spec = importlib.util.spec_from_file_location(
        "phase1_generated_contract_models",
        GENERATED_PYTHON_MODELS_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_rejected(schema: dict[str, Any], instance: dict[str, Any]) -> None:
    """用真实 Draft7Validator 断言不可信对象被 schema 拒绝。"""
    errors = list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=_authorization_format_checker(),
        ).iter_errors(instance)
    )
    assert errors, f"应被拒绝的合同对象意外通过: {instance}"


def _assert_accepted(schema: dict[str, Any], instance: dict[str, Any]) -> None:
    """用真实 Draft7Validator 断言完整冻结对象可以通过，不把拒绝测试误写成假绿。"""
    errors = list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=_authorization_format_checker(),
        ).iter_errors(instance)
    )
    assert not errors, f"应被接受的合同对象被拒绝: {errors}"


def _assert_all_object_schemas_fail_closed(schema: dict[str, Any], root_schema: dict[str, Any] | None = None) -> None:
    """递归检查授权 object 都闭合；snapshotRef 的本地 $ref 也必须实际闭合。"""
    root_schema = root_schema or schema
    if schema.get("type") == "object":
        refs = [
            item.get("$ref")
            for item in schema.get("allOf", [])
            if isinstance(item, dict) and isinstance(item.get("$ref"), str)
        ]
        closes_through_snapshot_ref = (
            "#/$defs/snapshotRef" in refs
            and root_schema.get("$defs", {}).get("snapshotRef", {}).get("additionalProperties") is False
        )
        assert schema.get("additionalProperties") is False or closes_through_snapshot_ref, schema
    for value in schema.values():
        if isinstance(value, dict):
            _assert_all_object_schemas_fail_closed(value, root_schema)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_all_object_schemas_fail_closed(item, root_schema)


def _is_known_retry_class(value: object) -> bool:
    """只接受策略 map 冻结的静态重试包络，不能误把 §17 动态错误类当静态策略。"""
    return isinstance(value, str) and value in RETRY_CLASSES


def _valid_intent_authorization() -> dict[str, Any]:
    """构造完整 IntentAuthorization 基准，覆盖 §6.2 的用户授权包络。"""
    return {
        "intentAuthorizationId": "intent-001",
        "taskId": "task-001",
        "userId": "user-001",
        "requirementDigest": _snapshot_ref("IntentAuthorization.requirementDigest", "1"),
        "projectId": "project-001",
        "repositoryId": "repo-001",
        "repositoryBindingDigest": _snapshot_ref("IntentAuthorization.repositoryBindingDigest", "2"),
        "baselineDigest": _snapshot_ref("IntentAuthorization.baselineDigest", "3"),
        "targetStage": "CODEX_APPROVED",
        "stageCapabilityMapVersion": "1",
        "allowedCapabilitySetDigest": _snapshot_ref("IntentAuthorization.allowedCapabilitySetDigest", "4"),
        "targetBindingDigest": _snapshot_ref("IntentAuthorization.targetBindingDigest", "5"),
        "riskCeiling": "medium",
        "estimatedCostAlertDigest": _snapshot_ref("IntentAuthorization.estimatedCostAlertDigest", "6"),
        "autonomousExecutionBudgetMs": 3_600_000,
        "repairLoopLimit": 3,
        "autoReplanLimit": 2,
        "attemptLimit": 4,
        "issuedAt": "2026-08-10T00:00:00Z",
        "expiresAt": "2026-08-11T00:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


def _minimum_input_bindings(node_type: str) -> dict[str, Any]:
    """为每类节点构造最小冻结输入，避免测试用无关字段掩盖授权条件。"""
    if node_type == "IMPLEMENT":
        return {"baseSha": "a" * 40}
    if node_type in EXPECTED_CANDIDATE_SHA_NODES:
        return {"candidateSha": "b" * 40}
    if node_type in EXPECTED_CANDIDATE_AND_CONTENT_NODES:
        return {
            "candidateSha": "b" * 40,
            "contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c"),
        }
    return {"contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c")}


def _valid_execution_authorization(
    *,
    node_type: str = "PUBLISH_PR",
    action_capability: str = "pr.create",
    input_bindings: dict[str, Any] | list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """构造可变节点/action 的基准授权；默认覆盖 PR 副作用的 candidate 绑定。"""
    return {
        "executionAuthorizationId": "execution-001",
        "intentAuthorizationId": "intent-001",
        "planRevisionId": "plan-001",
        "semanticPlanHash": _snapshot_ref("ExecutionAuthorization.semanticPlanHash", "3"),
        "planRevisionDigest": _snapshot_ref("ExecutionAuthorization.planRevisionDigest", "4"),
        "nodeType": node_type,
        "stageCapabilityMapVersion": "1",
        "stageCapabilityMapDigest": _snapshot_ref("ExecutionAuthorization.stageCapabilityMapDigest", "5"),
        "nodeCapabilityMapVersion": "1",
        "nodeCapabilityMapDigest": _snapshot_ref("ExecutionAuthorization.nodeCapabilityMapDigest", "6"),
        "runId": "run-001",
        "stepId": "step-001",
        "attemptId": "attempt-001",
        "executorId": "executor-001",
        "fencingToken": 1,
        "controlEpoch": 1,
        "acceptedControlCommandSeq": 1,
        "resourceFingerprint": _snapshot_ref("ExecutionAuthorization.resourceFingerprint", "7"),
        "capabilityScopeDigest": _snapshot_ref("ExecutionAuthorization.capabilityScopeDigest", "9"),
        "idempotencyKey": _snapshot_ref("ExecutionAuthorization.idempotencyKey", "a"),
        "inputBindings": (input_bindings if input_bindings is not None else _minimum_input_bindings(node_type)),
        "actionCapability": action_capability,
        "actionPolicySnapshotDigest": _snapshot_ref("ExecutionAuthorization.actionPolicySnapshotDigest", "c"),
        "maxUses": 1,
        "consumptionState": "AVAILABLE",
        "issuedAt": "2026-08-10T00:00:00Z",
        "expiresAt": "2026-08-10T01:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


def _registry_payload_validator(registry: dict[str, Any], binding: dict[str, Any]) -> jsonschema.Draft7Validator:
    """在测试中真实执行 registry 的 payload schema，而不是只检查说明字符串。"""
    wrapper = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$defs": registry["$defs"],
        "allOf": [binding["payloadSchema"]],
    }
    return jsonschema.Draft7Validator(wrapper)


def _registry_payload_schema(registry: dict[str, Any], binding: dict[str, Any]) -> dict[str, Any]:
    """解析 registry 内部 payload $ref，供 required/optional 与 validator 来源逐项核验。"""
    ref = binding["payloadSchema"]
    assert set(ref) == {"$ref"}
    assert isinstance(ref["$ref"], str) and ref["$ref"].startswith("#/$defs/")
    return registry["$defs"][ref["$ref"].removeprefix("#/$defs/")]


def _complete_run_spec_for_semantic_snapshot() -> dict[str, Any]:
    """构造完整 RunSpec，确保快照测试直接使用运行时语义投影而非缩小样本。"""
    return {
        "schemaVersion": 1,
        "taskId": "task-001",
        "goal": "完成授权摘要合同闭环",
        "assumptions": ["合同目录可作为唯一协议源"],
        "scope": {
            "include": ["contracts/policies/authorization-snapshot-registry.v1.json"],
            "exclude": ["runtime source"],
        },
        "constraints": ["摘要必须使用 NFC + RFC8785/JCS"],
        "acceptanceCriteria": ["字段漂移必须 fail-closed"],
        "targetStage": "CODEX_APPROVED",
        "repository": {
            "mode": "existing",
            "root": "D:/codex项目/AI-Coding-Factory",
            "baseBranch": "main",
            "baseCommit": "a" * 40,
        },
        "workPlan": {
            "dagVersion": 1,
            "nodes": [
                {
                    "logicalNodeId": "node-plan-001",
                    "businessPhase": "PLANNING",
                    "barrierOrdinal": 0,
                    "nodeType": "PLAN",
                    "required": True,
                    "dependsOn": [],
                    "sideEffectClass": "read-only",
                    "requiredArtifacts": [],
                    "successPredicateId": "plan-created-v1",
                    "timeoutMs": 1_000,
                    "retryPolicyId": "no-retry",
                }
            ],
            "barriers": [
                {
                    "businessPhase": "PLANNING",
                    "barrierOrdinal": 0,
                    "requiredNodeIds": ["node-plan-001"],
                    "settleTimeoutMs": 1_000,
                    "passPredicateId": "planning-complete-v1",
                }
            ],
        },
        "riskProfile": {"level": "low", "reasons": ["仅修改静态合同"]},
        "nodeCapabilityMapVersion": "1",
        "stageCapabilityMapVersion": "1",
        "intentAuthorizationId": "intent-001",
        "semanticPlanHash": "sha256:" + "e" * 64,
    }


def _complete_plan_revision_for_snapshot() -> dict[str, Any]:
    """构造完整 PlanRevision，覆盖 digest material 的所有 schema 字段与谱系。"""
    semantic_plan_hash = plan_hash.semantic_plan_hash(_complete_run_spec_for_semantic_snapshot())
    return {
        "planRevisionId": "plan-001",
        "parentRevisionId": "plan-000",
        "taskId": "task-001",
        "specRevision": 1,
        "intentAuthorizationId": "intent-001",
        "semanticPlanHash": semantic_plan_hash,
        "planRevisionDigest": "sha256:" + "f" * 64,
        "dagVersion": 1,
        "nodeCapabilityMapVersion": "1",
        "stageCapabilityMapVersion": "1",
        "nodes": [
            {
                "logicalNodeId": "node-plan-001",
                "nodeType": "PLAN",
                "businessPhase": "PLANNING",
                "dependencies": [],
                "hasSideEffect": False,
                "requiredArtifacts": [],
                "gate": "plan-created-v1",
            }
        ],
        "barriers": [
            {
                "barrierId": "barrier-plan-001",
                "businessPhase": "PLANNING",
                "nodeIds": ["node-plan-001"],
            }
        ],
        "stageMaps": {"CODEX_APPROVED": ["node-plan-001"]},
        "createdAt": "2026-08-10T00:00:00Z",
    }


def _mutate_schema_valid_value(value: object, schema: dict[str, object]) -> object:
    """在不改变字段类型/枚举约束的前提下构造不同值，验证每个字段均进入摘要。"""
    enum_values = schema.get("enum")
    if isinstance(enum_values, list):
        return next(candidate for candidate in enum_values if candidate != value)
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, str):
        if re.fullmatch(r"sha256:[a-f0-9]{64}", value):
            return "sha256:" + ("0" if value[-1] != "0" else "1") * 64
        if schema.get("format") == "date-time":
            return "2026-08-10T00:00:01Z"
        return value + "-drift"
    if isinstance(value, list):
        assert value, "测试样本必须为每个待变更数组提供至少一个合法元素"
        return [*copy.deepcopy(value), copy.deepcopy(value[-1])]
    if isinstance(value, dict):
        changed = copy.deepcopy(value)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for field, field_schema in properties.items():
                if isinstance(field, str) and isinstance(field_schema, dict) and field in changed:
                    changed[field] = _mutate_schema_valid_value(changed[field], field_schema)
                    return changed
    raise AssertionError(f"无法为 schema-valid 变更构造样本: {value!r}")


def _selected_action_policy_snapshot(
    policy_map: dict[str, Any],
    *,
    node_type: str,
    action_capability: str,
    node_capability_map_digest: str | None = None,
) -> dict[str, Any]:
    """从 node map 的已选 action 投影快照，避免把整组 action 当成可互换的证据。"""
    node_policy = policy_map["nodeTypes"][node_type]
    action_key = node_policy["idempotencyKeyTemplate"]["actions"]["byCapability"][action_capability]
    action_fact = node_policy["completionFact"]["actions"]["byCapability"][action_capability]
    return {
        "nodeCapabilityMapDigest": node_capability_map_digest or _raw_file_sha256(POLICY_PATH),
        "nodeType": node_type,
        "actionCapability": action_capability,
        "idempotencyKeyTemplate": {
            "version": node_policy["idempotencyKeyTemplate"]["version"],
            "hashAlgorithm": node_policy["idempotencyKeyTemplate"]["hashAlgorithm"],
            "jcsInputFields": action_key["jcsInputFields"],
        },
        "completionFact": action_fact,
        "authorizationConsumptionPoint": node_policy["authorizationConsumptionPoint"],
        "retryClass": node_policy["retryClass"],
    }


def _assert_action_snapshot_matches_node_map(payload: dict[str, Any], policy_map: dict[str, Any]) -> None:
    """证明 action 快照同时绑定 map digest、nodeType 与 selected action 的确定性投影。"""
    node_type = payload.get("nodeType")
    action_capability = payload.get("actionCapability")
    assert isinstance(node_type, str) and node_type in policy_map["nodeTypes"], node_type
    actions = policy_map["nodeTypes"][node_type]["idempotencyKeyTemplate"]["actions"]["byCapability"]
    assert isinstance(action_capability, str) and action_capability in actions, action_capability
    expected = _selected_action_policy_snapshot(
        policy_map,
        node_type=node_type,
        action_capability=action_capability,
    )
    assert payload == expected, "action policy snapshot 与冻结 node map 投影不一致"


def _snapshot_payload_samples(policy_map: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """提供每个 registry payload schema 的最小有效样本，确保验证器可实际运行。"""
    semantic_plan_payload = plan_hash.build_semantic_projection(_complete_run_spec_for_semantic_snapshot())
    plan_revision = _complete_plan_revision_for_snapshot()
    plan_revision_payload = {
        field: value for field, value in plan_revision.items() if field not in plan_hash._DIGEST_EXCLUDED_FIELDS
    }
    return {
        "IntentAuthorization.requirementDigest": {
            "requirementText": "为仓库增加可验证的授权合同",
            "normalizationVersion": "nfc-jcs-v1",
        },
        "IntentAuthorization.repositoryBindingDigest": {
            "repositoryId": "repo-001",
            "canonicalRemote": "https://example.invalid/factory.git",
            "physicalRootIdentity": "volume-d:factory",
            "mode": "existing",
        },
        "IntentAuthorization.baselineDigest": {
            "baseBranch": "main",
            "baseSha": "a" * 40,
            "bootstrapState": "EXISTS",
        },
        "IntentAuthorization.allowedCapabilitySetDigest": {
            "targetStage": "CODEX_APPROVED",
            "capabilities": ["repo.read", "worktree.write"],
        },
        "IntentAuthorization.targetBindingDigest": {
            "targetKind": "repository",
            "projectId": "project-001",
        },
        "IntentAuthorization.estimatedCostAlertDigest": {
            "currency": "CNY",
            "alertThreshold": 100,
            "hardStopThreshold": 200,
        },
        "ExecutionAuthorization.semanticPlanHash": semantic_plan_payload,
        "ExecutionAuthorization.planRevisionDigest": plan_revision_payload,
        "ExecutionAuthorization.stageCapabilityMapDigest": {
            "mapVersion": "1",
            "targetStage": "CODEX_APPROVED",
            "capabilities": ["repo.read", "worktree.write"],
        },
        "ExecutionAuthorization.nodeCapabilityMapDigest": {
            "mapVersion": "1",
            "rawFileDigest": _raw_file_sha256(POLICY_PATH),
        },
        "ExecutionAuthorization.resourceFingerprint": {
            "nodeType": "PUBLISH_PR",
            "fingerprint": FINGERPRINT_SAMPLES["PUBLISH_PR"],
        },
        "ExecutionAuthorization.capabilityScopeDigest": {
            "capabilities": ["pr.create"],
            "resourceFingerprintDigest": "sha256:" + "2" * 64,
        },
        "ExecutionAuthorization.idempotencyKey": {
            "nodeType": "PUBLISH_PR",
            "actionCapability": "pr.create",
            "inputBindingsDigest": "sha256:" + "3" * 64,
        },
        "ExecutionAuthorization.actionPolicySnapshotDigest": _selected_action_policy_snapshot(
            policy_map,
            node_type="PUBLISH_PR",
            action_capability="pr.create",
        ),
        "ExecutionAuthorization.inputBindings.contentDigest": {
            "contentKind": "release-material",
            "mediaType": "application/json",
            "byteLength": 128,
        },
    }


def test_authorization_snapshot_registry_is_closed_and_not_in_codegen() -> None:
    """快照 registry 必须是闭合 policy，且不能伪装成新增 codegen schema。"""
    assert SNAPSHOT_REGISTRY_PATH.exists(), f"授权快照 registry 缺失: {SNAPSHOT_REGISTRY_PATH}"
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    assert set(registry) == {
        "$schema",
        "$id",
        "title",
        "description",
        "version",
        "canonicalizer",
        "snapshotRefSchema",
        "artifactResolution",
        "$defs",
        "snapshotBindings",
    }
    assert registry["canonicalizer"] == {
        "unicodeNormalization": "NFC",
        "serialization": "RFC8785/JCS",
        "digestAlgorithm": "sha256",
        "domainSeparatedInput": "[schemaId,schemaVersion,payload]",
        "digestExpression": "sha256(JCS/NFC([schemaId,schemaVersion,payload]))",
    }
    assert registry["artifactResolution"] == {
        "requiredState": "COMMITTED",
        "immutable": True,
        "mustResolveArtifactId": True,
        "mustMatch": [
            "schemaId",
            "schemaVersion",
            "validatorSource",
            "recomputedDigest",
        ],
        "rejectOnMismatch": True,
    }

    ref_schema = registry["snapshotRefSchema"]
    jsonschema.Draft7Validator.check_schema(ref_schema)
    _assert_accepted(
        ref_schema,
        _snapshot_ref("IntentAuthorization.requirementDigest", "1"),
    )
    invalid_ref = _snapshot_ref("IntentAuthorization.requirementDigest", "1")
    invalid_ref["unapproved"] = "blocked"
    _assert_rejected(ref_schema, invalid_ref)

    assert set(registry["snapshotBindings"]) == set(SNAPSHOT_SCHEMA_IDS)
    for binding_key, schema_id in SNAPSHOT_SCHEMA_IDS.items():
        binding = registry["snapshotBindings"][binding_key]
        assert set(binding) == {
            "schemaId",
            "schemaVersion",
            "validator",
            "payloadSchema",
            "payloadKeys",
        }, binding_key
        assert binding["schemaId"] == schema_id
        assert binding["schemaVersion"] == SNAPSHOT_SCHEMA_VERSION
        assert set(binding["validator"]) >= {"kind", "source"}
        assert set(binding["payloadKeys"]) == {"required", "optional"}

    catalog = _load_json(CATALOG_PATH)
    assert all(
        entry["schemaPath"] != "contracts/policies/authorization-snapshot-registry.v1.json"
        for entry in catalog["schemas"]
    )


def test_snapshot_registry_payload_validators_are_executable_and_fail_closed() -> None:
    """registry 的每个 payload schema 都须实际执行，并拒绝未知字段。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    samples = _snapshot_payload_samples(_load_json(POLICY_PATH))

    for binding_key, sample in samples.items():
        binding = registry["snapshotBindings"][binding_key]
        validator = _registry_payload_validator(registry, binding)
        assert not list(validator.iter_errors(sample)), binding_key

        unknown = copy.deepcopy(sample)
        unknown["unapproved"] = "blocked"
        assert list(validator.iter_errors(unknown)), binding_key

        payload_schema = _registry_payload_schema(registry, binding)
        assert set(binding["payloadKeys"]["required"]) == set(payload_schema.get("required", [])), binding_key
        assert set(binding["payloadKeys"]["optional"]) == (
            set(payload_schema.get("properties", {})) - set(payload_schema.get("required", []))
        ), binding_key

        validator_source = binding["validator"]["source"]
        validator_kind = binding["validator"]["kind"]
        if validator_kind == "json-schema":
            assert validator_source == ("authorization-snapshot-registry.v1.json" + binding["payloadSchema"]["$ref"]), (
                binding_key
            )
        else:
            source_path = validator_source.split("#", maxsplit=1)[0]
            assert (REPO_ROOT / source_path).exists(), binding_key

    # 资源指纹的内层不是占位 dict；它必须使用 node map 中按 nodeType 选择的真实 validator。
    resource_payload = samples["ExecutionAuthorization.resourceFingerprint"]
    policy_map = _load_json(POLICY_PATH)
    resource_schema = policy_map["nodeTypes"][resource_payload["nodeType"]]["resourceFingerprintSchema"]
    assert not _validate_fingerprint_schema_with_local_defs(
        policy_map,
        resource_schema,
        resource_payload["fingerprint"],
    )
    resource_extra = copy.deepcopy(resource_payload["fingerprint"])
    resource_extra["unapprovedOverlay"] = "blocked"
    assert _validate_fingerprint_schema_with_local_defs(policy_map, resource_schema, resource_extra)


def test_semantic_plan_snapshot_matches_runtime_projection_and_observes_each_field() -> None:
    """真实 RunSpec 投影必须完整入 registry，逐字段删改都不能复用旧摘要。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    binding_key = "ExecutionAuthorization.semanticPlanHash"
    binding = registry["snapshotBindings"][binding_key]
    payload_schema = _registry_payload_schema(registry, binding)
    run_spec_schema = _load_json(RUN_SPEC_SCHEMA_PATH)
    run_spec = _complete_run_spec_for_semantic_snapshot()
    _assert_accepted(run_spec_schema, run_spec)

    # 不手抄字段表：直接以运行时 build_semantic_projection 的实际输出作为集合真源。
    projection = plan_hash.build_semantic_projection(run_spec)
    assert payload_schema["type"] == "object"
    assert payload_schema["additionalProperties"] is False
    assert set(payload_schema["properties"]) == set(projection)
    assert set(payload_schema["required"]) == set(projection)
    assert set(binding["payloadKeys"]["required"]) == set(projection)
    assert binding["payloadKeys"]["optional"] == []

    # 叶子字段复用 RunSpec schema；两个嵌套对象也只能包含运行时投影明确纳入的键。
    for field, value in projection.items():
        source_schema = run_spec_schema["properties"][field]
        registered_schema = payload_schema["properties"][field]
        if field not in {"repository", "workPlan"}:
            assert registered_schema == source_schema, field
            continue
        assert registered_schema["type"] == "object", field
        assert registered_schema["additionalProperties"] is False, field
        assert set(registered_schema["properties"]) == set(value), field
        assert set(registered_schema["required"]) == set(value), field
        for nested_field in value:
            assert registered_schema["properties"][nested_field] == source_schema["properties"][nested_field]

    validator = _registry_payload_validator(registry, binding)
    assert not list(validator.iter_errors(projection)), "真实 semantic projection 不得被 registry 拒绝"
    ref = _snapshot_ref(binding_key, "semantic")
    ref["digest"] = _snapshot_payload_digest(binding["schemaId"], binding["schemaVersion"], projection)
    _assert_snapshot_ref_matches_payload(ref, projection)
    baseline_hash = plan_hash.semantic_plan_hash(run_spec)

    for field in sorted(projection):
        missing = copy.deepcopy(projection)
        missing.pop(field)
        assert list(validator.iter_errors(missing)), f"缺失语义字段 {field} 不得通过"

        changed_run_spec = copy.deepcopy(run_spec)
        changed_run_spec[field] = _mutate_schema_valid_value(
            changed_run_spec[field], run_spec_schema["properties"][field]
        )
        _assert_accepted(run_spec_schema, changed_run_spec)
        changed_projection = plan_hash.build_semantic_projection(changed_run_spec)
        assert not list(validator.iter_errors(changed_projection)), f"变更后的 {field} 仍应是合法 projection"
        assert plan_hash.semantic_plan_hash(changed_run_spec) != baseline_hash, field
        with pytest.raises(AssertionError, match="重算值不一致"):
            _assert_snapshot_ref_matches_payload(ref, changed_projection)

    nested_extra = copy.deepcopy(projection)
    nested_extra["repository"]["unapproved"] = "blocked"
    assert list(validator.iter_errors(nested_extra))


def test_plan_revision_snapshot_is_complete_digest_material_and_observes_each_field() -> None:
    """PlanRevision 快照只排除自身摘要/签名，其他 schema 字段均必须进入 digest material。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    binding_key = "ExecutionAuthorization.planRevisionDigest"
    binding = registry["snapshotBindings"][binding_key]
    payload_schema = _registry_payload_schema(registry, binding)
    plan_revision_schema = _load_json(PLAN_REVISION_SCHEMA_PATH)
    revision = _complete_plan_revision_for_snapshot()
    _assert_accepted(plan_revision_schema, revision)

    # 排除集直接来自运行时摘要实现；PlanRevision schema 是 payload 字段和类型的唯一合同来源。
    excluded_fields = set(plan_hash._DIGEST_EXCLUDED_FIELDS)
    assert excluded_fields == {"planRevisionDigest", "signature"}
    expected_properties = set(plan_revision_schema["properties"]) - excluded_fields
    expected_required = set(plan_revision_schema["required"]) - excluded_fields
    expected_optional = expected_properties - expected_required
    assert payload_schema["type"] == "object"
    assert payload_schema["additionalProperties"] is False
    assert set(payload_schema["properties"]) == expected_properties
    assert set(payload_schema["required"]) == expected_required
    assert set(binding["payloadKeys"]["required"]) == expected_required
    assert set(binding["payloadKeys"]["optional"]) == expected_optional
    for field in expected_properties:
        assert payload_schema["properties"][field] == plan_revision_schema["properties"][field], field

    material = {field: value for field, value in revision.items() if field not in excluded_fields}
    assert set(material) == expected_properties
    validator = _registry_payload_validator(registry, binding)
    assert not list(validator.iter_errors(material)), "完整 PlanRevision digest material 不得被 registry 拒绝"
    ref = _snapshot_ref(binding_key, "revision")
    ref["digest"] = _snapshot_payload_digest(binding["schemaId"], binding["schemaVersion"], material)
    _assert_snapshot_ref_matches_payload(ref, material)
    baseline_digest = plan_hash.plan_revision_digest(revision)

    for field in sorted(material):
        missing = copy.deepcopy(material)
        missing.pop(field)
        if field in expected_required:
            assert list(validator.iter_errors(missing)), f"缺失 PlanRevision 必填字段 {field} 不得通过"
        else:
            # parentRevisionId 在权威 PlanRevision schema 中仍是可选字段，registry 不得擅自收紧。
            assert not list(validator.iter_errors(missing)), f"可选 PlanRevision 字段 {field} 不得被擅自要求"

        changed_revision = copy.deepcopy(revision)
        changed_revision[field] = _mutate_schema_valid_value(
            changed_revision[field], plan_revision_schema["properties"][field]
        )
        _assert_accepted(plan_revision_schema, changed_revision)
        changed_material = {
            name: value for name, value in changed_revision.items() if name not in excluded_fields
        }
        assert not list(validator.iter_errors(changed_material)), f"变更后的 {field} 仍应是合法 digest material"
        assert plan_hash.plan_revision_digest(changed_revision) != baseline_digest, field
        with pytest.raises(AssertionError, match="重算值不一致"):
            _assert_snapshot_ref_matches_payload(ref, changed_material)

    for excluded_field in sorted(excluded_fields):
        extra = copy.deepcopy(material)
        extra[excluded_field] = revision.get(excluded_field, "signature-001")
        assert list(validator.iter_errors(extra)), f"registry 不得把 {excluded_field} 重新纳入 material"

    signed_revision = {**revision, "signature": "signature-001"}
    rewritten_digest = {**revision, "planRevisionDigest": "sha256:" + "0" * 64}
    assert plan_hash.plan_revision_digest(signed_revision) == baseline_digest
    assert plan_hash.plan_revision_digest(rewritten_digest) == baseline_digest


def test_authorization_schemas_use_fixed_closed_snapshot_refs() -> None:
    """两份授权 schema 的 digest-backed 字段必须改为固定 schemaId/version 的闭合引用。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    intent_schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["IntentAuthorization"])
    execution_schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])

    fields_by_schema = {
        "IntentAuthorization": (intent_schema, INTENT_SNAPSHOT_FIELDS),
        "ExecutionAuthorization": (execution_schema, EXECUTION_SNAPSHOT_FIELDS),
    }
    for authorization_name, (schema, fields) in fields_by_schema.items():
        for field in fields:
            binding_key = f"{authorization_name}.{field}"
            prop = schema["properties"][field]
            assert prop["type"] == "object", binding_key
            assert prop["xSnapshotRegistryEntry"] == binding_key
            assert registry["snapshotBindings"][binding_key]["schemaId"] == SNAPSHOT_SCHEMA_IDS[binding_key]

    content_binding_key = "ExecutionAuthorization.inputBindings.contentDigest"
    content_prop = execution_schema["properties"]["inputBindings"]["properties"]["contentDigest"]
    assert content_prop["type"] == "object"
    assert content_prop["xSnapshotRegistryEntry"] == content_binding_key

    valid_intent = _valid_intent_authorization()
    _assert_accepted(intent_schema, valid_intent)
    # artifactId 是地址，不参与 JCS/NFC payload 摘要；切换地址但保留 digest 在 wire schema 中合法。
    alternate_artifact = copy.deepcopy(valid_intent)
    alternate_artifact["requirementDigest"]["artifactId"] = "artifact-replicated-requirement"
    _assert_accepted(intent_schema, alternate_artifact)

    for binding_key in SNAPSHOT_SCHEMA_IDS:
        if binding_key.startswith("IntentAuthorization."):
            schema = intent_schema
            instance = copy.deepcopy(valid_intent)
            field_path = binding_key.split(".", maxsplit=1)[1]
        elif binding_key == content_binding_key:
            schema = execution_schema
            instance = _valid_execution_authorization(
                node_type="DEPLOY_STAGING",
                action_capability="ssh.exec.scoped",
            )
        else:
            schema = execution_schema
            instance = _valid_execution_authorization()
            field_path = binding_key.split(".", maxsplit=1)[1]

        wrong_schema = copy.deepcopy(instance)
        if binding_key == content_binding_key:
            wrong_ref = wrong_schema["inputBindings"]["contentDigest"]
        else:
            wrong_ref = wrong_schema[field_path]
        wrong_ref["schemaId"] = "factory.authorization.unapproved.v1"
        _assert_rejected(schema, wrong_schema)

        wrong_version = copy.deepcopy(instance)
        if binding_key == content_binding_key:
            wrong_ref = wrong_version["inputBindings"]["contentDigest"]
        else:
            wrong_ref = wrong_version[field_path]
        wrong_ref["schemaVersion"] = "2"
        _assert_rejected(schema, wrong_version)

        extra_ref = copy.deepcopy(instance)
        if binding_key == content_binding_key:
            wrong_ref = extra_ref["inputBindings"]["contentDigest"]
        else:
            wrong_ref = extra_ref[field_path]
        wrong_ref["unapproved"] = "blocked"
        _assert_rejected(schema, extra_ref)


def test_snapshot_digest_domain_separation_detects_content_and_schema_drift() -> None:
    """artifactId 不改变 payload digest；内容或 schemaId/version 改变后复算值必须不同。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    binding_key = "IntentAuthorization.requirementDigest"
    binding = registry["snapshotBindings"][binding_key]
    payload = _snapshot_payload_samples(_load_json(POLICY_PATH))[binding_key]
    ref = _snapshot_ref(binding_key, "0")
    ref["digest"] = _snapshot_payload_digest(binding["schemaId"], binding["schemaVersion"], payload)
    _assert_snapshot_ref_matches_payload(ref, payload)

    alternate_artifact = copy.deepcopy(ref)
    alternate_artifact["artifactId"] = "artifact-replicated-requirement"
    assert alternate_artifact["digest"] == ref["digest"]

    changed_payload = copy.deepcopy(payload)
    changed_payload["requirementText"] = "同一需求但内容已经漂移"
    with pytest.raises(AssertionError, match="重算值不一致"):
        _assert_snapshot_ref_matches_payload(ref, changed_payload)

    changed_schema_id = "factory.authorization.intent.requirement.v2"
    wrong_schema_ref = copy.deepcopy(ref)
    wrong_schema_ref["schemaId"] = changed_schema_id
    with pytest.raises(AssertionError, match="重算值不一致"):
        _assert_snapshot_ref_matches_payload(wrong_schema_ref, payload)

    wrong_version_ref = copy.deepcopy(ref)
    wrong_version_ref["schemaVersion"] = "2"
    with pytest.raises(AssertionError, match="重算值不一致"):
        _assert_snapshot_ref_matches_payload(wrong_version_ref, payload)


def test_action_snapshot_binds_raw_node_map_digest_node_and_selected_action() -> None:
    """action snapshot 必须是单个 selected action 的投影，不能脱离 node map/node/action 复用。"""
    registry = _load_json(SNAPSHOT_REGISTRY_PATH)
    binding = registry["snapshotBindings"]["ExecutionAuthorization.actionPolicySnapshotDigest"]
    assert binding["validator"] == {
        "kind": "deterministic-action-projection",
        "source": "contracts/policies/node-capability-map.v1.json",
        "nodeTypeFrom": "ExecutionAuthorization.nodeType",
        "actionCapabilityFrom": "ExecutionAuthorization.actionCapability",
        "projectionFields": [
            "idempotencyKeyTemplate.actions.byCapability[{actionCapability}]",
            "completionFact.actions.byCapability[{actionCapability}]",
            "authorizationConsumptionPoint",
            "retryClass",
        ],
    }
    policy_map = _load_json(POLICY_PATH)
    payload = _selected_action_policy_snapshot(
        policy_map,
        node_type="PUBLISH_PR",
        action_capability="pr.create",
    )
    _assert_action_snapshot_matches_node_map(payload, policy_map)

    wrong_map_digest = copy.deepcopy(payload)
    wrong_map_digest["nodeCapabilityMapDigest"] = "sha256:" + "0" * 64
    with pytest.raises(AssertionError):
        _assert_action_snapshot_matches_node_map(wrong_map_digest, policy_map)

    wrong_node = copy.deepcopy(payload)
    wrong_node["nodeType"] = "CODE_REVIEW"
    with pytest.raises(AssertionError):
        _assert_action_snapshot_matches_node_map(wrong_node, policy_map)

    wrong_action = copy.deepcopy(payload)
    wrong_action["actionCapability"] = "git.push"
    with pytest.raises(AssertionError):
        _assert_action_snapshot_matches_node_map(wrong_action, policy_map)

    drifted_map = copy.deepcopy(policy_map)
    drifted_map["nodeTypes"]["PUBLISH_PR"]["completionFact"]["actions"]["byCapability"]["pr.create"]["predicateId"] = (
        "drifted-pr-create-fact-v1"
    )
    with pytest.raises(AssertionError):
        _assert_action_snapshot_matches_node_map(payload, drifted_map)


def test_compatibility_manifest_v1_uses_emitters_raw_file_node_map_digest() -> None:
    """CompatibilityManifest v1 的 node map 摘要唯一真源是 emitter 的 raw-file SHA-256。"""
    manifest_schema = _load_json(MANIFEST_SCHEMA_PATH)
    assert "nodeCapabilityMapDigest" in manifest_schema["required"]
    emitter = _load_emit_manifest_module()
    expected = _raw_file_sha256(POLICY_PATH)
    assert expected == EXPECTED_NODE_POLICY_RAW_FILE_SHA256
    assert emitter._sha256_file(POLICY_PATH) == expected
    _assert_raw_file_digest_matches(expected, POLICY_PATH)

    raw = POLICY_PATH.read_bytes()
    format_only = b"\n" + raw
    assert json.loads(format_only.decode("utf-8")) == _load_json(POLICY_PATH)
    assert "sha256:" + hashlib.sha256(format_only).hexdigest() != expected

    semantic_drift = copy.deepcopy(_load_json(POLICY_PATH))
    semantic_drift["nodeTypes"]["PLAN"]["retryClass"] = "local-fact-before-retry"
    semantic_bytes = json.dumps(semantic_drift, ensure_ascii=False, indent=2).encode("utf-8")
    assert "sha256:" + hashlib.sha256(semantic_bytes).hexdigest() != expected

    wrong_digest = "sha256:" + "f" * 64
    with pytest.raises(AssertionError, match="冻结文件不一致"):
        _assert_raw_file_digest_matches(wrong_digest, POLICY_PATH)


def test_authorization_schema_files_are_registered_for_codegen() -> None:
    """两份授权 schema 必须存在且只由 catalog 驱动三语言生成。"""
    catalog = _load_json(CATALOG_PATH)
    entries = {entry["name"]: entry for entry in catalog["schemas"]}

    for name, filename in AUTHORIZATION_SCHEMA_FILES.items():
        schema_path = SCHEMAS_DIR / filename
        assert schema_path.exists(), f"{name} schema 缺失: {schema_path}"
        entry = entries.get(name)
        assert entry is not None, f"catalog 未注册 {name}"
        assert entry["schemaPath"] == f"contracts/schemas/{filename}"
        assert entry["codegen"] is True
        assert entry["category"] == "generated"


def test_generated_python_typed_dict_keys_exactly_match_schema_required_fields() -> None:
    """所有 codegen schema 的 Python TypedDict 都只把 schema required 字段标为 Required。"""
    catalog = _load_json(CATALOG_PATH)
    models = _load_generated_python_models()

    for entry in catalog["schemas"]:
        if entry.get("codegen") is not True:
            continue
        schema = _load_json(REPO_ROOT / entry["schemaPath"])
        definition_key = entry.get("definitionKey")
        if definition_key:
            definitions = schema.get("definitions", schema.get("$defs", {}))
            target = definitions[definition_key]
        else:
            target = schema
        expected_required = frozenset(target.get("required", []))
        expected_optional = frozenset(target.get("properties", {})) - expected_required
        typed_dict = getattr(models, entry["name"])
        assert typed_dict.__required_keys__ == expected_required, entry["name"]
        assert typed_dict.__optional_keys__ == expected_optional, entry["name"]


def test_authorization_schemas_freeze_complete_required_fields_and_fail_closed() -> None:
    """授权合同必须覆盖 §6.2/§11 字段并禁止额外输入。"""
    intent_schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["IntentAuthorization"])
    execution_schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])

    assert intent_schema["type"] == "object"
    assert intent_schema["additionalProperties"] is False
    assert REQUIRED_INTENT_FIELDS <= set(intent_schema["required"])
    _assert_all_object_schemas_fail_closed(intent_schema)
    assert execution_schema["type"] == "object"
    assert execution_schema["additionalProperties"] is False
    assert REQUIRED_EXECUTION_FIELDS <= set(execution_schema["required"])
    _assert_all_object_schemas_fail_closed(execution_schema)


def test_intent_authorization_schema_rejects_missing_unknown_and_i_json_overflow() -> None:
    """IntentAuthorization 缺字段、未知 stage、额外字段和不安全整数必须被拒绝。"""
    schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["IntentAuthorization"])
    valid = _valid_intent_authorization()
    assert not list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=_authorization_format_checker(),
        ).iter_errors(valid)
    )

    # 每一个 required 字段缺失都必须 fail-closed，不能只覆盖一个示例字段。
    for field in sorted(REQUIRED_INTENT_FIELDS):
        missing_field = copy.deepcopy(valid)
        missing_field.pop(field)
        _assert_rejected(schema, missing_field)

    # 每个快照引用都拒绝大写十六进制和不完整长度，确保内容寻址不会静默归一化。
    for field in INTENT_SNAPSHOT_FIELDS:
        uppercase_digest = copy.deepcopy(valid)
        uppercase_digest[field]["digest"] = "sha256:" + "A" * 64
        _assert_rejected(schema, uppercase_digest)

        short_digest = copy.deepcopy(valid)
        short_digest[field]["digest"] = "sha256:" + "a" * 63
        _assert_rejected(schema, short_digest)

    unknown_stage = copy.deepcopy(valid)
    unknown_stage["targetStage"] = "SUPERUSER_APPROVED"
    _assert_rejected(schema, unknown_stage)

    unknown_risk = copy.deepcopy(valid)
    unknown_risk["riskCeiling"] = "unbounded"
    _assert_rejected(schema, unknown_risk)

    plan_revision_leak = copy.deepcopy(valid)
    plan_revision_leak["planRevisionId"] = "plan-001"
    _assert_rejected(schema, plan_revision_leak)

    double_hash_leak = copy.deepcopy(valid)
    double_hash_leak["semanticPlanHash"] = "sha256:" + "f" * 64
    _assert_rejected(schema, double_hash_leak)

    i_json_overflow = copy.deepcopy(valid)
    i_json_overflow["autonomousExecutionBudgetMs"] = 9_007_199_254_740_992
    _assert_rejected(schema, i_json_overflow)


def test_authorization_schemas_freeze_revocation_pair_and_timestamp_format() -> None:
    """撤销时间/原因只能成对出现，并拒绝不符合 RFC3339 的授权时间。"""
    cases = (
        (
            _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["IntentAuthorization"]),
            _valid_intent_authorization,
        ),
        (
            _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"]),
            _valid_execution_authorization,
        ),
    )
    for schema, factory in cases:
        unrevoked = factory()
        _assert_accepted(schema, unrevoked)

        revoked = copy.deepcopy(unrevoked)
        revoked["revokedAt"] = "2026-08-10T00:30:00Z"
        revoked["revokeReason"] = "用户取消"
        _assert_accepted(schema, revoked)

        reason_without_time = copy.deepcopy(unrevoked)
        reason_without_time["revokeReason"] = "无撤销时间"
        _assert_rejected(schema, reason_without_time)

        time_without_reason = copy.deepcopy(unrevoked)
        time_without_reason["revokedAt"] = "2026-08-10T00:30:00Z"
        _assert_rejected(schema, time_without_reason)

        illegal_issued_at = copy.deepcopy(unrevoked)
        illegal_issued_at["issuedAt"] = "2026-02-30T25:61:00Z"
        _assert_rejected(schema, illegal_issued_at)

        illegal_expires_at = copy.deepcopy(unrevoked)
        illegal_expires_at["expiresAt"] = "not-a-rfc3339-time"
        _assert_rejected(schema, illegal_expires_at)


def test_execution_authorization_schema_rejects_wrong_binding_unknown_enum_and_extra_field() -> None:
    """副作用授权必须绑定 candidate，且未知状态、额外键和缺 fencing 均 fail-closed。"""
    schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])
    valid = _valid_execution_authorization()
    assert not list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=_authorization_format_checker(),
        ).iter_errors(valid)
    )

    # 派生授权的全部 required 字段缺失均应被拒绝，包含 lease、消费与撤销投影。
    for field in sorted(REQUIRED_EXECUTION_FIELDS):
        missing_field = copy.deepcopy(valid)
        missing_field.pop(field)
        _assert_rejected(schema, missing_field)

    for field in EXECUTION_SNAPSHOT_FIELDS:
        uppercase_digest = copy.deepcopy(valid)
        uppercase_digest[field]["digest"] = "sha256:" + "B" * 64
        _assert_rejected(schema, uppercase_digest)

        short_digest = copy.deepcopy(valid)
        short_digest[field]["digest"] = "sha256:" + "b" * 63
        _assert_rejected(schema, short_digest)

    wrong_side_effect_binding = copy.deepcopy(valid)
    wrong_side_effect_binding["inputBindings"]["candidateSha"] = "not-a-commit-sha"
    _assert_rejected(schema, wrong_side_effect_binding)

    # contentDigest 仅对部署/验收等 content-bound 节点存在；使用该合法最小对象检查其 ref 格式。
    content_bound = _valid_execution_authorization(
        node_type="DEPLOY_STAGING",
        action_capability="ssh.exec.scoped",
    )
    uppercase_content_digest = copy.deepcopy(content_bound)
    uppercase_content_digest["inputBindings"]["contentDigest"]["digest"] = "sha256:" + "C" * 64
    _assert_rejected(schema, uppercase_content_digest)

    short_content_digest = copy.deepcopy(content_bound)
    short_content_digest["inputBindings"]["contentDigest"]["digest"] = "sha256:" + "c" * 63
    _assert_rejected(schema, short_content_digest)

    unknown_consumption_state = copy.deepcopy(valid)
    unknown_consumption_state["consumptionState"] = "SUCCEEDED_WITH_UNKNOWN_STATE"
    _assert_rejected(schema, unknown_consumption_state)

    for obsolete_state in ("REVOKED", "EXPIRED"):
        obsolete_consumption_state = copy.deepcopy(valid)
        obsolete_consumption_state["consumptionState"] = obsolete_state
        _assert_rejected(schema, obsolete_consumption_state)

    available_without_uses = copy.deepcopy(valid)
    available_without_uses["maxUses"] = 0
    _assert_rejected(schema, available_without_uses)

    consumed_with_remaining_uses = copy.deepcopy(valid)
    consumed_with_remaining_uses["consumptionState"] = "CONSUMED"
    consumed_with_remaining_uses["maxUses"] = 1
    _assert_rejected(schema, consumed_with_remaining_uses)

    old_stage_digest_spelling = copy.deepcopy(valid)
    old_stage_digest_spelling["stageCapeabilityMapDigest"] = "sha256:" + "e" * 64
    _assert_rejected(schema, old_stage_digest_spelling)

    unknown_node_type = copy.deepcopy(valid)
    unknown_node_type["nodeType"] = "UNFROZEN_NODE"
    _assert_rejected(schema, unknown_node_type)

    unknown_action = copy.deepcopy(valid)
    unknown_action["actionCapability"] = "repo.destroy"
    _assert_rejected(schema, unknown_action)

    invalid_resource_fingerprint_digest = copy.deepcopy(valid)
    invalid_resource_fingerprint_digest["resourceFingerprint"]["digest"] = "sha256:" + "D" * 64
    _assert_rejected(schema, invalid_resource_fingerprint_digest)

    resource_fingerprint_extra = copy.deepcopy(valid)
    resource_fingerprint_extra["resourceFingerprint"]["unapprovedOverlay"] = "blocked"
    _assert_rejected(schema, resource_fingerprint_extra)

    input_binding_extra = copy.deepcopy(valid)
    input_binding_extra["inputBindings"]["unapprovedOverlay"] = "blocked"
    _assert_rejected(schema, input_binding_extra)

    missing_fencing = copy.deepcopy(valid)
    missing_fencing.pop("fencingToken")
    _assert_rejected(schema, missing_fencing)


def test_execution_authorization_node_actions_and_minimum_input_bindings_are_exact() -> None:
    """nodeType/action 与最低 SHA/digest 输入必须和冻结策略逐项一致，不能用全局 action 误放行。"""
    schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])
    policy_node_types = _load_json(POLICY_PATH)["nodeTypes"]
    expected_actions = set().union(*EXPECTED_ACTIONS_BY_NODE.values())

    assert set(schema["properties"]["nodeType"]["enum"]) == set(EXPECTED_ACTIONS_BY_NODE)
    assert set(schema["properties"]["actionCapability"]["enum"]) == expected_actions
    assert set(policy_node_types) == set(EXPECTED_ACTIONS_BY_NODE)

    for node_type, expected_for_node in EXPECTED_ACTIONS_BY_NODE.items():
        policy_actions = set(policy_node_types[node_type]["requiredCapabilities"]) | set(
            policy_node_types[node_type]["optionalCapabilities"]
        )
        assert policy_actions == expected_for_node, node_type
        for action_capability in sorted(expected_for_node):
            _assert_accepted(
                schema,
                _valid_execution_authorization(
                    node_type=node_type,
                    action_capability=action_capability,
                ),
            )

        unapproved_action = next(iter(sorted(expected_actions - expected_for_node)))
        _assert_rejected(
            schema,
            _valid_execution_authorization(
                node_type=node_type,
                action_capability=unapproved_action,
            ),
        )


def test_publish_pr_completion_facts_remain_action_local() -> None:
    """PUBLISH_PR 的 push/create/update/observe 不能共用泛化完成事实，避免错误 receipt 互相满足。"""
    actions = _load_json(POLICY_PATH)["nodeTypes"]["PUBLISH_PR"]["completionFact"]["actions"]["byCapability"]
    expected_predicates = {
        "git.push": "pushed-ref-matches-candidate-v1",
        "pr.create": "created-pr-matches-candidate-v1",
        "pr.update": "updated-pr-state-matches-v1",
        "forge.observe.scoped": "published-pr-observation-matches-v1",
    }
    assert set(actions) == set(expected_predicates)
    assert {action["predicateId"] for action in actions.values()} == set(expected_predicates.values())
    assert "remote-ref-equals-candidate" in actions["git.push"]["requiredEvidence"]
    assert "head-base-pr-identity" in actions["pr.create"]["requiredEvidence"]
    assert "ready-state-no-drift" in actions["pr.update"]["requiredEvidence"]
    assert "required-actions-checks-attestation-pass" in actions["forge.observe.scoped"]["requiredEvidence"]


def test_execution_authorization_rejects_ambiguous_or_underbound_inputs() -> None:
    """输入绑定改为闭合对象；副作用节点不得用错误 SHA、仅内容摘要或重复数组绕过。"""
    schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])

    implementation_without_base = _valid_execution_authorization(
        node_type="IMPLEMENT",
        action_capability="worktree.write",
        input_bindings={"contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c")},
    )
    _assert_rejected(schema, implementation_without_base)

    for node_type in EXPECTED_CANDIDATE_SHA_NODES:
        only_base = _valid_execution_authorization(
            node_type=node_type,
            action_capability=sorted(EXPECTED_ACTIONS_BY_NODE[node_type])[0],
            input_bindings={"baseSha": "a" * 40},
        )
        _assert_rejected(schema, only_base)

    publish_only_content = _valid_execution_authorization(
        node_type="PUBLISH_PR",
        action_capability="pr.create",
        input_bindings={"contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c")},
    )
    _assert_rejected(schema, publish_only_content)

    code_review_only_base = _valid_execution_authorization(
        node_type="CODE_REVIEW",
        action_capability="repo.read",
        input_bindings={"baseSha": "a" * 40},
    )
    _assert_rejected(schema, code_review_only_base)

    for node_type in EXPECTED_CANDIDATE_AND_CONTENT_NODES:
        action_capability = sorted(EXPECTED_ACTIONS_BY_NODE[node_type])[0]
        candidate_only = _valid_execution_authorization(
            node_type=node_type,
            action_capability=action_capability,
            input_bindings={"candidateSha": "b" * 40},
        )
        _assert_rejected(schema, candidate_only)
        content_only = _valid_execution_authorization(
            node_type=node_type,
            action_capability=action_capability,
            input_bindings={"contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c")},
        )
        _assert_rejected(schema, content_only)

    duplicate_candidate_bindings = _valid_execution_authorization(
        input_bindings=[
            {"kind": "CANDIDATE_SHA", "value": "b" * 40},
            {"kind": "CANDIDATE_SHA", "value": "c" * 40},
        ],
    )
    _assert_rejected(schema, duplicate_candidate_bindings)

    plan_with_merge = _valid_execution_authorization(
        node_type="PLAN",
        action_capability="repo.merge",
        input_bindings={"contentDigest": _snapshot_ref("ExecutionAuthorization.inputBindings.contentDigest", "c")},
    )
    _assert_rejected(schema, plan_with_merge)


def _assert_action_policy_exact(node_types: dict[str, Any], node_type: str, capability: str) -> None:
    """逐字段核对单个 action，失败信息必须直接指出 node/action/field。"""
    expected_fields, expected_predicate, expected_evidence = EXPECTED_ACTION_POLICY[node_type][capability]
    node_policy = node_types[node_type]
    actual_key = node_policy["idempotencyKeyTemplate"]["actions"]["byCapability"][capability]
    actual_fact = node_policy["completionFact"]["actions"]["byCapability"][capability]
    assert tuple(actual_key["jcsInputFields"]) == expected_fields, f"{node_type}/{capability}/jcsInputFields"
    assert actual_fact["predicateId"] == expected_predicate, f"{node_type}/{capability}/predicateId"
    assert tuple(actual_fact["requiredEvidence"]) == expected_evidence, f"{node_type}/{capability}/requiredEvidence"


def _assert_node_policy_exact(node_types: dict[str, Any]) -> None:
    """冻结 17 个 node 与全部 action 的精确策略，而非仅检查字段存在或值合法。"""
    assert set(node_types) == set(EXPECTED_NODE_POLICY), "nodeTypes/exact-set"
    assert set(EXPECTED_ACTION_POLICY) == set(EXPECTED_NODE_POLICY), "expected-action-policy/exact-node-set"
    for node_type, expected in EXPECTED_NODE_POLICY.items():
        node_policy = node_types[node_type]
        for field, expected_value in expected.items():
            actual_value = node_policy[field]
            if field in {"requiredCapabilities", "optionalCapabilities"}:
                actual_value = tuple(actual_value)
            assert actual_value == expected_value, f"{node_type}/{field}"

        expected_actions = EXPECTED_ACTIONS_BY_NODE[node_type]
        action_keys = node_policy["idempotencyKeyTemplate"]["actions"]["byCapability"]
        action_facts = node_policy["completionFact"]["actions"]["byCapability"]
        assert set(action_keys) == expected_actions, f"{node_type}/idempotency-actions"
        assert set(action_facts) == expected_actions, f"{node_type}/completion-actions"
        assert set(EXPECTED_ACTION_POLICY[node_type]) == expected_actions, f"{node_type}/expected-action-set"
        for capability in sorted(expected_actions):
            _assert_action_policy_exact(node_types, node_type, capability)


def test_node_capability_map_matches_exact_node_and_action_policy_tables() -> None:
    """精确表同时冻结 capability、side effect、消费点、retry 与 action 事实。"""
    node_types = _load_json(POLICY_PATH)["nodeTypes"]
    _assert_node_policy_exact(node_types)

    guard_locations = [
        (node_type, field)
        for node_type, node_policy in node_types.items()
        for field in ("requiredCapabilities", "optionalCapabilities")
        if "target.guard.clear" in node_policy[field]
    ]
    assert guard_locations == [("RECONCILE_TARGET", "optionalCapabilities")]


def test_exact_policy_tables_reject_review_merge_and_publish_mutations() -> None:
    """复现审查 mutation，证明同步改 key/fact 或换合法枚举值也会被精确表拒绝。"""
    original = _load_json(POLICY_PATH)["nodeTypes"]

    code_review = copy.deepcopy(original)
    review_node = code_review["CODE_REVIEW"]
    review_node["requiredCapabilities"] = ["repo.merge"]
    for container_path in (
        ("idempotencyKeyTemplate", "actions", "byCapability"),
        ("completionFact", "actions", "byCapability"),
    ):
        container = review_node
        for part in container_path:
            container = container[part]
        container["repo.merge"] = container.pop("repo.read")
    with pytest.raises(AssertionError, match="CODE_REVIEW/requiredCapabilities"):
        _assert_node_policy_exact(code_review)

    merge_retry = copy.deepcopy(original)
    merge_retry["MERGE"]["retryClass"] = "bounded-no-external-side-effect"
    with pytest.raises(AssertionError, match="MERGE/retryClass"):
        _assert_node_policy_exact(merge_retry)

    for field in ("jcsInputFields", "predicateId", "requiredEvidence"):
        publish = copy.deepcopy(original)
        if field == "jcsInputFields":
            publish["PUBLISH_PR"]["idempotencyKeyTemplate"]["actions"]["byCapability"]["pr.create"][field] = [
                "repositoryId",
                "targetBranch",
            ]
        else:
            publish["PUBLISH_PR"]["completionFact"]["actions"]["byCapability"]["pr.create"][field] = (
                "mutated-v1" if field == "predicateId" else ["created-pr-id"]
            )
        with pytest.raises(AssertionError, match=f"PUBLISH_PR/pr.create/{field}"):
            _assert_action_policy_exact(publish, "PUBLISH_PR", "pr.create")


def test_node_capability_map_freezes_all_runtime_policy_fields() -> None:
    """17 种 nodeType 均必须给出可机械执行、闭集且可识别的运行时策略字段。"""
    node_types = _load_json(POLICY_PATH)["nodeTypes"]
    assert len(node_types) == 17

    for node_type, node_policy in node_types.items():
        missing = FROZEN_POLICY_FIELDS - set(node_policy)
        assert not missing, f"{node_type}: 缺少冻结运行时字段 {sorted(missing)}"
        for field in FROZEN_POLICY_FIELDS:
            assert node_policy[field], f"{node_type}: {field} 不得为空"
        assert _is_known_retry_class(node_policy["retryClass"]), (
            f"{node_type}: 未知 retryClass {node_policy['retryClass']}"
        )
        assert node_policy["authorizationConsumptionPoint"] in AUTHORIZATION_CONSUMPTION_POINTS, (
            f"{node_type}: 未知 authorizationConsumptionPoint {node_policy['authorizationConsumptionPoint']}"
        )
        fingerprint_schema = node_policy["resourceFingerprintSchema"]
        assert isinstance(fingerprint_schema, dict)
        jsonschema.Draft7Validator.check_schema(fingerprint_schema)
        assert fingerprint_schema.get("type") == "object"
        assert fingerprint_schema.get("additionalProperties") is False
        assert fingerprint_schema.get("required"), f"{node_type}: 指纹 schema 必须声明 required"
        assert fingerprint_schema.get("properties"), f"{node_type}: 指纹 schema 必须声明 properties"
        assert fingerprint_schema.get("allOf"), f"{node_type}: 指纹 schema 必须内嵌条件"

        key_template = node_policy["idempotencyKeyTemplate"]
        assert isinstance(key_template, dict)
        assert set(key_template) == {"version", "hashAlgorithm", "actions"}
        assert key_template["version"] == "factory-action-v1"
        assert key_template["hashAlgorithm"] == "sha256-jcs-nfc"
        assert set(key_template["actions"]) == {"byCapability"}
        action_keys = key_template["actions"]["byCapability"]
        assert isinstance(action_keys, dict) and action_keys

        completion_fact = node_policy["completionFact"]
        assert isinstance(completion_fact, dict)
        assert set(completion_fact) == {"version", "actions"}
        assert completion_fact["version"] == "v1"
        assert set(completion_fact["actions"]) == {"byCapability"}
        action_facts = completion_fact["actions"]["byCapability"]
        assert set(action_keys) == set(action_facts), f"{node_type}: 每个可派发 action 必须恰有一个幂等键定义和完成事实"

        # required/optional capability 都可能构成实际 action；可选项一旦进入 scope 同样不能
        # 使用通用字符串放行，因此都需要各自的 key/fact 模板。
        expected_actions = set(node_policy["requiredCapabilities"]) | set(node_policy["optionalCapabilities"])
        assert expected_actions == set(action_keys), f"{node_type}: actions/byCapability 未覆盖完整 capability 集"
        for capability, action_key in action_keys.items():
            assert isinstance(capability, str) and capability
            assert set(action_key) == {"jcsInputFields"}
            input_fields = action_key["jcsInputFields"]
            assert isinstance(input_fields, list) and input_fields
            assert len(input_fields) == len(set(input_fields))
            assert not (set(input_fields) & FORBIDDEN_IDEMPOTENCY_INPUT_FIELDS), (
                f"{node_type}/{capability}: 幂等键不得包含 Attempt、token、epoch、TTL 或时间"
            )

            action_fact = action_facts[capability]
            assert set(action_fact) == {"predicateId", "requiredEvidence"}
            assert isinstance(action_fact["predicateId"], str) and action_fact["predicateId"]
            evidence = action_fact["requiredEvidence"]
            assert isinstance(evidence, list) and evidence
            assert len(evidence) == len(set(evidence))

    transient_retry = copy.deepcopy(node_types["PLAN"])
    transient_retry["retryClass"] = "TRANSIENT"
    assert not _is_known_retry_class(transient_retry["retryClass"]), (
        "静态 retryClass 不得放行 §17 动态 TRANSIENT 错误类"
    )


def test_node_policy_environment_and_reconcile_constraints_are_fail_closed() -> None:
    """staging/production、恢复演练和目标核对的资源指纹条件必须在 map 内可机械验证。"""
    node_types = _load_json(POLICY_PATH)["nodeTypes"]

    assert node_types["DEPLOY_STAGING"]["resourceFingerprintSchema"]["properties"]["environment"] == {
        "const": "STAGING"
    }
    assert node_types["DEPLOY_PRODUCTION"]["resourceFingerprintSchema"]["properties"]["environment"] == {
        "const": "PRODUCTION"
    }
    assert node_types["ACCEPT_STAGING"]["resourceFingerprintSchema"]["properties"]["environment"] == {
        "const": "STAGING"
    }
    assert node_types["ACCEPT_PRODUCTION"]["resourceFingerprintSchema"]["properties"]["environment"] == {
        "const": "PRODUCTION"
    }

    restore_schema = node_types["RESTORE_DRILL"]["resourceFingerprintSchema"]
    assert restore_schema["properties"]["validationEnvironment"] == {"const": "NON_PRODUCTION"}
    assert "validationInstanceIdentity" in restore_schema["required"]
    assert "sourceDatabasePhysical" in restore_schema["required"]

    reconcile_schema = node_types["RECONCILE_TARGET"]["resourceFingerprintSchema"]
    assert reconcile_schema["properties"]["targetKind"]["enum"] == [
        "server",
        "database",
        "registry",
    ]
    assert "oneOf" in reconcile_schema
    assert "guardFingerprint" in reconcile_schema["required"]

    for node_type in (
        "BOOTSTRAP_REPOSITORY",
        "IMPLEMENT",
        "ATTEST_REVIEW",
        "PUBLISH_PR",
        "MERGE",
        "BUILD_ARTIFACT",
        "DEPLOY_STAGING",
        "ACCEPT_STAGING",
        "DEPLOY_PRODUCTION",
        "ACCEPT_PRODUCTION",
        "ROLLBACK",
        "RESTORE_DRILL",
        "RECONCILE_TARGET",
    ):
        assert node_types[node_type]["authorizationConsumptionPoint"] == ("with-action-started-transaction")


def _test_action_identity(node_policy: dict[str, Any], capability: str, inputs: dict[str, str]) -> str:
    """按策略声明字段构造测试 identity，验证环境域分离而不冒充尚未实现的 runtime。"""
    key_template = node_policy["idempotencyKeyTemplate"]
    fields = key_template["actions"]["byCapability"][capability]["jcsInputFields"]
    material = [key_template["version"], capability, [[field, inputs[field]] for field in fields]]
    return "sha256:" + hashlib.sha256(canonicalize(material)).hexdigest()


def test_environment_actions_bind_fingerprint_and_domain_separate_stage_from_production() -> None:
    """部署、验收与回滚的每个 action identity 都绑定环境和完整资源指纹摘要。"""
    node_types = _load_json(POLICY_PATH)["nodeTypes"]
    environment_nodes = (
        "DEPLOY_STAGING",
        "DEPLOY_PRODUCTION",
        "ACCEPT_STAGING",
        "ACCEPT_PRODUCTION",
        "ROLLBACK",
    )
    for node_type in environment_nodes:
        actions = node_types[node_type]["idempotencyKeyTemplate"]["actions"]["byCapability"]
        for capability, action in actions.items():
            fields = action["jcsInputFields"]
            assert "environment" in fields, f"{node_type}/{capability}/environment"
            assert "resourceFingerprintDigest" in fields, f"{node_type}/{capability}/resourceFingerprintDigest"

    for staging, production, capability in (
        ("DEPLOY_STAGING", "DEPLOY_PRODUCTION", "ssh.exec.scoped"),
        ("ACCEPT_STAGING", "ACCEPT_PRODUCTION", "http.check.scoped"),
    ):
        staging_policy = node_types[staging]
        production_policy = node_types[production]
        staging_fields = staging_policy["idempotencyKeyTemplate"]["actions"]["byCapability"][capability][
            "jcsInputFields"
        ]
        production_fields = production_policy["idempotencyKeyTemplate"]["actions"]["byCapability"][capability][
            "jcsInputFields"
        ]
        assert staging_fields == production_fields
        common_inputs = {field: f"same-{field}" for field in staging_fields}
        staging_inputs = {
            **common_inputs,
            "environment": "STAGING",
            "resourceFingerprintDigest": "sha256:" + "a" * 64,
        }
        production_inputs = {
            **common_inputs,
            "environment": "PRODUCTION",
            "resourceFingerprintDigest": "sha256:" + "b" * 64,
        }
        assert _test_action_identity(staging_policy, capability, staging_inputs) != (
            _test_action_identity(production_policy, capability, production_inputs)
        )


def test_every_environment_action_key_binding_deletion_mutation_is_rejected() -> None:
    """逐 action 删除 environment 或资源指纹摘要，证明部署、验收、回滚门禁均会定位。"""
    original = _load_json(POLICY_PATH)["nodeTypes"]
    for node_type in (
        "DEPLOY_STAGING",
        "DEPLOY_PRODUCTION",
        "ACCEPT_STAGING",
        "ACCEPT_PRODUCTION",
        "ROLLBACK",
    ):
        actions = original[node_type]["idempotencyKeyTemplate"]["actions"]["byCapability"]
        for capability in actions:
            for field in ENVIRONMENT_ACTION_PREFIX:
                mutated = copy.deepcopy(original)
                fields = mutated[node_type]["idempotencyKeyTemplate"]["actions"]["byCapability"][capability][
                    "jcsInputFields"
                ]
                fields.remove(field)
                with pytest.raises(
                    AssertionError,
                    match=rf"{node_type}/{re.escape(capability)}/jcsInputFields",
                ):
                    _assert_action_policy_exact(mutated, node_type, capability)


def _collect_refs(value: object) -> list[str]:
    """递归提取本地策略 JSON 中的 $ref，防止 schema 悄悄依赖外部或未知定义。"""
    if isinstance(value, dict):
        refs = [value["$ref"]] if isinstance(value.get("$ref"), str) else []
        for child in value.values():
            refs.extend(_collect_refs(child))
        return refs
    if isinstance(value, list):
        return [ref for child in value for ref in _collect_refs(child)]
    return []


def _validate_fingerprint_schema_with_local_defs(
    policy_map: dict, fingerprint_schema: dict, instance: dict
) -> list[jsonschema.ValidationError]:
    """以 node map 自身的本地 $defs 解析资源指纹，证明策略不是裸 ID 占位。"""
    wrapper = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$defs": policy_map["$defs"],
        "allOf": [fingerprint_schema],
    }
    return list(jsonschema.Draft7Validator(wrapper).iter_errors(instance))


def _object_paths(value: object, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """枚举实例中每个对象路径，使 nested $defs 也接受未知字段 mutation。"""
    if not isinstance(value, dict):
        return []
    paths = [path]
    for key, child in value.items():
        paths.extend(_object_paths(child, (*path, key)))
    return paths


def _assert_recursive_unknown_fields_rejected(policy_map: dict[str, Any]) -> None:
    """对每个根对象和嵌套对象注入 extra，要求资源指纹 schema 全路径拒绝。"""
    local_defs = policy_map["$defs"]
    for node_type, instance in FINGERPRINT_SAMPLES.items():
        schema = policy_map["nodeTypes"][node_type]["resourceFingerprintSchema"]
        for path in _object_paths(instance):
            mutated = copy.deepcopy(instance)
            cursor = mutated
            for part in path:
                cursor = cursor[part]
            cursor["unapprovedNestedOverlay"] = "blocked"
            errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, mutated)
            location = ".".join(path) or "<root>"
            assert errors, f"{node_type}/{location}: 资源指纹放行了未知字段"

    used_object_defs = {
        ref.removeprefix("#/$defs/")
        for node in policy_map["nodeTypes"].values()
        for ref in _collect_refs(node["resourceFingerprintSchema"])
        if local_defs.get(ref.removeprefix("#/$defs/"), {}).get("type") == "object"
    }
    assert used_object_defs == {
        "serverPhysical",
        "databasePhysical",
        "trafficAdapter",
        "registry",
        "releaseIdentity",
    }


def test_node_fingerprint_schemas_resolve_locally_and_reject_extra_fields() -> None:
    """每种 node 指纹都解析本地定义，并在根对象与每个 nested object 拒绝 extra。"""
    policy_map = _load_json(POLICY_PATH)
    local_defs = policy_map["$defs"]
    assert isinstance(local_defs, dict) and local_defs

    for node_type, instance in FINGERPRINT_SAMPLES.items():
        schema = policy_map["nodeTypes"][node_type]["resourceFingerprintSchema"]
        for ref in _collect_refs(schema):
            assert ref.startswith("#/$defs/"), f"{node_type}: 禁止外部或未知 $ref {ref}"
            assert ref.removeprefix("#/$defs/") in local_defs, f"{node_type}: $ref 未指向本文件定义 {ref}"
        errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, instance)
        assert not errors, f"{node_type}: 有效资源指纹未通过: {errors}"

    _assert_recursive_unknown_fields_rejected(policy_map)


def test_nested_definition_extra_field_mutation_is_rejected() -> None:
    """放开 serverPhysical 的 additionalProperties 必须被嵌套注入门禁定位。"""
    policy_map = _load_json(POLICY_PATH)
    policy_map["$defs"]["serverPhysical"]["additionalProperties"] = True
    with pytest.raises(
        AssertionError,
        match=r"DEPLOY_STAGING/serverPhysical: 资源指纹放行了未知字段",
    ):
        _assert_recursive_unknown_fields_rejected(policy_map)


def _selection_constraints(value: object) -> list[tuple[str, bool, tuple[str, ...]]]:
    """递归提取 *Selected=true/false 对应的 overlay 必需或禁止约束。"""
    if isinstance(value, list):
        return [row for child in value for row in _selection_constraints(child)]
    if not isinstance(value, dict):
        return []

    rows: list[tuple[str, bool, tuple[str, ...]]] = []
    condition_properties = value.get("if", {}).get("properties", {})
    selected_rules = [
        (name, rule["const"])
        for name, rule in condition_properties.items()
        if name.endswith("Selected") and isinstance(rule, dict) and isinstance(rule.get("const"), bool)
    ]
    if selected_rules:
        assert len(selected_rules) == 1
        selector, selected = selected_rules[0]
        if selected:
            overlays = tuple(value.get("then", {}).get("required", ()))
        else:
            overlays = tuple(value.get("then", {}).get("not", {}).get("required", ()))
        rows.append((selector, selected, overlays))

    for child in value.values():
        rows.extend(_selection_constraints(child))
    return rows


def _assert_selection_overlays_fail_closed(policy_map: dict[str, Any]) -> None:
    """穷举每个 selection 的 true 缺 overlay 与 false 携带 overlay 两类拒绝路径。"""
    for node_type, node_policy in policy_map["nodeTypes"].items():
        schema = node_policy["resourceFingerprintSchema"]
        selectors = {name for name in schema.get("properties", {}) if name.endswith("Selected")}
        constraints = _selection_constraints(schema)
        actual_pairs = {(selector, selected) for selector, selected, _ in constraints}
        expected_pairs = {(selector, selected) for selector in selectors for selected in (True, False)}
        assert actual_pairs == expected_pairs, f"{node_type}/selection-branches"

        for selector in sorted(selectors):
            overlays_by_value = {
                selected: overlays
                for selected_selector, selected, overlays in constraints
                if selected_selector == selector
            }
            assert overlays_by_value[True], f"{node_type}/{selector}=true/overlay"
            assert overlays_by_value[True] == overlays_by_value[False], f"{node_type}/{selector}/true-false-overlay"
            overlays = overlays_by_value[True]
            for overlay in overlays:
                true_missing = copy.deepcopy(FINGERPRINT_SAMPLES[node_type])
                true_missing[selector] = True
                true_missing.pop(overlay, None)
                errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, true_missing)
                assert errors, f"{node_type}/{selector}=true/{overlay}/missing"

                false_with_overlay = copy.deepcopy(FINGERPRINT_SAMPLES[node_type])
                false_with_overlay[selector] = False
                assert overlay in false_with_overlay, f"{node_type}/{selector}=false/{overlay}/sample"
                errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, false_with_overlay)
                assert errors, f"{node_type}/{selector}=false/{overlay}/present"

            false_without_overlays = copy.deepcopy(FINGERPRINT_SAMPLES[node_type])
            false_without_overlays[selector] = False
            for overlay in overlays:
                false_without_overlays.pop(overlay, None)
            errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, false_without_overlays)
            assert not errors, f"{node_type}/{selector}=false/without-overlay: {errors}"


def test_optional_capability_selection_branches_are_exhaustively_fail_closed() -> None:
    """自动枚举所有 selection，穷举 true 缺 overlay 与 false 携带 overlay。"""
    policy_map = _load_json(POLICY_PATH)
    _assert_selection_overlays_fail_closed(policy_map)


def test_every_selection_branch_deletion_mutation_is_rejected() -> None:
    """逐一删除 true/false 条件，证明 ACCEPT_STAGING DB 与全部 false 分支不再假绿。"""
    original = _load_json(POLICY_PATH)
    for node_type, node_policy in original["nodeTypes"].items():
        schema = node_policy["resourceFingerprintSchema"]
        for index, condition in enumerate(schema.get("allOf", [])):
            if not _selection_constraints(condition):
                continue
            mutated = copy.deepcopy(original)
            del mutated["nodeTypes"][node_type]["resourceFingerprintSchema"]["allOf"][index]
            with pytest.raises(
                AssertionError,
                match=rf"{node_type}/selection-branches",
            ):
                _assert_selection_overlays_fail_closed(mutated)


def test_generated_authorization_types_are_current_in_all_languages() -> None:
    """catalog 变更后必须由同一生成器更新 Python、TypeScript 和 Rust，禁止手写副本。"""
    result = subprocess.run(
        [sys.executable, str(CODEGEN_PATH), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"授权合同生成物发生漂移：\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"

    generated_sources = {
        "TypeScript": REPO_ROOT / "packages" / "factory-contracts" / "src" / "generated" / "contracts.ts",
        "Python": REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py",
        "Rust": REPO_ROOT / "crates" / "factory-contracts" / "src" / "generated" / "contracts.rs",
    }
    for language, path in generated_sources.items():
        content = path.read_text(encoding="utf-8")
        assert "IntentAuthorization" in content, f"{language} 未生成 IntentAuthorization"
        assert "ExecutionAuthorization" in content, f"{language} 未生成 ExecutionAuthorization"
