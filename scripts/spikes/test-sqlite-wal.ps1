<#
.SYNOPSIS  Spike 2: SQLite WAL FULL probe
.DESCRIPTION  Runs Python bench.py, emits receipt.json
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$benchPy    = "$scriptRoot\tools\compat-probes\sqlite_wal_full\bench.py"
$receiptOut = "$scriptRoot\tools\compat-probes\sqlite_wal_full\receipt.json"

$py = $null
foreach ($candidate in @("python","python3","py")) {
    try { $v = & $candidate --version 2>&1; if ($LASTEXITCODE -eq 0) { $py = $candidate; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

Write-Host "Running SQLite WAL FULL probe with $py ..."
$out = & $py $benchPy 2>&1
$ec  = $LASTEXITCODE
$outStr = $out -join "`n"
Write-Host $outStr

if (-not (Test-Path $receiptOut)) {
    $fallback = @{
        spike="sqlite_wal_full"; status="ERROR"
        error="bench.py did not write receipt.json"
        stdout=$outStr; exit_code=$ec
        timestamp=(Get-Date -Format "o")
    }
    $fallback | ConvertTo-Json -Depth 5 | Out-File -Encoding utf8 $receiptOut
}

$receipt = Get-Content $receiptOut -Raw | ConvertFrom-Json
Write-Host "STATUS: $($receipt.status)"
if ($receipt.status -ne "PASS") { exit 1 }
# 显式 exit 0：spike 通过时必须设置 $LASTEXITCODE，否则 test.ps1 的 Run-Suite
# 在 StrictMode 下读取未定义的 $LASTEXITCODE 会抛异常并中断整个套件。
exit 0