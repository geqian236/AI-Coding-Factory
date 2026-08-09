<#
.SYNOPSIS  Spike: Windows 持久 IO 探针(真·跨进程硬杀崩溃一致性验证)
.DESCRIPTION
    起一个独立的 Rust writer 子进程(windows_durable_io child <dir>):
      write -> FlushFileBuffers(sync_all)-> 原子 rename -> 打印 CHILD_READY
      (含自报 PID / nonce / 实测 flush 结果)-> 挂起。
    本 driver 作为**父/杀手进程**:轮询 stdout 上的 CHILD_READY 握手,确认 writer
    已完成 flush+rename 且仍在运行(未正常退出)后,用 Stop-Process -Force 对其
    **定点硬杀**;随后作为**独立进程**读回 final 文件,逐字节校验内容并核对 nonce
    新鲜度 —— 证明"writer 崩溃(被杀)后,独立进程仍读到完整正确的已落盘数据"。

    诚实边界:掉电级 durability(断电/内核崩溃后仍在)与"父目录 fsync 真实生效"
    在纯软件环境无法证明,如实写入 uncertified_aspects 标 BLOCKED_UNCERTIFIED,
    **不参与 gating**,绝不冒充已认证。非 Windows / 工具链缺失时如实写
    BLOCKED_UNCERTIFIED / ERROR。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$spikeName    = "windows_durable_io"
$scriptRoot   = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir     = "$scriptRoot\tools\compat-probes\windows_durable_io"
$receiptPath  = "$probeDir\receipt.json"
$manifest     = "$probeDir\Cargo.toml"
$devWrapper   = "$scriptRoot\scripts\dev.ps1"
$tmpDir       = "$probeDir\probe_tmp"
$childOut     = "$probeDir\_child_stdout.log"
$childErr     = "$probeDir\_child_stderr.log"
# dev.ps1 将 CARGO_TARGET_DIR 绑定到项目根下 DATA_ROOT 内的 per-worktree target。
# writer 是自足 exe,运行期不依赖 dev.ps1 环境(仅**构建**需要 ld.lld 链接器),
# 故构建后直接 Start-Process 该 exe 以取真实 PID 做定点硬杀。
# GPT 第九轮 P0：target 已按 worktree 哈希隔离(dev.ps1 构建时用 Get-WorktreeTargetDir
# 派生同一路径)。本 wrapper 用同一函数、同一 $scriptRoot(=worktree 根)算出相同 target,
# 从本 worktree 自己的产物取 exe,绝不误取另一 checkout 编译的 windows_durable_io.exe。
$dataRoot     = "D:\codex项目\AI-Coding-Factory-Data\dev"
. (Join-Path $scriptRoot "scripts\_worktree-target.ps1")
# GPT Phase 0 PR #2：non-Windows 与 build/linker 失败两条 BLOCKED 回执改用共享
# New-DurableIoBlockedReceipt（单一真源），补齐 validator 必需的 assertions/
# observable_facts/subcheckId，使拒因从 INVALID 变为合法 core BLOCKED（仍 fail-closed）。
. (Join-Path $scriptRoot "scripts\spikes\_durable-io-receipt.ps1")
$targetDir    = Get-WorktreeTargetDir -WorktreeRoot $scriptRoot -DataRoot $dataRoot
$writerExe    = "$targetDir\debug\windows_durable_io.exe"

# receipt 统一用无 BOM UTF-8 写:PowerShell 5.1 的 Out-File -Encoding utf8 会写 BOM,
# 令下游 Python json.load 报 "Unexpected UTF-8 BOM"。
function Write-Receipt($obj) {
    $json = $obj | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($receiptPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ── 0. 平台前置检查 ─────────────────────────────────────────────────────────────
if ($env:OS -notmatch "Windows") {
    # 非 Windows 宿主：合法 core BLOCKED（含 assertions/observable_facts/subcheckId），
    # validator 会以"core spike may not be BLOCKED_UNCERTIFIED"拒绝并让 CI fail-closed。
    $r = New-DurableIoBlockedReceipt -Kind non_windows -Detail ([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 1. 经 dev.ps1 + cargo 预构建 Rust writer(双 -- 调用协议)──────────────────────
# 第一个 -- 被 PowerShell & 当作"停止解析参数"标记吞掉,第二个 -- 才原样进入
# dev.ps1 的 $args 作为其子命令分隔符。dev.ps1 注入 Unicode 安全链接器 ld.lld,
# 使含 CJK 的项目根也能链接。
Push-Location $scriptRoot
try {
    & $devWrapper -- -- cargo build --manifest-path $manifest --quiet
    $buildExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($buildExit -ne 0 -or -not (Test-Path $writerExe)) {
    # 构建/链接失败：合法 core BLOCKED（含 assertions/observable_facts/subcheckId）。
    # 补齐证据结构只为让 validator 的拒因从"INVALID 缺字段"变成合法 core BLOCKED；
    # durable-IO 绝不进 allowlist，BLOCKED 仍让 CI fail-closed，绝不伪造 PASS。
    $r = New-DurableIoBlockedReceipt -Kind toolchain `
            -Detail "build_exit=$buildExit writer_exe=$writerExe" `
            -ExtraFacts @{ build_exit = $buildExit; writer_exe = $writerExe }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 2. 清理上轮中间产物,保证 nonce 新鲜度断言严格成立 ────────────────────────────
[System.IO.Directory]::CreateDirectory($tmpDir) | Out-Null
foreach ($f in @("probe_tmp.dat","probe_final.dat")) {
    $p = [System.IO.Path]::Combine($tmpDir, $f)
    if ([System.IO.File]::Exists($p)) { [System.IO.File]::Delete($p) }
}
foreach ($f in @($childOut, $childErr)) { if (Test-Path $f) { Remove-Item $f -Force } }

# ── 3. 起独立 writer 子进程(后台),轮询 stdout 上的 CHILD_READY 握手 ─────────────
Write-Host "启动 Rust writer 子进程(独立进程)..."
$child = Start-Process -FilePath $writerExe -ArgumentList @("child", $tmpDir) `
          -RedirectStandardOutput $childOut -RedirectStandardError $childErr `
          -PassThru -NoNewWindow
$spawnPid = $child.Id

$readyLine = $null
$deadline  = (Get-Date).AddSeconds(15)
while ((Get-Date) -lt $deadline) {
    if ($child.HasExited) { break }   # writer 提前退出 -> 停止等待
    if (Test-Path $childOut) {
        $m = Select-String -Path $childOut -Pattern '^CHILD_READY ' -ErrorAction SilentlyContinue |
             Select-Object -First 1
        if ($m) { $readyLine = $m.Line; break }
    }
    Start-Sleep -Milliseconds 100
}

if (-not $readyLine) {
    try { if (-not $child.HasExited) { $child.Kill() } } catch {}
    $errText = ""
    if (Test-Path $childErr) { $errText = (Get-Content $childErr -Raw) }
    $r = [ordered]@{ spike=$spikeName; status="ERROR"
                     error="writer did not signal CHILD_READY within timeout"
                     child_stderr=$errText; timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 1
}

# ── 4. 解析 READY 行,提取 writer 自报 PID / nonce / 实测 flush 结果 ───────────────
function Get-Field($line, $key) {
    $m = [regex]::Match($line, "$key=(\S+)")
    if ($m.Success) { return $m.Groups[1].Value } else { return $null }
}
$childPid   = [int](Get-Field $readyLine "pid")
$nonce      = Get-Field $readyLine "nonce"
$childBytes = [int](Get-Field $readyLine "bytes")
$flushOk    = (Get-Field $readyLine "flush_ok")    -eq "true"
$renameOk   = (Get-Field $readyLine "rename_ok")   -eq "true"
$dirSyncOk  = (Get-Field $readyLine "dir_sync_ok") -eq "true"

# ── 5. writer 已 flush+rename 且仍挂起(未正常退出)时,对其定点硬杀 ────────────────
# 这是"崩溃点":writer 从未执行任何清理/正常退出路径就被强杀。
$aliveBeforeKill = $false
try { $aliveBeforeKill = -not (Get-Process -Id $childPid -ErrorAction Stop).HasExited } catch {}
try { Stop-Process -Id $childPid -Force -ErrorAction Stop } catch {}

# 等待进程确实消失(最多 5s),确认硬杀生效。
$killed = $false
$killDeadline = (Get-Date).AddSeconds(5)
while ((Get-Date) -lt $killDeadline) {
    if (-not (Get-Process -Id $childPid -ErrorAction SilentlyContinue)) { $killed = $true; break }
    Start-Sleep -Milliseconds 100
}

# ── 6. 作为独立进程读回 final,逐字节校验内容 + nonce 新鲜度 ───────────────────────
$finalFile = [System.IO.Path]::Combine($tmpDir, "probe_final.dat")
$tmpFile   = [System.IO.Path]::Combine($tmpDir, "probe_tmp.dat")
$expected  = [System.Text.Encoding]::UTF8.GetBytes("DURABLE_IO_PROBE_v2`nnonce=$nonce`n")

$finalExists = [System.IO.File]::Exists($finalFile)
$tmpGone     = (-not [System.IO.File]::Exists($tmpFile))
$contentOk   = $false
$nonceFresh  = $false
$fileSize    = 0
$digest      = "none"
if ($finalExists) {
    $readBack = [System.IO.File]::ReadAllBytes($finalFile)
    $fileSize = $readBack.Length
    $contentOk = ($readBack.Length -eq $expected.Length)
    if ($contentOk) {
        for ($i = 0; $i -lt $expected.Length; $i++) {
            if ($readBack[$i] -ne $expected[$i]) { $contentOk = $false; break }
        }
    }
    $readText = [System.Text.Encoding]::UTF8.GetString($readBack)
    $nonceFresh = ($readText -match [regex]::Escape("nonce=$nonce"))
    $hashBytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash($readBack)
    $digest = "sha256:" + [System.BitConverter]::ToString($hashBytes).Replace("-","").ToLower()
}

# ── 7. 组装 gating 断言(全部来自真实观测)───────────────────────────────────────
$assertions = @()
$assertions += @{ name="writer_is_separate_process"
                  passed=($childPid -ne $PID) -and ($spawnPid -eq $childPid) -and ($childPid -ne 0)
                  detail="driver_pid=$PID spawn_pid=$spawnPid writer_self_reported_pid=$childPid" }
$assertions += @{ name="writer_alive_then_hard_killed"
                  passed=($aliveBeforeKill -and $killed)
                  detail="alive_before_kill=$aliveBeforeKill killed=$killed (被杀前仍在运行,未走正常退出路径)" }
$assertions += @{ name="atomic_rename_completed"
                  passed=($renameOk -and $finalExists -and $tmpGone)
                  detail="writer_rename_ok=$renameOk final_exists=$finalExists tmp_gone=$tmpGone" }
$assertions += @{ name="flush_before_crash"
                  passed=$flushOk
                  detail="writer 自报 sync_all(FlushFileBuffers) 实测结果=$flushOk(硬杀前已 flush)" }
$assertions += @{ name="data_survives_cross_process_kill"
                  passed=($contentOk -and ($fileSize -eq $childBytes))
                  detail="独立进程读回 byte-for-byte 匹配=$contentOk size=$fileSize writer_bytes=$childBytes" }
$assertions += @{ name="final_nonce_matches_this_writer"
                  passed=$nonceFresh
                  detail="final 文件含本 writer 自报 nonce=$nonce -> 非陈旧文件" }

$allPass = ($assertions | Where-Object { -not $_.passed } | Measure-Object).Count -eq 0
$status  = if ($allPass) { "PASS" } else { "FAIL" }

# ── 8. 诚实边界:无法在纯软件环境认证的方面,如实标 BLOCKED_UNCERTIFIED(非 gating)─
$uncertified = @(
    @{ aspect="power_loss_durability"; status="BLOCKED_UNCERTIFIED"
       reason="断电/内核崩溃后数据仍在 需真实掉电或内核崩溃 纯软件杀进程无法证明(数据仍在 OS page cache)" }
    @{ aspect="parent_dir_fsync_effective"; status="BLOCKED_UNCERTIFIED"
       reason="Windows 对目录句柄 sync_all 基本 no-op writer 实测 dir_sync_ok=$dirSyncOk 无法证明父目录条目已落盘" }
)

$final = [ordered]@{
    spike  = $spikeName
    status = $status
    environment = @{
        os            = ([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)
        arch          = ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString())
        ps_version    = ($PSVersionTable.PSVersion.ToString())
        probe_backend = "rust_child_writer + powershell_parent_killer(cross_process_hard_kill)"
        driver_pid    = $PID
    }
    actions = @(
        "cargo build windows_durable_io (via dev.ps1, ld.lld)",
        "Start-Process writer 'child' (独立进程) 后台",
        "poll stdout for CHILD_READY (writer 已 write+flush+rename)",
        "Stop-Process -Force 定点硬杀 writer(崩溃点:未走正常退出)",
        "独立进程读回 final:逐字节校验 + nonce 新鲜度 + sha256"
    )
    observable_facts = @{
        driver_pid          = $PID
        writer_pid          = $childPid
        writer_spawn_pid    = $spawnPid
        nonce               = $nonce
        writer_reported_bytes = $childBytes
        final_file_size     = $fileSize
        flush_ok            = $flushOk
        rename_ok           = $renameOk
        dir_sync_ok         = $dirSyncOk
        alive_before_kill   = $aliveBeforeKill
        writer_hard_killed  = $killed
        content_byte_match  = $contentOk
        nonce_fresh         = $nonceFresh
    }
    assertions        = $assertions
    uncertified_aspects = $uncertified
    artifact_digest   = $digest
    timestamp         = (Get-Date -Format "o")
}

Write-Receipt $final
Write-Host "STATUS: $status"
if ($status -notin @("PASS","BLOCKED_UNCERTIFIED")) { exit 1 }
# 成功/受阻显式 exit 0:确保 $LASTEXITCODE 被设置,避免上层 StrictMode 读未定义变量中断。
exit 0
