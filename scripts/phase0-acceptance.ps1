<#
.SYNOPSIS
    Phase 0 single acceptance script - runs the full A/B/C/spike set and emits
    a single total receipt bound to the 40-char candidate SHA.

.DESCRIPTION
    GPT round-3 REVISE item 1/5: one command must cover A-1/A-2/A-3b/B-1/B-2/
    C-1/C-2 + 15 gates + spike + receipt verification, and emit a single total
    receipt bound to the 40-char candidate SHA.

    ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as GBK on a
    Chinese Windows codepage; CJK comments/strings then corrupt string
    terminators and break the parse. Keeping this file pure ASCII removes the
    BOM dependency entirely.

    Design (aligned with known traps):
      - $PSScriptRoot self-location; never depends on caller cwd (avoids CJK
        path corruption through bash->powershell).
      - Rust explicit +stable-x86_64-pc-windows-gnu: rust-toolchain.toml
        channel="stable" resolves to msvc on a default-host=msvc machine and
        the build-script host link fails on missing link.exe; forcing the gnu
        toolchain makes build-scripts compile for the gnu host via rust-lld.
      - Python commands prefixed with PYTHONIOENCODING=utf-8 to bypass GBK.
      - Receipt written with .NET UTF8Encoding(false) = no BOM; downstream
        json.load does not choke on a BOM.
      - Frozen BLOCKED allowlist: only the listed environment-compat IDs may be
        BLOCKED_UNCERTIFIED; every core contract check must PASS.

.NOTES
    version: acceptance-set-v1
    output: <repo>/.phase0-acceptance-receipt.json
#>
[CmdletBinding()]
param(
    [string]$OutFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# repo root self-location (this script lives in <repo>/scripts/)
$REPO_ROOT = Split-Path $PSScriptRoot -Parent
Set-Location $REPO_ROOT

# Python 3.12 lock (mirrors check.ps1): PATH python may be anaconda 3.11 and
# drift results. Resolve 3.12 by: .python-version -> local install -> uv find.
# Fail-closed if 3.12 not found; A-1/B-2 must run under the locked interpreter.
$pythonVersionFile = Join-Path $REPO_ROOT ".python-version"
$requiredPython = if (Test-Path $pythonVersionFile) {
    (Get-Content $pythonVersionFile -Raw -Encoding utf8).Trim()
} else { "3.12" }
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
    Write-Error "[acceptance] fail-closed: Python $requiredPython not found. Set `$env:PYTHON_BIN or install Python $requiredPython."
    exit 1
}
$actualPyVer = (& $candidatePy --version 2>$null | Select-Object -First 1)
Write-Host "[acceptance] using $candidatePy ($actualPyVer)"
# alias python -> locked 3.12, overriding PATH
function python { & $candidatePy @args }

# acceptance-set version (bump when the acceptance set changes; recorded in the receipt)
$ACCEPTANCE_SET_VERSION = "acceptance-set-v1"

# Frozen BLOCKED allowlist: only these environment-compat IDs may be
# BLOCKED_UNCERTIFIED at the top level. Any other non-PASS check = overall FAIL.
$BLOCKED_ALLOWLIST = [ordered]@{
    "spike:sqlite_wal_full/disk_full_enospc" = "admin mounts a <=16MiB VHD then bench --probe-dir <VHD> certifies real ENOSPC"
    "spike:tauri_e2e/webview_runtime"        = "run the E2E spike on a host with WebView2 Runtime + Tauri CLI installed"
}

$results = [System.Collections.Generic.List[object]]::new()

# --- helpers ---------------------------------------------------------------

# SHA-256 of a file (hex, sha256: prefix); null when the file is absent.
function Get-FileSha256 {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLower()
    return "sha256:$hash"
}

# Run one acceptance command, capture exit code, record into $results.
function Invoke-AcceptanceCheck {
    param(
        [string]$Id,
        [string]$Desc,
        [scriptblock]$Action
    )
    Write-Host "-- [$Id] $Desc --" -ForegroundColor Cyan
    $stdout = ""
    $code = $null
    try {
        $stdout = (& $Action 2>&1 | Out-String)
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
    } catch {
        $stdout = ($_ | Out-String)
        $code = -1
    }
    $passed = ($code -eq 0)
    Write-Host $stdout
    Write-Host "  [$Id] exit=$code passed=$passed"
    $results.Add([ordered]@{
        id       = $Id
        desc     = $Desc
        exitCode = $code
        passed   = $passed
        tail     = ($stdout -split "`n" | Select-Object -Last 3) -join " | "
    })
    return $passed
}

# Rust gnu toolchain env (mirrors gate-14 + forces explicit gnu toolchain).
function Set-RustGnuEnv {
    if (-not $env:CARGO_HOME)  { $env:CARGO_HOME  = "D:\acf-dev\cargo-home" }
    if (-not $env:RUSTUP_HOME) { $env:RUSTUP_HOME = "D:\acf-dev\rustup-home" }
    $env:CARGO_TARGET_DIR = "D:\acf-dev\ct-gnu"
    # Only prepend CARGO_HOME\bin so `cargo` resolves to the rustup shim
    # (the shim understands `+toolchain`; the real gnu cargo.exe does not).
    # Do NOT prepend the gnu toolchain bin, or it shadows the shim and
    # `cargo +stable-x86_64-pc-windows-gnu` fails with "no such command".
    $env:PATH = "$($env:CARGO_HOME)\bin;$($env:PATH)"
    $gnuBin = Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-gnu\bin"
    $sysroot = (& "$gnuBin\rustc.exe" --print sysroot 2>$null | Select-Object -First 1)
    if ($sysroot) {
        $ldLld = Join-Path $sysroot "lib\rustlib\x86_64-pc-windows-gnu\bin\gcc-ld\ld.lld.exe"
        if (Test-Path $ldLld) {
            $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $ldLld
        }
    }
}

Write-Host "========================================"
Write-Host "Phase 0 Acceptance - $ACCEPTANCE_SET_VERSION"
Write-Host "repo: $REPO_ROOT"
Write-Host "========================================"

# candidate SHA + clean-tree status (for receipt binding)
$candidateSha = (& git rev-parse HEAD 2>$null | Select-Object -First 1)
$dirtyLines = (& git status --porcelain 2>$null)
$cleanTree = [string]::IsNullOrWhiteSpace(($dirtyLines | Out-String).Trim())
Write-Host "candidateSha = $candidateSha"
Write-Host "cleanTree    = $cleanTree"

# --- A. test suites --------------------------------------------------------

Invoke-AcceptanceCheck "A-1" "Python three-dir tests" {
    $env:PYTHONIOENCODING = "utf-8"
    python -m pytest tests/contract tests/agent tests/security -q
} | Out-Null

Invoke-AcceptanceCheck "A-2" "TypeScript contract vectors (vitest)" {
    corepack pnpm --filter "@factory/contracts" test
} | Out-Null

Set-RustGnuEnv
Invoke-AcceptanceCheck "A-3b" "Rust contract runtime assertions (cargo test, gnu)" {
    cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked
} | Out-Null

# --- B. reproducible build -------------------------------------------------

Invoke-AcceptanceCheck "B-1" "pnpm frozen lockfile install" {
    corepack pnpm install --frozen-lockfile
} | Out-Null

Invoke-AcceptanceCheck "B-2" "Python 3.12 pinned" {
    $pv = (python --version 2>&1 | Out-String).Trim()
    if ($pv -match "3\.12\.") { $global:LASTEXITCODE = 0 } else { Write-Host "expected 3.12.x, got $pv"; $global:LASTEXITCODE = 1 }
} | Out-Null

# --- C. CI fail-closed machine decision ------------------------------------

Invoke-AcceptanceCheck "C-1" "ci.yml has no echo OK/neutral placeholder" {
    $ci = Get-Content ".github/workflows/ci.yml" -Raw -Encoding utf8
    if ($ci -match 'echo\s+"?(plan-consistency|python|desktop|security).*(OK|neutral)"?') {
        Write-Host "found fixed-green placeholder"; $global:LASTEXITCODE = 1
    } else { $global:LASTEXITCODE = 0 }
} | Out-Null

Invoke-AcceptanceCheck "C-2" "no duplicate workflow (plan-validation.yml removed)" {
    if (Test-Path ".github/workflows/plan-validation.yml") {
        Write-Host "plan-validation.yml still present (duplicate workflow)"; $global:LASTEXITCODE = 1
    } else { $global:LASTEXITCODE = 0 }
} | Out-Null

# --- 15 gates --------------------------------------------------------------
Invoke-AcceptanceCheck "GATES" "check.ps1 15 gates" {
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
} | Out-Null

# --- spike + receipt status (judged against allowlist) ---------------------
$spikeReceipts = [ordered]@{
    "clock_source"        = "tools/compat-probes/clock_source/receipt.json"
    "windows_durable_io"  = "tools/compat-probes/windows_durable_io/receipt.json"
    "named_pipe"          = "tools/compat-probes/named_pipe/receipt.json"
    "git_object_bridge"   = "tools/compat-probes/git_object_bridge/receipt.json"
    "runner_identity"     = "tools/compat-probes/runner_identity/receipt.json"
    "sqlite_wal_full"     = "tools/compat-probes/sqlite_wal_full/receipt.json"
    "tauri_e2e"           = "tools/compat-probes/tauri_e2e/receipt.json"
}
# core spikes (must PASS) vs env-compat spikes (BLOCKED_UNCERTIFIED allowed)
$envCompatSpikes = @("sqlite_wal_full", "tauri_e2e")

$spikeStatuses = [ordered]@{}
foreach ($name in ($spikeReceipts.Keys)) {
    $path = $spikeReceipts[$name]
    $status = "MISSING"
    if (Test-Path $path) {
        try {
            $obj = Get-Content $path -Raw -Encoding utf8 | ConvertFrom-Json
            $status = $obj.status
        } catch { $status = "UNPARSEABLE" }
    }
    $spikeStatuses[$name] = $status
    if ($envCompatSpikes -contains $name) {
        $ok = ($status -eq "PASS" -or $status -eq "BLOCKED_UNCERTIFIED")
    } else {
        $ok = ($status -eq "PASS")
    }
    $results.Add([ordered]@{
        id = "spike:$name"; desc = "spike receipt status"; exitCode = $(if ($ok) {0} else {1}); passed = $ok; tail = "status=$status"
    })
    Write-Host "  [spike:$name] status=$status ok=$ok"
}

# --- summary + top-level decision ------------------------------------------
$failed = @($results | Where-Object { -not $_.passed })
$topStatus = if ($failed.Count -eq 0) { "PASS" } else { "FAIL" }

# --- digest binding (script / schema / env) --------------------------------
$scriptDigests = [ordered]@{
    "phase0-acceptance.ps1" = Get-FileSha256 (Join-Path $PSScriptRoot "phase0-acceptance.ps1")
    "check.ps1"             = Get-FileSha256 (Join-Path $PSScriptRoot "check.ps1")
    "PHASE_0_ACCEPTANCE.md" = Get-FileSha256 (Join-Path $REPO_ROOT "docs/operations/PHASE_0_ACCEPTANCE.md")
}
$schemaDigests = [ordered]@{}
Get-ChildItem (Join-Path $REPO_ROOT "contracts/schemas") -Filter *.json | Sort-Object Name | ForEach-Object {
    $schemaDigests[$_.Name] = Get-FileSha256 $_.FullName
}

$receipt = [ordered]@{
    acceptanceSetVersion = $ACCEPTANCE_SET_VERSION
    testedCandidateSha   = $candidateSha
    cleanTree            = $cleanTree
    dirtyFiles           = @($dirtyLines)
    generatedAt          = (Get-Date -Format "o")
    topStatus            = $topStatus
    checks               = $results
    spikeStatuses        = $spikeStatuses
    blockedAllowlist     = $BLOCKED_ALLOWLIST
    scriptDigests        = $scriptDigests
    schemaDigests        = $schemaDigests
    environment          = [ordered]@{
        os            = [System.Environment]::OSVersion.VersionString
        pythonVersion = (python --version 2>&1 | Out-String).Trim()
        nodeVersion   = (node --version 2>&1 | Out-String).Trim()
    }
}

if (-not $OutFile) { $OutFile = Join-Path $REPO_ROOT ".phase0-acceptance-receipt.json" }
$json = $receipt | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText($OutFile, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "========================================"
Write-Host "Acceptance receipt: $OutFile"
Write-Host "topStatus = $topStatus  (failed: $($failed.Count)/$($results.Count))"
Write-Host "========================================"
if ($topStatus -ne "PASS") {
    foreach ($f in $failed) { Write-Host "  FAIL: $($f.id) - $($f.tail)" -ForegroundColor Red }
    exit 1
}
Write-Host "[phase0-acceptance] PASS - all acceptance checks passed" -ForegroundColor Green
exit 0
