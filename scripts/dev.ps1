<#
.SYNOPSIS
    D 盘开发环境安全包装器 — 在执行任何开发工具前将所有缓存/工具链路径绑定到
    唯一允许的项目根 D:\codex项目 之下，并对每个路径做物理前缀校验。

.DESCRIPTION
    将 TEMP/TMP/CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME/PNPM_HOME/NPM_CONFIG_CACHE/
    UV_CACHE_DIR/PIP_CACHE_DIR/PLAYWRIGHT_BROWSERS_PATH 全部重定向到
    D:\codex项目\AI-Coding-Factory-Data\dev（存储合同：一切产物只允许落在 D:\codex项目）。
    路径解析为物理路径后校验：最终目标不在 D:\codex项目 前缀下（含 symlink/junction
    最终逃逸）时立即以非零退出。

    Rust 链接器：项目根 D:\codex项目 含 CJK 字符，而中文 Windows（GBK ANSI 代码页）
    下 MinGW binutils 的 `ld` 用窄字符 API 打开文件，无法解析含 CJK 的路径，链接阶段
    全部 "cannot find"。改用 Rust 自带、Unicode 安全的 lld：committed .cargo/config.toml
    对 gnu target 加 `-C link-arg=-fuse-ld=lld`，本 wrapper 再把工具链自带的 gcc-ld
    目录前置到 PATH，使 gcc 驱动能找到 `ld.lld`。这样工具链/产物可全部合规落在 D:\codex项目。

    子进程以 -- 后的参数数组启动；子进程退出非零时 wrapper 同样返回非零。

.PARAMETER args
    -- 之后的全部参数，原样传给子进程。

.NOTES
    版本：2.0  |  不依赖父终端环境，所有路径均在此脚本内强制设定。
#>
# 注意：本脚本刻意不声明 [CmdletBinding()] 或 param() 块。
# PowerShell 的 advanced script 会尝试把 `--` 当作参数名绑定并报
# AmbiguousParameter，且 advanced script 不填充 $args；只有普通脚本
# 才能让 `--` 原样进入 $args，这是本 wrapper 的调用协议所必需的。
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 1. 唯一允许的项目根与数据根目录 ────────────────────────────────────────────
# 存储合同：所有缓存/工具链/产物只允许落在 D:\codex项目 之下，不得外溢到别处。
$PROJECT_ROOT = "D:\codex项目"
$DATA_ROOT    = Join-Path $PROJECT_ROOT "AI-Coding-Factory-Data\dev"

# 确保数据根目录及全部子目录存在（Rust 三件套亦在 DATA_ROOT 下，随项目根合规）。
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
# 解析路径的最终物理目标（跟随 symlink/junction），用于验证是否仍在项目根下。
function Resolve-PhysicalPath {
    param([string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { return [System.IO.Path]::GetFullPath($Path) }
        # 对目录型 reparse point 尝试解析真实路径（防 junction/symlink 逃逸）。
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

# 验证路径的物理目标是否落在 D:\codex项目 前缀下，否则 fail-closed。
# 仅校验盘符不够：D:\acf-dev 同在 D 盘却违反存储合同，必须校验完整前缀。
function Assert-UnderProjectRoot {
    param([string]$Label, [string]$Path)
    $physical = Resolve-PhysicalPath $Path
    $rootFull = [System.IO.Path]::GetFullPath($PROJECT_ROOT).TrimEnd('\')
    # 规范化后必须等于根或以“根\”开头，避免 D:\codex项目-evil 之类前缀伪装。
    $normalized = $physical.TrimEnd('\')
    if ($normalized -ne $rootFull -and -not $normalized.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        Write-Error "[dev.ps1] 存储合同校验失败：$Label 的物理路径 '$physical' 不在项目根 '$PROJECT_ROOT' 之下，拒绝执行。"
        exit 1
    }
}

# ── 3. 绑定所有环境变量到项目根下的 DATA_ROOT ──────────────────────────────────
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

# ── 4. 执行前路径物理验证（全部必须在 D:\codex项目 之下）──────────────────────
Assert-UnderProjectRoot "TEMP"                     $env:TEMP
Assert-UnderProjectRoot "CARGO_HOME"               $env:CARGO_HOME
Assert-UnderProjectRoot "CARGO_TARGET_DIR"         $env:CARGO_TARGET_DIR
Assert-UnderProjectRoot "RUSTUP_HOME"              $env:RUSTUP_HOME
Assert-UnderProjectRoot "PNPM_HOME"                $env:PNPM_HOME
Assert-UnderProjectRoot "NPM_CONFIG_CACHE"         $env:NPM_CONFIG_CACHE
Assert-UnderProjectRoot "UV_CACHE_DIR"             $env:UV_CACHE_DIR
Assert-UnderProjectRoot "PIP_CACHE_DIR"            $env:PIP_CACHE_DIR
Assert-UnderProjectRoot "PLAYWRIGHT_BROWSERS_PATH" $env:PLAYWRIGHT_BROWSERS_PATH

# 将 cargo-home\bin 前置到 PATH，使子进程找到项目根下的 cargo/rustc。
# 前置而非追加：确保用本 wrapper 绑定的工具链，而非父环境里可能存在的其它 cargo。
$cargoBin = Join-Path $env:CARGO_HOME "bin"
$env:PATH = "$cargoBin;$env:PATH"

# Rust 工具链强制为 gnu：仓库根 rust-toolchain.toml 写 `channel = "stable"`（无 host
# 后缀），rustup 在本机（Default host = x86_64-pc-windows-msvc）会解析成
# stable-x86_64-pc-windows-msvc，使 build-script（proc-macro2/serde 等）为 msvc host
# 编译并要求 link.exe（本机未装 MSVC C++ build tools）而失败。设 RUSTUP_TOOLCHAIN 让
# 所有经本 wrapper 的 cargo/rustc（含子 wrapper 里的 cargo build/run/test）都强制走
# gnu 工具链，host 也是 gnu，build-script 用 ld.lld，全程不碰 msvc link.exe。
$env:RUSTUP_TOOLCHAIN = "stable-x86_64-pc-windows-gnu"

# Rust 链接器 lld 支持：直接把工具链自带、Unicode 安全的 rust-lld（ld flavor）设为链接器。
# 实测 `-fuse-ld=lld` 经 gcc 驱动仍回退到 GNU ld、在 CJK 路径失败；故绕过 gcc，
# 用 CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER 直接指向 ld.lld.exe，与 committed
# .cargo/config.toml 的 `linker-flavor=ld` 配合。
#
# ld.lld 路径**不经 `rustc --print sysroot` 捕获**：PowerShell 5.1 在 GBK 代码页下
# 捕获 native 命令 stdout 时会破坏 CJK 字节（$env:RUSTUP_HOME 含 "codex项目"），
# 使 Test-Path 在损坏路径上失败、linker 不被设置、cargo 回退 GNU ld 并在 CJK 路径挂。
# 改为从**字面量 $env:RUSTUP_HOME**（本脚本第 3 节已 Join-Path 设定，未经任何管道捕获）
# 直接拼接工具链内的 gcc-ld\ld.lld.exe，CJK 字节全程不经 native stdout，保持完整。
$gnuToolchain = Join-Path $env:RUSTUP_HOME "toolchains\stable-x86_64-pc-windows-gnu"
$ldLld = Join-Path $gnuToolchain "lib\rustlib\x86_64-pc-windows-gnu\bin\gcc-ld\ld.lld.exe"
if (Test-Path -LiteralPath $ldLld) {
    $env:CARGO_TARGET_X86_64_PC_WINDOWS_GNU_LINKER = $ldLld
} else {
    Write-Warning "[dev.ps1] 未找到 ld.lld '$ldLld'，Rust 链接会回退到 GNU ld 并在 CJK 路径失败。"
}

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
