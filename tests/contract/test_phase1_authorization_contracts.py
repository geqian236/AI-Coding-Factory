"""Phase 1 授权合同测试：冻结 schema、策略字段和三语言生成物的 fail-closed 边界。"""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "contracts" / "schemas"
CATALOG_PATH = REPO_ROOT / "contracts" / "codegen" / "catalog.v1.json"
POLICY_PATH = REPO_ROOT / "contracts" / "policies" / "node-capability-map.v1.json"
CODEGEN_PATH = REPO_ROOT / "contracts" / "codegen" / "generate.py"

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

INTENT_DIGEST_FIELDS = (
    "requirementDigest",
    "repositoryBindingDigest",
    "baselineDigest",
    "allowedCapabilitySetDigest",
    "targetBindingDigest",
    "estimatedCostAlertDigest",
)

EXECUTION_DIGEST_FIELDS = (
    "semanticPlanHash",
    "planRevisionDigest",
    "stageCapabilityMapDigest",
    "nodeCapabilityMapDigest",
    "capabilityScopeDigest",
    "idempotencyKey",
    "actionPolicySnapshotDigest",
)

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


def _assert_rejected(schema: dict[str, Any], instance: dict[str, Any]) -> None:
    """用真实 Draft7Validator 断言不可信对象被 schema 拒绝。"""
    errors = list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).iter_errors(instance)
    )
    assert errors, f"应被拒绝的合同对象意外通过: {instance}"


def _assert_all_object_schemas_fail_closed(schema: dict[str, Any]) -> None:
    """递归检查本轮授权 schema 的每个显式 object 都禁止未知字段。"""
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, schema
    for value in schema.values():
        if isinstance(value, dict):
            _assert_all_object_schemas_fail_closed(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_all_object_schemas_fail_closed(item)


def _is_known_retry_class(value: object) -> bool:
    """只接受策略 map 冻结的静态重试包络，不能误把 §17 动态错误类当静态策略。"""
    return isinstance(value, str) and value in RETRY_CLASSES


def _valid_intent_authorization() -> dict[str, Any]:
    """构造完整 IntentAuthorization 基准，覆盖 §6.2 的用户授权包络。"""
    return {
        "intentAuthorizationId": "intent-001",
        "taskId": "task-001",
        "userId": "user-001",
        "requirementDigest": "sha256:" + "1" * 64,
        "projectId": "project-001",
        "repositoryId": "repo-001",
        "repositoryBindingDigest": "sha256:" + "2" * 64,
        "baselineDigest": "sha256:" + "3" * 64,
        "targetStage": "CODEX_APPROVED",
        "stageCapabilityMapVersion": "1",
        "allowedCapabilitySetDigest": "sha256:" + "4" * 64,
        "targetBindingDigest": "sha256:" + "5" * 64,
        "riskCeiling": "medium",
        "estimatedCostAlertDigest": "sha256:" + "6" * 64,
        "autonomousExecutionBudgetMs": 3_600_000,
        "repairLoopLimit": 3,
        "autoReplanLimit": 2,
        "attemptLimit": 4,
        "issuedAt": "2026-08-10T00:00:00Z",
        "expiresAt": "2026-08-11T00:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


def _valid_execution_authorization() -> dict[str, Any]:
    """构造 PUBLISH_PR 授权基准，验证副作用前必须绑定 candidate 输入身份。"""
    return {
        "executionAuthorizationId": "execution-001",
        "intentAuthorizationId": "intent-001",
        "planRevisionId": "plan-001",
        "semanticPlanHash": "sha256:" + "3" * 64,
        "planRevisionDigest": "sha256:" + "4" * 64,
        "nodeType": "PUBLISH_PR",
        "stageCapabilityMapVersion": "1",
        "stageCapabilityMapDigest": "sha256:" + "5" * 64,
        "nodeCapabilityMapVersion": "1",
        "nodeCapabilityMapDigest": "sha256:" + "6" * 64,
        "runId": "run-001",
        "stepId": "step-001",
        "attemptId": "attempt-001",
        "executorId": "executor-001",
        "fencingToken": 1,
        "controlEpoch": 1,
        "acceptedControlCommandSeq": 1,
        "resourceFingerprint": {
            "schemaDigest": "sha256:" + "7" * 64,
            "fingerprintDigest": "sha256:" + "8" * 64,
        },
        "capabilityScopeDigest": "sha256:" + "9" * 64,
        "idempotencyKey": "sha256:" + "a" * 64,
        "inputBindings": [
            {"kind": "BASE_SHA", "value": "a" * 40},
            {"kind": "CANDIDATE_SHA", "value": "b" * 40},
        ],
        "actionCapability": "pr.create",
        "actionPolicySnapshotDigest": "sha256:" + "c" * 64,
        "maxUses": 1,
        "consumptionState": "AVAILABLE",
        "issuedAt": "2026-08-10T00:00:00Z",
        "expiresAt": "2026-08-10T01:00:00Z",
        "revokedAt": None,
        "revokeReason": None,
    }


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
            format_checker=jsonschema.FormatChecker(),
        ).iter_errors(valid)
    )

    # 每一个 required 字段缺失都必须 fail-closed，不能只覆盖一个示例字段。
    for field in sorted(REQUIRED_INTENT_FIELDS):
        missing_field = copy.deepcopy(valid)
        missing_field.pop(field)
        _assert_rejected(schema, missing_field)

    # 每个摘要字段都拒绝大写十六进制和不完整长度，确保内容寻址不会静默归一化。
    for field in INTENT_DIGEST_FIELDS:
        uppercase_digest = copy.deepcopy(valid)
        uppercase_digest[field] = "sha256:" + "A" * 64
        _assert_rejected(schema, uppercase_digest)

        short_digest = copy.deepcopy(valid)
        short_digest[field] = "sha256:" + "a" * 63
        _assert_rejected(schema, short_digest)

    unknown_stage = copy.deepcopy(valid)
    unknown_stage["targetStage"] = "SUPERUSER_APPROVED"
    _assert_rejected(schema, unknown_stage)

    plan_revision_leak = copy.deepcopy(valid)
    plan_revision_leak["planRevisionId"] = "plan-001"
    _assert_rejected(schema, plan_revision_leak)

    double_hash_leak = copy.deepcopy(valid)
    double_hash_leak["semanticPlanHash"] = "sha256:" + "f" * 64
    _assert_rejected(schema, double_hash_leak)

    i_json_overflow = copy.deepcopy(valid)
    i_json_overflow["autonomousExecutionBudgetMs"] = 9_007_199_254_740_992
    _assert_rejected(schema, i_json_overflow)


def test_execution_authorization_schema_rejects_wrong_binding_unknown_enum_and_extra_field() -> None:
    """副作用授权必须绑定 candidate，且未知状态、额外键和缺 fencing 均 fail-closed。"""
    schema = _load_json(SCHEMAS_DIR / AUTHORIZATION_SCHEMA_FILES["ExecutionAuthorization"])
    valid = _valid_execution_authorization()
    assert not list(
        jsonschema.Draft7Validator(
            schema,
            format_checker=jsonschema.FormatChecker(),
        ).iter_errors(valid)
    )

    # 派生授权的全部 required 字段缺失均应被拒绝，包含 lease、消费与撤销投影。
    for field in sorted(REQUIRED_EXECUTION_FIELDS):
        missing_field = copy.deepcopy(valid)
        missing_field.pop(field)
        _assert_rejected(schema, missing_field)

    for field in EXECUTION_DIGEST_FIELDS:
        uppercase_digest = copy.deepcopy(valid)
        uppercase_digest[field] = "sha256:" + "B" * 64
        _assert_rejected(schema, uppercase_digest)

        short_digest = copy.deepcopy(valid)
        short_digest[field] = "sha256:" + "b" * 63
        _assert_rejected(schema, short_digest)

    wrong_side_effect_binding = copy.deepcopy(valid)
    wrong_side_effect_binding["inputBindings"][1]["value"] = "not-a-commit-sha"
    _assert_rejected(schema, wrong_side_effect_binding)

    uppercase_content_digest = copy.deepcopy(valid)
    uppercase_content_digest["inputBindings"][1] = {
        "kind": "CONTENT_DIGEST",
        "value": "sha256:" + "C" * 64,
    }
    _assert_rejected(schema, uppercase_content_digest)

    short_content_digest = copy.deepcopy(valid)
    short_content_digest["inputBindings"][1] = {
        "kind": "CONTENT_DIGEST",
        "value": "sha256:" + "c" * 63,
    }
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

    resource_fingerprint_extra = copy.deepcopy(valid)
    resource_fingerprint_extra["resourceFingerprint"]["unapprovedOverlay"] = "blocked"
    _assert_rejected(schema, resource_fingerprint_extra)

    input_binding_extra = copy.deepcopy(valid)
    input_binding_extra["inputBindings"][0]["unapprovedOverlay"] = "blocked"
    _assert_rejected(schema, input_binding_extra)

    missing_fencing = copy.deepcopy(valid)
    missing_fencing.pop("fencingToken")
    _assert_rejected(schema, missing_fencing)


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
            f"{node_type}: 未知 authorizationConsumptionPoint "
            f"{node_policy['authorizationConsumptionPoint']}"
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
        assert set(action_keys) == set(action_facts), (
            f"{node_type}: 每个可派发 action 必须恰有一个幂等键定义和完成事实"
        )

        # required/optional capability 都可能构成实际 action；可选项一旦进入 scope 同样不能
        # 使用通用字符串放行，因此都需要各自的 key/fact 模板。
        expected_actions = set(node_policy["requiredCapabilities"]) | set(
            node_policy["optionalCapabilities"]
        )
        assert expected_actions == set(action_keys), (
            f"{node_type}: actions/byCapability 未覆盖完整 capability 集"
        )
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

    assert (
        node_types["DEPLOY_STAGING"]["resourceFingerprintSchema"]["properties"]["environment"]
        == {"const": "STAGING"}
    )
    assert (
        node_types["DEPLOY_PRODUCTION"]["resourceFingerprintSchema"]["properties"]["environment"]
        == {"const": "PRODUCTION"}
    )
    assert (
        node_types["ACCEPT_STAGING"]["resourceFingerprintSchema"]["properties"]["environment"]
        == {"const": "STAGING"}
    )
    assert (
        node_types["ACCEPT_PRODUCTION"]["resourceFingerprintSchema"]["properties"]["environment"]
        == {"const": "PRODUCTION"}
    )

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
        assert node_types[node_type]["authorizationConsumptionPoint"] == (
            "with-action-started-transaction"
        )


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


def test_node_fingerprint_schemas_resolve_locally_and_reject_extra_fields() -> None:
    """每种 node 指纹都必须真实解析本文件本地定义，并拒绝未知字段。"""
    policy_map = _load_json(POLICY_PATH)
    local_defs = policy_map["$defs"]
    assert isinstance(local_defs, dict) and local_defs

    for node_type, instance in FINGERPRINT_SAMPLES.items():
        schema = policy_map["nodeTypes"][node_type]["resourceFingerprintSchema"]
        for ref in _collect_refs(schema):
            assert ref.startswith("#/$defs/"), f"{node_type}: 禁止外部或未知 $ref {ref}"
            assert ref.removeprefix("#/$defs/") in local_defs, (
                f"{node_type}: $ref 未指向本文件定义 {ref}"
            )
        errors = _validate_fingerprint_schema_with_local_defs(policy_map, schema, instance)
        assert not errors, f"{node_type}: 有效资源指纹未通过: {errors}"

        unknown_field = copy.deepcopy(instance)
        unknown_field["unapprovedOverlay"] = "blocked"
        errors = _validate_fingerprint_schema_with_local_defs(
            policy_map, schema, unknown_field
        )
        assert errors, f"{node_type}: 资源指纹放行了未知字段"


def test_optional_capability_overlays_are_required_when_selected() -> None:
    """可选网络、registry、DB 与流量 capability 被选择时必须触发对应物理 overlay。"""
    policy_map = _load_json(POLICY_PATH)
    cases = [
        ("VERIFY", "egressPolicyDigest"),
        ("BUILD_ARTIFACT", "registry"),
        ("DEPLOY_STAGING", "databasePhysical"),
        ("DEPLOY_STAGING", "trafficAdapter"),
        ("ACCEPT_PRODUCTION", "databasePhysical"),
        ("ROLLBACK", "trafficAdapter"),
    ]
    for node_type, required_overlay in cases:
        invalid = copy.deepcopy(FINGERPRINT_SAMPLES[node_type])
        invalid.pop(required_overlay)
        errors = _validate_fingerprint_schema_with_local_defs(
            policy_map,
            policy_map["nodeTypes"][node_type]["resourceFingerprintSchema"],
            invalid,
        )
        assert errors, f"{node_type}: 已选择 capability 时缺失 {required_overlay} 仍被放行"


def _future_node_policy_compatibility_digest(policy_map: dict) -> str:
    """计算供 Task4 未来绑定的策略摘要；本节点不改写已冻结 CompatibilityManifest。"""
    canonical = json.dumps(
        policy_map, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_node_policy_change_changes_future_compatibility_digest() -> None:
    """node map 漂移会改变 Task4 后续必须绑定的摘要，但不伪称已改写历史 manifest。"""
    policy_map = _load_json(POLICY_PATH)
    baseline = _future_node_policy_compatibility_digest(policy_map)
    drifted = copy.deepcopy(policy_map)
    drifted["nodeTypes"]["PLAN"]["completionFact"]["actions"]["byCapability"][
        "repo.read"
    ]["predicateId"] = "plan-artifacts-replaced-v1"
    assert _future_node_policy_compatibility_digest(drifted) != baseline


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
    assert result.returncode == 0, (
        "授权合同生成物发生漂移：\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    generated_sources = {
        "TypeScript": REPO_ROOT / "packages" / "factory-contracts" / "src" / "generated" / "contracts.ts",
        "Python": REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py",
        "Rust": REPO_ROOT / "crates" / "factory-contracts" / "src" / "generated" / "contracts.rs",
    }
    for language, path in generated_sources.items():
        content = path.read_text(encoding="utf-8")
        assert "IntentAuthorization" in content, f"{language} 未生成 IntentAuthorization"
        assert "ExecutionAuthorization" in content, f"{language} 未生成 ExecutionAuthorization"
