<#
.SYNOPSIS
    Phase 0 total gate script - 9 contract-layer checks, all must pass before commit.

.DESCRIPTION
    Runs 9 gate checks for Phase 0:
    1. Codegen drift     - python contracts/codegen/generate.py --check
    2. Schema validity   - validate all 13 JSON schemas via pytest
    3. Golden vectors    - pytest test_plan_hash_vectors / test_event_hash_vectors
    4. Chinese coverage  - AST scan; every public function/class needs Chinese docstring
    5. No bare print     - no print()/console.log() outside test code
    6. Secret scan       - 0 hits for token/password/private_key in non-test source
    7. C-drive paths     - no controlled C-drive writes in source
    8. Catalog 47 IDs    - required-test-catalog.v1.json has exactly 47 unique IDs
    9. No doc placeholders - 0 TODO/FIXME/TBD/PLACEHOLDER in docs/

    All checks must pass before commit. Failures are summarised at the end.

.NOTES
    Version: 2.0 (Phase 0 total gate)
    Algorithm version: v1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$REPO_ROOT = Split-Path $PSScriptRoot -Parent
$failures = [System.Collections.Generic.List[string]]::new()
$gate_results = [System.Collections.Generic.List[hashtable]]::new()

function Invoke-GateCheck {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "-- Check: $Name --"
    try {
        & $Action
        $code = $LASTEXITCODE
        if ($null -eq $code) { $code = 0 }
        if ($code -ne 0) {
            $failures.Add($Name)
            $gate_results.Add(@{ name = $Name; result = "FAIL" })
            Write-Warning "  [$Name] FAIL (exit $code)"
        } else {
            $gate_results.Add(@{ name = $Name; result = "PASS" })
            Write-Host "  [$Name] PASS"
        }
    } catch {
        Write-Warning "  [$Name] ERROR: $_"
        $failures.Add($Name)
        $gate_results.Add(@{ name = $Name; result = "FAIL" })
    }
}

Set-Location $REPO_ROOT

# 1. Codegen drift
Invoke-GateCheck "1-codegen-drift" {
    python contracts/codegen/generate.py --check
}

# 2. Schema validity
Invoke-GateCheck "2-schema-validity" {
    python -m pytest tests/contract/test_schema_catalog.py -k "schema" -q --tb=short --no-header
}

# 3. Golden vectors
# fail-closed：plan_hash / event_hash 向量测试文件必须存在并通过；缺任一即整体红
# （GPT 审核指出此分支原为 SKIP-即-PASS 的 fail-open 漏洞）。
Invoke-GateCheck "3-golden-vectors" {
    $ph = "tests\contract\test_plan_hash_vectors.py"
    $ev = "tests\contract\test_event_hash_vectors.py"
    $missing = @()
    if (-not (Test-Path $ph)) { $missing += $ph }
    if (-not (Test-Path $ev)) { $missing += $ev }
    if ($missing.Count -gt 0) {
        Write-Host "  [FAIL] Golden vectors 测试文件缺失：$($missing -join ', ')"
        # 必须让 pytest 真跑（即便 no collection）以触发非零退出
        python -m pytest $ph $ev -q --tb=short --no-header
    } else {
        python -m pytest $ph $ev -q --tb=short --no-header
    }
}

# 4. Chinese coverage (via helper script)
Invoke-GateCheck "4-chinese-coverage" {
    python scripts/_gate_checks.py chinese-coverage
}

# 5. No bare print/console.log (via helper script)
Invoke-GateCheck "5-no-bare-print" {
    python scripts/_gate_checks.py no-bare-print
}

# 6. Secret scan (via helper script)
Invoke-GateCheck "6-secret-scan" {
    python scripts/_gate_checks.py secret-scan
}

# 7. C-drive paths (via helper script)
Invoke-GateCheck "7-c-drive-paths" {
    python scripts/_gate_checks.py c-drive-paths
}

# 8. Catalog 47 IDs
Invoke-GateCheck "8-catalog-47-ids" {
    python -m pytest "tests/contract/test_schema_catalog.py::test_required_test_catalog_has_exactly_47_ids" "tests/contract/test_schema_catalog.py::test_required_test_catalog_all_ids_unique" -q --tb=short --no-header
}

# 9. No doc placeholders
Invoke-GateCheck "9-no-doc-placeholders" {
    python -m pytest tests/contract/test_no_placeholders.py -q --tb=short --no-header
}

# Summary
Write-Host ""
Write-Host "========================================"
Write-Host "Gate results:"
foreach ($r in $gate_results) {
    $col = if ($r.result -eq "PASS") { "Green" } else { "Red" }
    Write-Host ("  {0,-30} {1}" -f $r.name, $r.result) -ForegroundColor $col
}
Write-Host "========================================"
if ($failures.Count -gt 0) {
    Write-Host "[check.ps1] FAIL - $($failures.Count)/9 check(s) failed" -ForegroundColor Red
    exit 1
}
Write-Host "[check.ps1] PASS - all 9 gate checks passed!" -ForegroundColor Green
exit 0
