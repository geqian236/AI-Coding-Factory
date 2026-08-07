"""
tests/contract/test_spike_binding_shape.py

GPT 第八轮 item 1：两条负例测试，验证 _receipt-validator.ps1 的绑定形态按
spike 名**冻结**，不根据 receipt 内容动态切换分支（防止伪造绕过）。

两个攻击场景：
  A. sqlite_wal_full 提供 runBinding（本应 run_nonce），以错误 probeDigest 绕过 → 应被拒
  B. clock_source 只提供 run_nonce + candidateSha，跳过 runId 检查 → 应被拒

这两个场景在 GPT 第八轮 REVISE 中被 reviewer 实测验证为漏洞；本文件锁死修复后的
行为，防止回退。

测试通过 PowerShell subprocess 调用 Test-SpikeReceiptEvidence 函数（单一真源），
仅在 Windows 上运行（PS 5.1 / pwsh 7 可用）；CI windows-probes job 会执行。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PS1 = REPO_ROOT / "scripts" / "spikes" / "_receipt-validator.ps1"


def _run_validator_check(receipt: dict, spike_name: str, run_nonce: str,
                          expect_candidate: str = "sha256:0" * 40,
                          expect_probe: str = "") -> dict:
    """
    构造临时 receipt 文件，通过 PS subprocess 调用 Test-SpikeReceiptEvidence，
    返回 {ok: bool, status: str, detail: str}。
    """
    tmp = REPO_ROOT / "tests" / "fixtures" / f"_tmp_binding_test_{spike_name}.json"
    try:
        tmp.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        ps_script = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence `
    -Name '{spike_name}' `
    -Path '{tmp}' `
    -EnvCompat $false `
    -Allowlist @() `
    -ExpectRunNonce '{run_nonce}' `
    -ExpectCandidateSha '{expect_candidate}' `
    -ExpectProbeDigest '{expect_probe}'
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=30,
        )
        output = result.stdout.strip()
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in output.split("|") if "=" in p}
        return {
            "ok": parts.get("ok", "").lower() == "true",
            "status": parts.get("status", "UNKNOWN"),
            "detail": parts.get("detail", output),
        }
    finally:
        tmp.unlink(missing_ok=True)


def _make_base_receipt(spike: str, status: str = "PASS") -> dict:
    """返回最小合法 receipt（断言含一条 passed=true）。"""
    return {
        "spike": spike,
        "status": status,
        "assertions": [{"name": "probe_ok", "passed": True, "detail": "test"}],
        "timestamp": "2026-01-01T00:00:00Z",
    }


@pytest.mark.skipif(sys.platform != "win32", reason="PS 5.1 validator tests require Windows")
def test_sqlite_with_runbinding_rejected() -> None:
    """
    攻击场景 A（GPT 第八轮 item 1）：sqlite_wal_full receipt 携带 runBinding（而非
    run_nonce），且 probeDigest 为零（错误）。旧代码根据 receipt 自带字段选分支，
    runBinding 分支不检查 probeDigest → 绑定绕过；修复后代码按 spike 名冻结分支，
    sqlite 必须走 run_nonce 路径，存在 runBinding 即 BINDING_WRONG_SHAPE。
    """
    nonce = "aabbccdd11223344"
    receipt = _make_base_receipt("sqlite_wal_full")
    # 伪造：给 sqlite 加 runBinding，不加 run_nonce
    receipt["runBinding"] = {
        "runId": "00000000-0000-0000-0000-000000000000",
        "runNonce": nonce,
        "candidateSha": "a" * 40,
        "spike": "sqlite_wal_full",
        "stampedBy": "forger",
    }
    result = _run_validator_check(receipt, "sqlite_wal_full", run_nonce=nonce)
    assert not result["ok"], (
        "sqlite_wal_full receipt with runBinding (wrong shape) must be REJECTED; "
        f"got ok=True status={result['status']} detail={result['detail']}"
    )
    assert "WRONG_SHAPE" in result["status"] or "WRONG_SHAPE" in result["detail"], (
        f"Expected BINDING_WRONG_SHAPE; got status={result['status']} detail={result['detail']}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="PS 5.1 validator tests require Windows")
def test_non_sqlite_with_only_run_nonce_rejected() -> None:
    """
    攻击场景 B（GPT 第八轮 item 1）：clock_source receipt 只提供 run_nonce +
    candidateSha（无 runBinding），旧代码路由到 run_nonce 分支而跳过 runId 检查
    → 伪造者不需要知道 runId 即可绕过；修复后代码按 spike 名冻结，非 sqlite 必须
    有 runBinding，只有 run_nonce 即 BINDING_WRONG_SHAPE。
    """
    nonce = "aabbccdd11223344"
    receipt = _make_base_receipt("clock_source")
    # 伪造：给 non-sqlite 加 run_nonce 但不加 runBinding
    receipt["run_nonce"] = nonce
    receipt["candidateSha"] = "a" * 40
    result = _run_validator_check(receipt, "clock_source", run_nonce=nonce)
    assert not result["ok"], (
        "clock_source receipt with only run_nonce (wrong shape) must be REJECTED; "
        f"got ok=True status={result['status']} detail={result['detail']}"
    )
    assert "WRONG_SHAPE" in result["status"] or "WRONG_SHAPE" in result["detail"], (
        f"Expected BINDING_WRONG_SHAPE; got status={result['status']} detail={result['detail']}"
    )
