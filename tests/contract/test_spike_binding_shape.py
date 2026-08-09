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

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VALIDATOR_PS1 = REPO_ROOT / "scripts" / "spikes" / "_receipt-validator.ps1"
SQLITE_BENCH = REPO_ROOT / "tools" / "compat-probes" / "sqlite_wal_full" / "bench.py"
_TEST_DATA_ROOT_ENV = "FACTORY_TEST_DATA_ROOT"
_APPROVED_D_TEST_PREFIX = Path(r"D:\codex项目")


def _sqlite_probe_temp_root() -> Path:
    """返回 SQLite CLI 回归专用的受控 D 盘临时根。

    Windows 合同测试不得回退到 ambient TEMP（其可能指向 C 盘）。CI 可以通过
    FACTORY_TEST_DATA_ROOT 明确传入受控 DataRoot；本地 worktree 则从祖先目录派生同一
    项目数据根。任何非 D:\\codex项目 前缀都在 mkdir 前失败，避免测试自身掩盖存储合同。
    """
    configured = os.environ.get(_TEST_DATA_ROOT_ENV)
    if configured:
        candidate = Path(configured)
    else:
        candidate = REPO_ROOT.parents[2] / "AI-Coding-Factory-Data" / "dev"

    raw = os.fspath(candidate)
    if not candidate.is_absolute() or candidate.drive.casefold() != "d:":
        raise RuntimeError(f"SQLite 测试临时根必须是 D 盘绝对路径，实际为：{raw}")
    if any(part == ".." for part in candidate.parts):
        raise RuntimeError(f"SQLite 测试临时根不得包含 '..'：{raw}")

    approved = _APPROVED_D_TEST_PREFIX.resolve(strict=False)
    resolved = candidate.resolve(strict=False)
    if resolved != approved and approved not in resolved.parents:
        raise RuntimeError(f"SQLite 测试临时根必须位于 {approved}，实际为：{resolved}")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _assert_sqlite_cli_stdout_is_utf8_under_cp1252() -> None:
    """在非 UTF-8 继承 stdout 下运行真实探针，stdout 仍须是完整 UTF-8 JSON。

    该回归直接覆盖 GitHub windows-probes 发现的边界：receipt 含中文且
    ensure_ascii=False 时，cp1252 默认 stdout 不能编码中文。探针及 pytest 的临时文件
    都显式落在经校验的 D 盘根；--no-default-write 防止修改仓库 receipt。
    """
    temp_root = _sqlite_probe_temp_root()
    with tempfile.TemporaryDirectory(prefix="sqlite-utf8-", dir=temp_root) as td:
        env = dict(os.environ)
        env.update({
            "PYTHONIOENCODING": "cp1252:strict",
            "PYTHONUTF8": "0",
            "TMP": td,
            "TEMP": td,
            "TMPDIR": td,
        })
        result = subprocess.run(
            [sys.executable, str(SQLITE_BENCH), "--no-default-write"],
            capture_output=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=45,
        )

    stdout = result.stdout.decode("utf-8", errors="strict")
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 0, (
        "SQLite probe 在 cp1252 继承 stdout 下必须完成；否则 stdout JSON/receipt 绑定会失效："
        f"rc={result.returncode} stderr={stderr!r}"
    )
    receipt = json.loads(stdout)
    assert receipt["spike"] == "sqlite_wal_full"
    assert "本 sandbox" in stdout, "stdout 必须保留中文 receipt 内容并以 UTF-8 可逆编码"


def _load_sqlite_bench_module() -> ModuleType:
    """以独立模块名加载 bench，供 stdout 被替换时的保护逻辑做无副作用验证。"""
    spec = importlib.util.spec_from_file_location("sqlite_bench_stdout_guard", SQLITE_BENCH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 SQLite bench 模块：{SQLITE_BENCH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_stdout_guard_handles_replacement_and_fails_closed() -> None:
    """被替换的 Unicode 捕获流可用；重配置异常不得静默降级到 cp1252。"""
    bench_module = _load_sqlite_bench_module()
    original_stdout = bench_module.sys.stdout

    class _FailingReconfigureStdout:
        """模拟测试/宿主替换 stdout 后，重配置在运行时失败的边界。"""

        encoding = "cp1252"

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            raise OSError("simulated stdout reconfigure failure")

    try:
        # StringIO 是 Unicode 字符流，不依赖终端编码；保护逻辑应兼容它。
        bench_module.sys.stdout = StringIO()
        bench_module._configure_stdout_utf8()

        # 不能配置为 UTF-8 时必须中止，禁止保留 cp1252 后继续输出半份 receipt。
        bench_module.sys.stdout = _FailingReconfigureStdout()
        with pytest.raises(RuntimeError, match="STDOUT_UTF8"):
            bench_module._configure_stdout_utf8()
    finally:
        bench_module.sys.stdout = original_stdout


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
    # 同一 SQLite receipt 合同节点还锁定 CLI 交付路径：stdout 编码失败会让有效回执
    # 在写出前中断，继发下游 INVALID；保持既有 pytest 节点数量不变。
    _assert_sqlite_cli_stdout_is_utf8_under_cp1252()
    _assert_stdout_guard_handles_replacement_and_fails_closed()


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
