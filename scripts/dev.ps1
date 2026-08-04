<#
.SYNOPSIS
    D 盘开发环境安全包装器 — 在执行任何开发工具前将所有缓存/工具链路径绑定到 D 盘，
    并在子进程退出后验证不产生受控 C 盘写入。

.DESCRIPTION
    将 TEMP/TMP/PNPM_HOME/NPM_CONFIG_CACHE/UV_CACHE_DIR/PIP_CACHE_DIR/
    PLAYWRIGHT_BROWSERS_PATH 重定向到 D:\codex项目\AI-Coding-Factory-Data\dev；
    Rust 三件套 CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME 单独重定向到纯 ASCII 根
    D:\acf-dev（原因见 $RUST_ROOT 注释：GBK 代码页下 `ld` 无法打开含 CJK 的路径）。
    所有路径解析为物理路径后校验：不落在 D 盘（含符号链接/联接点最终落在 C 盘）
    时立即以非零退出；Rust 三件套额外要求纯 ASCII。cargo-home\bin 前置到 PATH。
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

# Rust 专用根目录：必须是纯 ASCII 路径。
# 原因：中文 Windows（GBK ANSI 代码页）下，MinGW binutils 的 `ld` 用窄字符 API
# 打开文件，无法解析含 CJK 字符的路径；若 rustup-home/cargo-target 落在
# $DATA_ROOT（含“codex项目”）下，std 的 rlib 与 self-contained 系统库会以 CJK
# 绝对路径传给 `ld`，链接阶段全部 "cannot find"，导致任何 Rust 构建失败。
# 故将 RUSTUP_HOME/CARGO_HOME/CARGO_TARGET_DIR 单独绑定到此 ASCII 根，
# 其余缓存（pnpm/npm/uv/pip/playwright）不受该限制，仍留在 $DATA_ROOT。
$RUST_ROOT = "D:\acf-dev"

# 确保数据根目录存在（非 Rust 缓存仍在 CJK DATA_ROOT 下）。
$subDirs = @(
    "tmp", "pnpm-home", "npm-cache", "uv-cache", "pip-cache", "playwright"
)
foreach ($d in $subDirs) {
    $path = Join-Path $DATA_ROOT $d
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Force $path | Out-Null
    }
}

# 确保 Rust ASCII 根及其子目录存在。
$rustSubDirs = @("cargo-home", "cargo-target", "rustup-home")
foreach ($d in $rustSubDirs) {
    $path = Join-Path $RUST_ROOT $d
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

# 验证路径为纯 ASCII，否则 fail-closed。
# Rust 工具链/构建产物路径若含非 ASCII 字符，GBK 代码页下 `ld` 会链接失败，
# 且报错信息隐晦（大量 "cannot find"）。此处提前拦截，避免误配 $RUST_ROOT 后
# 出现难以定位的链接错误。
function Assert-Ascii {
    param([string]$Label, [string]$Path)
    if ($Path -match '[^\x00-\x7F]') {
        Write-Error "[dev.ps1] 路径安全检查失败：$Label 的路径 '$Path' 含非 ASCII 字符，GBK 代码页下 Rust 链接会失败，拒绝执行。"
        exit 1
    }
}

# ── 3. 绑定所有环境变量到 D 盘 ────────────────────────────────────────────────
$env:TEMP                     = Join-Path $DATA_ROOT "tmp"
$env:TMP                      = $env:TEMP
# Rust 三件套绑定到 ASCII 根，规避 GBK 代码页下 `ld` 无法打开 CJK 路径的链接失败。
$env:CARGO_HOME               = Join-Path $RUST_ROOT "cargo-home"
$env:CARGO_TARGET_DIR         = Join-Path $RUST_ROOT "cargo-target"
$env:RUSTUP_HOME              = Join-Path $RUST_ROOT "rustup-home"
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

# Rust 三件套额外做纯 ASCII 校验：GBK 代码页下含 CJK 的路径会导致 `ld` 链接失败。
Assert-Ascii "CARGO_HOME"       $env:CARGO_HOME
Assert-Ascii "CARGO_TARGET_DIR" $env:CARGO_TARGET_DIR
Assert-Ascii "RUSTUP_HOME"      $env:RUSTUP_HOME

# 将 cargo-home\bin 前置到 PATH，使子进程能找到搬迁到 ASCII 根后的 cargo/rustc。
# 前置而非追加：确保用本 wrapper 绑定的工具链，而非父环境里可能存在的其它 cargo。
$cargoBin = Join-Path $env:CARGO_HOME "bin"
$env:PATH = "$cargoBin;$env:PATH"

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
