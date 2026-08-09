<#
.SYNOPSIS
    runner_identity 的共享 Windows/WSL2/Docker Desktop Linux-container 平台事实探针。

.DESCRIPTION
    CI preflight 和本地 wrapper 必须使用同一套事实判定，避免 hosted Windows、远端 Linux
    daemon 或 WSL1 被其中一方误认成可认证环境。本 helper 只读取宿主/CLI 状态，不写文件；
    调用方决定如何把返回的 facts/assertions 写入 receipt 或以非零退出。
#>
Set-StrictMode -Version Latest

function Invoke-RunnerIdentityNativeProbe {
    <#
    .SYNOPSIS
        以统一、不可抛出的方式执行 git/docker/wsl 原生命令。

    .DESCRIPTION
        平台 helper 既供 CI preflight 使用，也供本地 wrapper 写 BLOCKED receipt 使用。
        命令缺失、启动异常或非零退出必须成为可观察的失败事实，而不能在 receipt 前抛异常。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [string[]]$Arguments = @()
    )

    try {
        # Get-Command 在 Windows PATH 上可能同时返回 docker.exe 与无扩展名包装项；不能
        # 直接读取数组 .Path（会拼接成一个不存在的命令）。选择第一个具备路径的 Application，
        # 真正启动失败仍由下方 catch 归为可复核的 stable failure。
        $commands = @(Get-Command -Name $Name -CommandType Application -ErrorAction Stop)
        $command = $null
        foreach ($candidate in $commands) {
            if ($null -ne $candidate -and -not [string]::IsNullOrWhiteSpace([string]$candidate.Path)) {
                $command = $candidate
                break
            }
        }
        if ($null -eq $command) {
            return [pscustomobject]@{ ok = $false; text = ""; exitCode = -1; detail = "command missing: $Name" }
        }
        # 先保存原生命令退出码，再处理输出；管道/Select-Object 可能覆盖 $LASTEXITCODE。
        $raw = @(& $command.Path @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
        # 原生命令的多段 stdout 必须只用真实换行拼接；PowerShell 里的 `\` 不是转义前缀，
        # 写成 "\`n" 会在每段末尾留下反斜杠，进而让 WSL2 表格行的严格正则误判失败。
        $text = ((@($raw | ForEach-Object { [string]$_ }) -join "`n").Trim())
        if ($exitCode -ne 0) {
            return [pscustomobject]@{ ok = $false; text = $text; exitCode = $exitCode; detail = "command nonzero: $Name exit=$exitCode" }
        }
        return [pscustomobject]@{ ok = $true; text = $text; exitCode = 0; detail = "ok" }
    } catch {
        return [pscustomobject]@{ ok = $false; text = ""; exitCode = -1; detail = "command exception: $Name" }
    }
}

function Complete-RunnerIdentityPlatform {
    <#
    .SYNOPSIS
        将累计断言转换为稳定的返回对象，所有失败路径都经此出口，供 wrapper 写合法 receipt。
    #>
    param(
        [System.Collections.Generic.List[object]]$Assertions,
        [hashtable]$Facts
    )
    $failed = @($Assertions | Where-Object { -not $_.passed })
    $detail = if ($failed.Count -eq 0) {
        "Windows 11 + Docker Desktop Linux backend + WSL2 platform verified"
    } else {
        "platform preflight failed: " + (($failed | ForEach-Object { $_.name }) -join ",")
    }
    return [pscustomobject]@{
        ok = ($failed.Count -eq 0)
        facts = $Facts
        assertions = @($Assertions)
        detail = $detail
    }
}

function Add-RunnerIdentityUnevaluatedAssertions {
    <#
    .SYNOPSIS
        在前置失败后补足未执行的断言，避免 BLOCKED receipt 因空证据被 validator 判 INVALID。
    #>
    param(
        [scriptblock]$AddAssertion,
        [bool]$RequireImage,
        [string]$Reason
    )
    foreach ($name in @(
        "candidate_sha_exact", "docker_daemon_available", "docker_linux_backend",
        "docker_desktop_backend", "docker_desktop_context",
        "docker_desktop_local_endpoint", "docker_desktop_wsl2"
    )) {
        & $AddAssertion $name $false $Reason
    }
    if ($RequireImage) {
        & $AddAssertion "linux_container_image" $false $Reason
    }
}

function Test-RunnerIdentityPlatform {
    <#
    .SYNOPSIS
        返回 runner_identity 所需的平台事实、可复核断言和总 ok，不产生任何副作用。

    .DESCRIPTION
        认证语义固定为 Windows 11 workstation + WSL2 + Docker Desktop Linux container。
        先拒绝 Docker 客户端环境覆盖，再读取宿主、git、docker 与 wsl 事实；任一步失败
        都返回可持久化的 false assertion，绝不改连远端 daemon 或让 wrapper 异常退出。

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

    # facts 仅记录稳定且不含敏感值的元数据；DOCKER_* 只记录哪些变量被设置，不泄露 endpoint/cert 路径。
    $facts = [ordered]@{
        windows_host            = $false
        windows_nt              = $false
        windows_product_type    = ""
        windows_build           = ""
        candidate_sha           = ""
        docker_env_overrides    = @()
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

    # Docker CLI 的 DOCKER_HOST/CONTEXT/TLS 覆盖可把同名 context 指到远端 daemon；
    # 必须在任何 docker 调用之前 fail-closed，且不把变量值写入 receipt。
    # 强制数组化：无覆盖时 Where-Object 会输出 $null；在 StrictMode 下直接读取
    # $null.Count 会抛异常，反而让 wrapper 无法写出合法 BLOCKED receipt。
    $overrideNames = @(@(
        "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace([string][Environment]::GetEnvironmentVariable($_)) })
    $facts.docker_env_overrides = @($overrideNames)
    $overridesClear = ($overrideNames.Count -eq 0)
    $overrideDetail = if ($overridesClear) { "no Docker client override" } else { "set=" + ($overrideNames -join ",") }
    & $addAssertion "docker_environment_overrides_clear" $overridesClear $overrideDetail
    if (-not $overridesClear) {
        Add-RunnerIdentityUnevaluatedAssertions -AddAssertion $addAssertion -RequireImage $RequireImage -Reason "not evaluated because Docker client overrides are set"
        return Complete-RunnerIdentityPlatform -Assertions $assertions -Facts $facts
    }

    # $env:OS 可被进程覆盖，不能单独作为认证事实。真实要求是 Win32NT，随后再由 CIM
    # 核验 workstation ProductType=1 与 build>=22000（Windows 11）。
    $facts.windows_nt = ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT -and $env:OS -ceq "Windows_NT")
    & $addAssertion "windows_nt" $facts.windows_nt "platform=$([Environment]::OSVersion.Platform) OS=$($env:OS)"
    $windows11 = $false
    if ($facts.windows_nt) {
        try {
            $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
            $facts.windows_product_type = [string]$osInfo.ProductType
            $facts.windows_build = [string]$osInfo.BuildNumber
            $build = 0
            $buildParsed = [int]::TryParse($facts.windows_build, [ref]$build)
            $windows11 = ([int]$osInfo.ProductType -eq 1 -and $buildParsed -and $build -ge 22000)
        } catch {
            $windows11 = $false
        }
    }
    $facts.windows_host = $windows11
    & $addAssertion "windows_11_workstation" $windows11 (
        "productType=$($facts.windows_product_type) build=$($facts.windows_build)"
    )
    if (-not $windows11) {
        Add-RunnerIdentityUnevaluatedAssertions -AddAssertion $addAssertion -RequireImage $RequireImage -Reason "not evaluated because host is not Windows 11 workstation"
        return Complete-RunnerIdentityPlatform -Assertions $assertions -Facts $facts
    }

    if ($ExpectedCandidateSha -ne "") {
        $gitProbe = Invoke-RunnerIdentityNativeProbe -Name "git" -Arguments @("rev-parse", "HEAD")
        $facts.candidate_sha = $gitProbe.text
        $candidateOk = ($gitProbe.ok -and $facts.candidate_sha -ceq $ExpectedCandidateSha)
        & $addAssertion "candidate_sha_exact" $candidateOk (
            "actual=$($facts.candidate_sha) expected=$ExpectedCandidateSha exit=$($gitProbe.exitCode)"
        )
        if (-not $candidateOk) {
            foreach ($name in @(
                "docker_daemon_available", "docker_linux_backend", "docker_desktop_backend",
                "docker_desktop_context", "docker_desktop_local_endpoint", "docker_desktop_wsl2"
            )) { & $addAssertion $name $false "not evaluated because candidate SHA is not exact" }
            if ($RequireImage) { & $addAssertion "linux_container_image" $false "not evaluated because candidate SHA is not exact" }
            return Complete-RunnerIdentityPlatform -Assertions $assertions -Facts $facts
        }
    }

    $serverProbe = Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @("version", "--format", "{{.Server.Version}}")
    $facts.docker_server_version = $serverProbe.text
    $daemonOk = ($serverProbe.ok -and -not [string]::IsNullOrWhiteSpace($facts.docker_server_version))
    & $addAssertion "docker_daemon_available" $daemonOk "server=$($facts.docker_server_version) exit=$($serverProbe.exitCode)"
    if (-not $daemonOk) {
        foreach ($name in @(
            "docker_linux_backend", "docker_desktop_backend", "docker_desktop_context",
            "docker_desktop_local_endpoint", "docker_desktop_wsl2"
        )) { & $addAssertion $name $false "not evaluated because Docker daemon is unavailable" }
        if ($RequireImage) { & $addAssertion "linux_container_image" $false "not evaluated because Docker daemon is unavailable" }
        return Complete-RunnerIdentityPlatform -Assertions $assertions -Facts $facts
    }

    $ostypeProbe = Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @("info", "--format", "{{.OSType}}")
    $facts.docker_ostype = $ostypeProbe.text
    $linuxOk = ($ostypeProbe.ok -and $facts.docker_ostype -ceq "linux")
    & $addAssertion "docker_linux_backend" $linuxOk "OSType=$($facts.docker_ostype) exit=$($ostypeProbe.exitCode)"

    $operatingProbe = Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @("info", "--format", "{{.OperatingSystem}}")
    $facts.docker_operating_system = $operatingProbe.text
    $desktopOk = ($operatingProbe.ok -and $facts.docker_operating_system -match "Docker Desktop")
    & $addAssertion "docker_desktop_backend" $desktopOk "OperatingSystem=$($facts.docker_operating_system) exit=$($operatingProbe.exitCode)"

    $contextProbe = Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @("context", "show")
    $facts.docker_context = $contextProbe.text
    $contextOk = ($contextProbe.ok -and $facts.docker_context -in @("desktop-linux", "docker-desktop"))
    & $addAssertion "docker_desktop_context" $contextOk "context=$($facts.docker_context) exit=$($contextProbe.exitCode)"

    $endpointProbe = if ($contextOk) {
        Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @(
            "context", "inspect", "--format", "{{.Endpoints.docker.Host}}", $facts.docker_context
        )
    } else {
        [pscustomobject]@{ ok = $false; text = ""; exitCode = -1; detail = "context unavailable" }
    }
    $facts.docker_context_endpoint = $endpointProbe.text
    $normalizedEndpoint = (($facts.docker_context_endpoint -replace '\\', '/') -replace '/+', '/')
    $endpointOk = ($endpointProbe.ok -and $normalizedEndpoint -ceq "npipe:/./pipe/dockerDesktopLinuxEngine")
    & $addAssertion "docker_desktop_local_endpoint" $endpointOk (
        "normalized=$normalizedEndpoint exit=$($endpointProbe.exitCode)"
    )

    $wslProbe = Invoke-RunnerIdentityNativeProbe -Name "wsl.exe" -Arguments @("--list", "--verbose")
    # PS 5.1 有时把 WSL 的 UTF-16 输出带入 NUL；先消除传输伪字符再判定 v2。
    $wslRows = ($wslProbe.text -replace "`0", "")
    $facts.wsl_docker_desktop_v2 = ($wslProbe.ok -and $wslRows -match "(?m)^\s*\*?\s*docker-desktop\s+\S+\s+2\s*$")
    & $addAssertion "docker_desktop_wsl2" $facts.wsl_docker_desktop_v2 "wsl_exit=$($wslProbe.exitCode)"

    if ($RequireImage) {
        $imageProbe = Invoke-RunnerIdentityNativeProbe -Name "docker" -Arguments @(
            "image", "inspect", "--format", "{{.Os}}", "python:3.12-slim"
        )
        $facts.image_os = $imageProbe.text
        $facts.image_available = ($imageProbe.ok -and -not [string]::IsNullOrWhiteSpace($facts.image_os))
        & $addAssertion "linux_container_image" (
            $facts.image_available -and $facts.image_os -ceq "linux"
        ) "image_os=$($facts.image_os) exit=$($imageProbe.exitCode)"
    }

    return Complete-RunnerIdentityPlatform -Assertions $assertions -Facts $facts
}
