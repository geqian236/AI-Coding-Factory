<#
.SYNOPSIS  Spike 6: Git Object Bridge probe
.DESCRIPTION  bundle/import/worktree roundtrip; verifies zero user workspace pollution

  P0-5 修复（防 stale-PASS 掩盖本轮 FAIL）：
    - 跑子进程**前**强制 Remove-StaleReceipt 删除旧 receipt
    - 生成 nonce 注入到本轮 receipt（git probe.py 不接受 --nonce，由 wrapper 在子
      进程退出后从磁盘读 receipt、补 run_nonce 字段、用 Write-ReceiptNoBom 重写）
    - 读 receipt 用 -Encoding utf8 修 GBK 解码
    - 子进程退出码非 0 → 写 FAIL receipt + exit 1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\git_object_bridge"
$receiptPath = "$probeDir\receipt.json"
$common      = "$PSScriptRoot\_common.ps1"

# P0-5：抽公共 helper
. $common

# 本轮 nonce：注入到 receipt，wrapper 读回时断言一致。
$nonce = [guid]::NewGuid().ToString("N")

$py = $null
foreach ($c in @("python","py","python3")) {
    try { & $c --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

# P0-5：跑子进程前删旧 receipt。
Remove-StaleReceipt -Path $receiptPath

Write-Host "Running Git Object Bridge probe with $py ..."
# P0-5：用 try/catch 捕获子进程调用异常（如 probe.py 不存在），不让异常吞掉 FAIL receipt 写盘。
try {
    $out = & $py "$probeDir\probe.py" 2>&1
    $ec  = $LASTEXITCODE
    $out -join "`n" | Write-Host
} catch {
    $ec = -1
    $out = @($_.ToString())
    Write-Host "subprocess raised: $($out -join `"`n`")"
}

# 子进程退出码非 0 → 写 FAIL receipt + exit 1（旧 PASS 不再掩盖本轮 FAIL）
if ($ec -ne 0) {
    $failReceipt = [ordered]@{
        spike       = "git_object_bridge"
        status      = "FAIL"
        error       = "probe.py 子进程退出码非 0（$ec），旧 PASS 不再掩盖本轮 FAIL"
        exit_code   = $ec
        run_nonce   = $nonce
        probe_stdout = ($out -join "`n")
        timestamp   = (Get-Date -Format "o")
    }
    Write-ReceiptNoBom -Path $receiptPath -Object $failReceipt
    exit 1
}

# 再校验本轮 receipt 是否存在：保险起见，子进程 exit 0 但未写盘的情况
if (-not (Test-Path $receiptPath)) {
    $missing = [ordered]@{
        spike        = "git_object_bridge"
        status       = "ERROR"
        error        = "probe.py exited 0 but no receipt.json produced"
        run_nonce    = $nonce
        probe_stdout = ($out -join "`n")
        timestamp    = (Get-Date -Format "o")
    }
    Write-ReceiptNoBom -Path $receiptPath -Object $missing
    exit 1
}

# 注入 nonce：git probe.py 不接受 --nonce，由 wrapper 读回 receipt、补字段、无 BOM 重写
$raw = Get-Content $receiptPath -Raw -Encoding utf8
$obj = $raw | ConvertFrom-Json
$obj | Add-Member -NotePropertyName run_nonce -NotePropertyValue $nonce -Force
Write-ReceiptNoBom -Path $receiptPath -Object $obj

# 读 receipt（带 nonce 断言）
$r = Get-FreshReceipt -Path $receiptPath -Nonce $nonce
Write-Host "STATUS: $($r.status)"
if ($r.status -ne "PASS") { exit 1 }
# 显式 exit 0：spike 通过时必须设置 $LASTEXITCODE，否则 test.ps1 的 Run-Suite
# 在 StrictMode 下读取未定义的 $LASTEXITCODE 会抛异常并中断整个套件。
exit 0