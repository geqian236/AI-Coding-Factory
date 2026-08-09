<#
.SYNOPSIS
    runner_identity 的共享 Windows/WSL2/Docker Desktop Linux-container 平台事实探针。

.DESCRIPTION
    CI preflight 和本地 wrapper 必须使用同一套事实判定，避免 hosted Windows、远端 Linux
    daemon 或 WSL1 被其中一方误认成可认证环境。本 helper 只读取宿主/CLI 状态，不写文件；
    调用方决定如何把返回的 facts/assertions 写入 receipt 或以非零退出。
#>
Set-StrictMode -Version Latest

function Test-RunnerIdentityPlatform {
    <#
    .SYNOPSIS
        返回 runner_identity 所需的平台事实、可复核断言和总 ok，不产生任何副作用。

    .PARAMETER ExpectedCandidateSha
        CI 传入 checkout 的精确 SHA；本地独立运行可留空，不把手动 probe 误判为候选漂移。

    .PARAMETER RequireImage
        wrapper 与 pull 后的 CI 校验为 true，要求 python:3.12-slim 的 image OS 为 linux；
        pull 前 CI 传 false，先只验证 backend，避免冷镜像影响平台身份判定。
    #>
    [CmdletBinding()]
    param(
        [string]$ExpectedCandidateSha = "",
        [bool]$RequireImage = $true
    )

    # facts 使用稳定、脱敏字段，既可直接进入 receipt，也可让 CI 仅输出失败摘要。
    $facts = [ordered]@{
        windows_host            = $false
        candidate_sha           = ""
        docker_server_version   = ""
        docker_ostype           = ""
        docker_operating_system = ""
        docker_context          = ""
        docker_context_endpoint = ""
        wsl_docker_desktop_v2   = $false
        image_available         = $false
        image_os                = ""
    }
    $assertions = [System.Collections.Generic.List[object]]::new()
    $addAssertion = {
        param([string]$Name, [bool]$Passed, [string]$Detail)
        $assertions.Add([ordered]@{ name = $Name; passed = $Passed; detail = $Detail })
    }

    $facts.windows_host = ($env:OS -match "Windows")
    & $addAssertion "windows_host" $facts.windows_host "OS=$($env:OS)"
    if (-not $facts.windows_host) {
        return [pscustomobject]@{
            ok = $false; facts = $facts; assertions = @($assertions)
            detail = "runner_identity requires a Windows host"
        }
    }

    if ($ExpectedCandidateSha -ne "") {
        $headRaw = git rev-parse HEAD 2>$null
        $headExit = $LASTEXITCODE
        $facts.candidate_sha = [string]($headRaw | Select-Object -First 1)
        $candidateOk = ($headExit -eq 0 -and $facts.candidate_sha -ceq $ExpectedCandidateSha)
        & $addAssertion "candidate_sha_exact" $candidateOk "actual=$($facts.candidate_sha) expected=$ExpectedCandidateSha exit=$headExit"
    }

    $serverRaw = docker version --format "{{.Server.Version}}" 2>$null
    $serverExit = $LASTEXITCODE
    $facts.docker_server_version = [string]($serverRaw | Select-Object -First 1)
    $daemonOk = ($serverExit -eq 0 -and -not [string]::IsNullOrWhiteSpace($facts.docker_server_version))
    & $addAssertion "docker_daemon_available" $daemonOk "server=$($facts.docker_server_version) exit=$serverExit"

    if ($daemonOk) {
        $ostypeRaw = docker info --format "{{.OSType}}" 2>$null
        $ostypeExit = $LASTEXITCODE
        $facts.docker_ostype = [string]($ostypeRaw | Select-Object -First 1)
        & $addAssertion "docker_linux_backend" ($ostypeExit -eq 0 -and $facts.docker_ostype -ceq "linux") "OSType=$($facts.docker_ostype) exit=$ostypeExit"

        $operatingRaw = docker info --format "{{.OperatingSystem}}" 2>$null
        $operatingExit = $LASTEXITCODE
        $facts.docker_operating_system = [string]($operatingRaw | Select-Object -First 1)
        & $addAssertion "docker_desktop_backend" ($operatingExit -eq 0 -and $facts.docker_operating_system -match "Docker Desktop") "OperatingSystem=$($facts.docker_operating_system) exit=$operatingExit"

        $contextRaw = docker context show 2>$null
        $contextExit = $LASTEXITCODE
        $facts.docker_context = [string]($contextRaw | Select-Object -First 1)
        $contextOk = ($contextExit -eq 0 -and $facts.docker_context -in @("desktop-linux", "docker-desktop"))
        & $addAssertion "docker_desktop_context" $contextOk "context=$($facts.docker_context) exit=$contextExit"

        # 名为 desktop-linux 的远端 context 仍可指向 TCP/SSH daemon；必须核验 Docker
        # Desktop Linux engine 的本机 named-pipe，斜杠形式归一后才允许等价写法。
        $endpointRaw = docker context inspect --format "{{.Endpoints.docker.Host}}" $facts.docker_context 2>$null
        $endpointExit = $LASTEXITCODE
        $facts.docker_context_endpoint = [string]($endpointRaw | Select-Object -First 1)
        $normalizedEndpoint = (($facts.docker_context_endpoint -replace '\\', '/') -replace '/+', '/')
        $endpointOk = ($endpointExit -eq 0 -and $normalizedEndpoint -ceq "npipe:/./pipe/dockerDesktopLinuxEngine")
        & $addAssertion "docker_desktop_local_endpoint" $endpointOk "endpoint=$($facts.docker_context_endpoint) normalized=$normalizedEndpoint exit=$endpointExit"

        # PowerShell 5.1 有时把 WSL 的 UTF-16 输出带入 NUL；先消除传输伪字符再判定 v2。
        $wslRows = ((wsl.exe --list --verbose 2>$null | Out-String) -replace "`0", "")
        $wslExit = $LASTEXITCODE
        $facts.wsl_docker_desktop_v2 = ($wslExit -eq 0 -and $wslRows -match "(?m)^\s*\*?\s*docker-desktop\s+\S+\s+2\s*$")
        & $addAssertion "docker_desktop_wsl2" $facts.wsl_docker_desktop_v2 "wsl_exit=$wslExit docker-desktop-v2=$($facts.wsl_docker_desktop_v2)"
    } else {
        # Docker daemon 不可达时剩余平台断言必须显式为 false，不能因未执行而从 receipt 消失。
        foreach ($name in @("docker_linux_backend", "docker_desktop_backend", "docker_desktop_context", "docker_desktop_local_endpoint", "docker_desktop_wsl2")) {
            & $addAssertion $name $false "not evaluated because Docker daemon is unavailable"
        }
    }

    if ($RequireImage) {
        $imageRaw = docker image inspect --format "{{.Os}}" "python:3.12-slim" 2>$null
        $imageExit = $LASTEXITCODE
        $facts.image_os = [string]($imageRaw | Select-Object -First 1)
        $facts.image_available = ($imageExit -eq 0 -and -not [string]::IsNullOrWhiteSpace($facts.image_os))
        & $addAssertion "linux_container_image" ($facts.image_available -and $facts.image_os -ceq "linux") "image_os=$($facts.image_os) exit=$imageExit"
    }

    $failed = @($assertions | Where-Object { -not $_.passed })
    $detail = if ($failed.Count -eq 0) {
        "Windows + Docker Desktop Linux backend + WSL2 platform verified"
    } else {
        "platform preflight failed: " + (($failed | ForEach-Object { $_.name }) -join ",")
    }
    return [pscustomobject]@{
        ok = ($failed.Count -eq 0)
        facts = $facts
        assertions = @($assertions)
        detail = $detail
    }
}
