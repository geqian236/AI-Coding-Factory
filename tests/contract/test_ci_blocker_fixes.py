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

R6 平台分层：GitHub hosted Windows 的 dockerd 只能运行 Windows 容器，不能伪装成
Windows 11 + WSL2 + Docker Desktop Linux backend 的 runner_identity 认证。故 hosted
job 只跑它可真实认证的 Windows-native spike；runner_identity 只能由受信 self-hosted
专用标签在本仓 ``codex/*`` 分支的 push 上执行，PR（包括同仓 PR）一律不调度。其在 pull
前验证候选 SHA、WSL2 与 Linux Docker backend，receipt 还必须绑定实际
runner_identity.exe SHA-256，不能只绑定源码候选 SHA。

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
RUNNER_CERTIFIED_YML = REPO_ROOT / ".github" / "workflows" / "runner-identity-certified.yml"
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
STAMP_RUN_BINDING_PY = REPO_ROOT / "scripts" / "spikes" / "stamp_run_binding.py"
PLATFORM_HELPER_PS1 = REPO_ROOT / "scripts" / "spikes" / "_runner-identity-platform.ps1"
RUNNER_IDENTITY_WRAPPER_PS1 = REPO_ROOT / "scripts" / "spikes" / "test-runner-identity.ps1"
RUNNER_CERTIFIED_JOB = "runner-identity-certified"
RUNNER_CERTIFIED_LABELS = ("self-hosted", "Windows", "X64", "acf-wsl2-linux", "ephemeral")
RUNNER_APPROVED_ROOT_ENV = "RUNNER_APPROVED_ROOT"


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


def _load_job_blocks(text: str) -> dict[str, list[str]]:
    """按 YAML 缩进取出每个 job 的原始结构块；注释行不参与后续语义解析。"""
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
    return jobs


def _load_jobs(
    text: str, *, required_jobs: tuple[str, ...] = ("desktop", "windows-probes", "contracts"),
) -> dict[str, list[dict[str, Any]]]:
    """解析一个 workflow，返回 {job_name: [step, ...]}，并验证调用方声明的必需 job。"""
    jobs = _load_job_blocks(text)
    parsed = {name: _parse_steps(body) for name, body in jobs.items()}
    for required_job in required_jobs:
        assert required_job in parsed, f"解析器未提取到 job '{required_job}'（解析器可能失效）"
        assert len(parsed[required_job]) >= 2, f"job '{required_job}' 解析出的 step 过少（解析器可能失效）"
    return parsed


def _job_metadata(
    name: str,
    workflow_text: str | None = None,
    *,
    workflow_path: Path = CI_YML,
) -> dict[str, Any]:
    """读取 job 顶层结构字段与 permissions 子映射，避免把注释文本当成调度策略。"""
    text = workflow_path.read_text(encoding="utf-8") if workflow_text is None else workflow_text
    blocks = _load_job_blocks(text)
    assert name in blocks, f"ci.yml 缺少 job '{name}'"
    block = "\n".join(blocks[name])
    def field(pattern: str) -> str:
        match = re.search(pattern, block, re.M)
        assert match is not None, f"认证 job 缺少结构字段：{pattern!r}"
        return _scalar(match.group(1))
    return {
        "runs-on": field(r"^    runs-on:\s*(.+?)\s*$"),
        "if": field(r"^    if:\s*(.+?)\s*$"),
        "permissions": {"contents": field(r"^      contents:\s*(.+?)\s*$")},
    }


def _job_env(
    name: str,
    workflow_text: str | None = None,
    *,
    workflow_path: Path = CI_YML,
) -> dict[str, str]:
    """解析 job 顶层 ``env`` 映射，避免把 run 正文或注释里的同名变量当作实际环境绑定。"""
    text = workflow_path.read_text(encoding="utf-8") if workflow_text is None else workflow_text
    blocks = _load_job_blocks(text)
    assert name in blocks, f"ci.yml 缺少 job '{name}'"
    lines = blocks[name]
    env_indexes = [index for index, line in enumerate(lines) if re.match(r"^    env:\s*$", line)]
    assert len(env_indexes) == 1, f"认证 job 必须且只能有一个顶层 env 映射，实际={env_indexes!r}"
    values: dict[str, str] = {}
    for line in lines[env_indexes[0] + 1:]:
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= 4:
            break
        match = re.match(r"^      ([A-Za-z_][A-Za-z0-9_]*):\s?(.*)$", line)
        assert match is not None, f"认证 job env 出现无法解析的结构行：{line!r}"
        values[match.group(1)] = _scalar(match.group(2))
    return values


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


def _runner_identity_certified_steps() -> list[dict[str, Any]]:
    """受信 self-hosted job 是 runner_identity 唯一可认证入口，缺失即 fail-closed。"""
    assert RUNNER_CERTIFIED_YML.exists(), "缺少独立 runner_identity 认证 workflow"
    jobs = _load_jobs(
        RUNNER_CERTIFIED_YML.read_text(encoding="utf-8"),
        required_jobs=(RUNNER_CERTIFIED_JOB,),
    )
    assert RUNNER_CERTIFIED_JOB in jobs, (
        "独立认证 workflow 必须提供 runner-identity-certified；hosted Windows Docker 不能认证 WSL2 Linux 容器语义"
    )
    return jobs[RUNNER_CERTIFIED_JOB]


def _required_named_step(steps: list[dict[str, Any]], name: str) -> dict[str, Any]:
    step = _find_step(steps, lambda s: str(s.get("name", "")).strip() == name)
    assert step is not None, f"缺少 CI 结构步骤 {name!r}"
    return step


def _parse_inline_labels(raw: str) -> tuple[str, ...]:
    """解析 workflow 的 runs-on 内联列表；调度标签属于 YAML 结构字段而非注释文本。"""
    assert raw.startswith("[") and raw.endswith("]"), f"runs-on 必须是标签列表，实际={raw!r}"
    return tuple(part.strip() for part in raw[1:-1].split(",") if part.strip())


def _normalize_expression(expr: str) -> str:
    """仅折叠 job-level if 的空白；逻辑 token 保持逐字比较，避免把 fork guard 放宽。"""
    return re.sub(r"\s+", "", expr)


# 预热步骤必须**实际执行**的完整命令（按 token 序列匹配，非注释子串）。
PREHEAT_REQUIRED_CMD = (
    "scripts/dev.ps1 -- -- rustup toolchain install "
    "stable-x86_64-pc-windows-gnu --profile minimal"
)
RUNNER_IDENTITY_PREFLIGHT_STEP_NAME = "认证前置：候选 SHA、WSL2 与 Docker Desktop Linux backend（fail-closed）"
RUNNER_IDENTITY_IMAGE_STEP_NAME = "准备 self-hosted runner-identity Linux 容器镜像（fail-closed，认证前）"
RUNNER_IDENTITY_EXECUTE_STEP_NAME = "执行并验证 runner_identity 真认证（Windows+WSL2+Linux Docker）"


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


def _runner_identity_image_step() -> dict[str, Any]:
    """按 YAML 显式步骤名定位 self-hosted Linux 镜像准备，不接受 hosted 替代。"""
    return _required_named_step(_runner_identity_certified_steps(), RUNNER_IDENTITY_IMAGE_STEP_NAME)


def test_windows_probes_preheats_toolchain_before_spike_loop() -> None:
    """hosted/self-hosted 必须严格分层：hosted 只认证 Windows-native spike，Linux 容器
    runner_identity 只能由受信专用 self-hosted runner 认证，且每个前置检查排在 pull 前。"""
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

    # hosted Windows 只能运行 Windows dockerd，出现 Linux 镜像 pull 或 runner_identity
    # 都意味着把平台不匹配伪装成认证；这两个动作必须完全从 hosted loop 移除。
    hosted_code = _strip_ps_comments(str(spike.get("run", "")))
    assert "runner_identity" not in hosted_code, "windows-probes 不得执行 runner_identity"
    assert "python:3.12-slim" not in hosted_code, "windows-probes 不得准备 Linux runner_identity 镜像"
    assert all("python:3.12-slim" not in _strip_ps_comments(str(s.get("run", ""))) for s in steps), (
        "hosted Windows 的任何 step 均不得 docker pull/inspect Linux runner_identity 镜像"
    )

    main_jobs = _load_jobs(CI_YML.read_text(encoding="utf-8"))
    assert RUNNER_CERTIFIED_JOB not in main_jobs, (
        "主 ci.yml 不得声明 runner_identity 认证 job；否则 PR 会出现 skipped 的同名 check"
    )
    meta = _job_metadata(RUNNER_CERTIFIED_JOB, workflow_path=RUNNER_CERTIFIED_YML)
    assert _parse_inline_labels(str(meta.get("runs-on", ""))) == RUNNER_CERTIFIED_LABELS, (
        "runner_identity 必须只投递到受信 Windows+WSL2 Linux-container 专用标签"
    )
    assert meta["permissions"].get("contents") == "read", "认证 job 必须最小权限 contents: read"
    runner_workflow = RUNNER_CERTIFIED_YML.read_text(encoding="utf-8")
    assert re.search(r"(?m)^name:\s*runner-identity-certified\s*$", runner_workflow), (
        "独立 workflow 名必须是唯一的 runner-identity-certified"
    )
    assert re.search(r"(?m)^on:\s*$", runner_workflow), "独立 workflow 必须显式监听 push"
    assert re.search(r"(?m)^\s*push:\s*$", runner_workflow), "独立 workflow 必须监听 push"
    assert re.search(r"(?m)^\s*-\s*codex/\*\*\s*$", runner_workflow), (
        "独立 workflow 只能监听 codex/** 分支"
    )
    assert "pull_request" not in _strip_ps_comments(runner_workflow)
    assert "pull_request_target" not in _strip_ps_comments(runner_workflow)
    certified_block = "\n".join(_load_job_blocks(runner_workflow)[RUNNER_CERTIFIED_JOB])
    assert re.search(r"^    timeout-minutes:\s*[1-9]\d*\s*$", certified_block, re.M), (
        "self-hosted 认证 job 必须设置正的 timeout-minutes，失控 Docker/WSL 命令不能无限占用专用 runner"
    )
    # 触发器负责事件类型和分支白名单；job guard 只再约束认证机所属仓库，避免
    # 主 ci 的 PR 运行产生同名 skipped check，也避免 fork 自己注册同名标签后调度。
    expected_if = (
        "github.repository == 'geqian236/AI-Coding-Factory' && "
        "startsWith(github.ref, 'refs/heads/codex/')"
    )
    assert _normalize_expression(str(meta.get("if", ""))) == _normalize_expression(expected_if), (
        "认证 job 只能接受受信本仓 codex/* push；任何 PR（含同仓）与 fork PR 均不得调度 self-hosted runner"
    )

    # 这三项分别是公共仓库 self-hosted 认证的信任边界。直接对结构化 job 字段施加
    # mutation，证明删除任一条件都会被当前测试拒绝，而不是只在注释里写了安全承诺。
    workflow = runner_workflow
    policy = str(meta.get("if", ""))
    for removed, replacement in (
        ("github.repository == 'geqian236/AI-Coding-Factory'", "github.repository == 'attacker/fork'"),
        ("startsWith(github.ref, 'refs/heads/codex/')", "startsWith(github.ref, 'refs/heads/main')"),
    ):
        mutated = workflow.replace(policy, policy.replace(removed, replacement), 1)
        assert mutated != workflow, f"未能对认证 job trust guard 施加 mutation：{removed!r}"
        altered = _job_metadata(
            RUNNER_CERTIFIED_JOB, mutated, workflow_path=RUNNER_CERTIFIED_YML,
        )
        assert _normalize_expression(str(altered.get("if", ""))) != _normalize_expression(expected_if), (
            f"删除/替换 trust guard {removed!r} 后仍与受信策略等价，测试无法防止 self-hosted 误调度"
        )

    certified = _runner_identity_certified_steps()
    d_root_guard = _required_named_step(certified, "认证机 D 根路径前置（fail-closed）")
    checkout = _find_step(certified, lambda s: "actions/checkout" in str(s.get("uses", "")))
    expected_checkout = "actions/checkout@11d5960a326750d5838078e36cf38b85af677262"
    assert checkout is not None and checkout.get("uses") == expected_checkout, (
        "自托管认证 job 必须以经核验的不可变 actions/checkout commit 运行，不能使用可变 tag"
    )
    assert checkout.get("with", {}).get("persist-credentials") == "false", (
        "self-hosted checkout 必须关闭 persist-credentials，避免把可写 token 暴露给认证机"
    )
    credential_mutant = workflow.replace("persist-credentials: false", "persist-credentials: true", 1)
    altered_checkout = _find_step(
        _load_jobs(credential_mutant, required_jobs=(RUNNER_CERTIFIED_JOB,))[RUNNER_CERTIFIED_JOB],
        lambda step: "actions/checkout" in str(step.get("uses", "")),
    )
    assert altered_checkout is not None
    assert altered_checkout.get("with", {}).get("persist-credentials") != "false", (
        "将 checkout 的凭据持久化改为 true 后，结构化 CI 合同必须翻转为拒绝"
    )
    preflight = _required_named_step(certified, RUNNER_IDENTITY_PREFLIGHT_STEP_NAME)
    toolchain_check = _required_named_step(certified, "验证受控 Python 3.12 与本机工具链入口（fail-closed）")
    image_prep = _runner_identity_image_step()
    execute = _required_named_step(certified, RUNNER_IDENTITY_EXECUTE_STEP_NAME)
    assert d_root_guard["_order"] == 0 and d_root_guard["_order"] < checkout["_order"] < preflight["_order"], (
        "D 根路径断言必须是认证 job 首个 step，并在 checkout/任何下载前 fail-closed"
    )
    d_root_code = _strip_ps_comments(str(d_root_guard.get("run", "")))

    # GitHub runner 会以 UTF-8 无 BOM 临时文件交给 Windows PowerShell 5.1。批准根的 CJK
    # 值只能由 YAML 环境变量传入，run block 自身必须只读取 ASCII 的 $env 名称；否则
    # PS 5.1 会按本地代码页把内联 ``D:\\codex项目`` 误解码，首个安全检查反而误拒。
    d_root_env = _job_env(RUNNER_CERTIFIED_JOB, runner_workflow, workflow_path=RUNNER_CERTIFIED_YML)
    approved_root = d_root_env.get(RUNNER_APPROVED_ROOT_ENV, "")
    if sys.platform == "win32":
        encoding_tmp = _unique_tmp_dir("runner-approved-root-ps51")
        try:
            runner_temp = encoding_tmp / "runner" / "_work" / "_temp"
            runner_tool_cache = encoding_tmp / "runner" / "_work" / "_tool"
            workspace = encoding_tmp / "workspace"
            factory_tmp = encoding_tmp / "factory" / "tmp"
            cargo_home = encoding_tmp / "factory" / "cargo-home"
            rustup_home = encoding_tmp / "factory" / "rustup-home"
            for owned in (runner_temp, runner_tool_cache, workspace, factory_tmp, cargo_home, rustup_home):
                owned.mkdir(parents=True, exist_ok=True)
            encoding_env = dict(os.environ)
            encoding_env.update({
                RUNNER_APPROVED_ROOT_ENV: approved_root,
                "RUNNER_TEMP": str(runner_temp),
                "RUNNER_TOOL_CACHE": str(runner_tool_cache),
                "GITHUB_WORKSPACE": str(workspace),
                "TEMP": str(factory_tmp),
                "TMP": str(factory_tmp),
                "FACTORY_TEST_DATA_ROOT": str(encoding_tmp / "factory"),
                "CARGO_HOME": str(cargo_home),
                "RUSTUP_HOME": str(rustup_home),
            })
            dynamic = _run_d_root_guard_as_utf8_no_bom(
                d_root_code,
                script_path=encoding_tmp / "d-root-good.ps1",
                cwd=encoding_tmp,
                env=encoding_env,
            )
            assert dynamic.returncode == 0, (
                "真实 PS 5.1 UTF-8 无 BOM D 根 guard 必须接受 YAML 环境变量提供的批准根；"
                f"stdout={dynamic.stdout!r} stderr={dynamic.stderr!r}"
            )

            # 把生产代码恢复为 CJK 字面量时，同一无 BOM 临时脚本必须重现远程误解码并失败，
            # 不能只靠文本断言宣称编码安全。
            hardcoded_root = d_root_code.replace(
                f"$approvedRoot = [string]$env:{RUNNER_APPROVED_ROOT_ENV}",
                r'$approvedRoot = "D:\codex项目"',
                1,
            )
            assert hardcoded_root != d_root_code, "D 根 guard 必须从 RUNNER_APPROVED_ROOT 环境变量读取批准根"
            hardcoded = _run_d_root_guard_as_utf8_no_bom(
                hardcoded_root,
                script_path=encoding_tmp / "d-root-hardcoded.ps1",
                cwd=encoding_tmp,
                env=encoding_env,
            )
            assert hardcoded.returncode != 0, (
                "恢复内联 CJK 批准根后，PS 5.1 UTF-8 无 BOM 回归必须失败，防止编码问题假绿"
            )

            # 删除 workflow env 绑定会让生产代码拿到空值，必须同样 fail-closed；避免有人
            # 保留 $env 读取形式却忘了把 Unicode 值真正交给 runner。
            missing_env = dict(encoding_env)
            missing_env.pop(RUNNER_APPROVED_ROOT_ENV, None)
            missing = _run_d_root_guard_as_utf8_no_bom(
                d_root_code,
                script_path=encoding_tmp / "d-root-missing-env.ps1",
                cwd=encoding_tmp,
                env=missing_env,
            )
            assert missing.returncode != 0, "缺少 RUNNER_APPROVED_ROOT 时 D 根 guard 必须 fail-closed"
        finally:
            _remove_tree_strict(encoding_tmp)

    assert approved_root == r"D:\codex项目", (
        "认证 job 必须在 YAML 顶层 env 绑定 Unicode RUNNER_APPROVED_ROOT，不能把批准根写进 PS run block"
    )
    assert re.search(
        rf"(?m)^\s*\$approvedRoot\s*=\s*\[string\]\$env:{RUNNER_APPROVED_ROOT_ENV}\s*$",
        d_root_code,
    ), "D 根 guard 的生产代码必须只从 RUNNER_APPROVED_ROOT 环境变量读取批准根"
    removed_env = runner_workflow.replace(
        f"      {RUNNER_APPROVED_ROOT_ENV}: {approved_root}\n", "", 1,
    )
    assert removed_env != runner_workflow, "未能对 YAML 的 RUNNER_APPROVED_ROOT 绑定施加删除 mutation"
    assert RUNNER_APPROVED_ROOT_ENV not in _job_env(
        RUNNER_CERTIFIED_JOB, removed_env, workflow_path=RUNNER_CERTIFIED_YML,
    ), "删除 YAML 环境变量绑定后，结构化合同必须拒绝"

    d_root_requirements = (
        "RUNNER_TEMP", "RUNNER_TOOL_CACHE", "GITHUB_WORKSPACE", "CURRENT_WORKING_DIRECTORY",
        "TEMP", "TMP", "FACTORY_TEST_DATA_ROOT", "CARGO_HOME", "RUSTUP_HOME",
        "Get-CimInstance", "Get-PSDrive -Name D", "ReparsePoint", "StartsWith($approvedRoot",
        "New-Item", "post-create", "exit 1",
    )
    for required in d_root_requirements:
        assert required in d_root_code, f"认证机 D 根首步缺少可执行断言：{required!r}"
    junction_mutant = d_root_code.replace("ReparsePoint", "NoJunctionFlag", 1)
    assert all(required in d_root_code for required in d_root_requirements)
    assert not all(required in junction_mutant for required in d_root_requirements), (
        "移除真实 ReparsePoint 审计后，D 根路径合同必须失效，不能只保留 lexical StartsWith"
    )

    # D 根首步不能只靠文本包含 ReparsePoint：取出 workflow 中真正定义的路径函数，在
    # D 盘创建真实 junction 后直接调用，证明缺失叶节点会向上找到祖先而 junction 必定拒绝。
    # 同时对 guard 前插入 exit 0 的可执行源码施加 PowerShell AST mutation，避免有人把
    # 安全退出藏在校验循环之前而静态 token 检查仍误判通过。
    d_root_analysis = _analyze_d_root_guard_run(d_root_code)
    assert d_root_analysis["parseErrors"] == 0, f"D 根首步必须可由 PowerShell AST 解析：{d_root_analysis}"
    assert d_root_analysis["dRootValid"] is True, f"D 根首步缺少实际路径审计顺序：{d_root_analysis}"
    # 安全短路若在 run block 第一行成功退出，后续完整路径审计即使仍存在也永远不可达；
    # 这里必须直接前置 `exit 0`，不能只在函数体/guard 内注入而漏掉顶层早退。
    first_line_exit_droot = "exit 0\n" + d_root_code
    first_line_droot = _analyze_d_root_guard_run(first_line_exit_droot)
    assert first_line_droot["parseErrors"] == 0
    assert first_line_droot["dRootValid"] is False and first_line_droot["unsafeTerminalBeforeAudit"] is True, (
        f"D 根 run block 第一行 exit 0 必须被拒绝：{first_line_droot}"
    )
    for terminal in ("exit", "return"):
        shorted = _analyze_d_root_guard_run(terminal + "\n" + d_root_code)
        assert shorted["parseErrors"] == 0
        assert shorted["dRootValid"] is False and shorted["unsafeTerminalBeforeAudit"] is True, (
            f"D 根 run block 首行 {terminal!r} 必须被视为成功短路：{shorted}"
        )
    # `exit 1` 的安全性来自 ExitStatementAst 的整数常量值，而不是源码大小写；
    # Windows PowerShell 对关键字大小写不敏感，`Exit 1` 必须仍是合法 fail-closed。
    casefolded_exit_one = d_root_code.replace("exit 1", "Exit 1")
    assert casefolded_exit_one != d_root_code
    casefolded_d_root = _analyze_d_root_guard_run(casefolded_exit_one)
    assert casefolded_d_root["parseErrors"] == 0 and casefolded_d_root["dRootValid"] is True, (
        f"D 根分析器必须按 AST 常量 1 识别 fail-closed，而非匹配源码文本：{casefolded_d_root}"
    )

    # 只检查首次 audit 之前仍可在第一轮 pre-create audit 后成功退出，使余下路径及全部
    # post-create 复核不可达。三种成功终止共用同一真实 AST 插入点，测试节点数不变。
    first_pre_create_audit = (
        '[void](Assert-ApprovedRunnerPath -Name $entry.Key -Value $entry.Value -Stage "pre-create")'
    )
    assert first_pre_create_audit in d_root_code
    for terminal in ("exit 0", "exit", "return"):
        mid_chain = d_root_code.replace(
            first_pre_create_audit, first_pre_create_audit + "\n              " + terminal, 1,
        )
        analysis = _analyze_d_root_guard_run(mid_chain)
        assert analysis["parseErrors"] == 0
        assert analysis.get("unsafeTerminalInAuditChain") is True and analysis["dRootValid"] is False, (
            f"首次 audit 后的 {terminal!r} 必须在 post-create 复核完成前被拒绝：{analysis}"
        )

    # post-create audit 本身位于 foreach；在命令后成功终止会跳过后续
    # TEMP/TMP/缓存路径项。保护边界必须覆盖整个 ForEachStatementAst，
    # 不能停在首轮实际执行的 audit CommandAst 文本末尾。
    post_create_audit = (
        '[void](Assert-ApprovedRunnerPath -Name $name -Value $paths[$name] -Stage "post-create")'
    )
    assert post_create_audit in d_root_code
    for terminal in ("exit 0", "exit", "return"):
        loop_shortcut = d_root_code.replace(
            post_create_audit, post_create_audit + "\n              " + terminal, 1,
        )
        analysis = _analyze_d_root_guard_run(loop_shortcut)
        assert analysis["parseErrors"] == 0
        assert analysis.get("unsafeTerminalInAuditChain") is True and analysis["dRootValid"] is False, (
            f"post-create foreach 首轮 audit 后的 {terminal!r} 必须被拒绝：{analysis}"
        )
    # YAML/AST 静态合同在所有平台都要继续执行；但真实 D 根目录、junction 与
    # Windows PowerShell 路径 guard 会改变 Windows 文件系统，只能在 Windows 运行时验证。
    if sys.platform == "win32":
        d_root_tmp = _unique_tmp_dir("runner-d-root-guard")
        junction = d_root_tmp / "reparse-link"
        junction_target = d_root_tmp / "target"
        safe_missing_leaf = d_root_tmp / "safe" / "missing" / "leaf"
        junction_target.mkdir()
        try:
            _create_windows_junction(junction, junction_target)
            rejected = _invoke_d_root_path_guard(d_root_code, junction / "child")
            assert rejected["ok"] is False and "reparse" in rejected["detail"].lower(), (
                f"真实 junction 必须在创建前被路径 guard 拒绝：{rejected}"
            )
            accepted = _invoke_d_root_path_guard(d_root_code, safe_missing_leaf)
            assert accepted["ok"] is True, (
                f"不存在的叶节点必须回退到最近已有祖先继续审计，不能误拒：{accepted}"
            )
        finally:
            _remove_junction_strict(junction)
            _remove_tree_strict(d_root_tmp)

    unsafe_early_exit = d_root_code.replace(
        "$paths = [ordered]@{", "exit 0\n          $paths = [ordered]@{", 1
    )
    assert unsafe_early_exit != d_root_code, "未能在 D 根路径审计前注入 exit 0 mutation"
    unsafe_analysis = _analyze_d_root_guard_run(unsafe_early_exit)
    assert unsafe_analysis["parseErrors"] == 0
    assert unsafe_analysis["dRootValid"] is False and unsafe_analysis["unsafeTerminalBeforeAudit"] is True, (
        f"D 根审计前的 exit 0 必须翻转结构化合同：{unsafe_analysis}"
    )
    toolchain_code = _strip_ps_comments(str(toolchain_check.get("run", "")))
    for required in ("sys.version_info[:2] == (3, 12)", '"rustup"', '"cargo"', "Get-Command", "exit 1"):
        assert required in toolchain_code, f"认证机受控工具链检查缺少 fail-closed 规则：{required!r}"
    assert preflight["_order"] < toolchain_check["_order"] < image_prep["_order"] < execute["_order"], (
        "候选/WSL2/Linux-backend 前置检查、镜像准备、真实 wrapper+receipt 验证必须严格按此顺序执行"
    )
    assert all(str(step.get("shell", "")).strip() == "powershell" for step in (preflight, image_prep, execute)), (
        "runner_identity 认证步骤必须使用已冻结并在本机验证的 Windows PowerShell，而非未认证的 pwsh 依赖"
    )
    execute_code = _strip_ps_comments(str(execute.get("run", "")))
    for required in (
        "test-runner-identity.ps1", "Get-FileHash -Algorithm SHA256", "runner_identity.exe",
        "stamp_run_binding.py", "--probe-digest $probeDigest", "Test-SpikeReceiptEvidence",
        "-ExpectCandidateSha $env:SPIKE_CANDIDATE_SHA", "-ExpectProbeDigest $probeDigest",
        '$v.status -ne "PASS"',
    ):
        assert required in execute_code, f"认证执行步骤缺少真实 receipt/probe 绑定动作：{required!r}"
    preflight_code = _strip_ps_comments(str(preflight.get("run", "")))
    assert "_runner-identity-platform.ps1" in preflight_code and "Test-RunnerIdentityPlatform" in preflight_code, (
        "CI 前置必须 dot-source 共享平台事实 helper，不能与 wrapper 复制 Docker/WSL 判定"
    )


def test_windows_probes_preheat_is_fail_closed() -> None:
    """hosted 与 self-hosted job 均不得吞错；hosted 不将 runner_identity BLOCKED 降级为 PASS。"""
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
    certified = _runner_identity_certified_steps()
    certified_offenders = [
        s for s in certified if str(s.get("continue-on-error", "")).strip().lower() == "true"
    ]
    assert not certified_offenders, "self-hosted runner_identity 认证不得 continue-on-error"
    hosted_spike = _find_step(steps, _is_spike_loop_step)
    assert hosted_spike is not None
    hosted_code = _strip_ps_comments(str(hosted_spike.get("run", "")))
    assert '"runner_identity"' not in hosted_code, "hosted job 不得把 core runner_identity 加入任何 allowlist/循环"


def test_dev_ps1_does_not_implicitly_install_toolchain() -> None:
    """不得把工具链或 runner_identity 镜像准备塞进 dev.ps1（否则每次运行都可能隐式联网）。"""
    dev = (REPO_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    code_lines = [ln for ln in dev.split("\n") if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert not re.search(r"rustup\s+toolchain\s+install", code), (
        "dev.ps1 不得隐式安装工具链（安装应是 CI 显式预热步骤）"
    )
    assert not re.search(r"docker\s+pull\s+python:3\.12-slim", code), (
        "dev.ps1 不得隐式拉取 runner_identity 镜像（拉取应是 CI 显式 fail-closed 步骤）"
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


# runner_identity 独立认证的首步既要检查 D 根，也要避免有人在真正的路径审计前插入成功
# 退出。这里用 PowerShell AST 读取 workflow 的可执行 run block，而不是搜索注释或 token。
_D_ROOT_GUARD_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)

$fn = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $args[0].Name -eq 'Assert-ApprovedRunnerPath'
}, $true))
$pathsAssignment = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $args[0].Left.Extent.Text -eq '$paths'
}, $true) | Sort-Object { $_.Extent.StartOffset } | Select-Object -First 1)
$pathsOffset = if ($pathsAssignment.Count -eq 1) { $pathsAssignment[0].Extent.StartOffset } else { -1 }
$auditCommands = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.CommandAst] -and
    $args[0].GetCommandName() -eq 'Assert-ApprovedRunnerPath'
}, $true) | Where-Object { $_.Extent.StartOffset -gt $pathsOffset } | Sort-Object { $_.Extent.StartOffset })
$newItems = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.CommandAst] -and
    $args[0].GetCommandName() -eq 'New-Item'
}, $true) | Sort-Object { $_.Extent.StartOffset })
$lastAudit = if ($auditCommands.Count -gt 0) { $auditCommands[$auditCommands.Count - 1] } else { $null }
$postCreateLoop = $null
if ($null -ne $lastAudit) {
    $cursor = $lastAudit.Parent
    while ($null -ne $cursor) {
        if ($cursor -is [System.Management.Automation.Language.ForEachStatementAst]) {
            $postCreateLoop = $cursor
            break
        }
        $cursor = $cursor.Parent
    }
}
$auditChainEndOffset = if ($null -ne $postCreateLoop) { $postCreateLoop.Extent.EndOffset } else { -1 }

function Test-InFunctionScope($node) {
    $cursor = $node.Parent
    while ($null -ne $cursor) {
        if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $true }
        $cursor = $cursor.Parent
    }
    return $false
}

function Test-ConstantExitOne($node) {
    if ($node -isnot [System.Management.Automation.Language.ExitStatementAst] -or
        $null -eq $node.Pipeline) { return $false }
    $elements = @($node.Pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        $elements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) {
        return $false
    }
    $expression = $elements[0].Expression
    return (
        $expression -is [System.Management.Automation.Language.ConstantExpressionAst] -and
        $expression.Value -is [int] -and $expression.Value -eq 1
    )
}

$unsafeTerminalInAuditChain = $false
$terminals = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.ExitStatementAst] -or
    $args[0] -is [System.Management.Automation.Language.ReturnStatementAst] -or
    $args[0] -is [System.Management.Automation.Language.BreakStatementAst] -or
    $args[0] -is [System.Management.Automation.Language.ContinueStatementAst]
}, $true))
foreach ($terminal in $terminals) {
    # 只忽略函数定义内的终止语句；顶层保护区间必须延伸到最后一次
    # post-create 审计结束，否则首次 audit 后仍能成功短路。
    if ((Test-InFunctionScope $terminal) -or $auditChainEndOffset -lt 0 -or
        $terminal.Extent.StartOffset -ge $auditChainEndOffset) { continue }
    # 只有 ExitStatementAst 中的整数常量 1 是合法 fail-closed；不依赖源码大小写。
    if (-not (Test-ConstantExitOne $terminal)) { $unsafeTerminalInAuditChain = $true }
}

$pathAuditBeforeCreate = ($pathsOffset -ge 0 -and $auditCommands.Count -ge 2 -and
    $newItems.Count -ge 1 -and $auditCommands[0].Extent.StartOffset -lt $newItems[0].Extent.StartOffset)
[ordered]@{
    parseErrors = @($errors).Count
    parseErrorMessages = @($errors | ForEach-Object { $_.Message })
    approvedPathFunctionCount = $fn.Count
    pathsAssignmentFound = ($pathsOffset -ge 0)
    pathAuditBeforeCreate = $pathAuditBeforeCreate
    postCreateLoopFound = ($null -ne $postCreateLoop)
    unsafeTerminalBeforeAudit = $unsafeTerminalInAuditChain
    unsafeTerminalInAuditChain = $unsafeTerminalInAuditChain
    dRootValid = (
        @($errors).Count -eq 0 -and $fn.Count -eq 1 -and $null -ne $postCreateLoop -and
        $pathAuditBeforeCreate -and -not $unsafeTerminalInAuditChain
    )
} | ConvertTo-Json -Compress
"""


def _analyze_d_root_guard_run(run: str) -> dict[str, Any]:
    """用真实 PowerShell AST 检查 D 根首步的路径审计和值流顺序。"""
    return _run_ps_analyzer(_D_ROOT_GUARD_AST_ANALYZER, run)


# 直接从 YAML run block 提取生产路径函数执行；fixture 只在 D 盘临时根创建真实 junction，
# 不复制一份 Python 路径逻辑，从而防止测试与 workflow 的 reparse 规则漂移。
_D_ROOT_PATH_GUARD_HARNESS = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$payloadJson = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$payload = $payloadJson | ConvertFrom-Json
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput([string]$payload.run, [ref]$tokens, [ref]$errors)
if (@($errors).Count -ne 0) {
    [ordered]@{ ok = $false; detail = 'workflow run parse failed' } | ConvertTo-Json -Compress
    exit 0
}
$definitions = @($ast.FindAll({
    $args[0] -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $args[0].Name -eq 'Assert-ApprovedRunnerPath'
}, $true))
if ($definitions.Count -ne 1) {
    [ordered]@{ ok = $false; detail = 'Assert-ApprovedRunnerPath missing or ambiguous' } | ConvertTo-Json -Compress
    exit 0
}
$approvedRoot = 'D:\codex项目'
. ([scriptblock]::Create($definitions[0].Extent.Text))
try {
    # 先把 JSON 属性物化为局部变量；PowerShell 5.1 会把 -Value ([string]$payload.target)
    # 解析为歧义参数集，反而没有执行生产路径函数。
    $targetPath = [string]$payload.target
    $value = Assert-ApprovedRunnerPath -Name 'test-path' -Value $targetPath -Stage 'dynamic-test'
    [ordered]@{ ok = $true; detail = 'accepted'; value = [string]$value } | ConvertTo-Json -Compress
} catch {
    [ordered]@{
        ok = $false
        detail = [string]$_.Exception.Message
        parameterNames = @((Get-Command Assert-ApprovedRunnerPath).Parameters.Keys)
    } | ConvertTo-Json -Compress
}
"""


def _invoke_d_root_path_guard(run: str, target: Path) -> dict[str, Any]:
    """让 workflow 内的生产 Assert-ApprovedRunnerPath 审计一个真实 D 盘路径。"""
    payload = json.dumps({"run": run, "target": str(target)}, ensure_ascii=False)
    return _run_ps_analyzer(_D_ROOT_PATH_GUARD_HARNESS, payload)


def _run_d_root_guard_as_utf8_no_bom(
    run: str,
    *,
    script_path: Path,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """用真实 Windows PowerShell 5.1 执行 UTF-8 无 BOM 的 workflow run block。

    GitHub runner 会把 ``shell: powershell`` 的 run 内容写成无 BOM 临时 ``.ps1``；这里严格
    复现该读取边界，防止 CJK 路径又被内联进脚本而在 PS 5.1 按本地代码页误解码。
    """
    powershell = shutil.which("powershell")
    assert powershell is not None, "Windows 动态编码回归必须找到 powershell.exe"
    version = subprocess.run(
        [powershell, "-NoProfile", "-Command", "[Console]::Out.Write($PSVersionTable.PSVersion.Major)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(cwd), timeout=30,
    )
    assert version.returncode == 0 and version.stdout.strip() == "5", (
        f"动态编码回归必须由 Windows PowerShell 5.1 执行，实际 stdout={version.stdout!r} stderr={version.stderr!r}"
    )
    script_path.write_bytes(run.encode("utf-8"))
    assert not script_path.read_bytes().startswith(b"\xef\xbb\xbf"), (
        "回归脚本必须是 UTF-8 无 BOM，才能复现 runner 临时脚本读取边界"
    )
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(cwd), timeout=60, env=env,
    )


# runner_identity 的本地 wrapper 也可能直接产生 authoritative receipt，不能只依赖 CI
# preflight 记住平台条件。这里从生产 PowerShell AST 取两类 receipt 的 observable_facts：
# toolchain BLOCKED 必须克隆完整 platform facts，最终 PASS/FAIL receipt 必须逐项写入。
_RUNNER_IDENTITY_FACTS_AST_ANALYZER = r"""
using namespace System.Management.Automation.Language

$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)

function Get-AstExtentText($node) {
    # PowerShell 5.1/pwsh 7 在错误恢复或候选 AST 差异下都可能给出 null/非 AST 节点；
    # 统一在读取 Extent/Text 前拒绝，避免 StrictMode 把审计器自身变成 false-positive。
    if ($null -eq $node -or $node -isnot [Ast]) { return $null }
    $extent = $node.Extent
    if ($null -eq $extent -or $extent -isnot [IScriptExtent]) { return $null }
    $text = $extent.Text
    if ($text -isnot [string]) { return $null }
    return $text
}

function Get-TrimmedAstExtentText($node) {
    $text = Get-AstExtentText $node
    if ($null -eq $text) { return $null }
    return $text.Trim()
}

function Get-AstStartOffset($node) {
    if ($null -eq $node -or $node -isnot [Ast]) { return [int]::MaxValue }
    $extent = $node.Extent
    if ($null -eq $extent -or $extent -isnot [IScriptExtent]) {
        return [int]::MaxValue
    }
    $offset = $extent.StartOffset
    if ($offset -isnot [int]) { return [int]::MaxValue }
    return $offset
}

function Get-Pair($htable, [string]$Name) {
    if ($null -eq $htable -or $htable -isnot [HashtableAst]) { return }
    $candidates = $htable.KeyValuePairs
    if ($null -eq $candidates) { return }
    foreach ($candidate in @($candidates)) {
        # 每个 candidate、Item1、Item2 都先验证 concrete AST 类型，再读取 Extent/Text。
        if ($null -eq $candidate -or $candidate -isnot [System.Tuple[ExpressionAst, StatementAst]]) {
            continue
        }
        $keyNode = $candidate.Item1
        $valueNode = $candidate.Item2
        $keyText = Get-AstExtentText $keyNode
        $valueText = Get-AstExtentText $valueNode
        if ($null -eq $keyText -or $null -eq $valueText) { continue }
        if ($keyText -eq $Name) { Write-Output $candidate }
    }
}

function Get-InnerFacts($htable) {
    $pair = @(Get-Pair $htable 'observable_facts')
    if ($pair.Count -ne 1) { return $null }
    $valueNode = $pair[0].Item2
    $valueText = Get-AstExtentText $valueNode
    if ($null -eq $valueText -or $valueNode -isnot [Ast]) { return $null }
    $inner = @($valueNode.FindAll({
        $args[0] -is [HashtableAst]
    }, $true) | Sort-Object { Get-AstStartOffset $_ } | Select-Object -First 1)
    if ($inner.Count -ne 1 -or $inner[0] -isnot [HashtableAst]) { return $null }
    $values = [ordered]@{}
    foreach ($entry in @($inner[0].KeyValuePairs)) {
        if ($null -eq $entry -or $entry -isnot [System.Tuple[ExpressionAst, StatementAst]]) {
            return $null
        }
        $keyText = Get-AstExtentText $entry.Item1
        $valueText = Get-TrimmedAstExtentText $entry.Item2
        if ($null -eq $keyText -or $null -eq $valueText) { return $null }
        $values[$keyText] = $valueText
    }
    return $values
}

$tables = @($ast.FindAll({ $args[0] -is [HashtableAst] }, $true))
$toolchainCandidates = @()
$finalCandidates = @()
foreach ($table in $tables) {
    $subcheckPair = @(Get-Pair $table 'subcheckId')
    if ($subcheckPair.Count -eq 1) {
        $subcheckText = Get-AstExtentText $subcheckPair[0].Item2
        if ($null -ne $subcheckText -and $subcheckText -match 'toolchain_unavailable') {
            $toolchainCandidates += $table
        }
    }
    $statusPair = @(Get-Pair $table 'status')
    $factsPair = @(Get-Pair $table 'observable_facts')
    if ($statusPair.Count -eq 1 -and $factsPair.Count -eq 1) {
        $statusText = Get-TrimmedAstExtentText $statusPair[0].Item2
        if ($null -ne $statusText -and $statusText -eq '$status') {
            $finalCandidates += $table
        }
    }
}
$toolchain = @($toolchainCandidates | Select-Object -First 1)
$final = @($finalCandidates | Select-Object -First 1)

$expected = @(
    'windows_host', 'windows_nt', 'windows_product_type', 'windows_build', 'candidate_sha',
    'docker_env_overrides', 'docker_server_version', 'docker_ostype', 'docker_operating_system',
    'docker_context', 'docker_context_endpoint', 'wsl_docker_desktop_v2', 'image_available', 'image_os'
)
$finalFacts = if ($final.Count -eq 1) { Get-InnerFacts $final[0] } else { $null }
$finalMappingsExact = ($null -ne $finalFacts)
if ($finalMappingsExact) {
    foreach ($name in $expected) {
        if (-not $finalFacts.Contains($name) -or $finalFacts[$name] -ne ('$platform.facts["' + $name + '"]')) {
            $finalMappingsExact = $false
        }
    }
}

$toolchainFactsPair = @()
if ($toolchain.Count -eq 1) {
    # if 语句本身会枚举单元素数组；先建立数组容器再直接赋值，StrictMode 下始终可读取 Count。
    $toolchainFactsPair = @(Get-Pair $toolchain[0] 'observable_facts')
}
$toolchainFactsText = $null
if ($toolchainFactsPair.Count -eq 1) {
    $toolchainFactsText = Get-TrimmedAstExtentText $toolchainFactsPair[0].Item2
}
$toolchainUsesClone = ($null -ne $toolchainFactsText -and $toolchainFactsText -eq '$blockedFacts')
$cloneLoops = @($ast.FindAll({
    if ($args[0] -isnot [ForEachStatementAst]) { return $false }
    $loopText = Get-AstExtentText $args[0]
    return $null -ne $loopText -and $loopText -match '\$platform\.facts\.GetEnumerator\(\)'
}, $true))
$cloneAssignments = @($ast.FindAll({
    if ($args[0] -isnot [AssignmentStatementAst]) { return $false }
    $leftText = Get-AstExtentText $args[0].Left
    $rightText = Get-TrimmedAstExtentText $args[0].Right
    return $null -ne $leftText -and $null -ne $rightText -and
        $leftText -match '^\$blockedFacts\[\$fact\.Key\]$' -and $rightText -eq '$fact.Value'
}, $true))
$toolchainCloneExact = ($toolchainUsesClone -and $cloneLoops.Count -eq 1 -and $cloneAssignments.Count -eq 1)

[ordered]@{
    parseErrors = @($errors).Count
    finalFound = ($final.Count -eq 1)
    finalMappingsExact = $finalMappingsExact
    toolchainFound = ($toolchain.Count -eq 1)
    toolchainCloneExact = $toolchainCloneExact
    valid = (@($errors).Count -eq 0 -and $finalMappingsExact -and $toolchainCloneExact)
} | ConvertTo-Json -Compress
"""


def _analyze_runner_identity_platform_facts(source_text: str) -> dict[str, Any]:
    """用生产 AST 证明本地 toolchain/final receipt 都留存完整平台身份事实。"""
    return _run_ps_analyzer(_RUNNER_IDENTITY_FACTS_AST_ANALYZER, source_text)


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

function Test-ConstantExitOne($node) {
    if ($node -isnot [System.Management.Automation.Language.ExitStatementAst] -or
        $null -eq $node.Pipeline) { return $false }
    $elements = @($node.Pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        $elements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) {
        return $false
    }
    $expression = $elements[0].Expression
    return (
        $expression -is [System.Management.Automation.Language.ConstantExpressionAst] -and
        $expression.Value -is [int] -and $expression.Value -eq 1
    )
}

function Test-UnsafeTopLevelTerminalBefore([object[]]$statements, [int]$limitIndex) {
    # 只审计首个必需顶层动作之前真正会执行的终止语句；函数定义内部的 return/exit
    # 只是未来调用体，不能与 run block 首行 exit 0 混为一谈。
    for ($i = 0; $i -lt $limitIndex; $i++) {
        $terminals = @($statements[$i].FindAll({
            $args[0] -is [System.Management.Automation.Language.ExitStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ReturnStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.BreakStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ContinueStatementAst]
        }, $true))
        foreach ($terminal in $terminals) {
            $cursor = $terminal.Parent
            $inFunction = $false
            while ($null -ne $cursor) {
                if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
                    $inFunction = $true; break
                }
                $cursor = $cursor.Parent
            }
            if (-not $inFunction) {
                # 只有 ExitStatementAst 中的整数常量 1 是合法 fail-closed；
                # bare/0/变量 exit 及 return/break/continue 都可能成功短路。
                if (-not (Test-ConstantExitOne $terminal)) { return $true }
            }
        }
    }
    return $false
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

    # 当前 CI 的 fail-closed guard 仅允许两个**直接顶层**语句：直接调用 Write-Error，
    # 再以常量 exit 1 结束。任何前置 return、exit 0、嵌套分支或其他短路控制流都会使
    # 末尾 exit 1 不可达，不能被误判为已可靠失败退出。
    $directStatements = @($body.Statements)
    if ($directStatements.Count -ne 2) { return $result }
    $writeErrorStatement = $directStatements[0]
    if ($writeErrorStatement -isnot [System.Management.Automation.Language.PipelineAst]) { return $result }
    $writeErrorPipeline = @($writeErrorStatement.PipelineElements)
    if ($writeErrorPipeline.Count -ne 1 -or
        $writeErrorPipeline[0] -isnot [System.Management.Automation.Language.CommandAst]) { return $result }
    $writeError = $writeErrorPipeline[0]
    $writeErrorElements = @($writeError.CommandElements)
    if ($writeError.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown -or
        $writeErrorElements.Count -lt 2 -or
        (Normalize-CommandElement $writeErrorElements[0]) -cne 'Write-Error') { return $result }
    # 即使顶层只剩 Write-Error，也要递归拒绝消息子表达式里的短路或控制流；例如
    # `Write-Error "$(exit 0) ..."` 会在记录错误前提前退出，不能借块尾 exit 1 假绿。
    $nestedControlFlow = @($writeErrorStatement.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.ReturnStatementAst] -or
        $node -is [System.Management.Automation.Language.ExitStatementAst] -or
        $node -is [System.Management.Automation.Language.ThrowStatementAst] -or
        $node -is [System.Management.Automation.Language.BreakStatementAst] -or
        $node -is [System.Management.Automation.Language.ContinueStatementAst] -or
        $node -is [System.Management.Automation.Language.TrapStatementAst] -or
        $node -is [System.Management.Automation.Language.IfStatementAst] -or
        $node -is [System.Management.Automation.Language.SwitchStatementAst] -or
        $node -is [System.Management.Automation.Language.TryStatementAst] -or
        $node -is [System.Management.Automation.Language.ForEachStatementAst] -or
        $node -is [System.Management.Automation.Language.ForStatementAst] -or
        $node -is [System.Management.Automation.Language.WhileStatementAst] -or
        $node -is [System.Management.Automation.Language.DoWhileStatementAst] -or
        $node -is [System.Management.Automation.Language.DoUntilStatementAst]
    }, $true))
    if ($nestedControlFlow.Count -ne 0) { return $result }

    $lastStatement = $directStatements[1]
    $result.exitsOne = (Test-ConstantExitOne $lastStatement)
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
$unsafeTerminalBeforePreheat = if ($devIndex -ge 0) {
    Test-UnsafeTopLevelTerminalBefore -statements $topStatements -limitIndex $devIndex
} else { $true }
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
    -not $unsafeTerminalBeforePreheat -and $nextTopLevelIsExitGuard -and
    $guardConditionExact -and $guardExitsOne)

[pscustomobject]@{
    parseErrors              = $parseErrors
    devInvokes               = $devInvokes
    devInvokeCount           = $devInvokeCount
    hasExitGuard             = $hasExitGuard
    commandElementsExact     = $commandElementsExact
    devPreheatAtTopLevel     = $devPreheatAtTopLevel
    unsafeTerminalBeforePreheat = $unsafeTerminalBeforePreheat
    nextTopLevelIsExitGuard  = $nextTopLevelIsExitGuard
    guardConditionExact      = $guardConditionExact
    guardExitsOne            = $guardExitsOne
    preheatValid             = $preheatValid
} | ConvertTo-Json -Compress
"""


# runner_identity 镜像准备 AST：命令必须是独立的 CommandElements，且每次原生命令的下一个
# 顶层语句就是 $LASTEXITCODE -ne 0 → 直接 exit 1。这样注释、未使用字符串、守卫提前
# 或插入其他原生命令都不能把失败路径伪装成已覆盖。
_CI_RUNNER_IMAGE_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$parseErrors = @($errors).Count

function Normalize-CommandElement($element) {
    $value = $element.Extent.Text.Trim()
    if ($value.Length -ge 2 -and (
        ($value.StartsWith([string][char]34) -and $value.EndsWith([string][char]34)) -or
        ($value.StartsWith([string][char]39) -and $value.EndsWith([string][char]39)))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return ($value -replace '\\', '/')
}

function Test-ExactElements($command, [string[]]$expected) {
    $elements = @($command.CommandElements)
    if ($elements.Count -ne $expected.Count) { return $false }
    for ($i = 0; $i -lt $expected.Count; $i++) {
        if ((Normalize-CommandElement $elements[$i]) -cne $expected[$i]) { return $false }
    }
    return $true
}

function Test-ExactPull($command) {
    return (Test-ExactElements -command $command -expected @('docker', 'pull', 'python:3.12-slim'))
}

function Test-ExactInspect($command) {
    return (Test-ExactElements -command $command -expected @('docker', 'image', 'inspect', 'python:3.12-slim'))
}

function Test-ConstantExitOne($node) {
    if ($node -isnot [System.Management.Automation.Language.ExitStatementAst] -or
        $null -eq $node.Pipeline) { return $false }
    $elements = @($node.Pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        $elements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) {
        return $false
    }
    $expression = $elements[0].Expression
    return (
        $expression -is [System.Management.Automation.Language.ConstantExpressionAst] -and
        $expression.Value -is [int] -and $expression.Value -eq 1
    )
}

function Test-UnsafeTopLevelTerminalBefore([object[]]$statements, [int]$limitIndex) {
    # docker pull 前的成功终止会令后续 pull/inspect 文本永远不可达；只忽略函数定义
    # 内的终止语句，避免把未来调用体误判为当前 workflow 的执行短路。
    for ($i = 0; $i -lt $limitIndex; $i++) {
        $terminals = @($statements[$i].FindAll({
            $args[0] -is [System.Management.Automation.Language.ExitStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ReturnStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.BreakStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ContinueStatementAst]
        }, $true))
        foreach ($terminal in $terminals) {
            $cursor = $terminal.Parent
            $inFunction = $false
            while ($null -ne $cursor) {
                if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
                    $inFunction = $true; break
                }
                $cursor = $cursor.Parent
            }
            if (-not $inFunction) {
                # 镜像准备前仅允许 ExitStatementAst 中的整数常量 1；
                # 任何可成功退出或不透明退出码都会令 Linux image 认证不可达。
                if (-not (Test-ConstantExitOne $terminal)) { return $true }
            }
        }
    }
    return $false
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

    # 镜像准备的 guard 同样只能是直接 Write-Error 后紧接常量 exit 1。前置 return、
    # exit 0、嵌套分支或其他短路控制流会让块尾 exit 1 不可达，必须 fail-closed 拒绝。
    $directStatements = @($body.Statements)
    if ($directStatements.Count -ne 2) { return $result }
    $writeErrorStatement = $directStatements[0]
    if ($writeErrorStatement -isnot [System.Management.Automation.Language.PipelineAst]) { return $result }
    $writeErrorPipeline = @($writeErrorStatement.PipelineElements)
    if ($writeErrorPipeline.Count -ne 1 -or
        $writeErrorPipeline[0] -isnot [System.Management.Automation.Language.CommandAst]) { return $result }
    $writeError = $writeErrorPipeline[0]
    $writeErrorElements = @($writeError.CommandElements)
    if ($writeError.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown -or
        $writeErrorElements.Count -lt 2 -or
        (Normalize-CommandElement $writeErrorElements[0]) -cne 'Write-Error') { return $result }
    # Docker guard 的日志参数同样不得暗藏 return/exit/throw 等控制流；否则命令文本末尾
    # 虽有 exit 1，实际却可能在 Write-Error 构造消息时提前短路并绕过 fail-closed。
    $nestedControlFlow = @($writeErrorStatement.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.ReturnStatementAst] -or
        $node -is [System.Management.Automation.Language.ExitStatementAst] -or
        $node -is [System.Management.Automation.Language.ThrowStatementAst] -or
        $node -is [System.Management.Automation.Language.BreakStatementAst] -or
        $node -is [System.Management.Automation.Language.ContinueStatementAst] -or
        $node -is [System.Management.Automation.Language.TrapStatementAst] -or
        $node -is [System.Management.Automation.Language.IfStatementAst] -or
        $node -is [System.Management.Automation.Language.SwitchStatementAst] -or
        $node -is [System.Management.Automation.Language.TryStatementAst] -or
        $node -is [System.Management.Automation.Language.ForEachStatementAst] -or
        $node -is [System.Management.Automation.Language.ForStatementAst] -or
        $node -is [System.Management.Automation.Language.WhileStatementAst] -or
        $node -is [System.Management.Automation.Language.DoWhileStatementAst] -or
        $node -is [System.Management.Automation.Language.DoUntilStatementAst]
    }, $true))
    if ($nestedControlFlow.Count -ne 0) { return $result }

    $lastStatement = $directStatements[1]
    $result.exitsOne = (Test-ConstantExitOne $lastStatement)
    return $result
}

$topStatements = @($ast.EndBlock.Statements)
$pullCount = 0; $pullIndex = -1
$inspectCount = 0; $inspectIndex = -1
for ($i = 0; $i -lt $topStatements.Count; $i++) {
    $statement = $topStatements[$i]
    if ($statement -isnot [System.Management.Automation.Language.PipelineAst]) { continue }
    $pipelineElements = @($statement.PipelineElements)
    if ($pipelineElements.Count -ne 1 -or
        $pipelineElements[0] -isnot [System.Management.Automation.Language.CommandAst]) { continue }
    $command = $pipelineElements[0]
    if (Test-ExactPull $command) {
        $pullCount++
        $pullIndex = $i
    }
    if (Test-ExactInspect $command) {
        $inspectCount++
        $inspectIndex = $i
    }
}

$pullGuard = [ordered]@{ isGuard = $false; conditionExact = $false; exitsOne = $false }
$inspectGuard = [ordered]@{ isGuard = $false; conditionExact = $false; exitsOne = $false }
$unsafeTerminalBeforePull = if ($pullIndex -ge 0) {
    Test-UnsafeTopLevelTerminalBefore -statements $topStatements -limitIndex $pullIndex
} else { $true }
if ($pullCount -eq 1 -and $pullIndex + 1 -lt $topStatements.Count) {
    $pullGuard = Get-ExitGuardDetails $topStatements[$pullIndex + 1]
}
if ($inspectCount -eq 1 -and $inspectIndex + 1 -lt $topStatements.Count) {
    $inspectGuard = Get-ExitGuardDetails $topStatements[$inspectIndex + 1]
}

$pullImmediatelyGuarded = ($pullGuard.isGuard -and $pullGuard.conditionExact -and $pullGuard.exitsOne)
$inspectImmediatelyGuarded = ($inspectGuard.isGuard -and $inspectGuard.conditionExact -and $inspectGuard.exitsOne)
$sequenceExact = ($pullCount -eq 1 -and $inspectCount -eq 1 -and
    $inspectIndex -eq ($pullIndex + 2))
$imagePreparationValid = ($sequenceExact -and -not $unsafeTerminalBeforePull -and
    $pullImmediatelyGuarded -and $inspectImmediatelyGuarded)

[pscustomobject]@{
    parseErrors               = $parseErrors
    pullCommandCount          = $pullCount
    inspectCommandCount       = $inspectCount
    pullImmediatelyGuarded    = $pullImmediatelyGuarded
    inspectImmediatelyGuarded = $inspectImmediatelyGuarded
    sequenceExact             = $sequenceExact
    unsafeTerminalBeforePull  = $unsafeTerminalBeforePull
    imagePreparationValid     = $imagePreparationValid
} | ConvertTo-Json -Compress
"""


def _analyze_wrapper_source(source_text: str) -> dict[str, Any]:
    return _run_ps_analyzer(_WRAPPER_AST_ANALYZER, source_text)


def _analyze_ci_run_block(run_text: str) -> dict[str, Any]:
    return _run_ps_analyzer(
        _CI_PREHEAT_AST_ANALYZER, run_text, {"R16_REQUIRED_CMD": PREHEAT_REQUIRED_CMD}
    )


def _analyze_runner_identity_image_run(run_text: str) -> dict[str, Any]:
    """用真实 PowerShell AST 验证 Docker 准备步骤，不把注释、字符串或文本检索当作执行证据。"""
    return _run_ps_analyzer(_CI_RUNNER_IMAGE_AST_ANALYZER, run_text)


# runner_identity 最终执行链必须是一个可证明的值流：真实 wrapper 命令 -> 立即捕获
# LASTEXITCODE -> 立即 reject 非零 -> 已执行 exe 摘要 -> stamp -> stamp guard -> validator
# -> PASS guard。这里按顶层 PowerShell AST statement 的真实顺序锁定，拒绝注释、字符串、
# 参数折叠、提前 guard 或插入原生命令造成的假绿。
_CI_RUNNER_EXECUTE_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$b64 = [Console]::In.ReadToEnd()
$src = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)

function Get-TopCommand($statement) {
    if ($statement -isnot [System.Management.Automation.Language.PipelineAst]) { return $null }
    $parts = @($statement.PipelineElements)
    if ($parts.Count -ne 1 -or $parts[0] -isnot [System.Management.Automation.Language.CommandAst]) { return $null }
    return $parts[0]
}
function Get-ElementText($element) {
    $value = $element.Extent.Text.Trim()
    if ($value.Length -ge 2 -and (
        ($value[0] -eq [char]34 -and $value[$value.Length - 1] -eq [char]34) -or
        ($value[0] -eq [char]39 -and $value[$value.Length - 1] -eq [char]39)
    )) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return ($value -replace '\\', '/')
}
function Test-ExactCommand($statement, [string[]]$expected, [bool]$requireAmpersand) {
    $cmd = Get-TopCommand $statement
    if ($null -eq $cmd) { return $false }
    if ($requireAmpersand -and
        $cmd.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Ampersand) {
        return $false
    }
    $els = @($cmd.CommandElements)
    if ($els.Count -ne $expected.Count) { return $false }
    for ($i = 0; $i -lt $els.Count; $i++) { if ((Get-ElementText $els[$i]) -cne $expected[$i]) { return $false } }
    return $true
}
function Test-LastExitCapture($statement) {
    if ($statement -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { return $false }
    $right = $statement.Right
    if ($right -is [System.Management.Automation.Language.PipelineAst]) {
        $parts = @($right.PipelineElements)
        if ($parts.Count -eq 1 -and $parts[0] -is [System.Management.Automation.Language.CommandExpressionAst]) {
            $right = $parts[0].Expression
        }
    }
    elseif ($right -is [System.Management.Automation.Language.CommandExpressionAst]) {
        $right = $right.Expression
    }
    return ($statement.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $statement.Left.VariablePath.UserPath -eq 'wrapperExit' -and
        $right -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $right.VariablePath.UserPath -eq 'LASTEXITCODE')
}
function Test-ConstantExitOne($node) {
    if ($node -isnot [System.Management.Automation.Language.ExitStatementAst] -or
        $null -eq $node.Pipeline) { return $false }
    $elements = @($node.Pipeline.PipelineElements)
    if ($elements.Count -ne 1 -or
        $elements[0] -isnot [System.Management.Automation.Language.CommandExpressionAst]) {
        return $false
    }
    $expression = $elements[0].Expression
    return (
        $expression -is [System.Management.Automation.Language.ConstantExpressionAst] -and
        $expression.Value -is [int] -and $expression.Value -eq 1
    )
}
function Test-RejectGuard($statement, [string]$requiredCondition) {
    if ($statement -isnot [System.Management.Automation.Language.IfStatementAst]) { return $false }
    $clauses = @($statement.Clauses)
    if ($clauses.Count -ne 1 -or $null -ne $statement.ElseClause) { return $false }
    $condition = ($clauses[0].Item1.Extent.Text -replace '\s+', '')
    if ($condition -cne $requiredCondition) { return $false }
    $body = $clauses[0].Item2
    $top = @($body.Statements)
    if ($top.Count -ne 2 -or $top[1] -isnot [System.Management.Automation.Language.ExitStatementAst]) { return $false }
    $pipe = @($top[1].Pipeline.PipelineElements)
    if ($pipe.Count -ne 1 -or $pipe[0] -isnot [System.Management.Automation.Language.CommandExpressionAst] -or
        $pipe[0].Expression -isnot [System.Management.Automation.Language.ConstantExpressionAst] -or
        $pipe[0].Expression.Value -ne 1) { return $false }
    $unsafe = @($body.FindAll({
        param($node)
        ($node -is [System.Management.Automation.Language.ReturnStatementAst]) -or
        ($node -is [System.Management.Automation.Language.ThrowStatementAst]) -or
        ($node -is [System.Management.Automation.Language.BreakStatementAst]) -or
        ($node -is [System.Management.Automation.Language.ContinueStatementAst]) -or
        ($node -is [System.Management.Automation.Language.ExitStatementAst] -and
            -not (Test-ConstantExitOne $node))
    }, $true))
    return ($unsafe.Count -eq 0)
}
function Test-UnsafeTopLevelTerminalBefore([object[]]$statements, [int]$limitIndex) {
    # 保护区间内的 exit 0、bare exit、return 等都会让后续盖章或校验不可达；
    # 仅函数定义内的终止语句可忽略。
    for ($i = 0; $i -lt $limitIndex; $i++) {
        $terminals = @($statements[$i].FindAll({
            $args[0] -is [System.Management.Automation.Language.ExitStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ReturnStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.BreakStatementAst] -or
            $args[0] -is [System.Management.Automation.Language.ContinueStatementAst]
        }, $true))
        foreach ($terminal in $terminals) {
            $cursor = $terminal.Parent
            $inFunction = $false
            while ($null -ne $cursor) {
                if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
                    $inFunction = $true; break
                }
                $cursor = $cursor.Parent
            }
            if (-not $inFunction) {
                # 只有 ExitStatementAst 中的整数常量 1 是合法 fail-closed，不依赖源码文本。
                if (-not (Test-ConstantExitOne $terminal)) { return $true }
            }
        }
    }
    return $false
}
function Find-StatementIndex([object[]]$statements, [int]$start, [scriptblock]$predicate) {
    for ($i = $start; $i -lt $statements.Count; $i++) { if (& $predicate $statements[$i]) { return $i } }
    return -1
}
function Has-Command($statement, [string]$name) {
    $commands = @($statement.FindAll({
        param($node)
        $node -is [System.Management.Automation.Language.CommandAst] -and
            $node.GetCommandName() -eq $name
    }, $true))
    return $commands.Count -eq 1
}

$top = @($ast.EndBlock.Statements)
$wrapperExpected = @(
    'powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',
    'scripts/spikes/test-runner-identity.ps1'
)
$wrapperIndex = Find-StatementIndex $top 0 { param($s) Test-ExactCommand $s $wrapperExpected $true }
$captureIndex = if ($wrapperIndex -ge 0) { $wrapperIndex + 1 } else { -1 }
$captureValid = ($captureIndex -ge 0 -and $captureIndex -lt $top.Count -and (Test-LastExitCapture $top[$captureIndex]))
$wrapperGuardIndex = if ($captureValid) { $captureIndex + 1 } else { -1 }
$wrapperGuardValid = ($wrapperGuardIndex -ge 0 -and $wrapperGuardIndex -lt $top.Count -and
    (Test-RejectGuard $top[$wrapperGuardIndex] '$null-eq$wrapperExit-or$wrapperExit-ne0'))
$hashIndex = if ($wrapperGuardValid) {
    Find-StatementIndex $top ($wrapperGuardIndex + 1) {
        param($s)
        Has-Command $s 'Get-FileHash'
    }
} else { -1 }
$stampIndex = if ($hashIndex -ge 0) { Find-StatementIndex $top ($hashIndex + 1) {
    param($s)
    Test-ExactCommand $s @(
        'python','scripts/spikes/stamp_run_binding.py','$receiptPath','runner_identity',
        '--probe-digest','$probeDigest'
    ) $false
} } else { -1 }
$stampGuardIndex = if ($stampIndex -ge 0) { $stampIndex + 1 } else { -1 }
$stampGuardValid = (
    $stampGuardIndex -ge 0 -and $stampGuardIndex -lt $top.Count -and
    (Test-RejectGuard $top[$stampGuardIndex] '$LASTEXITCODE-ne0')
)
$validatorIndex = if ($stampGuardValid) {
    Find-StatementIndex $top ($stampGuardIndex + 1) {
        param($s)
        Has-Command $s 'Test-SpikeReceiptEvidence'
    }
} else { -1 }
$validatorGuardIndex = if ($validatorIndex -ge 0) { $validatorIndex + 1 } else { -1 }
$validatorGuardValid = (
    $validatorGuardIndex -ge 0 -and $validatorGuardIndex -lt $top.Count -and
    (Test-RejectGuard $top[$validatorGuardIndex] '-not$v.ok-or$v.status-ne"PASS"')
)
$executeChainLimitIndex = if ($validatorGuardIndex -ge 0) { $validatorGuardIndex + 1 } else { -1 }
$unsafeTerminalInExecuteChain = if ($executeChainLimitIndex -ge 0) {
    # 从 run block 起点一直扫描到 validator 拒绝 guard，覆盖 wrapper→hash→stamp→validator 全链。
    Test-UnsafeTopLevelTerminalBefore -statements $top -limitIndex $executeChainLimitIndex
} else { $true }

[pscustomobject]@{
    parseErrors = @($errors).Count
    wrapperIndex = $wrapperIndex
    captureValid = $captureValid
    wrapperGuardValid = $wrapperGuardValid
    hashIndex = $hashIndex
    stampIndex = $stampIndex
    stampGuardValid = $stampGuardValid
    validatorIndex = $validatorIndex
    validatorGuardValid = $validatorGuardValid
    unsafeTerminalBeforeWrapper = $unsafeTerminalInExecuteChain
    unsafeTerminalInExecuteChain = $unsafeTerminalInExecuteChain
    executeValid = (
        $wrapperIndex -ge 0 -and -not $unsafeTerminalInExecuteChain -and
        $captureValid -and $wrapperGuardValid -and
        $hashIndex -gt $wrapperGuardIndex -and $stampIndex -gt $hashIndex -and
        $stampGuardValid -and $validatorIndex -gt $stampGuardIndex -and $validatorGuardValid
    )
} | ConvertTo-Json -Compress
"""


def _analyze_runner_identity_execute_run(run_text: str) -> dict[str, Any]:
    """以 PowerShell AST 校验 runner_identity 最终执行链的真实命令和值流顺序。"""
    return _run_ps_analyzer(_CI_RUNNER_EXECUTE_AST_ANALYZER, run_text)


def _run_runner_platform_helper_mock(
    helper_path: Path, *, ostype: str = "linux", operating_system: str = "Docker Desktop",
    context: str = "desktop-linux", endpoint: str = "npipe:////./pipe/dockerDesktopLinuxEngine",
    image_os: str = "linux", wsl_has_nul: bool = False, wsl_multiline: bool = False,
    candidate_sha: str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    product_type: int = 1, build_number: str = "22631", os_value: str = "Windows_NT",
    missing_command: str = "", throwing_command: str = "", duplicate_command: str = "",
    docker_override: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """在不接触真实 Docker 的 Windows PowerShell 子进程中执行生产 helper。

    Docker/git/WSL 均由同进程函数替身提供可控事实；这验证 helper 的真实 PS5 运行语义，
    包括 WSL UTF-16 NUL 清理与 remote context named-pipe 拒绝，而非只断言源码片段。
    """
    ps = r"""
$ErrorActionPreference = 'Stop'
function git {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CmdArgs)
    if ($env:FACTORY_MOCK_THROWING_COMMAND -eq 'git') { throw 'mock git exception' }
    $global:LASTEXITCODE = 0
    $env:FACTORY_MOCK_GIT_SHA
}
function Get-CimInstance {
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Args)
    [pscustomobject]@{
        ProductType = [int]$env:FACTORY_MOCK_PRODUCT_TYPE
        BuildNumber = $env:FACTORY_MOCK_BUILD_NUMBER
        Caption = 'Microsoft Windows mock'
    }
}
function Get-Command {
    param([string]$Name, [Parameter(ValueFromRemainingArguments = $true)][object[]]$Args)
    if ($Name -eq $env:FACTORY_MOCK_MISSING_COMMAND) { return $null }
    # Windows PATH 可能同时解析 docker.exe 与无扩展名包装项；生产 helper 必须选择一个
    # 可执行 command，而不能把数组 Path 拼成无法执行的单个字符串。
    if ($Name -eq $env:FACTORY_MOCK_DUPLICATE_COMMAND) {
        @(
            [pscustomobject]@{ Name = $Name; CommandType = 'Application'; Path = $Name }
            [pscustomobject]@{ Name = $Name; CommandType = 'Application'; Path = $Name }
        )
        return
    }
    [pscustomobject]@{ Name = $Name; CommandType = 'Application'; Path = $Name }
}
function docker {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CmdArgs)
    if ($env:FACTORY_MOCK_THROWING_COMMAND -eq 'docker') { throw 'mock docker exception' }
    $joined = $CmdArgs -join ' '
    if ($joined -like 'version *') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_VERSION; return }
    if ($joined -like 'info *OSType*') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_OSTYPE; return }
    if ($joined -like 'info *OperatingSystem*') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_OPERATING; return }
    if ($joined -eq 'context show') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_CONTEXT; return }
    if ($joined -like 'context inspect *') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_ENDPOINT; return }
    if ($joined -like 'image inspect *') { $global:LASTEXITCODE = 0; $env:FACTORY_MOCK_IMAGE_OS; return }
    $global:LASTEXITCODE = 1
}
function wsl.exe {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CmdArgs)
    if ($env:FACTORY_MOCK_THROWING_COMMAND -eq 'wsl.exe') { throw 'mock wsl exception' }
    $global:LASTEXITCODE = 0
    # 真实 PS 5.1 的 wsl.exe 可能输出表头、空行和多条对象；保留多对象形态，
    # 才能覆盖 native probe 在连接 stdout 片段时不能把分隔符写进数据行尾的回归。
    if ($env:FACTORY_MOCK_WSL_MULTILINE -eq '1') {
        if ($env:FACTORY_MOCK_WSL_NUL -eq '1') {
            # UTF-16 经 PS5 stdout 传输时，空行也会成为仅含 NUL 的对象，不能用空字符串代替。
            "  NAME              STATE           VERSION`0"
            "`0"
            "`0* docker-desktop`0 Running`0 2`0"
            "`0"
        } else {
            '  NAME              STATE           VERSION'
            'sentinel-empty-row'
            '* docker-desktop Running 2'
        }
    } elseif ($env:FACTORY_MOCK_WSL_NUL -eq '1') { "  docker-desktop`0 Running`0 2`0" }
    else { '  docker-desktop Running 2' }
}
. $env:FACTORY_PLATFORM_HELPER
$result = Test-RunnerIdentityPlatform `
    -ExpectedCandidateSha 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' -RequireImage $true
[pscustomobject]@{
    ok = [bool]$result.ok
    failed = @($result.assertions | Where-Object { -not $_.passed } | ForEach-Object { $_.name })
} | ConvertTo-Json -Compress
"""
    env = dict(os.environ)
    env.update({
        "FACTORY_PLATFORM_HELPER": str(helper_path),
        "FACTORY_MOCK_VERSION": "27.5.1",
        "FACTORY_MOCK_OSTYPE": ostype,
        "FACTORY_MOCK_OPERATING": operating_system,
        "FACTORY_MOCK_CONTEXT": context,
        "FACTORY_MOCK_ENDPOINT": endpoint,
        "FACTORY_MOCK_IMAGE_OS": image_os,
        "FACTORY_MOCK_WSL_NUL": "1" if wsl_has_nul else "0",
        "FACTORY_MOCK_WSL_MULTILINE": "1" if wsl_multiline else "0",
        "FACTORY_MOCK_GIT_SHA": candidate_sha,
        "FACTORY_MOCK_PRODUCT_TYPE": str(product_type),
        "FACTORY_MOCK_BUILD_NUMBER": build_number,
        "FACTORY_MOCK_MISSING_COMMAND": missing_command,
        "FACTORY_MOCK_THROWING_COMMAND": throwing_command,
        "FACTORY_MOCK_DUPLICATE_COMMAND": duplicate_command,
        "OS": os_value,
        "TEMP": r"D:\codex项目\AI-Coding-Factory-Data\dev\tmp",
        "TMP": r"D:\codex项目\AI-Coding-Factory-Data\dev\tmp",
    })
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(name, None)
    if docker_override is not None:
        env[docker_override[0]] = docker_override[1]
    result = subprocess.run(
        [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", _encoded_command(ps)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
        timeout=60, env=env,
    )
    assert result.returncode == 0, (
        f"共享 runner 平台 helper mock 执行失败 rc={result.returncode} stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


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


def _pwsh_validate_receipt(
    receipt: object, env_compat: bool, *, name: str = "windows_durable_io",
    run_id: str = "", run_nonce: str = "", candidate_sha: str = "", probe_digest: str = "",
) -> dict[str, Any]:
    """把 receipt 写唯一临时文件，经生产 validator 判定；可传运行/候选/probe 三重绑定。"""
    tmp_dir = _unique_tmp_dir("validate")
    tmp = tmp_dir / f"receipt_{uuid.uuid4().hex[:8]}.json"
    try:
        tmp.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        env_flag = "$true" if env_compat else "$false"
        bindings = (
            f"-ExpectRunId '{run_id}' -ExpectRunNonce '{run_nonce}' "
            f"-ExpectCandidateSha '{candidate_sha}' -ExpectProbeDigest '{probe_digest}'"
        )
        ps = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence -Name '{name}' -Path '{tmp}' -EnvCompat {env_flag} -Allowlist @() {bindings}
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            [_ps_host(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=60,
        )
        assert result.returncode == 0, (
            "生产 validator 必须把坏 receipt 转为稳定 verdict，不能因顶层 null/非对象等输入抛出："
            f"rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
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

    # runner_identity 依赖的容器镜像必须由独立步骤显式准备；两条 Docker 原生命令均需
    # 使用独立 CommandElements，并分别紧邻自身的 $LASTEXITCODE fail-closed 守卫。
    image_a = _analyze_runner_identity_image_run(
        str(_runner_identity_image_step().get("run", ""))
    )
    assert image_a["parseErrors"] == 0, (
        f"镜像准备 run block 应可解析；parseErrors={image_a['parseErrors']}"
    )
    assert image_a["pullCommandCount"] == 1, "必须恰有一条真实 docker pull python:3.12-slim"
    assert image_a["inspectCommandCount"] == 1, (
        "必须恰有一条真实 docker image inspect python:3.12-slim"
    )
    assert image_a["pullImmediatelyGuarded"] is True
    assert image_a["inspectImmediatelyGuarded"] is True
    assert image_a["sequenceExact"] is True
    assert image_a["imagePreparationValid"] is True

    # 认证机必须在任何 Linux image 拉取前调用共享 helper；helper 的真实 CommandAst
    # 覆盖候选 SHA、Linux OSType、Docker Desktop backend/context 与 WSL2 v2，注释或
    # 未使用字符串不能充数。
    ci_preflight_run = str(
        _required_named_step(_runner_identity_certified_steps(), RUNNER_IDENTITY_PREFLIGHT_STEP_NAME).get("run", "")
    )
    ci_preflight_code = _strip_ps_comments(ci_preflight_run)
    assert "_runner-identity-platform.ps1" in ci_preflight_code
    expected_preflight_call = (
        "Test-RunnerIdentityPlatform -ExpectedCandidateSha $env:EXPECTED_CANDIDATE_SHA -RequireImage $false"
    )
    assert expected_preflight_call in ci_preflight_code
    assert re.search(r"if\s*\(\s*-not\s+\$platform\.ok\s*\).*?exit\s+1", ci_preflight_code, re.S), (
        "共享平台 helper 返回失败时，CI preflight 必须在 pull 前 exit 1"
    )
    helper_run = PLATFORM_HELPER_PS1.read_text(encoding="utf-8-sig")
    helper_code = _strip_ps_comments(helper_run)
    # 所有 git/docker/wsl 原生命令必须走同一 fail-closed 封装；不能再通过搜裸命令
    # 字串误判安全性。下方 mock 直接运行生产 helper 覆盖命令缺失、异常及顺序。
    for required in (
        "Invoke-RunnerIdentityNativeProbe",
        "Get-Command -Name $Name -CommandType Application",
        "docker_environment_overrides_clear",
        "windows_11_workstation",
        "[Environment]::OSVersion.Platform",
        "Get-CimInstance -ClassName Win32_OperatingSystem",
        '$facts.docker_ostype -ceq "linux"',
        '$facts.docker_operating_system -match "Docker Desktop"',
        '$facts.docker_context -in @("desktop-linux", "docker-desktop")',
        "docker_desktop_local_endpoint",
        "docker-desktop\\s+\\S+\\s+2",
        '$facts.image_os -ceq "linux"',
        "candidate_sha_exact",
    ):
        assert required in helper_code, f"共享 helper 缺少平台事实/断言：{required!r}"

    # 真实执行生产 helper：WSL 的 UTF-16/NUL 输出仍须识别 docker-desktop v2；Windows
    # containers、同名但远端 TCP context、候选 SHA 漂移均必须 fail-closed。最后一项用
    # 删除 NUL 清理的临时 mutant 复现 PS5 漏判，确保该兼容代码不是无牙注释。
    helper_tmp = _unique_tmp_dir("runner-platform-helper")
    try:
        happy = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, wsl_has_nul=True)
        assert happy["ok"] is True, f"NUL 清理后的本机 Docker Desktop 事实应通过：{happy}"
        multiline_wsl = _run_runner_platform_helper_mock(
            PLATFORM_HELPER_PS1, wsl_has_nul=True, wsl_multiline=True,
        )
        assert multiline_wsl["ok"] is True, (
            "WSL 表头/空行拆成多个 stdout 对象时，native probe 必须仅以换行连接，"
            f"不能把反斜杠等分隔符残留到 docker-desktop v2 数据行尾：{multiline_wsl}"
        )
        duplicate_docker = _run_runner_platform_helper_mock(
            PLATFORM_HELPER_PS1, duplicate_command="docker",
        )
        assert duplicate_docker["ok"] is True, (
            "Get-Command 返回 docker.exe/包装项等多个 Application 时，helper 必须选择一个可执行项；"
            f"实际={duplicate_docker}"
        )
        windows_engine = _run_runner_platform_helper_mock(
            PLATFORM_HELPER_PS1, ostype="windows", image_os="windows",
        )
        assert windows_engine["ok"] is False and "docker_linux_backend" in windows_engine["failed"]
        remote_context = _run_runner_platform_helper_mock(
            PLATFORM_HELPER_PS1, endpoint="tcp://remote.example:2376",
        )
        assert remote_context["ok"] is False and "docker_desktop_local_endpoint" in remote_context["failed"]
        wrong_candidate = _run_runner_platform_helper_mock(
            PLATFORM_HELPER_PS1, candidate_sha="b" * 40,
        )
        assert wrong_candidate["ok"] is False and "candidate_sha_exact" in wrong_candidate["failed"]

        # 认证环境不可被 DOCKER_* 覆盖到远端 daemon；每个变量都必须在任何 docker
        # 调用前统一拒绝。逐个实际运行生产 helper，避免只扫描变量名的假绿。
        for name, value in (
            ("DOCKER_HOST", "tcp://remote.example:2376"),
            ("DOCKER_CONTEXT", "attacker-context"),
            ("DOCKER_TLS_VERIFY", "1"),
            ("DOCKER_CERT_PATH", r"D:\untrusted-cert"),
        ):
            overridden = _run_runner_platform_helper_mock(
                PLATFORM_HELPER_PS1, docker_override=(name, value),
            )
            assert overridden["ok"] is False and "docker_environment_overrides_clear" in overridden["failed"], (
                f"{name} 非空时必须在 docker 调用前 fail-closed：{overridden}"
            )

        # $env:OS 只能作为辅助事实，真实认证还必须是 Windows NT 的 Windows 11
        # workstation；Server 和 Windows 10 都不能借文字环境变量混入认证路径。
        server = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, product_type=3)
        assert server["ok"] is False and "windows_11_workstation" in server["failed"]
        win10 = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, build_number="19045")
        assert win10["ok"] is False and "windows_11_workstation" in win10["failed"]
        non_nt = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, os_value="Unix")
        assert non_nt["ok"] is False and "windows_nt" in non_nt["failed"]

        # git/docker/wsl 原生命令缺失或抛异常都必须返回稳定失败对象，wrapper 才能
        # 写出合法 BLOCKED receipt，而不是在 dot-source 阶段异常退出且没有证据。
        for command, assertion in (
            ("git", "candidate_sha_exact"),
            ("docker", "docker_daemon_available"),
            ("wsl.exe", "docker_desktop_wsl2"),
        ):
            absent = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, missing_command=command)
            assert absent["ok"] is False and assertion in absent["failed"], (
                f"缺少 {command} 必须产生稳定的 {assertion} 失败：{absent}"
            )
            thrown = _run_runner_platform_helper_mock(PLATFORM_HELPER_PS1, throwing_command=command)
            assert thrown["ok"] is False and assertion in thrown["failed"], (
                f"{command} 异常必须产生稳定的 {assertion} 失败：{thrown}"
            )

        nul_mutant = helper_run.replace('$wslProbe.text -replace "`0", ""', "$wslProbe.text", 1)
        assert nul_mutant != helper_run, "未能定位生产 helper 的 WSL NUL 清理以施加 mutation"
        mutant_path = helper_tmp / "runner-identity-platform-no-nul.ps1"
        mutant_path.write_text(nul_mutant, encoding="utf-8-sig")
        nul_regression = _run_runner_platform_helper_mock(mutant_path, wsl_has_nul=True)
        assert nul_regression["ok"] is False and "docker_desktop_wsl2" in nul_regression["failed"], (
            "移除 NUL 清理后必须复现 PS5 WSL2 假阴性，证明生产兼容修复有回归牙齿"
        )
    finally:
        # 该目录由 _unique_tmp_dir 独占创建；清理失败必须显式失败，不能以 ignore_errors
        # 掩盖残留而让后续 mutation 使用过期临时脚本。
        _remove_tree_strict(helper_tmp)

    # 本地 wrapper 是 Phase 0 的权威验收入口之一：即使 CI 已经 preflight，wrapper 自己
    # 写出的 toolchain BLOCKED 与最终 receipt 也必须可复核 Windows/WSL/Docker 身份。
    wrapper_source = RUNNER_IDENTITY_WRAPPER_PS1.read_text(encoding="utf-8-sig")
    wrapper_facts = _analyze_runner_identity_platform_facts(wrapper_source)
    assert wrapper_facts["parseErrors"] == 0, f"runner wrapper 必须可由 PowerShell AST 解析：{wrapper_facts}"
    assert wrapper_facts["valid"] is True, (
        "runner wrapper 的 toolchain BLOCKED 必须克隆完整 platform facts，最终 receipt 必须逐项留存；"
        f"实际={wrapper_facts}"
    )
    # GitHub Windows runner 用 pwsh 7 执行合同测试；强制开启 StrictMode 后必须仍能解析真实
    # wrapper，避免 PowerShell 5.1 的宽松属性访问掩盖候选 AST 的 null/类型缺陷。
    strict_facts_analyzer = _RUNNER_IDENTITY_FACTS_AST_ANALYZER.replace(
        "$ErrorActionPreference = 'Stop'",
        "$ErrorActionPreference = 'Stop'\nSet-StrictMode -Version Latest",
        1,
    )
    strict_wrapper_facts = _run_ps_analyzer(strict_facts_analyzer, wrapper_source)
    assert strict_wrapper_facts["parseErrors"] == 0, (
        "StrictMode 下 runner wrapper 必须仍可由事实 AST 分析器解析："
        f"{strict_wrapper_facts}"
    )
    assert strict_wrapper_facts["valid"] is True, (
        "StrictMode 下 runner wrapper 的 toolchain clone 和最终事实映射必须完整："
        f"{strict_wrapper_facts}"
    )
    final_endpoint_mutant = wrapper_source.rsplit(
        'docker_context_endpoint = $platform.facts["docker_context_endpoint"]', 1,
    )
    assert len(final_endpoint_mutant) == 2, "未能定位最终 receipt 的 Docker context endpoint 映射"
    final_endpoint_mutant_source = final_endpoint_mutant[0] + (
        '# docker_context_endpoint = $platform.facts["docker_context_endpoint"]'
    ) + final_endpoint_mutant[1]
    final_endpoint_analysis = _analyze_runner_identity_platform_facts(final_endpoint_mutant_source)
    assert final_endpoint_analysis["finalMappingsExact"] is False, (
        "注释掉最终 receipt 的真实 endpoint 映射后 AST 必须拒绝，不能由注释或其他 receipt 代偿"
    )
    clone_mutant = wrapper_source.replace("$platform.facts.GetEnumerator()", "@().GetEnumerator()", 1)
    assert clone_mutant != wrapper_source, "未能定位 toolchain receipt 的 platform facts clone"
    clone_analysis = _analyze_runner_identity_platform_facts(clone_mutant)
    assert clone_analysis["toolchainCloneExact"] is False, (
        "将 toolchain clone 改为空集合后 AST 必须拒绝，不能仅因 observable_facts 非空而放行"
    )

    image_code = _strip_ps_comments(str(_runner_identity_image_step().get("run", "")))
    expected_image_call = (
        "Test-RunnerIdentityPlatform -ExpectedCandidateSha $env:EXPECTED_CANDIDATE_SHA -RequireImage $true"
    )
    assert expected_image_call in image_code, (
        "pull 后必须用同一 helper 复核 Linux image OS，不能只检查镜像名称存在"
    )

    # runner_identity 是非 sqlite receipt，过去仅校验 run/candidate，真实 exe 被替换时
    # 仍可能放行。上方已结构化断言 workflow 以 Get-FileHash 取 exe；这里用格式合法的
    # 摘要直接让生产盖章器/validator 对缺失、全零、错值逐一拒绝，避免重复构建 Rust probe。
    tmp_dir = _unique_tmp_dir("runner-identity-probe-binding")
    try:
        probe_digest = "sha256:" + "a" * 64
        receipt_path = tmp_dir / "runner_identity_receipt.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "spike": "runner_identity",
                    "status": "PASS",
                    "assertions": [{"id": "runner_identity_fixture", "passed": True}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        run_id = "r6-" + uuid.uuid4().hex
        run_nonce = uuid.uuid4().hex
        candidate_sha = "a" * 40
        stamp_env = os.environ.copy()
        stamp_env.update(
            {
                "SPIKE_RUN_ID": run_id,
                "SPIKE_RUN_NONCE": run_nonce,
                "SPIKE_CANDIDATE_SHA": candidate_sha,
            }
        )
        stamped = subprocess.run(
            [
                sys.executable,
                str(STAMP_RUN_BINDING_PY),
                str(receipt_path),
                "runner_identity",
                "--probe-digest",
                probe_digest,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO_ROOT),
            env=stamp_env,
            timeout=60,
        )
        assert stamped.returncode == 0, (
            "生产盖章器必须接受真实 exe digest："
            f"stdout={stamped.stdout!r} stderr={stamped.stderr!r}"
        )
        good = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert good["runBinding"]["probeDigest"] == probe_digest
        verdict = _pwsh_validate_receipt(
            good, False, name="runner_identity", run_id=run_id, run_nonce=run_nonce,
            candidate_sha=candidate_sha, probe_digest=probe_digest,
        )
        assert verdict["ok"] is True, f"真实绑定必须通过：{verdict}"

        for label, bad_digest in (
            ("missing", None),
            ("zero", "sha256:" + "0" * 64),
            ("wrong", "sha256:" + "f" * 64),
            ("uppercase_receipt", probe_digest.upper()),
        ):
            mutated = json.loads(json.dumps(good))
            if bad_digest is None:
                del mutated["runBinding"]["probeDigest"]
            else:
                mutated["runBinding"]["probeDigest"] = bad_digest
            verdict = _pwsh_validate_receipt(
                mutated, False, name="runner_identity", run_id=run_id, run_nonce=run_nonce,
                candidate_sha=candidate_sha, probe_digest=probe_digest,
            )
            assert verdict["ok"] is False, (
                f"probeDigest={label} 必须被生产 validator 拒绝：{verdict}"
            )
            assert verdict["status"] == "BINDING_MISMATCH", (
                f"probeDigest={label} 必须报 BINDING_MISMATCH，而非被静默跳过：{verdict}"
            )

        # 摘要是字节身份，不是普通文本：期望值仅大小写不同也必须拒绝。该反例与上面的
        # receipt 大写反例共同锁住 -cnotmatch/-cne，防止 PowerShell 默认大小写不敏感比较。
        uppercase_expected = _pwsh_validate_receipt(
            good, False, name="runner_identity", run_id=run_id, run_nonce=run_nonce,
            candidate_sha=candidate_sha, probe_digest=probe_digest.upper(),
        )
        assert uppercase_expected["ok"] is False and uppercase_expected["status"] == "BINDING_MISMATCH", (
            f"仅大小写不同的 expected probeDigest 必须被拒绝：{uppercase_expected}"
        )

        # runner_identity 是实际二进制身份绑定的唯一消费者：遗漏 --probe-digest 或用全零占位
        # 都不能被盖章器默许，否则 CI 只绑源码候选而没有绑已执行的 probe。
        missing_digest = subprocess.run(
            [sys.executable, str(STAMP_RUN_BINDING_PY), str(receipt_path), "runner_identity"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
            env=stamp_env, timeout=60,
        )
        assert missing_digest.returncode != 0 and "STAMP_PROBE_DIGEST_REQUIRED" in missing_digest.stderr
        zero_digest = subprocess.run(
            [
                sys.executable, str(STAMP_RUN_BINDING_PY), str(receipt_path), "runner_identity",
                "--probe-digest", "sha256:" + "0" * 64,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(REPO_ROOT),
            env=stamp_env, timeout=60,
        )
        assert zero_digest.returncode != 0 and "STAMP_INVALID_PROBE_DIGEST" in zero_digest.stderr

        # validator 接受的是外部 receipt，任何 JSON 顶层类型、断言布尔或 required 字段的
        # 异常都必须稳定 fail-closed，绝不能让 PowerShell 的隐式转换或属性访问异常绕过。
        base = {
            "spike": "runner_identity",
            "status": "PASS",
            "assertions": [{"name": "fixture", "passed": True}],
            "runBinding": good["runBinding"],
        }
        malformed_cases: tuple[tuple[str, Any], ...] = (
            ("null_top", None),
            ("array_top", []),
            ("string_top", "not-an-object"),
            ("null_assertion", {**base, "assertions": [None]}),
            ("string_passed", {**base, "assertions": [{"name": "fixture", "passed": "true"}]}),
            (
                "missing_required",
                {**base, "subResults": [{"aspect": "disk_full", "status": "PASS"}]},
            ),
            (
                "string_required",
                {**base, "subResults": [{"aspect": "disk_full", "status": "PASS", "required": "false"}]},
            ),
            ("empty_run_binding", {**base, "runBinding": {}}),
            (
                "missing_run_nonce",
                {**base, "runBinding": {k: v for k, v in good["runBinding"].items() if k != "runNonce"}},
            ),
            (
                "missing_run_id",
                {**base, "runBinding": {k: v for k, v in good["runBinding"].items() if k != "runId"}},
            ),
            (
                "missing_candidate_sha",
                {**base, "runBinding": {k: v for k, v in good["runBinding"].items() if k != "candidateSha"}},
            ),
            (
                "missing_binding_spike",
                {**base, "runBinding": {k: v for k, v in good["runBinding"].items() if k != "spike"}},
            ),
            (
                "required_subresult_missing_status",
                {**base, "subResults": [{"aspect": "disk_full", "required": True}]},
            ),
            (
                "optional_subresult_missing_status",
                {**base, "subResults": [{"aspect": "disk_full", "required": False}]},
            ),
            (
                "subresult_missing_aspect",
                {**base, "subResults": [{"status": "BLOCKED_UNCERTIFIED", "required": True}]},
            ),
        )
        for label, malformed in malformed_cases:
            verdict = _pwsh_validate_receipt(
                malformed, False, name="runner_identity", run_id=run_id, run_nonce=run_nonce,
                candidate_sha=candidate_sha, probe_digest=probe_digest,
            )
            assert verdict["ok"] is False and verdict["status"] == "INVALID", (
                f"坏 receipt {label} 必须稳定 fail-closed，实际={verdict}"
            )
    finally:
        _remove_tree_strict(tmp_dir)


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_preheat_ast_immune_to_unused_string_mutation() -> None:
    """P1-3 mutation 实证：把真实 dev.ps1 调用行注释掉 + 追加含完整命令的**未使用字符串**后，
    AST 必须报 devInvokes=False；同时 guard 中嵌套/字符串形式的 ``exit 1`` 也不得冒充直接失败退出。"""
    step = _preheat_step()
    run = str(step.get("run", ""))
    # 首行成功退出会让真正 dev.ps1 预热不可达；不能只检查“后面存在命令和 guard”。
    first_line_exit_preheat = _analyze_ci_run_block("exit 0\n" + run)
    assert first_line_exit_preheat["parseErrors"] == 0
    assert first_line_exit_preheat["preheatValid"] is False, (
        f"预热 run block 第一行 exit 0 必须被拒绝：{first_line_exit_preheat}"
    )
    for terminal in ("exit", "exit $code", "return"):
        shorted = _analyze_ci_run_block(terminal + "\n" + run)
        assert shorted["parseErrors"] == 0 and shorted["preheatValid"] is False, (
            f"预热 run block 首行 {terminal!r} 必须被拒绝：{shorted}"
        )
    # 在必需动作前的失败退出只按 ExitStatementAst 整数常量 1 判定；
    # PowerShell 关键字大小写不敏感，`exit 1` 与 `Exit 1` 必须同样合法。
    for terminal in ("exit 1", "Exit 1"):
        fail_closed = _analyze_ci_run_block(terminal + "\n" + run)
        assert fail_closed["parseErrors"] == 0 and fail_closed["preheatValid"] is True, (
            f"预热分析器必须按 AST 常量 1 识别 {terminal!r}：{fail_closed}"
        )
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

    # guard 即使末尾仍留有 exit 1，前置 return 或 exit 0 也会让失败路径提前成功返回；
    # 必须拒绝这两种短路，锁定当前唯一允许的 Write-Error → exit 1 形状。
    early_return = run.replace("Write-Error ", "return\n            Write-Error ", 1)
    assert early_return != run, "未能定位预热 guard 的 Write-Error 以施加 early-return mutation"
    returned = _analyze_ci_run_block(early_return)
    assert returned["parseErrors"] == 0, f"early-return mutation 后仍应可解析；结果={returned}"
    assert returned["guardExitsOne"] is False and returned["preheatValid"] is False, (
        "guard 中前置 return 后末尾 exit 1 不可达，AST 必须拒绝该假 fail-closed 形状"
    )

    early_exit_zero = run.replace("Write-Error ", "exit 0\n            Write-Error ", 1)
    assert early_exit_zero != run, "未能定位预热 guard 的 Write-Error 以施加 early-exit0 mutation"
    exited_zero = _analyze_ci_run_block(early_exit_zero)
    assert exited_zero["parseErrors"] == 0, f"early-exit0 mutation 后仍应可解析；结果={exited_zero}"
    assert exited_zero["guardExitsOne"] is False and exited_zero["preheatValid"] is False, (
        "guard 中前置 exit 0 后末尾 exit 1 不可达，AST 必须拒绝该假 fail-closed 形状"
    )

    # 短路也不能藏在 Write-Error 的可展开字符串子表达式中：执行消息构造时会先执行 exit 0，
    # 因而后续的错误日志和 exit 1 都不可达，必须由深层 AST 检查拒绝。
    nested_exit_zero = run.replace('Write-Error "', 'Write-Error "$(exit 0) ', 1)
    assert nested_exit_zero != run, "未能定位预热 guard 的 Write-Error 以施加嵌套 exit0 mutation"
    nested_zero = _analyze_ci_run_block(nested_exit_zero)
    assert nested_zero["parseErrors"] == 0, f"嵌套 exit0 mutation 后仍应可解析；结果={nested_zero}"
    assert nested_zero["guardExitsOne"] is False and nested_zero["preheatValid"] is False, (
        "Write-Error 子表达式中的 exit 0 也会短路，AST 必须拒绝该假 fail-closed 形状"
    )

    # 新镜像准备规则的 mutation 也放在既有节点中，保持冻结测试节点数量不变。注释整行或
    # 仅保留未使用字符串都不能算作 Docker 命令；守卫提前、参数折叠、原生命令插队均必须红。
    image_run = str(_runner_identity_image_step().get("run", ""))
    # 镜像拉取同样不能被 run block 首行成功退出短路；后续 pull/inspect 仍存在并不等于可达。
    first_line_exit_image = _analyze_runner_identity_image_run("exit 0\n" + image_run)
    assert first_line_exit_image["parseErrors"] == 0
    assert first_line_exit_image["imagePreparationValid"] is False, (
        f"镜像准备 run block 第一行 exit 0 必须被拒绝：{first_line_exit_image}"
    )
    for terminal in ("exit", "exit $code", "return"):
        shorted = _analyze_runner_identity_image_run(terminal + "\n" + image_run)
        assert shorted["parseErrors"] == 0 and shorted["imagePreparationValid"] is False, (
            f"镜像准备 run block 首行 {terminal!r} 必须被拒绝：{shorted}"
        )
    for terminal in ("exit 1", "Exit 1"):
        fail_closed = _analyze_runner_identity_image_run(terminal + "\n" + image_run)
        assert fail_closed["parseErrors"] == 0 and fail_closed["imagePreparationValid"] is True, (
            f"镜像分析器必须按 AST 常量 1 识别 {terminal!r}：{fail_closed}"
        )
    commented_pull = _mut_comment_exact_command(
        image_run, _RUNNER_IMAGE_PULL_COMMAND, add_unused_string=False
    )
    assert commented_pull != image_run, "未能定位真实 docker pull 行以施加整行注释 mutation"
    image_a = _analyze_runner_identity_image_run(commented_pull)
    assert image_a["parseErrors"] == 0
    assert image_a["pullCommandCount"] == 0 and image_a["imagePreparationValid"] is False

    unused_pull = _mut_comment_exact_command(
        image_run, _RUNNER_IMAGE_PULL_COMMAND, add_unused_string=True
    )
    assert unused_pull != image_run, "未能定位真实 docker pull 行以施加未使用字符串 mutation"
    image_a = _analyze_runner_identity_image_run(unused_pull)
    assert image_a["parseErrors"] == 0
    assert image_a["pullCommandCount"] == 0 and image_a["imagePreparationValid"] is False

    folded_pull = image_run.replace(
        _RUNNER_IMAGE_PULL_COMMAND, 'docker "pull python:3.12-slim"', 1
    )
    assert folded_pull != image_run, "未能定位真实 docker pull 行以施加参数折叠 mutation"
    image_a = _analyze_runner_identity_image_run(folded_pull)
    assert image_a["parseErrors"] == 0
    assert image_a["pullCommandCount"] == 0 and image_a["imagePreparationValid"] is False

    early_pull_guard = _mut_move_guard_before_command(image_run, _RUNNER_IMAGE_PULL_COMMAND)
    assert early_pull_guard != image_run, "未能定位 docker pull 守卫以施加守卫提前 mutation"
    image_a = _analyze_runner_identity_image_run(early_pull_guard)
    assert image_a["parseErrors"] == 0
    assert image_a["pullImmediatelyGuarded"] is False and image_a["imagePreparationValid"] is False

    inspect_interleaved = _mut_insert_native_between_command_and_guard(
        image_run, _RUNNER_IMAGE_INSPECT_COMMAND
    )
    assert inspect_interleaved != image_run, "未能定位 docker inspect 行以施加原生命令插队 mutation"
    image_a = _analyze_runner_identity_image_run(inspect_interleaved)
    assert image_a["parseErrors"] == 0
    assert image_a["inspectImmediatelyGuarded"] is False and image_a["imagePreparationValid"] is False

    # 镜像步骤与工具链预热共用同一 fail-closed 合同：首个 guard 发生早期 return / exit 0
    # 时，不能仅因块末尾还留有 exit 1 就被误判为安全。
    image_early_return = image_run.replace("Write-Error ", "return\n            Write-Error ", 1)
    assert image_early_return != image_run, "未能定位镜像 guard 的 Write-Error 以施加 early-return mutation"
    image_a = _analyze_runner_identity_image_run(image_early_return)
    assert image_a["parseErrors"] == 0
    assert image_a["pullImmediatelyGuarded"] is False and image_a["imagePreparationValid"] is False

    image_early_exit_zero = image_run.replace("Write-Error ", "exit 0\n            Write-Error ", 1)
    assert image_early_exit_zero != image_run, "未能定位镜像 guard 的 Write-Error 以施加 early-exit0 mutation"
    image_a = _analyze_runner_identity_image_run(image_early_exit_zero)
    assert image_a["parseErrors"] == 0
    assert image_a["pullImmediatelyGuarded"] is False and image_a["imagePreparationValid"] is False

    image_nested_exit_zero = image_run.replace(
        'Write-Error "', 'Write-Error "$(exit 0) ', 1
    )
    assert image_nested_exit_zero != image_run, "未能定位镜像 guard 的 Write-Error 以施加嵌套 exit0 mutation"
    image_a = _analyze_runner_identity_image_run(image_nested_exit_zero)
    assert image_a["parseErrors"] == 0
    assert image_a["pullImmediatelyGuarded"] is False and image_a["imagePreparationValid"] is False

    # 最终 execute 不能只包含这些关键字：必须由 AST 证明 wrapper 的真实 native 调用、
    # LASTEXITCODE 捕获、非零拒绝、exe digest、盖章及 validator 的顺序没有可插队空隙。
    execute_run = str(_required_named_step(
        _runner_identity_certified_steps(), RUNNER_IDENTITY_EXECUTE_STEP_NAME
    ).get("run", ""))
    execute_a = _analyze_runner_identity_execute_run(execute_run)
    assert execute_a["parseErrors"] == 0, f"execute run 必须可由 PowerShell AST 解析：{execute_a}"
    assert execute_a["executeValid"] is True, f"最终认证执行值流不完整或顺序被绕过：{execute_a}"
    # 最终 wrapper→stamp→validator 链的每个元素即使齐全，首行 exit 0 也会让认证从未发生。
    first_line_exit_execute = _analyze_runner_identity_execute_run("exit 0\n" + execute_run)
    assert first_line_exit_execute["parseErrors"] == 0
    assert first_line_exit_execute["executeValid"] is False, (
        f"最终 execute run block 第一行 exit 0 必须被拒绝：{first_line_exit_execute}"
    )
    for terminal in ("exit", "return"):
        shorted = _analyze_runner_identity_execute_run(terminal + "\n" + execute_run)
        assert shorted["parseErrors"] == 0 and shorted["executeValid"] is False, (
            f"最终 execute run block 首行 {terminal!r} 必须被拒绝：{shorted}"
        )

    # fail-closed exit 的合法性必须来自 AST 常量整数 1；仅把关键字改成 `Exit 1`
    # 不应使认证链失效，否则分析器实际上仍依赖源码文本大小写。
    execute_casefolded_exit_one = execute_run.replace("exit 1", "Exit 1")
    assert execute_casefolded_exit_one != execute_run
    casefolded_execute = _analyze_runner_identity_execute_run(execute_casefolded_exit_one)
    assert casefolded_execute["parseErrors"] == 0 and casefolded_execute["executeValid"] is True, (
        f"execute 分析器必须按 ExitStatementAst 常量值识别 exit 1：{casefolded_execute}"
    )

    # Find-StatementIndex 会越过普通顶层语句；因此必须在完整 wrapper→hash→stamp→validator
    # 保护区间扫描成功终止，而不是仅靠各锚点仍按相对顺序出现。每个真实间隙均覆盖
    # exit 0，并在同一位置覆盖 bare exit/return，保持既有 pytest 节点数量不变。
    execute_gaps = (
        ("wrapper_to_hash", "after", '$preStampMtime = (Get-Item -LiteralPath $receiptPath).LastWriteTime'),
        (
            "hash_to_stamp", "after",
            '$probeDigest = "sha256:" + '
            '(Get-FileHash -Algorithm SHA256 -LiteralPath $probePath).Hash.ToLowerInvariant()',
        ),
        ("stamp_to_validator", "before", '. "$PWD/scripts/spikes/_receipt-validator.ps1"'),
        ("validator_to_guard", "before", 'if (-not $v.ok -or $v.status -ne "PASS") {'),
    )
    for gap, placement, anchor in execute_gaps:
        assert anchor in execute_run, f"未能定位 execute 间隙 {gap} 的真实锚点"
        for terminal in ("exit 0", "exit", "return"):
            replacement = anchor + "\n" + terminal if placement == "after" else terminal + "\n" + anchor
            mutated = execute_run.replace(anchor, replacement, 1)
            analysis = _analyze_runner_identity_execute_run(mutated)
            assert analysis["parseErrors"] == 0
            assert analysis.get("unsafeTerminalInExecuteChain") is True and analysis["executeValid"] is False, (
                f"execute {gap} 插入 {terminal!r} 必须被全链 AST 扫描拒绝：{analysis}"
            )

    # 注释真实 wrapper 再塞未使用字符串、把参数折叠、捕获后插入另一原生命令，或在拒绝
    # guard 内提前 exit 0，均不得被任何文字匹配误当为完整认证链。
    wrapper_command = '& powershell -NoProfile -ExecutionPolicy Bypass -File "scripts/spikes/test-runner-identity.ps1"'
    commented_execute = _mut_comment_exact_command(execute_run, wrapper_command, add_unused_string=True)
    assert commented_execute != execute_run
    assert _analyze_runner_identity_execute_run(commented_execute)["executeValid"] is False

    folded_execute = execute_run.replace(
        wrapper_command,
        '& powershell "-NoProfile -ExecutionPolicy Bypass -File scripts/spikes/test-runner-identity.ps1"',
        1,
    )
    assert folded_execute != execute_run
    assert _analyze_runner_identity_execute_run(folded_execute)["executeValid"] is False

    interleaved_execute = execute_run.replace(
        "$wrapperExit = $LASTEXITCODE", "$wrapperExit = $LASTEXITCODE\ncmd /d /c ver | Out-Null", 1
    )
    assert interleaved_execute != execute_run
    assert _analyze_runner_identity_execute_run(interleaved_execute)["executeValid"] is False

    early_exit_execute = execute_run.replace(
        'Write-Error "[runner_identity] wrapper exit=$wrapperExit，拒绝认证"',
        'exit 0\n  Write-Error "[runner_identity] wrapper exit=$wrapperExit，拒绝认证"',
        1,
    )
    assert early_exit_execute != execute_run
    assert _analyze_runner_identity_execute_run(early_exit_execute)["executeValid"] is False

_PREHEAT_COMMAND = (
    '& "$PWD/scripts/dev.ps1" -- -- rustup toolchain install '
    'stable-x86_64-pc-windows-gnu --profile minimal'
)
_RUNNER_IMAGE_PULL_COMMAND = "docker pull python:3.12-slim"
_RUNNER_IMAGE_INSPECT_COMMAND = "docker image inspect python:3.12-slim"


def _mut_comment_exact_command(run: str, command: str, *, add_unused_string: bool) -> str:
    """注释完整原生命令行；可选未使用字符串用于证明 AST 不把文本当成实际执行。"""
    out: list[str] = []
    changed = False
    for line in run.split("\n"):
        if not changed and line.strip() == command:
            indent = line[: len(line) - len(line.lstrip())]
            out.append(indent + "# " + line.lstrip())
            if add_unused_string:
                out.append(indent + f'$unusedDockerCommand = "{command}"')
            changed = True
        else:
            out.append(line)
    return "\n".join(out)


def _mut_move_guard_before_command(run: str, command: str) -> str:
    """把指定命令的紧邻守卫提前，验证 $LASTEXITCODE 守卫不能只在同一 run block 出现即可。"""
    lines = run.split("\n")
    command_index = next((i for i, line in enumerate(lines) if line.strip() == command), None)
    if command_index is None:
        return run
    guard_index = next(
        (
            i
            for i in range(command_index + 1, len(lines))
            if lines[i].strip() == "if ($LASTEXITCODE -ne 0) {"
        ),
        None,
    )
    if guard_index is None:
        return run
    guard_end = next(
        (i for i in range(guard_index + 1, len(lines)) if lines[i].strip() == "}"),
        None,
    )
    if guard_end is None:
        return run
    guard = lines[guard_index : guard_end + 1]
    return "\n".join(
        lines[:command_index]
        + guard
        + [lines[command_index]]
        + lines[command_index + 1 : guard_index]
        + lines[guard_end + 1 :]
    )


def _mut_insert_native_between_command_and_guard(run: str, command: str) -> str:
    """在命令与守卫之间插入原生命令，模拟 $LASTEXITCODE 被覆盖的回归。"""
    return run.replace(command, command + "\n          & cmd /c exit 0", 1)


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
        "spikes/_runner-identity-platform.ps1",
    ], "scriptDigests 必须绑定冻结的 7 个生产脚本；新增共享平台 helper 不得成为未留痕依赖"
    wired = re.search(
        r'"spikes/_durable-io-receipt\.ps1"\s*=\s*Get-FileSha256\s*\(\s*'
        r'Join-Path\s+\$PSScriptRoot\s+"spikes/_durable-io-receipt\.ps1"\s*\)',
        block,
    )
    assert wired is not None, (
        "scriptDigests 必须把 'spikes/_durable-io-receipt.ps1' 接到 "
        "Get-FileSha256(Join-Path $PSScriptRoot \"spikes/_durable-io-receipt.ps1\")（去注释代码解析）"
    )
    platform_wired = re.search(
        r'"spikes/_runner-identity-platform\.ps1"\s*=\s*Get-FileSha256\s*\(\s*'
        r'Join-Path\s+\$PSScriptRoot\s+"spikes/_runner-identity-platform\.ps1"\s*\)',
        block,
    )
    assert platform_wired is not None, "共享 runner_identity 平台 helper 必须进入动态 scriptDigests"


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
    # C-1 不复制一套脆弱的正则审计：它必须实际重跑包含 YAML 结构解析、PowerShell AST 与
    # 动态 mutation 的同一 CI 合同测试。旧的 Test-RunnerIdentityCiContract 只能搜 token，
    # 无法证明 early exit/未使用字符串等绕过路径，因此不得继续作为验收判据。
    assert not re.search(r"function\s+Test-RunnerIdentityCiContract\b", code), (
        "C-1 不得保留并调用弱字符串版 runner_identity CI 审计"
    )
    assert re.search(
        r"uv\s+run\s+--locked\s+python\s+-m\s+pytest\s+tests/contract/test_ci_blocker_fixes\.py\s+-q",
        code,
    ), "C-1 必须重跑结构化 CI contract 测试，而非只依赖 A-1 的历史结果"
    for required in (
        'if ($name -eq "runner_identity")', "Get-WorktreeTargetDir -WorktreeRoot $REPO_ROOT",
        "runner_identity.exe", '"--probe-digest", $expectProbeDigest', "ExpectProbeDigest  = $expectProbeDigest",
    ):
        assert required in code, f"本地验收 runner_identity 缺少实际 exe probeDigest 绑定：{required!r}"
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
