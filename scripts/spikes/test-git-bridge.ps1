<#
.SYNOPSIS  Spike 6: Git Object Bridge probe
.DESCRIPTION  bundle/import/worktree roundtrip; verifies zero user workspace pollution
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\git_object_bridge"
$receiptPath = "$probeDir\receipt.json"

$py = $null
foreach ($c in @("python","py","python3")) {
    try { & $c --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

Write-Host "Running Git Object Bridge probe with $py ..."
$out = & $py "$probeDir\probe.py" 2>&1
$ec  = $LASTEXITCODE
$out -join "`n" | Write-Host

if (-not (Test-Path $receiptPath)) {
    @{ spike="git_object_bridge"; status="ERROR"; error="no receipt.json produced"
       exit_code=$ec; timestamp=(Get-Date -Format "o") } |
        ConvertTo-Json | Out-File -Encoding utf8 $receiptPath
}
$r = Get-Content $receiptPath -Raw | ConvertFrom-Json
Write-Host "STATUS: $($r.status)"
if ($r.status -ne "PASS") { exit 1 }
# 显式 exit 0：spike 通过时必须设置 $LASTEXITCODE，否则 test.ps1 的 Run-Suite
# 在 StrictMode 下读取未定义的 $LASTEXITCODE 会抛异常并中断整个套件。
exit 0