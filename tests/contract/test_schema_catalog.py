"""
tests/contract/test_schema_catalog.py

Task 2 合同层测试：验证 schema、策略目录和测试目录的完整性与正确性。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "contracts" / "schemas"
POLICIES_DIR = REPO_ROOT / "contracts" / "policies"
TESTING_DIR = REPO_ROOT / "contracts" / "testing"
CODEGEN_DIR = REPO_ROOT / "contracts" / "codegen"

# 精确期望的 nodeType 集合（来自 Master Spec §6.3）
EXPECTED_NODE_TYPES = {
    "PLAN", "DESIGN_REVIEW", "BOOTSTRAP_REPOSITORY", "IMPLEMENT", "VERIFY",
    "CODE_REVIEW", "ATTEST_REVIEW", "PUBLISH_PR", "MERGE", "BUILD_ARTIFACT",
    "DEPLOY_STAGING", "ACCEPT_STAGING", "DEPLOY_PRODUCTION",
    "ACCEPT_PRODUCTION", "ROLLBACK", "RECONCILE_TARGET",
    "RESTORE_DRILL",
}

# 精确期望的 target_stage 集合
EXPECTED_TARGET_STAGES = {
    "DESIGN_REVIEW", "CODE_REVIEW", "PUBLISH_PR",
    "MERGE", "ACCEPT_STAGING", "ACCEPT_PRODUCTION",
}

# 精确期望的 47 个测试 ID（来自 Master Spec §20.2）
EXPECTED_TEST_IDS = {
    "STAGE-001", "STAGE-002", "STAGE-003", "STAGE-004", "STAGE-005", "STAGE-006",
    "AUTH-001", "AUTH-002",
    "PLAN-001", "PLAN-HASH-001",
    "BOOT-001", "MCP-001",
    "STATE-001", "LEASE-001", "PROC-001",
    "CTRL-001", "CTRL-002",
    "EVENT-HASH-001",
    "STREAM-001", "STREAM-002", "STREAM-DUR-001", "STREAM-BP-001",
    "STREAM-PERF-BURST", "STREAM-PERF-SUSTAINED", "STREAM-PERF-SPARSE",
    "REVIEW-001", "GIT-001", "SIDEFX-001",
    "DEPLOY-001", "DEPLOY-002", "DEPLOY-003",
    "GUARD-001", "DEPLOY-FENCE-001",
    "STORE-001", "BUDGET-001", "RETRY-001", "NOTIFY-001",
    "PATH-001", "PATH-002", "CLI-PROFILE-001",
    "RUNTIME-001", "WSL-IO-001", "NGINX-001", "BACKUP-001",
    "DEPLOY-RAM-001", "SCHED-PERF-001", "COMPAT-001",
}

# 必须存在的 13 个 schema 文件
EXPECTED_SCHEMA_FILES = {
    "run-spec.v1.schema.json",
    "plan-revision.v1.schema.json",
    "control-command.v1.schema.json",
    "credential-ref.v1.schema.json",
    "task-intake.v1.schema.json",
    "claude-self-review.v1.schema.json",
    "prepared-event.v2.schema.json",
    "prepared-batch.v2.schema.json",
    "durable-event.v2.schema.json",
    "ipc-envelope.v1.schema.json",
    "runner-protocol.v1.schema.json",
    "test-receipt.v1.schema.json",
    "compatibility-manifest.v1.schema.json",
}


# ─────────────────────── fixtures ───────────────────────


@pytest.fixture(scope="session")
def node_capability_map() -> dict:  # type: ignore[type-arg]
    """加载 node-capability-map.v1.json"""
    path = POLICIES_DIR / "node-capability-map.v1.json"
    assert path.exists(), f"node-capability-map.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def stage_capability_map() -> dict:  # type: ignore[type-arg]
    """加载 stage-capability-map.v1.json"""
    path = POLICIES_DIR / "stage-capability-map.v1.json"
    assert path.exists(), f"stage-capability-map.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def node_pause_policy() -> dict:  # type: ignore[type-arg]
    """加载 node-pause-policy.v1.json"""
    path = POLICIES_DIR / "node-pause-policy.v1.json"
    assert path.exists(), f"node-pause-policy.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def catalog() -> dict:  # type: ignore[type-arg]
    """加载 node-capability-map（作为 catalog 别名，保持向后兼容）"""
    path = POLICIES_DIR / "node-capability-map.v1.json"
    assert path.exists(), f"node-capability-map.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    # 注入 node_types 属性方便测试
    data["node_types"] = list(data.get("nodeTypes", {}).keys())
    return data  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def required_test_catalog() -> dict:  # type: ignore[type-arg]
    """加载 required-test-catalog.v1.json"""
    path = TESTING_DIR / "required-test-catalog.v1.json"
    assert path.exists(), f"required-test-catalog.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


@pytest.fixture(scope="session")
def codegen_catalog() -> dict:  # type: ignore[type-arg]
    """加载 codegen catalog.v1.json"""
    path = CODEGEN_DIR / "catalog.v1.json"
    assert path.exists(), f"catalog.v1.json 不存在: {path}"
    with path.open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


# ─────────────────────── schema file tests ───────────────────────


def test_all_schema_files_exist() -> None:
    """所有 13 个 schema 文件必须存在"""
    missing = []
    for fname in EXPECTED_SCHEMA_FILES:
        if not (SCHEMAS_DIR / fname).exists():
            missing.append(fname)
    assert not missing, f"缺少 schema 文件: {missing}"


def test_all_schemas_have_additional_properties_false() -> None:
    """所有 schema 的顶层对象必须设置 additionalProperties: false（fail-closed 规则）"""
    errors = []
    for fname in EXPECTED_SCHEMA_FILES:
        path = SCHEMAS_DIR / fname
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            schema = json.load(f)
        # task-intake.v1 使用 oneOf + definitions，检查 definitions 内每个对象
        if "definitions" in schema:
            for def_name, def_schema in schema["definitions"].items():
                if def_schema.get("type") == "object":
                    if def_schema.get("additionalProperties") is not False:
                        errors.append(f"{fname}#definitions/{def_name}: 缺少 additionalProperties:false")
        elif schema.get("type") == "object":
            if schema.get("additionalProperties") is not False:
                errors.append(f"{fname}: 顶层对象缺少 additionalProperties:false")
    assert not errors, f"additionalProperties 违规:\n" + "\n".join(errors)


def test_all_schemas_are_valid_json() -> None:
    """所有 schema 文件必须是合法 JSON"""
    for fname in EXPECTED_SCHEMA_FILES:
        path = SCHEMAS_DIR / fname
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as f:
            schema = json.load(f)  # 如果不合法 JSON 会抛出异常
        assert isinstance(schema, dict), f"{fname}: 解析结果不是 dict"


# ─────────────────────── node type tests ───────────────────────


def test_unknown_node_type_is_not_present_in_capability_catalog(catalog: dict) -> None:
    """node-capability-map 必须精确包含 17 个 nodeType，不多不少"""
    actual = set(catalog["node_types"])
    assert actual == EXPECTED_NODE_TYPES, (
        f"nodeType 集合不匹配。\n"
        f"  多出: {actual - EXPECTED_NODE_TYPES}\n"
        f"  缺少: {EXPECTED_NODE_TYPES - actual}"
    )


def test_node_capability_map_has_exactly_17_node_types(node_capability_map: dict) -> None:
    """node-capability-map 必须有精确 17 个 nodeType"""
    node_types = set(node_capability_map.get("nodeTypes", {}).keys())
    assert len(node_types) == 17, f"nodeType 数量应为 17，实际为 {len(node_types)}"
    assert node_types == EXPECTED_NODE_TYPES


def test_each_node_type_has_required_capabilities(node_capability_map: dict) -> None:
    """每个 nodeType 必须有 requiredCapabilities 和 sideEffectClass"""
    for node_type, node_def in node_capability_map.get("nodeTypes", {}).items():
        assert "requiredCapabilities" in node_def, (
            f"{node_type}: 缺少 requiredCapabilities"
        )
        assert isinstance(node_def["requiredCapabilities"], list), (
            f"{node_type}: requiredCapabilities 必须是数组"
        )
        assert "sideEffectClass" in node_def, (
            f"{node_type}: 缺少 sideEffectClass"
        )


def test_restore_drill_has_required_capabilities(node_capability_map: dict) -> None:
    """RESTORE_DRILL 必须包含 db.restore、restore.validation.instance、db.check"""
    restore_drill = node_capability_map["nodeTypes"]["RESTORE_DRILL"]
    required = set(restore_drill.get("requiredCapabilities", []))
    assert "db.restore" in required, "RESTORE_DRILL 缺少 db.restore"
    assert "restore.validation.instance" in required, "RESTORE_DRILL 缺少 restore.validation.instance"
    assert "db.check" in required, "RESTORE_DRILL 缺少 db.check"


# ─────────────────────── stage capability tests ───────────────────────


def test_stage_capability_map_has_all_six_stages(stage_capability_map: dict) -> None:
    """stage-capability-map 必须包含全部 6 个 target_stage"""
    stage_caps = set(stage_capability_map.get("stageCapabilities", {}).keys())
    assert stage_caps == EXPECTED_TARGET_STAGES, (
        f"stage 集合不匹配。\n"
        f"  多出: {stage_caps - EXPECTED_TARGET_STAGES}\n"
        f"  缺少: {EXPECTED_TARGET_STAGES - stage_caps}"
    )


def test_stage_capability_arrays_are_sorted(stage_capability_map: dict) -> None:
    """每个 stage 的 capabilities 数组必须已排序（完全展开并排序）"""
    for stage, stage_def in stage_capability_map.get("stageCapabilities", ).items():
        caps = stage_def.get("capabilities", [])
        assert caps == sorted(caps), (
            f"{stage}: capabilities 数组未排序。\n"
            f"  实际: {caps}\n"
            f"  期望: {sorted(caps)}"
        )


def test_stage_capability_arrays_have_no_duplicates(stage_capability_map: dict) -> None:
    """每个 stage 的 capabilities 数组不能有重复"""
    for stage, stage_def in stage_capability_map.get("stageCapabilities", {}).items():
        caps = stage_def.get("capabilities", [])
        assert len(caps) == len(set(caps)), (
            f"{stage}: capabilities 数组有重复项: {caps}"
        )


def test_higher_stages_include_lower_stage_capabilities(stage_capability_map: dict) -> None:
    """高级 stage 的 capabilities 必须是低级 stage 的超集（渐进式权限）"""
    stage_caps = stage_capability_map.get("stageCapabilities", {})
    design = set(stage_caps["DESIGN_REVIEW"]["capabilities"])
    code_review = set(stage_caps["CODE_REVIEW"]["capabilities"])
    publish_pr = set(stage_caps["PUBLISH_PR"]["capabilities"])
    merge = set(stage_caps["MERGE"]["capabilities"])
    accept_staging = set(stage_caps["ACCEPT_STAGING"]["capabilities"])
    accept_prod = set(stage_caps["ACCEPT_PRODUCTION"]["capabilities"])

    assert design.issubset(code_review), "CODE_REVIEW 应包含 DESIGN_REVIEW 的全部 capabilities"
    assert code_review.issubset(publish_pr), "PUBLISH_PR 应包含 CODE_REVIEW 的全部 capabilities"
    assert publish_pr.issubset(merge), "MERGE 应包含 PUBLISH_PR 的全部 capabilities"
    assert merge.issubset(accept_staging), "ACCEPT_STAGING 应包含 MERGE 的全部 capabilities"
    assert accept_staging.issubset(accept_prod), "ACCEPT_PRODUCTION 应包含 ACCEPT_STAGING 的全部 capabilities"


# ─────────────────────── node pause policy tests ───────────────────────


def test_node_pause_policy_covers_all_node_types(node_pause_policy: dict) -> None:
    """node-pause-policy 必须覆盖所有 17 种 nodeType"""
    covered = set(node_pause_policy.get("policies", {}).keys())
    missing = EXPECTED_NODE_TYPES - covered
    assert not missing, f"node-pause-policy 缺少以下 nodeType 的策略: {missing}"


def test_node_pause_policy_has_unknown_disposition(node_pause_policy: dict) -> None:
    """node-pause-policy 必须有 unknownNodeTypeDisposition（UNKNOWN 处置）"""
    assert "unknownNodeTypeDisposition" in node_pause_policy, (
        "node-pause-policy 缺少 unknownNodeTypeDisposition"
    )
    disposition = node_pause_policy["unknownNodeTypeDisposition"]
    assert "action" in disposition, "unknownNodeTypeDisposition 缺少 action 字段"


def test_each_pause_policy_has_required_fields(node_pause_policy: dict) -> None:
    """每个 nodeType 的暂停策略必须包含 safePoint、graceMs、criticalSections、unknownDisposition"""
    for node_type, policy in node_pause_policy.get("policies", {}).items():
        assert "safePoint" in policy, f"{node_type}: 缺少 safePoint"
        assert "graceMs" in policy, f"{node_type}: 缺少 graceMs"
        assert "criticalSections" in policy, f"{node_type}: 缺少 criticalSections"
        assert "unknownDisposition" in policy, f"{node_type}: 缺少 unknownDisposition"
        assert isinstance(policy["criticalSections"], list), (
            f"{node_type}: criticalSections 必须是数组"
        )


# ─────────────────────── required test catalog tests ───────────────────────


def test_required_test_catalog_has_exactly_47_ids(required_test_catalog: dict) -> None:
    """required-test-catalog 必须有精确 47 个测试 ID"""
    tests = required_test_catalog.get("tests", [])
    assert len(tests) == 47, f"测试 ID 数量应为 47，实际为 {len(tests)}"


def test_required_test_catalog_all_ids_unique(required_test_catalog: dict) -> None:
    """required-test-catalog 中的测试 ID 必须全部唯一"""
    ids = [t["testId"] for t in required_test_catalog.get("tests", [])]
    assert len(ids) == len(set(ids)), (
        f"有重复测试 ID: {[i for i in ids if ids.count(i) > 1]}"
    )


def test_required_test_catalog_matches_expected_ids(required_test_catalog: dict) -> None:
    """required-test-catalog 中的测试 ID 必须与 Master Spec §20.2 完全匹配"""
    actual_ids = {t["testId"] for t in required_test_catalog.get("tests", [])}
    assert actual_ids == EXPECTED_TEST_IDS, (
        f"测试 ID 集合不匹配。\n"
        f"  多出: {actual_ids - EXPECTED_TEST_IDS}\n"
        f"  缺少: {EXPECTED_TEST_IDS - actual_ids}"
    )


def test_required_test_catalog_total_count_matches(required_test_catalog: dict) -> None:
    """required-test-catalog 的 totalCount 字段必须等于实际测试数量"""
    total = required_test_catalog.get("totalCount", 0)
    actual = len(required_test_catalog.get("tests", []))
    assert total == actual, f"totalCount={total} 与实际测试数 {actual} 不符"


def test_each_test_has_required_catalog_fields(required_test_catalog: dict) -> None:
    """每个测试条目必须包含 implementationContributors/finalPassOwner/requiredReplays/scenarioContractDigest"""
    for test in required_test_catalog.get("tests", []):
        tid = test.get("testId", "?")
        assert "implementationContributors" in test, f"{tid}: 缺少 implementationContributors"
        assert "finalPassOwner" in test, f"{tid}: 缺少 finalPassOwner"
        assert "requiredReplays" in test, f"{tid}: 缺少 requiredReplays"
        assert "scenarioContractDigest" in test, f"{tid}: 缺少 scenarioContractDigest"


def test_stream_dur_001_has_crash_point_ids(required_test_catalog: dict) -> None:
    """STREAM-DUR-001 的场景摘要必须包含稳定 crashPointId record_prefix_mid_batch 和 record_torn_next"""
    tests_by_id = {t["testId"]: t for t in required_test_catalog.get("tests", [])}
    stream_dur = tests_by_id.get("STREAM-DUR-001")
    assert stream_dur is not None, "未找到 STREAM-DUR-001"
    crash_points = stream_dur.get("crashPointIds", [])
    assert "record_prefix_mid_batch" in crash_points, (
        "STREAM-DUR-001 缺少 crashPointId: record_prefix_mid_batch"
    )
    assert "record_torn_next" in crash_points, (
        "STREAM-DUR-001 缺少 crashPointId: record_torn_next"
    )


# ─────────────────────── codegen catalog tests ───────────────────────


def test_codegen_catalog_exists(codegen_catalog: dict) -> None:
    """codegen catalog.v1.json 必须存在且可解析"""
    assert "schemas" in codegen_catalog, "catalog.v1.json 缺少 schemas 字段"


def test_codegen_catalog_has_output_targets(codegen_catalog: dict) -> None:
    """codegen catalog 必须声明三语言输出目标"""
    targets = codegen_catalog.get("outputTargets", {})
    assert "typescript" in targets, "catalog 缺少 typescript 输出目标"
    assert "python" in targets, "catalog 缺少 python 输出目标"
    assert "rust" in targets, "catalog 缺少 rust 输出目标"


def test_codegen_generated_files_exist() -> None:
    """三语言生成文件必须存在"""
    ts_path = REPO_ROOT / "packages" / "factory-contracts" / "src" / "generated" / "contracts.ts"
    py_path = REPO_ROOT / "apps" / "agent" / "src" / "factory_agent" / "contracts" / "generated" / "models.py"
    rs_path = REPO_ROOT / "crates" / "factory-contracts" / "src" / "generated" / "contracts.rs"

    assert ts_path.exists(), f"TypeScript 生成文件不存在: {ts_path}"
    assert py_path.exists(), f"Python 生成文件不存在: {py_path}"
    assert rs_path.exists(), f"Rust 生成文件不存在: {rs_path}"


def test_codegen_no_drift() -> None:
    """generate.py --check 必须通过（三语言生成树无漂移）"""
    import subprocess

    result = subprocess.run(
        ["python", str(CODEGEN_DIR / "generate.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"generate.py --check 失败（漂移检测）:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )


def test_runtime_only_schemas_not_in_codegen(codegen_catalog: dict) -> None:
    """runtime-only schema 不应包含在 codegen 生成目标中"""
    for schema_entry in codegen_catalog.get("schemas", []):
        if schema_entry.get("category") == "runtime-only":
            assert schema_entry.get("codegen") is not True, (
                f"runtime-only schema '{schema_entry.get('name')}' 不应设置 codegen=true"
            )


# ─────────────────────── claude-self-review specific tests ───────────────────────


def test_claude_self_review_result_is_enum() -> None:
    """claude-self-review.v1 的 result 字段必须是 pass|needs_fix 枚举"""
    path = SCHEMAS_DIR / "claude-self-review.v1.schema.json"
    with path.open(encoding="utf-8") as f:
        schema = json.load(f)
    result_prop = schema["properties"]["result"]
    assert "enum" in result_prop, "result 字段必须使用 enum"
    assert set(result_prop["enum"]) == {"pass", "needs_fix"}, (
        f"result enum 应为 ['pass', 'needs_fix']，实际为 {result_prop['enum']}"
    )


def test_compatibility_manifest_synchronous_is_full_only() -> None:
    """compatibility-manifest.v1 的 eventBatchParameters.synchronous 必须 const='FULL'"""
    path = SCHEMAS_DIR / "compatibility-manifest.v1.schema.json"
    with path.open(encoding="utf-8") as f:
        schema = json.load(f)
    sync_prop = schema["properties"]["eventBatchParameters"]["properties"]["synchronous"]
    assert sync_prop.get("const") == "FULL", (
        f"synchronous 必须 const='FULL'，实际: {sync_prop}"
    )


def test_task_intake_request_excludes_agent_fields() -> None:
    """TaskIntakeRequest 不得包含 capability digest、规范化绑定或 IntentAuthorization 字段"""
    path = SCHEMAS_DIR / "task-intake.v1.schema.json"
    with path.open(encoding="utf-8") as f:
        schema = json.load(f)
    request_def = schema["definitions"]["TaskIntakeRequest"]
    props = set(request_def.get("properties", {}).keys())
    forbidden = {
        "allowedCapabilitySetDigest", "intentAuthorizationId", "taskId",
        "normalizedRepositoryBinding", "riskSummaryDigest",
    }
    overlap = props & forbidden
    assert not overlap, (
        f"TaskIntakeRequest 不得包含以下 Agent 专用字段: {overlap}"
    )


def test_task_intake_accepted_excludes_workspace_fields() -> None:
    """TaskIntakeAccepted 不得暴露 workspace lease、checkpoint namespace 或 bundle 内部路径"""
    path = SCHEMAS_DIR / "task-intake.v1.schema.json"
    with path.open(encoding="utf-8") as f:
        schema = json.load(f)
    accepted_def = schema["definitions"]["TaskIntakeAccepted"]
    props = set(accepted_def.get("properties", {}).keys())
    forbidden = {
        "workspaceLease", "checkpointNamespace", "bundlePath",
        "internalPath", "worktreePath",
    }
    overlap = props & forbidden
    assert not overlap, (
        f"TaskIntakeAccepted 不得暴露以下工作区内部字段: {overlap}"
    )
