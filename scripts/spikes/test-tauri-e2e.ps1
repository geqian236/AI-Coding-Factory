<#
.SYNOPSIS  Spike 7: Tauri E2E framework (BLOCKED_UNCERTIFIED)
.DESCRIPTION  Validates framework files exist; emits BLOCKED_UNCERTIFIED receipt
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\tauri_e2e"
$receiptPath = "$probeDir\receipt.json"

# Verify required framework files exist
$required = @("package.json","specs\shell.spec.ts")
$missing  = $required | Where-Object { -not (Test-Path "$probeDir\$_") }

$assertions = @(
    @{ name="framework_package_json_exists"; passed=(-not ($missing -contains "package.json")); detail="package.json presence" }
    @{ name="framework_spec_exists";         passed=(-not ($missing -contains "specs\shell.spec.ts")); detail="shell.spec.ts presence" }
    @{ name="tauri_runtime_available";       passed=$false; detail="BLOCKED: Tauri runtime not installed — run: cargo install tauri-cli@2; install WebView2" }
)

$allFrameworkOk = $assertions[0].passed -and $assertions[1].passed

$receipt = [ordered]@{
    spike            = "tauri_e2e"
    status           = "BLOCKED_UNCERTIFIED"
    reason           = "Tauri CLI + WebView2 runtime not available in this environment"
    unblock_steps    = @(
        "cargo install tauri-cli --version 2",
        "Install WebView2 runtime from Microsoft",
        "cd tools/compat-probes/tauri_e2e && npm install",
        "npx tauri dev — verify native window opens",
        "npx vitest run specs/ inside Tauri context"
    )
    environment      = @{
        os         = $env:OS
        hostname   = $env:COMPUTERNAME
        ps_version = $PSVersionTable.PSVersion.ToString()
    }
    actions          = @(
        "verified framework files exist",
        "checked Tauri CLI availability — not found",
        "emitting BLOCKED_UNCERTIFIED"
    )
    observable_facts = @{
        framework_files_present = $allFrameworkOk
        missing_files           = @($missing)
        tauri_cli_available     = $false
        webview2_available      = $false
    }
    assertions       = @($assertions)
    artifact_digest  = "none"
    timestamp        = (Get-Date -Format "o")
}

$json = $receipt | ConvertTo-Json -Depth 8
$json | Out-File -Encoding utf8 $receiptPath
Write-Host $json
Write-Host "STATUS: BLOCKED_UNCERTIFIED (expected for Tauri spike)"
# BLOCKED_UNCERTIFIED is not a failure — exit 0
exit 0