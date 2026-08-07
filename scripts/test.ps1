<#
.SYNOPSIS
    测试运行脚本 — 按 Suite 名称分派到对应的测试运行器。

.DESCRIPTION
    封装 pytest / pnpm vitest / cargo test，统一入口，支持 -EmitReceipts 开关。

.PARAMETER Suite
    测试套件名称：contract-plan-hash | contract-event-hash | compatibility-spikes | all

.PARAMETER EmitReceipts
    生成机器可读的测试回执（用于 Task 6/7）。

.NOTES
    版本：1.0
#>
[CmdletBinding()]
param(
    [string]$Suite = "all",
    [switch]$EmitReceipts,
    # GPT 第十轮 P1：调用方级回归测试用。设置并打印 per-worktree CARGO_TARGET_DIR 后
    # 立即退出（不跑任何 cargo/pytest），使 test_cargo_target_isolation.py 能廉价断言
    # test.ps1 这个正式入口确实覆盖了继承的（可能是陈旧共享的）CARGO_TARGET_DIR。
    [switch]$PrintTargetDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$REPO_ROOT = Split-Path $PSScriptRoot -Parent
$exitCode  = 0

# GPT 第十轮 P1：test.ps1 是跑 cargo test 的正式入口之一，但过去从不设置
# CARGO_TARGET_DIR，直接继承进程环境。若父进程残留旧版**共享** cargo-target，
# 本脚本的 cargo test 会跑到另一 worktree 编译的二进制并读其 golden（跨 worktree
# 污染，与第九轮 P0 同一根因）。此处按本 worktree 派生 per-worktree namespace 并
# **强制**设定 CARGO_TARGET_DIR，与 dev.ps1 / phase0-acceptance.ps1 / check.ps1
# 同一 Get-WorktreeTargetDir（$REPO_ROOT = 本脚本目录上一级 = 本 worktree 根）。
# 仅当能定位到项目根（AI-Coding-Factory-Data 祖先）时设定，找不到即 fail-closed，
# 绝不静默沿用可疑的继承值。
. (Join-Path $PSScriptRoot "_worktree-target.ps1")

# GPT 第十一轮 P1：调用方级回归（test_cargo_target_isolation.py）在 GitHub Windows
# runner 上 checkout 于 D:\a\... —— 无 AI-Coding-Factory-Data 祖先，旧逻辑会 fail-closed，
# 令关键的"test.ps1 覆盖继承 CARGO_TARGET_DIR"断言在 CI 静默不跑。为让 CI 真实执行该
# 断言，提供**仅测试用**的受控 DataRoot 合同：环境变量 FACTORY_TEST_DATA_ROOT 仅在
# -PrintTargetDir（纯打印、绝不跑 cargo/pytest）时被采纳，用它作 DataRoot 派生 namespace
# 并跳过项目根 walk-up。正式跑测试（无 -PrintTargetDir）永不采纳该覆盖，仍必须定位真实
# 项目根，故它不能成为绕过存储合同的通道。
if ($PrintTargetDir -and $env:FACTORY_TEST_DATA_ROOT) {
    $_dataRoot = $env:FACTORY_TEST_DATA_ROOT
    Write-Host "[test.ps1] (-PrintTargetDir) 受控 DataRoot = $_dataRoot"
} else {
    $_projRoot = $null
    $_probe = $REPO_ROOT
    while ($_probe) {
        if (Test-Path (Join-Path $_probe "AI-Coding-Factory-Data")) { $_projRoot = $_probe; break }
        $_parent = Split-Path $_probe -Parent
        if (-not $_parent -or $_parent -eq $_probe) { break }
        $_probe = $_parent
    }
    if (-not $_projRoot) {
        Write-Error "[test.ps1] fail-closed: 找不到 AI-Coding-Factory-Data 项目根（$REPO_ROOT 的任何祖先），拒绝在未隔离的 CARGO_TARGET_DIR 下跑 cargo test"
        exit 1
    }
    $_dataRoot = Join-Path $_projRoot "AI-Coding-Factory-Data\dev"
}
$env:CARGO_TARGET_DIR = Get-WorktreeTargetDir -WorktreeRoot $REPO_ROOT -DataRoot $_dataRoot
Write-Host "[test.ps1] CARGO_TARGET_DIR = $env:CARGO_TARGET_DIR （per-worktree 隔离）"

# GPT 第十轮 P1：-PrintTargetDir 仅供调用方级回归测试用。它证明本入口**强制**用
# per-worktree namespace 覆盖任何继承的（可能是旧版共享的）CARGO_TARGET_DIR，然后
# 不跑任何 cargo/pytest 即退出。用 base64(UTF-8) 传出，避免 GBK 代码页在 stdout 上
# 破坏含 CJK 的路径字节（与 test_cargo_target_isolation.py 同一手法）。
if ($PrintTargetDir) {
    $b = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($env:CARGO_TARGET_DIR))
    Write-Output "TARGETDIR_B64=$b"
    exit 0
}

function Run-Suite {
    param([string]$Label, [scriptblock]$Cmd)
    Write-Host "═══ 套件：$Label ═══"
    & $Cmd
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[$Label] 失败，退出码 $LASTEXITCODE"
        $script:exitCode = $LASTEXITCODE
    }
}

switch ($Suite) {
    "contract-plan-hash" {
        Run-Suite "contract-plan-hash (Python)" {
            python -m pytest "$REPO_ROOT\tests\contract\test_plan_hash_vectors.py" -q
        }
        Run-Suite "contract-plan-hash (TypeScript)" {
            pnpm --filter "@factory/contracts" test -- plan.test.ts
        }
        Run-Suite "contract-plan-hash (Rust)" {
            cargo test -p factory-contracts --test plan_vectors
        }
    }
    "contract-event-hash" {
        Run-Suite "contract-event-hash (Python)" {
            python -m pytest "$REPO_ROOT\tests\contract\test_event_hash_vectors.py" -q
        }
        Run-Suite "contract-event-hash (TypeScript)" {
            pnpm --filter "@factory/contracts" test -- event.test.ts
        }
        Run-Suite "contract-event-hash (Rust)" {
            cargo test -p factory-contracts --test event_vectors
        }
    }
    "compatibility-spikes" {
        $spikes = @(
            "test-durable-io", "test-sqlite-wal", "test-clock-source",
            "test-named-pipe", "test-runner-identity", "test-git-bridge", "test-tauri-e2e"
        )
        foreach ($spike in $spikes) {
            $script = Join-Path $REPO_ROOT "scripts\spikes\$spike.ps1"
            if (Test-Path $script) {
                $emitFlag = if ($EmitReceipts) { @("-EmitReceipts") } else { @() }
                Run-Suite $spike { & $script @emitFlag }
            } else {
                # GPT 第二轮审核：spike 脚本缺失时不能跳过；标记为 fail-closed 缺口。
                Write-Warning "[$spike] 脚本不存在，记为缺口（exit 1）"
                $script:exitCode = 1
            }
        }
    }
    "all" {
        # GPT 第二轮审核：默认套件必须真跨三语言跑合同测试 + spikes；
        # 不能仅跑 Python 合同测试就宣称套件 PASS。
        Run-Suite "contract (Python)" {
            python -m pytest "$REPO_ROOT\tests\contract" -q
        }
        Run-Suite "contract (TypeScript)" {
            pnpm --filter "@factory/contracts" test
        }
        Run-Suite "contract (Rust, lib)" {
            cargo test -p factory-contracts --tests
        }
        # spikes 必跑：每个 wrapper exit 0/1/2 都按真实 status 处理
        $spikes = @(
            "test-durable-io", "test-sqlite-wal", "test-clock-source",
            "test-named-pipe", "test-runner-identity", "test-git-bridge"
        )
        foreach ($spike in $spikes) {
            $script = Join-Path $REPO_ROOT "scripts\spikes\$spike.ps1"
            if (Test-Path $script) {
                Run-Suite $spike { & $script }
            } else {
                Write-Warning "[$spike] 脚本不存在，记为缺口"
                $script:exitCode = 1
            }
        }
    }
    default {
        # 默认仍为 Python 合同测试；用 "all" 触发跨栈套件。
        Run-Suite "contract (Python)" {
            python -m pytest "$REPO_ROOT\tests\contract" -q
        }
    }
}

exit $exitCode
