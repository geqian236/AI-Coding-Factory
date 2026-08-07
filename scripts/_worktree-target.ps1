# scripts/_worktree-target.ps1
# Pure ASCII, no BOM: dot-sourced by both BOM'd CJK files (dev.ps1, check.ps1,
# the durable-io / runner-identity wrappers) and no-BOM ASCII files
# (phase0-acceptance.ps1). ASCII is a subset of every encoding those files use,
# so this loads identically under PS 5.1 (GBK codepage) and pwsh 7.
#
# GPT round-9 P0 (shared Cargo target breaks cross-worktree isolation):
#   Every worktree previously pointed CARGO_TARGET_DIR at one shared
#   <DATA_ROOT>\cargo-target. Rust test binaries bake env!("CARGO_MANIFEST_DIR")
#   - the COMPILE-TIME worktree path - into themselves and read
#   contracts/golden/*.json through it (see crates/factory-contracts/tests/
#   event_vectors.rs). With a shared target dir, worktree B can RUN a binary
#   compiled by worktree A and read A's golden files. If A was deleted, B panics
#   on a vanished path (false FAIL); if A still exists, B silently executes
#   another checkout's code and reads its golden (false PASS / bad acceptance
#   evidence). Both were reproduced by the reviewer.
#
#   Fix: namespace the target dir by a digest of the NORMALIZED worktree root, so
#   each checkout gets its own <DATA_ROOT>\cargo-target\<digest>. A binary is then
#   only ever run from the exact tree that compiled it. CARGO_HOME / RUSTUP_HOME
#   stay SHARED - the toolchain itself is worktree-independent, only build output
#   must be isolated.
#
#   The digest is the SHA-256 of the UTF-8 bytes of the normalized path, computed
#   via .NET (never a captured native-command stdout), so PS 5.1's GBK codepage
#   cannot corrupt it: the CJK worktree path lives as a .NET string in memory and
#   Encoding.UTF8.GetBytes reads it deterministically on any codepage.
Set-StrictMode -Version Latest

function Get-WorktreeTargetDir {
    <#
    .SYNOPSIS
        Per-worktree Cargo target dir under the shared DATA_ROOT.
    .PARAMETER WorktreeRoot
        Absolute path to the worktree (repo) root. Normalized before hashing so
        two spellings of the same tree (case / trailing slash) map to one digest.
    .PARAMETER DataRoot
        Shared <PROJECT_ROOT>\AI-Coding-Factory-Data\dev root.
    .OUTPUTS
        <DataRoot>\cargo-target\<12-hex-digest>
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$WorktreeRoot,
        [Parameter(Mandatory = $true)][string]$DataRoot
    )
    # Normalize: full path, drop trailing separator, lowercase. Windows paths are
    # case-insensitive, so C:\A and c:\a\ are the SAME tree and must not split
    # into two target namespaces (which would defeat the shared-toolchain reuse).
    $normalized = [System.IO.Path]::GetFullPath($WorktreeRoot).TrimEnd('\').ToLowerInvariant()
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    # 6 bytes = 12 hex chars = 2^48 space: collision-free for the handful of
    # worktrees any dev/CI host ever holds, short enough to keep paths sane.
    $digest = -join ($hashBytes[0..5] | ForEach-Object { $_.ToString('x2') })
    return (Join-Path (Join-Path $DataRoot 'cargo-target') $digest)
}
