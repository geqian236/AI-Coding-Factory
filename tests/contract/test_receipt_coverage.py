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

# P0-6 负例 fixture：每条都应在 verify() 中以非零退出 / passed=False 失败
P06_NEGATIVE_FIXTURES = {
    "forged_empty_pass.json": "EMPTY_EXPECTED/EMPTY_ACTUAL",
    "missing_owner.json": "MISSING_OWNER",
    "owner_mismatch.json": "UNAUTHORIZED_OWNER",
    "insufficient_replays.json": "INSUFFICIENT_REPLAYS",
    "pass_without_digest.json": "MISSING_DIGEST",
    "duplicate_receipt_id.json": "DUPLICATE_RECEIPT_ID",
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


def test_missing_env_digest_fails_model_validation() -> None:
    """missing.json 中的回执必须因 MISSING_ENV_DIGEST 而失败。"""
    from factory_agent.testing.receipts import load_receipts_from_file  # type: ignore[import]

    receipts = load_receipts_from_file(MISSING_FIXTURE)
    assert len(receipts) >= 1, "missing.json 应至少包含一个回执"
    all_errors: list[str] = []
    for receipt in receipts:
        all_errors.extend(receipt.validate())
    env_errors = [e for e in all_errors if "MISSING_ENV_DIGEST" in e]
    assert env_errors, (
        f"missing.json 应产生 MISSING_ENV_DIGEST 错误，但未发现。所有错误: {all_errors}"
    )


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
    """verify(catalog, missing.json) 必须因 MISSING_ENV_DIGEST 返回 passed=False。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    result = verify(CATALOG_PATH, MISSING_FIXTURE)
    assert not result.passed, "missing.json 应验证失败"
    env_errors = [e for e in result.errors if "MISSING_ENV_DIGEST" in e]
    assert env_errors, f"缺少 MISSING_ENV_DIGEST 错误。所有错误: {result.errors}"


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

    receipt_file = tmp_path / "unknown_id.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-unknown-001",
            "testId": "NONEXISTENT-TEST-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [{"actionId": "act-001", "description": "test action", "executedAt": "2026-08-04T00:00:00Z"}],
            "expected": {},
            "actual": {},
            "sideEffectCount": 0,
            "artifactDigests": ["sha256:" + "b" * 64],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
        }]),
        encoding="utf-8",
    )
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    unmapped_errors = [e for e in result.errors if "UNMAPPED_ID" in e]
    assert unmapped_errors, f"缺少 UNMAPPED_ID 错误。所有错误: {result.errors}"


def test_verify_rejects_manual_pass(tmp_path: Path) -> None:
    """result=PASS 但 actions 为空时必须报告 MANUAL_PASS 错误。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    receipt_file = tmp_path / "manual_pass.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-manual-pass-001",
            "testId": "PLAN-HASH-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [],          # 空 actions → 疑似手工文字 PASS
            "expected": {},
            "actual": {},
            "sideEffectCount": 0,
            "artifactDigests": [],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
        }]),
        encoding="utf-8",
    )
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    manual_errors = [e for e in result.errors if "MANUAL_PASS" in e]
    assert manual_errors, f"缺少 MANUAL_PASS 错误。所有错误: {result.errors}"


def test_verify_rejects_unknown_runtime(tmp_path: Path) -> None:
    """runtimeId 不在已知列表时必须报告 UNKNOWN_RUNTIME 错误。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    receipt_file = tmp_path / "unknown_runtime.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-unknown-rt-001",
            "testId": "PLAN-HASH-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [{"actionId": "act-001", "description": "run test", "executedAt": "2026-08-04T00:00:00Z"}],
            "expected": {},
            "actual": {},
            "sideEffectCount": 0,
            "artifactDigests": ["sha256:" + "b" * 64],
            "result": "PASS",
            "createdAt": "2026-08-04T00:00:00Z",
            "runtimeId": "unknown-runtime-x99",   # 未知运行时
        }]),
        encoding="utf-8",
    )
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    rt_errors = [e for e in result.errors if "UNKNOWN_RUNTIME" in e]
    assert rt_errors, f"缺少 UNKNOWN_RUNTIME 错误。所有错误: {result.errors}"


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


def test_cli_valid_fixture_exits_zero() -> None:
    """CLI --receipts valid.json 必须退出码为 0。"""
    proc = _run_verify_cli(CATALOG_PATH, VALID_FIXTURE)
    assert proc.returncode == 0, (
        f"CLI 对 valid.json 应退出码 0，实际 {proc.returncode}\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )


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
    ("fixture_name", "expected_error_token"),
    list(P06_NEGATIVE_FIXTURES.items()),
)
def test_p06_negative_fixture_fails_closed(
    fixture_name: str, expected_error_token: str
) -> None:
    """每个 P0-6 负例 fixture 在 verify() 中必须 fail closed 且触发预期错误码。"""
    from factory_agent.testing.verify_receipts import verify  # type: ignore[import]

    fixture_path = FIXTURES_DIR / fixture_name
    # insufficient_replays.json 需要 require_coverage=True 才能触发 INSUFFICIENT_REPLAYS
    require_coverage = fixture_name == "insufficient_replays.json"
    result = verify(CATALOG_PATH, fixture_path, require_coverage=require_coverage)
    assert not result.passed, (
        f"{fixture_name} 应验证失败（fail closed），但实际通过"
    )
    matched = [e for e in result.errors if expected_error_token.split("/")[0] in e]
    assert matched, (
        f"{fixture_name} 应触发 {expected_error_token} 错误，但未发现。"
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
