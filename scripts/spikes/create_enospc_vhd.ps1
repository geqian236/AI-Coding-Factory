<#
.SYNOPSIS
    创建 ≤16 MiB VHD 并挂载到 ASCII 路径，用于 sqlite_wal_full spike 真实认证 ENOSPC。

.DESCRIPTION
    GPT 第二轮审核要求 sqlite spike 在真实 ENOSPC 路径下认证：
    disk-full 是 §10.4 + catalog STORE-001 的必选子项，Phase 0 sandbox
    无独立小容量卷无法可靠触发。本脚本是解封路径——在管理员权限下：

    1. 创建 VHD（≤16 MiB）
    2. 初始化 NTFS + 挂载到 D:\vhd_enospc（ASCII 路径）
    3. 输出挂载路径给 sqlite spike 用

    必须在 PowerShell 提升上下文（管理员）中运行：
        Start-Process powershell -Verb RunAs -ArgumentList @(
            "-NoProfile","-ExecutionPolicy","Bypass",
            "-File","scripts/spikes/create_enospc_vhd.ps1"
        )

    卸除：
        scripts/spikes/create_enospc_vhd.ps1 -Unmount

.NOTES
    路径要求 ASCII：VHD 路径与挂载点路径都不能含中文（GBK 代码页下
    含 CJK 的路径会让 sqlite / diskpart API 行为异常）。
#>
[CmdletBinding()]
param(
    [switch] $Unmount,
    [string] $VhdPath = "D:\vhd_enospc.vhdx",
    [string] $MountPoint = "D:\vhd_enospc",
    [int]    $SizeMB = 16
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 必须 ASCII 路径（防止 GBK 代码页 bug）
function Assert-AsciiPath {
    param([string]$Path, [string]$Label)
    if ($Path -match '[^\x00-\x7F]') {
        Write-Error "[$Label] 路径含非 ASCII 字符（GBK 代码页下 diskpart / sqlite 行为不可靠）：$Path"
        exit 1
    }
}

Assert-AsciiPath $VhdPath "VhdPath"
Assert-AsciiPath $MountPoint "MountPoint"

# 必须管理员
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "[vhd] 需要管理员权限。请以 RunAs 提升上下文运行。"
    exit 1
}

function Dismount-VHD {
    # 先 unmount 文件系统卷
    $vol = Get-Volume -FilePath $MountPoint -ErrorAction SilentlyContinue
    if ($vol) {
        Write-Host "[vhd] Dismount-VHD on $MountPoint"
        Dismount-DiskImage -ImagePath $VhdPath -ErrorAction SilentlyContinue
    }
    if (Test-Path $MountPoint) {
        # 卸下挂载点（如果是空的盘符）
        $driveLetter = (Get-Disk | Where-Object { $_.Location -like "*$($VhdPath)*" } | Get-Partition | Get-Volume).DriveLetter
        if ($driveLetter) {
            Write-Host "[vhd] Removing drive letter $driveLetter"
            Remove-PartitionAccessPath -DriveLetter $driveLetter -AccessPath $MountPoint -ErrorAction SilentlyContinue
        }
    }
}

if ($Unmount) {
    Dismount-VHD
    Write-Host "[vhd] 卸除完成：VHD=$VhdPath Mount=$MountPoint"
    exit 0
}

# ── 创建 / 附加 VHD ──────────────────────────────────────────
if (-not (Test-Path $VhdPath)) {
    Write-Host "[vhd] 创建 $VhdPath ($SizeMB MiB)"
    New-VHD -Path $VhdPath -SizeBytes ($SizeMB * 1MB) -Dynamic -Confirm:$false
}

Write-Host "[vhd] Mount-DiskImage $VhdPath"
Mount-DiskImage -ImagePath $VhdPath

# 初始化 NTFS（如果是 raw VHD）
$disk = Get-DiskImage -ImagePath $VhdPath | Get-Disk
if (-not (Get-Partition -Disk $disk -ErrorAction SilentlyContinue)) {
    Write-Host "[vhd] 初始化分区与 NTFS"
    $part = New-Partition -Disk $disk -UseMaximumSize -AssignDriveLetter
    Format-Volume -DriveLetter $part.DriveLetter -FileSystem NTFS -Confirm:$false -Force
}

# 挂载到 MountPoint
$part = Get-Partition -DiskNumber $disk.Number
if ($part) {
    Add-PartitionAccessPath -DiskNumber $disk.Number -PartitionNumber $part.PartitionNumber -AccessPath $MountPoint
}

# 确保挂载点目录存在
if (-not (Test-Path $MountPoint)) {
    New-Item -ItemType Directory -Path $MountPoint -Force | Out-Null
}

# 报告
$vol = Get-Volume -FilePath $MountPoint -ErrorAction SilentlyContinue
Write-Host "[vhd] 挂载成功：$MountPoint"
Write-Host "[vhd] 卷剩余：$((Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -eq "$MountPoint\" }).Free) bytes"
Write-Host ""
Write-Host "[vhd] 现在可以运行 sqlite spike："
Write-Host "  python tools/compat-probes/sqlite_wal_full/bench.py \\"
Write-Host "      --probe-dir ${MountPoint}\crash_probe.db \\"
Write-Host "      --emit-receipt tools/compat-probes/sqlite_wal_full/receipt.json"