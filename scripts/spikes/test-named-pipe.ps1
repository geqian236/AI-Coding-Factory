<#
.SYNOPSIS  Spike 4: Authenticated Named Pipe probe (real two-process certification)
.DESCRIPTION
    起两个独立进程做真·跨进程认证管道验证:
      1. Python server (python_server.py): 建带当前用户 SID ACL 的命名管道,
         循环接受 2 次连接,凭自身 nonce 注册表做跨进程重放判定,
         并用 GetNamedPipeClientProcessId 记录对端真实 PID。
      2. Rust client (named_pipe.exe): 作为**独立进程**连两次,第 1 次发新 nonce
         期望 ACCEPT,第 2 次重发同一 nonce 期望 server 判 REPLAY。
    driver 通过 stdout 上的 PIPE_READY 握手确保 server 先就绪再起 client,
    最终合成 receipt.json —— 两侧断言 + "对端确为独立进程" 拓扑断言全过才 PASS。
    非 Windows 或工具链缺失时如实写 BLOCKED_UNCERTIFIED / ERROR,绝不伪造 PASS。
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\named_pipe"
$receiptPath = "$probeDir\receipt.json"
$serverPy    = "$probeDir\python_server.py"
$serverRcpt  = "$probeDir\server_receipt.json"
$clientRcpt  = "$probeDir\client_receipt.json"
$serverOut   = "$probeDir\_server_stdout.log"
$serverErr   = "$probeDir\_server_stderr.log"
# Rust client 全程经 dev.ps1 + cargo 构建/运行:dev.ps1 将 CARGO_TARGET_DIR/
# CARGO_HOME/RUSTUP_HOME 绑定到项目根下 AI-Coding-Factory-Data\dev,并注入 Unicode
# 安全链接器 ld.lld(CJK 路径也可链接)。故此处不硬编码易漂移的 exe 绝对路径,
# 由 cargo 自行用绑定的 CARGO_TARGET_DIR 定位 target,避免指向陈旧位置跑到旧协议 exe。
$devWrapper  = "$scriptRoot\scripts\dev.ps1"
$manifest    = "$probeDir\Cargo.toml"

function Write-Receipt($obj) {
    # PowerShell 5.1 的 Out-File -Encoding utf8 会写 UTF-8 BOM,Python json.load 会报
    # "Unexpected UTF-8 BOM"。故用 .NET WriteAllText + UTF8Encoding($false) 写无 BOM UTF-8,
    # 保证下游 receipt 门禁/Python 读取端能正常解析。
    $json = ($obj | ConvertTo-Json -Depth 12)
    [System.IO.File]::WriteAllText($receiptPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

# ── 0. 平台前置检查 ────────────────────────────────────────────────────────────
if ($env:OS -notmatch "Windows") {
    $blocked = @{ spike="named_pipe"; status="BLOCKED_UNCERTIFIED"
                  reason="Windows Named Pipes require Windows host"
                  timestamp=(Get-Date -Format "o") }
    Write-Receipt $blocked
    Write-Host ($blocked | ConvertTo-Json)
    exit 0
}

$py = $null
foreach ($c in @("python","py","python3")) {
    try { & $c --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
}
if (-not $py) { throw "Python not found on PATH" }

# ── 1. 经 dev.ps1 + cargo 预构建 Rust client ─────────────────────────────────────
# dev.ps1 绑定 CARGO_HOME/CARGO_TARGET_DIR/RUSTUP_HOME 到项目根下 dev 目录并注入
# Unicode 安全链接器 ld.lld。cargo 的 .cargo/config.toml(linker-flavor=ld)按 cwd
# 向上发现,故必须在仓库根 $scriptRoot 执行,否则 linker-flavor 丢失、链接失败。
# 双 -- 调用协议:第一个 -- 被 PowerShell & 当作"停止解析参数"标记吞掉,第二个 --
# 才原样进入 dev.ps1 的 $args 作为其子命令分隔符。
Push-Location $scriptRoot
try {
    & $devWrapper -- -- cargo build --manifest-path $manifest --quiet
    $buildExit = $LASTEXITCODE
} finally {
    Pop-Location
}
if ($buildExit -ne 0) {
    # 工具链/链接器不可用:如实写 BLOCKED_UNCERTIFIED,绝不伪造 PASS。
    $err = @{ spike="named_pipe"; status="BLOCKED_UNCERTIFIED"
              reason="Rust client build failed (toolchain/linker unavailable)"
              build_exit=$buildExit; timestamp=(Get-Date -Format "o") }
    Write-Receipt $err
    Write-Host ($err | ConvertTo-Json)
    exit 0
}

# ── 2. 清理上轮中间产物,避免读到陈旧 receipt ────────────────────────────────────
foreach ($f in @($serverRcpt, $clientRcpt, $serverOut, $serverErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

# ── 3. 启动 Python server(独立进程,后台),轮询 stdout 上的 PIPE_READY 握手 ──────
Write-Host "启动 Python server ($py) ..."
$srv = Start-Process -FilePath $py -ArgumentList @($serverPy) `
        -RedirectStandardOutput $serverOut -RedirectStandardError $serverErr `
        -PassThru -NoNewWindow

$ready = $false
$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    if ($srv.HasExited) { break }  # server 提前崩溃 -> 停止等待
    if ((Test-Path $serverOut) -and
        (Select-String -Path $serverOut -Pattern 'PIPE_READY' -Quiet)) {
        $ready = $true; break
    }
    Start-Sleep -Milliseconds 100
}

if (-not $ready) {
    try { if (-not $srv.HasExited) { $srv.Kill() } } catch {}
    $errText = ""
    if (Test-Path $serverErr) { $errText = (Get-Content $serverErr -Raw) }
    $r = @{ spike="named_pipe"; status="ERROR"
            error="server did not signal PIPE_READY within timeout"
            server_stderr=$errText; timestamp=(Get-Date -Format "o") }
    Write-Receipt $r
    Write-Host ($r | ConvertTo-Json)
    exit 1
}

# ── 4. 经 dev.ps1 + cargo run 启动 Rust client(独立进程),同次运行内连两次 ──────────
# 用 cargo run 而非硬编码 exe 路径:cargo 用 dev.ps1 绑定的 CARGO_TARGET_DIR 定位刚
# 构建的二进制并作为**独立子进程**运行。cargo 只是父进程,不碰管道 —— 真正 CreateFileW
# 连管道的是 named_pipe.exe 本身,故 server 的 GetNamedPipeClientProcessId 取到的对端
# PID 即该 exe 的 PID,与 client 自报 std::process::id() 一致,拓扑铁证成立。
# client_receipt.json 路径经 cargo 自己的 -- 传给程序 argv[1]。
Write-Host "server 就绪,经 dev.ps1 + cargo run 启动 Rust client (独立进程) ..."
Push-Location $scriptRoot
try {
    & $devWrapper -- -- cargo run --manifest-path $manifest --quiet -- $clientRcpt | Out-Host
    $clientExit = $LASTEXITCODE
} finally {
    Pop-Location
}

# ── 5. 等 server 处理完两次连接后自行退出 ────────────────────────────────────────
if (-not $srv.WaitForExit(15000)) {
    try { $srv.Kill() } catch {}
}

# ── 6. 合成最终 receipt:两侧 receipt + 跨进程拓扑断言 ────────────────────────────
if (-not (Test-Path $serverRcpt)) {
    $r = @{ spike="named_pipe"; status="ERROR"; error="no server_receipt.json"
            server_stderr=(Get-Content $serverErr -Raw -ErrorAction SilentlyContinue)
            timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 1
}
if (-not (Test-Path $clientRcpt)) {
    $r = @{ spike="named_pipe"; status="ERROR"; error="no client_receipt.json"
            client_exit=$clientExit; timestamp=(Get-Date -Format "o") }
    Write-Receipt $r; Write-Host ($r | ConvertTo-Json); exit 1
}

$srvR = Get-Content $serverRcpt -Raw | ConvertFrom-Json
$cliR = Get-Content $clientRcpt -Raw | ConvertFrom-Json

# 汇总两侧断言,并各自加前缀标明来源进程。
$assertions = @()
foreach ($a in $srvR.assertions) {
    $assertions += @{ name="server.$($a.name)"; passed=[bool]$a.passed; detail=$a.detail }
}
foreach ($a in $cliR.assertions) {
    $assertions += @{ name="client.$($a.name)"; passed=[bool]$a.passed; detail=$a.detail }
}

# 顶层拓扑铁证:server 观测到的对端 PID 必须等于 client 进程自报 PID,
# 且都 != server_pid —— 证明确为两个独立进程完成的握手。
$serverPid = [int]$srvR.observable_facts.server_pid
$clientPid = [int]$cliR.environment.client_pid
$observedClientPids = @()
foreach ($c in $srvR.observable_facts.connections) { $observedClientPids += [int]$c.client_pid }
$topologyOk = ($observedClientPids.Count -ge 1) -and
              ($observedClientPids | ForEach-Object { $_ -eq $clientPid } | Where-Object { $_ } | Measure-Object).Count -eq $observedClientPids.Count -and
              ($clientPid -ne $serverPid) -and ($clientPid -ne 0)
$assertions += @{ name="two_process_topology_confirmed"; passed=$topologyOk
                  detail="server_pid=$serverPid client_pid=$clientPid observed=$($observedClientPids -join ',')" }

$allPass = ($assertions | Where-Object { -not $_.passed } | Measure-Object).Count -eq 0
$status  = if ($allPass -and $srvR.status -eq "PASS" -and $cliR.status -eq "PASS") { "PASS" } else { "FAIL" }

$final = [ordered]@{
    spike   = "named_pipe"
    status  = $status
    environment = @{
        os             = "Windows"
        python_version = $srvR.environment.python_version
        sid            = $srvR.environment.sid
        probe_backend  = "python_ctypes_win32 + rust_client_cross_process"
    }
    observable_facts = @{
        pipe_name          = $srvR.observable_facts.pipe_name
        sid                = $srvR.observable_facts.sid
        acl_applied        = [bool]$srvR.observable_facts.acl_applied
        server_pid         = $serverPid
        client_pid         = $clientPid
        observed_client_pids = $observedClientPids
        served_connections = [int]$srvR.observable_facts.served_connections
        first_verdict      = [int]$cliR.observable_facts.first_verdict
        second_verdict     = [int]$cliR.observable_facts.second_verdict
        server_status      = $srvR.status
        client_status      = $cliR.status
        client_exit_code   = $clientExit
    }
    assertions = $assertions
    artifact_digest = @{ server=$srvR.artifact_digest; client=$cliR.artifact_digest }
    timestamp = (Get-Date -Format "o")
}

Write-Receipt $final
Write-Host "STATUS: $status"
if ($status -notin @("PASS","BLOCKED_UNCERTIFIED")) { exit 1 }
# 成功/受阻显式 exit 0:确保 $LASTEXITCODE 被设置,避免上层 StrictMode 读未定义变量中断。
exit 0
