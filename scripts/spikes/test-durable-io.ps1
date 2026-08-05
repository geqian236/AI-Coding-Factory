Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$spikeName   = "windows_durable_io"
$scriptRoot  = Split-Path $PSScriptRoot -Parent | Split-Path -Parent
$receiptDir  = "$scriptRoot\tools\compat-probes\windows_durable_io"
$receiptPath = "$receiptDir\receipt.json"
[System.IO.Directory]::CreateDirectory($receiptDir) | Out-Null

$actions    = [System.Collections.Generic.List[string]]::new()
$assertions = [System.Collections.Generic.List[hashtable]]::new()
$status     = "PASS"
$tmpDir     = [System.IO.Path]::Combine($receiptDir, "probe_tmp")
[System.IO.Directory]::CreateDirectory($tmpDir) | Out-Null
# 幂等：按名删除上一轮遗留的两个探针文件。不用 Directory.Delete(recursive)——
# Windows 上递归删目录常因句柄占用而延迟/竞争，残留的 probe_final.dat 会让
# File.Move（PS 5.1 无覆盖重载）再次因“目标已存在”失败。按文件名删则确定生效，
# File.Delete 对不存在的文件是 no-op。
foreach ($f in @("probe_tmp.dat","probe_final.dat")) {
    $p = [System.IO.Path]::Combine($tmpDir, $f)
    if ([System.IO.File]::Exists($p)) { [System.IO.File]::Delete($p) }
}

$envInfo = @{
    os=([System.Runtime.InteropServices.RuntimeInformation]::OSDescription)
    arch=([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString())
    ps_version=($PSVersionTable.PSVersion.ToString())
    hostname=($env:COMPUTERNAME)
    probe_backend="powershell_dotnet"
}

try {
    $payload   = [System.Text.Encoding]::UTF8.GetBytes("DURABLE_IO_PROBE_PAYLOAD_v1")
    $tmpFile   = [System.IO.Path]::Combine($tmpDir,"probe_tmp.dat")
    $finalFile = [System.IO.Path]::Combine($tmpDir,"probe_final.dat")
    [System.IO.File]::WriteAllBytes($tmpFile,$payload)
    $actions.Add("write " + $payload.Length.ToString() + " bytes to probe_tmp.dat")

    $fs = New-Object System.IO.FileStream($tmpFile,[System.IO.FileMode]::Open,[System.IO.FileAccess]::ReadWrite,[System.IO.FileShare]::None)
    $fs.Flush($true); $fs.Close(); $fs.Dispose()
    $actions.Add("FileStream.Flush(flushToDisk=true) => FlushFileBuffers on file")

    [System.IO.File]::Move($tmpFile,$finalFile)
    $renameOk = [System.IO.File]::Exists($finalFile)
    $tmpGone  = (-not [System.IO.File]::Exists($tmpFile))
    $actions.Add("File.Move rename: dest_exists=" + $renameOk.ToString() + " src_gone=" + $tmpGone.ToString())

    $readBack  = [System.IO.File]::ReadAllBytes($finalFile)
    $contentOk = ($readBack.Length -eq $payload.Length)
    if ($contentOk) {
        for ($i=0;$i -lt $payload.Length;$i++) {
            if ($readBack[$i] -ne $payload[$i]) { $contentOk=$false; break }
        }
    }
    $actions.Add("read back " + $readBack.Length.ToString() + " bytes: content_match=" + $contentOk.ToString())

    $dirInfo  = New-Object System.IO.DirectoryInfo($tmpDir)
    $nFiles   = $dirInfo.GetFiles().Count
    $fileVisibleInDir = ($nFiles -gt 0)
    $actions.Add("parent dir GetFiles count=" + $nFiles.ToString())

    $fileSize = (New-Object System.IO.FileInfo($finalFile)).Length
    $hashBytes = [System.Security.Cryptography.SHA256]::Create().ComputeHash($readBack)
    $digest = "sha256:" + [System.BitConverter]::ToString($hashBytes).Replace("-","").ToLower()

    $facts = [ordered]@{
        write_bytes                   = $payload.Length
        flush_file_succeeded          = $true
        rename_succeeded              = $renameOk
        src_gone_after_rename         = $tmpGone
        content_verified_after_rename = $contentOk
        file_size_bytes               = $fileSize
        file_visible_in_parent_dir    = $fileVisibleInDir
    }
    $assertions.Add(@{name="content_survives_rename";    passed=$contentOk;        detail="byte-for-byte match after File.Move"})
    $assertions.Add(@{name="src_absent_after_rename";    passed=$tmpGone;          detail="source absent after rename"})
    $assertions.Add(@{name="file_size_matches_write";    passed=($fileSize -eq $payload.Length); detail="exp=" + $payload.Length.ToString() + " got=" + $fileSize.ToString()})
    $assertions.Add(@{name="file_visible_in_parent_dir"; passed=$fileVisibleInDir; detail="DirectoryInfo.GetFiles returned " + $nFiles.ToString() + " file(s)"})
    foreach ($a in $assertions) { if (-not $a.passed) { $status="FAIL" } }

    $receipt=[ordered]@{spike=$spikeName;status=$status;environment=$envInfo;actions=@($actions);observable_facts=$facts;assertions=@($assertions);artifact_digest=$digest;timestamp=(Get-Date -Format "o")}
} catch {
    $status="ERROR"
    $receipt=[ordered]@{spike=$spikeName;status="ERROR";error=$_.Exception.Message;timestamp=(Get-Date -Format "o")}
}

$json=$receipt|ConvertTo-Json -Depth 10
$json|Out-File -Encoding utf8 $receiptPath
Write-Host $json
if ($status -ne "PASS") { exit 1 }
# 显式 exit 0：spike 通过时必须设置 $LASTEXITCODE，否则 test.ps1 的 Run-Suite
# 在 StrictMode 下读取未定义的 $LASTEXITCODE 会抛异常并中断整个套件。
exit 0
