"""
tests/contract/test_scenario_digests.py

Task 15 合同层测试：scenarioContractDigest 真实性 + fail-closed 覆盖门禁 + digest 绑定。

验证内容：
  - required-test-catalog 的 47 个 scenarioContractDigest 全部为真实 sha256（无占位符、全唯一）
  - 每个 digest 可由 scenario_contract_digest() 从条目内容确定性重算（防漂移）
  - verify(require_coverage=True) 对部分回执（1/47）报 MISSING_COVERAGE 而 fail
  - verify(require_coverage=True) 对完整 47/47 PASS 回执通过
  - 回执 scenarioContractDigest 与 catalog 冻结值不一致时报 DIGEST_MISMATCH
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory_agent.testing.required_test_catalog import (
    digest_by_test_id,
    load_catalog,
    scenario_contract_digest,
)
from factory_agent.testing.verify_receipts import verify

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "contracts" / "testing" / "required-test-catalog.v1.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "receipts"
VALID_FIXTURE = FIXTURES_DIR / "valid.json"

_PLACEHOLDER = "placeholder-to-be-filled-by-implementer"


# ─────────────────────── scenarioContractDigest 真实性 ───────────────────────


def test_no_placeholder_digests_remain() -> None:
    """catalog 中不得残留任何占位 scenarioContractDigest。"""
    catalog = load_catalog(CATALOG_PATH)
    placeholders = [
        e["testId"] for e in catalog["tests"] if e["scenarioContractDigest"] == _PLACEHOLDER
    ]
    assert not placeholders, f"仍有占位 digest 的条目: {placeholders}"


def test_all_digests_are_sha256_prefixed() -> None:
    """所有 scenarioContractDigest 必须为 sha256:<64 位十六进制> 形式。"""
    catalog = load_catalog(CATALOG_PATH)
    for entry in catalog["tests"]:
        digest = entry["scenarioContractDigest"]
        assert digest.startswith("sha256:"), f"{entry['testId']}: digest 无 sha256: 前缀"
        hexpart = digest[len("sha256:"):]
        assert len(hexpart) == 64, f"{entry['testId']}: digest 十六进制长度非 64"
        assert all(c in "0123456789abcdef" for c in hexpart), (
            f"{entry['testId']}: digest 含非小写十六进制字符"
        )


def test_all_47_digests_unique() -> None:
    """47 个 scenarioContractDigest 必须互不相同（各绑定不同场景合同）。"""
    catalog = load_catalog(CATALOG_PATH)
    digests = [e["scenarioContractDigest"] for e in catalog["tests"]]
    assert len(digests) == 47
    assert len(set(digests)) == 47, "存在重复 scenarioContractDigest"


def test_digests_are_deterministically_recomputable() -> None:
    """每个存储的 digest 必须能由 scenario_contract_digest() 从条目内容重算一致（防漂移）。"""
    catalog = load_catalog(CATALOG_PATH)
    for entry in catalog["tests"]:
        recomputed = scenario_contract_digest(entry)
        assert recomputed == entry["scenarioContractDigest"], (
            f"{entry['testId']}: 存储 digest 与重算值不一致，场景合同已漂移"
        )


def test_digest_changes_when_scenario_drifts() -> None:
    """场景任一字段改动都必须改变 digest（防篡改绑定）。"""
    catalog = load_catalog(CATALOG_PATH)
    entry = dict(catalog["tests"][0])
    baseline = scenario_contract_digest(entry)
    drifted = dict(entry)
    drifted["scenario"] = entry["scenario"] + "（漂移）"
    assert scenario_contract_digest(drifted) != baseline, "scenario 文本改动未改变 digest"


# ─────────────────────── fail-closed 覆盖门禁 ───────────────────────


def test_require_coverage_rejects_partial_receipts() -> None:
    """require_coverage=True 时，仅 1/47 的回执必须因 MISSING_COVERAGE 失败。"""
    result = verify(CATALOG_PATH, VALID_FIXTURE, require_coverage=True)
    assert not result.passed, "1/47 覆盖不应通过覆盖门禁"
    coverage_errors = [e for e in result.errors if "MISSING_COVERAGE" in e]
    assert coverage_errors, f"缺少 MISSING_COVERAGE 错误。所有错误: {result.errors}"


def test_default_mode_does_not_require_coverage() -> None:
    """默认（require_coverage=False）保持逐回执模式：valid.json 单条应通过。"""
    result = verify(CATALOG_PATH, VALID_FIXTURE)
    assert result.passed, f"默认模式下 valid.json 应通过，错误: {result.errors}"


def test_require_coverage_passes_with_full_47(tmp_path: Path) -> None:
    """require_coverage=True 时，为全部 47 个 testId 各造一条真实 PASS 回执必须通过。

    P0-6：每条回执必须满足 schema 必填项（finalPassOwner/qualification/
    requiredReplays/scenarioContractDigest/非空 expected/actual/至少 1 个 actions）。
    对 requiredReplays>1 的条目生成多条独立 receiptId 通过 INSUFFICIENT_REPLAYS。
    """
    from factory_agent.testing.required_test_catalog import (
        owner_by_test_id,
        replays_by_test_id,
    )

    catalog = load_catalog(CATALOG_PATH)
    digests = digest_by_test_id(catalog)
    owners = owner_by_test_id(catalog)
    replays = replays_by_test_id(catalog)

    receipts = []
    for entry in catalog["tests"]:
        tid = entry["testId"]
        n_replays = replays[tid]
        for i in range(n_replays):
            ordinal = i + 1
            receipts.append({
                "receiptId": f"rcpt-{tid}-{ordinal:02d}",
                "testId": tid,
                "environmentManifestDigest": "sha256:" + "a" * 64,
                "actions": [{
                    "actionId": f"act-{tid}-{ordinal:02d}",
                    "description": f"执行 {tid} 场景重放 #{ordinal} 并观察结果",
                    "executedAt": "2026-08-05T00:00:00Z",
                }],
                "expected": {"observed": True, "testId": tid, "replayOrdinal": ordinal},
                "actual": {"observed": True, "testId": tid, "replayOrdinal": ordinal},
                "sideEffectCount": 0,
                "artifactDigests": ["sha256:" + "b" * 64],
                "result": "PASS",
                "createdAt": "2026-08-05T00:00:00Z",
                "scenarioContractDigest": digests[tid],
                "finalPassOwner": owners[tid],
                "qualification": owners[tid],
                "requiredReplays": n_replays,
            })
    full = tmp_path / "full_coverage.json"
    full.write_text(json.dumps(receipts), encoding="utf-8")
    result = verify(CATALOG_PATH, full, require_coverage=True)
    assert result.passed, f"完整 47/47 覆盖应通过，错误: {result.errors}"


# ─────────────────────── digest 绑定 ───────────────────────


def test_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    """回执 scenarioContractDigest 与 catalog 冻结值不一致时必须报 DIGEST_MISMATCH。"""
    receipt_file = tmp_path / "wrong_digest.json"
    receipt_file.write_text(
        json.dumps([{
            "receiptId": "rcpt-wrong-digest",
            "testId": "PLAN-HASH-001",
            "environmentManifestDigest": "sha256:" + "a" * 64,
            "actions": [{
                "actionId": "act-001",
                "description": "run",
                "executedAt": "2026-08-05T00:00:00Z",
            }],
            "expected": {},
            "actual": {},
            "sideEffectCount": 0,
            "artifactDigests": [],
            "result": "PASS",
            "createdAt": "2026-08-05T00:00:00Z",
            "scenarioContractDigest": "sha256:" + "0" * 64,  # 故意与 catalog 冻结值不符
        }]),
        encoding="utf-8",
    )
    result = verify(CATALOG_PATH, receipt_file)
    assert not result.passed
    mismatch_errors = [e for e in result.errors if "DIGEST_MISMATCH" in e]
    assert mismatch_errors, f"缺少 DIGEST_MISMATCH 错误。所有错误: {result.errors}"


def test_valid_fixture_digest_matches_catalog() -> None:
    """valid.json 携带的 PLAN-HASH-001 digest 必须与 catalog 冻结值一致（绑定不误伤真值）。"""
    catalog = load_catalog(CATALOG_PATH)
    digests = digest_by_test_id(catalog)
    receipts = json.loads(VALID_FIXTURE.read_text(encoding="utf-8"))
    for r in receipts:
        declared = r.get("scenarioContractDigest")
        if declared is not None:
            assert declared == digests[r["testId"]], (
                f"valid.json {r['testId']} 的 digest 与 catalog 不一致"
            )
