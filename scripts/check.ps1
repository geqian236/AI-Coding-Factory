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

# ── Python 3.12 锁定（GPT 第二轮审核指出）─────────────────────────────────
# 仓库 .python-version 声明 3.12，但 PATH 中的 python 可能被 anaconda 等
# 覆写为 3.11，导致 CI 与本机结果漂移。本段按以下优先级解析 Python 3.12：
#   1. .python-version 指定 3.12（pyenv / GitHub Actions standard）
#   2. Windows 标准安装路径 %LOCALAPPDATA%\Programs\Python\Python312\python.exe
#   3. uv python find 3.12（跨平台回退）
# 锁定失败直接 fail closed（exit 1），不再回退到 PATH 中任意 python。
$pythonVersionFile = Join-Path $REPO_ROOT ".python-version"
$requiredPython = if (Test-Path $pythonVersionFile) {
    (Get-Content $pythonVersionFile -Raw -Encoding utf8).Trim()
} else { "3.12" }
Write-Host "[check.ps1] 要求 Python $requiredPython"

# 候选解析路径：env PYTHON_BIN 覆盖 > 用户本地安装 > uv 解析
$candidatePy = $env:PYTHON_BIN
if (-not $candidatePy -or -not (Test-Path $candidatePy)) {
    $localPy = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$($requiredPython.Replace('.',''))\python.exe"
    if (Test-Path $localPy) { $candidatePy = $localPy }
}
if (-not $candidatePy -or -not (Test-Path $candidatePy)) {
    try {
        $uvPy = (& uv python find $requiredPython 2>$null | Select-Object -First 1)
        if ($uvPy -and (Test-Path $uvPy)) { $candidatePy = $uvPy }
    } catch {}
}
if (-not $candidatePy -or -not (Test-Path $candidatePy)) {
    Write-Error "[check.ps1] fail-closed: 未找到 Python $requiredPython。设置 `$env:PYTHON_BIN 指向 python.exe 或安装 Python $requiredPython。"
    exit 1
}
$actualVer = (& $candidatePy --version 2>$null | Select-Object -First 1)
Write-Host "[check.ps1] 使用 $candidatePy ($actualVer)"
# 用别名 python 指向锁定的 3.12，覆盖 PATH 中的旧版
function python { & $candidatePy @args }

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

# 4. Chinese coverage (via helper script, locked Python 3.12)
Invoke-GateCheck "4-chinese-coverage" {
    & $candidatePy scripts/_gate_checks.py chinese-coverage
}

# 5. No bare print/console.log (via helper script)
Invoke-GateCheck "5-no-bare-print" {
    & $candidatePy scripts/_gate_checks.py no-bare-print
}

# 6. Secret scan (via helper script)
Invoke-GateCheck "6-secret-scan" {
    & $candidatePy scripts/_gate_checks.py secret-scan
}

# 7. C-drive paths (via helper script)
Invoke-GateCheck "7-c-drive-paths" {
    & $candidatePy scripts/_gate_checks.py c-drive-paths
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
# ruff
Invoke-GateCheck "10-ruff-lint" {
    uv run ruff check apps/agent/src scripts tests/contract tests/security
}

# mypy
Invoke-GateCheck "11-mypy-strict" {
    uv run mypy apps/agent/src --config-file pyproject.toml
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
    $env:CARGO_HOME = if ($env:CARGO_HOME) { $env:CARGO_HOME } else { "D:\acf-dev\cargo-home" }
    $env:RUSTUP_HOME = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } else { "D:\acf-dev\rustup-home" }
    # 只前置 CARGO_HOME\bin（rustup shim 所在），不前置 gnu 工具链 bin：
    # 否则 cargo/rustc 解析成 gnu 工具链里的真实 exe，不认 +toolchain 语法
    # （报 "no such command: +stable-..."）。shim 才能分发 +toolchain。
    $env:PATH = "$env:CARGO_HOME\bin;$env:PATH"
    $env:CARGO_TARGET_DIR = "D:\codex项目\AI-Coding-Factory-Data\dev\cargo-target"
    $sysroot = (& rustc +stable-x86_64-pc-windows-gnu --print sysroot 2>$null | Select-Object -First 1)
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = Join-Path $sysroot "lib\rustlib\x86_64-pc-windows-gnu\bin\gcc-ld\ld.lld.exe"
    cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked
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
