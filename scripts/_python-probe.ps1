# scripts/_python-probe.ps1
# Pure ASCII, no BOM: dot-sourced by both BOM'd CJK files (check.ps1) and no-BOM
# ASCII files (phase0-acceptance.ps1), plus the regression test via subprocess.
# ASCII is a subset of every encoding those files use, so this loads identically
# under PS 5.1 (GBK codepage) and pwsh 7.
#
# GPT round-13 P1-1 (single source of truth for the uv-locked Python probe):
#   Round-11/12 hardened the "which Python is the uv-locked env" probe in TWO places
#   (B-2 in phase0-acceptance.ps1, the pre-gate probe in check.ps1) with byte-identical
#   inline code, and the round-12 regression test RE-IMPLEMENTED that logic a third
#   time. A mutation (revert a production entry point to the vulnerable 2>&1 / drop the
#   strict parse) therefore would NOT be caught by the test - the test only exercised
#   its own copy. Extracting the probe here makes it the ONE place the logic lives; both
#   entry points AND the test call Get-UvLockedPythonVersion, and the test also asserts
#   the entry points actually reference it (see test_python_pin_probe.py).
Set-StrictMode -Version Latest

function Get-UvLockedPythonVersion {
    <#
    .SYNOPSIS
        Probe the uv-locked env's Python version, hardened against stderr pollution
        and pinned to the frozen major.minor from .python-version.
    .DESCRIPTION
        The caller must have already set $env:UV_PYTHON and run `uv sync --locked`
        (so the .venv exists). This function then:
          - runs `uv run --locked python` with stderr SEPARATED (2>$null), so a uv
            warning (e.g. a mismatched VIRTUAL_ENV "will be ignored") can never merge
            into the version stdout (GPT round-12 P2). NEVER 2>&1 here.
          - strict-parses EXACTLY one ^\d+\.\d+\.\d+$ line from stdout; zero / multiple
            / malformed => not ok (fail-closed), so a polluted or ambiguous string is
            never accepted as the version.
          - reads the frozen contract major.minor from .python-version (GPT round-13
            P1-2: do NOT hardcode a patch version - any 3.12.x patch is legal, a 3.13
            is rejected). The in-probe sys.exit and the returned .ok both enforce it.
        Capture-then-read-$LASTEXITCODE-then-parse: never pipe the native command
        straight into Select-Object before reading the exit code (that trips
        StopUpstreamCommandsException and pollutes $LASTEXITCODE; see the round-10
        version-probe bug and test-runner-identity.ps1's docker probe).
    .PARAMETER RepoRoot
        Repo root holding .python-version (the frozen major.minor contract).
    .OUTPUTS
        Hashtable: ok(bool), version(string|$null), exitCode(int), matchCount(int),
        expectedMajorMinor(string).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot
    )
    # Frozen contract: major.minor from .python-version (e.g. "3.12"); default "3.12".
    $pyVerFile = Join-Path $RepoRoot ".python-version"
    $reqRaw = if (Test-Path -LiteralPath $pyVerFile) {
        (Get-Content -LiteralPath $pyVerFile -Raw -Encoding utf8).Trim()
    } else { "3.12" }
    # Keep only major.minor even if .python-version carries a patch or suffix.
    $mm = if ($reqRaw -match '^(\d+)\.(\d+)') { "$($Matches[1]).$($Matches[2])" } else { "3.12" }
    $parts = $mm -split '\.'
    $major = [int]$parts[0]
    $minor = [int]$parts[1]

    # Probe source: print version, exit 0 ONLY if major.minor matches the frozen
    # contract. Built as a PS variable (not inline in the command line) to avoid
    # quote-nesting fragility; $major/$minor interpolate, the '%d.%d.%d' single quotes
    # stay literal inside this double-quoted PS string.
    $code = "import sys; print('%d.%d.%d' % sys.version_info[:3]); sys.exit(0 if sys.version_info[:2]==($major,$minor) else 3)"
    $raw = & uv run --locked python -c $code 2>$null
    $exitCode = $LASTEXITCODE
    $verLines = @(("$raw" -split "`r?`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ -match '^\d+\.\d+\.\d+$' })
    $version = if ($verLines.Count -eq 1) { $verLines[0] } else { $null }
    # ok requires ALL of: clean exit 0, exactly one strict-version line, and that
    # version's major.minor equals the frozen contract (belt-and-suspenders with the
    # in-probe sys.exit, so a broken exit code alone cannot pass a wrong version).
    $ok = ($exitCode -eq 0) -and ($verLines.Count -eq 1) -and ($null -ne $version) -and ($version -match "^$major\.$minor\.\d+$")
    return @{
        ok                 = $ok
        version            = $version
        exitCode           = $exitCode
        matchCount         = $verLines.Count
        expectedMajorMinor = $mm
    }
}
