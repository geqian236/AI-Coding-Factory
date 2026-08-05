<#
.SYNOPSIS  Spike: WSL Runner Identity 探针(非破坏性"孤儿存活"真验证)
.DESCRIPTION
    起一个独立的 Rust broker 子进程(runner_identity broker <name>):用 driver 传入
    的容器名(含 nonce)起 detached 容器 -> 打印 BROKER_READY(含自报 PID / name /
    container_id)-> 挂起。本 driver 作为**父/杀手进程**:
      1. 检查 docker 可用性(缺失 -> BLOCKED_UNCERTIFIED,不伪造);
      2. 确认 python:3.12-slim 镜像本地可用(缺失 -> BLOCKED_UNCERTIFIED,区分基建问题与能力);
      3. 起 broker,轮询 stdout 上的 BROKER_READY 握手;
      4. 轮询 docker inspect 确认容器 Running=true(容器已由 broker 拉起);
      5. 确认 broker 仍在运行后,用 Stop-Process -Force 对其**定点硬杀**;
      6. 作为**独立进程**再次 docker inspect,确认容器在 broker 死后**仍 Running**
         (孤儿存活),并核对存活容器 ID 与 broker 自报 container_id 一致(因果铁证);
      7. docker rm -f 清理容器。

    诚实边界:真正的 broker-kill(杀 LxssManager 服务 / dockerd daemon)需管理员且
    **破坏共享系统**,故不做,如实写入 uncertified_aspects 标 BLOCKED_UNCERTIFIED,
    **不参与 gating**,绝不冒充已认证。非 Windows / docker 缺失 / 工具链缺失时如实写
    BLOCKED_UNCERTIFIED / ERROR。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$spikeName    = "runner_identity"
$scriptRoot   = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir     = "$scriptRoot\tools\compat-probes\runner_identity"
$receiptPath  = "$probeDir\receipt.json"
$manifest     = "$probeDir\Cargo.toml"
$devWrapper   = "$scriptRoot\scripts\dev.ps1"
$brokerOut    = "$probeDir\_broker_stdout.log"
$brokerErr    = "$probeDir\_broker_stderr.log"
# dev.ps1 将 CARGO_TARGET_DIR 绑定到 D:\codex项目\AI-Coding-Factory-Data\dev\cargo-target。
# broker 是自足 exe,运行期不依赖 dev.ps1 环境(仅**构建**需要 ld.lld 链接器),
# 故构建后直接 Start-Process 该 exe 以取真实 PID 做定点硬杀。
$dataRoot     = "D:\codex项目\AI-Coding-Factory-Data\dev"
$brokerExe    = "$dataRoot\cargo-target\debug\runner_identity.exe"

# receipt 统一用无 BOM UTF-8 写:PowerShell 5.1 的 Out-File -Encoding utf8 会写 BOM,
# 令下游 Python json.load 报 "Unexpected UTF-8 BOM"。
function Write-Receipt($obj) {
    $json = $obj | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText($receiptPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ── 0. 平台前置检查 ─────────────────────────────────────────────────────────────
if ($env:OS -notmatch "Windows") {
    $r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"
                     reason="WSL/Windows runner identity semantics require Windows host"
                     timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 1. 检查 docker 可用性(缺失如实 BLOCKED,不伪造)──────────────────────────────
$dockerVersion = $null
try {
    # 原生命令管道给 Select-Object -First 1 会因上游被提前中断(StopUpstreamCommandsException)
    # 把 $LASTEXITCODE 污染成 -1,令 daemon 明明可用却误判不可用(假 BLOCKED)。
    # 故先落变量、立即取 $LASTEXITCODE,再切片。
    $verRaw  = docker version --format "{{.Server.Version}}" 2>$null
    $verExit = $LASTEXITCODE
    if ($verExit -eq 0) { $dockerVersion = ($verRaw | Select-Object -First 1) }
} catch { $dockerVersion = $null }

if (-not $dockerVersion) {
    $r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"
                     reason="Docker CLI/daemon 不可用 —— 安装 Docker Desktop(WSL2 后端)并启动 daemon 后方可认证"
                     observable_facts=@{ docker_available=$false }
                     assertions=@(); timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 2. 确认 python:3.12-slim 镜像本地可用(本地已缓存;缺失 -> BLOCKED,区分基建问题)──
Write-Host "检查 python:3.12-slim 本地镜像 ..."
docker image inspect "python:3.12-slim" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"
                     reason="本地无 python:3.12-slim 镜像且 registry 不可达 —— 基建问题,非能力缺陷;请先 docker pull python:3.12-slim"
                     observable_facts=@{ docker_available=$true; image_available=$false }
                     assertions=@(); timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 3. 经 dev.ps1 + cargo 预构建 Rust broker(双 -- 调用协议)────────────────────────
Push-Location $scriptRoot
try {
    & $devWrapper -- -- cargo build --manifest-path $manifest --quiet
    $buildExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($buildExit -ne 0 -or -not (Test-Path $brokerExe)) {
    $r = [ordered]@{ spike=$spikeName; status="BLOCKED_UNCERTIFIED"
                     reason="Rust broker build failed or exe missing (toolchain/linker unavailable)"
                     build_exit=$buildExit; broker_exe=$brokerExe; timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 0
}

# ── 4. 生成 nonce 容器名,并清理可能残留的同名容器,保证因果关联干净 ────────────────
$nonce         = [guid]::NewGuid().ToString("N")
$containerName = "acf_probe_$nonce"
try { docker rm -f $containerName 2>$null | Out-Null } catch {}   # 幂等:容器不存在时 no-op
foreach ($f in @($brokerOut, $brokerErr)) { if (Test-Path $f) { Remove-Item $f -Force } }

# cleanup 辅助:无论后续哪条路径退出,都尽力 rm -f 容器,不给共享系统留垃圾。
function Invoke-Cleanup { try { docker rm -f $containerName 2>$null | Out-Null } catch {} }

try {
    # ── 5. 起独立 broker 子进程(后台),轮询 stdout 上的 BROKER_READY 握手 ───────────
    Write-Host "启动 Rust broker 子进程(独立进程)..."
    $broker = Start-Process -FilePath $brokerExe -ArgumentList @("broker", $containerName) `
              -RedirectStandardOutput $brokerOut -RedirectStandardError $brokerErr `
              -PassThru -NoNewWindow
    $spawnPid = $broker.Id

    $readyLine = $null
    $deadline  = (Get-Date).AddSeconds(30)   # 含容器启动;镜像本地已缓存,通常数秒内就绪
    while ((Get-Date) -lt $deadline) {
        if ($broker.HasExited) { break }
        if (Test-Path $brokerOut) {
            $m = Select-String -Path $brokerOut -Pattern '^BROKER_READY ' -ErrorAction SilentlyContinue |
                 Select-Object -First 1
            if ($m) { $readyLine = $m.Line; break }
        }
        Start-Sleep -Milliseconds 150
    }

    if (-not $readyLine) {
        try { if (-not $broker.HasExited) { $broker.Kill() } } catch {}
        $errText = ""
        if (Test-Path $brokerErr) { $errText = (Get-Content $brokerErr -Raw) }
        Invoke-Cleanup
        $r = [ordered]@{ spike=$spikeName; status="ERROR"
                         error="broker did not signal BROKER_READY within timeout"
                         broker_stderr=$errText; timestamp=(Get-Date -Format "o") }
        Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 1
    }

    # ── 6. 解析 READY 行,提取 broker 自报 PID / name / container_id ─────────────────
    function Get-Field($line, $key) {
        $m = [regex]::Match($line, "$key=(\S+)")
        if ($m.Success) { return $m.Groups[1].Value } else { return $null }
    }
    $brokerPid          = [int](Get-Field $readyLine "pid")
    $reportedName       = Get-Field $readyLine "name"
    $reportedContainer  = Get-Field $readyLine "container_id"

    # ── 7. 轮询确认容器已由 broker 拉起并 Running ─────────────────────────────────────
    $runningBefore = $false
    $idBefore      = $null
    $runDeadline   = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $runDeadline) {
        # 同样避开 Select-Object -First 1 污染 $LASTEXITCODE:先落变量再取退出码。
        $stateRaw  = docker inspect --format "{{.State.Running}}" $containerName 2>$null
        $stateExit = $LASTEXITCODE
        $state     = ($stateRaw | Select-Object -First 1)
        if ($stateExit -eq 0 -and $state -eq "true") {
            $runningBefore = $true
            $idBefore = (docker inspect --format "{{.Id}}" $containerName 2>$null | Select-Object -First 1)
            break
        }
        Start-Sleep -Milliseconds 200
    }

    # ── 8. broker 仍在运行(未正常退出)时,对其定点硬杀 —— 这是"broker 崩溃点" ────────
    $aliveBeforeKill = $false
    try { $aliveBeforeKill = -not (Get-Process -Id $brokerPid -ErrorAction Stop).HasExited } catch {}
    try { Stop-Process -Id $brokerPid -Force -ErrorAction Stop } catch {}

    $brokerKilled = $false
    $killDeadline = (Get-Date).AddSeconds(5)
    while ((Get-Date) -lt $killDeadline) {
        if (-not (Get-Process -Id $brokerPid -ErrorAction SilentlyContinue)) { $brokerKilled = $true; break }
        Start-Sleep -Milliseconds 100
    }

    # ── 9. broker 死后,作为独立进程再查容器:仍 Running 即证孤儿存活 ───────────────────
    # 给一小段观察窗,确认容器不因 broker 死亡而被联动清理(而非仅"查询时序早于联动")。
    Start-Sleep -Milliseconds 800
    $runningAfter = $false
    $idAfter      = $null
    $state2Raw  = docker inspect --format "{{.State.Running}}" $containerName 2>$null
    $state2Exit = $LASTEXITCODE
    $state2     = ($state2Raw | Select-Object -First 1)
    if ($state2Exit -eq 0) {
        $runningAfter = ($state2 -eq "true")
        $idAfter = (docker inspect --format "{{.Id}}" $containerName 2>$null | Select-Object -First 1)
    }

    # 因果核对:broker 死后仍存活的容器 ID,必须与 broker 自报 container_id 一致。
    $idMatches = $false
    if ($idAfter -and $reportedContainer) {
        $idMatches = $reportedContainer.StartsWith($idAfter) -or $idAfter.StartsWith($reportedContainer) -or ($idAfter -eq $reportedContainer)
    }

    # ── 10. 清理容器(不依赖 broker 存活 —— 恰印证容器与 broker 解耦)─────────────────
    Invoke-Cleanup

    # ── 11. 组装 gating 断言(全部来自真实观测)─────────────────────────────────────
    $assertions = @()
    $assertions += @{ name="broker_is_separate_process"
                      passed=($brokerPid -ne $PID) -and ($spawnPid -eq $brokerPid) -and ($brokerPid -ne 0)
                      detail="driver_pid=$PID spawn_pid=$spawnPid broker_self_reported_pid=$brokerPid" }
    $assertions += @{ name="broker_alive_then_hard_killed"
                      passed=($aliveBeforeKill -and $brokerKilled)
                      detail="alive_before_kill=$aliveBeforeKill killed=$brokerKilled (被杀前仍在运行,未走正常退出路径)" }
    $assertions += @{ name="container_running_before_kill"
                      passed=$runningBefore
                      detail="broker 存活期间 docker inspect State.Running=$runningBefore" }
    $assertions += @{ name="container_survives_broker_kill"
                      passed=$runningAfter
                      detail="broker 被硬杀后 docker inspect State.Running=$runningAfter(孤儿存活)" }
    $assertions += @{ name="surviving_container_matches_broker"
                      passed=$idMatches
                      detail="存活容器 id=$idAfter broker 自报 container_id=$reportedContainer -> 因果一致=$idMatches" }
    $assertions += @{ name="container_name_matches_nonce"
                      passed=($reportedName -eq $containerName)
                      detail="broker 自报 name=$reportedName driver 生成 name=$containerName" }

    $allPass = ($assertions | Where-Object { -not $_.passed } | Measure-Object).Count -eq 0
    $status  = if ($allPass) { "PASS" } else { "FAIL" }

    # ── 12. 诚实边界:daemon/服务级 kill 无法在非破坏性前提下认证,如实标 BLOCKED ──────
    $uncertified = @(
        @{ aspect="dockerd_daemon_kill"; status="BLOCKED_UNCERTIFIED"
           reason="杀 dockerd/com.docker.backend daemon 会中断整机所有容器 属破坏共享系统 需管理员 故不做" }
        @{ aspect="lxssmanager_service_kill"; status="BLOCKED_UNCERTIFIED"
           reason="杀 LxssManager 服务会重置整个 WSL2 子系统 属破坏共享系统 需管理员 故不做" }
    )

    $final = [ordered]@{
        spike  = $spikeName
        status = $status
        environment = @{
            os             = ([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)
            arch           = ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString())
            ps_version     = ($PSVersionTable.PSVersion.ToString())
            docker_version = $dockerVersion
            probe_backend  = "rust_broker + powershell_parent_killer(non_destructive_orphan_survival)"
            driver_pid     = $PID
        }
        actions = @(
            "docker version + docker image inspect python:3.12-slim (确认本地缓存可用)",
            "cargo build runner_identity (via dev.ps1, ld.lld)",
            "Start-Process broker 'broker <name>' (独立进程) 后台",
            "poll stdout for BROKER_READY (broker 已 docker run -d 起容器)",
            "poll docker inspect State.Running=true (容器就绪)",
            "Stop-Process -Force 定点硬杀 broker(崩溃点:未走正常退出)",
            "独立进程 docker inspect:确认容器仍 Running(孤儿存活)+ 因果核对 container_id",
            "docker rm -f 清理容器"
        )
        observable_facts = @{
            docker_available     = $true
            image_available       = $true
            driver_pid           = $PID
            broker_pid           = $brokerPid
            broker_spawn_pid     = $spawnPid
            container_name       = $containerName
            broker_reported_id   = $reportedContainer
            inspected_id_before  = $idBefore
            inspected_id_after   = $idAfter
            running_before_kill  = $runningBefore
            running_after_kill   = $runningAfter
            alive_before_kill    = $aliveBeforeKill
            broker_hard_killed   = $brokerKilled
            container_id_matches = $idMatches
        }
        assertions          = $assertions
        uncertified_aspects = $uncertified
        artifact_digest     = "container_id:$reportedContainer"
        timestamp           = (Get-Date -Format "o")
    }

    Write-Receipt $final
    Write-Host "STATUS: $status"
    if ($status -notin @("PASS","BLOCKED_UNCERTIFIED")) { exit 1 }
    exit 0
} catch {
    # 任何异常:尽力清理容器,如实写 ERROR,绝不伪造 PASS。
    Invoke-Cleanup
    $r = [ordered]@{ spike=$spikeName; status="ERROR"; error=$_.Exception.Message
                     timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 1
}
