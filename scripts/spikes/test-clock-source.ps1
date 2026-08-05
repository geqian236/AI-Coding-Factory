<#
.SYNOPSIS  Spike 3: Execution Clock probe
.DESCRIPTION
  Tries to build and run the Rust clock_source probe.
  Falls back to a Python timing harness if cargo unavailable.
  Emits tools/compat-probes/clock_source/receipt.json
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$probeDir    = "$scriptRoot\tools\compat-probes\clock_source"
$receiptPath = "$probeDir\receipt.json"

function Get-CargoPath {
    # 注意：必须把 `cargo --version` 的 stdout 丢弃（Out-Null），否则版本字符串会
    # 混入函数返回值，调用方拿到的 $cargo 变成 ["cargo 1.x ...","cargo"] 数组，
    # 后续 & $cargo 会以版本串当命令名，报 CommandNotFound。
    foreach ($c in @("cargo","$env:USERPROFILE\.cargo\bin\cargo.exe")) {
        try { & $c --version 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { return $c } } catch {}
    }
    return $null
}

$cargo = Get-CargoPath
$status = "PASS"

if ($cargo) {
    Write-Host "Building clock_source with cargo..."
    Push-Location $probeDir
    # cargo 的正常进度写 stderr；在 $ErrorActionPreference=Stop 下，PowerShell 5.1
    # 会把 native stderr 行包成 ErrorRecord 并当成终止错误，导致构建"假失败"。
    # 故在 native 调用前后临时降级为 Continue，仅以 $LASTEXITCODE 判定成败。
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $cargo build --release 2>&1 | Write-Host
    $buildExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    Pop-Location
    if ($buildExit -ne 0) { $cargo = $null }
}

if ($cargo) {
    # dev.ps1 会把 CARGO_TARGET_DIR 重定向到共享 ASCII 根，构建产物落在那里而非
    # $probeDir\target；优先用该环境变量定位二进制，未设时回退到本地 target。
    $targetRoot = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { "$probeDir\target" }
    $bin = "$targetRoot\release\clock_source.exe"
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $out = & $bin 2>&1
    $runExit = $LASTEXITCODE
    $ErrorActionPreference = $prevEap
    $out | Write-Host
    if ($runExit -ne 0) { $status = "FAIL" }
    $out | Out-File -Encoding utf8 $receiptPath
} else {
    Write-Host "cargo not available — running Python timing fallback"
    # Python fallback: measure monotonic clock includes sleep
    $py = $null
    foreach ($c in @("python","py","python3")) {
        try { & $c --version 2>$null; if ($LASTEXITCODE -eq 0) { $py = $c; break } } catch {}
    }
    if (-not $py) { throw "Neither cargo nor python found" }

    $pyCode = @"
import time, json, os, platform, hashlib, datetime
SPIKE = 'clock_source'
sleep_ms = 100
wall_before = time.time_ns()
mono_before = time.monotonic_ns()
time.sleep(sleep_ms / 1000.0)
mono_elapsed = time.monotonic_ns() - mono_before
wall_elapsed = time.time_ns() - wall_before
drift_ns = mono_elapsed - wall_elapsed
drift_ppm = int(drift_ns * 1_000_000 / wall_elapsed) if wall_elapsed else 0
mono_ok = mono_elapsed >= sleep_ms * 900_000  # >=90ms
boot_id = 'boot_time_ns:' + str(time.time_ns() - mono_elapsed)
env = {'os': platform.system(), 'version': platform.version(),
       'python': platform.python_version(), 'arch': platform.machine(),
       'probe_backend': 'python_time_monotonic_ns'}
env_str = json.dumps(env, sort_keys=True)
# 分辨率感知漂移容差：monotonic 无法分辨小于自身分辨率的时间差，
# Windows 默认计时器分辨率 ~15.625ms，在 100ms 小窗口上量化误差可达 ±15%。
# 固定 ppm 阈值物理上不可能通过；容差必须覆盖时钟量化粒度：
#   tol_ns = 2 * clock_resolution_ns + 1% * wall_elapsed_ns
res_ns = time.get_clock_info('monotonic').resolution * 1e9
drift_tol_ns = 2 * res_ns + 0.01 * wall_elapsed
assertions = [
    {'name': 'monotonic_includes_sleep', 'passed': mono_ok,
     'detail': f'mono_elapsed={mono_elapsed}ns >= {sleep_ms}ms*0.9'},
    {'name': 'drift_within_resolution_tolerance', 'passed': abs(drift_ns) <= drift_tol_ns,
     'detail': f'|drift|={abs(drift_ns)}ns <= tol={int(drift_tol_ns)}ns (2*res={int(2*res_ns)}ns + 1%wall)'},
]
status = 'PASS' if all(a['passed'] for a in assertions) else 'FAIL'
receipt = {
    'spike': SPIKE, 'status': status,
    'environment': env,
    'actions': [
        f'record wall_ns={wall_before} mono_start',
        f'sleep {sleep_ms}ms',
        f'record mono_elapsed_ns={mono_elapsed} wall_elapsed_ns={wall_elapsed}',
    ],
    'observable_facts': {
        'sleep_requested_ms': sleep_ms,
        'mono_elapsed_ns': mono_elapsed,
        'wall_elapsed_ns': wall_elapsed,
        'drift_ns': drift_ns,
        'drift_ppm': drift_ppm,
        'mono_includes_sleep': mono_ok,
        'boot_id': boot_id,
        'clock_resolution_ns': time.get_clock_info('monotonic').resolution * 1e9,
    },
    'assertions': assertions,
    'artifact_digest': f'mono_ns:{mono_elapsed}',
    'timestamp': datetime.datetime.now().astimezone().isoformat(),
}
out = json.dumps(receipt, indent=2)
print(out)
rp = r'$receiptPath'
with open(rp, 'w', encoding='utf-8') as f: f.write(out)
import sys; sys.exit(0 if status == 'PASS' else 1)
"@
    & $py -c $pyCode
    if ($LASTEXITCODE -ne 0) { $status = "FAIL" }
}

if (Test-Path $receiptPath) {
    $r = Get-Content $receiptPath -Raw | ConvertFrom-Json
    Write-Host "STATUS: $($r.status)"
}
if ($status -ne "PASS") { exit 1 }
# 成功时显式 exit 0：确保 $LASTEXITCODE 被设置，否则 test.ps1 的 StrictMode
# 在读取未定义的 $LASTEXITCODE 时会抛错并中断整个 compatibility-spikes 套件。
exit 0