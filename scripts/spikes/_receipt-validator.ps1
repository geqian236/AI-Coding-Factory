# scripts/spikes/_receipt-validator.ps1
# ---------------------------------------------------------------------------
# Shared spike-receipt evidence validator (GPT round-6 items 2 + 4).
#
# SINGLE SOURCE OF TRUTH: dot-sourced by BOTH scripts/phase0-acceptance.ps1
# (Windows PowerShell 5.1 on a GBK codepage) AND .github/workflows/ci.yml
# windows-probes (pwsh 7+). Before this file the runner and CI each hand-rolled
# their own receipt check and drifted into different fail-open holes (round-6
# item 4). Now both call Test-SpikeReceiptEvidence, so a hole can only be closed
# in one place.
#
# ASCII-only, BOM-less on purpose: PS 5.1 reads a BOM-less .ps1 as GBK on a
# Chinese Windows codepage and CJK bytes corrupt string terminators. Keeping
# this file pure ASCII removes the BOM dependency.
#
# Round-6 item 2 fixes (all verified against the committed receipts):
#   (a) STRICT boolean: a JSON string "false" is NOT accepted as passed. PS
#       treats any non-empty string as truthy (-not "false" -> $false), which is
#       exactly how {"passed":"false"} was forged into a PASS. We require the
#       assertion's `passed` to be a real [bool] (ConvertFrom-Json maps a JSON
#       boolean to [bool] and a JSON string stays [string]).
#   (b) ALL blockers allowlisted: a BLOCKED receipt is accepted only when EVERY
#       blocking subcheckId is in the frozen allowlist. The old code accepted as
#       long as ONE hit, so an allowlisted blocker + an unauthorized blocker slid
#       through.
#   (c) Wrapper exit code participates in the verdict: resolved PASS requires
#       exit==0; resolved BLOCKED (only env-compat spikes reach here) requires
#       exit in {0,2}; exit==1 (the wrappers' FAIL code) is always rejected.
#   (d) Run binding: when the caller passes the round's run identity, the receipt
#       must carry a matching runBinding (six probes, stamped by the runner) OR a
#       matching run_nonce (sqlite, which owns its nonce end-to-end).
# ---------------------------------------------------------------------------

Set-StrictMode -Version Latest

# Deep-validate one spike receipt. Returns @{ ok=<bool>; status=<string>; detail=<string> }.
function Test-SpikeReceiptEvidence {
    param(
        [Parameter(Mandatory = $true)] [string]   $Name,
        [Parameter(Mandatory = $true)] [string]   $Path,
        [Parameter(Mandatory = $true)] [bool]     $EnvCompat,     # true => BLOCKED_UNCERTIFIED allowed if all blocker ids allowlisted
        [string[]] $Allowlist = @(),                              # frozen allowlist of blocking subcheckIds
        [datetime] $RunStart = [datetime]::MinValue,              # receipt must be regenerated this round (mtime >= RunStart)
        [int]      $WrapperExitCode = 0,                          # observed wrapper exit code (item 2c)
        [string]   $ExpectRunId = "",                             # item 2d run binding expectations (empty => skip binding check)
        [string]   $ExpectRunNonce = "",
        [string]   $ExpectCandidateSha = "",
        [string]   $ExpectProbeDigest = "",                       # round-7 item 2: sqlite run_nonce branch must ALSO bind probeDigest
        [datetime] $ReceiptMtime = [datetime]::MinValue           # round-7 item 1: pre-stamp mtime (stamping refreshes mtime; freshness must use the wrapper's own write time, not the post-stamp one)
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return @{ ok = $false; status = "MISSING"; detail = "receipt file absent" }
    }
    try {
        $obj = Get-Content -LiteralPath $Path -Raw -Encoding utf8 | ConvertFrom-Json
    } catch {
        return @{ ok = $false; status = "UNPARSEABLE"; detail = "JSON parse failed" }
    }

    # Required fields: a minimal {"status":"PASS"} lacks these.
    foreach ($f in @("spike", "status", "assertions")) {
        if (-not ($obj.PSObject.Properties.Name -contains $f)) {
            return @{ ok = $false; status = "INVALID"; detail = "missing required field '$f'" }
        }
    }

    # The receipt's own spike name MUST match the expected node (self-report / wrong probe).
    if ([string]$obj.spike -ne $Name) {
        return @{ ok = $false; status = "NAME_MISMATCH"; detail = "receipt spike='$($obj.spike)' != expected '$Name'" }
    }

    # Freshness: regenerated during THIS run. A stale receipt from a prior round is
    # rejected, so the caller cannot pass on historical evidence.
    # Round-7 item 1: the runner stamps runBinding AFTER the wrapper writes, which
    # refreshes the file mtime - so the on-disk mtime can no longer prove the WRAPPER
    # regenerated it this round (a purge that silently failed + a stamp would refresh a
    # stale receipt's mtime and slip through). The caller therefore captures the
    # PRE-STAMP mtime (the wrapper's own write time) and passes it as $ReceiptMtime; we
    # use that when supplied, falling back to the on-disk mtime only when it is absent.
    if ($RunStart -ne [datetime]::MinValue) {
        $mtime = if ($ReceiptMtime -ne [datetime]::MinValue) { $ReceiptMtime } else { (Get-Item -LiteralPath $Path).LastWriteTime }
        if ($mtime -lt $RunStart) {
            return @{ ok = $false; status = "STALE"; detail = "receipt (pre-stamp) mtime $($mtime.ToString('o')) < runStart $($RunStart.ToString('o')) (wrapper did not regenerate this round)" }
        }
    }

    # Run binding: enforced when the caller supplies the round nonce.
    # SHAPE IS FROZEN BY SPIKE NAME, not by what fields the receipt happens to carry.
    # Routing by receipt content allows a forger to switch shape (e.g. give a
    # sqlite-labelled receipt runBinding instead of run_nonce, or give a non-sqlite
    # receipt only run_nonce + candidateSha to skip the runId check).
    #   - sqlite_wal_full  MUST use run_nonce + candidateSha + probeDigest  (never runBinding)
    #   - all other spikes MUST use runBinding with runId/runNonce/candidateSha/spike (never run_nonce)
    if ($ExpectRunNonce -ne "") {
        if ($Name -eq "sqlite_wal_full") {
            # SQLITE PATH - must have run_nonce; runBinding is wrong shape and rejected.
            if ($obj.PSObject.Properties.Name -contains "runBinding") {
                return @{ ok = $false; status = "BINDING_WRONG_SHAPE"; detail = "sqlite_wal_full must bind via run_nonce (not runBinding); found runBinding - possible receipt identity switch" }
            }
            if (-not ($obj.PSObject.Properties.Name -contains "run_nonce")) {
                return @{ ok = $false; status = "BINDING_MISSING"; detail = "sqlite_wal_full receipt missing run_nonce; not bound to this run" }
            }
            if ([string]$obj.run_nonce -ne $ExpectRunNonce) {
                return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "run_nonce='$($obj.run_nonce)' != '$ExpectRunNonce'" }
            }
            if ($ExpectCandidateSha -ne "") {
                $rcptSha = if ($obj.PSObject.Properties.Name -contains "candidateSha") { [string]$obj.candidateSha } else { "" }
                if ($rcptSha -ne $ExpectCandidateSha) {
                    return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "receipt candidateSha='$rcptSha' != '$ExpectCandidateSha'" }
                }
            }
            if ($ExpectProbeDigest -ne "") {
                $rcptProbe = if ($obj.PSObject.Properties.Name -contains "probeDigest") { [string]$obj.probeDigest } else { "" }
                if ($rcptProbe -ne $ExpectProbeDigest) {
                    return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "receipt probeDigest='$rcptProbe' != expected '$ExpectProbeDigest' (probe source mismatch/forgery)" }
                }
            }
        } else {
            # NON-SQLITE PATH - must have runBinding; run_nonce alone is wrong shape.
            if ($obj.PSObject.Properties.Name -contains "run_nonce" -and -not ($obj.PSObject.Properties.Name -contains "runBinding")) {
                return @{ ok = $false; status = "BINDING_WRONG_SHAPE"; detail = "'$Name' must bind via runBinding (not run_nonce); found only run_nonce - possible receipt identity switch (run_nonce path skips runId)" }
            }
            if (-not ($obj.PSObject.Properties.Name -contains "runBinding")) {
                return @{ ok = $false; status = "BINDING_MISSING"; detail = "no runBinding; '$Name' receipt not bound to this run" }
            }
            $rb = $obj.runBinding
            if ([string]$rb.runNonce -ne $ExpectRunNonce) {
                return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "runBinding.runNonce='$($rb.runNonce)' != '$ExpectRunNonce'" }
            }
            if ($ExpectRunId -ne "" -and [string]$rb.runId -ne $ExpectRunId) {
                return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "runBinding.runId='$($rb.runId)' != '$ExpectRunId'" }
            }
            if ($ExpectCandidateSha -ne "" -and [string]$rb.candidateSha -ne $ExpectCandidateSha) {
                return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "runBinding.candidateSha='$($rb.candidateSha)' != '$ExpectCandidateSha'" }
            }
            if ([string]$rb.spike -ne $Name) {
                return @{ ok = $false; status = "BINDING_MISMATCH"; detail = "runBinding.spike='$($rb.spike)' != '$Name'" }
            }
        }
    }

    $status     = [string]$obj.status
    $assertions = @($obj.assertions)
    if ($assertions.Count -lt 1) {
        return @{ ok = $false; status = $status; detail = "assertions empty (no observable evidence)" }
    }

    # (a) STRICT boolean: every assertion must carry `passed` AND it must be a real
    # [bool]. This rejects {"passed":"false"} (a [string], truthy in PS).
    foreach ($a in $assertions) {
        if (-not ($a.PSObject.Properties.Name -contains "passed")) {
            return @{ ok = $false; status = $status; detail = "an assertion lacks 'passed'" }
        }
        if (-not ($a.passed -is [bool])) {
            return @{ ok = $false; status = $status; detail = "an assertion 'passed' is not a JSON boolean (got '$($a.passed)' typed $($a.passed.GetType().Name)); string forgery rejected" }
        }
    }
    $failedAsserts = @($assertions | Where-Object { $_.passed -eq $false })
    $allPassed = ($failedAsserts.Count -eq 0)

    # Blocking subchecks come in two real shapes (verified against committed receipts):
    #   Shape A (tauri_e2e):      top-level "subcheckId" + a failed assertion.
    #   Shape B (sqlite_wal_full): a "subResults" entry with required==true and
    #     status BLOCKED_UNCERTIFIED/FAIL; its id is spike:<name>/<aspect>.
    # "uncertified_aspects" (no "required" field) is informational, never blocking.
    $blockingSubIds = @()
    if ($obj.PSObject.Properties.Name -contains "subResults") {
        foreach ($sr in @($obj.subResults)) {
            $srStatus = [string]$sr.status
            $srRequired = $false
            if ($sr.PSObject.Properties.Name -contains "required") { $srRequired = [bool]$sr.required }
            if ($srRequired -and ($srStatus -eq "BLOCKED_UNCERTIFIED" -or $srStatus -eq "FAIL")) {
                $blockingSubIds += "spike:$Name/$($sr.aspect)"
            }
        }
    }
    $topSubId = if ($obj.PSObject.Properties.Name -contains "subcheckId") { [string]$obj.subcheckId } else { $null }

    if ($status -eq "PASS") {
        if (-not $allPassed) {
            return @{ ok = $false; status = $status; detail = "status=PASS but $($failedAsserts.Count) assertion(s) failed" }
        }
        if ($blockingSubIds.Count -gt 0) {
            return @{ ok = $false; status = $status; detail = "status=PASS but required subResult blocked: $($blockingSubIds -join ',')" }
        }
        # (c) exit code participates: a PASS wrapper must exit 0.
        if ($WrapperExitCode -ne 0) {
            return @{ ok = $false; status = $status; detail = "status=PASS but wrapper exit=$WrapperExitCode (expected 0)" }
        }
        return @{ ok = $true; status = $status; detail = "PASS: $($assertions.Count) assertions all passed; exit=0" }
    }
    elseif ($status -eq "BLOCKED_UNCERTIFIED") {
        if (-not $EnvCompat) {
            return @{ ok = $false; status = $status; detail = "core spike may not be BLOCKED_UNCERTIFIED" }
        }
        # A BLOCKED must carry real blocking evidence (failed assertion or required blocked subResult).
        if ($failedAsserts.Count -eq 0 -and $blockingSubIds.Count -eq 0) {
            return @{ ok = $false; status = $status; detail = "BLOCKED but no failed assertion and no required blocked subResult (self-reported)" }
        }
        $candidateIds = @()
        if ($topSubId) { $candidateIds += $topSubId }
        $candidateIds += $blockingSubIds
        if ($candidateIds.Count -lt 1) {
            return @{ ok = $false; status = $status; detail = "BLOCKED but no subcheckId to allowlist-check" }
        }
        # (b) EVERY blocking id must be in the allowlist (not just one).
        $notAllowed = @($candidateIds | Where-Object { $Allowlist -notcontains $_ })
        if ($notAllowed.Count -gt 0) {
            return @{ ok = $false; status = $status; detail = "unauthorized blocker(s) not in allowlist: $($notAllowed -join ',')" }
        }
        # (c) exit code participates: BLOCKED wrappers exit 0 (tauri) or 2 (sqlite); reject 1 (FAIL).
        if ($WrapperExitCode -ne 0 -and $WrapperExitCode -ne 2) {
            return @{ ok = $false; status = $status; detail = "status=BLOCKED but wrapper exit=$WrapperExitCode (expected 0 or 2)" }
        }
        return @{ ok = $true; status = $status; detail = "BLOCKED_UNCERTIFIED all blockers allowlisted: $($candidateIds -join ','); exit=$WrapperExitCode" }
    }
    else {
        return @{ ok = $false; status = $status; detail = "status '$status' not PASS/BLOCKED_UNCERTIFIED" }
    }
}
