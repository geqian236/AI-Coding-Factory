r"""
tests/contract/test_python_pin_probe.py

GPT 第十二轮 P2 + 第十三轮 P1-1/P1-2/P2：uv 锁定 Python 探针的回归测试，锁定
receipt.pythonVersion 抗 uv warning 污染，且**绑定正式生产入口**（不再自实现）。

第十二轮根因：phase0-acceptance.ps1 的 B-2 探针与 check.ps1 的版本探针用 `2>&1`
合并 stderr，敌意 VIRTUAL_ENV 触发的 uv warning 被并入流、污染 receipt.pythonVersion。
修复：`2>$null` 分离 stderr + 严格解析唯一 ^\d+\.\d+\.\d+$。

第十三轮根因（本文件自身的缺陷）：
  P1-1：旧回归测试自己重实现 uv 命令与正则，未调用正式脚本 → mutation（把生产入口
        改回 2>&1）测试仍假绿。修法：探针逻辑抽成共享函数 Get-UvLockedPythonVersion
        （scripts/_python-probe.ps1），两个正式入口 + 本测试**共用同一实现**；本测试
        Test A 直接调用该共享函数，Test B 锁定两入口确实 dot-source 并调用它、且不再
        内联旧探针（mutation 防线）。
  P1-2：旧测试硬编码补丁版本 3.12.10，但 .python-version/CI/合同只冻结 3.12.x，
        合法补丁升级会 CI 假红。修法：从 .python-version 读 major.minor，断言输出满足
        ^<major.minor>\.\d+$，并断言 returncode==0 与函数 exitCode==0。
  P2：旧测试在 uv 不再打 warning 时 pytest.xfail，但冻结门禁要求固定计数，xfail 会变
      337/1/1xfailed 被 A-1-count 拒。修法：去掉 xfail——无 warning 时普通 PASS，
      冻结计数不变。

Test A 仅在 Windows 运行（需 PowerShell + uv 锁定 .venv）；Test B 纯读脚本文本，
跨平台运行（ubuntu CI 也执行，锁定生产入口引用）。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

# 行级 dot-source 正则（GPT 第十四轮 P2）：匹配"以 dot-source 运算符 . 开头、最终引用
# _python-probe.ps1"的真实代码行。去注释后，被注释掉的 dot-source 行整行消失，此正则
# 不再命中——故"把真实 dot-source 行注释掉"的 mutation 会被捕获。
DOTSOURCE_PROBE_RE = re.compile(r"(?m)^\s*\.\s+.*_python-probe\.ps1")

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_PS1 = REPO_ROOT / "scripts" / "_python-probe.ps1"
ACCEPTANCE_PS1 = REPO_ROOT / "scripts" / "phase0-acceptance.ps1"
CHECK_PS1 = REPO_ROOT / "scripts" / "check.ps1"
PYTHON_VERSION_FILE = REPO_ROOT / ".python-version"

# 敌意 VIRTUAL_ENV：指向不存在的路径，触发 uv 的 "will be ignored" stderr warning。
HOSTILE_VENV = r"D:\codex项目\nonexistent-hostile-venv"

# 行级 dot-source 正则（GPT 第十四轮 P2）：匹配**真实**的 dot-source 语句
# `. <path...>_python-probe.ps1`（行首可有空白，'.' 后必须有空白）。在**去注释**后的
# 纯代码上匹配——注释掉真实 dot-source 行（前加 '#'）后该行会被 _strip_ps_comments
# 删除，正则不再命中，从而捕获"注释代偿"这类 mutation（旧版在原始文本里搜子串会假绿）。
DOTSOURCE_RE = re.compile(r"(?m)^\s*\.\s+.*_python-probe\.ps1")


def _frozen_major_minor() -> str:
    """从 .python-version 读冻结的 major.minor（如 "3.12"）；缺失回退 "3.12"。

    GPT 第十三轮 P1-2：断言基于此，不硬编码补丁版本——任何 3.12.x 补丁合法。
    """
    if PYTHON_VERSION_FILE.exists():
        raw = PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
        m = re.match(r"^(\d+)\.(\d+)", raw)
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return "3.12"


def _parse_fields(stdout: str) -> dict[str, str]:
    """从 KEY=VALUE 行解析字段字典（共享函数经 PS 包装只回传 ASCII 字段）。"""
    out: dict[str, str] = {}
    for ln in stdout.splitlines():
        ln = ln.strip()
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


@pytest.mark.skipif(sys.platform != "win32", reason="uv-locked python 探针需 Windows PowerShell + 锁定 .venv")
def test_shared_probe_returns_frozen_major_minor_under_hostile_virtualenv() -> None:
    """P1-1 + P1-2 + P2 核心：在**敌意 VIRTUAL_ENV** 下调用**正式共享探针**
    Get-UvLockedPythonVersion（生产入口 B-2 / check.ps1 用的同一函数），断言：
      - ok=True、函数 exitCode=0、PS 进程 returncode=0（P1-2 补的退出码断言）；
      - version 满足 ^<.python-version 的 major.minor>\\.\\d+$（P1-2：不硬编码补丁）；
      - version 不含 'warning'（P2 修法在共享函数内的 2>$null 分离生效）。

    因为直接调用生产函数，若把共享函数改回 2>&1 / 去掉严格解析，本断言会真失败
    （不再像旧版自实现那样假绿）。
    """
    mm = _frozen_major_minor()
    # PS 包装：dot-source 共享探针 → 设敌意 VIRTUAL_ENV → 调用函数 → 只回传 ASCII 字段。
    # 探针内部已 2>$null 分离 stderr、严格解析、从 .python-version 读 major.minor。
    script = (
        f". '{PROBE_PS1}'; "
        f'$env:UV_PYTHON="{mm}"; '
        f'$env:VIRTUAL_ENV="{HOSTILE_VENV}"; '
        f"$r = Get-UvLockedPythonVersion -RepoRoot '{REPO_ROOT}'; "
        r'Write-Output ("OK=" + $r.ok); '
        r'Write-Output ("VER=" + $r.version); '
        r'Write-Output ("EXIT=" + $r.exitCode); '
        r'Write-Output ("COUNT=" + $r.matchCount); '
        r'Write-Output ("MM=" + $r.expectedMajorMinor)'
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=180,
    )
    assert proc.returncode == 0, (
        f"共享探针 PS 包装应正常退出: rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    fields = _parse_fields(proc.stdout)
    assert fields.get("OK") == "True", (
        f"共享探针在敌意 VIRTUAL_ENV 下应 ok=True: fields={fields} stdout={proc.stdout!r}"
    )
    assert fields.get("EXIT") == "0", (
        f"探针 exitCode 应为 0（uv 锁定 env 就是 {mm}）: fields={fields}"
    )
    assert fields.get("COUNT") == "1", (
        f"严格解析必须恰得 1 条版本行（stderr 已 2>$null 分离，无 warning 混入）: fields={fields}"
    )
    assert fields.get("MM") == mm, (
        f"探针读取的 major.minor 应等于 .python-version 的 {mm}: fields={fields}"
    )
    ver = fields.get("VER", "")
    assert re.match(rf"^{re.escape(mm)}\.\d+$", ver), (
        f"写入 receipt.pythonVersion 的值必须满足 ^{mm}.<patch>$（任意补丁合法，无 warning 污染）: got {ver!r}"
    )
    assert "warning" not in ver.lower(), f"版本值不得含 uv warning: {ver!r}"


def _read(p: Path) -> str:
    # utf-8-sig：_python-probe.ps1 现为 UTF-8 with BOM（第十四轮 P1 中文注释），
    # utf-8-sig 会剥掉 BOM，避免首行前缀污染行级正则与 token 匹配。
    return p.read_text(encoding="utf-8-sig")


def _strip_ps_comments(text: str) -> str:
    """去掉 PowerShell 注释后返回纯代码，供断言用。

    先删块注释 <# ... #>，再删行注释（# 到行尾）。本仓这些脚本的代码字符串里不含
    '#'，故行注释删除不会误伤代码。GPT 第十四轮 P2：**正向**断言（必须引用共享探针）
    也改跑在去注释代码上——否则把真实 dot-source 行注释掉、仅靠注释里残留的
    "_python-probe.ps1" 字面就能让测试假绿（reviewer 实测）。
    """
    no_block = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return re.sub(r"(?m)#.*$", "", no_block)


# 行级 dot-source 正则：匹配以 dot-source 运算符 `.` 开头、引用 _python-probe.ps1 的
# **真实代码行**（如 `. (Join-Path $PSScriptRoot "_python-probe.ps1")`）。跑在去注释
# 代码上：把该行注释掉后，_strip_ps_comments 会整行删除，正则不再命中 → mutation 被抓。
DOTSOURCE_PROBE_RE = re.compile(r"(?m)^\s*\.\s+.*_python-probe\.ps1")


def test_production_entry_points_use_shared_probe_no_inline_probe() -> None:
    """P1-1 mutation 防线（跨平台）：锁定两个正式入口确实**引用共享探针**、且**不再内联**
    旧探针逻辑；并锁定共享探针本身用 2>$null 分离 stderr（非 2>&1）。

    这样 reviewer 的 mutation（把生产入口改回有漏洞的 2>&1 / 去严格解析）必被捕获：
      - 若某入口不再 dot-source 或不再调用 Get-UvLockedPythonVersion → 本测试失败；
      - 若某入口重新内联 `sys.version_info[:2]==` 探针 → 本测试失败；
      - 若共享探针把 2>$null 改回 2>&1 → 本测试失败。

    正向断言（必须出现某 token）跑在原始文本上；负向断言（不得出现某模式）跑在
    **去注释后的纯代码**上——否则解释性注释里提到 "2>&1"/"sys.version_info" 会误伤。
    """
    probe_src = _read(PROBE_PS1)
    probe_code = _strip_ps_comments(probe_src)
    # 共享探针（正向跑原始文本，负向跑纯代码）：
    assert "2>$null" in probe_code, "共享探针必须在**代码**中用 2>$null 分离 stderr（P2 修法）"
    assert "2>&1" not in probe_code, "共享探针**代码**绝不能用 2>&1（会把 uv warning 并入版本流）"
    assert r"^\d+\.\d+\.\d+$" in probe_src, "共享探针必须含严格版本解析正则 ^\\d+\\.\\d+\\.\\d+$"
    assert ".python-version" in probe_src, "共享探针必须从 .python-version 读 major.minor（P1-2）"

    for label, path in (("phase0-acceptance.ps1", ACCEPTANCE_PS1), ("check.ps1", CHECK_PS1)):
        src = _read(path)
        code = _strip_ps_comments(src)
        # GPT 第十四轮 P2：dot-source 断言必须跑在**去注释后的代码**上，并用**行级正则**
        # 锁定真实 dot-source 语句（`. <path>\_python-probe.ps1`）。旧版在**原始文本**里
        # 找子串 "_python-probe.ps1"——把真正的 dot-source 行注释掉后，注释里仍有该子串，
        # 测试假绿（reviewer 实测）。行级正则 + 去注释使被注释掉的 dot-source 行消失即失败。
        assert DOTSOURCE_PROBE_RE.search(code), (
            f"{label} 必须在**代码**中以行级 dot-source 语句引用 _python-probe.ps1"
            f"（不能只在注释里出现）"
        )
        assert "Get-UvLockedPythonVersion" in code, (
            f"{label} 必须在**代码**中调用共享探针 Get-UvLockedPythonVersion"
        )
        # 不得再内联旧探针（该逻辑现在只应存在于 _python-probe.ps1 单一真源）。
        assert "sys.version_info" not in code, (
            f"{label} **代码**不得再内联 sys.version_info 探针（逻辑应只在共享探针里）"
        )


def test_dotsource_assertion_is_immune_to_commenting_out_the_line() -> None:
    """P2 mutation 负例（跨平台）：证明 dot-source 断言用的是**去注释 + 行级正则**，
    而非在原始文本里找子串。reviewer 实测把 phase0-acceptance.ps1 里真实的 dot-source
    行注释掉后旧测试仍 2 passed（注释里仍有 "_python-probe.ps1" 子串）。这里就地模拟
    该 mutation：把每个入口的真实 dot-source 行前置 '# ' 注释掉，去注释后行级正则必须
    不再命中——即上面的 test 会真失败。

    不落地改动任何生产文件：只在内存里对读到的文本做 mutation 后断言。
    """
    for label, path in (("phase0-acceptance.ps1", ACCEPTANCE_PS1), ("check.ps1", CHECK_PS1)):
        src = _read(path)
        # 原始（未 mutation）代码：行级正则应命中真实 dot-source 语句。
        assert DOTSOURCE_PROBE_RE.search(_strip_ps_comments(src)), (
            f"{label} 前置条件：未 mutation 时应命中真实 dot-source 行"
        )
        # mutation：把真实 dot-source 行整行注释掉（前置 '# '），模拟 reviewer 的攻击。
        mutated = re.sub(
            r"(?m)^(\s*)(\.\s+.*_python-probe\.ps1.*)$",
            r"\1# \2",
            src,
        )
        assert mutated != src, f"{label}：未能定位真实 dot-source 行以施加 mutation"
        mutated_code = _strip_ps_comments(mutated)
        # 关键断言：注释掉真实 dot-source 行后，去注释代码里行级正则必须不再命中
        #（若仍命中，说明断言又退化成"原始文本找子串"，会被注释代偿假绿）。
        assert not DOTSOURCE_PROBE_RE.search(mutated_code), (
            f"{label}：把真实 dot-source 行注释掉后，行级正则仍命中——断言可被注释代偿（P2 回归）"
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Get-UvLockedPythonVersion 需 Windows PowerShell")
def test_shared_probe_fail_closed_on_bad_python_version_contract() -> None:
    """P1 负例（GPT 第十四轮）：.python-version 缺失/空/不可读格式一律 fail-closed。

    旧版在 .python-version 缺失/非法时静默回退 "3.12" 并 ok=true（reviewer 实测：文件
    不存在仍返回 ok=true/version=3.12.10）。修复后共享探针在跑 uv 探针**之前**就对契约
    做校验：缺失/空/格式非法 → ok=$false + contractError，绝不放行。

    用受控临时目录做 RepoRoot（各含一个坏的或缺失的 .python-version），直接调用共享
    函数，断言 ok=False 且 contractError 命中对应分类。因走 fail-fast 分支、在 uv 探针
    之前返回，故不依赖网络/锁定 .venv，跑得快且确定。
    """
    import tempfile

    cases = [
        ("missing", None),                 # 不建 .python-version
        ("empty", ""),                     # 空文件
        ("empty", "   \n\t "),             # 仅空白 → 归类 empty
        ("malformed", "not-a-version"),    # 非法格式
        ("malformed", "3"),                # 缺 minor
        ("malformed", "x.y"),              # 非数字
    ]
    for expected_cat, content in cases:
        with tempfile.TemporaryDirectory() as td:
            if content is not None:
                (Path(td) / ".python-version").write_text(content, encoding="utf-8")
            script = (
                f". '{PROBE_PS1}'; "
                f"$r = Get-UvLockedPythonVersion -RepoRoot '{td}'; "
                r'Write-Output ("OK=" + $r.ok); '
                r'Write-Output ("CERR=" + $r.contractError)'
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=str(REPO_ROOT), timeout=60,
            )
            fields = _parse_fields(proc.stdout)
            assert fields.get("OK") == "False", (
                f"契约坏值（{expected_cat}, content={content!r}）必须 fail-closed ok=False: "
                f"fields={fields} stdout={proc.stdout!r} stderr={proc.stderr!r}"
            )
            cerr = fields.get("CERR", "")
            assert cerr.startswith(expected_cat), (
                f"contractError 应以 '{expected_cat}' 归类: got {cerr!r} (content={content!r})"
            )
