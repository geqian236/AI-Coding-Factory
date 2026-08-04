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
"""
from __future__ import annotations

import json
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
    env_with_path = {**__import__("os").environ, "PYTHONPATH": agent_src}
    return subprocess.run(
        [
            sys.executable, "-m", "factory_agent.testing.verify_receipts",
            "--catalog", str(catalog),
            "--receipts", str(receipts),
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env_with_path,
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
