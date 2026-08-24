<#
.SYNOPSIS
    Phase 0 total gate script - 9 contract-layer checks, all must pass before commit.

.DESCRIPTION
    Runs 9 gate checks for Phase 0:
    1. Codegen drift     - python contracts/codegen/generate.py --check
    2. Schema validity   - validate all 13 JSON schemas via pytest
    3. Golden vectors    - pytest test_plan_hash_vectors / test_event_hash_vectors
    4. Chinese coverage  - AST scan; every public function/class needs Chinese docstring
    5. No bare print     - no print()/console.log() outside test code
    6. Secret scan       - 0 hits for token/password/private_key in non-test source
    7. C-drive paths     - no controlled C-drive writes in source
    8. Catalog 47 IDs    - required-test-catalog.v1.json has exactly 47 unique IDs
    9. No doc placeholders - 0 TODO/FIXME/TBD/PLACEHOLDER in docs/

    All checks must pass before commit. Failures are summarised at the end.

.NOTES
    Version: 2.0 (Phase 0 total gate)
    Algorithm version: v1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$REPO_ROOT = Split-Path $PSScriptRoot -Parent
# GPT 第九轮 P0：per-worktree Cargo target 隔离。gate-14 跑 cargo test，Rust 测试
# 二进制把编译期 worktree 路径（env!("CARGO_MANIFEST_DIR")）烧进自身并据此读
# contracts/golden/*.json；共享 cargo-target 会让一个 worktree 跑到另一个编译出的
# 二进制（对方删除即假失败，对方尚存即假通过）。Get-WorktreeTargetDir 按规范化
# $REPO_ROOT 的 SHA-256 摘要派生独立 target；与 dev.ps1 / phase0-acceptance.ps1 同源。
. (Join-Path $PSScriptRoot "_worktree-target.ps1")
. (Join-Path $PSScriptRoot "_rust-gnu-binutils.ps1")
# GPT 第十三轮 P1-1：uv 锁定 Python 探针抽成共享函数 Get-UvLockedPythonVersion
# （scripts/_python-probe.ps1），本门禁前置探针、phase0-acceptance.ps1 的 B-2、回归测试
# 三处共用同一实现；任一入口回退到有漏洞的 2>&1 / 去掉严格解析都能被测试锁定。
. (Join-Path $PSScriptRoot "_python-probe.ps1")
$failures = [System.Collections.Generic.List[string]]::new()
$gate_results = [System.Collections.Generic.List[hashtable]]::new()

function Invoke-GateCheck {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "-- Check: $Name --"
    try {
        & $Action
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        if ($code -ne 0) {
            $failures.Add($Name)
            $gate_results.Add(@{ name = $Name; result = "FAIL" })
            Write-Warning "  [$Name] FAIL (exit $code)"
        } else {
            $gate_results.Add(@{ name = $Name; result = "PASS" })
            Write-Host "  [$Name] PASS"
        }
    } catch {
        Write-Warning "  [$Name] ERROR: $_"
        $failures.Add($Name)
        $gate_results.Add(@{ name = $Name; result = "FAIL" })
    }
}

Set-Location $REPO_ROOT

# ── Python 锁定环境（GPT 第十轮 P1：门禁必须与 A-1 同一个锁定 .venv）───────────
# 旧做法从系统 PATH / 本地安装解析一个"3.12 解释器"，与 phase0-acceptance.ps1 的
# A-1（跑在 uv 锁定的 .venv 里）用的是**两个不同环境**：实测系统 python 为
# pytest 9.0.3，.venv 为 9.1.1。门禁与 A-1 版本漂移 → 同一候选在两处结果可能不一致，
# PASS 无法证明"锁定环境下通过"。
#
# 修法：check.ps1 也强制走 uv 锁定环境。
#   1. uv sync --locked --all-groups：从 uv.lock 物化 .venv（--locked 断言 lock 与
#      pyproject.toml 一致，漂移即 fail-closed），并按 .python-version 锁定 3.12。
#   2. python 别名改为经 `uv run --locked python` 分发：所有门禁（1-9 用 python 别名、
#      4-7 直调）都在这个锁定 .venv 里跑，与 A-1 同源；--locked 使运行期再校验锁未漂移。
# 这样门禁与 A-1 用的是同一个 pytest / ruff / mypy，不再有 9.0.3 vs 9.1.1 的分裂。
$pythonVersionFile = Join-Path $REPO_ROOT ".python-version"
$requiredPython = if (Test-Path $pythonVersionFile) {
    (Get-Content $pythonVersionFile -Raw -Encoding utf8).Trim()
} else { "3.12" }
Write-Host "[check.ps1] 要求 Python $requiredPython（经 uv 锁定 .venv）"

# GPT 第十一轮 P0：强制 UV_PYTHON=3.12。pyproject.toml 的 requires-python 是 >=3.12，
# 不钉死时 uv 可能给门禁解析出 3.13（reviewer 的 UV_PYTHON=3.13 攻击）。此处在 uv sync
# 前设定，令门禁的 .venv 与 phase0-acceptance.ps1 A-1 同为 3.12。
$env:UV_PYTHON = "3.12"

# 从 uv.lock 物化锁定 .venv；--locked 漂移即 fail-closed（与 A-1-sync 一致）。
& uv sync --locked --all-groups
if ($LASTEXITCODE -ne 0) {
    Write-Error "[check.ps1] fail-closed: uv sync --locked --all-groups 失败（uv.lock 与 pyproject.toml 漂移？）exit=$LASTEXITCODE"
    exit 1
}
# GPT 第十一轮 P0：不仅探针"python 能否启动"，还必须**断言它是 3.12**（旧版只查退出码，
# 允许 3.13 通过）。GPT 第十二轮 P2：2>$null 分离 stderr（uv 的 VIRTUAL_ENV warning 不得
# 混入版本）+ 严格解析唯一 ^\d+\.\d+\.\d+$。GPT 第十三轮 P1-1：这套逻辑抽成共享函数
# Get-UvLockedPythonVersion（scripts/_python-probe.ps1），与 phase0-acceptance.ps1 B-2、
# 回归测试三处共用同一实现（不再各写一份），major.minor 从 .python-version 读（不硬编码补丁）。
$probe = Get-UvLockedPythonVersion -RepoRoot $REPO_ROOT
if (-not $probe.ok) {
    Write-Error "[check.ps1] fail-closed: uv 锁定 .venv 非 Python $($probe.expectedMajorMinor).x 或版本解析异常（exit=$($probe.exitCode)，匹配行数=$($probe.matchCount)，version='$($probe.version)'）"
    exit 1
}
$lockedVer = $probe.version
Write-Host "[check.ps1] 使用锁定 .venv：$lockedVer（$($probe.expectedMajorMinor) 已断言）"
# 用别名 python 指向锁定 .venv：所有门禁经 uv run --locked python 分发，与 A-1 同源。
function python { & uv run --locked python @args }

# 1. Codegen drift
Invoke-GateCheck "1-codegen-drift" {
    python contracts/codegen/generate.py --check
}

# 2. Schema validity
Invoke-GateCheck "2-schema-validity" {
    python -m pytest tests/contract/test_schema_catalog.py -k "schema" -q --tb=short --no-header
}

# 3. Golden vectors
# fail-closed：plan_hash / event_hash 向量测试文件必须存在并通过；缺任一即整体红
# （GPT 审核指出此分支原为 SKIP-即-PASS 的 fail-open 漏洞）。
Invoke-GateCheck "3-golden-vectors" {
    $ph = "tests\contract\test_plan_hash_vectors.py"
    $ev = "tests\contract\test_event_hash_vectors.py"
    $missing = @()
    if (-not (Test-Path $ph)) { $missing += $ph }
    if (-not (Test-Path $ev)) { $missing += $ev }
    if ($missing.Count -gt 0) {
        Write-Host "  [FAIL] Golden vectors 测试文件缺失：$($missing -join ', ')"
        # 必须让 pytest 真跑（即便 no collection）以触发非零退出
        python -m pytest $ph $ev -q --tb=short --no-header
    } else {
        python -m pytest $ph $ev -q --tb=short --no-header
    }
}

# 4. Chinese coverage (via helper script, locked .venv Python 3.12)
Invoke-GateCheck "4-chinese-coverage" {
    python scripts/_gate_checks.py chinese-coverage
}

# 5. No bare print/console.log (via helper script)
Invoke-GateCheck "5-no-bare-print" {
    python scripts/_gate_checks.py no-bare-print
}

# 6. Secret scan (via helper script)
Invoke-GateCheck "6-secret-scan" {
    python scripts/_gate_checks.py secret-scan
}

# 7. C-drive paths (via helper script)
Invoke-GateCheck "7-c-drive-paths" {
    python scripts/_gate_checks.py c-drive-paths
}

# 8. Catalog 47 IDs
Invoke-GateCheck "8-catalog-47-ids" {
    python -m pytest "tests/contract/test_schema_catalog.py::test_required_test_catalog_has_exactly_47_ids" "tests/contract/test_schema_catalog.py::test_required_test_catalog_all_ids_unique" -q --tb=short --no-header
}

# 9. No doc placeholders
Invoke-GateCheck "9-no-doc-placeholders" {
    python -m pytest tests/contract/test_no_placeholders.py -q --tb=short --no-header
}

# ── GPT 第二轮审核要求：check.ps1 真调用全栈（不能再 9 项假绿）──────────────
# ruff（GPT 第十轮 P1：--locked 与 gate 1-9 同一锁定 .venv，锁漂移即整体红）
Invoke-GateCheck "10-ruff-lint" {
    uv run --locked ruff check apps/agent/src scripts tests/contract tests/security
}

# mypy（同上，--locked）
Invoke-GateCheck "11-mypy-strict" {
    uv run --locked mypy apps/agent/src --config-file pyproject.toml
}

# TypeScript tsc + vitest
Invoke-GateCheck "12-tsc-noemit" {
    Push-Location packages/factory-contracts
    try {
        corepack pnpm exec tsc --noEmit
    } finally {
        Pop-Location
    }
}
Invoke-GateCheck "13-vitest" {
    corepack pnpm --filter "@factory/contracts" test
}

# Rust cargo test（GPT 第三轮 item 3：cargo check 不能替代运行时断言，必须真跑 test）。
# 根因修复：rust-toolchain.toml 的 channel="stable" 在 msvc 默认 host 上被解析成
# stable-x86_64-pc-windows-msvc，build-script 为 msvc host 编译 → 找不到 link.exe。
# 用 +stable-x86_64-pc-windows-gnu 显式覆盖，使 host 也是 gnu，build-script 走 gnu host
# + rust-lld（config.toml linker-flavor=ld），全程不碰 msvc link.exe。
Invoke-GateCheck "14-rust-test" {
    # 存储合同（GPT 第六轮 item 5）：standalone fallback 过去用 D:\acf-dev，超出允许根
    # D:\codex项目，被判违约。改为从 $REPO_ROOT 向上派生 AI-Coding-Factory-Data 项目根
    # （与 scripts/phase0-acceptance.ps1 / dev.ps1 同源做法，跨 worktree 稳健），三件套
    # 缓存/工具链/产物全部落项目根下的 AI-Coding-Factory-Data\dev。找不到项目根即 throw，
    # 被 Invoke-GateCheck 的 try/catch 记为 FAIL（fail-closed，绝不静默回退到 C: 或 acf-dev）。
    $_projRoot = $null
    $_probe = $REPO_ROOT
    while ($_probe) {
        if (Test-Path (Join-Path $_probe "AI-Coding-Factory-Data")) { $_projRoot = $_probe; break }
        $_parent = Split-Path $_probe -Parent
        if (-not $_parent -or $_parent -eq $_probe) { break }
        $_probe = $_parent
    }
    if (-not $_projRoot) {
        throw "gate-14 fail-closed: 找不到 AI-Coding-Factory-Data 项目根（$REPO_ROOT 的任何祖先），拒绝回退到 D:\acf-dev 等允许根外路径"
    }
    # GNU dlltool / binutils 仍会把输入路径交给窄字符 CreateProcess；即使 ld.lld 已经
    # Unicode-safe，只要 import-lib 生成阶段看到 D:\codex项目 这样的 CJK 参数仍会失败。
    # 因此 Rust gate 使用临时 subst 盘符提供 ASCII 视图，但物理文件仍落在 D:\codex项目 下。
    $substDrive = $null
    foreach ($candidate in @("Q:", "R:", "S:", "T:")) {
        if (-not (Test-Path -LiteralPath "$candidate\")) {
            $substDrive = $candidate
            break
        }
    }
    if (-not $substDrive) {
        throw "gate-14 fail-closed: 没有可用临时盘符用于 Rust ASCII 路径视图"
    }
    $createdSubst = $false
    $oldLocation = Get-Location
    $oldCargoHome = $env:CARGO_HOME
    $oldRustupHome = $env:RUSTUP_HOME
    $oldCargoTarget = $env:CARGO_TARGET_DIR
    $oldPath = $env:PATH
    $oldLinker = $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER
    $oldRustFlags = $env:RUSTFLAGS
    try {
        cmd /c "subst $substDrive `"$($_projRoot)`""
        if ($LASTEXITCODE -ne 0) {
            throw "gate-14 fail-closed: subst $substDrive -> $_projRoot failed"
        }
        $createdSubst = $true
        $relativeRepo = $REPO_ROOT.Substring($_projRoot.Length).TrimStart("\")
        $repoRootAscii = Join-Path "$substDrive\" $relativeRepo
        $_dataRoot = Join-Path "$substDrive\" "AI-Coding-Factory-Data\dev"
        $env:CARGO_HOME = Join-Path $_dataRoot "cargo-home"
        $env:RUSTUP_HOME = Join-Path $_dataRoot "rustup-home"
        $env:CARGO_TARGET_DIR = Get-WorktreeTargetDir -WorktreeRoot $repoRootAscii -DataRoot $_dataRoot
        $gnuToolchain = Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-gnu"
        $rustBin = Join-Path $gnuToolchain "lib\rustlib\x86_64-pc-windows-gnu\bin"
        $selfContainedBin = Join-Path $rustBin "self-contained"
        $rustLld = Join-Path $rustBin "rust-lld.exe"
        $dlltool = Join-Path $selfContainedBin "dlltool.exe"
        if (-not (Test-Path -LiteralPath $dlltool)) {
            throw "gate-14 fail-closed: dlltool not found at '$dlltool'"
        }
        if (-not (Test-Path -LiteralPath $rustLld)) {
            throw "gate-14 fail-closed: rust-lld not found at '$rustLld'"
        }
        $assemblerBin = Ensure-RustGnuAssemblerBundle -DataRoot $_dataRoot
        $assembler = Join-Path $assemblerBin "as.exe"
        $dlltoolWrapper = New-RustGnuDlltoolWrapper -DataRoot $_dataRoot -DlltoolPath $dlltool -AssemblerPath $assembler
        # 只前置 rustup shim 与 self-contained binutils；不前置完整 toolchain bin，
        # 避免真实 cargo.exe 遮蔽 shim 后不识别 +toolchain 语法。
        $env:PATH = "$env:CARGO_HOME\bin;$selfContainedBin;$assemblerBin;$oldPath"
        $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $rustLld
        $rustFlagPatch = "-C link-self-contained=yes -C dlltool=$dlltoolWrapper"
        $env:RUSTFLAGS = if ($oldRustFlags) { "$oldRustFlags $rustFlagPatch" } else { $rustFlagPatch }
        Set-Location -LiteralPath $repoRootAscii
        cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked
        $cargoExit = $LASTEXITCODE
        if ($cargoExit -ne 0) {
            throw "gate-14 cargo failed (exit $cargoExit)"
        }
    } finally {
        Set-Location $oldLocation
        $env:CARGO_HOME = $oldCargoHome
        $env:RUSTUP_HOME = $oldRustupHome
        $env:CARGO_TARGET_DIR = $oldCargoTarget
        $env:PATH = $oldPath
        $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $oldLinker
        $env:RUSTFLAGS = $oldRustFlags
        if ($createdSubst) {
            cmd /c "subst $substDrive /D" | Out-Null
        }
    }
}

# Bootstrap-dev 必须能跑 -VerifyOnly（plan-validation 引用已删除）
Invoke-GateCheck "15-bootstrap-verify" {
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap-dev.ps1 -VerifyOnly
}

# Summary
Write-Host ""
Write-Host "========================================"
Write-Host "Gate results:"
foreach ($r in $gate_results) {
    $col = if ($r.result -eq "PASS") { "Green" } else { "Red" }
    Write-Host ("  {0,-30} {1}" -f $r.name, $r.result) -ForegroundColor $col
}
Write-Host "========================================"
if ($failures.Count -gt 0) {
    Write-Host "[check.ps1] FAIL - $($failures.Count)/$($gate_results.Count) check(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host "[check.ps1] PASS - all $($gate_results.Count) gate checks passed!" -ForegroundColor Green
exit 0
