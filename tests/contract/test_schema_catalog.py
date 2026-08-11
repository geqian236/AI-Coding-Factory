"""
tests/contract/test_schema_catalog.py

Task 2 合同层测试：验证 schema、策略目录和测试目录的完整性与正确性。
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
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

# 精确期望的 target_stage 集合（Master Spec §6.1 权威 6 阶段）
EXPECTED_TARGET_STAGES = {
    "DESIGN_APPROVED", "CODEX_APPROVED", "PR_READY",
    "MERGED", "STAGING_ACCEPTED", "PRODUCTION_ACCEPTED",
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

# 必须存在的 16 个 schema 文件
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
    "authoritative-state-event.v1.schema.json",
    "ipc-envelope.v1.schema.json",
    "runner-protocol.v1.schema.json",
    "test-receipt.v1.schema.json",
    "compatibility-manifest.v1.schema.json",
    "intent-authorization.v1.schema.json",
    "execution-authorization.v1.schema.json",
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
    """所有 16 个 schema 文件必须存在"""
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
    assert not errors, "additionalProperties 违规:\n" + "\n".join(errors)


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
    design = set(stage_caps["DESIGN_APPROVED"]["capabilities"])
    codex_approved = set(stage_caps["CODEX_APPROVED"]["capabilities"])
    pr_ready = set(stage_caps["PR_READY"]["capabilities"])
    merged = set(stage_caps["MERGED"]["capabilities"])
    staging_accepted = set(stage_caps["STAGING_ACCEPTED"]["capabilities"])
    production_accepted = set(stage_caps["PRODUCTION_ACCEPTED"]["capabilities"])

    assert design.issubset(codex_approved), "CODEX_APPROVED 应包含 DESIGN_APPROVED 的全部 capabilities"
    assert codex_approved.issubset(pr_ready), "PR_READY 应包含 CODEX_APPROVED 的全部 capabilities"
    assert pr_ready.issubset(merged), "MERGED 应包含 PR_READY 的全部 capabilities"
    assert merged.issubset(staging_accepted), "STAGING_ACCEPTED 应包含 MERGED 的全部 capabilities"
    assert (
        staging_accepted.issubset(production_accepted)
    ), "PRODUCTION_ACCEPTED 应包含 STAGING_ACCEPTED 的全部 capabilities"


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


def test_codegen_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """generate.py --check 必须通过，并在 cp1252:strict 与配置失败时保持 UTF-8/fail-closed。

    使用 sys.executable 而非裸 "python"：后者经 PATH 解析，
    可能命中与运行 pytest 不同的解释器，导致漂移检测结果不可信。
    显式 encoding="utf-8" 避免 Windows locale（GBK）解码子进程中文输出时崩溃。
    """
    result = subprocess.run(
        [sys.executable, str(CODEGEN_DIR / "generate.py"), "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, (
        f"generate.py --check 失败（漂移检测）:\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )

    hostile_env = {
        **os.environ,
        "PYTHONIOENCODING": "cp1252:strict",
        # 显式关闭 UTF-8 mode，验证入口不依赖 workflow 或用户环境掩盖问题。
        "PYTHONUTF8": "0",
    }
    hostile = subprocess.run(
        [sys.executable, str(CODEGEN_DIR / "generate.py"), "--check"],
        capture_output=True,
        text=False,
        cwd=str(REPO_ROOT),
        env=hostile_env,
        check=False,
    )
    hostile_stdout = hostile.stdout.decode("utf-8", errors="strict")
    hostile_stderr = hostile.stderr.decode("utf-8", errors="strict")
    assert hostile.returncode == 0, (
        "cp1252:strict 宿主中的 generate.py 必须由入口自行建立 UTF-8："
        f"rc={hostile.returncode}\nstdout={hostile_stdout}\nstderr={hostile_stderr}"
    )
    assert "三语言生成树无漂移" in hostile_stdout, "中文 check 成功消息不得丢失或被转义"
    assert hostile_stderr == "", "无漂移时不应把诊断正文写入 stderr"

    spec = importlib.util.spec_from_file_location("codegen_utf8_failure_probe", CODEGEN_DIR / "generate.py")
    assert spec is not None and spec.loader is not None
    codegen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codegen)

    class _FailingReconfigureStream:
        """模拟 stdout 拒绝 UTF-8 重配置，禁止入口继续输出生成或路径正文。"""

        encoding = "cp1252"

        def __init__(self) -> None:
            self.writes: list[str] = []

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            raise OSError("secret reconfigure detail")

        def write(self, text: str) -> int:
            self.writes.append(text)
            raise AssertionError(f"UTF-8 配置失败后不得输出生成正文：{text!r}")

        def flush(self) -> None:
            return None

    class _AsciiErrorSink:
        """收集稳定失败分类，避免将底层异常或文件路径带到 stderr。"""

        # stdout 负例的配对 stderr 也必须独立满足流合同，不能由自身配置失败掩盖回归。
        encoding = "utf-8"
        errors = "strict"

        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            return None

    failing_stdout = _FailingReconfigureStream()
    error_sink = _AsciiErrorSink()
    with monkeypatch.context() as stream_patch:
        stream_patch.setattr(codegen.sys, "stdout", failing_stdout)
        stream_patch.setattr(codegen.sys, "stderr", error_sink)
        stream_patch.setattr(codegen.sys, "argv", ["generate.py", "--check"])
        assert codegen.main() == 2
    assert failing_stdout.writes == [], "配置失败前不得写出半份生成结果"
    assert error_sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"
    assert error_sink.text.isascii(), "配置失败分类必须保持 ASCII，适配未知 stderr 编码"

    class _ReconfigurableStrictStream:
        """模拟可重配置文本流，只有回读到 UTF-8/strict 后才允许生成器输出正文。"""

        def __init__(self) -> None:
            self.encoding = "cp1252"
            self.errors = "replace"
            self.text = ""

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.errors = errors

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            return None

    class _PlainUtf8StrictStream:
        """模拟无 reconfigure 的普通流；仅 UTF-8/strict 声明可作为生成证据通道。"""

        encoding = "UTF_8"
        errors = "strict"

        def __init__(self) -> None:
            self.text = ""

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            return None

    class _PlainUtf8MissingErrorsStream:
        """模拟缺少 errors 状态的普通流，防止仅凭 UTF-8 名称被错误放行。"""

        encoding = "utf-8"

        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, value: str) -> int:
            self.writes.append(value)
            raise AssertionError(f"缺少 strict 状态时不得输出生成正文：{value!r}")

        def flush(self) -> None:
            return None

    class _PlainCp1252StrictStream:
        """模拟无 reconfigure 的 ANSI 流；即使 errors=strict 也不能把中文生成结果写入。"""

        encoding = "cp1252"
        errors = "strict"

        def __init__(self) -> None:
            self.writes: list[str] = []

        def write(self, value: str) -> int:
            self.writes.append(value)
            raise AssertionError(f"cp1252 流不得输出生成正文：{value!r}")

        def flush(self) -> None:
            return None

    class _RejectedOutputStream:
        """模拟假成功重配置和属性读取异常，验证入口不会泄露生成正文或异常路径。"""

        def __init__(
            self,
            *,
            encoding: str = "utf-8",
            errors: str = "strict",
            failure: str | None = None,
        ) -> None:
            self._encoding = encoding
            self._errors = errors
            self._failure = failure
            self.reconfigure_calls: list[tuple[str, str]] = []
            self.writes: list[str] = []

        @property
        def reconfigure(self) -> object:
            if self._failure == "reconfigure_getter":
                raise OSError("SECRET_PATH: reconfigure getter")
            return self._reconfigure

        def _reconfigure(self, *, encoding: str, errors: str) -> None:
            self.reconfigure_calls.append((encoding, errors))

        @property
        def encoding(self) -> str:
            if self._failure == "encoding_getter":
                raise OSError("SECRET_PATH: encoding getter")
            return self._encoding

        @property
        def errors(self) -> str:
            if self._failure == "errors_getter":
                raise OSError("SECRET_PATH: errors getter")
            return self._errors

        def write(self, value: str) -> int:
            self.writes.append(value)
            raise AssertionError(f"UTF-8 配置未被严格确认时不得输出生成正文：{value!r}")

        def flush(self) -> None:
            return None

    class _RejectedStderr:
        """模拟 stderr 重配置失败或伪成功后仍为 cp1252，收集唯一允许的 ASCII 分类。"""

        encoding = "cp1252"
        errors = "strict"

        def __init__(self, *, failure: str) -> None:
            self.failure = failure
            self.text = ""

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            if self.failure == "call":
                raise OSError("SECRET_PATH: stderr reconfigure")

        def write(self, value: str) -> int:
            self.text += value
            return len(value)

        def flush(self) -> None:
            return None

    def _run_with_streams(stdout: object, stderr: object) -> int:
        """在隔离的 sys 流替身下调用既有 --check 节点，避免新增 pytest 节点。"""
        with monkeypatch.context() as stream_patch:
            stream_patch.setattr(codegen.sys, "stdout", stdout)
            stream_patch.setattr(codegen.sys, "stderr", stderr)
            stream_patch.setattr(codegen.sys, "argv", ["generate.py", "--check"])
            return codegen.main()

    def _assert_fail_closed(stream: _RejectedOutputStream) -> None:
        """统一验收不严格状态和属性异常只能返回 ASCII 分类，且不泄露生成正文。"""
        sink = _AsciiErrorSink()
        assert _run_with_streams(stream, sink) == 2
        assert stream.writes == [], "配置失败前不得写出半份生成结果"
        assert sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"
        assert sink.text.isascii()

    # 可重配置流要回读 UTF-8/strict，防止宿主伪称配置成功却保留 replace/ignore。
    configured_stdout = _ReconfigurableStrictStream()
    configured_stderr = _ReconfigurableStrictStream()
    assert _run_with_streams(configured_stdout, configured_stderr) == 0
    assert configured_stdout.encoding == "utf-8" and configured_stdout.errors == "strict"
    assert "三语言生成树无漂移" in configured_stdout.text

    # StringIO 是纯 Unicode 内存流，可安全作为没有 encoding/errors 的唯一例外。
    unicode_stdout = io.StringIO()
    assert _run_with_streams(unicode_stdout, io.StringIO()) == 0
    assert "三语言生成树无漂移" in unicode_stdout.getvalue()

    # 无 reconfigure 的普通流只有同时声明 UTF-8/strict 才能继续。
    plain_stdout = _PlainUtf8StrictStream()
    assert _run_with_streams(plain_stdout, _PlainUtf8StrictStream()) == 0
    assert "三语言生成树无漂移" in plain_stdout.text

    missing_errors_stdout = _PlainUtf8MissingErrorsStream()
    missing_errors_sink = _AsciiErrorSink()
    assert _run_with_streams(missing_errors_stdout, missing_errors_sink) == 2
    assert missing_errors_stdout.writes == []
    assert missing_errors_sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"

    # 无 reconfigure 的 cp1252+strict 仍不可作为中文生成证据通道，必须 fail-closed。
    plain_cp1252_stdout = _PlainCp1252StrictStream()
    plain_cp1252_sink = _AsciiErrorSink()
    assert _run_with_streams(plain_cp1252_stdout, plain_cp1252_sink) == 2
    assert plain_cp1252_stdout.writes == []
    assert plain_cp1252_sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"

    # reconfigure 即使返回成功，只要回读仍为 cp1252+strict，也必须停止而非输出半份生成正文。
    fake_success_cp1252 = _RejectedOutputStream(encoding="cp1252")
    _assert_fail_closed(fake_success_cp1252)
    assert fake_success_cp1252.reconfigure_calls == [("utf-8", "strict")]

    # stdout 已严格 UTF-8 时，stderr 失败或仍是 cp1252 也必须阻止业务正文，只留 ASCII 分类。
    for stderr_failure in ("call", "invalid"):
        valid_stdout = _ReconfigurableStrictStream()
        rejected_stderr = _RejectedStderr(failure=stderr_failure)
        assert _run_with_streams(valid_stdout, rejected_stderr) == 2
        assert valid_stdout.text == "", "stderr 配置失败前 stdout 不得写出生成正文"
        assert rejected_stderr.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"
        assert rejected_stderr.text.isascii()

    for non_strict_errors in ("replace", "ignore"):
        non_strict_stream = _RejectedOutputStream(errors=non_strict_errors)
        _assert_fail_closed(non_strict_stream)
        assert non_strict_stream.reconfigure_calls == [("utf-8", "strict")]

    for getter_failure in ("reconfigure_getter", "encoding_getter", "errors_getter"):
        _assert_fail_closed(_RejectedOutputStream(failure=getter_failure))


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
