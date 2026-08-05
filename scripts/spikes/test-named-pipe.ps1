<#
.SYNOPSIS  Spike 4: Authenticated Named Pipe probe
.DESCRIPTION  Runs Python server (SID ACL + nonce + replay rejection)
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\named_pipe"
$receiptPath = "$probeDir\receipt.json"

$py = $null
foreach ($c in @("python","py","python3")) {
    try { & $c --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

# Check we are on Windows (named pipes are Windows-only)
if ($env:OS -notmatch "Windows") {
    $blocked = @{ spike="named_pipe"; status="BLOCKED_UNCERTIFIED"
                  reason="Windows Named Pipes require Windows host"
                  timestamp=(Get-Date -Format "o") }
    $blocked | ConvertTo-Json | Out-File -Encoding utf8 $receiptPath
    Write-Host ($blocked | ConvertTo-Json)
    exit 0
}

Write-Host "Running Named Pipe probe with $py ..."
$out = & $py "$probeDir\python_server.py" 2>&1
$ec  = $LASTEXITCODE
$outStr = $out -join "`n"
Write-Host $outStr

if (-not (Test-Path $receiptPath)) {
    @{ spike="named_pipe"; status="ERROR"; error="no receipt.json produced"
       stdout=$outStr; exit_code=$ec; timestamp=(Get-Date -Format "o") } |
        ConvertTo-Json | Out-File -Encoding utf8 $receiptPath
}

$r = Get-Content $receiptPath -Raw | ConvertFrom-Json
Write-Host "STATUS: $($r.status)"
if ($r.status -notin @("PASS","BLOCKED_UNCERTIFIED")) { exit 1 }
# 成功/受阻时显式 exit 0：确保 $LASTEXITCODE 被设置，避免 test.ps1 StrictMode
# 读取未定义变量而中断套件。
exit 0