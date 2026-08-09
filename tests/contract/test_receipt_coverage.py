"""
tests/contract/test_receipt_coverage.py

Task 6 合同层测试：测试回执覆盖率验证与 fail-closed 规则。

验证内容：
  - required-test-catalog 精确加载 47 个 ID
  - valid.json fixture 通过所有验证
  - missing.json fixture 因缺少 environmentManifestDigest 而失败
  - conflict.json fixture 因同一 testId 结果冲突而失败
  - 未映射 testId 被拒绝
  - 手工文字 PASS（空 actions + result=PASS）被拒绝
  - 未知 runtimeId 被拒绝
  - P0-6：6 个负例 fixture 全 fail closed：
    forged_empty_pass / missing_owner / owner_mismatch /
    insufficient_replays / pass_without_digest / duplicate_receipt_id
  - P0-6：full_coverage_valid.json 正例通过（满足 47 ID + 各 requiredReplays +
    owner 白名单 + 非空 expected/actual + 正确 digest）
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "contracts" / "testing" / "required-test-catalog.v1.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "receipts"
VALID_FIXTURE = FIXTURES_DIR / "valid.json"
MISSING_FIXTURE = FIXTURES_DIR / "missing.json"
CONFLICT_FIXTURE = FIXTURES_DIR / "conflict.json"
FULL_COVERAGE_FIXTURE = FIXTURES_DIR / "full_coverage_valid.json"

# P0-6 负例 fixture：每条都必须 fail-closed（verify passed=False）。
# GPT 第七轮 item 3：load_receipts_from_file 现在对原始 JSON 先做全量 schema 校验，
# 再构造 dataclass。故 schema-invalid 的负例在 **load 门** 就被拒（RECEIPTS_LOAD +
# schema 点名字段），比旧的语义层拒绝更早、更强；schema-valid 但语义违规的负例仍在
# verify 语义层被拒。每条标注捕获层与期望 token，断言据此精确匹配（不放宽）：
#   layer="load"   -> 期望 result.errors 含 RECEIPTS_LOAD 且点名 <token>（schema 字段名）
#   layer="verify" -> 期望 result.errors 含 <token>（语义错误码）
P06_NEGATIVE_FIXTURES = {
    # schema-invalid：expected/actual 空对象违反 minProperties -> load 门拒绝。
    "forged_empty_pass.json": ("load", "expected"),
    # schema-invalid：缺 required finalPassOwner -> load 门拒绝。
    "missing_owner.json": ("load", "finalPassOwner"),
    # schema-invalid：缺 required scenarioContractDigest -> load 门拒绝。
    "pass_without_digest.json": ("load", "scenarioContractDigest"),
    # schema-valid，语义违规：owner 不在白名单 -> verify 语义层拒绝。
    "owner_mismatch.json": ("verify", "UNAUTHORIZED_OWNER"),
    # schema-valid，语义违规：PASS 去重数 < requiredReplays -> verify 覆盖门禁拒绝。
    "insufficient_replays.json": ("verify", "INSUFFICIENT_REPLAYS"),
    # schema-valid，语义违规：receiptId 全局重复 -> verify 语义层拒绝。
    "duplicate_receipt_id.json": ("verify", "DUPLICATE_RECEIPT_ID"),
}

# 精确期望的 47 个测试 ID（同 test_schema_catalog.py 以保持一致性）
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


# ─────────────────────── required_test_catalog 测试 ─────────────────────────


def test_required_test_catalog_loads_47_ids() -> None:
    """required_test_catalog.load_catalog() 必须加载精确 47 个测试 ID。"""
    from factory_agent.testing.required_test_catalog import load_catalog  # type: ignore[import]

    catalog = load_catalog(CATALOG_PATH)
    ids = {entry["testId"] for entry in catalog["tests"]}
    assert len(ids) == 47, f"catalog 应有 47 个 ID，实际 {len(ids)}"


def test_required_test_catalog_matches_expected_ids() -> None:
    """required_test_catalog 中的测试 ID 集合必须与 Master Spec §20.2 精确匹配。"""
    from factory_agent.testing.required_test_catalog import get_test_ids, load_catalog  # type: ignore[import]

    catalog = load_catalog(CATALOG_PATH)
    actual_ids = get_test_ids(catalog)
    assert actual_ids == EXPECTED_TEST_IDS, (
        f"ID 集合不匹配。\n  多出: {actual_ids - EXPECTED_TEST_IDS}\n  缺少: {EXPECTED_TEST_IDS - actual_ids}"
    )


def test_required_test_catalog_rejects_wrong_count(tmp_path: Path) -> None:
    """catalog 条目数不为 47 时 load_catalog 必须抛出 ValueError。"""
    from factory_agent.testing.required_test_catalog import load_catalog  # type: ignore[import]

    bad_catalog = {
        "tests": [
            {
                "testId": "STAGE-001",
                "implementationContributors": ["cp"],
                "finalPassOwner": "rev",
                "requiredReplays": 1,
                "scenarioContractDigest": "ph",
            }
        ]
    }
    bad_path = tmp_path / "bad_catalog.json"
    bad_path.write_text(json.dumps(bad_catalog), encoding="utf-8")
    with pytest.raises(ValueError, match="47"):
        load_catalog(bad_path)


# ─────────────────────── fixture 文件存在性测试 ─────────────────────────────


def test_receipt_fixtures_exist() -> None:
    """valid.json / missing.json / conflict.json 三个 fixture 文件必须存在。"""
    for fixture in (VALID_FIXTURE, MISSING_FIXTURE, CONFLICT_FIXTURE):
        assert fixture.exists(), f"receipt fixture 不存在: {fixture}"


def test_receipt_fixtures_are_valid_json() -> None:
    """每个 fixture 文件必须是合法 JSON 数组。"""
    for fixture in (VALID_FIXTURE, MISSING_FIXTURE, CONFLICT_FIXTURE):
        with fixture.open(encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list), f"{fixture.name} 不是 JSON 数组"


# ─────────────────────── TestReceipt 模型验证 ────────────────────────────────


def test_valid_receipt_passes_model_validation() -> None:
    """valid.json 中的回执必须通过 TestReceipt.validate() 无错误。"""
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]

    receipts = load_receipts_from_file(VALID_FIXTURE)
    assert len(receipts) >= 1, "valid.json 应至少包含一个回执"
    for receipt in receipts:
        errors = receipt.validate()
        assert not errors, f"valid.json 回执 [{receipt.receiptId}] 验证失败: {errors}"


def test_missing_env_digest_fails_at_load() -> None:
    """missing.json 空 environmentManifestDigest（minLength）+ 空 actual（minProperties）
    是 schema-invalid（GPT 第七轮 item 3）：load_receipts_from_file 必须在构造 dataclass
    前对原始 JSON 做全量 schema 校验并抛 ValueError，而非等到 dataclass 归一化后。

    旧行为：from_dict 归一化后 validate() 才报 MISSING_ENV_DIGEST（有损归一化可掩盖
    缺字段/未知字段）。新行为：raw schema 校验在 load 层 fail-closed，错误消息点名字段。
    """
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]

    with pytest.raises(ValueError, match="environmentManifestDigest"):
        load_receipts_from_file(MISSING_FIXTURE)


def test_conflict_fixture_has_two_receipts_for_same_id() -> None:
    """conflict.json 必须包含同一 testId 的两条不同 result 的回执。"""
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]

    receipts = load_receipts_from_file(CONFLICT_FIXTURE)
    by_id: dict[str, list[str]] = {}
    for r in receipts:
        by_id.setdefault(r.testId, []).append(r.result)

    conflicts = {tid: rs for tid, rs in by_id.items() if len(set(rs)) > 1}
    assert conflicts, (
        "conflict.json 应存在同一 testId 不同 result 的冲突，但未发现"
    )


# ─────────────────────── verify_receipts 单元测试 ────────────────────────────


def test_verify_valid_fixture_passes() -> None:
    """verify(catalog, valid.json) 必须返回 passed=True。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, VALID_FIXTURE)
    assert result.passed, f"valid.json 验证失败，错误: {result.errors}"


def test_verify_missing_env_digest_fixture_fails() -> None:
    """verify(catalog, missing.json) 必须 fail-closed。

    GPT 第七轮 item 3：load 层现对原始 JSON 做全量 schema 校验，missing.json 的空
    environmentManifestDigest（minLength）+ 空 actual（minProperties）在 load 层即被
    拒（RECEIPTS_LOAD，点名 environmentManifestDigest），verify() 据此 fail-closed。
    这比旧的「归一化后 validate 报 MISSING_ENV_DIGEST」更强——原始违规不再被有损归一化掩盖。
    """
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, MISSING_FIXTURE)
    assert not result.passed, "missing.json 应验证失败"
    # load 层 schema 报错点名 environmentManifestDigest（RECEIPTS_LOAD 包裹）。
    named = [e for e in result.errors if "environmentManifestDigest" in e]
    assert named, f"错误应点名 environmentManifestDigest。所有错误: {result.errors}"


def test_verify_conflict_fixture_fails() -> None:
    """verify(catalog, conflict.json) 必须因 CONFLICTING_RESULTS 返回 passed=False。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, CONFLICT_FIXTURE)
    assert not result.passed, "conflict.json 应验证失败"
    conflict_errors = [e for e in result.errors if "CONFLICTING_RESULTS" in e]
    assert conflict_errors, f"缺少 CONFLICTING_RESULTS 错误。所有错误: {result.errors}"


def test_verify_rejects_unmapped_test_id(tmp_path: Path) -> None:
    """testId 不在 catalog 中时 verify 必须报告 UNMAPPED_ID 错误。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    # UNMAPPED_ID 是 catalog 语义（testId 不在 catalog），非 schema 约束——回执本身
    # 必须 schema-valid（第七轮 item 3：load 层已对原始 JSON 做全量 schema 校验，
    # 空 expected/actual、缺 owner 等会在 load 层被拒，测不到语义规则）。故内联回执
    # 补全所有 required 字段 + 非空 expected/actual，只让 testId 越界以隔离 UNMAPPED_ID。
    receipt_file = tmp_path / "unknown_id.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-unknown-001",
            "testId": "NONEXISTENT-TEST-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [{"actionId": "act-001", "description": "test action", "executedAt": "2026-08-04T00:00:00Z"}],
            "expected": {"ok": True},
            "actual": {"ok": True},
            "sideEffectCount": 0,
            "artifactDigests": ["sha256:" + "b" * 64],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
            "finalPassOwner": "codex-reviewer",
            "qualification": "FINAL",
            "requiredReplays": 1,
            "scenarioContractDigest": "sha256:" + "0" * 64,
        }]),
        encoding="utf-8",
    )
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    unmapped_errors = [e for e in result.errors if "UNMAPPED_ID" in e]
    assert unmapped_errors, f"缺少 UNMAPPED_ID 错误。所有错误: {result.errors}"


def test_verify_rejects_manual_pass(tmp_path: Path) -> None:
    """空 actions（疑似手工文字 PASS）现在在 load 层就被拒（第七轮 item 3 强化）。

    schema 声明 actions.minItems=1；旧代码靠归一化后 validate() 的 MANUAL_PASS 语义
    检查，而第七轮要求 load_receipts_from_file 先对原始 JSON 做全量 schema 校验——
    空 actions 在构造 dataclass 之前就被 fail-closed，比语义层更早。verify() 捕获
    load 异常并以 RECEIPTS_LOAD 报告，错误必须点名 actions。
    """
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    receipt_file = tmp_path / "manual_pass.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-manual-pass-001",
            "testId": "PLAN-HASH-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [],          # 空 actions → schema minItems=1 违规
            "expected": {"ok": True},
            "actual": {"ok": True},
            "sideEffectCount": 0,
            "artifactDigests": [],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
            "finalPassOwner": "codex-reviewer",
            "qualification": "FINAL",
            "requiredReplays": 1,
            "scenarioContractDigest": "sha256:" + "0" * 64,
        }]),
        encoding="utf-8",
    )
    # load 层 fail-closed：空 actions 违反 schema，抛 ValueError 点名 actions。
    with pytest.raises(ValueError, match="actions"):
        load_receipts_from_file(receipt_file)
    # verify() 捕获为 RECEIPTS_LOAD 并 fail-closed。
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    assert any("RECEIPTS_LOAD" in e and "actions" in e for e in result.errors), (
        f"应因 actions 违反 schema 在 load 层 fail-closed。所有错误: {result.errors}"
    )


def test_verify_rejects_unknown_runtime(tmp_path: Path) -> None:
    """未知 runtimeId 现在在 load 层就被拒（第七轮 item 3 强化）。

    runtimeId enum 是 schema 单一真源（KNOWN_RUNTIME_IDS 由 test 机械绑定）；第七轮
    要求 load 先对原始 JSON 做全量 schema 校验，故越界 runtimeId 在构造 dataclass 前
    即 fail-closed，比 validate() 的 UNKNOWN_RUNTIME 语义检查更早。其余字段全部合规，
    使唯一违规为 runtimeId。
    """
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    receipt_file = tmp_path / "unknown_runtime.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-unknown-rt-001",
            "testId": "PLAN-HASH-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [{"actionId": "act-001", "description": "run test", "executedAt": "2026-08-04T00:00:00Z"}],
            "expected": {"ok": True},
            "actual": {"ok": True},
            "sideEffectCount": 0,
            "artifactDigests": ["sha256:" + "b" * 64],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
            "finalPassOwner": "codex-reviewer",
            "qualification": "FINAL",
            "requiredReplays": 1,
            "scenarioContractDigest": "sha256:" + "0" * 64,
            "runtimeId": "unknown-runtime-x99",   # enum 越界 → schema 违规
        }]),
        encoding="utf-8",
    )
    # load 层 fail-closed：runtimeId 不在 enum，抛 ValueError 点名 runtimeId。
    with pytest.raises(ValueError, match="runtimeId"):
        load_receipts_from_file(receipt_file)
    # verify() 捕获为 RECEIPTS_LOAD 并 fail-closed。
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    assert any("RECEIPTS_LOAD" in e and "runtimeId" in e for e in result.errors), (
        f"应因 runtimeId 违反 schema enum 在 load 层 fail-closed。所有错误: {result.errors}"
    )


# ─────────────────────── CLI 集成测试（subprocess）────────────────────────────


def _run_verify_cli(catalog: Path, receipts: Path) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """调用 verify_receipts CLI 并返回 CompletedProcess。"""
    agent_src = str(REPO_ROOT / "apps" / "agent" / "src")
    env_with_path = {**os.environ, "PYTHONPATH": agent_src}
    # 必须显式指定 encoding：Windows 上 text=True 默认走 locale 编码（GBK），
    # 子进程输出的 UTF-8 中文会让读取线程抛 UnicodeDecodeError，
    # 导致断言失败时 stdout/stderr 全部丢失、无法排查。
    return subprocess.run(
        [
            sys.executable, "-m", "factory_agent.testing.verify_receipts",
            "--catalog", str(catalog),
            "--receipts", str(receipts),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(REPO_ROOT),
        env=env_with_path,
        check=False,
    )


def _run_verify_cli_cp1252(catalog: Path, receipts: Path) -> subprocess.CompletedProcess[bytes]:
    """以 cp1252:strict 启动真实 CLI，并保留原始字节供 UTF-8 严格解码验收。"""
    agent_src = str(REPO_ROOT / "apps" / "agent" / "src")
    env_with_path = {
        **os.environ,
        "PYTHONPATH": agent_src,
        "PYTHONIOENCODING": "cp1252:strict",
        # 显式关闭 UTF-8 mode，避免宿主默认值掩盖 CLI 自身的 stdout/stderr 配置责任。
        "PYTHONUTF8": "0",
    }
    return subprocess.run(
        [
            sys.executable, "-m", "factory_agent.testing.verify_receipts",
            "--catalog", str(catalog),
            "--receipts", str(receipts),
        ],
        capture_output=True,
        text=False,
        cwd=str(REPO_ROOT),
        env=env_with_path,
        check=False,
    )


def test_cli_valid_fixture_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 对有效回执在默认和 cp1252:strict 宿主均须输出完整 UTF-8，并在配置失败时 fail-closed。"""
    proc = _run_verify_cli(CATALOG_PATH, VALID_FIXTURE)
    assert proc.returncode == 0, (
        f"CLI 对 valid.json 应退出码 0，实际 {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    assert "所有回执验证通过" in proc.stdout

    hostile_proc = _run_verify_cli_cp1252(CATALOG_PATH, VALID_FIXTURE)
    hostile_stdout = hostile_proc.stdout.decode("utf-8", errors="strict")
    hostile_stderr = hostile_proc.stderr.decode("utf-8", errors="strict")
    assert hostile_proc.returncode == 0, (
        "cp1252:strict 宿主中的 CLI 仍必须由入口自行改为 UTF-8："
        f"rc={hostile_proc.returncode}\nstdout={hostile_stdout}\nstderr={hostile_stderr}"
    )
    assert "所有回执验证通过" in hostile_stdout, "中文 PASS 报告不得丢失或被转义"
    assert hostile_stderr == "", "有效回执不应把诊断正文写入 stderr"

    from factory_agent.testing import verify_receipts

    class _FailingReconfigureStream:
        """模拟宿主拒绝 reconfigure；若 CLI 提前写正文，测试立即失败。"""

        encoding = "cp1252"

        def __init__(self) -> None:
            self.writes: list[str] = []

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            raise OSError("secret reconfigure detail")

        def write(self, text: str) -> int:
            self.writes.append(text)
            raise AssertionError(f"UTF-8 配置失败后不得输出回执正文：{text!r}")

        def flush(self) -> None:
            return None

    class _AsciiErrorSink:
        """收集 fail-closed 分类，验证不泄露路径、回执或底层异常。"""

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
        stream_patch.setattr(verify_receipts.sys, "stdout", failing_stdout)
        stream_patch.setattr(verify_receipts.sys, "stderr", error_sink)
        assert verify_receipts.main(["--catalog", str(CATALOG_PATH), "--receipts", str(VALID_FIXTURE)]) == 2
    assert failing_stdout.writes == [], "配置失败前不得写出半份报告"
    assert error_sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"
    assert error_sink.text.isascii(), "配置失败分类必须保持 ASCII，适配未知 stderr 编码"

    class _ReconfigurableStrictStream:
        """模拟可重配置文本流，只有回读到 UTF-8/strict 后才允许 CLI 输出正文。"""

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
        """模拟无 reconfigure 的普通流；声明 UTF-8/strict 时才是可接受的证据通道。"""

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
            raise AssertionError(f"缺少 strict 状态时不得输出回执正文：{value!r}")

        def flush(self) -> None:
            return None

    class _RejectedOutputStream:
        """模拟假成功重配置和属性访问异常，任何一类都不得继续输出回执正文。"""

        def __init__(self, *, errors: str = "strict", failure: str | None = None) -> None:
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
            return "utf-8"

        @property
        def errors(self) -> str:
            if self._failure == "errors_getter":
                raise OSError("SECRET_PATH: errors getter")
            return self._errors

        def write(self, value: str) -> int:
            self.writes.append(value)
            raise AssertionError(f"UTF-8 配置未被严格确认时不得输出回执正文：{value!r}")

        def flush(self) -> None:
            return None

    def _run_with_streams(stdout: object, stderr: object) -> int:
        """在隔离的 sys 流替身下调用既有 CLI 节点，避免新增 pytest 节点。"""
        with monkeypatch.context() as stream_patch:
            stream_patch.setattr(verify_receipts.sys, "stdout", stdout)
            stream_patch.setattr(verify_receipts.sys, "stderr", stderr)
            return verify_receipts.main(["--catalog", str(CATALOG_PATH), "--receipts", str(VALID_FIXTURE)])

    def _assert_fail_closed(stream: _RejectedOutputStream) -> None:
        """统一验收属性异常或非 strict 状态只返回 ASCII 分类，且不泄露正文。"""
        sink = _AsciiErrorSink()
        assert _run_with_streams(stream, sink) == 2
        assert stream.writes == [], "配置失败前不得写出半份回执"
        assert sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"
        assert sink.text.isascii()

    # 可重配置流必须回读到 UTF-8/strict；这是正常 TextIOWrapper 的等价合同。
    configured_stdout = _ReconfigurableStrictStream()
    configured_stderr = _ReconfigurableStrictStream()
    assert _run_with_streams(configured_stdout, configured_stderr) == 0
    assert configured_stdout.encoding == "utf-8" and configured_stdout.errors == "strict"
    assert "所有回执验证通过" in configured_stdout.text

    # StringIO 是纯 Unicode 内存流，可安全作为没有 encoding/errors 的唯一例外。
    unicode_stdout = io.StringIO()
    assert _run_with_streams(unicode_stdout, io.StringIO()) == 0
    assert "所有回执验证通过" in unicode_stdout.getvalue()

    # 无 reconfigure 的普通流不能靠 UTF-8 名称猜测，必须同时声明 strict。
    plain_stdout = _PlainUtf8StrictStream()
    assert _run_with_streams(plain_stdout, _PlainUtf8StrictStream()) == 0
    assert "所有回执验证通过" in plain_stdout.text

    missing_errors_stdout = _PlainUtf8MissingErrorsStream()
    missing_errors_sink = _AsciiErrorSink()
    assert _run_with_streams(missing_errors_stdout, missing_errors_sink) == 2
    assert missing_errors_stdout.writes == []
    assert missing_errors_sink.text == "CLI_TEXT_UTF8_CONFIGURATION_FAILED\n"

    for non_strict_errors in ("replace", "ignore"):
        non_strict_stream = _RejectedOutputStream(errors=non_strict_errors)
        _assert_fail_closed(non_strict_stream)
        assert non_strict_stream.reconfigure_calls == [("utf-8", "strict")]

    for getter_failure in ("reconfigure_getter", "encoding_getter", "errors_getter"):
        _assert_fail_closed(_RejectedOutputStream(failure=getter_failure))


def test_cli_missing_fixture_exits_nonzero() -> None:
    """CLI --receipts missing.json 必须退出码非零。"""
    proc = _run_verify_cli(CATALOG_PATH, MISSING_FIXTURE)
    assert proc.returncode != 0, (
        f"CLI 对 missing.json 应退出码非零，实际 {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


def test_cli_conflict_fixture_exits_nonzero() -> None:
    """CLI --receipts conflict.json 必须退出码非零。"""
    proc = _run_verify_cli(CATALOG_PATH, CONFLICT_FIXTURE)
    assert proc.returncode != 0, (
        f"CLI 对 conflict.json 应退出码非零，实际 {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


# ─────────────────────── P0-6 负例 fixture fail-closed 测试 ──────────────────


def test_p06_negative_fixtures_exist() -> None:
    """6 个 P0-6 负例 fixture 必须全部存在。"""
    for name in P06_NEGATIVE_FIXTURES:
        fixture = FIXTURES_DIR / name
        assert fixture.exists(), f"P0-6 负例 fixture 缺失: {fixture}"


def test_p06_positive_fixture_exists() -> None:
    """P0-6 正例 fixture full_coverage_valid.json 必须存在。"""
    assert FULL_COVERAGE_FIXTURE.exists(), f"P0-6 正例 fixture 缺失: {FULL_COVERAGE_FIXTURE}"


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    list(P06_NEGATIVE_FIXTURES.items()),
)
def test_p06_negative_fixture_fails_closed(
    fixture_name: str, expected: tuple[str, str]
) -> None:
    """每个 P0-6 负例 fixture 必须 fail closed，且在预期层触发预期错误。

    GPT 第七轮 item 3：schema-invalid 负例在 load 门被拒（RECEIPTS_LOAD + schema
    点名字段）；schema-valid 但语义违规的负例在 verify 语义层被拒。断言精确匹配层
    与 token，不放宽（load 门是比语义层更早、更强的 fail-closed，不是绕过）。
    """
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    layer, token = expected
    fixture_path = FIXTURES_DIR / fixture_name
    # insufficient_replays.json 需要 require_coverage=True 才能触发 INSUFFICIENT_REPLAYS
    require_coverage = fixture_name == "insufficient_replays.json"
    result = verify(CATALOG_PATH, fixture_path, require_coverage=require_coverage)
    assert not result.passed, (
        f"{fixture_name} 应验证失败（fail closed），但实际通过"
    )
    if layer == "load":
        # schema-invalid：verify 的 load 步骤捕获 ValueError 记为 RECEIPTS_LOAD，
        # 且 schema 错误必须点名该缺失/空字段。
        matched = [
            e for e in result.errors
            if "RECEIPTS_LOAD" in e and token in e
        ]
        assert matched, (
            f"{fixture_name} 应在 load 门以 RECEIPTS_LOAD 拒绝且点名 '{token}'，"
            f"但未发现。所有错误: {result.errors}"
        )
    else:
        matched = [e for e in result.errors if token in e]
        assert matched, (
            f"{fixture_name} 应在 verify 语义层触发 {token} 错误，但未发现。"
            f"所有错误: {result.errors}"
        )


def test_p06_positive_fixture_passes_full_coverage() -> None:
    """full_coverage_valid.json 在 require_coverage=True 下必须通过——满足全部 47 ID +
    各 requiredReplays + owner 白名单 + 非空 expected/actual + 正确 digest。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, FULL_COVERAGE_FIXTURE, require_coverage=True)
    assert result.passed, f"full_coverage_valid.json 应通过，错误: {result.errors}"


def test_p06_positive_fixture_passes_default_mode() -> None:
    """full_coverage_valid.json 在默认（无 require_coverage）下也必须通过——逐回执校验。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, FULL_COVERAGE_FIXTURE)
    assert result.passed, f"full_coverage_valid.json 应通过，错误: {result.errors}"


def test_p06_full_coverage_has_47_unique_ids() -> None:
    """full_coverage_valid.json 必须覆盖全部 47 个唯一 testId。"""
    with FULL_COVERAGE_FIXTURE.open(encoding="utf-8") as f:
        data = json.load(f)
    ids = {r["testId"] for r in data}
    assert ids == EXPECTED_TEST_IDS, (
        f"full_coverage_valid.json ID 集合不完整。\n"
        f"  多出: {ids - EXPECTED_TEST_IDS}\n"
        f"  缺少: {EXPECTED_TEST_IDS - ids}"
    )


def test_p06_full_coverage_has_unique_receipt_ids() -> None:
    """full_coverage_valid.json 内所有 receiptId 必须唯一（与 DUPLICATE_RECEIPT_ID 规则一致）。"""
    with FULL_COVERAGE_FIXTURE.open(encoding="utf-8") as f:
        data = json.load(f)
    rids = [r["receiptId"] for r in data]
    assert len(rids) == len(set(rids)), (
        f"full_coverage_valid.json 内存在重复 receiptId: "
        f"{[r for r in rids if rids.count(r) > 1]}"
    )


def test_p06_full_coverage_replay_counts_meet_catalog() -> None:
    """full_coverage_valid.json 每个 testId 的去重 receiptId 数必须 >= catalog.requiredReplays。"""
    from factory_agent.testing.required_test_catalog import (  # type: ignore[import]
        load_catalog,
        replays_by_test_id,
    )

    catalog = load_catalog(CATALOG_PATH)
    replays = replays_by_test_id(catalog)
    with FULL_COVERAGE_FIXTURE.open(encoding="utf-8") as f:
        data = json.load(f)
    counts: dict[str, set[str]] = {}
    for r in data:
        counts.setdefault(r["testId"], set()).add(r["receiptId"])
    for tid, required in replays.items():
        actual = len(counts.get(tid, set()))
        assert actual >= required, (
            f"{tid}: actual={actual} < required={required}"
        )


# ─────────────────────── P0-6 单元校验：catalog helper ──────────────────────


def test_p06_catalog_helpers_match_entries() -> None:
    """owner_by_test_id / replays_by_test_id 必须与 catalog 条目内容一致。"""
    from factory_agent.testing.required_test_catalog import (  # type: ignore[import]
        load_catalog,
        owner_by_test_id,
        replays_by_test_id,
    )

    catalog = load_catalog(CATALOG_PATH)
    owners = owner_by_test_id(catalog)
    replays = replays_by_test_id(catalog)
    assert len(owners) == 47
    assert len(replays) == 47
    for entry in catalog["tests"]:
        tid = entry["testId"]
        assert owners[tid] == entry["finalPassOwner"], f"{tid}: owner 映射不一致"
        assert replays[tid] == entry["requiredReplays"], f"{tid}: replays 映射不一致"


# ─────────────────────── P0-6 TestReceipt 字段级校验 ─────────────────────────


def test_p06_receipt_validate_flags_empty_expected() -> None:
    """result=PASS + 空 expected 必须触发 EMPTY_EXPECTED。"""
    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    rcpt = TestReceipt(
        receiptId="x",
        testId="PLAN-HASH-001",
        environmentManifestDigest="sha256:" + "a" * 64,
        actions=[],
        expected={},
        actual={"ok": True},
        sideEffectCount=0,
        artifactDigests=[],
        result="PASS",
        createdAt="2026-08-05T00:00:00Z",
        scenarioContractDigest="sha256:" + "0" * 64,
        finalPassOwner="codex-reviewer",
        qualification="codex-reviewer",
        requiredReplays=1,
    )
    errors = rcpt.validate()
    assert any("EMPTY_EXPECTED" in e for e in errors), f"缺 EMPTY_EXPECTED: {errors}"


def test_p06_receipt_validate_flags_empty_actual() -> None:
    """result=PASS + 空 actual 必须触发 EMPTY_ACTUAL。"""
    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    rcpt = TestReceipt(
        receiptId="x",
        testId="PLAN-HASH-001",
        environmentManifestDigest="sha256:" + "a" * 64,
        actions=[],
        expected={"ok": True},
        actual={},
        sideEffectCount=0,
        artifactDigests=[],
        result="PASS",
        createdAt="2026-08-05T00:00:00Z",
        scenarioContractDigest="sha256:" + "0" * 64,
        finalPassOwner="codex-reviewer",
        qualification="codex-reviewer",
        requiredReplays=1,
    )
    errors = rcpt.validate()
    assert any("EMPTY_ACTUAL" in e for e in errors), f"缺 EMPTY_ACTUAL: {errors}"


def test_p06_receipt_validate_flags_missing_owner() -> None:
    """finalPassOwner 缺失必须触发 MISSING_OWNER。"""
    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    rcpt = TestReceipt(
        receiptId="x",
        testId="PLAN-HASH-001",
        environmentManifestDigest="sha256:" + "a" * 64,
        actions=[],
        expected={"ok": True},
        actual={"ok": True},
        sideEffectCount=0,
        artifactDigests=[],
        result="PASS",
        createdAt="2026-08-05T00:00:00Z",
        scenarioContractDigest="sha256:" + "0" * 64,
        finalPassOwner=None,
        qualification="codex-reviewer",
        requiredReplays=1,
    )
    errors = rcpt.validate()
    assert any("MISSING_OWNER" in e for e in errors), f"缺 MISSING_OWNER: {errors}"


def test_p06_receipt_validate_flags_missing_replays() -> None:
    """requiredReplays None 必须触发 MISSING_REPLAYS。"""
    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    rcpt = TestReceipt(
        receiptId="x",
        testId="PLAN-HASH-001",
        environmentManifestDigest="sha256:" + "a" * 64,
        actions=[],
        expected={"ok": True},
        actual={"ok": True},
        sideEffectCount=0,
        artifactDigests=[],
        result="PASS",
        createdAt="2026-08-05T00:00:00Z",
        scenarioContractDigest="sha256:" + "0" * 64,
        finalPassOwner="codex-reviewer",
        qualification="codex-reviewer",
        requiredReplays=None,
    )
    errors = rcpt.validate()
    assert any("MISSING_REPLAYS" in e for e in errors), f"缺 MISSING_REPLAYS: {errors}"


# ─────────────── TestReceipt schema ↔ dataclass 单一真源一致性 ───────────────
# V2（第四轮 REVISE item 2）：test-receipt.v1 从 codegen 移除（codegen=false），
# 消除「生成 TypedDict + 手写 dataclass」双源。schema 为字段与运行时白名单的
# 单一真源；dataclass 是唯一 Python 表示。以下测试机械绑定二者，杜绝漂移。

TEST_RECEIPT_SCHEMA_PATH = (
    REPO_ROOT / "contracts" / "schemas" / "test-receipt.v1.schema.json"
)


def _load_test_receipt_schema() -> dict:
    """加载 test-receipt.v1 schema。"""
    with TEST_RECEIPT_SCHEMA_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def test_test_receipt_not_in_codegen_catalog() -> None:
    """test-receipt.v1 必须 codegen=false（单一真源：dataclass，非生成 TypedDict）。"""
    catalog_path = REPO_ROOT / "contracts" / "codegen" / "catalog.v1.json"
    with catalog_path.open(encoding="utf-8") as f:
        catalog = json.load(f)
    entry = next(
        (s for s in catalog["schemas"] if s.get("schemaId") == "test-receipt.v1"),
        None,
    )
    assert entry is not None, "catalog 缺 test-receipt.v1 条目"
    assert entry["codegen"] is False, (
        "test-receipt.v1 必须 codegen=false，否则生成 TypedDict 与手写 dataclass 双源"
    )
    assert entry["category"] == "runtime-only"


def test_test_receipt_not_in_generated_models() -> None:
    """生成的 models.py 不得再含 TestReceipt（已移出 codegen，避免双源）。"""
    models_path = (
        REPO_ROOT / "apps" / "agent" / "src" / "factory_agent"
        / "contracts" / "generated" / "models.py"
    )
    text = models_path.read_text(encoding="utf-8")
    assert "class TestReceipt" not in text, (
        "生成物仍含 TestReceipt TypedDict，与手写 dataclass 构成双源；"
        "应重新运行 generate.py"
    )


def test_dataclass_fields_match_schema_properties() -> None:
    """dataclass 字段集必须与 schema properties 完全一致（双向，无漂移）。"""
    from dataclasses import fields

    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    schema = _load_test_receipt_schema()
    schema_props = set(schema["properties"].keys())
    dataclass_fields = {f.name for f in fields(TestReceipt)}

    missing_in_dataclass = schema_props - dataclass_fields
    extra_in_dataclass = dataclass_fields - schema_props
    assert not missing_in_dataclass, (
        f"schema 有但 dataclass 缺的字段: {sorted(missing_in_dataclass)}"
    )
    assert not extra_in_dataclass, (
        f"dataclass 有但 schema 缺的字段（additionalProperties:false 会拒绝）: "
        f"{sorted(extra_in_dataclass)}"
    )


def test_schema_required_subset_of_dataclass() -> None:
    """schema required 字段必须都是 dataclass 字段（否则反序列化必失败）。"""
    from dataclasses import fields

    from factory_agent.testing.receipts import TestReceipt  # type: ignore[import]

    schema = _load_test_receipt_schema()
    required = set(schema["required"])
    dataclass_fields = {f.name for f in fields(TestReceipt)}
    missing = required - dataclass_fields
    assert not missing, f"schema required 但 dataclass 缺: {sorted(missing)}"


def test_known_runtime_ids_match_schema_enum() -> None:
    """KNOWN_RUNTIME_IDS 必须与 schema runtimeId enum 完全一致（运行时白名单单一真源）。"""
    from factory_agent.testing.receipts import KNOWN_RUNTIME_IDS  # type: ignore[import]

    schema = _load_test_receipt_schema()
    schema_enum = set(schema["properties"]["runtimeId"]["enum"])
    assert set(KNOWN_RUNTIME_IDS) == schema_enum, (
        f"KNOWN_RUNTIME_IDS 与 schema runtimeId enum 漂移："
        f"仅代码有 {set(KNOWN_RUNTIME_IDS) - schema_enum}，"
        f"仅 schema 有 {schema_enum - set(KNOWN_RUNTIME_IDS)}"
    )
