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
        (only the gitignored receipt may appear dirty); frozen counts (352/1,
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

# GPT round-9 P0: per-worktree Cargo target isolation. Rust test binaries bake
# env!("CARGO_MANIFEST_DIR") (the compile-time worktree path) into themselves and
# read contracts/golden/*.json through it; a shared cargo-target let one worktree
# run another's binary (false FAIL if that tree was deleted, false PASS if it still
# exists). Get-WorktreeTargetDir derives <DATA_ROOT>\cargo-target\<digest> from a
# SHA-256 of the normalized $REPO_ROOT so each checkout is isolated. dev.ps1,
# check.ps1 and the durable-io/runner-identity wrappers dot-source the same helper.
. (Join-Path $PSScriptRoot "_worktree-target.ps1")
# GPT round-13 P1-1: the uv-locked Python probe is a single shared function (used by
# B-2 here AND check.ps1's pre-gate probe AND the regression test), so a mutation can
# be caught in one place. See scripts/_python-probe.ps1.
. (Join-Path $PSScriptRoot "_python-probe.ps1")

# --- W3 storage contract: every cache/temp path must live under the project ---
# root. GPT round-5 item 3: the runner set only Rust paths, leaving TEMP/TMP/
# COREPACK_HOME/PNPM_STORE_DIR on C:. Mirror dev.ps1: bind all of them under
# DATA_ROOT and fail-closed assert each resolves under the project root, so the
# single documented command never writes outside D:\codex项目.
$_cacheDirs = @{
    TEMP                     = (Join-Path $DATA_ROOT "tmp")
    TMP                      = (Join-Path $DATA_ROOT "tmp")
    COREPACK_HOME            = (Join-Path $DATA_ROOT "corepack")
    PNPM_STORE_DIR           = (Join-Path $DATA_ROOT "pnpm-store")
    PNPM_HOME                = (Join-Path $DATA_ROOT "pnpm-home")
    NPM_CONFIG_CACHE         = (Join-Path $DATA_ROOT "npm-cache")
    UV_CACHE_DIR             = (Join-Path $DATA_ROOT "uv-cache")
    PIP_CACHE_DIR            = (Join-Path $DATA_ROOT "pip-cache")
    PLAYWRIGHT_BROWSERS_PATH = (Join-Path $DATA_ROOT "playwright")
}
# Reparse-point guard (round-6 item 5b): a purely lexical GetFullPath check cannot
# detect a junction/symlink anywhere in the chain that redirects the PHYSICAL target
# outside the project root (e.g. $DATA_ROOT\tmp junctioned to C:\tmp - the lexical
# path still looks in-bounds while writes land on C:). After creating each cache dir
# we walk the chain from the dir up to the project root and reject if ANY component
# carries the ReparsePoint attribute, so no junction escape can slip a write onto C:.
# Returns the offending path (or $null). Must be defined before the loop below (PS
# executes top-to-bottom; the loop runs at parse-forward time, before later helpers).
function Test-ReparsePointInChain {
    param([string]$Leaf, [string]$Root)
    $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    $cur = [System.IO.Path]::GetFullPath($Leaf).TrimEnd('\')
    while ($cur) {
        if (Test-Path -LiteralPath $cur) {
            $attr = (Get-Item -LiteralPath $cur -Force).Attributes
            if ($attr -band [System.IO.FileAttributes]::ReparsePoint) { return $cur }
        }
        if ($cur -eq $rootFull) { break }
        $parent = Split-Path $cur -Parent
        if (-not $parent -or $parent -eq $cur) { break }
        $cur = $parent.TrimEnd('\')
    }
    return $null
}
$rootFull = [System.IO.Path]::GetFullPath($PROJECT_ROOT).TrimEnd('\')
foreach ($k in $_cacheDirs.Keys) {
    $dir = $_cacheDirs[$k]
    # GPT round-7 item 4: validate BEFORE the first write. The old order ran New-Item
    # first and only then checked lexical root + reparse chain - so a junctioned target
    # dir was already created (a write) before the guard tripped, violating "fail-closed
    # before any write". Now: (1) lexical check, (2) reparse-check the nearest EXISTING
    # ancestor chain (Test-ReparsePointInChain skips not-yet-existing leaves and walks up
    # to the first real ancestor), both BEFORE New-Item; (3) create; (4) re-check the FULL
    # chain including the newly created dir.
    # (1) lexical: the physical target must resolve under the project root.
    $full = [System.IO.Path]::GetFullPath($dir)
    if ($full.TrimEnd('\') -ne $rootFull -and -not $full.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "[acceptance] fail-closed: cache path $k '$full' is not under project root '$PROJECT_ROOT'."
        exit 1
    }
    # (2) reparse-check the nearest existing ancestor chain BEFORE creating anything:
    # if a parent is already a junction/symlink pointing off the project root, creating
    # under it would land the write on C: - reject before the write happens.
    $rpPre = Test-ReparsePointInChain -Leaf $dir -Root $PROJECT_ROOT
    if ($rpPre) {
        Write-Error "[acceptance] fail-closed: cache path $k ancestor '$rpPre' is a reparse point (junction escape) - refusing to write under it."
        exit 1
    }
    # (3) now safe to create.
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    # (4) re-check the FULL chain (incl. the newly created dir) - defends against a TOCTOU
    # swap between the pre-check and creation.
    $rpPost = Test-ReparsePointInChain -Leaf $dir -Root $PROJECT_ROOT
    if ($rpPost) {
        Write-Error "[acceptance] fail-closed: cache path $k chain contains reparse point '$rpPost' after creation (possible junction escape out of project root)."
        exit 1
    }
    Set-Item -Path "Env:$k" -Value $dir
}

# GPT round-11 P0: pin the uv-locked environment to Python 3.12. pyproject.toml's
# requires-python is >=3.12, so WITHOUT this uv may resolve 3.13 for A-1/GATES while
# B-2 checked a system 3.12 -> false PASS (reviewer's UV_PYTHON=3.13 attack). Setting
# UV_PYTHON here (before the first uv command, A-1-sync) forces the single .venv that
# A-1 / GATES / B-2 all use to be 3.12; B-2 then asserts (3,12) FROM that env and the
# receipt records that env's version. Child spike wrappers inherit this env var but
# never call uv (they use dev.ps1 + cargo), so it is a no-op for them.
$env:UV_PYTHON = "3.12"
# uvPythonVersion: single source for B-2 assert + receipt.pythonVersion (set in B-2).
# GPT round-12 P2: init to $null (NOT a placeholder string). If B-2 never runs or its
# strict parse fails, the receipt must carry $null, not a stale/fake version string.
$uvPythonVersion = $null

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
# round-9 P0: 332 -> 334 (+2 Windows-only cargo-target isolation regression tests).
# round-10 P1: 334 -> 336 (+2 more Windows-only tests in test_cargo_target_isolation.py:
# case-variant collision + test.ps1 caller-level override; runner is Windows so all run).
# round-14 P1/P2: 338 -> 340 (+2 net tests in test_python_pin_probe.py: fail-closed contract
# negative [Windows-only] + dot-source comment-immune mutation defense [cross-platform]).
# round-12 P2: 336 -> 338 (+2 Windows-only tests in test_python_pin_probe.py: pythonVersion
# strict-parse under hostile VIRTUAL_ENV + warning-lands-on-stderr repro; runner is Windows).
# round-15 P1/P2: 340 -> 341 (+1 Windows-only test in test_python_pin_probe.py: non-terminating
# read error -> unreadable + uvCalls=0; the fail-closed-contract test also gained multiline /
# oversized-digit / leading-zero boundary cases but stays one test).
# PR#2 CI-fix: 341 -> 352 (+11 items in tests/contract/test_ci_blocker_fixes.py: 7 cross-platform
# [desktop node-version-file + .node-version=22 + @types/node in package & pnpm-lock importer +
# windows-probes preheat-before-spike-loop + preheat-fail-closed + dev.ps1-no-implicit-install]
# and 4 Windows-only [durable-io BLOCKED receipt evidence x2 params + validator core-blocked-not-
# INVALID + envcompat structural anchor]). Windows runner (spikes-first) = 352/1; the 11 all run
# on Windows. Ubuntu contracts (bare) = 207/14: 13 Windows-only contract items + 1 sqlite-receipt.
$FROZEN_PYTEST_PASSED  = 352
$FROZEN_PYTEST_SKIPPED = 1
$FROZEN_VITEST_PASSED  = 70
$FROZEN_CARGO_PASSED   = 14
# Frozen identity of the single allowed skip (V5: exact skip node enforced).
$FROZEN_SKIP_MATCH = "test_config.py"

# Frozen BLOCKED allowlist: only these subcheck IDs may be BLOCKED_UNCERTIFIED.
# Any other non-PASS check = overall FAIL. Keyed by subcheckId (V5: unified id).
$BLOCKED_ALLOWLIST = [ordered]@{
    "spike:sqlite_wal_full/disk_full_enospc"  = "admin mounts a <=16MiB VHD then bench --probe-dir <VHD> certifies real ENOSPC"
    "spike:tauri_e2e/webview_runtime"         = "run the E2E spike on a host with WebView2 Runtime + Tauri CLI installed"
    # NOTE: runner_identity is NOT in this allowlist. It is a core spike that must PASS.
    # The wrapper emits BLOCKED_UNCERTIFIED when Docker is unavailable, but that causes
    # topStatus=FAIL (not PASS). Adding runner_identity here requires an explicit
    # reviewer decision and would expand the frozen acceptance set.
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

# Strip ANSI/VT escape sequences (W1: vitest/pytest embed color codes between
# tokens like "Tests <ESC>[32m70 passed", which breaks count regexes and makes
# a PASS depend on the terminal's color environment). We also set NO_COLOR /
# FORCE_COLOR=0 to suppress codes at the source; this is the defensive second
# layer so count parsing is deterministic regardless of tool/term behavior.
function Remove-Ansi {
    param([string]$Text)
    if ($null -eq $Text) { return "" }
    # CSI sequences: ESC [ ... final-byte ; also lone ESC and OSC sequences.
    $esc = [char]27
    return ($Text -replace "$esc\[[0-9;?]*[ -/]*[@-~]", "" -replace "$esc\][^$esc]*$esc\\", "" -replace "$esc[@-Z\\-_]", "")
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
    # W1: strip ANSI so tail + returned string are deterministic for count regexes.
    $clean = Remove-Ansi $stdout
    $results.Add([ordered]@{
        id       = $Id
        desc     = $Desc
        exitCode = $code
        passed   = $passed
        tail     = ($clean -split "`n" | Select-Object -Last 3) -join " | "
    })
    return $clean
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
    # round-9 P0: per-worktree target (not the shared cargo-target) so this run's
    # cargo build/test both produce AND consume binaries under this worktree's own
    # digest; a leftover binary from another checkout can never be executed here.
    $env:CARGO_TARGET_DIR = Get-WorktreeTargetDir -WorktreeRoot $REPO_ROOT -DataRoot $DATA_ROOT
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

# Spike-receipt evidence validation now lives in the shared module dot-sourced
# below (GPT round-6 item 4: single source of truth). The runner and CI both call
# Test-SpikeReceiptEvidence from scripts/spikes/_receipt-validator.ps1, so a
# fail-open hole can only be closed in one place. The old in-file Test-SpikeReceipt
# was removed to eliminate the second, drifting copy.
. (Join-Path $PSScriptRoot "spikes\_receipt-validator.ps1")

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

# --- spikes FIRST (round-6 item 1): execute every wrapper and validate evidence
# BEFORE A-1. Root cause of the fresh-worktree failure: A-1's
# test_emit_manifest_accepts_blocked_spike_receipt SKIPS when the sqlite receipt
# is absent, so a truly fresh detached worktree (no gitignored receipt) counted
# 326/2 (the reviewer's exact observation), NOT the leftover-receipt 327/1 that
# a prior dry-run receipt `git status` cannot see had leaned on. Running spikes
# first makes the single command self-contained: purge all 7 receipts, regenerate
# them THIS round, then A-1 deterministically reads the fresh sqlite receipt so
# that skip flips to pass. Combined with the 3 new round-6 item-3 conformance
# tests (test_receipt_schema_conformance.py, real jsonschema present in the locked
# py312) and the Windows-only isolation/binding tests, the frozen A-1
# baseline is now 352 passed / 1 skipped (Windows runner; Ubuntu fresh = 207/14:
# 13 Windows-only contract tests skip off-platform AND the sqlite-receipt test
# skips because the Ubuntu `contracts` CI job runs pytest WITHOUT spikes-first.
# round-13/14/15: the pin-probe file has 5 tests - 3 Windows-only (shared-probe end-to-end
# under hostile VIRTUAL_ENV + fail-closed .python-version contract negatives + round-15
# non-terminating-read-error-without-calling-uv) and 2 cross-platform (mutation defense +
# dot-source comment-immunity), so 2 of the 5 run on Ubuntu too.
# PR#2 CI-fix: tests/contract/test_ci_blocker_fixes.py adds 11 items - 4 Windows-only
# (durable-io BLOCKED receipt evidence x2 + validator core-blocked verdict x2) and 7
# cross-platform (ci.yml/package.json/pnpm-lock structural parses). 13 Windows-only
# (cargo 4 + ci-blocker 4 + pin-probe 3 + binding 2) + 1 sqlite skip = Ubuntu 207/14).
#
# Evidence validation is delegated to the shared Test-SpikeReceiptEvidence (see
# scripts/spikes/_receipt-validator.ps1, also dot-sourced by CI) so runner and CI
# cannot drift (round-6 item 4). Each wrapper runs as a CHILD process (its
# `exit N` cannot kill this runner); we capture its exit code (item 2c), stamp a
# runBinding into the six non-sqlite receipts (item 2d; sqlite binds via run_nonce
# to avoid disturbing emit_manifest's digest recompute over eventBatchParameters),
# then validate: name match + freshness (mtime >= runStart) + strict-boolean
# assertions + all-blockers-allowlisted + exit-code + run binding. Each receipt is
# digest-bound into the total receipt.
$spikeReceipts = [ordered]@{
    "clock_source"       = "tools/compat-probes/clock_source/receipt.json"
    "windows_durable_io" = "tools/compat-probes/windows_durable_io/receipt.json"
    "named_pipe"         = "tools/compat-probes/named_pipe/receipt.json"
    "git_object_bridge"  = "tools/compat-probes/git_object_bridge/receipt.json"
    "runner_identity"    = "tools/compat-probes/runner_identity/receipt.json"
    "sqlite_wal_full"    = "tools/compat-probes/sqlite_wal_full/receipt.json"
    "tauri_e2e"          = "tools/compat-probes/tauri_e2e/receipt.json"
}
$spikeWrappers = [ordered]@{
    "clock_source"       = "scripts/spikes/test-clock-source.ps1"
    "windows_durable_io" = "scripts/spikes/test-durable-io.ps1"
    "named_pipe"         = "scripts/spikes/test-named-pipe.ps1"
    "git_object_bridge"  = "scripts/spikes/test-git-bridge.ps1"
    "runner_identity"    = "scripts/spikes/test-runner-identity.ps1"
    "sqlite_wal_full"    = "scripts/spikes/test-sqlite-wal.ps1"
    "tauri_e2e"          = "scripts/spikes/test-tauri-e2e.ps1"
}
$envCompatSpikes = @("sqlite_wal_full", "tauri_e2e")
$stampScript = Join-Path $PSScriptRoot "spikes\stamp_run_binding.py"

# Per-run identity: bound into the total receipt and stamped into each receipt.
$runId    = [guid]::NewGuid().ToString()
$runNonce = -join ((1..16) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
$runStart = Get-Date

# Purge ALL receipts BEFORE running (round-6 item 1): a hermetic run must not lean
# on a gitignored receipt from a prior round. With the freshness check
# (mtime >= runStart) this proves every receipt A-1 later reads was produced by
# THIS run, closing the historical-pollution hole `git status` cannot detect.
#
# GPT round-7 item 1: delete MUST be fail-closed. Under the global
# $ErrorActionPreference='Continue', a locked/undeletable receipt let Remove-Item
# fail silently; the stale file survived, and a later stamp/wrapper write merely
# refreshed its mtime so the freshness check still accepted it. We now delete with
# -ErrorAction Stop and ASSERT the file is gone; any residual receipt aborts the run.
foreach ($name in $spikeReceipts.Keys) {
    $rp = Join-Path $REPO_ROOT $spikeReceipts[$name]
    if (Test-Path -LiteralPath $rp) {
        try {
            Remove-Item -LiteralPath $rp -Force -ErrorAction Stop
        } catch {
            Write-Error "[acceptance] fail-closed: could not purge stale receipt '$rp': $_"
            exit 1
        }
    }
    if (Test-Path -LiteralPath $rp) {
        Write-Error "[acceptance] fail-closed: stale receipt '$rp' still present after purge (locked?); a refreshed mtime would defeat freshness."
        exit 1
    }
}

# Rust env for cargo-building wrappers (clock/durable-io/named-pipe/runner-
# identity). Idempotent; A-3b calls Set-RustGnuEnv again later.
Set-RustGnuEnv

# Run identity to each wrapper via env (item 2d): a wrapper inherits it as a child
# process. sqlite reuses SPIKE_RUN_NONCE as its --nonce so its receipt.run_nonce
# binds to this run; the runner stamps runBinding into the other six.
$env:SPIKE_RUN_ID        = $runId
$env:SPIKE_RUN_NONCE     = $runNonce
$env:SPIKE_CANDIDATE_SHA = $candidateSha

$spikeStatuses = [ordered]@{}
$spikeBindings = [ordered]@{}
foreach ($name in ($spikeReceipts.Keys)) {
    $isEnv   = ($envCompatSpikes -contains $name)
    $wrapper = Join-Path $REPO_ROOT $spikeWrappers[$name]
    $receiptPath = $spikeReceipts[$name]
    Write-Host "-- [spike:$name] executing $($spikeWrappers[$name]) --" -ForegroundColor Cyan
    $wrapperExit = 1
    if (Test-Path -LiteralPath $wrapper) {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $wrapper 2>&1 | Out-String | Write-Host
        $wrapperExit = $LASTEXITCODE
        if ($null -eq $wrapperExit) { $wrapperExit = 0 }
        Write-Host "  [spike:$name] wrapper exit=$wrapperExit"
    } else {
        Write-Host "  [spike:$name] wrapper MISSING: $wrapper"
    }
    # GPT round-7 item 1: capture the receipt mtime BEFORE stamping. Stamping (below)
    # rewrites the file and refreshes mtime, so freshness MUST be judged on this
    # pre-stamp timestamp - otherwise a wrapper that failed to regenerate its receipt
    # (stale file surviving a failed purge) would be laundered fresh by the stamp write.
    $preStampMtime = [datetime]::MinValue
    if (Test-Path -LiteralPath $receiptPath) {
        $preStampMtime = (Get-Item -LiteralPath $receiptPath).LastWriteTime
    }
    # GPT round-7 item 2: sqlite's probe is bench.py, which self-reports
    # probeDigest = sha256(bench.py) + candidateSha = git HEAD. The old validator's
    # run_nonce branch checked ONLY the nonce, so a receipt with the right nonce but
    # zeroed candidateSha/probeDigest passed. Compute the expected bench.py digest here
    # so the validator can bind sqlite to the real on-disk probe + this candidate.
    $expectProbeDigest = ""
    if ($name -eq "sqlite_wal_full") {
        $benchPy = Join-Path $REPO_ROOT "tools/compat-probes/sqlite_wal_full/bench.py"
        $expectProbeDigest = Get-FileSha256 $benchPy
    }
    # Stamp runBinding into the six non-sqlite receipts (item 2d). sqlite binds via
    # run_nonce + candidateSha + probeDigest (all written by bench.py); stamping sqlite
    # would round-trip a receipt whose digests emit_manifest recomputes.
    if ($name -ne "sqlite_wal_full" -and (Test-Path -LiteralPath $receiptPath)) {
        python $stampScript $receiptPath $name | Write-Host
        if ($LASTEXITCODE -ne 0) {
            # Fail-closed: stamp failure means runBinding is absent, so the validator
            # will see BINDING_MISSING and reject. Make the runner consistent with CI
            # (which also fails hard on stamp failure) rather than merely warning.
            Add-CheckResult "stamp:$name" "stamp runBinding into receipt" $false "stamp_run_binding exit=$LASTEXITCODE"
        }
    }
    # Shared evidence validation (single source of truth with CI; item 4). ReceiptMtime
    # is the PRE-stamp mtime so freshness is judged before stamping refreshed it.
    $vArgs = @{
        Name               = $name
        Path               = $receiptPath
        EnvCompat          = $isEnv
        Allowlist          = [string[]]$BLOCKED_ALLOWLIST.Keys
        RunStart           = $runStart
        ReceiptMtime       = $preStampMtime
        WrapperExitCode    = $wrapperExit
        ExpectRunId        = $runId
        ExpectRunNonce     = $runNonce
        ExpectCandidateSha = $candidateSha
        ExpectProbeDigest  = $expectProbeDigest
    }
    $v = Test-SpikeReceiptEvidence @vArgs
    $spikeStatuses[$name] = $v.status
    $spikeBindings[$name] = [ordered]@{
        status        = $v.status
        ok            = $v.ok
        receiptDigest = (Get-FileSha256 $receiptPath)
    }
    Add-CheckResult "spike:$name" "spike executed + receipt evidence" $v.ok "status=$($v.status); $($v.detail)"
}

# --- A-1 + B-1 + A-2 (V1: install before vitest) ---------------------------
# GPT round-8: A-1 must consume uv.lock (not rely on whatever the system Python
# happens to have installed). Run uv sync --locked --all-groups first to populate
# .venv from the lockfile, then use uv run --locked pytest so the locked environment
# is used. UV_CACHE_DIR is already set to the project data root (storage contract
# compliant). .venv is gitignored, so this sync does not dirty the tree.
#
# GPT round-10 P0: use --locked (NOT --frozen) here and on every uv run below, to
# MATCH the frozen contract in PHASE_0_ACCEPTANCE.md line 194 (`uv sync --locked`).
# --frozen only refuses to CHANGE the lock; --locked additionally ASSERTS uv.lock is
# consistent with pyproject.toml and fails on drift. A PASS receipt recording --frozen
# does not prove the lock-consistency the doc promises, so the doc and the single
# entry point were out of sync. uv run --locked re-checks the lock at run time too.
Invoke-AcceptanceCheck "A-1-sync" "uv sync --locked (assert lock consistent + populate .venv before pytest)" {
    & uv sync --locked --all-groups
} | Out-Null

$a1out = Invoke-AcceptanceCheck "A-1" "Python three-dir tests" {
    $env:PYTHONIOENCODING = "utf-8"
    # uv run --locked python -m pytest: -m flag adds cwd to sys.path (same as
    # python -m pytest), required for tests that do 'from tests.conftest import
    # REPO_ROOT'. Without -m, uv run pytest (script mode) does not add cwd, causing
    # ModuleNotFoundError on test_event_hash_vectors.py and test_plan_hash_vectors.py.
    # --locked re-asserts lock consistency at run time (round-10 P0).
    & uv run --locked python -m pytest tests/contract tests/agent tests/security -q -rs
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
# --no-fail-fast: run BOTH test binaries (event_vectors + plan_vectors) and
# report each result, so a first-binary hiccup cannot hide the second's count.
# Bounded single retry: a freshly-linked test.exe can fail to LAUNCH on its
# first execution on Windows (Defender real-time scan locks the image / libgcc
# DLL not yet flushed) -> cargo exits 101 before any assertion runs. The image
# is byte-identical across runs, so a genuine assertion defect fails BOTH
# attempts; only a transient first-launch failure clears on retry. This keeps
# A-3b deterministic on a cold detached worktree (the reviewer's exact case).
Set-RustGnuEnv
$a3out = Invoke-AcceptanceCheck "A-3b" "Rust contract runtime assertions (cargo test, gnu)" {
    # Capture each attempt's output into $out (not the pipeline) so a retry
    # REPLACES rather than appends - otherwise the count regex below would sum
    # both attempts (e.g. 7 + 7+7 = 21) and break A-3b-count. Only the final
    # attempt is emitted, so the count reflects one clean run (14).
    $out = (cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked --no-fail-fast 2>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[A-3b] first attempt exit=$LASTEXITCODE; retrying once (Windows first-launch transient guard)"
        $out = (cargo +stable-x86_64-pc-windows-gnu test -p factory-contracts --locked --no-fail-fast 2>&1 | Out-String)
    }
    Write-Output $out
}
# Sum "N passed" across the test binaries (event_vectors + plan_vectors).
$cargoPassed = 0
foreach ($m in [regex]::Matches($a3out, "test result: ok\.\s+(\d+)\s+passed")) {
    $cargoPassed += [int]$m.Groups[1].Value
}
Add-CheckResult "A-3b-count" "cargo test frozen count == $FROZEN_CARGO_PASSED" ($cargoPassed -eq $FROZEN_CARGO_PASSED) "observed $cargoPassed passed"

# --- B-2 Python pin -------------------------------------------------------
# GPT round-11 P0: assert 3.12 FROM the uv-locked env (the one A-1/GATES ran in),
# NOT a system python that may differ. sys.version_info[:2]==(3,12) is checked inside
# `uv run --locked python`, so setting UV_PYTHON=3.13 can no longer let A-1 run on 3.13
# while B-2 silently passes on system 3.12. The exact version string feeds the receipt.
Invoke-AcceptanceCheck "B-2" "Python 3.12 pinned (uv-locked env)" {
    # GPT round-13 P1-1: delegate to the shared Get-UvLockedPythonVersion (scripts/
    # _python-probe.ps1) so B-2, check.ps1's pre-gate probe, and the regression test all
    # exercise ONE implementation. The function separates stderr (2>$null, round-12 P2),
    # strict-parses exactly one ^\d+\.\d+\.\d+$ line, and pins major.minor from
    # .python-version (round-13 P1-2: no hardcoded patch). It records the clean version
    # into receipt.pythonVersion; fail-closed on mismatch / bad parse.
    $probe = Get-UvLockedPythonVersion -RepoRoot $REPO_ROOT
    if ($probe.ok) {
        $script:uvPythonVersion = $probe.version
        Write-Host "uv-locked python = $($probe.version) ($($probe.expectedMajorMinor) confirmed)"
        $global:LASTEXITCODE = 0
    } else {
        # Leave $script:uvPythonVersion as-is ($null unless a prior clean parse set it).
        Write-Host "B-2 fail-closed: ok=$($probe.ok) exit=$($probe.exitCode) matchCount=$($probe.matchCount) version='$($probe.version)' expected=$($probe.expectedMajorMinor).x"
        $global:LASTEXITCODE = 1
    }
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

# (spikes ran FIRST, before A-1 - see the spike block above; round-6 item 1.)

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
# GPT round-14 P1: the receipt's scriptDigests bound only the three "top-level"
# scripts and OMITTED the dot-sourced production helpers. _python-probe.ps1 (the
# uv-locked Python probe, round-13) and _worktree-target.ps1 (per-worktree Cargo
# target isolation, round-9) are BOTH real production code that acceptance depends
# on; a tamper there previously left no digest trace in the evidence. Bind them too.
$scriptDigests = [ordered]@{
    "phase0-acceptance.ps1" = Get-FileSha256 (Join-Path $PSScriptRoot "phase0-acceptance.ps1")
    "check.ps1"             = Get-FileSha256 (Join-Path $PSScriptRoot "check.ps1")
    "dev.ps1"               = Get-FileSha256 (Join-Path $PSScriptRoot "dev.ps1")
    "_python-probe.ps1"     = Get-FileSha256 (Join-Path $PSScriptRoot "_python-probe.ps1")
    "_worktree-target.ps1"  = Get-FileSha256 (Join-Path $PSScriptRoot "_worktree-target.ps1")
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
    runId                = $runId
    runNonce             = $runNonce
    runStartedAt         = ($runStart.ToString("o"))
    spikeStatuses        = $spikeStatuses
    spikeBindings        = $spikeBindings
    blockedAllowlist     = $BLOCKED_ALLOWLIST
    scriptDigests        = $scriptDigests
    schemaDigests        = $schemaDigests
    environment          = [ordered]@{
        os            = [System.Environment]::OSVersion.VersionString
        # round-11 P0: pythonVersion is the uv-locked env version (asserted ==3.12 by
        # B-2), NOT the system python $actualPyVer - so the receipt records the exact
        # interpreter A-1/GATES/B-2 actually ran under, not one that merely happened to
        # be on PATH. Falls back to $actualPyVer only if B-2 somehow did not set it.
        pythonVersion = $(if ($uvPythonVersion) { $uvPythonVersion } else { $actualPyVer })
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
