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
        Absolute path to the worktree (repo) root. Normalized (GetFullPath +
        TrimEnd '\') before hashing so a trailing separator does not split one
        tree into two digests. Case is NOT folded (round-10 P1): two genuinely
        different worktrees on a case-sensitive root must NOT collide, and every
        caller derives this from its own $PSScriptRoot so one tree keeps one
        spelling anyway.
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
    # Normalize: full path + drop trailing separator ONLY. Do NOT lowercase.
    # GPT round-10 P1: an unconditional .ToLowerInvariant() would map two GENUINELY
    # DIFFERENT worktrees on a case-sensitive root (e.g. NTFS dir with per-directory
    # case sensitivity enabled, or a case-sensitive network/dev-drive mount) to the
    # SAME digest -> shared target -> the very cross-worktree pollution this helper
    # exists to prevent. Consistency across callers does not need lowercasing: every
    # caller derives WorktreeRoot from its own $PSScriptRoot on disk, so the same tree
    # yields the same spelling (Windows GetFullPath preserves case), and two spellings
    # differing only in case never arise for one tree in practice. Dropping lowercase
    # is therefore strictly safer: identical trees still collapse, different trees stay
    # distinct on any filesystem.
    $normalized = [System.IO.Path]::GetFullPath($WorktreeRoot).TrimEnd('\')
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hashBytes = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    # 6 bytes = 12 hex chars = 2^48 space. Collision-RESISTANT, not collision-free:
    # birthday-bound probability is negligible for the handful of worktrees any dev/CI
    # host ever holds (~1e-14 at 100 worktrees), and short enough to keep paths sane.
    $digest = -join ($hashBytes[0..5] | ForEach-Object { $_.ToString('x2') })
    return (Join-Path (Join-Path $DataRoot 'cargo-target') $digest)
}
