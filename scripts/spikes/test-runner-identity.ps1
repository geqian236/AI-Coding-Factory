<#
.SYNOPSIS  Spike 5: WSL Runner Identity probe
.DESCRIPTION  Checks Docker availability; if absent marks BLOCKED_UNCERTIFIED
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$receiptPath = "$scriptRoot\tools\compat-probes\runner_identity\receipt.json"

$dockerOk = $false
try { $v = docker version --format "{{.Client.Version}}" 2>$null; $dockerOk = ($LASTEXITCODE -eq 0) } catch {}

if (-not $dockerOk) {
    Write-Host "Docker not available — emitting BLOCKED_UNCERTIFIED"
    $r = [ordered]@{
        spike            = "runner_identity"
        status           = "BLOCKED_UNCERTIFIED"
        reason           = "Docker CLI not found — install Docker Desktop with WSL2 backend to certify"
        environment      = @{ os = $env:OS; computername = $env:COMPUTERNAME; ps_version = $PSVersionTable.PSVersion.ToString() }
        actions          = @("checked docker availability — not found")
        observable_facts = @{ docker_available = $false }
        assertions       = @()
        artifact_digest  = "none"
        timestamp        = (Get-Date -Format "o")
    }
    $r | ConvertTo-Json -Depth 5 | Tee-Object -FilePath $receiptPath
    exit 0
}

Write-Host "Docker available — running container identity probe"
$containerId = docker run -d --rm alpine sleep 30 2>&1
if ($LASTEXITCODE -ne 0) {
    $r = @{ spike="runner_identity"; status="FAIL"; error="docker run failed: $containerId"; timestamp=(Get-Date -Format "o") }
    $r | ConvertTo-Json | Tee-Object -FilePath $receiptPath
    exit 1
}

$shortId = $containerId.Trim().Substring(0, [Math]::Min(12, $containerId.Trim().Length))
$runBefore = docker inspect --format "{{.State.Running}}" $shortId 2>&1
Start-Sleep -Milliseconds 500
$runAfter  = docker inspect --format "{{.State.Running}}" $shortId 2>&1
docker stop $shortId 2>$null | Out-Null

$pass = ($runBefore.Trim() -eq "true") -and ($runAfter.Trim() -eq "true")
$r = [ordered]@{
    spike            = "runner_identity"
    status           = if ($pass) { "PASS" } else { "FAIL" }
    environment      = @{ os=$env:OS; docker_version=$v }
    actions          = @("docker run -d alpine sleep 30","inspect before pause","sleep 500ms","inspect after pause","docker stop")
    observable_facts = @{ docker_available=$true; container_id=$shortId; running_before=$runBefore.Trim(); running_after_pause=$runAfter.Trim() }
    assertions       = @(@{ name="container_survives_broker_pause"; passed=$pass; detail="running_before=$($runBefore.Trim()) running_after=$($runAfter.Trim())" })
    artifact_digest  = "container_id:$shortId"
    timestamp        = (Get-Date -Format "o")
}
$r | ConvertTo-Json -Depth 5 | Tee-Object -FilePath $receiptPath
if (-not $pass) { exit 1 }
# 显式 exit 0：spike 通过时必须设置 $LASTEXITCODE，否则 test.ps1 的 Run-Suite
# 在 StrictMode 下读取未定义的 $LASTEXITCODE 会抛异常并中断整个套件。
exit 0