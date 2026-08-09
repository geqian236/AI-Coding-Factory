"""
tests/contract/test_ci_blocker_fixes.py

GPT Phase 0 PR #2 CI blocker 修复的回归测试（锁定行为不回退）。

背景：PR #2 的两个 GitHub Actions blocker——
  1. desktop job：`.node-version`/engines 要求 Node 22，但 ci.yml 的 desktop job
     硬编码 Node 20；且 packages/factory-contracts 直接 import node:crypto/fs/url/path
     却未声明 @types/node，fresh frozen install 后 tsc 报 8 个 TS2307。
  2. windows-probes job：dev.ps1 把 RUSTUP_HOME 改到 D 盘后，在首次 D-root GNU
     toolchain 下载完成前就检查 ld.lld → 第一次构建无 lld、回退 GNU ld、含中文路径
     链接 exit 101。需在 spike loop 前 fail-closed 预热工具链（经 dev.ps1）。
  另外 test-durable-io.ps1 的两条 BLOCKED_UNCERTIFIED 回执（non-Windows / build 失败）
  缺 validator 必需的证据结构（assertions/passed=false/observable_facts/subcheckId），
  被 validator 判 INVALID 而非合法 core BLOCKED。

GPT round-16 二审强化（本文件）：
  - P1-1：wrapper non-Windows 分支必须在任何 D 盘引用之前可达——用去注释可执行代码做
    位置断言（跨平台）+ 真实 non-Windows/pwsh 的隔离仓库回执路径验证。
  - P1-2：wrapper AST 必须锁定**真实值流**——正确分支→正确 -Kind→赋值 $r→无中途覆盖→
    Write-Receipt $r；并对交换 Kind/覆盖 $r/错误赋值变量/退回旧回执逐一做 mutation 实证。
  - P1-3：CI 预热检测必须用 AST 确认存在**真实 CommandAst** 调用 dev.ps1（完整参数序列），
    并绑定 $LASTEXITCODE -ne 0 → exit 1；对"注释真实调用 + 未使用字符串"做 mutation 实证。
  - P1-4：临时文件用唯一目录（D 盘 DataRoot、worktree/进程/随机唯一），try/finally 清理，
    绝不落 C 盘、不共享、不覆盖他人预存文件；AST 一律 ParseInput/stdin，不生成分析器文件。
  - P2-1：静态接线验证（本文件）与**动态回执反查**（phase0-acceptance.ps1 回执写盘后
    重算 SHA256 fail-closed）分离——本文件只断言静态接线与"动态门存在"，不再空过。
  - P2-2：有 pwsh 时用 pwsh 执行生产 helper/validator/AST（诚实记录 host）。

本文件**不搜索注释子串**证明修复：CI 结构类用缩进感知 Actions 解析器；package.json 按 JSON
解析；pnpm-lock 结构化提取；durable-IO 回执/validator/wrapper 值流**直接调用生产实现 + AST**。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRACTS_PKG = REPO_ROOT / "packages" / "factory-contracts" / "package.json"
PNPM_LOCK = REPO_ROOT / "pnpm-lock.yaml"
NODE_VERSION_FILE = REPO_ROOT / ".node-version"
VALIDATOR_PS1 = REPO_ROOT / "scripts" / "spikes" / "_receipt-validator.ps1"
DURABLE_IO_RECEIPT_PS1 = REPO_ROOT / "scripts" / "spikes" / "_durable-io-receipt.ps1"
DURABLE_IO_WRAPPER_PS1 = REPO_ROOT / "scripts" / "spikes" / "test-durable-io.ps1"
DURABLE_IO_WRAPPER_RECEIPT = (
    REPO_ROOT / "tools" / "compat-probes" / "windows_durable_io" / "receipt.json"
)
ACCEPTANCE_PS1 = REPO_ROOT / "scripts" / "phase0-acceptance.ps1"


# ─────────────────────────────────────────────────────────────────────────────
# PowerShell host 选择（GPT round-16 二审 P2-2）：有 pwsh 时优先 pwsh（CI windows-probes
# 用 shell: pwsh，本处随之在 pwsh 下真跑生产实现，提供 pwsh 通过证据）；本机无 pwsh 时
# 诚实回退 Windows PowerShell 5.1。返回可执行绝对路径；无任何 PS host 时抛错（win32-only
# 测试才会调用，非 Windows 早已 skip）。
# ─────────────────────────────────────────────────────────────────────────────
def _ps_host() -> str:
    exe = shutil.which("pwsh") or shutil.which("powershell")
    if exe is None:
        raise RuntimeError("no PowerShell host (pwsh/powershell) found")
    return exe


def _has_pwsh() -> bool:
    return shutil.which("pwsh") is not None


def _strip_ps_comments(text: str) -> str:
    """去掉 PowerShell 注释后返回**纯可执行代码**（供"不得被注释绕过"的断言用）。

    先删块注释 <# ... #>，再删行注释（# 到行尾）。本仓 CI/spike 脚本的代码字符串里
    不含裸 '#'，故行注释删除不会误伤可执行代码。断言必须跑在去注释代码上——把真实命令
    注释掉、只在注释里留关键词时，测试必须失败。
    """
    no_block = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return re.sub(r"(?m)#.*$", "", no_block)


def _normalize_ws(text: str) -> str:
    """折叠空白 + 去引号，便于把多行/多空格命令按 token 序列做确定性子串匹配。"""
    no_quotes = text.replace('"', " ").replace("'", " ")
    return re.sub(r"\s+", " ", no_quotes).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 缩进感知的 GitHub Actions workflow 解析器（解析可执行 YAML 结构，非注释）。
# 无需第三方 YAML 库（locked venv 无 PyYAML）；只解析本仓 ci.yml 用到的构造：
#   jobs:(col0) → <job>:(col2) → steps:(col4) → '- key:'(col6) → 'key:'(col8)
#   → 'with:' 子键(col10)；block scalar 'run: |' 捕获缩进 > 8 的正文。
# 解析器自带 sanity 断言（见 _load_jobs），解析器 bug 不会静默放行。
# ─────────────────────────────────────────────────────────────────────────────
def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] in "'\"" and v[-1] == v[0]:
        return v[1:-1]
    return v


def _capture_block(lines: list[str], i: int, key_indent: int) -> tuple[str, int]:
    """捕获 block scalar（'key: |'）正文：缩进 > key_indent 的连续行，去掉 key_indent+2 前缀。"""
    out: list[str] = []
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "":
            out.append("")
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= key_indent:
            break
        prefix = key_indent + 2
        out.append(line[prefix:] if len(line) > prefix else line.strip())
        i += 1
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out), i


def _capture_with(lines: list[str], i: int) -> tuple[dict[str, str], int]:
    """捕获 'with:' 映射的子键（缩进 >= 10）。"""
    out: dict[str, str] = {}
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.strip() == "" or line.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent < 10:
            break
        m = re.match(r"^\s+([A-Za-z0-9_.-]+):\s?(.*)$", line)
        if m:
            out[m.group(1)] = _scalar(m.group(2))
        i += 1
    return out, i


def _parse_steps(job_lines: list[str]) -> list[dict[str, Any]]:
    """解析一个 job 的 steps: 序列，返回按顺序的 step 字典列表（含 _order）。"""
    steps: list[dict[str, Any]] = []
    n = len(job_lines)
    i = 0
    while i < n and not re.match(r"^    steps:\s*$", job_lines[i]):
        i += 1
    i += 1  # 跳过 'steps:'
    cur: dict[str, Any] | None = None
    while i < n:
        line = job_lines[i]
        if line.strip() == "" or line.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= 4 and line.strip() != "":
            break
        m = re.match(r"^      - ([A-Za-z0-9_.-]+):\s?(.*)$", line)
        if m:
            if cur is not None:
                steps.append(cur)
            cur = {"_order": len(steps)}
            key, val = m.group(1), m.group(2)
            cur[key] = _scalar(val)
            i += 1
            continue
        m = re.match(r"^        ([A-Za-z0-9_.-]+):\s?(.*)$", line)
        if m and cur is not None:
            key, val = m.group(1), m.group(2)
            if val.strip() in ("|", "|-", ">", ">-", ""):
                if key == "with":
                    withmap, i = _capture_with(job_lines, i + 1)
                    cur["with"] = withmap
                    continue
                block, i = _capture_block(job_lines, i + 1, 8)
                cur[key] = block
                continue
            cur[key] = _scalar(val)
            i += 1
            continue
        i += 1
    if cur is not None:
        steps.append(cur)
    return steps


def _load_jobs(text: str) -> dict[str, list[dict[str, Any]]]:
    """解析整个 workflow，返回 {job_name: [step, ...]}。自带 sanity 断言。"""
    lines = text.split("\n")
    n = len(lines)
    i = 0
    while i < n and not re.match(r"^jobs:\s*$", lines[i]):
        i += 1
    assert i < n, "ci.yml 未找到顶层 jobs: 键（解析器前置失败）"
    i += 1
    jobs: dict[str, list[str]] = {}
    cur_name: str | None = None
    start = 0
    while i < n:
        line = lines[i]
        if re.match(r"^[A-Za-z]", line):
            break
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if cur_name is not None:
                jobs[cur_name] = lines[start:i]
            cur_name = m.group(1)
            start = i + 1
        i += 1
    if cur_name is not None:
        jobs[cur_name] = lines[start:i]
    parsed = {name: _parse_steps(body) for name, body in jobs.items()}
    for required_job in ("desktop", "windows-probes", "contracts"):
        assert required_job in parsed, f"解析器未提取到 job '{required_job}'（解析器可能失效）"
        assert len(parsed[required_job]) >= 2, f"job '{required_job}' 解析出的 step 过少（解析器可能失效）"
    return parsed


def _find_step(
    steps: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any] | None:
    for s in steps:
        if predicate(s):
            return s
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 一、desktop CI：Node 版本单一真源
# ─────────────────────────────────────────────────────────────────────────────
def test_desktop_job_uses_node_version_file_not_hardcoded() -> None:
    """desktop job 的 setup-node 必须用 node-version-file: '.node-version'（唯一真源），
    绝不硬编码 node-version（无论 20 还是 22）。"""
    jobs = _load_jobs(CI_YML.read_text(encoding="utf-8"))
    desktop = jobs["desktop"]
    setup_node = _find_step(desktop, lambda s: "actions/setup-node" in str(s.get("uses", "")))
    assert setup_node is not None, "desktop job 缺少 actions/setup-node 步骤"
    with_map = setup_node.get("with", {})
    assert with_map.get("node-version-file") == ".node-version", (
        f"setup-node 必须用 node-version-file='.node-version'（读 .node-version 单一真源）；"
        f"实际 with={with_map}"
    )
    assert "node-version" not in with_map, (
        f"setup-node 不得硬编码 node-version（应只用 node-version-file）；实际 with={with_map}"
    )


def test_node_version_file_is_22() -> None:
    """.node-version 单一真源必须是 22（与 engines 一致）；desktop CI 经 file 读到它。"""
    assert NODE_VERSION_FILE.read_text(encoding="utf-8").strip() == "22", (
        "..node-version 必须为 22（Node 版本唯一真源）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 二、factory-contracts 直接声明 @types/node（package importer + lock importer）
# ─────────────────────────────────────────────────────────────────────────────
def test_factory_contracts_package_declares_types_node() -> None:
    """packages/factory-contracts/package.json 的 devDependencies 必须直接含 @types/node。"""
    pkg = json.loads(CONTRACTS_PKG.read_text(encoding="utf-8"))
    dev = pkg.get("devDependencies", {})
    assert "@types/node" in dev, (
        f"factory-contracts devDependencies 必须直接声明 @types/node；实际 ={dev}"
    )
    spec = dev["@types/node"]
    assert re.search(r"2[2-9]|[3-9]\d", spec), (
        f"@types/node 版本应兼容 Node 22（如 ^22.x）；实际 spec='{spec}'"
    )


def test_pnpm_lock_pins_types_node_for_factory_contracts() -> None:
    """pnpm-lock.yaml 的 importers['packages/factory-contracts'].devDependencies 必须
    结构化包含 @types/node（fresh --frozen-lockfile 才能装到它）。结构化解析 importer 块。"""
    text = PNPM_LOCK.read_text(encoding="utf-8")
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n and lines[i].rstrip() != "importers:":
        i += 1
    assert i < n, "pnpm-lock.yaml 未找到 importers:"
    while i < n and lines[i].strip() != "packages/factory-contracts:":
        i += 1
    assert i < n, "pnpm-lock.yaml importers 未找到 packages/factory-contracts"
    importer_indent = len(lines[i]) - len(lines[i].lstrip())
    i += 1
    in_dev = False
    found = False
    dev_indent = -1
    while i < n:
        line = lines[i]
        if line.strip() == "":
            i += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= importer_indent:
            break
        if line.strip() == "devDependencies:":
            in_dev = True
            dev_indent = indent
            i += 1
            continue
        if in_dev:
            if indent <= dev_indent:
                in_dev = False
                continue
            if re.match(r"^\s*'?@types/node'?:\s*$", line):
                found = True
                break
        i += 1
    assert found, (
        "pnpm-lock.yaml 的 importers['packages/factory-contracts'].devDependencies "
        "必须含 @types/node（用 pnpm 9.15.4 frozen 更新锁文件）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 三、windows-probes CI：spike loop 前 fail-closed 预热工具链（经 dev.ps1）
# ─────────────────────────────────────────────────────────────────────────────
def _windows_probes_steps() -> list[dict[str, Any]]:
    return _load_jobs(CI_YML.read_text(encoding="utf-8"))["windows-probes"]


# 预热步骤必须**实际执行**的完整命令（按 token 序列匹配，非注释子串）。
PREHEAT_REQUIRED_CMD = (
    "scripts/dev.ps1 -- -- rustup toolchain install "
    "stable-x86_64-pc-windows-gnu --profile minimal"
)


def _is_spike_loop_step(step: dict[str, Any]) -> bool:
    """spike 执行步骤：其**去注释可执行** run 正文 dot-source 校验器并遍历 $spikes。"""
    code = _strip_ps_comments(str(step.get("run", "")))
    return "_receipt-validator.ps1" in code and "$spikes" in code


def _is_preheat_step(step: dict[str, Any]) -> bool:
    """预热步骤识别（结构定位用）：**去注释可执行代码**里出现完整预热命令。
    仅用于定位 + 顺序断言；命令"真实性"由 P1-3 的 AST 测试权威判定。"""
    code_norm = _normalize_ws(_strip_ps_comments(str(step.get("run", ""))))
    return PREHEAT_REQUIRED_CMD in code_norm


def _preheat_step() -> dict[str, Any]:
    """结构锚点定位预热步骤：优先按 step 的 name 字段含"预热"（YAML 结构字段，非 run 正文，
    不可被 run 内注释绕过），回退到去注释 token 匹配。"""
    steps = _windows_probes_steps()
    by_name = _find_step(steps, lambda s: "预热" in str(s.get("name", "")))
    if by_name is not None:
        return by_name
    tok = _find_step(steps, _is_preheat_step)
    assert tok is not None, "windows-probes 未找到预热步骤"
    return tok


def test_windows_probes_preheats_toolchain_before_spike_loop() -> None:
    """windows-probes 必须在 spike loop **之前**有预热步骤，其**去注释可执行代码**完整包含
    reviewer 指定命令，且 shell 为 pwsh。（结构 + 顺序断言；命令真实性见 AST 测试。）"""
    steps = _windows_probes_steps()
    preheat = _preheat_step()
    spike = _find_step(steps, _is_spike_loop_step)
    assert spike is not None, "windows-probes 缺少 spike 执行步骤（解析器或 CI 结构异常）"
    assert preheat["_order"] < spike["_order"], (
        f"预热步骤（order={preheat['_order']}）必须在 spike loop（order={spike['_order']}）之前"
    )
    assert str(preheat.get("shell", "")).strip() == "pwsh", (
        f"预热步骤 shell 必须为 pwsh；实际 ={preheat.get('shell')!r}"
    )
    code_norm = _normalize_ws(_strip_ps_comments(str(preheat["run"])))
    assert PREHEAT_REQUIRED_CMD in code_norm, (
        f"预热**可执行代码**必须完整包含 '{PREHEAT_REQUIRED_CMD}'；实际归一化后 ={code_norm!r}"
    )


def test_windows_probes_preheat_is_fail_closed() -> None:
    """windows-probes 无 continue-on-error；预热**可执行代码**显式检查 $LASTEXITCODE 且非零退出。"""
    steps = _windows_probes_steps()
    offenders = [
        s for s in steps
        if str(s.get("continue-on-error", "")).strip().lower() == "true"
    ]
    offender_labels = [s.get("name") or s.get("uses") for s in offenders]
    assert not offenders, (
        f"windows-probes 不得有 continue-on-error: true 的步骤；违规 ={offender_labels}"
    )
    preheat = _preheat_step()
    code = _strip_ps_comments(str(preheat["run"]))
    assert re.search(r"\$LASTEXITCODE\s+-ne\s+0", code), (
        f"预热可执行代码必须检查 $LASTEXITCODE -ne 0；实际去注释代码 ={code!r}"
    )
    assert re.search(r"(?m)^\s*exit\s+1\s*$", code) or "exit 1" in _normalize_ws(code), (
        f"预热可执行代码必须在安装失败时 exit 1（fail-closed）；实际去注释代码 ={code!r}"
    )


def test_dev_ps1_does_not_implicitly_install_toolchain() -> None:
    """不得把安装逻辑塞进 dev.ps1（否则每次运行隐式联网）。dev.ps1 正文不得出现
    rustup toolchain install。"""
    dev = (REPO_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    code_lines = [ln for ln in dev.split("\n") if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert not re.search(r"rustup\s+toolchain\s+install", code), (
        "dev.ps1 不得隐式安装工具链（安装应是 CI 显式预热步骤）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# AST 分析基础设施（GPT round-16 二审 P1-4）：一律 ParseInput + stdin，不生成分析器文件。
#   - 分析器脚本经 -EncodedCommand（UTF-16LE base64）传入，纯 ASCII、无临时文件；
#   - 被分析源码经 stdin 以 base64(UTF-8) 传入，PS 端 base64 解码后 ParseInput，
#     不受控制台代码页（GBK）影响，含 CJK 的 wrapper 也不会因编码假阳。
# ─────────────────────────────────────────────────────────────────────────────
def _encoded_command(ps: str) -> str:
    return base64.b64encode(ps.encode("utf-16-le")).decode("ascii")


def _run_ps_analyzer(
    analyzer_ps: str, source_text: str, extra_env: dict[str, str] | None = None
) -> dict[str, Any]:
    enc = _encoded_command(analyzer_ps)
    src_b64 = base64.b64encode(source_text.encode("utf-8")).decode("ascii")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc],
        input=src_b64, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=120, env=env,
    )
    assert result.returncode == 0, (
        f"AST 分析器失败 rc={result.returncode} stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


# wrapper 值流 AST 分析器（P1-2）：证明每个分支的完整值流
#   正确条件（env:OS / buildExit）→ 正确 -Kind → 最后一次 $r 赋值来自 helper（无中途覆盖）
#   → Write-Receipt 消费 $r。任一环节被 mutation（换 Kind / 覆盖 $r / 错误变量 / 退回旧回执）
#   破坏都会翻转对应布尔，测试随即失败。
_WRAPPER_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$parseErrors = @($errors).Count
$cmds = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)

$dotSourcesHelper = $false
foreach ($c in $cmds) {
    if ($c.InvocationOperator -eq [System.Management.Automation.Language.TokenKind]::Dot -and
        $c.Extent.Text -match '_durable-io-receipt\.ps1') {
        $dotSourcesHelper = $true
    }
}
$helperCalls = @($cmds | Where-Object { $_.GetCommandName() -eq 'New-DurableIoBlockedReceipt' })

function Get-KindArg($cmd) {
    $els = @($cmd.CommandElements)
    for ($i = 0; $i -lt $els.Count; $i++) {
        $e = $els[$i]
        if ($e -is [System.Management.Automation.Language.CommandParameterAst] -and
            $e.ParameterName -eq 'Kind') {
            if ($null -ne $e.Argument) { return $e.Argument.Extent.Text }
            if ($i + 1 -lt $els.Count) { return $els[$i + 1].Extent.Text }
        }
    }
    return $null
}

# P1-2：以下三个谓词把“出现过变量”收紧为具体 helper 赋值和精确 Write-Receipt $r。
function Test-ExactVariableR($node) {
    return ($node -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $node.VariablePath.UserPath -eq 'r')
}

function Test-RMutationTarget($node) {
    if (Test-ExactVariableR $node) { return $true }
    # 成员/索引改写（$r.reason / $r['reason']）同样会劫持即将落盘的回执。
    return ($node.Extent.Text -match '^\s*\$r(?:\.|\[)')
}

function Test-ExactWriteReceiptR($cmd) {
    $els = @($cmd.CommandElements)
    return ($cmd.GetCommandName() -eq 'Write-Receipt' -and $els.Count -eq 2 -and
        (Test-ExactVariableR $els[1]))
}

function Analyze-Branch($bodyStart, $bodyEnd) {
    $res = [ordered]@{
        kind = $null; lastRFromHelper = $false; writesR = $false
        helperEndBeforeWrite = $false; writeExactR = $false
        noIntermediateRMutation = $false; valueFlowValid = $false
    }
    $assigns = @($ast.FindAll(
        { $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) |
        Where-Object { $_.Extent.StartOffset -ge $bodyStart -and $_.Extent.EndOffset -le $bodyEnd })
    $rAssigns = @($assigns | Where-Object {
        $_.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $_.Left.VariablePath.UserPath -eq 'r'
    } | Sort-Object { $_.Extent.StartOffset })
    $helperAssigns = @()
    foreach ($assign in $rAssigns) {
        $rhs = @($assign.Right.FindAll(
            { $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true) |
            Where-Object { $_.GetCommandName() -eq 'New-DurableIoBlockedReceipt' })
        if ($rhs.Count -eq 1) {
            $helperAssigns += [pscustomobject]@{ assignment = $assign; command = $rhs[0] }
        }
    }
    $writes = @($cmds | Where-Object {
        $_.GetCommandName() -eq 'Write-Receipt' -and
        $_.Extent.StartOffset -ge $bodyStart -and $_.Extent.EndOffset -le $bodyEnd
    })
    $exactWrites = @($writes | Where-Object { Test-ExactWriteReceiptR $_ })
    $res.writesR = ($exactWrites.Count -ge 1)
    # 一个分支只能有一个落盘写；这样“先写后 helper”不会被后续的第二次写掩盖。
    $res.writeExactR = ($writes.Count -eq 1 -and $exactWrites.Count -eq 1)
    if ($helperAssigns.Count -eq 1) {
        $helper = $helperAssigns[0]
        $res.kind = Get-KindArg $helper.command
        $res.lastRFromHelper = ($rAssigns.Count -ge 1 -and
            $rAssigns[$rAssigns.Count - 1].Extent.StartOffset -eq $helper.assignment.Extent.StartOffset)
        if ($res.writeExactR) {
            $write = $exactWrites[0]
            $res.helperEndBeforeWrite = ($helper.assignment.Extent.EndOffset -lt $write.Extent.StartOffset)
            $res.noIntermediateRMutation = $true
            $between = @($assigns | Where-Object {
                $_.Extent.StartOffset -ge $helper.assignment.Extent.EndOffset -and
                $_.Extent.EndOffset -le $write.Extent.StartOffset
            })
            foreach ($assign in $between) {
                if (Test-RMutationTarget $assign.Left) {
                    $res.noIntermediateRMutation = $false
                }
            }
        }
    }
    $res.valueFlowValid = ($helperAssigns.Count -eq 1 -and $res.lastRFromHelper -and
        $res.writeExactR -and $res.helperEndBeforeWrite -and $res.noIntermediateRMutation)
    return $res
}

$ifs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.IfStatementAst] }, $true)
$nonWin = [ordered]@{
    found = $false; kind = $null; lastRFromHelper = $false; writesR = $false
    conditionExact = $false; helperEndBeforeWrite = $false; writeExactR = $false
    noIntermediateRMutation = $false; valueFlowValid = $false
}
$buildFail = [ordered]@{
    found = $false; kind = $null; lastRFromHelper = $false; writesR = $false
    conditionExact = $false; helperEndBeforeWrite = $false; writeExactR = $false
    noIntermediateRMutation = $false; valueFlowValid = $false
}
foreach ($if in $ifs) {
    foreach ($clause in $if.Clauses) {
        $cond = $clause.Item1; $body = $clause.Item2
        $ct = $cond.Extent.Text
        $inBody = @($helperCalls | Where-Object {
            $_.Extent.StartOffset -ge $body.Extent.StartOffset -and
            $_.Extent.EndOffset -le $body.Extent.EndOffset
        })
        if ($inBody.Count -lt 1) { continue }
        $a = Analyze-Branch $body.Extent.StartOffset $body.Extent.EndOffset
        if ($ct -match 'env:OS') {
            $nonWin.found = $true; $nonWin.kind = $a.kind
            $nonWin.lastRFromHelper = $a.lastRFromHelper; $nonWin.writesR = $a.writesR
            $normalizedCondition = ($ct -replace '"', '' -replace "'", '' -replace '\s+', '')
            $nonWin.conditionExact = ($normalizedCondition -eq '$env:OS-notmatchWindows')
            $nonWin.helperEndBeforeWrite = $a.helperEndBeforeWrite; $nonWin.writeExactR = $a.writeExactR
            $nonWin.noIntermediateRMutation = $a.noIntermediateRMutation
            $nonWin.valueFlowValid = $a.valueFlowValid
        }
        if ($ct -match 'buildExit') {
            $buildFail.found = $true; $buildFail.kind = $a.kind
            $buildFail.lastRFromHelper = $a.lastRFromHelper; $buildFail.writesR = $a.writesR
            $normalizedCondition = ($ct -replace '"', '' -replace "'", '' -replace '\s+', '')
            $buildFail.conditionExact = ($normalizedCondition -eq '$buildExit-ne0-or-not(Test-Path$writerExe)')
            $buildFail.helperEndBeforeWrite = $a.helperEndBeforeWrite; $buildFail.writeExactR = $a.writeExactR
            $buildFail.noIntermediateRMutation = $a.noIntermediateRMutation
            $buildFail.valueFlowValid = $a.valueFlowValid
        }
    }
}

[pscustomobject]@{
    parseErrors               = $parseErrors
    dotSourcesHelper          = $dotSourcesHelper
    helperCallCount           = $helperCalls.Count
    nonWindowsFound           = $nonWin.found
    nonWindowsKind            = $nonWin.kind
    nonWindowsLastRFromHelper = $nonWin.lastRFromHelper
    nonWindowsWritesR         = $nonWin.writesR
    nonWindowsConditionExact   = $nonWin.conditionExact
    nonWindowsHelperEndBeforeWrite = $nonWin.helperEndBeforeWrite
    nonWindowsWriteExactR      = $nonWin.writeExactR
    nonWindowsNoIntermediateRMutation = $nonWin.noIntermediateRMutation
    nonWindowsValueFlowValid   = $nonWin.valueFlowValid
    buildFailFound            = $buildFail.found
    buildFailKind             = $buildFail.kind
    buildFailLastRFromHelper  = $buildFail.lastRFromHelper
    buildFailWritesR          = $buildFail.writesR
    buildFailConditionExact   = $buildFail.conditionExact
    buildFailHelperEndBeforeWrite = $buildFail.helperEndBeforeWrite
    buildFailWriteExactR      = $buildFail.writeExactR
    buildFailNoIntermediateRMutation = $buildFail.noIntermediateRMutation
    buildFailValueFlowValid   = $buildFail.valueFlowValid
} | ConvertTo-Json -Compress
"""


# CI 预热 run block AST 分析器（P1-3）：确认存在真实 CommandAst 调用 dev.ps1（完整参数序列），
# 而非仅在未使用字符串/注释里出现命令文本；守卫末尾必须是直接的 `exit 1` AST，不能用正文正则。
_CI_PREHEAT_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$parseErrors = @($errors).Count

# P1-3：逐项比较 CommandElements，禁止把多个参数塞回一个字符串后靠全文正则蒙混过关。
$expectedElements = @(
    '$PWD/scripts/dev.ps1', '--', '--', 'rustup', 'toolchain', 'install',
    'stable-x86_64-pc-windows-gnu', '--profile', 'minimal'
)

function Normalize-CommandElement($element) {
    $value = $element.Extent.Text.Trim()
    # CommandElement 的首项在 YAML 中带双引号；只去除完整包裹引号，保留参数边界。
    if ($value.Length -ge 2 -and (
        ($value.StartsWith([string][char]34) -and $value.EndsWith([string][char]34)) -or
        ($value.StartsWith([string][char]39) -and $value.EndsWith([string][char]39)))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return ($value -replace '\\', '/')
}

function Test-ExactPreheatElements($cmd) {
    $elements = @($cmd.CommandElements)
    if ($elements.Count -ne $expectedElements.Count) { return $false }
    for ($i = 0; $i -lt $expectedElements.Count; $i++) {
        if ((Normalize-CommandElement $elements[$i]) -cne $expectedElements[$i]) {
            return $false
        }
    }
    return $true
}

function Get-ExitGuardDetails($statement) {
    $result = [ordered]@{ isGuard = $false; conditionExact = $false; exitsOne = $false }
    if ($statement -isnot [System.Management.Automation.Language.IfStatementAst]) { return $result }
    $clauses = @($statement.Clauses)
    if ($clauses.Count -ne 1 -or $null -ne $statement.ElseClause) { return $result }
    $result.isGuard = $true
    $condition = $clauses[0].Item1
    $body = $clauses[0].Item2
    $normalizedCondition = ($condition.Extent.Text -replace '\s+', '')
    $result.conditionExact = ($normalizedCondition -ceq '$LASTEXITCODE-ne0')

    # 只能接受 guard 代码块最后一个**直接顶层**语句的 ExitStatementAst。嵌套 if、字符串
    # 或前置 exit 都不代表失败分支一定退出；参数还必须是文本和 AST 值均精确为常量 1。
    $directStatements = @($body.Statements)
    if ($directStatements.Count -eq 0) { return $result }
    $lastStatement = $directStatements[$directStatements.Count - 1]
    if ($lastStatement -isnot [System.Management.Automation.Language.ExitStatementAst]) { return $result }
    $pipeline = $lastStatement.Pipeline
    $pipelineElements = @($pipeline.PipelineElements)
    if ($pipelineElements.Count -ne 1 -or
        $pipelineElements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) { return $result }
    $expression = $pipelineElements[0].Expression
    if ($expression -isnot [System.Management.Automation.Language.ConstantExpressionAst]) { return $result }
    $result.exitsOne = ($expression.Value -is [int] -and $expression.Value -eq 1 -and
        $expression.Extent.Text -ceq '1')
    return $result
}

$topStatements = @($ast.EndBlock.Statements)
$devInvokeCount = 0
$devExactCount = 0
$devIndex = -1
for ($i = 0; $i -lt $topStatements.Count; $i++) {
    $statement = $topStatements[$i]
    if ($statement -isnot [System.Management.Automation.Language.PipelineAst]) { continue }
    $pipelineElements = @($statement.PipelineElements)
    if ($pipelineElements.Count -ne 1 -or
        $pipelineElements[0] -isnot [System.Management.Automation.Language.CommandAst]) { continue }
    $command = $pipelineElements[0]
    $elements = @($command.CommandElements)
    if ($elements.Count -lt 1 -or
        (Normalize-CommandElement $elements[0]) -cne '$PWD/scripts/dev.ps1') { continue }
    $devInvokeCount++
    if (Test-ExactPreheatElements $command) {
        $devExactCount++
        $devIndex = $i
    }
}

$commandElementsExact = ($devExactCount -eq 1)
$devPreheatAtTopLevel = $commandElementsExact
$nextTopLevelIsExitGuard = $false
$guardConditionExact = $false
$guardExitsOne = $false
if ($devPreheatAtTopLevel -and $devIndex + 1 -lt $topStatements.Count) {
    $guard = Get-ExitGuardDetails $topStatements[$devIndex + 1]
    $nextTopLevelIsExitGuard = $guard.isGuard
    $guardConditionExact = $guard.conditionExact
    $guardExitsOne = $guard.exitsOne
}
$devInvokes = $commandElementsExact
$hasExitGuard = ($nextTopLevelIsExitGuard -and $guardConditionExact)
$preheatValid = ($commandElementsExact -and $devPreheatAtTopLevel -and
    $nextTopLevelIsExitGuard -and $guardConditionExact -and $guardExitsOne)

[pscustomobject]@{
    parseErrors              = $parseErrors
    devInvokes               = $devInvokes
    devInvokeCount           = $devInvokeCount
    hasExitGuard             = $hasExitGuard
    commandElementsExact     = $commandElementsExact
    devPreheatAtTopLevel     = $devPreheatAtTopLevel
    nextTopLevelIsExitGuard  = $nextTopLevelIsExitGuard
    guardConditionExact      = $guardConditionExact
    guardExitsOne            = $guardExitsOne
    preheatValid             = $preheatValid
} | ConvertTo-Json -Compress
"""


def _analyze_wrapper_source(source_text: str) -> dict[str, Any]:
    return _run_ps_analyzer(_WRAPPER_AST_ANALYZER, source_text)


def _analyze_ci_run_block(run_text: str) -> dict[str, Any]:
    return _run_ps_analyzer(
        _CI_PREHEAT_AST_ANALYZER, run_text, {"R16_REQUIRED_CMD": PREHEAT_REQUIRED_CMD}
    )


# ─────────────────────────────────────────────────────────────────────────────
# 四、durable-IO 两条 BLOCKED 回执证据结构 + validator 判定（直接调用生产实现）
# ─────────────────────────────────────────────────────────────────────────────
def _pwsh_build_durable_io_receipt(kind: str) -> dict[str, Any]:
    """dot-source 生产 helper _durable-io-receipt.ps1，构建指定 BLOCKED 回执并回传 JSON。
    kind ∈ {non_windows, toolchain}。用 _ps_host()（有 pwsh 用 pwsh）直接调用生产实现。"""
    ps = f"""
. '{DURABLE_IO_RECEIPT_PS1}'
$r = New-DurableIoBlockedReceipt -Kind '{kind}' -Detail 'regression-test'
$r | ConvertTo-Json -Depth 12
"""
    result = subprocess.run(
        [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 0, f"helper 调用失败 rc={result.returncode} stderr={result.stderr!r}"
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


@pytest.mark.skipif(sys.platform != "win32", reason="durable-io 回执 helper 需 Windows PowerShell")
@pytest.mark.parametrize(
    ("kind", "expected_subcheck"),
    [
        ("non_windows", "spike:windows_durable_io/non_windows_host"),
        ("toolchain", "spike:windows_durable_io/toolchain_unavailable"),
    ],
)
def test_durable_io_blocked_receipt_has_valid_evidence(kind: str, expected_subcheck: str) -> None:
    """两条 BLOCKED 回执必须：status=BLOCKED_UNCERTIFIED、assertions 非空、每条 passed
    为 JSON boolean false、observable_facts 非空、subcheckId 稳定。"""
    r = _pwsh_build_durable_io_receipt(kind)
    assert r["spike"] == "windows_durable_io"
    assert r["status"] == "BLOCKED_UNCERTIFIED"
    assert r.get("subcheckId") == expected_subcheck, (
        f"subcheckId 必须稳定为 '{expected_subcheck}'；实际 ={r.get('subcheckId')}"
    )
    asserts = r.get("assertions")
    assert isinstance(asserts, list) and len(asserts) >= 1, "assertions 必须非空"
    for a in asserts:
        assert a.get("passed") is False, (
            f"每条 assertion 的 passed 必须为 JSON boolean false；实际 ={a.get('passed')!r}"
        )
    facts = r.get("observable_facts")
    assert isinstance(facts, dict) and len(facts) >= 1, "observable_facts 必须非空"


def _pwsh_validate_receipt(receipt: dict[str, Any], env_compat: bool) -> dict[str, Any]:
    """把 receipt 写唯一临时文件，经生产 validator Test-SpikeReceiptEvidence 判定。"""
    tmp_dir = _unique_tmp_dir("validate")
    tmp = tmp_dir / f"receipt_{uuid.uuid4().hex[:8]}.json"
    try:
        tmp.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        env_flag = "$true" if env_compat else "$false"
        ps = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence -Name 'windows_durable_io' -Path '{tmp}' -EnvCompat {env_flag} -Allowlist @()
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=60,
        )
        out = result.stdout.strip()
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in out.split("|") if "=" in p}
        return {
            "ok": parts.get("ok", "").lower() == "true",
            "status": parts.get("status", "UNKNOWN"),
            "detail": parts.get("detail", out),
        }
    finally:
        _remove_tree_strict(tmp_dir)


@pytest.mark.skipif(sys.platform != "win32", reason="validator 需 Windows PowerShell")
def test_validator_rejects_core_durable_io_blocked_as_core_blocked_not_invalid() -> None:
    """durable-IO 是 core spike：其 BLOCKED 回执经 validator（EnvCompat=$false）必须被拒，
    且拒因是**合法 core BLOCKED**，而非 INVALID/missing 字段，也不得放行。"""
    r = _pwsh_build_durable_io_receipt("toolchain")
    verdict = _pwsh_validate_receipt(r, env_compat=False)
    st = verdict["status"]
    detail = verdict["detail"]
    assert not verdict["ok"], (
        f"core durable-io BLOCKED 必须被拒；实际 ok=True status={st} detail={detail}"
    )
    assert "core spike may not be BLOCKED_UNCERTIFIED" in detail, (
        f"拒因必须是合法 core BLOCKED（非 INVALID/missing 字段）；实际 status={st} detail={detail}"
    )
    assert "missing required field" not in detail, (
        f"回执不得再因缺字段被判 INVALID；detail={detail}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="validator 需 Windows PowerShell")
def test_validator_would_accept_durable_io_blocked_only_if_it_were_envcompat() -> None:
    """反向锚点：同一 BLOCKED 回执在 EnvCompat=$true 且 subcheckId 入 allowlist 时才会通过——
    证明拒绝**仅**因 core 身份（EnvCompat=$false），而非证据结构缺陷。
    durable-IO 实际禁止进 allowlist；此测试只用临时 allowlist 证明证据结构本身合法。"""
    r = _pwsh_build_durable_io_receipt("toolchain")
    tmp_dir = _unique_tmp_dir("envcompat")
    tmp = tmp_dir / f"receipt_{uuid.uuid4().hex[:8]}.json"
    try:
        tmp.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        ps = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence -Name 'windows_durable_io' -Path '{tmp}' -EnvCompat $true `
    -Allowlist @('spike:windows_durable_io/toolchain_unavailable')
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=60,
        )
        out = result.stdout.strip()
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in out.split("|") if "=" in p}
        ok = parts.get("ok", "").lower() == "true"
    finally:
        _remove_tree_strict(tmp_dir)
    assert ok, (
        "证据结构合法性反证失败：BLOCKED 回执在 EnvCompat=$true + allowlist 命中时应被接受 "
        f"（说明结构完整）；实际 detail={parts.get('detail')}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 五、P1-1：wrapper non-Windows 分支可达性（结构 + 运行时）
# ─────────────────────────────────────────────────────────────────────────────
def test_wrapper_source_guards_non_windows_before_any_d_reference() -> None:
    """P1-1（结构，跨平台）：non-Windows guard（含 exit）必须出现在任何 D 盘引用
    （字面 'D:\\'、Get-WorktreeTargetDir、dot-source _worktree-target.ps1、$dataRoot）之前，
    否则非 Windows 无 D: provider 时先抛 DriveNotFoundException，non-Windows 分支不可达。
    用去注释可执行代码做位置断言（非注释子串）。"""
    code = _strip_ps_comments(DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig"))
    m_guard = re.search(r"\$env:OS\s+-notmatch", code)
    assert m_guard is not None, "未找到 non-Windows guard 条件 $env:OS -notmatch"
    m_exit = re.search(r"exit\s+0", code[m_guard.start():])
    assert m_exit is not None, "non-Windows guard 分支必须 exit（提前退出）"
    guard_exit_pos = m_guard.start() + m_exit.start()
    d_refs: list[tuple[str, int]] = []
    for pat in (r"D:\\", r"Get-WorktreeTargetDir", r"_worktree-target\.ps1", r"\$dataRoot"):
        for mm in re.finditer(pat, code):
            d_refs.append((pat, mm.start()))
    assert d_refs, "预期 wrapper 含 D 盘引用（Windows 分支）"
    early = [(p, pos) for (p, pos) in d_refs if pos <= guard_exit_pos]
    assert not early, f"以下 D 盘引用出现在 non-Windows guard exit 之前（P1-1 回归）：{early}"


def test_wrapper_uses_cross_platform_default_receipt_path_without_override() -> None:
    """默认回执路径必须由 Join-Path 逐段构造，且生产 wrapper 不得保留任意环境变量覆写。

    这条静态锚点与后续真实 pwsh/non-Windows 执行测试配对：前者防止把测试隔离退化为
    任意路径绕过，后者防止反斜杠在非 Windows 被当作普通文件名而导致回执落错位置。
    """
    code = _strip_ps_comments(DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig"))
    assert re.search(
        r"\$probeDir\s*=\s*Join-Path\s+\$scriptRoot\s+['\"]tools/compat-probes/windows_durable_io['\"]",
        code,
    ), "probeDir 必须用 Join-Path 从仓库根构造跨平台路径"
    assert re.search(
        r"\$receiptPath\s*=\s*Join-Path\s+\$probeDir\s+['\"]receipt\.json['\"]",
        code,
    ), "receiptPath 必须用 Join-Path 从 probeDir 构造"
    assert "DURABLE_IO_RECEIPT_OVERRIDE" not in code, (
        "生产 wrapper 不得保留 DURABLE_IO_RECEIPT_OVERRIDE；测试必须使用隔离仓库而非任意路径覆写"
    )
    assert re.search(
        r'\.\s*\(Join-Path\s+\$scriptRoot\s+["\']scripts/spikes/_durable-io-receipt\.ps1["\']\)',
        code,
    ), "non-Windows 前置 helper 必须使用 / 子路径，不能把反斜杠当作文件名的一部分"


def _copy_isolated_durable_wrapper(root: Path) -> tuple[Path, Path]:
    """复制最小生产 wrapper/helper 到独立仓库根，默认回执只能落入该副本自己的 tools 目录。"""
    fake_repo = root / "isolated-repo"
    spike_dir = fake_repo / "scripts" / "spikes"
    expected = fake_repo / "tools" / "compat-probes" / "windows_durable_io" / "receipt.json"
    spike_dir.mkdir(parents=True, exist_ok=False)
    expected.parent.mkdir(parents=True, exist_ok=False)
    shutil.copy2(DURABLE_IO_WRAPPER_PS1, spike_dir / DURABLE_IO_WRAPPER_PS1.name)
    shutil.copy2(DURABLE_IO_RECEIPT_PS1, spike_dir / DURABLE_IO_RECEIPT_PS1.name)
    return spike_dir / DURABLE_IO_WRAPPER_PS1.name, expected


def _assert_valid_non_windows_durable_receipt(path: Path) -> None:
    """校验真实 non-Windows 分支生成的最小合法 core-BLOCKED 回执。"""
    assert path.exists(), f"默认回执必须落在隔离仓库的 tools 目录：{path}"
    receipt = json.loads(path.read_text(encoding="utf-8"))
    assert receipt["status"] == "BLOCKED_UNCERTIFIED"
    assert receipt["subcheckId"] == "spike:windows_durable_io/non_windows_host"
    assertions = receipt.get("assertions")
    assert isinstance(assertions, list) and assertions, "assertions 必须非空"
    assert all(item.get("passed") is False for item in assertions), (
        "每条 assertion passed 必须为 JSON boolean false"
    )


def test_wrapper_real_non_windows_pwsh_uses_default_repo_receipt_without_backslash_artifact() -> None:
    """在真正 non-Windows + pwsh 宿主执行生产 wrapper，验证默认 receipt 的仓库位置。

    Windows 上立即返回且不创建临时目录；Linux runner 必须真实执行 pwsh。
    """
    if sys.platform == "win32":
        return
    pwsh = shutil.which("pwsh")
    assert pwsh is not None, "non-Windows CI 必须提供 pwsh 以执行跨平台路径回归"
    isolated = Path(tempfile.mkdtemp(prefix="durable_non_windows_"))
    real_before = DURABLE_IO_WRAPPER_RECEIPT.read_bytes() if DURABLE_IO_WRAPPER_RECEIPT.exists() else None
    try:
        wrapper, expected = _copy_isolated_durable_wrapper(isolated)
        env = dict(os.environ)
        env["OS"] = ""
        env.pop("DURABLE_IO_RECEIPT_OVERRIDE", None)
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(wrapper)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(wrapper.parents[2]), timeout=120, env=env,
        )
        assert result.returncode == 0, (
            f"真实 non-Windows pwsh 分支应 exit 0；rc={result.returncode} stderr={result.stderr!r}"
        )
        _assert_valid_non_windows_durable_receipt(expected)
        backslash_artifact = Path(str(wrapper.parents[2]) + r"\tools\compat-probes\windows_durable_io\receipt.json")
        assert not backslash_artifact.exists(), f"不得产生含反斜杠的错误回执路径：{backslash_artifact}"
        if real_before is None:
            assert not DURABLE_IO_WRAPPER_RECEIPT.exists(), "真实持久 receipt 原先不存在时运行后仍必须不存在"
        else:
            assert DURABLE_IO_WRAPPER_RECEIPT.read_bytes() == real_before, "隔离运行不得改动真实持久 receipt"
    finally:
        _remove_tree_strict(isolated)


# ─────────────────────────────────────────────────────────────────────────────
# 六、P1-2：wrapper AST 锁定真实值流（正向 + mutation 实证）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_wrapper_ast_value_flow_locks_both_branches() -> None:
    """Blocker P1-2（正向）：AST 证明 wrapper (a) dot-source helper；
    (b) non-Windows 分支 -Kind non_windows、最后一次 $r 赋值来自 helper（无中途覆盖）、
        Write-Receipt 消费 $r；(c) build/linker 分支 -Kind toolchain、同样的值流。"""
    src = DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig")
    a = _analyze_wrapper_source(src)
    assert a["parseErrors"] == 0, f"wrapper 应无解析错误；parseErrors={a['parseErrors']}"
    assert a["dotSourcesHelper"] is True, "wrapper 必须 dot-source _durable-io-receipt.ps1"
    assert a["helperCallCount"] >= 2, (
        f"wrapper 至少两处调用 New-DurableIoBlockedReceipt；实际 ={a['helperCallCount']}"
    )
    # non-Windows 分支完整值流
    assert a["nonWindowsFound"] is True, "non-Windows 分支必须调用 helper"
    assert a["nonWindowsKind"] == "non_windows", (
        f"non-Windows 分支必须 -Kind non_windows；实际 ={a['nonWindowsKind']}"
    )
    assert a["nonWindowsLastRFromHelper"] is True, "non-Windows 分支最后一次 $r 赋值必须来自 helper（无覆盖）"
    assert a["nonWindowsWritesR"] is True, "non-Windows 分支必须 Write-Receipt $r"
    # 新增的顺序/精确参数锚点：不能只证明“某处出现过 helper 与 $r”。
    assert a["nonWindowsConditionExact"] is True
    assert a["nonWindowsHelperEndBeforeWrite"] is True
    assert a["nonWindowsWriteExactR"] is True
    assert a["nonWindowsNoIntermediateRMutation"] is True
    assert a["nonWindowsValueFlowValid"] is True
    # build/linker 分支完整值流
    assert a["buildFailFound"] is True, "build/linker 分支必须调用 helper"
    assert a["buildFailKind"] == "toolchain", (
        f"build/linker 分支必须 -Kind toolchain；实际 ={a['buildFailKind']}"
    )
    assert a["buildFailLastRFromHelper"] is True, "build/linker 分支最后一次 $r 赋值必须来自 helper（无覆盖）"
    assert a["buildFailWritesR"] is True, "build/linker 分支必须 Write-Receipt $r"
    assert a["buildFailConditionExact"] is True
    assert a["buildFailHelperEndBeforeWrite"] is True
    assert a["buildFailWriteExactR"] is True
    assert a["buildFailNoIntermediateRMutation"] is True
    assert a["buildFailValueFlowValid"] is True


# ── mutation 定义（每个把真实值流破坏一处，AST 必须捕获对应布尔翻转）────────────────
_NONWIN_CALL = (
    "$r = New-DurableIoBlockedReceipt -Kind non_windows "
    "-Detail ([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)"
)
_BUILDFAIL_CALL_RE = re.compile(
    r"\$r = New-DurableIoBlockedReceipt -Kind toolchain.*?"
    r"-ExtraFacts @\{ build_exit = \$buildExit; writer_exe = \$writerExe \}",
    re.S,
)
_LEGACY = '$r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"; reason="legacy" }'


def _mut_swap_kind(src: str) -> str:
    """交换两分支的 -Kind：non_windows <-> toolchain。"""
    s = src.replace("-Kind non_windows", "-Kind __SWAP__", 1)
    s = s.replace("-Kind toolchain", "-Kind non_windows", 1)
    return s.replace("-Kind __SWAP__", "-Kind toolchain", 1)


def _mut_overwrite_r_nonwin(src: str) -> str:
    """helper 调用后、Write-Receipt 前用旧 hashtable 覆盖 $r（值流被劫持）。"""
    return src.replace(_NONWIN_CALL, _NONWIN_CALL + "\n    " + _LEGACY, 1)


def _mut_wrong_var_nonwin(src: str) -> str:
    """non-Windows 分支 helper 结果赋给 $x（Write-Receipt $r 拿不到 helper 结果）。"""
    return src.replace(
        "$r = New-DurableIoBlockedReceipt -Kind non_windows",
        "$x = New-DurableIoBlockedReceipt -Kind non_windows",
        1,
    )


def _mut_write_before_helper_nonwin(src: str) -> str:
    """把唯一的 non-Windows 落盘写移到 helper 赋值之前，验证 EndOffset 顺序约束。"""
    return src.replace(
        _NONWIN_CALL + "\n    Write-Receipt $r",
        "Write-Receipt $r\n    " + _NONWIN_CALL,
        1,
    )


def _mut_member_rewrite_nonwin(src: str) -> str:
    """helper 与落盘写之间改写 $r 成员，不能让“最后赋值仍是 helper”掩盖劫持。"""
    return src.replace(_NONWIN_CALL, _NONWIN_CALL + "\n    $r.reason = 'tampered'", 1)


def _mut_legacy_nonwin(src: str) -> str:
    """non-Windows 分支整体退回手写旧回执（无 helper 调用）。"""
    return src.replace(_NONWIN_CALL, _LEGACY, 1)


def _mut_legacy_buildfail(src: str) -> str:
    """build/linker 分支整体退回手写旧回执（无 helper 调用）。"""
    return _BUILDFAIL_CALL_RE.sub(_LEGACY, src, count=1)


def _mut_write_before_helper_build(src: str) -> str:
    """把 build/linker 分支唯一的 Write-Receipt $r 移到 helper 赋值之前。"""
    match = _BUILDFAIL_CALL_RE.search(src)
    if match is None:
        return src
    following = re.match(r"\r?\n\s*Write-Receipt \$r", src[match.end():])
    if following is None:
        return src
    end = match.end() + following.end()
    return src[:match.start()] + "Write-Receipt $r\n    " + match.group(0) + src[end:]


def _mut_overwrite_r_build(src: str) -> str:
    """build/linker helper 后以旧 hashtable 覆盖 $r，验证中间赋值检查。"""
    return _BUILDFAIL_CALL_RE.sub(lambda m: m.group(0) + "\n    " + _LEGACY, src, count=1)


def _mut_wrong_var_build(src: str) -> str:
    """build/linker helper 的结果改赋给 $x，Write-Receipt $r 不再消费 helper 结果。"""
    return src.replace(
        "$r = New-DurableIoBlockedReceipt -Kind toolchain",
        "$x = New-DurableIoBlockedReceipt -Kind toolchain",
        1,
    )


def _mut_invert_build_condition(src: str) -> str:
    """反转 build 失败条件的退出码判断，验证条件 AST 不只按变量名匹配。"""
    return src.replace(
        "$buildExit -ne 0 -or -not (Test-Path $writerExe)",
        "$buildExit -eq 0 -or -not (Test-Path $writerExe)",
        1,
    )


def _mut_comment_dotsource(src: str) -> str:
    """注释掉 dot-source helper 那一行。"""
    out = []
    for ln in src.split("\n"):
        if ("_durable-io-receipt.ps1" in ln and ln.lstrip().startswith(".")
                and not ln.lstrip().startswith("#")):
            out.append("# " + ln)
        else:
            out.append(ln)
    return "\n".join(out)


def _chk_swap(a: dict[str, Any]) -> bool:
    # 交换后 non-Windows 分支不再是 non_windows（AST 捕获）
    return bool(a["nonWindowsKind"] != "non_windows")


def _chk_nonwin_broken(a: dict[str, Any]) -> bool:
    return a["nonWindowsLastRFromHelper"] is False


def _chk_nonwin_value_flow_broken(a: dict[str, Any]) -> bool:
    return a["nonWindowsValueFlowValid"] is False


def _chk_buildfail_broken(a: dict[str, Any]) -> bool:
    return a["buildFailLastRFromHelper"] is False


def _chk_build_value_flow_broken(a: dict[str, Any]) -> bool:
    return a["buildFailValueFlowValid"] is False


def _chk_build_condition_broken(a: dict[str, Any]) -> bool:
    return a["buildFailConditionExact"] is False


def _chk_no_dotsource(a: dict[str, Any]) -> bool:
    return a["dotSourcesHelper"] is False


_VALUE_FLOW_MUTATIONS: list[tuple[str, Callable[[str], str], Callable[[dict[str, Any]], bool]]] = [
    ("swap_kind", _mut_swap_kind, _chk_swap),
    ("overwrite_r_nonwin", _mut_overwrite_r_nonwin, _chk_nonwin_broken),
    ("wrong_var_nonwin", _mut_wrong_var_nonwin, _chk_nonwin_broken),
    ("write_before_helper_nonwin", _mut_write_before_helper_nonwin, _chk_nonwin_value_flow_broken),
    ("member_rewrite_nonwin", _mut_member_rewrite_nonwin, _chk_nonwin_value_flow_broken),
    ("legacy_nonwin", _mut_legacy_nonwin, _chk_nonwin_broken),
    ("legacy_buildfail", _mut_legacy_buildfail, _chk_buildfail_broken),
    ("write_before_helper_build", _mut_write_before_helper_build, _chk_build_value_flow_broken),
    ("overwrite_r_build", _mut_overwrite_r_build, _chk_build_value_flow_broken),
    ("wrong_var_build", _mut_wrong_var_build, _chk_build_value_flow_broken),
    ("invert_build_condition", _mut_invert_build_condition, _chk_build_condition_broken),
    ("comment_dotsource", _mut_comment_dotsource, _chk_no_dotsource),
]


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
@pytest.mark.parametrize(
    ("mut_id", "mutate", "check"), _VALUE_FLOW_MUTATIONS,
    ids=[m[0] for m in _VALUE_FLOW_MUTATIONS],
)
def test_wrapper_value_flow_mutation_is_caught(
    mut_id: str,
    mutate: Callable[[str], str],
    check: Callable[[dict[str, Any]], bool],
) -> None:
    """P1-2 mutation 实证：破坏真实值流（交换 Kind / 覆盖 $r / 错误变量 / 退回旧回执 /
    注释 dot-source）后，AST 分析必须捕获对应翻转——证明测试锁定的是真实值流而非表面出现。
    源码经 stdin 传入 ParseInput，不生成任何临时文件。"""
    src = DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig")
    mutated = mutate(src)
    assert mutated != src, f"mutation '{mut_id}' 未改动源码（锚点失配）"
    a = _analyze_wrapper_source(mutated)
    assert a["parseErrors"] == 0, f"mutation '{mut_id}' 后仍应可解析；parseErrors={a['parseErrors']}"
    assert check(a), f"mutation '{mut_id}' 未被 AST 捕获（P1-2 回归）：{a}"


# ─────────────────────────────────────────────────────────────────────────────
# 七、P1-3：CI 预热 AST 真实 CommandAst 检测（正向 + 未使用字符串 mutation）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_windows_probes_preheat_ast_requires_real_command() -> None:
    """P1-3（正向）：预热 run block 必须存在**真实 CommandAst** 调用 dev.ps1 且参数序列完整为
    `-- -- rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal`，
    并绑定 `if ($LASTEXITCODE -ne 0) { ... exit 1 }`。"""
    step = _preheat_step()
    a = _analyze_ci_run_block(str(step.get("run", "")))
    assert a["parseErrors"] == 0, f"预热 run block 应可解析；parseErrors={a['parseErrors']}"
    assert a["devInvokes"] is True, (
        "预热必须有真实 CommandAst 调用 dev.ps1 且参数序列完整（非未使用字符串/注释）"
    )
    assert a["devInvokeCount"] >= 1
    assert a["hasExitGuard"] is True, "必须有 if ($LASTEXITCODE -ne 0) 守卫"
    # 顺序及 token 级锚点：一个包含相同文本的命令或过早守卫都不能通过。
    assert a["commandElementsExact"] is True
    assert a["devPreheatAtTopLevel"] is True
    assert a["nextTopLevelIsExitGuard"] is True
    assert a["guardConditionExact"] is True
    assert a["guardExitsOne"] is True
    assert a["preheatValid"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_preheat_ast_immune_to_unused_string_mutation() -> None:
    """P1-3 mutation 实证：把真实 dev.ps1 调用行注释掉 + 追加含完整命令的**未使用字符串**后，
    AST 必须报 devInvokes=False；同时 guard 中嵌套/字符串形式的 ``exit 1`` 也不得冒充直接失败退出。"""
    step = _preheat_step()
    run = str(step.get("run", ""))
    unused = (
        '$unused = "& $PWD/scripts/dev.ps1 -- -- rustup toolchain install '
        'stable-x86_64-pc-windows-gnu --profile minimal"'
    )
    mutated_lines: list[str] = []
    did = False
    for ln in run.split("\n"):
        if "dev.ps1" in ln and "rustup" in ln and not ln.lstrip().startswith("#"):
            mutated_lines.append("# " + ln)
            mutated_lines.append(unused)
            did = True
        else:
            mutated_lines.append(ln)
    assert did, "未能定位真实预热命令行以施加 mutation"
    a = _analyze_ci_run_block("\n".join(mutated_lines))
    assert a["parseErrors"] == 0, f"mutation 后仍应可解析；parseErrors={a['parseErrors']}"
    assert a["devInvokes"] is False, (
        "注释真实调用后仅剩未使用字符串仍判 devInvokes=True——AST 未验证真实 CommandAst（P1-3 回归）"
    )

    # 守卫体中嵌套的 exit 即使文本匹配，也不是 $LASTEXITCODE 失败时的直接顶层退出。
    nested_exit = run.replace("exit 1", "if ($false) { exit 1 }", 1)
    assert nested_exit != run, "未能定位 CI 预热 guard 的直接 exit 1 以施加嵌套 mutation"
    nested = _analyze_ci_run_block(nested_exit)
    assert nested["parseErrors"] == 0, f"嵌套 exit mutation 后仍应可解析；结果={nested}"
    assert nested["guardExitsOne"] is False and nested["preheatValid"] is False, (
        "if ($false) { exit 1 } 不能作为 guard 的直接失败退出——AST 未锁定顶层 ExitStatementAst"
    )

    # 未使用字符串中的 exit 1 也必须被 AST 排除，不能再靠守卫正文正则放行。
    string_exit = run.replace("exit 1", '$unusedExitText = "exit 1"', 1)
    assert string_exit != run, "未能定位 CI 预热 guard 的直接 exit 1 以施加字符串 mutation"
    string_only = _analyze_ci_run_block(string_exit)
    assert string_only["parseErrors"] == 0, f"字符串 exit mutation 后仍应可解析；结果={string_only}"
    assert string_only["guardExitsOne"] is False and string_only["preheatValid"] is False, (
        "未使用字符串中的 exit 1 不能作为 guard 的直接失败退出——AST 未锁定常量 ExitStatementAst"
    )


_PREHEAT_COMMAND = (
    '& "$PWD/scripts/dev.ps1" -- -- rustup toolchain install '
    'stable-x86_64-pc-windows-gnu --profile minimal'
)


def _mut_preheat_single_string_argument(run: str) -> str:
    """把独立参数合并成一个字符串，验证 CommandElements 必须逐项精确匹配。"""
    return run.replace(
        _PREHEAT_COMMAND,
        '& "$PWD/scripts/dev.ps1" "-- -- rustup toolchain install '
        'stable-x86_64-pc-windows-gnu --profile minimal"',
        1,
    )


def _mut_preheat_guard_before_call(run: str) -> str:
    """把守卫整体移动到调用之前，验证不能只在同一 run block 内搜到 guard。"""
    lines = run.split("\n")
    command_index = next((i for i, line in enumerate(lines) if _PREHEAT_COMMAND in line), None)
    guard_index = next(
        (i for i, line in enumerate(lines) if "if ($LASTEXITCODE -ne 0)" in line), None
    )
    if command_index is None or guard_index is None:
        return run
    guard_end = next(
        (i for i in range(guard_index + 1, len(lines)) if lines[i].strip() == "}"), None
    )
    if guard_end is None:
        return run
    command = lines[command_index]
    guard = lines[guard_index:guard_end + 1]
    return "\n".join(
        lines[:command_index] + guard + lines[command_index + 1:guard_index] + [command] + lines[guard_end + 1:]
    )


def _mut_preheat_intervening_native_command(run: str) -> str:
    """在 dev 调用与守卫之间插入原生命令，模拟污染 $LASTEXITCODE 的回归。"""
    return run.replace(_PREHEAT_COMMAND, _PREHEAT_COMMAND + "\n          & cmd /c exit 0", 1)


def _chk_preheat_token_boundary(a: dict[str, Any]) -> bool:
    return a["commandElementsExact"] is False and a["preheatValid"] is False


def _chk_preheat_not_immediately_guarded(a: dict[str, Any]) -> bool:
    return a["nextTopLevelIsExitGuard"] is False and a["preheatValid"] is False


_PREHEAT_ORDER_MUTATIONS: list[tuple[str, Callable[[str], str], Callable[[dict[str, Any]], bool]]] = [
    ("single_string_argument", _mut_preheat_single_string_argument, _chk_preheat_token_boundary),
    ("guard_before_call", _mut_preheat_guard_before_call, _chk_preheat_not_immediately_guarded),
    ("intervening_native_command", _mut_preheat_intervening_native_command, _chk_preheat_not_immediately_guarded),
]


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
@pytest.mark.parametrize(
    ("mut_id", "mutate", "check"), _PREHEAT_ORDER_MUTATIONS,
    ids=[m[0] for m in _PREHEAT_ORDER_MUTATIONS],
)
def test_preheat_ast_rejects_token_and_order_mutations(
    mut_id: str,
    mutate: Callable[[str], str],
    check: Callable[[dict[str, Any]], bool],
) -> None:
    """P1-3 mutation 实证：参数折叠、守卫提前、原生命令插队都必须让预热合同失效。"""
    run = str(_preheat_step().get("run", ""))
    mutated = mutate(run)
    assert mutated != run, f"mutation '{mut_id}' 未改动预热 run block（锚点失配）"
    a = _analyze_ci_run_block(mutated)
    assert a["parseErrors"] == 0, f"mutation '{mut_id}' 后仍应可解析；parseErrors={a['parseErrors']}"
    assert check(a), f"mutation '{mut_id}' 未被 AST 捕获（P1-3 回归）：{a}"


# ─────────────────────────────────────────────────────────────────────────────
# 八、P1-4：唯一临时目录（D 盘 DataRoot、worktree/进程/随机唯一、绝不落 C 盘）
# ─────────────────────────────────────────────────────────────────────────────
_TEST_DATA_ROOT_ENV = "FACTORY_TEST_DATA_ROOT"
_APPROVED_D_TEST_PREFIX = Path(r"D:\codex项目")


def _validate_d_test_root(candidate: Path) -> Path:
    """在任何 mkdir/写入前验证测试根：仅允许 D:\\codex项目 下的非 reparse 物理路径。"""
    raw = os.fspath(candidate)
    path = Path(raw)
    if not path.is_absolute() or path.drive.casefold() != "d:":
        raise RuntimeError(f"测试 DataRoot 必须是 D 盘绝对路径，实际为：{raw}")
    if any(part == ".." for part in path.parts):
        raise RuntimeError(f"测试 DataRoot 不得包含 '..'：{raw}")

    # 先逐层检查已存在组件，拒绝 symlink/junction；未存在叶子仅在全部前置检查完成后创建。
    current = Path(path.anchor)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    for part in path.parts[1:]:
        current = current / part
        if not current.exists():
            break
        attrs = getattr(os.lstat(current), "st_file_attributes", 0)
        if current.is_symlink() or bool(attrs & reparse_flag):
            raise RuntimeError(f"测试 DataRoot 不得穿越 reparse/junction：{current}")

    resolved = path.resolve(strict=False)
    approved = _APPROVED_D_TEST_PREFIX.resolve(strict=False)
    resolved_text = str(resolved).rstrip("\\/").casefold()
    approved_text = str(approved).rstrip("\\/").casefold()
    if resolved_text != approved_text and not resolved_text.startswith(approved_text + "\\"):
        raise RuntimeError(
            f"测试 DataRoot 必须位于批准根 '{approved}' 内，实际为：{resolved}"
        )
    if resolved.drive.casefold() != "d:":
        raise RuntimeError(f"测试 DataRoot 物理解析后离开 D 盘：{resolved}")
    return resolved


def _data_root() -> Path:
    """定位 <PROJECT_ROOT>\\AI-Coding-Factory-Data\\dev（向上找 AI-Coding-Factory-Data 祖先）。
    找不到时 fail-closed；绝不回退 ambient TEMP。需要隔离根时只能显式给出经验证的
    FACTORY_TEST_DATA_ROOT，且物理路径必须位于批准的 D:\\codex项目 根内。"""
    override = os.environ.get(_TEST_DATA_ROOT_ENV)
    if override:
        return _validate_d_test_root(Path(override))

    cur = REPO_ROOT
    for _ in range(8):
        cand = cur / "AI-Coding-Factory-Data"
        if cand.exists():
            return _validate_d_test_root(cand / "dev")
        if cur.parent == cur:
            break
        cur = cur.parent
    raise RuntimeError(
        "未找到批准的 D 盘 AI-Coding-Factory-Data；请显式设置经过校验的 FACTORY_TEST_DATA_ROOT"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘测试根校验是 Windows 存储合同")
def test_windows_data_root_rejects_missing_approved_root_instead_of_ambient_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 找不到项目批准 DataRoot 时必须失败，绝不能静默回退 ambient TEMP（可能是 C 盘）。"""
    monkeypatch.delenv("FACTORY_TEST_DATA_ROOT", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", Path(r"D:\\__missing_factory_root__"))
    with pytest.raises(RuntimeError, match="DataRoot|D 盘|D:"):
        _data_root()


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘测试根校验是 Windows 存储合同")
def test_windows_data_root_rejects_unapproved_override_before_any_mkdir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FACTORY_TEST_DATA_ROOT 若指向 C 盘必须在创建目录前拒绝，避免测试路径越界。"""
    monkeypatch.setenv("FACTORY_TEST_DATA_ROOT", r"C:\\must-not-write")
    with pytest.raises(RuntimeError, match="D 盘|C:"):
        _data_root()


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘测试根校验是 Windows 存储合同")
@pytest.mark.parametrize(
    "override",
    [r"D:\outside-approved-root", r"D:\codex项目\candidate\..\escape"],
    ids=["outside-approved-d-root", "parent-traversal"],
)
def test_windows_data_root_rejects_outside_or_parent_traversal_override(
    monkeypatch: pytest.MonkeyPatch,
    override: str,
) -> None:
    """显式测试根不能越过批准 D 根，也不能用 .. 在校验后绕回其他位置。"""
    monkeypatch.setenv(_TEST_DATA_ROOT_ENV, override)
    with pytest.raises(RuntimeError, match="批准根|不得包含"):
        _data_root()


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘测试根校验是 Windows 存储合同")
def test_windows_data_root_rejects_reparse_component_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟文件系统的 reparse flag；无需创建需要特权的真实 junction 也要覆盖拒绝分支。"""
    existing = {r"d:\codex项目", r"d:\codex项目\reparse"}

    def fake_exists(path: Path) -> bool:
        return str(path).replace("/", "\\").rstrip("\\").casefold() in existing

    fake_stat = type("ReparseStat", (), {"st_file_attributes": 0x0400})()
    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "is_symlink", lambda _path: False)
    monkeypatch.setattr(os, "lstat", lambda _path: fake_stat)
    with pytest.raises(RuntimeError, match="reparse/junction"):
        _validate_d_test_root(Path(r"D:\codex项目\reparse\child"))


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘临时目录清理合同在 Windows 验证")
def test_strict_temp_cleanup_propagates_failure_and_finally_removes_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清理失败必须冒泡，不能用 ignore_errors 假绿；恢复后 finally 路径必须确认目录不存在。"""
    owned = _unique_tmp_dir("cleanup-strict")
    try:
        def denied(_path: Path) -> None:
            raise OSError("simulated cleanup denial")

        monkeypatch.setattr(shutil, "rmtree", denied)
        with pytest.raises(OSError, match="cleanup denial"):
            _remove_tree_strict(owned)
        assert owned.exists(), "模拟清理失败后目录应仍存在，不能误报已清理"
    finally:
        monkeypatch.undo()
        _remove_tree_strict(owned)


def _worktree_digest(root_str: str) -> str:
    """规范化 worktree 根路径的 SHA-256 前 12 hex——不同 worktree 派生不同临时目录，绝不共享。"""
    import hashlib
    return hashlib.sha256(root_str.encode("utf-8")).hexdigest()[:12]


def _unique_tmp_dir(tag: str) -> Path:
    """在 D 盘 DataRoot\\tmp 下创建 worktree/进程/uuid 唯一子目录（并发/xdist 不互撞）。
    调用方必须在 finally 调用 _remove_tree_strict；清理失败会失败，且 helper 断言目录不存在。"""
    root = _data_root() / "phase0-contract-tests"
    root = _validate_d_test_root(root)
    # 共享父目录允许并发调用共同创建；预验证后以 exist_ok=True 消除 exists()+mkdir 的 TOCTOU，
    # 并立即再次验证真实链路，防止创建窗口被 junction/reparse 偷换。
    root.mkdir(parents=True, exist_ok=True)
    root = _validate_d_test_root(root)
    # 只有本调用拥有的 UUID 叶目录必须拒绝复用，避免不同测试/xdist 互相覆盖。
    uniq = f"_r16_{tag}_{_worktree_digest(str(REPO_ROOT))}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    d = root / uniq
    _validate_d_test_root(d)
    d.mkdir(exist_ok=False)
    return d


def _remove_tree_strict(path: Path) -> None:
    """严格清理本测试自己创建的唯一目录；清理失败必须让测试失败，避免假绿残留。"""
    if path.exists():
        shutil.rmtree(path)
    assert not path.exists(), f"临时目录清理后仍存在：{path}"


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘 DataRoot 唯一性是 Windows 存储合同")
def test_temp_paths_are_unique_per_worktree_and_off_c_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-4：不同 worktree 派生不同临时目录 digest（不共享）；同一 worktree 两次分配也不撞
    （uuid/pid）；所有临时目录落在 D 盘 DataRoot 下、绝不落 C 盘。"""
    d1 = _worktree_digest(r"D:\codex项目\.codex-worktrees\AI-Coding-Factory\wt-A")
    d2 = _worktree_digest(r"D:\codex项目\.codex-worktrees\AI-Coding-Factory\wt-B")
    assert d1 != d2, "不同 worktree 必须派生不同临时目录 digest（不得共享临时路径）"
    a = _unique_tmp_dir("uniqtest")
    b: Path | None = None
    try:
        b = _unique_tmp_dir("uniqtest")
        assert a != b, "同一 worktree 两次分配必须得到不同目录（uuid 唯一）"
        data_root = str(_data_root()).replace("/", "\\").lower()
        for d in (a, b):
            rp = str(d).replace("/", "\\").lower()
            assert not rp.startswith("c:\\"), f"临时目录不得落在 C 盘：{d}"
            assert rp.startswith(data_root), f"临时目录必须在 D 盘 DataRoot 下：{d}"
    finally:
        for d in (a, b):
            if d is not None:
                _remove_tree_strict(d)

    # 首次创建共享父目录时，8 个并发调用都必须成功；只有 UUID 叶目录允许 exist_ok=False。
    # 使用独占 D 盘测试根保证 shared parent 起初不存在，避免已有目录掩盖 TOCTOU。
    parallel_root = _data_root() / f"_r16_parallel_root_{uuid.uuid4().hex[:8]}"
    allocated: list[Path] = []
    _validate_d_test_root(parallel_root)
    parallel_root.mkdir(exist_ok=False)
    monkeypatch.setenv(_TEST_DATA_ROOT_ENV, str(parallel_root))
    barrier = threading.Barrier(8)

    def allocate_after_barrier() -> Path:
        barrier.wait(timeout=15)
        return _unique_tmp_dir("parallel")

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(allocate_after_barrier) for _ in range(8)]
            allocated = [future.result(timeout=30) for future in futures]
        assert len(allocated) == 8 and len(set(allocated)) == 8, (
            f"8 线程首次分配必须全成功且路径唯一：{allocated}"
        )
        assert all(path.exists() for path in allocated), "每个并发分配目录都必须真实创建"
    finally:
        for path in allocated:
            _remove_tree_strict(path)
        _remove_tree_strict(parallel_root)


# ─────────────────────────────────────────────────────────────────────────────
# 九、P2-1：新 helper 静态接线 + 动态 fail-closed 回执反查门存在
# ─────────────────────────────────────────────────────────────────────────────
def test_durable_io_helper_wired_into_script_digests() -> None:
    """P2-1（静态接线）：phase0-acceptance.ps1 的 $scriptDigests（去注释可执行代码）必须把键
    'spikes/_durable-io-receipt.ps1' 接到 Get-FileSha256(Join-Path $PSScriptRoot ...)——
    运行时会对该 helper 真实文件算 SHA256 写入总回执（非注释、非硬编码）。"""
    code = _strip_ps_comments(ACCEPTANCE_PS1.read_text(encoding="utf-8"))
    m = re.search(r"\$scriptDigests\s*=\s*\[ordered\]@\{(.*?)\n\s*\}", code, re.S)
    assert m is not None, "phase0-acceptance.ps1 未找到 $scriptDigests [ordered]@{...} 块"
    block = m.group(1)
    keys = re.findall(r'^\s*"([^"]+)"\s*=', block, re.M)
    assert keys == [
        "phase0-acceptance.ps1", "check.ps1", "dev.ps1", "_python-probe.ps1",
        "_worktree-target.ps1", "spikes/_durable-io-receipt.ps1",
    ], "scriptDigests 必须保持冻结的 6 个生产脚本，不能删减或无界扩大绑定范围"
    wired = re.search(
        r'"spikes/_durable-io-receipt\.ps1"\s*=\s*Get-FileSha256\s*\(\s*'
        r'Join-Path\s+\$PSScriptRoot\s+"spikes/_durable-io-receipt\.ps1"\s*\)',
        block,
    )
    assert wired is not None, (
        "scriptDigests 必须把 'spikes/_durable-io-receipt.ps1' 接到 "
        "Get-FileSha256(Join-Path $PSScriptRoot \"spikes/_durable-io-receipt.ps1\")（去注释代码解析）"
    )


def test_acceptance_has_dynamic_receipt_digest_gate() -> None:
    """动态反查必须先在 verified provisional 回执上完成，再作为正式 check 写入最终回执。

    测试结构保证：缺字段/缺键/错摘要都先令 script-digests=false，随后才原子发布 FAIL；
    发布失败还会删除旧 final，不能遗留历史 PASS。
    """
    # 这里刻意验证源码交付约束：PS 5.1 需要 BOM 才能可靠读取同文件中的中文审计说明。
    raw = ACCEPTANCE_PS1.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        "phase0-acceptance.ps1 必须以 UTF-8 BOM 保存，供 Windows PowerShell 5.1 识别中文注释"
    )
    source = raw.decode("utf-8-sig")
    for required_comment in (
        "回执发布职责",
        "摘要反查失败",
        "受控 provisional",
        "同卷原子替换",
        "失败清理",
    ):
        assert required_comment in source, f"缺少动态回执安全规则的中文说明：{required_comment}"
    assert re.search(r"(?i)(?:pure[- ]ascii|ascii-only).{0,64}bom[- ]less", source) is None, (
        "UTF-8 BOM 脚本不得残留 pure-ASCII/ASCII-only 与 BOM-less 组合的过期说明"
    )
    code = _strip_ps_comments(source)
    assert re.search(r"function\s+Publish-AcceptanceReceipt\b", code), (
        "动态反查与发布必须收敛为 Publish-AcceptanceReceipt，避免先写 final PASS"
    )
    assert re.search(r"\$writtenReceipt\s*=\s*Get-Content\s+-LiteralPath\s+\$provisionalFull\s+-Raw", code), (
        "动态门必须回读 verified provisional 回执，而不是先发布最终文件"
    )
    # 逐脚本重算当前磁盘文件的真实 SHA256（遍历 ScriptDigests.Keys → Get-FileSha256 digestPath）。
    assert re.search(r"foreach\s*\(\s*\$digestKey\s+in\s+\$ScriptDigests\.Keys\s*\)", code), (
        "动态门必须遍历 ScriptDigests.Keys 逐脚本反查"
    )
    assert re.search(r"\$realDigest\s*=\s*Get-FileSha256\s+\$digestPath", code), (
        "动态门必须对每个键重算当前磁盘文件真实 SHA256"
    )
    # 比对回执绑定值 != 当前真实 SHA256 → fail-closed。
    assert re.search(r"\$boundDigest\s+-ne\s+\$realDigest", code), (
        "动态门必须比对回执绑定值 != 当前文件真实 SHA256"
    )
    # helper 键必须在动态反查名单内（即 $scriptDigests 含该键，遍历时会反查它）。
    assert '"spikes/_durable-io-receipt.ps1"' in code, "动态门反查名单必须含 durable-io helper 键"
    # 反查不是写盘后的孤立 exit：必须被记录为正式 check，重算顶层状态后才发布最终回执。
    assert re.search(r'Add-CheckResult\s+"script-digests"', code), (
        "scriptDigests 反查必须作为正式 check 写入 results，不能只在最终回执写盘后直接 exit"
    )
    assert "Publish-AcceptanceReceipt" in code, (
        "最终回执必须经受控 provisional 校验后原子发布，避免动态反查失败留下 PASS 文件"
    )
    assert "[System.IO.File]::Replace($provisionalFull, $OutFull, $backupFull)" in code, (
        "已有最终回执时必须以具名受控备份调用 File.Replace 原子发布 verified provisional"
    )
    assert "[System.IO.File]::Move($provisionalFull, $OutFull)" in code, (
        "首次发布必须从 verified provisional 原子移动到最终路径"
    )
    assert "Remove-Item -LiteralPath $OutFull -Force -ErrorAction Stop" in code, (
        "原子发布失败时必须移除旧 final，避免残留历史 PASS"
    )


def _create_windows_junction(link: Path, target: Path) -> None:
    """创建真实 D 盘 junction，供发布器验证已存在 reparse 链而非 mock 检查器。"""
    assert link.parent.exists() and target.exists(), "junction 的父目录和目标必须先真实存在"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, (
        f"真实 junction 创建失败；rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    attrs = getattr(os.lstat(link), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    assert link.exists() and bool(attrs & reparse_flag), "测试夹具必须是实际 reparse/junction"


def _remove_junction_strict(link: Path) -> None:
    """只移除测试拥有的 junction 本身，绝不递归删除其指向的外部目标。"""
    if os.path.lexists(link):
        os.rmdir(link)
    assert not os.path.lexists(link), f"junction 清理后仍存在：{link}"


def _run_script_digest_publish_case(
    case: str,
    *,
    existing_final: bool = False,
    missing_final_parent: bool = False,
    junction_escape: bool = False,
) -> tuple[int, dict[str, Any]]:
    """从生产脚本 AST 提取发布函数，在 D 盘隔离目录复放发布和真实 junction 拒绝路径。"""
    d = _unique_tmp_dir(f"digest-{case}")
    project_root = d / "project"
    script_root = project_root / "scripts"
    data_root = d / "data-root"
    provisional_dir = data_root / "tmp"
    project_root.mkdir(exist_ok=False)
    script_root.mkdir(exist_ok=False)
    provisional_dir.mkdir(parents=True, exist_ok=False)
    bound = script_root / "bound.ps1"
    bound.write_text("# digest binding fixture\n", encoding="utf-8")
    escape_link: Path | None = None
    if junction_escape:
        # lexical 路径仍在 project 内，但真实目标位于 project 外；两侧仍受 D 测试根约束。
        escape_target = d / "outside"
        escape_target.mkdir(exist_ok=False)
        escape_link = project_root / "escape"
        _create_windows_junction(escape_link, escape_target)
        final = escape_link / "final-receipt.json"
    else:
        final = (
            project_root / "missing-final-parent" / "final-receipt.json"
            if missing_final_parent
            else project_root / "final-receipt.json"
        )
    existing_final_ps = "$true" if existing_final else "$false"
    watch_source = "$true" if junction_escape else "$false"
    event_source = f"phase0-provisional-{uuid.uuid4().hex}"
    try:
        ps = f"""
$ErrorActionPreference = 'Stop'
$source = Get-Content -LiteralPath '{ACCEPTANCE_PS1}' -Raw -Encoding utf8
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
if (@($errors).Count -ne 0) {{ throw 'phase0 source parse failed' }}
$definitions = @($ast.FindAll(
    {{ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $args[0].Name -eq 'Publish-AcceptanceReceipt' }},
    $true
))
$definition = $definitions[0]
if ($null -eq $definition) {{ throw 'Publish-AcceptanceReceipt missing' }}
$reparseDefinitions = @($ast.FindAll(
    {{ $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $args[0].Name -eq 'Test-ReparsePointInChain' }},
    $true
))
$reparseDefinition = $reparseDefinitions[0]
if ($null -eq $reparseDefinition) {{ throw 'Test-ReparsePointInChain missing' }}
. ([scriptblock]::Create($reparseDefinition.Extent.Text))
. ([scriptblock]::Create($definition.Extent.Text))
function Get-FileSha256 {{
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {{ return $null }}
    return 'sha256:' + (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
}}
$global:results = [System.Collections.Generic.List[object]]::new()
$global:PROJECT_ROOT = '{d}'
$global:REPO_ROOT = '{project_root}'
function Add-CheckResult {{
    param([string]$Id, [string]$Desc, [bool]$Passed, [string]$Detail)
    $entry = [ordered]@{{
        id=$Id; desc=$Desc; exitCode=$(if ($Passed) {{0}} else {{1}})
        passed=$Passed; tail=$Detail
    }}
    $global:results.Add($entry)
}}
$scriptRoot = '{script_root}'
$boundPath = Join-Path $scriptRoot 'bound.ps1'
$actual = Get-FileSha256 $boundPath
$digests = [ordered]@{{ 'bound.ps1' = $actual }}
$receipt = [ordered]@{{ topStatus='PASS'; checks=@() }}
switch ('{case}') {{
    'missing_field' {{ }}
    'missing_key' {{ $receipt['scriptDigests'] = [ordered]@{{}} }}
    'wrong_digest' {{ $receipt['scriptDigests'] = [ordered]@{{ 'bound.ps1' = ('sha256:' + ('0' * 64)) }} }}
    'valid' {{ $receipt['scriptDigests'] = [ordered]@{{ 'bound.ps1' = $actual }} }}
    default {{ throw 'unknown case' }}
}}
$existingFinal = {existing_final_ps}
if ($existingFinal) {{
    $oldReceipt = '{{"topStatus":"PASS","origin":"old"}}'
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText('{final}', $oldReceipt, $utf8NoBom)
}}
$watcher = $null
$eventSource = '{event_source}'
if ({watch_source}) {{
    $watcher = New-Object System.IO.FileSystemWatcher '{provisional_dir}', '*.provisional.json'
    $watcher.EnableRaisingEvents = $true
    $null = Register-ObjectEvent -InputObject $watcher -EventName Created -SourceIdentifier $eventSource
}}
$priorErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$publishException = ''
try {{
    $published = Publish-AcceptanceReceipt `
        -Receipt $receipt -ScriptDigests $digests -ScriptRoot $scriptRoot `
        -OutFull '{final}' -VerifiedProvisionalDir '{provisional_dir}' -DataRoot '{data_root}'
}} catch {{
    $publishException = $_.Exception.Message
    $published = [ordered]@{{ topStatus='FAIL'; published=$false }}
}}
$ErrorActionPreference = $priorErrorActionPreference
if ($null -ne $watcher) {{
    Start-Sleep -Milliseconds 250
    $provisionalEvents = @(Get-Event -SourceIdentifier $eventSource -ErrorAction SilentlyContinue).Count
    Unregister-Event -SourceIdentifier $eventSource -ErrorAction SilentlyContinue
    Get-Event -SourceIdentifier $eventSource -ErrorAction SilentlyContinue | Remove-Event -ErrorAction SilentlyContinue
    $watcher.Dispose()
}} else {{
    $provisionalEvents = 0
}}
$provisionalFiles = @(
    Get-ChildItem -LiteralPath '{provisional_dir}' -Filter '*.provisional.json' -File -ErrorAction SilentlyContinue
)
$backupFiles = @(
    Get-ChildItem -LiteralPath '{provisional_dir}' -Filter '*.replace-backup.json' -File -ErrorAction SilentlyContinue
)
$payload = [ordered]@{{
    topStatus = [string]$published.topStatus
    published = [bool]$published.published
    finalExists = (Test-Path -LiteralPath '{final}')
    provisionalLeft = $provisionalFiles.Count
    backupLeft = $backupFiles.Count
    provisionalEvents = $provisionalEvents
    publishException = $publishException
}}
if ($payload.finalExists) {{
    $payload['finalReceipt'] = Get-Content -LiteralPath '{final}' -Raw -Encoding utf8 | ConvertFrom-Json
}}
$payload | ConvertTo-Json -Depth 12 -Compress
if ($published.topStatus -eq 'PASS') {{ exit 0 }}
exit 1
"""
        result = subprocess.run(
            [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert result.stdout.strip(), (
            f"回执失败语义 harness 未输出 JSON；stderr={result.stderr!r}"
        )
        return result.returncode, json.loads(result.stdout)
    finally:
        if escape_link is not None:
            _remove_junction_strict(escape_link)
        _remove_tree_strict(d)


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘原子回执发布语义需 Windows PowerShell")
@pytest.mark.parametrize(
    ("case", "covers_publish_paths"),
    [("missing_field", True), ("missing_key", False), ("wrong_digest", False)],
)
def test_script_digest_publish_contract(case: str, covers_publish_paths: bool) -> None:
    """动态摘要失败不得发布 PASS；同一合同还覆盖原子发布、真实 junction 拒绝与严格清理。"""
    rc, payload = _run_script_digest_publish_case(case)
    assert rc == 1, f"{case} 动态 digest 失败必须 exit 1；实际 rc={rc} payload={payload}"
    assert payload["topStatus"] == "FAIL", f"{case} 失败时发布器必须返回 FAIL：{payload}"
    assert payload["finalExists"] is True, f"{case} 失败时应发布可审计 FAIL 回执，而非遗留旧 PASS"
    final_receipt = payload["finalReceipt"]
    assert final_receipt["topStatus"] == "FAIL", f"{case} 最终回执不得残留 PASS：{final_receipt}"
    digest_check = next(item for item in final_receipt["checks"] if item["id"] == "script-digests")
    assert digest_check["passed"] is False, f"{case} script-digests 正式 check 必须失败：{digest_check}"
    assert payload["provisionalLeft"] == 0, f"{case} 完成后不得残留 provisional：{payload}"
    assert payload["backupLeft"] == 0, f"{case} 完成后不得残留 replace backup：{payload}"

    if covers_publish_paths:
        rc, no_final = _run_script_digest_publish_case("valid")
        assert rc == 0 and no_final["published"] is True, f"首次发布必须成功：{no_final}"
        assert no_final["finalExists"] is True, f"首次发布必须产生最终回执：{no_final}"
        assert no_final["finalReceipt"]["topStatus"] == "PASS", f"首次发布回执必须为 PASS：{no_final}"

        rc, existing_final = _run_script_digest_publish_case("valid", existing_final=True)
        assert rc == 0 and existing_final["published"] is True, (
            f"已有 final 时必须同卷原子替换并成功：{existing_final}"
        )
        assert existing_final["finalExists"] is True, f"替换后最终回执不得丢失：{existing_final}"
        assert existing_final["finalReceipt"]["topStatus"] == "PASS", (
            f"替换后最终回执必须为本次 PASS：{existing_final}"
        )
        assert "origin" not in existing_final["finalReceipt"], (
            f"替换后不得继续读取预存 final 内容：{existing_final}"
        )
        assert existing_final["provisionalLeft"] == 0 and existing_final["backupLeft"] == 0, (
            f"原子替换成功后临时文件必须清理：{existing_final}"
        )

        rc, publish_failure = _run_script_digest_publish_case("valid", missing_final_parent=True)
        assert rc == 1 and publish_failure["published"] is False, (
            f"发布目标目录缺失时必须 fail-closed：{publish_failure}"
        )
        assert publish_failure["topStatus"] == "FAIL", f"发布异常必须返回 FAIL：{publish_failure}"
        assert publish_failure["finalExists"] is False, f"发布异常不得遗留最终 PASS：{publish_failure}"
        assert publish_failure["provisionalLeft"] == 0 and publish_failure["backupLeft"] == 0, (
            f"发布异常后临时文件必须清理：{publish_failure}"
        )

        rc, junction_escape = _run_script_digest_publish_case("valid", junction_escape=True)
        assert rc == 1 and junction_escape["published"] is False, (
            f"OutFull 经真实 D 盘 junction 逃逸时必须在发布前 fail-closed：{junction_escape}"
        )
        assert junction_escape["topStatus"] == "FAIL", (
            f"junction 拒绝必须返回 FAIL，不能把 PASS 作为本次验收结果：{junction_escape}"
        )
        assert junction_escape["finalExists"] is False, (
            f"junction 拒绝前不得写出目标 final（尤其不能留下 PASS）：{junction_escape}"
        )
        assert junction_escape["provisionalEvents"] == 0, (
            f"junction 必须在首次 provisional 写入前被拒绝：{junction_escape}"
        )
        assert junction_escape["provisionalLeft"] == 0 and junction_escape["backupLeft"] == 0, (
            f"junction 拒绝后不得残留受控临时文件：{junction_escape}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 十、P2-2：有 pwsh 时用 pwsh 执行生产实现（诚实记录 host）
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell 生产实现需 Windows；CI 提供 pwsh")
def test_production_ps_runs_under_pwsh_when_available() -> None:
    """P2-2：有 pwsh（CI windows-probes 装了 pwsh）时必须用 pwsh 执行生产 helper（诚实记录 host）；
    本机无 pwsh 时回退 Windows PowerShell 5.1 并记录。此处即用选定 host 真跑生产 helper 并断言成功，
    在 CI 上提供 pwsh 执行证据。"""
    host = _ps_host()
    kind = "pwsh" if os.path.basename(host).lower().startswith("pwsh") else "powershell"
    print(f"[r16][ps-host] using {kind}: {host}")
    r = _pwsh_build_durable_io_receipt("non_windows")
    assert r["status"] == "BLOCKED_UNCERTIFIED"
    assert r["subcheckId"] == "spike:windows_durable_io/non_windows_host"
    if _has_pwsh():
        assert kind == "pwsh", "系统存在 pwsh 时必须用 pwsh 执行生产实现（不得静默退回 powershell）"
