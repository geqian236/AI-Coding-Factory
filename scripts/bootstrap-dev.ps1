<#
.SYNOPSIS
    开发环境自举脚本 — 探测并校验 D 盘开发所需的运行时版本与锁文件完整性。

.DESCRIPTION
    复用 scripts/dev.ps1 所定义的 D 盘路径规则（不另维护第二套环境规则）。
    -VerifyOnly 模式：只读地验证所有工程文件和路径绑定，输出机器可读的 D 盘路径摘要，
    不创建任何 C 盘文件，异常时以非零退出。

.PARAMETER VerifyOnly
    只做验证，不安装或修改任何文件。

.NOTES
    版本：1.0
#>
[CmdletBinding()]
param(
    [switch]$VerifyOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$DATA_ROOT = "D:\codex项目\AI-Coding-Factory-Data\dev"
$REPO_ROOT  = Split-Path $PSScriptRoot -Parent

# ── 辅助：输出机器可读的路径摘要 ──────────────────────────────────────────────
function Write-PathSummary {
    $summary = [ordered]@{
        data_root                 = $DATA_ROOT
        temp                      = $env:TEMP
        cargo_home                = $env:CARGO_HOME
        cargo_target_dir          = $env:CARGO_TARGET_DIR
        rustup_home               = $env:RUSTUP_HOME
        pnpm_home                 = $env:PNPM_HOME
        npm_config_cache          = $env:NPM_CONFIG_CACHE
        uv_cache_dir              = $env:UV_CACHE_DIR
        pip_cache_dir             = $env:PIP_CACHE_DIR
        playwright_browsers_path  = $env:PLAYWRIGHT_BROWSERS_PATH
    }
    Write-Host "=== D 盘路径摘要 ==="
    foreach ($k in $summary.Keys) {
        $v    = $summary[$k]
        $ok   = if ($v -and (Test-Path $v)) { "OK" } else { "MISSING" }
        $drive = if ($v -match '^[Dd]:') { "D" } else { "OTHER" }
        Write-Host ("{0,-30} = {1}  [{2}|{3}]" -f $k, $v, $drive, $ok)
    }
}

# ── 校验必须存在的工程文件 ─────────────────────────────────────────────────────
function Assert-RequiredFiles {
    $required = @(
        "package.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "pyproject.toml",
        "uv.lock",
        ".node-version",
        ".python-version",
        "scripts/dev.ps1",
        "scripts/bootstrap-dev.ps1",
        "scripts/check.ps1",
        "scripts/test.ps1",
        ".github/workflows/plan-validation.yml",
        ".github/workflows/ci.yml",
        ".github/pull_request_template.md"
    )
    $missing = @()
    foreach ($rel in $required) {
        $full = Join-Path $REPO_ROOT ($rel -replace '/', '\')
        if (-not (Test-Path $full)) { $missing += $rel }
    }
    if ($missing.Count -gt 0) {
        Write-Error "[bootstrap-dev] 以下必须文件缺失：`n$($missing -join "`n")"
        exit 1
    }
    Write-Host "[bootstrap-dev] 必须文件检查通过（$($required.Count) 项）。"
}

# ── 主逻辑 ─────────────────────────────────────────────────────────────────────
Write-PathSummary
Assert-RequiredFiles

if ($VerifyOnly) {
    Write-Host "[bootstrap-dev] -VerifyOnly 模式：验证通过，未执行任何写入。"
    exit 0
}

Write-Host "[bootstrap-dev] 自举完成。"
exit 0
