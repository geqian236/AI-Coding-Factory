<#
.SYNOPSIS
    Phase 0 single acceptance script - runs the full A/B/C/spike set and emits
    a single total receipt bound to the 40-char candidate SHA.

.DESCRIPTION
    GPT round-4 REVISE items 1/3/5/7. One command covers A-1/A-2/A-3b/B-1/B-2/
    C-1/C-2 + 15 gates + spike evidence validation, and emits a single total
    receipt bound to the 40-char candidate SHA.

    ASCII-only on purpose: PowerShell 5.1 reads a BOM-less .ps1 as GBK on a
    Chinese Windows codepage; CJK comments/strings then corrupt string
    terminators and break the parse. Keeping this file pure ASCII removes the
    BOM dependency entirely.

    Design (each backed by verified evidence this round):
      - V1: B-1 (pnpm install) runs BEFORE A-2 (vitest). On a clean checkout
        with no node_modules, vitest is unresolved until frozen install runs.
      - V3: spike receipts are validated for evidence, not trusted by top-level
        status alone. A minimal {"status":"PASS"} no longer passes: required
        fields must exist, assertions must be non-empty, and status must be
        consistent with assertions (PASS => all assertions passed; BLOCKED =>
        at least one assertion failed AND a subcheckId in the frozen allowlist).
      - V5: acceptanceSetVersion matches the doc (phase0-acceptance-v1);
        acceptanceSetDigest is computed from the frozen doc and written to the
        receipt; cleanTree is a pass condition and is re-checked after the run
        (only the gitignored receipt may appear dirty); frozen counts (327/1,
        70, 14) are parsed from output and enforced.
      - V7: Rust env uses project-root paths (<PROJECT_ROOT>\AI-Coding-Factory-Data\
        dev), never D:\acf-dev; the storage contract keeps every artifact under
        the project root. OutFile is fail-closed under the project root. cwd is
        set to REPO_ROOT so .cargo/config.toml (linker-flavor=ld) loads - this
        is required for the gnu linker to work on the CJK project path (verified
        by a cold-compile test: config must load from repo-root cwd).
      - Rust explicit +stable-x86_64-pc-windows-gnu: rust-toolchain.toml
        channel="stable" resolves to msvc on a default-host=msvc machine and the
        build-script host link fails on missing link.exe; forcing gnu makes
        build-scripts compile for the gnu host via ld.lld.
      - Python commands prefixed with PYTHONIOENCODING=utf-8 to bypass GBK.
      - Receipt written with .NET UTF8Encoding(false) = no BOM.

.NOTES
    version: phase0-acceptance-v1
    output: <repo>/.phase0-acceptance-receipt.json
#>
[CmdletBinding()]
param(
    [string]$OutFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# repo root self-location (this script lives in <repo>/scripts/).
# cwd MUST be REPO_ROOT so cargo loads .cargo/config.toml (linker-flavor=ld).
$REPO_ROOT = Split-Path $PSScriptRoot -Parent
Set-Location $REPO_ROOT

# Project root (storage contract): every artifact must live under this prefix.
# Derived at RUNTIME by walking up from $REPO_ROOT to the ancestor that holds
# the AI-Coding-Factory-Data sibling. This avoids a CJK path literal in this
# pure-ASCII, BOM-less script: PS 5.1 would read such a literal as GBK
# and corrupt every derived path (CARGO_HOME/RUSTUP_HOME/OutFile prefix). The
# runtime path from $PSScriptRoot carries correct filesystem bytes.
$PROJECT_ROOT = $null
$probe = $REPO_ROOT
while ($probe) {
    if (Test-Path (Join-Path $probe "AI-Coding-Factory-Data")) { $PROJECT_ROOT = $probe; break }
    $parent = Split-Path $probe -Parent
    if (-not $parent -or $parent -eq $probe) { break }
    $probe = $parent
}
if (-not $PROJECT_ROOT) {
    Write-Error "[acceptance] fail-closed: cannot locate project root (no AI-Coding-Factory-Data ancestor of '$REPO_ROOT')."
    exit 1
}
# Project-root data dir (mirrors dev.ps1); Rust toolchain/target all under it.
$DATA_ROOT = Join-Path $PROJECT_ROOT "AI-Coding-Factory-Data\dev"

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

# acceptance-set version (matches PHASE_0_ACCEPTANCE.md header; bump together).
$ACCEPTANCE_SET_VERSION = "phase0-acceptance-v1"
$ACCEPTANCE_SET_DOC = Join-Path $REPO_ROOT "docs/operations/PHASE_0_ACCEPTANCE.md"

# Frozen baseline counts (V5: parsed from output and enforced, not eyeballed).
$FROZEN_PYTEST_PASSED  = 327
$FROZEN_PYTEST_SKIPPED = 1
$FROZEN_VITEST_PASSED  = 70
$FROZEN_CARGO_PASSED   = 14
# Frozen identity of the single allowed skip (V5: exact skip node enforced).
$FROZEN_SKIP_MATCH = "test_config.py"

# Frozen BLOCKED allowlist: only these subcheck IDs may be BLOCKED_UNCERTIFIED.
# Any other non-PASS check = overall FAIL. Keyed by subcheckId (V5: unified id).
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

# Run one acceptance command, capture exit code + full stdout, record result.
# Returns the full stdout string so callers can parse frozen counts.
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
    return $stdout
}

# Record a synthetic (non-command) check result into $results.
function Add-CheckResult {
    param([string]$Id, [string]$Desc, [bool]$Passed, [string]$Detail)
    $results.Add([ordered]@{
        id = $Id; desc = $Desc; exitCode = $(if ($Passed) {0} else {1}); passed = $Passed; tail = $Detail
    })
    Write-Host "  [$Id] passed=$Passed - $Detail"
}

# Rust gnu toolchain env: project-root paths only (V7 storage contract).
# Linker path is built from the literal $env:RUSTUP_HOME (never captured from
# rustc stdout, which PS 5.1 corrupts on a GBK codepage). config.toml supplies
# linker-flavor=ld and loads because cwd == REPO_ROOT.
function Set-RustGnuEnv {
    $env:CARGO_HOME       = Join-Path $DATA_ROOT "cargo-home"
    $env:RUSTUP_HOME      = Join-Path $DATA_ROOT "rustup-home"
    $env:CARGO_TARGET_DIR = Join-Path $DATA_ROOT "cargo-target"
    # Prepend CARGO_HOME\bin so `cargo` resolves to the rustup shim (the shim
    # understands +toolchain; the real gnu cargo.exe does not). Do NOT prepend
    # the gnu toolchain bin, or it shadows the shim.
    $env:PATH = "$($env:CARGO_HOME)\bin;$($env:PATH)"
    $gnuToolchain = Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-gnu"
    $ldLld = Join-Path $gnuToolchain "lib\rustlib\x86_64-pc-windows-gnu\bin\gcc-ld\ld.lld.exe"
    if (Test-Path -LiteralPath $ldLld) {
        $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $ldLld
    } else {
        Write-Warning "[acceptance] ld.lld not found at '$ldLld'; Rust link will fall back and fail on the CJK path."
    }
}

# Deep-validate one spike receipt (V3: evidence, not bare top-level status).
# Returns @{ ok = <bool>; status = <string>; detail = <string> }.
function Test-SpikeReceipt {
    param(
        [string]$Name,
        [string]$Path,
        [bool]$EnvCompat  # true => BLOCKED_UNCERTIFIED allowed if subcheckId in allowlist
    )
    if (-not (Test-Path $Path)) {
        return @{ ok = $false; status = "MISSING"; detail = "receipt file absent" }
    }
    try {
        $obj = Get-Content $Path -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        return @{ ok = $false; status = "UNPARSEABLE"; detail = "JSON parse failed" }
    }
    # Required fields (V3: a minimal {"status":"PASS"} lacks these).
    foreach ($f in @("spike", "status", "assertions")) {
        if (-not ($obj.PSObject.Properties.Name -contains $f)) {
            return @{ ok = $false; status = "INVALID"; detail = "missing required field '$f'" }
        }
    }
    $status = [string]$obj.status
    $assertions = @($obj.assertions)
    if ($assertions.Count -lt 1) {
        return @{ ok = $false; status = $status; detail = "assertions empty (no observable evidence)" }
    }
    # Each assertion must carry passed (bool).
    foreach ($a in $assertions) {
        if (-not ($a.PSObject.Properties.Name -contains "passed")) {
            return @{ ok = $false; status = $status; detail = "an assertion lacks 'passed'" }
        }
    }
    $failedAsserts = @($assertions | Where-Object { -not $_.passed })
    $allPassed = ($failedAsserts.Count -eq 0)

    # Blocking subchecks come in two real shapes (verified against committed receipts):
    #   Shape A (tauri_e2e): a top-level "subcheckId" string + a failed assertion.
    #   Shape B (sqlite_wal_full): a "subResults" entry with required==true and
    #     status BLOCKED_UNCERTIFIED/FAIL; its id is spike:<name>/<aspect>.
    # Note: some core spikes carry "uncertified_aspects" (informational, no
    # "required" field) - those are NOT blocking and must not fail a PASS spike.
    $blockingSubIds = @()
    if ($obj.PSObject.Properties.Name -contains "subResults") {
        foreach ($sr in @($obj.subResults)) {
            $srStatus = [string]$sr.status
            $srRequired = $false
            if ($sr.PSObject.Properties.Name -contains "required") { $srRequired = [bool]$sr.required }
            if ($srRequired -and ($srStatus -eq "BLOCKED_UNCERTIFIED" -or $srStatus -eq "FAIL")) {
                $blockingSubIds += "spike:$Name/$($sr.aspect)"
            }
        }
    }
    $topSubId = if ($obj.PSObject.Properties.Name -contains "subcheckId") { [string]$obj.subcheckId } else { $null }

    if ($status -eq "PASS") {
        # V3 consistency: PASS requires every top-level assertion passed AND no
        # required subResult in a blocking state.
        if (-not $allPassed) {
            return @{ ok = $false; status = $status; detail = "status=PASS but $($failedAsserts.Count) assertion(s) failed" }
        }
        if ($blockingSubIds.Count -gt 0) {
            return @{ ok = $false; status = $status; detail = "status=PASS but required subResult blocked: $($blockingSubIds -join ',')" }
        }
        return @{ ok = $true; status = $status; detail = "PASS: $($assertions.Count) assertions all passed" }
    }
    elseif ($status -eq "BLOCKED_UNCERTIFIED") {
        if (-not $EnvCompat) {
            return @{ ok = $false; status = $status; detail = "core spike may not be BLOCKED_UNCERTIFIED" }
        }
        # V3 consistency: a BLOCKED must carry real blocking evidence - either a
        # failed top-level assertion (shape A) or a required blocked subResult
        # (shape B). A bare self-reported BLOCKED with all-passed evidence is rejected.
        if ($failedAsserts.Count -eq 0 -and $blockingSubIds.Count -eq 0) {
            return @{ ok = $false; status = $status; detail = "BLOCKED but no failed assertion and no required blocked subResult (self-reported)" }
        }
        # The blocking subcheck id (from either shape) must be in the frozen allowlist.
        $candidateIds = @()
        if ($topSubId) { $candidateIds += $topSubId }
        $candidateIds += $blockingSubIds
        $allowedHit = @($candidateIds | Where-Object { $BLOCKED_ALLOWLIST.Keys -contains $_ })
        if ($allowedHit.Count -lt 1) {
            return @{ ok = $false; status = $status; detail = "no allowlisted subcheckId (candidates: $($candidateIds -join ','))" }
        }
        return @{ ok = $true; status = $status; detail = "BLOCKED_UNCERTIFIED allowlisted: $($allowedHit -join ',')" }
    }
    else {
        return @{ ok = $false; status = $status; detail = "status '$status' not PASS/BLOCKED_UNCERTIFIED" }
    }
}

Write-Host "========================================"
Write-Host "Phase 0 Acceptance - $ACCEPTANCE_SET_VERSION"
Write-Host "repo: $REPO_ROOT"
Write-Host "========================================"

# candidate SHA + clean-tree status at start (V5: cleanTree is a pass condition).
$candidateSha = (& git rev-parse HEAD 2>$null | Select-Object -First 1)
$dirtyLinesStart = @(& git status --porcelain 2>$null)
$cleanTreeStart = ($dirtyLinesStart.Count -eq 0)
Write-Host "candidateSha  = $candidateSha"
Write-Host "cleanTreeStart= $cleanTreeStart"

# --- A-1 + B-1 + A-2 (V1: install before vitest) ---------------------------

$a1out = Invoke-AcceptanceCheck "A-1" "Python three-dir tests" {
    $env:PYTHONIOENCODING = "utf-8"
    # -rs: report skipped with reason lines (SKIPPED [1] <file>:<line>: <reason>),
    # so the V5 skip-identity check below can match the frozen skip node. -q alone
    # prints only the "N passed, M skipped" summary without the skip's file path.
    python -m pytest tests/contract tests/agent tests/security -q -rs
}
# V5: enforce frozen counts + single skip identity.
if ($a1out -match "(\d+)\s+passed,\s+(\d+)\s+skipped") {
    $p = [int]$Matches[1]; $s = [int]$Matches[2]
    $countOk = ($p -eq $FROZEN_PYTEST_PASSED -and $s -eq $FROZEN_PYTEST_SKIPPED)
    Add-CheckResult "A-1-count" "pytest frozen count == $FROZEN_PYTEST_PASSED passed / $FROZEN_PYTEST_SKIPPED skipped" $countOk "observed $p passed / $s skipped"
} else {
    Add-CheckResult "A-1-count" "pytest frozen count parse" $false "could not parse 'N passed, M skipped'"
}
# V5: the single skip must be the frozen node (D: fixed-volume gate), not a
# silently-added skip elsewhere.
$skipOk = ($a1out -match [regex]::Escape($FROZEN_SKIP_MATCH))
Add-CheckResult "A-1-skip-id" "single skip is the frozen node ($FROZEN_SKIP_MATCH)" $skipOk "skip identity check"

Invoke-AcceptanceCheck "B-1" "pnpm frozen lockfile install (before vitest)" {
    corepack pnpm install --frozen-lockfile
} | Out-Null

$a2out = Invoke-AcceptanceCheck "A-2" "TypeScript contract vectors (vitest)" {
    corepack pnpm --filter "@factory/contracts" test
}
if ($a2out -match "Tests\s+(\d+)\s+passed") {
    $tp = [int]$Matches[1]
    Add-CheckResult "A-2-count" "vitest frozen count == $FROZEN_VITEST_PASSED" ($tp -eq $FROZEN_VITEST_PASSED) "observed $tp passed"
} else {
    Add-CheckResult "A-2-count" "vitest frozen count parse" $false "could not parse 'Tests N passed'"
}

# --- A-3b Rust runtime assertions (V7: project-root env; cwd already REPO_ROOT)
Set-RustGnuEnv
$a3out = Invoke-AcceptanceCheck "A-3b" "Rust contract runtime assertions (cargo test, gnu)" {
    cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked
}
# Sum "N passed" across the test binaries (event_vectors + plan_vectors).
$cargoPassed = 0
foreach ($m in [regex]::Matches($a3out, "test result: ok\.\s+(\d+)\s+passed")) {
    $cargoPassed += [int]$m.Groups[1].Value
}
Add-CheckResult "A-3b-count" "cargo test frozen count == $FROZEN_CARGO_PASSED" ($cargoPassed -eq $FROZEN_CARGO_PASSED) "observed $cargoPassed passed"

# --- B-2 Python pin -------------------------------------------------------
Invoke-AcceptanceCheck "B-2" "Python 3.12 pinned" {
    $pv = (python --version 2>&1 | Out-String).Trim()
    if ($pv -match "3\.12\.") { $global:LASTEXITCODE = 0 } else { Write-Host "expected 3.12.x, got $pv"; $global:LASTEXITCODE = 1 }
} | Out-Null

# --- C-1 CI fail-closed (V6/C-1 strengthened) ------------------------------
Invoke-AcceptanceCheck "C-1" "ci.yml has no fail-open patterns" {
    $ci = Get-Content ".github/workflows/ci.yml" -Raw -Encoding utf8
    $bad = @()
    # (a) echo OK/neutral placeholders
    if ($ci -match 'echo\s+"?(plan-consistency|python|desktop|security|contracts|rust).*(OK|neutral)"?') {
        $bad += "echo OK/neutral placeholder"
    }
    # (b) mypy strict||lax short-circuit (strict failure still passes)
    if ($ci -match 'mypy[^\r\n]*\|\|\s*mypy') {
        $bad += "mypy strict||lax short-circuit"
    }
    # (c) windows-probes job that only runs pytest and never invokes a spike
    if ($ci -match '(?ms)windows-probes:.*?(?=\r?\n\s{2}\w[\w-]*:\s*$|\Z)') {
        $wp = $Matches[0]
        if ($wp -notmatch 'spike' -and $wp -notmatch 'test-') {
            $bad += "windows-probes runs no spike"
        }
    }
    if ($bad.Count -gt 0) { Write-Host ("fail-open: " + ($bad -join "; ")); $global:LASTEXITCODE = 1 }
    else { $global:LASTEXITCODE = 0 }
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

# --- spike evidence validation (V3: not bare top-level status) -------------
$spikeReceipts = [ordered]@{
    "clock_source"       = "tools/compat-probes/clock_source/receipt.json"
    "windows_durable_io" = "tools/compat-probes/windows_durable_io/receipt.json"
    "named_pipe"         = "tools/compat-probes/named_pipe/receipt.json"
    "git_object_bridge"  = "tools/compat-probes/git_object_bridge/receipt.json"
    "runner_identity"    = "tools/compat-probes/runner_identity/receipt.json"
    "sqlite_wal_full"    = "tools/compat-probes/sqlite_wal_full/receipt.json"
    "tauri_e2e"          = "tools/compat-probes/tauri_e2e/receipt.json"
}
$envCompatSpikes = @("sqlite_wal_full", "tauri_e2e")

$spikeStatuses = [ordered]@{}
foreach ($name in ($spikeReceipts.Keys)) {
    $isEnv = ($envCompatSpikes -contains $name)
    $v = Test-SpikeReceipt -Name $name -Path $spikeReceipts[$name] -EnvCompat $isEnv
    $spikeStatuses[$name] = $v.status
    Add-CheckResult "spike:$name" "spike receipt evidence" $v.ok "status=$($v.status); $($v.detail)"
}

# --- clean-tree re-check (V5: only the gitignored receipt may be dirty) -----
$dirtyLinesEnd = @(& git status --porcelain 2>$null)
# Filter out the acceptance receipt itself (gitignored, but be defensive).
$trackedDirty = @($dirtyLinesEnd | Where-Object { $_ -notmatch '\.phase0-acceptance-receipt\.json' })
$cleanTreeEnd = ($trackedDirty.Count -eq 0)
Add-CheckResult "clean-tree" "tree clean at tested SHA (start + after run)" ($cleanTreeStart -and $cleanTreeEnd) "start=$cleanTreeStart end=$cleanTreeEnd trackedDirty=$($trackedDirty.Count)"

# --- summary + top-level decision ------------------------------------------
$failed = @($results | Where-Object { -not $_.passed })
$topStatus = if ($failed.Count -eq 0) { "PASS" } else { "FAIL" }

# --- digest binding (acceptance-set doc / scripts / schema / env) ----------
$acceptanceSetDigest = Get-FileSha256 $ACCEPTANCE_SET_DOC
$scriptDigests = [ordered]@{
    "phase0-acceptance.ps1" = Get-FileSha256 (Join-Path $PSScriptRoot "phase0-acceptance.ps1")
    "check.ps1"             = Get-FileSha256 (Join-Path $PSScriptRoot "check.ps1")
    "dev.ps1"               = Get-FileSha256 (Join-Path $PSScriptRoot "dev.ps1")
}
$schemaDigests = [ordered]@{}
Get-ChildItem (Join-Path $REPO_ROOT "contracts/schemas") -Filter *.json | Sort-Object Name | ForEach-Object {
    $schemaDigests[$_.Name] = Get-FileSha256 $_.FullName
}

$receipt = [ordered]@{
    acceptanceSetVersion = $ACCEPTANCE_SET_VERSION
    acceptanceSetDigest  = $acceptanceSetDigest
    testedCandidateSha   = $candidateSha
    cleanTree            = ($cleanTreeStart -and $cleanTreeEnd)
    cleanTreeStart       = $cleanTreeStart
    cleanTreeEnd         = $cleanTreeEnd
    dirtyFilesStart      = @($dirtyLinesStart)
    trackedDirtyEnd      = @($trackedDirty)
    generatedAt          = (Get-Date -Format "o")
    topStatus            = $topStatus
    frozenCounts         = [ordered]@{
        pytestPassed  = $FROZEN_PYTEST_PASSED
        pytestSkipped = $FROZEN_PYTEST_SKIPPED
        vitestPassed  = $FROZEN_VITEST_PASSED
        cargoPassed   = $FROZEN_CARGO_PASSED
    }
    checks               = $results
    spikeStatuses        = $spikeStatuses
    blockedAllowlist     = $BLOCKED_ALLOWLIST
    scriptDigests        = $scriptDigests
    schemaDigests        = $schemaDigests
    environment          = [ordered]@{
        os            = [System.Environment]::OSVersion.VersionString
        pythonVersion = $actualPyVer
        nodeVersion   = (node --version 2>&1 | Out-String).Trim()
    }
}

if (-not $OutFile) { $OutFile = Join-Path $REPO_ROOT ".phase0-acceptance-receipt.json" }
# V7: OutFile fail-closed under the project root.
$outFull = [System.IO.Path]::GetFullPath($OutFile)
$rootFull = [System.IO.Path]::GetFullPath($PROJECT_ROOT).TrimEnd('\')
if ($outFull -ne $rootFull -and -not $outFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    Write-Error "[acceptance] fail-closed: OutFile '$outFull' is not under project root '$PROJECT_ROOT'."
    exit 1
}
$json = $receipt | ConvertTo-Json -Depth 12
[System.IO.File]::WriteAllText($outFull, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "========================================"
Write-Host "Acceptance receipt: $outFull"
Write-Host "topStatus = $topStatus  (failed: $($failed.Count)/$($results.Count))"
Write-Host "========================================"
if ($topStatus -ne "PASS") {
    foreach ($f in $failed) { Write-Host "  FAIL: $($f.id) - $($f.tail)" -ForegroundColor Red }
    exit 1
}
Write-Host "[phase0-acceptance] PASS - all acceptance checks passed" -ForegroundColor Green
exit 0
