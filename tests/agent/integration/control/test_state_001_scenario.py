"""STATE-001 固定真实场景与 FINAL TestReceipt 集成验收。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from factory_agent.testing.required_test_catalog import DEFAULT_CATALOG_PATH
from factory_agent.testing.verify_receipts import verify


@pytest.mark.asyncio
async def test_state_001_final_receipt_contains_fixed_real_scenario_evidence() -> None:
    """FINAL 只能来自 D 盘固定场景的真实分组执行与 receipt verifier。"""
    try:
        from factory_agent.testing.state_001 import run_state_001_rule_scenario
    except ModuleNotFoundError:
        pytest.fail("STATE-001 fixed scenario runner is missing")

    d_temp_root = Path(r"D:\codex项目\.t")
    d_temp_root.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="state-001-integration-", dir=d_temp_root) as isolated:
        isolated_root = Path(isolated)
        receipt = await run_state_001_rule_scenario(temp_root=isolated_root)
        receipt_path = isolated_root / "state-001-receipt.json"
        receipt_path.write_text(
            json.dumps([receipt.to_dict()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        verification = verify(DEFAULT_CATALOG_PATH, receipt_path)

    assert not isolated_root.exists()
    assert frozenset(receipt.actual) == frozenset(
        {
            "catalogBinding",
            "fiveDimensions",
            "samePlanRevisionMultiRun",
            "logicalNodeReplan",
            "barrierSettledPassed",
            "unknownSettleTimeout",
            "milestoneArtifactAtomicity",
            "successPredicateArtifactAtomicity",
            "fastPauseResume",
            "casCompetition",
        }
    )
    assert receipt.actual["catalogBinding"] == receipt.expected["catalogBinding"]
    for group_name in frozenset(receipt.actual) - {"catalogBinding"}:
        expected_group = receipt.expected[group_name]
        actual_group = receipt.actual[group_name]
        assert actual_group["pytestExitCode"] == expected_group["pytestExitCode"] == 0
        assert actual_group["passedCount"] >= expected_group["minimumPassedCount"]
        assert actual_group["fixedNodeCount"] == expected_group["fixedNodeCount"]
        assert len(actual_group["selectedNodes"]) == actual_group["fixedNodeCount"]
        assert actual_group["selectedNodesDigest"].startswith("sha256:")
        assert actual_group["outputDigest"].startswith("sha256:")
    assert verification.passed, verification.report()
    assert receipt.actions
    assert receipt.sideEffectCount == 0
    assert len(receipt.artifactDigests) == 9
    assert receipt.result == "PASS"
    assert receipt.validate() == []
