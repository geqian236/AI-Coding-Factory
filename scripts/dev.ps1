<#
.SYNOPSIS
    D 盘开发环境安全包装器 — 在执行任何开发工具前将所有缓存/工具链路径绑定到 D 盘，
    并在子进程退出后验证不产生受控 C 盘写入。

.DESCRIPTION
    将 TEMP/TMP/CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME/PNPM_HOME/NPM_CONFIG_CACHE/
    UV_CACHE_DIR/PIP_CACHE_DIR/PLAYWRIGHT_BROWSERS_PATH 全部解析为物理路径并重定向到
    D:\codex项目\AI-Coding-Factory-Data\dev 下对应子目录。
    路径不可信（指向 C 盘或符号链接/联接点最终落在 C 盘）时立即以非零退出。
    子进程以 -- 后的参数数组启动；子进程退出非零时 wrapper 同样返回非零。

.PARAMETER args
    -- 之后的全部参数，原样传给子进程。

.NOTES
    版本：1.0  |  不依赖父终端环境，所有路径均在此脚本内强制设定。
#>
# 注意：本脚本刻意不声明 [CmdletBinding()] 或 param() 块。
# PowerShell 的 advanced script 会尝试把 `--` 当作参数名绑定并报
# AmbiguousParameter，且 advanced script 不填充 $args；只有普通脚本
# 才能让 `--` 原样进入 $args，这是本 wrapper 的调用协议所必需的。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 1. D 盘数据根目录 ──────────────────────────────────────────────────────────
$DATA_ROOT = "D:\codex项目\AI-Coding-Factory-Data\dev"

# 确保数据根目录存在
$subDirs = @(
    "tmp", "cargo-home", "cargo-target", "rustup-home",
    "pnpm-home", "npm-cache", "uv-cache", "pip-cache", "playwright"
)
foreach ($d in $subDirs) {
    $path = Join-Path $DATA_ROOT $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Force $path | Out-Null
    }
}

# ── 2. 物理路径解析辅助函数 ────────────────────────────────────────────────────
# 解析路径的最终物理目标（跟随 symlink/junction），用于验证是否落在 D 盘。
function Resolve-PhysicalDrive {
    param([string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { return $Path }
        # 对目录型 reparse point 尝试解析真实路径
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            $target = [System.IO.Path]::GetFullPath(
                [System.IO.Directory]::ResolveLinkTarget($Path, $true).FullName
            )
            return $target
        }
        return [System.IO.Path]::GetFullPath($Path)
    } catch {
        return $Path
    }
}

# 验证路径是否物理落在 D 盘（D:\ 开头），否则 fail-closed。
function Assert-OnDDrive {
    param([string]$Label, [string]$Path)
    $physical = Resolve-PhysicalDrive $Path
    if (-not ($physical -match '^[Dd]:[/\\]')) {
        Write-Error "[dev.ps1] 路径安全检查失败：$Label 的物理路径 '$physical' 不在 D 盘，拒绝执行。"
        exit 1
    }
}

# ── 3. 绑定所有环境变量到 D 盘 ────────────────────────────────────────────────
$env:TEMP                     = Join-Path $DATA_ROOT "tmp"
$env:TMP                      = $env:TEMP
$env:CARGO_HOME               = Join-Path $DATA_ROOT "cargo-home"
$env:CARGO_TARGET_DIR         = Join-Path $DATA_ROOT "cargo-target"
$env:RUSTUP_HOME              = Join-Path $DATA_ROOT "rustup-home"
$env:PNPM_HOME                = Join-Path $DATA_ROOT "pnpm-home"
$env:NPM_CONFIG_CACHE         = Join-Path $DATA_ROOT "npm-cache"
$env:UV_CACHE_DIR             = Join-Path $DATA_ROOT "uv-cache"
$env:PIP_CACHE_DIR            = Join-Path $DATA_ROOT "pip-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $DATA_ROOT "playwright"

# ── 4. 执行前路径物理验证 ──────────────────────────────────────────────────────
Assert-OnDDrive "TEMP"                     $env:TEMP
Assert-OnDDrive "CARGO_HOME"               $env:CARGO_HOME
Assert-OnDDrive "CARGO_TARGET_DIR"         $env:CARGO_TARGET_DIR
Assert-OnDDrive "RUSTUP_HOME"              $env:RUSTUP_HOME
Assert-OnDDrive "PNPM_HOME"               $env:PNPM_HOME
Assert-OnDDrive "NPM_CONFIG_CACHE"         $env:NPM_CONFIG_CACHE
Assert-OnDDrive "UV_CACHE_DIR"             $env:UV_CACHE_DIR
Assert-OnDDrive "PIP_CACHE_DIR"            $env:PIP_CACHE_DIR
Assert-OnDDrive "PLAYWRIGHT_BROWSERS_PATH" $env:PLAYWRIGHT_BROWSERS_PATH

# ── 5. 解析 -- 分隔符，提取子命令 ─────────────────────────────────────────────
$rawArgs = $args
$sepIdx  = [Array]::IndexOf($rawArgs, '--')

if ($sepIdx -lt 0) {
    Write-Error "[dev.ps1] 用法：dev.ps1 [选项] -- <命令> [参数...]"
    exit 1
}

# 必须先判断 -- 之后是否还有元素，再做切片。
# PowerShell 的 a..b 在 a > b 时是降序范围，$rawArgs[N..(N-1)] 会返回
# 反转后的非空数组，使 "Count -eq 0" 守卫永远不触发并执行到垃圾命令。
if ($sepIdx -ge ($rawArgs.Length - 1)) {
    Write-Error "[dev.ps1] -- 后未提供任何子命令。"
    exit 1
}

$subCmd = @($rawArgs[($sepIdx + 1)..($rawArgs.Length - 1)])

# ── 6. 启动子进程并捕获退出码 ──────────────────────────────────────────────────
$exe  = $subCmd[0]
$rest = if ($subCmd.Count -gt 1) { $subCmd[1..($subCmd.Count - 1)] } else { @() }

& $exe @rest
$exitCode = $LASTEXITCODE

if ($exitCode -ne 0) {
    Write-Warning "[dev.ps1] 子进程 '$exe' 退出码：$exitCode"
    exit $exitCode
}

exit 0
