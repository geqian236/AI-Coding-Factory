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
    位置断言（跨平台）+ 运行时强制非 Windows 分支产出合法 BLOCKED 回执且不触碰 D:（Windows）。
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
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
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

function Analyze-Branch($bodyStart, $bodyEnd) {
    $res = [ordered]@{ kind = $null; lastRFromHelper = $false; writesR = $false }
    $assigns = @($ast.FindAll(
        { $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true) |
        Where-Object { $_.Extent.StartOffset -ge $bodyStart -and $_.Extent.EndOffset -le $bodyEnd })
    $rAssigns = @($assigns | Where-Object {
        $_.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $_.Left.VariablePath.UserPath -eq 'r'
    } | Sort-Object { $_.Extent.StartOffset })
    if ($rAssigns.Count -ge 1) {
        $last = $rAssigns[$rAssigns.Count - 1]
        $rhs = @($last.FindAll(
            { $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true) |
            Where-Object { $_.GetCommandName() -eq 'New-DurableIoBlockedReceipt' })
        if ($rhs.Count -ge 1) {
            $res.lastRFromHelper = $true
            $res.kind = Get-KindArg $rhs[0]
        }
    }
    $writes = @($cmds | Where-Object {
        $_.GetCommandName() -eq 'Write-Receipt' -and
        $_.Extent.StartOffset -ge $bodyStart -and $_.Extent.EndOffset -le $bodyEnd
    })
    foreach ($w in $writes) {
        $vars = @($w.FindAll(
            { $args[0] -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) |
            Where-Object { $_.VariablePath.UserPath -eq 'r' })
        if ($vars.Count -ge 1) { $res.writesR = $true }
    }
    return $res
}

$ifs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.IfStatementAst] }, $true)
$nonWin = [ordered]@{ found = $false; kind = $null; lastRFromHelper = $false; writesR = $false }
$buildFail = [ordered]@{ found = $false; kind = $null; lastRFromHelper = $false; writesR = $false }
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
        }
        if ($ct -match 'buildExit') {
            $buildFail.found = $true; $buildFail.kind = $a.kind
            $buildFail.lastRFromHelper = $a.lastRFromHelper; $buildFail.writesR = $a.writesR
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
    buildFailFound            = $buildFail.found
    buildFailKind             = $buildFail.kind
    buildFailLastRFromHelper  = $buildFail.lastRFromHelper
    buildFailWritesR          = $buildFail.writesR
} | ConvertTo-Json -Compress
"""


# CI 预热 run block AST 分析器（P1-3）：确认存在真实 CommandAst 调用 dev.ps1（完整参数序列），
# 而非仅在未使用字符串/注释里出现命令文本；并定位 $LASTEXITCODE -ne 0 守卫体（供 Python 校验 exit 1）。
_CI_PREHEAT_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$required = $env:R16_REQUIRED_CMD
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$parseErrors = @($errors).Count
$cmds = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.CommandAst] }, $true)

function Norm($s) {
    $t = $s -replace '"', ' ' -replace "'", ' ' -replace '\\', '/'
    return ($t -replace '\s+', ' ').Trim()
}

$devInvokes = $false
$devInvokeCount = 0
foreach ($c in $cmds) {
    $els = @($c.CommandElements)
    if ($els.Count -lt 1) { continue }
    $first = $els[0].Extent.Text
    if ($first -match 'dev\.ps1') {
        $devInvokeCount++
        $norm = Norm($c.Extent.Text)
        if ($norm -match [regex]::Escape($required)) { $devInvokes = $true }
    }
}

$ifs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.IfStatementAst] }, $true)
$hasExitGuard = $false
$exitGuardBodyText = ""
foreach ($if in $ifs) {
    foreach ($clause in $if.Clauses) {
        $cond = $clause.Item1; $body = $clause.Item2
        if ($cond.Extent.Text -match '\$LASTEXITCODE\s+-ne\s+0') {
            $hasExitGuard = $true
            $exitGuardBodyText = $body.Extent.Text
        }
    }
}

[pscustomobject]@{
    parseErrors       = $parseErrors
    devInvokes        = $devInvokes
    devInvokeCount    = $devInvokeCount
    hasExitGuard      = $hasExitGuard
    exitGuardBodyText = $exitGuardBodyText
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
        shutil.rmtree(tmp_dir, ignore_errors=True)


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
        shutil.rmtree(tmp_dir, ignore_errors=True)
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


@pytest.mark.skipif(sys.platform != "win32", reason="wrapper 运行时验证需 Windows PowerShell")
def test_wrapper_non_windows_branch_reachable_without_touching_d() -> None:
    """P1-1（运行时）：强制 $env:OS 为空（伪装非 Windows）+ 回执写唯一临时 override，
    wrapper 必须 exit 0、产出合法 BLOCKED 回执（subcheckId + 非空 assertions + passed=false），
    override 落 D 盘非 C 盘，且**不触碰**真实 receipt.json（字节级不变）。"""
    d = _unique_tmp_dir("nonwin")
    override = d / f"receipt_{uuid.uuid4().hex[:8]}.json"
    real = DURABLE_IO_WRAPPER_RECEIPT
    real_before = real.read_bytes() if real.exists() else None
    env = dict(os.environ)
    env["OS"] = ""  # 伪装非 Windows 宿主（"" -notmatch "Windows" -> true）
    env["DURABLE_IO_RECEIPT_OVERRIDE"] = str(override)
    try:
        result = subprocess.run(
            [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DURABLE_IO_WRAPPER_PS1)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=120, env=env,
        )
        assert result.returncode == 0, (
            f"非 Windows 分支应 exit 0；rc={result.returncode} stderr={result.stderr!r}"
        )
        assert override.exists(), "非 Windows 分支必须把 BLOCKED 回执写到 override 路径"
        r = json.loads(override.read_text(encoding="utf-8"))
        assert r["status"] == "BLOCKED_UNCERTIFIED"
        assert r["subcheckId"] == "spike:windows_durable_io/non_windows_host"
        asserts = r.get("assertions")
        assert isinstance(asserts, list) and len(asserts) >= 1, "assertions 必须非空"
        for a in asserts:
            assert a.get("passed") is False, "每条 assertion passed 必须为 JSON boolean false"
        rp = str(override).replace("/", "\\")
        assert not rp.lower().startswith("c:\\"), f"override 不得落 C 盘：{override}"
        if real_before is not None:
            assert real.exists() and real.read_bytes() == real_before, (
                "非 Windows 分支不得改动真实 receipt.json"
            )
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
    # build/linker 分支完整值流
    assert a["buildFailFound"] is True, "build/linker 分支必须调用 helper"
    assert a["buildFailKind"] == "toolchain", (
        f"build/linker 分支必须 -Kind toolchain；实际 ={a['buildFailKind']}"
    )
    assert a["buildFailLastRFromHelper"] is True, "build/linker 分支最后一次 $r 赋值必须来自 helper（无覆盖）"
    assert a["buildFailWritesR"] is True, "build/linker 分支必须 Write-Receipt $r"


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


def _mut_legacy_nonwin(src: str) -> str:
    """non-Windows 分支整体退回手写旧回执（无 helper 调用）。"""
    return src.replace(_NONWIN_CALL, _LEGACY, 1)


def _mut_legacy_buildfail(src: str) -> str:
    """build/linker 分支整体退回手写旧回执（无 helper 调用）。"""
    return _BUILDFAIL_CALL_RE.sub(_LEGACY, src, count=1)


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


def _chk_buildfail_broken(a: dict[str, Any]) -> bool:
    return a["buildFailLastRFromHelper"] is False


def _chk_no_dotsource(a: dict[str, Any]) -> bool:
    return a["dotSourcesHelper"] is False


_VALUE_FLOW_MUTATIONS: list[tuple[str, Callable[[str], str], Callable[[dict[str, Any]], bool]]] = [
    ("swap_kind", _mut_swap_kind, _chk_swap),
    ("overwrite_r_nonwin", _mut_overwrite_r_nonwin, _chk_nonwin_broken),
    ("wrong_var_nonwin", _mut_wrong_var_nonwin, _chk_nonwin_broken),
    ("legacy_nonwin", _mut_legacy_nonwin, _chk_nonwin_broken),
    ("legacy_buildfail", _mut_legacy_buildfail, _chk_buildfail_broken),
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
    body = _strip_ps_comments(str(a.get("exitGuardBodyText", "")))
    assert re.search(r"(?m)(^|\s)exit\s+1(\s|$)", body), (
        f"LASTEXITCODE 守卫体必须 exit 1（去注释后）；body={body!r}"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_preheat_ast_immune_to_unused_string_mutation() -> None:
    """P1-3 mutation 实证：把真实 dev.ps1 调用行注释掉 + 追加含完整命令的**未使用字符串**后，
    AST 必须报 devInvokes=False（证明验证的是真实 CommandAst，而非命令文本出现在字符串/注释里）。"""
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


# ─────────────────────────────────────────────────────────────────────────────
# 八、P1-4：唯一临时目录（D 盘 DataRoot、worktree/进程/随机唯一、绝不落 C 盘）
# ─────────────────────────────────────────────────────────────────────────────
def _data_root() -> Path:
    """定位 <PROJECT_ROOT>\\AI-Coding-Factory-Data\\dev（向上找 AI-Coding-Factory-Data 祖先）。
    找不到时回退系统临时目录（仅非 Windows / 无 DataRoot 环境；Windows 存储合同下必命中 D 盘）。"""
    cur = REPO_ROOT
    for _ in range(8):
        cand = cur / "AI-Coding-Factory-Data"
        if cand.exists():
            return cand / "dev"
        if cur.parent == cur:
            break
        cur = cur.parent
    return Path(tempfile.gettempdir())


def _worktree_digest(root_str: str) -> str:
    """规范化 worktree 根路径的 SHA-256 前 12 hex——不同 worktree 派生不同临时目录，绝不共享。"""
    import hashlib
    return hashlib.sha256(root_str.encode("utf-8")).hexdigest()[:12]


def _unique_tmp_dir(tag: str) -> Path:
    """在 D 盘 DataRoot\\tmp 下创建 worktree/进程/uuid 唯一子目录（并发/xdist 不互撞）。
    调用方 try/finally shutil.rmtree 清理，不残留、不覆盖他人预存文件。"""
    root = _data_root() / "tmp"
    uniq = f"_r16_{tag}_{_worktree_digest(str(REPO_ROOT))}_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    d = root / uniq
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.skipif(sys.platform != "win32", reason="D 盘 DataRoot 唯一性是 Windows 存储合同")
def test_temp_paths_are_unique_per_worktree_and_off_c_drive() -> None:
    """P1-4：不同 worktree 派生不同临时目录 digest（不共享）；同一 worktree 两次分配也不撞
    （uuid/pid）；所有临时目录落在 D 盘 DataRoot 下、绝不落 C 盘。"""
    d1 = _worktree_digest(r"D:\codex项目\.codex-worktrees\AI-Coding-Factory\wt-A")
    d2 = _worktree_digest(r"D:\codex项目\.codex-worktrees\AI-Coding-Factory\wt-B")
    assert d1 != d2, "不同 worktree 必须派生不同临时目录 digest（不得共享临时路径）"
    a = _unique_tmp_dir("uniqtest")
    b = _unique_tmp_dir("uniqtest")
    try:
        assert a != b, "同一 worktree 两次分配必须得到不同目录（uuid 唯一）"
        data_root = str(_data_root()).replace("/", "\\").lower()
        for d in (a, b):
            rp = str(d).replace("/", "\\").lower()
            assert not rp.startswith("c:\\"), f"临时目录不得落在 C 盘：{d}"
            assert rp.startswith(data_root), f"临时目录必须在 D 盘 DataRoot 下：{d}"
    finally:
        for d in (a, b):
            shutil.rmtree(d, ignore_errors=True)


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
    """P2-1（动态门存在）：phase0-acceptance.ps1 必须在回执**写盘后回读**并对每个执行脚本
    重算 SHA256 与回执绑定值逐一比对，缺键/不匹配 fail-closed exit 1。
    这是权威动态反查（Python 侧因 CI 中回执尚未生成而空过的盲区在此堵死）。
    用去注释可执行代码做结构断言（非注释子串）。"""
    code = _strip_ps_comments(ACCEPTANCE_PS1.read_text(encoding="utf-8"))
    # 回执写盘后回读（$outFull = 刚写出的总回执路径）。
    assert re.search(r"\$writtenReceipt\s*=\s*Get-Content\s+-LiteralPath\s+\$outFull\s+-Raw", code), (
        "动态门必须回读已写盘的回执文件 $outFull"
    )
    # 逐脚本重算当前磁盘文件的真实 SHA256（遍历 $scriptDigests.Keys → Get-FileSha256 $digestPath）。
    assert re.search(r"foreach\s*\(\s*\$digestKey\s+in\s+\$scriptDigests\.Keys\s*\)", code), (
        "动态门必须遍历 $scriptDigests.Keys 逐脚本反查"
    )
    assert re.search(r"\$realDigest\s*=\s*Get-FileSha256\s+\$digestPath", code), (
        "动态门必须对每个键重算当前磁盘文件真实 SHA256"
    )
    # 比对回执绑定值 != 当前真实 SHA256 → fail-closed。
    assert re.search(r"\$boundDigest\s+-ne\s+\$realDigest", code), (
        "动态门必须比对回执绑定值 != 当前文件真实 SHA256"
    )
    # 缺 scriptDigests / 缺键 / 不匹配三路都要 fail-closed exit 1。
    assert code.count("exit 1") >= 3, "动态门缺字段/缺键/不匹配三路都必须 exit 1（fail-closed）"
    # helper 键必须在动态反查名单内（即 $scriptDigests 含该键，遍历时会反查它）。
    assert '"spikes/_durable-io-receipt.ps1"' in code, "动态门反查名单必须含 durable-io helper 键"


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
