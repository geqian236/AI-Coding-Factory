<#
.SYNOPSIS  Spike wrapper 公共 helper（P0-5 抽出）
.DESCRIPTION
    集中三个被多 wrapper 复用的功能（修 stale-PASS + UTF-8/BOM）：
      - Write-ReceiptNoBom  无 BOM UTF-8 写 receipt（PowerShell 5.1 Out-File utf8 会写 BOM）
      - Remove-StaleReceipt  跑子进程前强制删旧 receipt，避免上一轮 PASS 掩盖本轮 FAIL
      - Get-FreshReceipt     修 GBK 解码：显式 -Encoding utf8 读回并断言 nonce

    原先 _common 的能力散布在 test-durable-io.ps1:37-40 / test-runner-identity.ps1:40-43，
    但只在 Rust 子进程路径上完整，无 nonce、无 stale 删除；P0-5 把这些拼成统一 helper，
    供 test-sqlite-wal.ps1 与 test-git-bridge.ps1（P0-5 主目标）使用，并向下兼容
    test-named-pipe.ps1 / test-durable-io.ps1 / test-runner-identity.ps1 等复用点。
#>
Set-StrictMode -Version Latest

# 无 BOM UTF-8 写 receipt：PowerShell 5.1 的 Out-File -Encoding utf8 会写 BOM,
# 下游 Python json.load 会报 "Unexpected UTF-8 BOM"。故用 .NET WriteAllText + UTF8Encoding($false)。
function Write-ReceiptNoBom {
    param(
        [Parameter(Mandatory=$true)] [string] $Path,
        [Parameter(Mandatory=$true)] [object] $Object
    )
    $json = $Object | ConvertTo-Json -Depth 12
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    [System.IO.File]::WriteAllText(
        $Path, $json, (New-Object System.Text.UTF8Encoding($false))
    )
}

# 跑子进程前强制删除旧 receipt：本轮若子进程崩溃不写 receipt，旧 PASS 就会
# 掩盖本轮 FAIL。删除后再跑，确保读回的是本轮产物。幂等：不存在不报错。
function Remove-StaleReceipt {
    param(
        [Parameter(Mandatory=$true)] [string] $Path
    )
    if (Test-Path $Path) {
        Remove-Item $Path -Force
    }
}

# 读 receipt：-Encoding utf8 显式指定防止 GBK 解码（PowerShell 默认按系统 ANSI）。
# 同时断言 run_nonce 与本轮生成的 nonce 一致，避免读到陈旧文件。
function Get-FreshReceipt {
    param(
        [Parameter(Mandatory=$true)] [string] $Path,
        [Parameter(Mandatory=$true)] [string] $Nonce,
        [switch] $RequireNonce
    )
    if (-not (Test-Path $Path)) {
        throw "RECEIPT_MISSING: 本轮未生成 receipt: $Path（子进程崩？或未跑？）"
    }
    $raw = Get-Content $Path -Raw -Encoding utf8
    $obj = $raw | ConvertFrom-Json
    if ($RequireNonce -or $obj.PSObject.Properties.Match('run_nonce').Count -gt 0) {
        if (-not ($obj.PSObject.Properties.Match('run_nonce').Count -gt 0)) {
            throw "RECEIPT_NONCE_MISSING: receipt 缺 run_nonce 字段"
        }
        if ($obj.run_nonce -ne $Nonce) {
            throw "RECEIPT_NONCE_MISMATCH: receipt.run_nonce=$($obj.run_nonce) != 本轮 nonce=$Nonce（陈旧文件未被删？）"
        }
    }
    return $obj
}