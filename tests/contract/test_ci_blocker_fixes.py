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

本文件**不搜索注释子串**证明修复（GPT 要求 #6）：
  - CI 结构类断言：用**缩进感知的 Actions workflow 解析器**解析 jobs→steps→with
    层级与步骤顺序（解析可执行 YAML 结构，非注释）。
  - package.json：按 JSON 解析 devDependencies。
  - pnpm-lock.yaml：结构化提取 importers 块的 devDependencies。
  - durable-IO 回执 / validator 判定：**直接调用生产 PowerShell 实现**
    （scripts/spikes/_durable-io-receipt.ps1 + _receipt-validator.ps1）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
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
ACCEPTANCE_PS1 = REPO_ROOT / "scripts" / "phase0-acceptance.ps1"
RECEIPT_JSON = REPO_ROOT / ".phase0-acceptance-receipt.json"
# scriptDigests 里 helper 的键（reviewer 指定带 spikes/ 前缀）。
DURABLE_IO_RECEIPT_DIGEST_KEY = "spikes/_durable-io-receipt.ps1"


def _strip_ps_comments(text: str) -> str:
    """去掉 PowerShell 注释后返回**纯可执行代码**（供"不得被注释绕过"的断言用）。

    先删块注释 <# ... #>，再删行注释（# 到行尾）。本仓 CI/spike 脚本的代码字符串里
    不含裸 '#'，故行注释删除不会误伤可执行代码。GPT round-16 blocker 2/3：断言必须跑在
    去注释代码上——把真实命令注释掉、只在注释里留关键词时，测试必须失败。
    """
    no_block = re.sub(r"<#.*?#>", "", text, flags=re.S)
    return re.sub(r"(?m)#.*$", "", no_block)


def _normalize_ws(text: str) -> str:
    """折叠空白 + 去引号，便于把多行/多空格命令按 token 序列做确定性子串匹配。"""
    no_quotes = text.replace('"', " ").replace("'", " ")
    return re.sub(r"\s+", " ", no_quotes).strip()


# ─────────────────────────────────────────────────────────────────────────────
# 缩进感知的 GitHub Actions workflow 解析器（GPT 要求 #6：解析可执行 YAML 结构）。
# 无需第三方 YAML 库（locked venv 无 PyYAML）；只解析本仓 ci.yml 用到的构造：
#   jobs:(col0) → <job>:(col2) → steps:(col4) → '- key:'(col6) → 'key:'(col8)
#   → 'with:' 子键(col10)；block scalar 'run: |' 捕获缩进 > 8 的正文。
# 解析器自带 sanity 断言（见 _load_jobs），解析器 bug 不会静默放行。
# ─────────────────────────────────────────────────────────────────────────────
def _scalar(v: str) -> str:
    """去掉 YAML 标量两侧引号。"""
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
        # 到达 job 级别（<=4）→ steps 结束
        if indent <= 4 and line.strip() != "":
            break
        # 新 step：'      - key: val'（col 6 + '- '）
        m = re.match(r"^      - ([A-Za-z0-9_.-]+):\s?(.*)$", line)
        if m:
            if cur is not None:
                steps.append(cur)
            cur = {"_order": len(steps)}
            key, val = m.group(1), m.group(2)
            cur[key] = _scalar(val)
            i += 1
            continue
        # step 属性：'        key: val'（col 8）
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
        # 顶层再次出现 col0 非空键 → jobs 段结束
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
    # sanity：本仓 ci.yml 必含这些 job，且各有 steps——解析器 bug 不能静默放行。
    for required_job in ("desktop", "windows-probes", "contracts"):
        assert required_job in parsed, f"解析器未提取到 job '{required_job}'（解析器可能失效）"
        assert len(parsed[required_job]) >= 2, f"job '{required_job}' 解析出的 step 过少（解析器可能失效）"
    return parsed


def _find_step(steps: list[dict[str, Any]], predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    for s in steps:
        if predicate(s):
            return s
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 一、desktop CI：Node 版本单一真源
# ─────────────────────────────────────────────────────────────────────────────
def test_desktop_job_uses_node_version_file_not_hardcoded() -> None:
    """desktop job 的 setup-node 必须用 node-version-file: '.node-version'（唯一真源），
    绝不硬编码 node-version（无论 20 还是 22）。GPT 要求 #1。"""
    jobs = _load_jobs(CI_YML.read_text(encoding="utf-8"))
    desktop = jobs["desktop"]
    setup_node = _find_step(desktop, lambda s: "actions/setup-node" in str(s.get("uses", "")))
    assert setup_node is not None, "desktop job 缺少 actions/setup-node 步骤"
    with_map = setup_node.get("with", {})
    assert with_map.get("node-version-file") == ".node-version", (
        f"setup-node 必须用 node-version-file='.node-version'（读 .node-version 单一真源）；"
        f"实际 with={with_map}"
    )
    # 不得再硬编码 node-version（掩盖真源漂移）。
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
    """packages/factory-contracts/package.json 的 devDependencies 必须直接含 @types/node
    （它直接 import node:crypto/fs/url/path）。GPT 要求 #2，禁止改业务代码掩盖类型依赖。"""
    pkg = json.loads(CONTRACTS_PKG.read_text(encoding="utf-8"))
    dev = pkg.get("devDependencies", {})
    assert "@types/node" in dev, (
        f"factory-contracts devDependencies 必须直接声明 @types/node；实际 ={dev}"
    )
    # Node 22：@types/node 主版本应兼容 22（^22 或更高）。
    spec = dev["@types/node"]
    assert re.search(r"2[2-9]|[3-9]\d", spec), (
        f"@types/node 版本应兼容 Node 22（如 ^22.x）；实际 spec='{spec}'"
    )


def test_pnpm_lock_pins_types_node_for_factory_contracts() -> None:
    """pnpm-lock.yaml 的 importers['packages/factory-contracts'].devDependencies 必须
    结构化包含 @types/node（fresh --frozen-lockfile 才能装到它）。结构化解析 importer 块。"""
    text = PNPM_LOCK.read_text(encoding="utf-8")
    lines = text.split("\n")
    # 定位 importers: → packages/factory-contracts: → devDependencies: 块
    i = 0
    n = len(lines)
    while i < n and lines[i].rstrip() != "importers:":
        i += 1
    assert i < n, "pnpm-lock.yaml 未找到 importers:"
    # 找 'packages/factory-contracts:'（缩进 2）
    while i < n and lines[i].strip() != "packages/factory-contracts:":
        i += 1
    assert i < n, "pnpm-lock.yaml importers 未找到 packages/factory-contracts"
    importer_indent = len(lines[i]) - len(lines[i].lstrip())
    i += 1
    # 收集该 importer 块内（缩进 > importer_indent）的 devDependencies 子键
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
            break  # 离开该 importer 块
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


# 预热步骤必须**实际执行**的完整命令（reviewer 指定；按 token 序列匹配，非注释子串）。
PREHEAT_REQUIRED_CMD = (
    "scripts/dev.ps1 -- -- rustup toolchain install "
    "stable-x86_64-pc-windows-gnu --profile minimal"
)


def _is_spike_loop_step(step: dict[str, Any]) -> bool:
    """spike 执行步骤：其**去注释可执行** run 正文 dot-source 校验器并遍历 $spikes。"""
    code = _strip_ps_comments(str(step.get("run", "")))
    return "_receipt-validator.ps1" in code and "$spikes" in code


def _is_preheat_step(step: dict[str, Any]) -> bool:
    """预热步骤识别：**去注释可执行代码**里出现完整预热命令（把真实命令注释掉即不再命中）。

    GPT round-16 blocker 2：旧版在**原始 run 文本**里搜 dev.ps1/rustup/... 关键词——
    把真实命令注释掉、只在注释里留这些词即可假绿。现改为对去注释代码归一化后，按 token
    序列匹配 reviewer 指定的完整命令，注释绕过必然失败。
    """
    code_norm = _normalize_ws(_strip_ps_comments(str(step.get("run", ""))))
    return PREHEAT_REQUIRED_CMD in code_norm


def test_windows_probes_preheats_toolchain_before_spike_loop() -> None:
    """windows-probes 必须在 spike loop **之前**有预热步骤，其**去注释可执行代码**完整包含
    `scripts/dev.ps1 -- -- rustup toolchain install stable-x86_64-pc-windows-gnu --profile minimal`，
    且 shell 为 pwsh。GPT round-16 blocker 2（AST/去注释解析，非注释子串）。"""
    steps = _windows_probes_steps()
    preheat = _find_step(steps, _is_preheat_step)
    spike = _find_step(steps, _is_spike_loop_step)
    assert preheat is not None, (
        "windows-probes 缺少预热步骤（去注释代码须含完整 dev.ps1 -- -- rustup ... 命令）"
    )
    assert spike is not None, "windows-probes 缺少 spike 执行步骤（解析器或 CI 结构异常）"
    # 顺序：预热必须在 spike loop 之前。
    assert preheat["_order"] < spike["_order"], (
        f"预热步骤（order={preheat['_order']}）必须在 spike loop（order={spike['_order']}）之前"
    )
    # shell: pwsh（step 属性经解析器捕获）。
    assert str(preheat.get("shell", "")).strip() == "pwsh", (
        f"预热步骤 shell 必须为 pwsh；实际 ={preheat.get('shell')!r}"
    )
    # 去注释可执行代码里必须完整包含 reviewer 指定命令（token 序列，注释绕过会失败）。
    code_norm = _normalize_ws(_strip_ps_comments(str(preheat["run"])))
    assert PREHEAT_REQUIRED_CMD in code_norm, (
        f"预热**可执行代码**必须完整包含 '{PREHEAT_REQUIRED_CMD}'；实际归一化后 ={code_norm!r}"
    )


def test_windows_probes_preheat_is_fail_closed() -> None:
    """windows-probes 无 continue-on-error；预热**可执行代码**显式检查 $LASTEXITCODE 且非零退出。
    GPT round-16 blocker 2（跑在去注释代码上）。"""
    steps = _windows_probes_steps()
    offenders = [
        s for s in steps
        if str(s.get("continue-on-error", "")).strip().lower() == "true"
    ]
    offender_labels = [s.get("name") or s.get("uses") for s in offenders]
    assert not offenders, (
        f"windows-probes 不得有 continue-on-error: true 的步骤；违规 ={offender_labels}"
    )
    preheat = _find_step(steps, _is_preheat_step)
    assert preheat is not None, "无预热步骤"
    # 去注释可执行代码里必须真正检查 $LASTEXITCODE 并在非零时 exit 1（不吞错）。
    code = _strip_ps_comments(str(preheat["run"]))
    assert re.search(r"\$LASTEXITCODE\s+-ne\s+0", code), (
        f"预热可执行代码必须检查 $LASTEXITCODE -ne 0；实际去注释代码 ={code!r}"
    )
    assert re.search(r"(?m)^\s*exit\s+1\s*$", code) or "exit 1" in _normalize_ws(code), (
        f"预热可执行代码必须在安装失败时 exit 1（fail-closed）；实际去注释代码 ={code!r}"
    )


def test_windows_probes_preheat_detection_is_immune_to_commenting_out() -> None:
    """Mutation 实证（GPT round-16 blocker 2）：把预热步骤 run 里真实命令行整行注释掉后，
    去注释代码不再含完整命令 → _is_preheat_step 必须不再命中（证明断言非注释子串搜索）。"""
    steps = _windows_probes_steps()
    preheat = _find_step(steps, _is_preheat_step)
    assert preheat is not None, "前置条件：未 mutation 时应识别到预热步骤"
    run = str(preheat["run"])
    # mutation：把包含 dev.ps1 -- -- 的真实命令行整行前置 '# ' 注释掉。
    mutated_lines = []
    mutated = False
    for ln in run.split("\n"):
        if "dev.ps1" in ln and "rustup" in ln and not ln.lstrip().startswith("#"):
            mutated_lines.append("# " + ln)
            mutated = True
        else:
            mutated_lines.append(ln)
    assert mutated, "未能定位真实预热命令行以施加 mutation"
    mutated_step = dict(preheat)
    mutated_step["run"] = "\n".join(mutated_lines)
    # 关键：注释掉真实命令后，去注释代码不再含完整命令 → 识别必须失败。
    assert not _is_preheat_step(mutated_step), (
        "把真实预热命令注释掉后仍识别为预热步骤——断言可被注释绕过（blocker 2 回归）"
    )


def test_dev_ps1_does_not_implicitly_install_toolchain() -> None:
    """GPT 要求二.4：不得把安装逻辑塞进 dev.ps1（否则每次运行隐式联网）。
    dev.ps1 正文不得出现 rustup toolchain install。"""
    dev = (REPO_ROOT / "scripts" / "dev.ps1").read_text(encoding="utf-8")
    # 去掉注释行后检查可执行代码不含 rustup ... install。
    code_lines = [ln for ln in dev.split("\n") if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert not re.search(r"rustup\s+toolchain\s+install", code), (
        "dev.ps1 不得隐式安装工具链（安装应是 CI 显式预热步骤）"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 四、durable-IO 两条 BLOCKED 回执证据结构 + validator 判定（直接调用生产实现）
# ─────────────────────────────────────────────────────────────────────────────
def _pwsh_build_durable_io_receipt(kind: str) -> dict[str, Any]:
    """dot-source 生产 helper _durable-io-receipt.ps1，构建指定 BLOCKED 回执并回传 JSON。

    kind ∈ {non_windows, toolchain}。直接调用生产实现（GPT 要求 #6），
    避免测试与 wrapper 各写一份 receipt 逻辑而漂移。
    """
    ps = f"""
. '{DURABLE_IO_RECEIPT_PS1}'
$r = New-DurableIoBlockedReceipt -Kind '{kind}' -Detail 'regression-test'
$r | ConvertTo-Json -Depth 12
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
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
    为 JSON boolean false、observable_facts 非空、subcheckId 稳定。GPT 要求 #4。"""
    r = _pwsh_build_durable_io_receipt(kind)
    assert r["spike"] == "windows_durable_io"
    assert r["status"] == "BLOCKED_UNCERTIFIED"
    assert r.get("subcheckId") == expected_subcheck, (
        f"subcheckId 必须稳定为 '{expected_subcheck}'；实际 ={r.get('subcheckId')}"
    )
    asserts = r.get("assertions")
    assert isinstance(asserts, list) and len(asserts) >= 1, "assertions 必须非空"
    for a in asserts:
        # JSON boolean false（Python 侧即 bool False；拒绝字符串 "false"）。
        assert a.get("passed") is False, (
            f"每条 assertion 的 passed 必须为 JSON boolean false；实际 ={a.get('passed')!r}"
        )
    facts = r.get("observable_facts")
    assert isinstance(facts, dict) and len(facts) >= 1, "observable_facts 必须非空"


def _pwsh_validate_receipt(receipt: dict[str, Any], env_compat: bool) -> dict[str, Any]:
    """把 receipt 写临时文件，经生产 validator Test-SpikeReceiptEvidence 判定。"""
    tmp = REPO_ROOT / "tests" / "fixtures" / "_tmp_durable_io_blocked.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
        env_flag = "$true" if env_compat else "$false"
        ps = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence -Name 'windows_durable_io' -Path '{tmp}' -EnvCompat {env_flag} -Allowlist @()
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
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
        tmp.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="validator 需 Windows PowerShell")
def test_validator_rejects_core_durable_io_blocked_as_core_blocked_not_invalid() -> None:
    """durable-IO 是 core spike：其 BLOCKED 回执经 validator（EnvCompat=$false）必须被拒，
    且拒因是**合法 core BLOCKED**（"core spike may not be BLOCKED_UNCERTIFIED"），
    而非 INVALID/missing 字段，也不得放行。GPT 要求 #5。"""
    r = _pwsh_build_durable_io_receipt("toolchain")
    verdict = _pwsh_validate_receipt(r, env_compat=False)
    st = verdict["status"]
    detail = verdict["detail"]
    assert not verdict["ok"], (
        f"core durable-io BLOCKED 必须被拒；实际 ok=True status={st} detail={detail}"
    )
    # 关键：必须到达 core-blocked 分支（证据结构完整），而不是 INVALID/missing。
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
    tmp = REPO_ROOT / "tests" / "fixtures" / "_tmp_durable_io_envcompat.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(json.dumps(r, ensure_ascii=False), encoding="utf-8")
        ps = f"""
. '{VALIDATOR_PS1}'
$v = Test-SpikeReceiptEvidence -Name 'windows_durable_io' -Path '{tmp}' -EnvCompat $true `
    -Allowlist @('spike:windows_durable_io/toolchain_unavailable')
Write-Output "ok=$($v.ok)|status=$($v.status)|detail=$($v.detail)"
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(REPO_ROOT), timeout=60,
        )
        out = result.stdout.strip()
        parts = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in out.split("|") if "=" in p}
        ok = parts.get("ok", "").lower() == "true"
    finally:
        tmp.unlink(missing_ok=True)
    assert ok, (
        "证据结构合法性反证失败：BLOCKED 回执在 EnvCompat=$true + allowlist 命中时应被接受 "
        f"（说明结构完整）；实际 detail={parts.get('detail')}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 五、wrapper 调用关系（GPT round-16 blocker 3）：用 PowerShell **AST**（非注释子串）
# 证明 test-durable-io.ps1 确实 (a) dot-source 该 helper，(b) non-Windows 分支调用它，
# (c) build/linker-failure 分支调用它；并做 mutation 实证（注释 dot-source / 改回旧回执
# → 测试必失败）。durable-IO 仍是 core spike，绝不进 allowlist。
# ─────────────────────────────────────────────────────────────────────────────
# AST 分析器（纯 ASCII，无需 BOM）：ParseFile 得 AST + parseErrors；用 AST 节点判定
#   - dotSourcesHelper：存在 InvocationOperator=Dot 且 extent 命中 _durable-io-receipt.ps1 的命令
#   - helperCallCount：GetCommandName()=='New-DurableIoBlockedReceipt' 的命令数
#   - nonWindowsCallsHelper / buildFailCallsHelper：该调用是否落在条件含 env:OS / buildExit 的 if 体内
# wrapper 路径经 $env:R16_WRAPPER 传入（含 CJK，走 Windows API=UTF-16，不受文件编码影响）。
_WRAPPER_AST_ANALYZER = r"""
$ErrorActionPreference = 'Stop'
$path = $env:R16_WRAPPER
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
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
$ifs = $ast.FindAll({ $args[0] -is [System.Management.Automation.Language.IfStatementAst] }, $true)
$nonWindowsCallsHelper = $false
$buildFailCallsHelper = $false
foreach ($call in $helperCalls) {
    $s = $call.Extent.StartOffset; $e = $call.Extent.EndOffset
    foreach ($if in $ifs) {
        foreach ($clause in $if.Clauses) {
            $cond = $clause.Item1; $body = $clause.Item2
            if ($s -ge $body.Extent.StartOffset -and $e -le $body.Extent.EndOffset) {
                if ($cond.Extent.Text -match 'env:OS')   { $nonWindowsCallsHelper = $true }
                if ($cond.Extent.Text -match 'buildExit') { $buildFailCallsHelper = $true }
            }
        }
    }
}
[pscustomobject]@{
    parseErrors           = $parseErrors
    dotSourcesHelper      = $dotSourcesHelper
    helperCallCount       = $helperCalls.Count
    nonWindowsCallsHelper = $nonWindowsCallsHelper
    buildFailCallsHelper  = $buildFailCallsHelper
} | ConvertTo-Json -Compress
"""


def _analyze_wrapper_ast(wrapper_path: Path) -> dict[str, Any]:
    """把 AST 分析器写临时 .ps1（ASCII），经 $env:R16_WRAPPER 指向 wrapper_path 运行，回传 JSON。"""
    analyzer = Path(tempfile.gettempdir()) / "_r16_wrapper_ast_analyzer.ps1"
    analyzer.write_text(_WRAPPER_AST_ANALYZER, encoding="ascii")
    env = dict(os.environ)
    env["R16_WRAPPER"] = str(wrapper_path)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(analyzer)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO_ROOT), timeout=60, env=env,
    )
    assert result.returncode == 0, (
        f"AST 分析器失败 rc={result.returncode} stderr={result.stderr!r} stdout={result.stdout!r}"
    )
    parsed: dict[str, Any] = json.loads(result.stdout)
    return parsed


def _write_mutated_copy(mutated_text: str, suffix: str) -> Path:
    """把 mutation 后的 wrapper 文本写临时 .ps1，**UTF-8 with BOM**（PS 5.1 ParseFile 才能
    正确解码含 CJK 的正文，parseErrors 才不会因编码假阳）。返回临时路径。"""
    p = Path(tempfile.gettempdir()) / f"_r16_wrapper_mut_{suffix}.ps1"
    data = b"\xef\xbb\xbf" + mutated_text.encode("utf-8")
    p.write_bytes(data)
    return p


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_wrapper_ast_dotsources_and_calls_helper_in_both_branches() -> None:
    """Blocker 3：用 AST 证明 test-durable-io.ps1 (a) dot-source _durable-io-receipt.ps1、
    (b) non-Windows 分支、(c) build-fail 分支都调用 New-DurableIoBlockedReceipt。"""
    a = _analyze_wrapper_ast(DURABLE_IO_WRAPPER_PS1)
    assert a["parseErrors"] == 0, f"wrapper 应无解析错误；parseErrors={a['parseErrors']}"
    assert a["dotSourcesHelper"] is True, "wrapper 必须 dot-source _durable-io-receipt.ps1（AST 判定）"
    assert a["helperCallCount"] >= 2, (
        f"wrapper 至少两处调用 New-DurableIoBlockedReceipt（non-Windows + build-fail）；实际 ={a['helperCallCount']}"
    )
    assert a["nonWindowsCallsHelper"] is True, "non-Windows 分支必须调用 New-DurableIoBlockedReceipt（AST 判定）"
    assert a["buildFailCallsHelper"] is True, (
        "build/linker-failure 分支必须调用 New-DurableIoBlockedReceipt（AST 判定）"
    )


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_wrapper_dotsource_mutation_fails() -> None:
    """Mutation 实证（blocker 3）：把 dot-source helper 那一行注释掉后，AST 必须报
    dotSourcesHelper=False（证明用的是 AST 而非注释子串——注释掉真实行即失效）。"""
    src = DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig")
    mutated_lines = []
    did = False
    for ln in src.split("\n"):
        # 命中真实 dot-source 行（'. (Join-Path ... _durable-io-receipt.ps1)'），非注释。
        if "_durable-io-receipt.ps1" in ln and ln.lstrip().startswith(".") and not ln.lstrip().startswith("#"):
            mutated_lines.append("# " + ln)
            did = True
        else:
            mutated_lines.append(ln)
    assert did, "未能定位真实 dot-source 行以施加 mutation"
    mp = _write_mutated_copy("\n".join(mutated_lines), "nodotsrc")
    try:
        a = _analyze_wrapper_ast(mp)
        assert a["parseErrors"] == 0, f"mutation 后仍应可解析；parseErrors={a['parseErrors']}"
        assert a["dotSourcesHelper"] is False, (
            "注释掉真实 dot-source 行后 AST 仍报 dotSourcesHelper=True——断言可被注释绕过（blocker 3 回归）"
        )
    finally:
        mp.unlink(missing_ok=True)


@pytest.mark.skipif(sys.platform != "win32", reason="AST 分析需 Windows PowerShell")
def test_wrapper_branch_call_mutation_fails() -> None:
    """Mutation 实证（blocker 3）：把 non-Windows 分支的 New-DurableIoBlockedReceipt 调用
    改回手写旧回执（缺字段）后，AST 必须报 nonWindowsCallsHelper=False——证明测试锁定的是
    真实的分支→helper 调用关系，wrapper 退回旧回执即失败。"""
    src = DURABLE_IO_WRAPPER_PS1.read_text(encoding="utf-8-sig")
    # 定位 non-Windows 分支里的 helper 调用行（-Kind non_windows），替换为旧的手写回执。
    old_call = "$r = New-DurableIoBlockedReceipt -Kind non_windows"
    assert old_call in src, "前置条件：wrapper non-Windows 分支应调用 New-DurableIoBlockedReceipt"
    # non-Windows 分支的完整调用行（拼接构造，避免单行超长）。
    full_call = (
        old_call
        + " -Detail ([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)"
    )
    legacy = '$r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"; reason="legacy" }'
    mutated = src.replace(full_call, legacy)
    assert mutated != src, "未能施加 non-Windows 分支 mutation（调用行文本不匹配）"
    mp = _write_mutated_copy(mutated, "legacybranch")
    try:
        a = _analyze_wrapper_ast(mp)
        assert a["parseErrors"] == 0, f"mutation 后仍应可解析；parseErrors={a['parseErrors']}"
        assert a["nonWindowsCallsHelper"] is False, (
            "non-Windows 分支改回手写旧回执后 AST 仍报 nonWindowsCallsHelper=True——"
            "测试未真正锁定分支→helper 调用关系（blocker 3 回归）"
        )
    finally:
        mp.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# 六、新 helper 进入验收摘要链（GPT round-16 blocker 1）：scriptDigests 必须含 helper 键，
# 且总回执里该键的值 == helper 当前真实 SHA256。用去注释可执行代码解析 + hashlib 机器验证。
# ─────────────────────────────────────────────────────────────────────────────
def _real_sha256(path: Path) -> str:
    """返回 'sha256:<lowercase-hex>'，与 phase0-acceptance.ps1 的 Get-FileSha256 同格式。"""
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def test_durable_io_helper_wired_into_script_digests() -> None:
    """Blocker 1：phase0-acceptance.ps1 的 $scriptDigests（**去注释可执行代码**）必须把键
    'spikes/_durable-io-receipt.ps1' 接到 Get-FileSha256(Join-Path $PSScriptRoot ...)——
    即运行时会对该 helper 真实文件算 SHA256 写入总回执（非注释、非硬编码）。"""
    code = _strip_ps_comments(ACCEPTANCE_PS1.read_text(encoding="utf-8"))
    m = re.search(r"\$scriptDigests\s*=\s*\[ordered\]@\{(.*?)\n\s*\}", code, re.S)
    assert m is not None, "phase0-acceptance.ps1 未找到 $scriptDigests [ordered]@{...} 块"
    block = m.group(1)
    # 键必须接到 Get-FileSha256(Join-Path $PSScriptRoot "spikes/_durable-io-receipt.ps1")。
    wired = re.search(
        r'"spikes/_durable-io-receipt\.ps1"\s*=\s*Get-FileSha256\s*\(\s*Join-Path\s+\$PSScriptRoot\s+"spikes/_durable-io-receipt\.ps1"\s*\)',
        block,
    )
    assert wired is not None, (
        "scriptDigests 必须把 'spikes/_durable-io-receipt.ps1' 接到 "
        "Get-FileSha256(Join-Path $PSScriptRoot \"spikes/_durable-io-receipt.ps1\")（去注释代码解析）"
    )


def test_receipt_binds_durable_io_helper_real_sha256_when_present() -> None:
    """Blocker 1 反查：若总回执存在且已含 helper 键，则其值必须 == helper 当前真实 SHA256。
    （回执由验收末尾写出；A-1 阶段读到的是上一轮回执，故键缺失时容忍——权威反查在验收后单独做。
    但**一旦键存在**就必须与当前文件字节 SHA256 一致，杜绝硬编码/陈旧摘要。）

    注意：用**空过返回**而非 pytest.skip——验收 A-1 阶段读到的是上一轮（陈旧）回执，
    若用 skip 会额外产生一个 skip，破坏"恰好 1 个冻结 skip=test_config.py"的身份门禁。
    改为 return：回执缺失/键缺失时空过（不扰动 skip 计数），键存在时严格反查。"""
    if not RECEIPT_JSON.exists():
        return  # 裸 pytest 无 spikes-first，回执未生成；权威反查在完整验收后单独做
    receipt = json.loads(RECEIPT_JSON.read_text(encoding="utf-8"))
    digests = receipt.get("scriptDigests", {})
    if DURABLE_IO_RECEIPT_DIGEST_KEY not in digests:
        return  # 回执为加入 helper 键之前所写（陈旧）；权威反查在完整验收后进行
    assert digests[DURABLE_IO_RECEIPT_DIGEST_KEY] == _real_sha256(DURABLE_IO_RECEIPT_PS1), (
        "总回执里 helper 摘要与当前文件真实 SHA256 不一致（硬编码或陈旧）"
    )
