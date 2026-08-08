# scripts/_python-probe.ps1
# 编码：UTF-8 with BOM（GPT 第十四轮 P1）。本文件含中文注释，必须带 BOM——否则
# PowerShell 5.1 在 GBK 代码页下会把无 BOM 的 UTF-8 中文误解码成乱码。带 BOM 后
# powershell.exe（5.1）与 pwsh 7 都能无歧义按 UTF-8 读取。被 BOM 的 CJK 文件
# （check.ps1）、无 BOM 的 ASCII 文件（phase0-acceptance.ps1）以及回归测试
# （subprocess dot-source）共同引用；BOM 检测是逐文件的，跨文件 dot-source 不受影响。
#
# GPT 第十三轮 P1-1（uv 锁定 Python 探针的单一真源）：
#   第十一/十二轮把"判定 uv 锁定环境是哪个 Python"的探针在两处（phase0-acceptance.ps1
#   的 B-2、check.ps1 的门禁前置探针）各写了一份字节相同的内联代码，第十二轮的回归测试
#   又第三次重实现同一逻辑。于是任一入口回退到有漏洞的 2>&1 / 去掉严格解析，测试都抓不到
#   （测试只跑自己那份）。把探针抽到本文件成为唯一真源：两个正式入口与测试都调用
#   Get-UvLockedPythonVersion，测试再断言两入口确实引用它（见 test_python_pin_probe.py）。
#
# GPT 第十四轮 P1（.python-version 契约 fail-closed）：
#   旧版在 .python-version 缺失/非法时静默回退到 "3.12"——这会在契约文件被删或损坏时
#   仍放行。现改为：缺失、空、不可读、格式非法一律 fail-closed（ok=$false + contractError），
#   且在跑 uv 探针**之前**就返回（fail-fast），绝不静默默认任何版本。
Set-StrictMode -Version Latest

function Get-UvLockedPythonVersion {
    <#
    .SYNOPSIS
        探测 uv 锁定环境的 Python 版本；抗 stderr 污染，并按 .python-version 冻结的
        major.minor 判定。缺失/非法契约一律 fail-closed。
    .DESCRIPTION
        调用方须已设 $env:UV_PYTHON 并跑过 `uv sync --locked`（.venv 已就绪）。本函数：
          1) 读取并**严格校验** .python-version（冻结契约）——缺失/空/不可读/格式非法
             任一情形立即返回 ok=$false + contractError，不跑探针、不默认版本
             （GPT 第十四轮 P1：杜绝契约损坏时的静默放行）。
          2) 在 uv 锁定环境跑 `uv run --locked python`，stderr 用 2>$null **分离**——
             uv 的 warning（如 VIRTUAL_ENV 不匹配时 "will be ignored"）绝不并入版本
             stdout（GPT 第十二轮 P2）。此处**绝不**用 2>&1。
          3) 从 stdout **严格解析**恰好一行 ^\d+\.\d+\.\d+$；零行/多行/格式异常均判 not ok
             （fail-closed），污染或歧义字符串绝不被当作版本。
          4) 判定该版本的 major.minor 等于 .python-version 冻结契约（GPT 第十三轮 P1-2：
             不硬编码补丁号——任意 3.12.x 补丁合法，3.13 被拒）。探针内的 sys.exit 与
             返回的 .ok 双重把关，坏退出码单独也不能放行错版本。
        先落变量、立即读 $LASTEXITCODE、再解析：绝不把 native 命令直接管道给
        Select-Object（那会触发 StopUpstreamCommandsException 污染 $LASTEXITCODE；
        见第十轮版本探针 bug 与 test-runner-identity.ps1 的 docker 探针）。
    .PARAMETER RepoRoot
        持有 .python-version（冻结 major.minor 契约）的仓库根。
    .OUTPUTS
        Hashtable：ok(bool)、version(string|$null)、exitCode(int)、matchCount(int)、
        expectedMajorMinor(string|$null)、contractError(string|$null)。
        contractError 非 $null 表示 .python-version 契约无效（fail-closed，未跑探针）。
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    # ── 步骤 1：读取并严格校验冻结契约 .python-version（fail-closed）──────────────
    # 缺失/空/不可读/格式非法一律返回 ok=$false + contractError，且不跑探针、不默认版本。
    $pyVerFile = Join-Path $RepoRoot ".python-version"
    $contractError = $null
    $mm = $null
    $major = $null
    $minor = $null
    if (-not (Test-Path -LiteralPath $pyVerFile)) {
        $contractError = "missing"
    } else {
        $reqRaw = $null
        try {
            $reqRaw = (Get-Content -LiteralPath $pyVerFile -Raw -Encoding utf8)
        } catch {
            $contractError = "unreadable"
        }
        if ($null -eq $contractError) {
            # 取第一条非空行（容忍尾随换行/空行），再严格匹配 major.minor[.patch]。
            $firstLine = (("$reqRaw" -split "`r?`n") |
                ForEach-Object { $_.Trim() } |
                Where-Object { $_ -ne "" } |
                Select-Object -First 1)
            if (-not $firstLine) {
                $contractError = "empty"
            } elseif ($firstLine -match '^(\d+)\.(\d+)(?:\.\d+)?$') {
                $mm = "$($Matches[1]).$($Matches[2])"
                $major = [int]$Matches[1]
                $minor = [int]$Matches[2]
            } else {
                $contractError = "malformed:$firstLine"
            }
        }
    }
    if ($contractError) {
        # fail-closed：契约无效即返回，绝不跑探针、绝不默认任何版本。
        return @{
            ok                 = $false
            version            = $null
            exitCode           = -1
            matchCount         = 0
            expectedMajorMinor = $null
            contractError      = $contractError
        }
    }

    # ── 步骤 2-4：在 uv 锁定环境探测版本并严格校验 ────────────────────────────────
    # 探针源码：打印版本，仅当 major.minor 等于冻结契约时 exit 0。以 PS 变量承载（不内联
    # 到命令行）以避免引号嵌套脆弱；$major/$minor 插值，'%d.%d.%d' 的单引号在此双引号
    # PS 字符串里保持字面。
    $code = "import sys; print('%d.%d.%d' % sys.version_info[:3]); sys.exit(0 if sys.version_info[:2]==($major,$minor) else 3)"
    $raw = & uv run --locked python -c $code 2>$null
    $exitCode = $LASTEXITCODE
    $verLines = @(("$raw" -split "`r?`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+\.\d+\.\d+$' })
    $version = if ($verLines.Count -eq 1) { $verLines[0] } else { $null }
    # ok 需同时满足：干净 exit 0、恰一行严格版本、且该版本 major.minor 等于冻结契约
    # （与探针内 sys.exit 双保险：坏退出码单独也不能放行错版本）。
    $ok = ($exitCode -eq 0) -and ($verLines.Count -eq 1) -and ($null -ne $version) -and ($version -match "^$major\.$minor\.\d+$")
    return @{
        ok                 = $ok
        version            = $version
        exitCode           = $exitCode
        matchCount         = $verLines.Count
        expectedMajorMinor = $mm
        contractError      = $null
    }
}
