# Rust GNU binutils helper.
#
# 中文说明：Rust 的 x86_64-pc-windows-gnu self-contained 工具链带 dlltool，
# 但不带 as.exe。dlltool 生成 Windows import library 时会再启动 assembler；
# 在本机 CJK 路径下还需要由调用方提供 ASCII 视图。因此本文件只负责在
# D 盘 data root 下准备固定 SHA 的 MSYS2 assembler 运行时，并生成一个
# dlltool wrapper；调用方仍负责 subst/工作目录/环境变量。

function Get-RustGnuBinutilsPackages {
    return @(
        @{
            Name = "binutils"
            File = "mingw-w64-x86_64-binutils-2.47-3-any.pkg.tar.zst"
            Sha256 = "827363748ce3320683319d860ee4fcfcdbb36baf66aac6dab0ec3822be2d4ff7"
            Members = @("mingw64/bin/as.exe")
        },
        @{
            Name = "gcc-libs"
            File = "mingw-w64-x86_64-gcc-libs-16.2.0-3-any.pkg.tar.zst"
            Sha256 = "f8e25ea67bb796e7f65550f0dca9fce4cdde8aaa3dadafe4d13c6a8233c8de26"
            Members = @(
                "mingw64/bin/libatomic-1.dll",
                "mingw64/bin/libgcc_s_seh-1.dll",
                "mingw64/bin/libgomp-1.dll",
                "mingw64/bin/libquadmath-0.dll",
                "mingw64/bin/libstdc++-6.dll"
            )
        },
        @{
            Name = "gettext-runtime"
            File = "mingw-w64-x86_64-gettext-runtime-1.0-1-any.pkg.tar.zst"
            Sha256 = "be68d7f260633284b910c588c6d82ee304a81c8817a686d2cd9df83f872c27af"
            Members = @(
                "mingw64/bin/libasprintf-0.dll",
                "mingw64/bin/libintl-8.dll"
            )
        },
        @{
            Name = "libiconv"
            File = "mingw-w64-x86_64-libiconv-1.19-1-any.pkg.tar.zst"
            Sha256 = "21e334d0911f25de75d3e18e0697648bcecfa9658256d600cad0827d719c2f35"
            Members = @(
                "mingw64/bin/libcharset-1.dll",
                "mingw64/bin/libiconv-2.dll"
            )
        },
        @{
            Name = "libwinpthread"
            File = "mingw-w64-x86_64-libwinpthread-14.0.0.r262.g5ea8e9fac-1-any.pkg.tar.zst"
            Sha256 = "e3e46297b13e180218f84df64ae206949f3f9acdfe686c8ba28db9353d4845f7"
            Members = @("mingw64/bin/libwinpthread-1.dll")
        },
        @{
            Name = "zlib"
            File = "mingw-w64-x86_64-zlib-1.3.2-2-any.pkg.tar.zst"
            Sha256 = "9e75842a070ba648e986e12424e1c92c9d7d77200e85f6a34eeb600819f2e694"
            Members = @("mingw64/bin/zlib1.dll")
        },
        @{
            Name = "zstd"
            File = "mingw-w64-x86_64-zstd-1.5.7-2-any.pkg.tar.zst"
            Sha256 = "1add6705b344664f6aca108c85f79ab5bdd9e1162662bb06a4cf40a34f6e0907"
            Members = @("mingw64/bin/libzstd.dll")
        },
        @{
            Name = "tzdata"
            File = "mingw-w64-x86_64-tzdata-2026c-1-any.pkg.tar.zst"
            Sha256 = "502e6f8e65c554e717f6c749dcbb70cc9b85ed94b01c151c26a0c25235e63df2"
            Members = @()
        }
    )
}

function Get-RustGnuFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Assert-RustGnuPathUnderRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Root
    )
    $fullPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($fullPath -ne $fullRoot -and -not $fullPath.StartsWith($fullRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "rust-gnu-binutils path escapes data root: $fullPath"
    }
}

function Save-RustGnuPackage {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Package,
        [Parameter(Mandatory = $true)][string]$PackageRoot
    )
    New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null
    $destination = Join-Path $PackageRoot $Package.File
    if (Test-Path -LiteralPath $destination) {
        $existingHash = Get-RustGnuFileSha256 -Path $destination
        if ($existingHash -eq $Package.Sha256) {
            return $destination
        }
        throw "rust-gnu-binutils package hash mismatch for existing $($Package.File): $existingHash"
    }

    $part = "$destination.part"
    if (Test-Path -LiteralPath $part) {
        Remove-Item -LiteralPath $part -Force
    }

    $baseUrls = @(
        "https://repo.msys2.org/mingw/mingw64",
        "https://mirror.msys2.org/mingw/mingw64"
    )
    $errors = [System.Collections.Generic.List[string]]::new()
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    foreach ($baseUrl in $baseUrls) {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            try {
                $client = New-Object Net.WebClient
                try {
                    $client.Headers.Add("User-Agent", "AI-Coding-Factory-rust-gnu-binutils/1.0")
                    $client.DownloadFile("$baseUrl/$($Package.File)", $part)
                } finally {
                    $client.Dispose()
                }
                $actualHash = Get-RustGnuFileSha256 -Path $part
                if ($actualHash -ne $Package.Sha256) {
                    throw "sha256 mismatch: $actualHash"
                }
                Move-Item -LiteralPath $part -Destination $destination -Force
                return $destination
            } catch {
                if (Test-Path -LiteralPath $part) {
                    Remove-Item -LiteralPath $part -Force
                }
                $errors.Add("$baseUrl attempt ${attempt}: $($_.Exception.Message)")
                Start-Sleep -Seconds 1
            }
        }
    }

    throw "rust-gnu-binutils download failed for $($Package.File): $($errors -join ' | ')"
}

function Ensure-RustGnuAssemblerBundle {
    param([Parameter(Mandatory = $true)][string]$DataRoot)

    $toolRoot = Join-Path $DataRoot "rust-gnu-binutils"
    $packageRoot = Join-Path $toolRoot "packages"
    $bundleRoot = Join-Path $toolRoot "bundle"
    $bundleBin = Join-Path $bundleRoot "mingw64\bin"
    Assert-RustGnuPathUnderRoot -Path $toolRoot -Root $DataRoot

    $packages = Get-RustGnuBinutilsPackages
    $expectedLeafNames = [System.Collections.Generic.List[string]]::new()
    foreach ($package in $packages) {
        foreach ($member in $package.Members) {
            $expectedLeafNames.Add((Split-Path $member -Leaf))
        }
    }
    $expectedLeafNames = @($expectedLeafNames | Sort-Object -Unique)

    $bundleReady = $true
    foreach ($leaf in $expectedLeafNames) {
        if (-not (Test-Path -LiteralPath (Join-Path $bundleBin $leaf))) {
            $bundleReady = $false
            break
        }
    }
    if ($bundleReady) {
        return $bundleBin
    }

    foreach ($package in $packages) {
        Save-RustGnuPackage -Package $package -PackageRoot $packageRoot | Out-Null
    }

    if (Test-Path -LiteralPath $bundleRoot) {
        Assert-RustGnuPathUnderRoot -Path $bundleRoot -Root $DataRoot
        Remove-Item -LiteralPath $bundleRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $bundleRoot | Out-Null

    foreach ($package in $packages) {
        if (@($package.Members).Count -eq 0) {
            continue
        }
        $packagePath = Join-Path $packageRoot $package.File
        [string[]]$members = @($package.Members)
        $tarArgs = @("-xf", $packagePath, "-C", $bundleRoot) + $members
        & tar @tarArgs
        if ($LASTEXITCODE -ne 0) {
            throw "rust-gnu-binutils extract failed for $($package.File)"
        }
    }

    foreach ($leaf in $expectedLeafNames) {
        $filePath = Join-Path $bundleBin $leaf
        if (-not (Test-Path -LiteralPath $filePath -PathType Leaf)) {
            throw "rust-gnu-binutils missing extracted file: $leaf"
        }
    }
    $actualLeafNames = @(Get-ChildItem -LiteralPath $bundleBin -File | ForEach-Object { $_.Name } | Sort-Object)
    $expectedText = ($expectedLeafNames -join "`n")
    $actualText = ($actualLeafNames -join "`n")
    if ($actualText -ne $expectedText) {
        throw "rust-gnu-binutils extracted file set mismatch"
    }

    return $bundleBin
}

function New-RustGnuDlltoolWrapper {
    param(
        [Parameter(Mandatory = $true)][string]$DataRoot,
        [Parameter(Mandatory = $true)][string]$DlltoolPath,
        [Parameter(Mandatory = $true)][string]$AssemblerPath
    )
    if (-not (Test-Path -LiteralPath $DlltoolPath -PathType Leaf)) {
        throw "rust-gnu-binutils dlltool not found: $DlltoolPath"
    }
    if (-not (Test-Path -LiteralPath $AssemblerPath -PathType Leaf)) {
        throw "rust-gnu-binutils assembler not found: $AssemblerPath"
    }

    $wrapperRoot = Join-Path $DataRoot "rust-gnu-binutils\wrappers"
    Assert-RustGnuPathUnderRoot -Path $wrapperRoot -Root $DataRoot
    New-Item -ItemType Directory -Force -Path $wrapperRoot | Out-Null
    $wrapperPath = Join-Path $wrapperRoot "dlltool-as.cmd"
    $content = "@echo off`r`n`"$DlltoolPath`" -S `"$AssemblerPath`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    Set-Content -LiteralPath $wrapperPath -Value $content -Encoding ASCII
    return $wrapperPath
}
