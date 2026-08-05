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
    [switch]$EmitReceipts
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$REPO_ROOT = Split-Path $PSScriptRoot -Parent
$exitCode  = 0

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
