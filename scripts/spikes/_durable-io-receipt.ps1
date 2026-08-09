# scripts/spikes/_durable-io-receipt.ps1
# 编码：UTF-8 with BOM（本文件含中文注释；PS 5.1 在 GBK 代码页下读无 BOM 的 UTF-8 中文会乱码，
# 带 BOM 后 powershell.exe(5.1) 与 pwsh 7 都能无歧义按 UTF-8 读取；BOM 检测逐文件，
# 与无 BOM 的 _receipt-validator.ps1 混合 dot-source 不受影响）。
#
# GPT Phase 0 PR #2 修复（durable-IO 两条 BLOCKED 回执缺证据结构）：
#   test-durable-io.ps1 原先在 (1) 非 Windows 宿主、(2) Rust writer 构建/链接失败 两条
#   BLOCKED_UNCERTIFIED 路径上，只写了 { spike/status/reason/timestamp }，**缺** validator
#   （_receipt-validator.ps1 的 Test-SpikeReceiptEvidence）判定所必需的 assertions/
#   observable_facts/subcheckId。于是 validator 在"必需字段 assertions 缺失"处就判 INVALID，
#   而不是把它当作**合法的 core BLOCKED**来拒绝。
#
#   本 helper 把这两条 BLOCKED 回执的构造抽成**单一真源**：wrapper 与回归测试都调用
#   New-DurableIoBlockedReceipt，绝不各写一份而漂移（与 _receipt-validator.ps1 同一模式）。
#   产出的回执带：稳定 subcheckId、非空 assertions（每条 passed 为**真** [bool] $false）、
#   非空 observable_facts。
#
#   注意边界：durable-IO 是 **core spike**，绝不进 envCompat allowlist。补齐证据结构**不是**
#   为了让它被放行，而是让 validator 的拒因从"INVALID 缺字段"变成合法可审计的
#   "core spike may not be BLOCKED_UNCERTIFIED"——BLOCKED 仍让 CI fail-closed，绝不伪造 PASS。
Set-StrictMode -Version Latest

function New-DurableIoBlockedReceipt {
    <#
    .SYNOPSIS
        构造 windows_durable_io 的一条**合法** BLOCKED_UNCERTIFIED 回执（含 validator 必需证据）。
    .PARAMETER Kind
        BLOCKED 原因分类，决定稳定的 subcheckId：
          - non_windows：非 Windows 宿主，无法验证 Windows durable-IO 语义。
          - toolchain  ：Rust writer 构建/链接失败（工具链/链接器不可用）。
    .PARAMETER Detail
        追加到 assertion.detail 与 observable_facts 的诊断文本（如 build_exit、OS 描述）。
    .PARAMETER ExtraFacts
        额外并入 observable_facts 的键值（如 build_exit / writer_exe）。
    .OUTPUTS
        [ordered] 回执对象：spike/status/subcheckId/reason/observable_facts/assertions/timestamp。
        每条 assertion 的 passed 是真正的 [bool] $false（ConvertTo-Json 会写成 JSON boolean false，
        绝非字符串 "false"——validator 的 strict-bool 会拒绝字符串伪造）。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("non_windows", "toolchain")]
        [string]$Kind,
        [string]$Detail = "",
        [hashtable]$ExtraFacts = $null
    )
    $spikeName = "windows_durable_io"

    if ($Kind -eq "non_windows") {
        # 稳定 subcheckId：非 Windows 宿主。
        $subcheckId = "spike:$spikeName/non_windows_host"
        $reason     = "Windows durable-IO semantics require Windows host"
        $assertions = @(
            [ordered]@{
                name   = "host_is_windows"
                passed = $false
                detail = "宿主非 Windows，无法验证 Windows durable-IO 语义；$Detail"
            }
        )
        $facts = [ordered]@{
            host_is_windows = $false
            os_detail       = $Detail
        }
    }
    else {
        # 稳定 subcheckId：工具链/链接器不可用致 Rust writer 构建失败。
        $subcheckId = "spike:$spikeName/toolchain_unavailable"
        $reason     = "Rust writer build failed or exe missing (toolchain/linker unavailable)"
        $assertions = @(
            [ordered]@{
                name   = "rust_writer_built"
                passed = $false
                detail = "Rust writer 构建失败或 exe 缺失（工具链/链接器不可用）；$Detail"
            }
        )
        $facts = [ordered]@{
            rust_writer_built = $false
            build_detail      = $Detail
        }
    }

    # 并入调用方提供的额外事实（如 build_exit / writer_exe），保持 observable_facts 可审计。
    if ($null -ne $ExtraFacts) {
        foreach ($k in $ExtraFacts.Keys) { $facts[$k] = $ExtraFacts[$k] }
    }

    return [ordered]@{
        spike            = $spikeName
        status           = "BLOCKED_UNCERTIFIED"
        subcheckId       = $subcheckId
        reason           = $reason
        observable_facts = $facts
        assertions       = $assertions
        timestamp        = (Get-Date -Format "o")
    }
}
