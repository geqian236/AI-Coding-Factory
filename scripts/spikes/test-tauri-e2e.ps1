<#
.SYNOPSIS  Spike 7: Tauri E2E framework compatibility probe
.DESCRIPTION
  Derives status from OBSERVABLE evidence only (GPT round-4 item 4):
  actually probes for (a) Tauri CLI, (b) WebView2 runtime, (c) framework files.
  Never hardcodes assertion results. Emits receipt whose status is computed
  from the probe outcomes:
    - all present            -> PASS
    - framework ok but CLI or WebView2 missing -> BLOCKED_UNCERTIFIED
    - framework files missing -> FAIL
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\tauri_e2e"
$receiptPath = "$probeDir\receipt.json"

# --- (a) framework files: actually test each path ---------------------------
$required = @("package.json", "specs\shell.spec.ts")
$missing  = @($required | Where-Object { -not (Test-Path "$probeDir\$_") })
$frameworkPkgOk  = (Test-Path "$probeDir\package.json")
$frameworkSpecOk = (Test-Path "$probeDir\specs\shell.spec.ts")

# --- (b) Tauri CLI: actually invoke it --------------------------------------
# Try `tauri --version`, then `cargo tauri --version`. Capture real version if any.
$tauriCliVersion = $null
$tauriCliOk = $false
foreach ($probe in @(
    @{ exe = "tauri";  cmd = { & tauri --version } },
    @{ exe = "cargo";  cmd = { & cargo tauri --version } }
)) {
    $exeOnPath = Get-Command $probe.exe -ErrorAction SilentlyContinue
    if (-not $exeOnPath) { continue }
    try {
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        $out = (& $probe.cmd 2>&1 | Out-String).Trim()
        $rc  = $LASTEXITCODE
        $ErrorActionPreference = $prevEap
        if ($rc -eq 0 -and $out -match '(\d+\.\d+\.\d+)') {
            $tauriCliVersion = $Matches[1]
            $tauriCliOk = $true
            break
        }
    } catch {
        # keep probing; a throw here is itself observable evidence of absence
    }
}

# --- (c) WebView2 runtime: actually read the registry ------------------------
# WebView2 Evergreen Runtime publishes its version under the EdgeUpdate client
# GUID {F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}. Presence + non-empty pv = installed.
$webview2Version = $null
$webview2Ok = $false
$wvKeys = @(
    "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    "HKCU:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
)
foreach ($k in $wvKeys) {
    try {
        $pv = (Get-ItemProperty -Path $k -Name pv -ErrorAction Stop).pv
        if ($pv -and $pv -ne "0.0.0.0") {
            $webview2Version = $pv
            $webview2Ok = $true
            break
        }
    } catch {
        # key absent = observable evidence WebView2 not installed via this path
    }
}

# --- derive status from observed evidence (never hardcoded) -----------------
$frameworkOk = $frameworkPkgOk -and $frameworkSpecOk
if (-not $frameworkOk) {
    $status = "FAIL"
    $reason = "framework files missing: $($missing -join ', ')"
} elseif ($tauriCliOk -and $webview2Ok) {
    # Runtime deps present; headless E2E launch still not attempted here, so this
    # spike certifies "environment ready" as PASS; actual window E2E is Phase 2.
    $status = "PASS"
    $reason = "Tauri CLI $tauriCliVersion + WebView2 $webview2Version present; framework files present"
} else {
    $status = "BLOCKED_UNCERTIFIED"
    $missingDeps = @()
    if (-not $tauriCliOk) { $missingDeps += "tauri-cli" }
    if (-not $webview2Ok) { $missingDeps += "webview2-runtime" }
    $reason = "framework files present; missing runtime dependency: $($missingDeps -join ', ')"
}

$assertions = @(
    @{ name = "framework_package_json_exists"; passed = $frameworkPkgOk;  detail = "Test-Path package.json = $frameworkPkgOk" }
    @{ name = "framework_spec_exists";         passed = $frameworkSpecOk; detail = "Test-Path specs\shell.spec.ts = $frameworkSpecOk" }
    @{ name = "tauri_cli_available";           passed = $tauriCliOk;      detail = "probed tauri/cargo-tauri --version -> $(if ($tauriCliOk) { $tauriCliVersion } else { 'not found' })" }
    @{ name = "webview2_runtime_available";    passed = $webview2Ok;      detail = "EdgeUpdate client pv -> $(if ($webview2Ok) { $webview2Version } else { 'not found' })" }
)

$receipt = [ordered]@{
    spike            = "tauri_e2e"
    subcheckId       = "spike:tauri_e2e/webview_runtime"
    status           = $status
    reason           = $reason
    unblock_steps    = @(
        "cargo install tauri-cli --version ^2",
        "Install WebView2 Evergreen Runtime from Microsoft",
        "cd tools/compat-probes/tauri_e2e && npm install",
        "npx tauri dev - verify native window opens",
        "npx vitest run specs/ inside Tauri context"
    )
    environment      = [ordered]@{
        os         = [System.Environment]::OSVersion.VersionString
        hostname   = $env:COMPUTERNAME
        ps_version = $PSVersionTable.PSVersion.ToString()
    }
    actions          = @(
        "Test-Path package.json / specs\shell.spec.ts",
        "invoked tauri --version and cargo tauri --version",
        "read EdgeUpdate WebView2 client pv from registry",
        "derived status from observed evidence"
    )
    observable_facts = [ordered]@{
        framework_files_present = $frameworkOk
        missing_files           = @($missing)
        tauri_cli_available     = $tauriCliOk
        tauri_cli_version       = $tauriCliVersion
        webview2_available      = $webview2Ok
        webview2_version        = $webview2Version
    }
    assertions       = @($assertions)
    artifact_digest  = "none"
    timestamp        = (Get-Date -Format "o")
}

$json = $receipt | ConvertTo-Json -Depth 8
$json | Out-File -Encoding utf8 $receiptPath
Write-Host $json
Write-Host "STATUS: $status ($reason)"
# BLOCKED_UNCERTIFIED and PASS are both non-failing for this env-compat spike;
# only FAIL (framework files missing) is a hard failure.
if ($status -eq "FAIL") { exit 1 }
exit 0
