r"""
tests/contract/test_python_pin_probe.py

GPT 第十二轮 P2：回执 pythonVersion 字段抗 uv warning 污染的回归测试。

第十二轮根因：phase0-acceptance.ps1 的 B-2 探针与 check.ps1 的版本探针都用 `2>&1`
把 stderr 合并进 stdout，再"取首行"。当父环境注入不匹配的 VIRTUAL_ENV 时，uv 向
stderr 打印 `warning: VIRTUAL_ENV=... will be ignored`，该 warning 被合并且落在版本行
之前 → 旧"取首行"把 warning 写进 receipt.environment.pythonVersion，破坏机器可读回执
（exit-code 断言仍有效，故不是假 PASS，但回执字段被污染）。

修复：探针 stderr 分离（`2>$null`，只留纯 stdout 版本行）+ 严格解析唯一的
`^\d+\.\d+\.\d+$`（缺失/多条/格式异常均 fail-closed）。本测试锁死该行为不回退。

设计：核心测试在 PowerShell 内跑与 runner/门禁**逐字相同**的提取管线（`2>$null` +
严格解析），只回传 ASCII 字段（EXIT/COUNT/VER），断言在**注入敌意 VIRTUAL_ENV** 下
提取值恰为 `3.12.10`——这正是写进 receipt.environment.pythonVersion 的值。第二个测试
以 Python 分离管道**独立复现**漏洞：证明敌意 VIRTUAL_ENV 的 warning 确实落在 stderr、
若并入 stdout 首行就会污染，而 stderr 分离后 stdout 恰为纯版本。

仅在 Windows 上运行（PS 5.1 / pwsh 7 可用）；CI windows-probes job 会执行。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# 与 phase0-acceptance.ps1 B-2 / check.ps1 完全相同的探针 Python（单一真源，逐字一致）。
PROBE_PY = (
    "import sys; print('%d.%d.%d' % sys.version_info[:3]); "
    "sys.exit(0 if sys.version_info[:2]==(3,12) else 3)"
)
# 敌意 VIRTUAL_ENV：指向不存在的路径，触发 uv 的 "will be ignored" stderr warning。
HOSTILE_VENV = r"D:\codex项目\nonexistent-hostile-venv"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def _run_ps(script: str) -> subprocess.CompletedProcess[str]:
    """跑一段 PowerShell，stdout/stderr 分别由 Python 独立捕获（不在 PS 内做 2>&1）。"""
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=120,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="uv-locked python 探针需 Windows PowerShell")
def test_python_version_field_strict_312_under_hostile_virtualenv() -> None:
    """核心断言（GPT 第十二轮 P2 修法 1+2）：敌意 VIRTUAL_ENV 下，PS 内跑与 runner/门禁
    **逐字相同**的探针命令并 `2>$null` 分离 stderr（这是本轮的实际修法），只回收纯 stdout；
    再用与 PS 脚本**字节相同**的正则 ^\\d+\\.\\d+\\.\\d+$ 严格解析，断言恰得一条 3.12.10——
    这正是写入 receipt.environment.pythonVersion 的值。

    只在 PS 内做"探针 + stderr 分离"这一段（test 2 已证其可稳定跑通），严格解析放到
    Python 用同一正则复刻：既避免多语句内联 PS 的脆弱转义（`r?`n / 嵌套引号），又忠实
    覆盖修法两部分——stderr 分离由 PS 的 2>$null 真实执行，严格解析由同源正则验证。
    """
    script = (
        f'$env:UV_PYTHON="3.12"; '
        f'$env:VIRTUAL_ENV="{HOSTILE_VENV}"; '
        f'& uv run --locked python -c "{PROBE_PY}" 2>$null'
    )
    proc = _run_ps(script)  # stderr 已在 PS 内 2>$null 丢弃；stdout 应为纯版本行
    # 与 phase0-acceptance.ps1 B-2 / check.ps1 字节相同的严格解析：恰一条 ^\d+\.\d+\.\d+$。
    versions = [ln.strip() for ln in proc.stdout.splitlines() if VERSION_RE.match(ln.strip())]
    assert versions == ["3.12.10"], (
        f"stderr 分离（2>$null）+ 严格解析后必须恰得 ['3.12.10']（无 warning 污染），"
        f"这是写入 receipt.pythonVersion 的值: 得到 {versions!r}; raw stdout={proc.stdout!r}"
    )
    # 分离 stderr 后，stdout 中绝不含 uv warning（污染源）。
    assert "warning" not in proc.stdout.lower(), (
        f"分离 stderr 后 stdout 不应含 uv warning: {proc.stdout!r}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="uv-locked python 探针需 Windows PowerShell")
def test_hostile_virtualenv_warning_lands_on_stderr_not_stdout() -> None:
    """反向证明漏洞真实存在（GPT 第十二轮复现）：敌意 VIRTUAL_ENV 下，uv 的
    "will be ignored" warning 落在 **stderr**，stdout 恰为纯版本行。因此旧 `2>&1` 把
    stderr 并入 stdout 才会污染"首行"；修复后 `2>$null` 分离 stderr 使 stdout 纯净。

    用 Python 分离管道（不在 PS 内 2>&1，避开 PS 5.1 把 native stderr 包成
    NativeCommandError 的 exit 1 干扰）：直接观察 stdout 与 stderr 的实际归属。
    若某天 uv 不再对敌意 VIRTUAL_ENV 打 warning，则无污染可查 → xfail 容忍，
    核心保证由上一条 strict-parse 测试提供。
    """
    script = (
        f'$env:UV_PYTHON="3.12"; '
        f'$env:VIRTUAL_ENV="{HOSTILE_VENV}"; '
        f'& uv run --locked python -c "{PROBE_PY}"'
    )
    proc = _run_ps(script)  # stdout / stderr 由 Python 分别捕获

    stdout_versions = [ln.strip() for ln in proc.stdout.splitlines() if VERSION_RE.match(ln.strip())]
    assert stdout_versions == ["3.12.10"], (
        f"分离后 stdout 应恰为纯版本 3.12.10（无 warning）: stdout={proc.stdout!r}"
    )
    assert "warning" not in proc.stdout.lower(), (
        f"分离后 stdout 不应含 uv warning: {proc.stdout!r}"
    )

    if "warning" not in proc.stderr.lower():
        pytest.xfail("此 uv 版本未对敌意 VIRTUAL_ENV 打 warning；核心保证见 strict-parse 测试")
    # 漏洞根源确认：warning 确实存在于 stderr——旧 2>&1 会把它并入 stdout 首行造成污染。
    assert "ignored" in proc.stderr.lower() or "virtual_env" in proc.stderr.lower(), (
        f"期望 stderr 含 VIRTUAL_ENV will-be-ignored warning: {proc.stderr!r}"
    )
