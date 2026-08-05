<#
.SYNOPSIS  Spike 2: SQLite WAL FULL probe
.DESCRIPTION
  跑 tools/compat-probes/sqlite_wal_full/bench.py,生成 receipt.json。

  P0-5 修复（防 stale-PASS 掩盖本轮 FAIL）：
    - 跑子进程**前**强制 Remove-StaleReceipt 删除旧 receipt
    - 生成 nonce 传子进程（--nonce），要求 receipt.run_nonce 一致
    - 读 receipt 用 -Encoding utf8 修 GBK 解码
    - 写 receipt 复用 Write-ReceiptNoBom 修 BOM
    - 子进程退出码非 0 → 写 FAIL receipt + exit 1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$benchPy    = "$scriptRoot\tools\compat-probes\sqlite_wal_full\bench.py"
$receiptOut = "$scriptRoot\tools\compat-probes\sqlite_wal_full\receipt.json"
$common     = "$PSScriptRoot\_common.ps1"

# P0-5：抽公共 helper（修 UTF-8/BOM + stale receipt + nonce）
. $common

# 本轮 nonce：传给子进程，再断言 receipt.run_nonce 一致，避免读到陈旧 PASS。
$nonce = [guid]::NewGuid().ToString("N")

$py = $null
foreach ($candidate in @("python","python3","py")) {
    try { $v = & $candidate --version 2>&1; if ($LASTEXITCODE -eq 0) { $py = $candidate; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

# P0-5：跑子进程前删旧 receipt，旧 PASS 不能掩盖本轮 FAIL。
Remove-StaleReceipt -Path $receiptOut

Write-Host "Running SQLite WAL FULL probe with $py ..."
# P0-5：用 try/catch 捕获子进程调用异常（如 python 找不到 bench.py 报
# NativeCommandError），不能让异常直接抛到上层吞掉 FAIL receipt 写盘步骤。
try {
    $out = & $py $benchPy --nonce $nonce 2>&1
    $ec  = $LASTEXITCODE
    $outStr = $out -join "`n"
    Write-Host $outStr
} catch {
    $ec = -1
    $outStr = ($_ | Out-String)
    Write-Host "subprocess raised: $outStr"
}

# 子进程退出码非 0 → 写 FAIL receipt + exit 1（不依赖 bench 是否写盘）
if ($ec -ne 0) {
    $failReceipt = [ordered]@{
        spike       = "sqlite_wal_full"
        status      = "FAIL"
        error       = "bench.py 子进程退出码非 0（$ec），旧 PASS 不再掩盖本轮 FAIL"
        exit_code   = $ec
        run_nonce   = $nonce
        bench_stdout = $outStr
        timestamp   = (Get-Date -Format "o")
    }
    Write-ReceiptNoBom -Path $receiptOut -Object $failReceipt
    exit 1
}

# 再校验本轮 receipt 是否存在：若 bench 异常退出但 exit 0（极少见），仍保险。
if (-not (Test-Path $receiptOut)) {
    $missing = [ordered]@{
        spike       = "sqlite_wal_full"
        status      = "ERROR"
        error       = "bench.py exited 0 but no receipt.json produced"
        run_nonce   = $nonce
        bench_stdout = $outStr
        timestamp   = (Get-Date -Format "o")
    }
    Write-ReceiptNoBom -Path $receiptOut -Object $missing
    exit 1
}

# 读 receipt：-Encoding utf8 修 GBK；断言 nonce 与本轮一致。
$receipt = Get-FreshReceipt -Path $receiptOut -Nonce $nonce
Write-Host "STATUS: $($receipt.status)"
# FAIL-closed (GPT 第二轮审核指出)：spike 只有在 PASS 时才允许 wrapper exit 0。
# BLOCKED_UNCERTIFIED 在 Phase 0 sandbox 是因为缺独立小卷（disk-full 必选子项），
# 但 wrapper 把 BLOCKED 当 exit 0 等于把"未认证"伪装成"已认证"，掩盖真实的
# ENOSPC 验证缺失。统一三态退出码由 _common.ps1 的 Exit-ByReceiptStatus 提供。
exit (Exit-ByReceiptStatus -Status $receipt.status)