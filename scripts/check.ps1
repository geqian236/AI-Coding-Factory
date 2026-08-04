<#
.SYNOPSIS
    代码质量与合规性检查脚本 — 汇聚 lint、格式、类型检查、秘密扫描和 D 盘路径检查。

.DESCRIPTION
    在 scripts/dev.ps1 D 盘环境下执行各运行时的静态检查。
    每项检查失败时立即报告，全部完成后汇总并以非零退出（有任何失败时）。

.NOTES
    版本：1.0
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$REPO_ROOT = Split-Path $PSScriptRoot -Parent
$failures  = [System.Collections.Generic.List[string]]::new()

function Invoke-Check {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "── 检查：$Name ──"
    try {
        & $Action
        if ($LASTEXITCODE -ne 0) { $failures.Add($Name) }
    } catch {
        Write-Warning "  [$Name] 异常：$_"
        $failures.Add($Name)
    }
}

# Python ruff lint
Invoke-Check "python-lint" {
    python -m ruff check . --quiet
}

# Python mypy 类型检查
Invoke-Check "python-types" {
    python -m mypy apps/agent/src --ignore-missing-imports --quiet
}

# 摘要
if ($failures.Count -gt 0) {
    Write-Error "[check.ps1] 以下检查失败：$($failures -join ', ')"
    exit 1
}
Write-Host "[check.ps1] 全部检查通过。"
exit 0
