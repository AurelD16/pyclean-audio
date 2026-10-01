#requires -Version 5.1
<#
.SYNOPSIS
    pyclean-audio — packs dist\pyclean-audio into a portable ZIP, for a user who
    wants one file and no installer (see packaging\README.md).

.DESCRIPTION
    Input: dist\pyclean-audio\ (built by build_runtime.ps1).
    Output: dist\pyclean-audio-<version>-windows-x86_64.zip, holding a single
    `pyclean-audio\` directory: the user extracts it anywhere (a USB stick, a
    folder they can make read-only) and double-clicks pyclean-audio.exe.

    **Compress-Archive must not be used**: it refuses anything above 2 GB, and
    this tree is 1.4-2.6 GB — it would fail, or leave a truncated archive, half
    way through. `System.IO.Compression.ZipFile::CreateFromDirectory` writes
    ZIP64 and has no such limit.

    Nothing is installed, no registry entry, no shortcut, no elevation: the
    application already writes everything to %LOCALAPPDATA%\pyclean-audio, so the
    extracted folder needs no write access.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\make-portable.ps1
#>
[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$OutputName = ""          # defaults to pyclean-audio-<version>-windows-x86_64.zip
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DistRoot = Join-Path $Root "dist"
$Src = Join-Path $DistRoot "pyclean-audio"
if (-not $OutputName) { $OutputName = "pyclean-audio-$Version-windows-x86_64.zip" }
$Zip = Join-Path $DistRoot $OutputName

function Say([string]$Message) { Write-Host ""; Write-Host "=== $Message" -ForegroundColor Cyan }
function Die([string]$Message) { throw "BUILD FAILED: $Message" }

if (-not (Test-Path (Join-Path $Src "runtime\python\python.exe"))) {
    Die "dist\pyclean-audio is missing or has no runtime\python — run packaging\build_runtime.ps1 first"
}
if (-not (Test-Path (Join-Path $Src "pyclean-audio.exe"))) {
    Die "dist\pyclean-audio has no pyclean-audio.exe (the launcher is frozen by build_runtime.ps1)"
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

Say "packing $OutputName"
if (Test-Path $Zip) { Remove-Item -Force $Zip }

# CreateFromDirectory, not Compress-Archive: ZIP64, no 2 GB ceiling.
# includeBaseDirectory: the archive must hold ONE directory, pyclean-audio\.
# (CompressionLevel::Optimal is the default and what we want: the archive is
# downloaded once and unpacked once.)
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $Src,
    $Zip,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true
)

if (-not (Test-Path $Zip)) { Die "no archive was produced" }
$SizeMb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
Write-Host "  $OutputName — $SizeMb MB"

# The archive has to be readable and complete: a 1.4-2.6 GB payload that failed
# half way through would otherwise be discovered by a user. Read the central
# directory back, then decompress the largest entry **to its end** and compare
# the byte count with the directory's — a truncation anywhere inside that entry
# (a ZIP64 writer that gave up half way, a full disk) shows up here.
Say "checking the archive"
$archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try {
    $names = @{}
    foreach ($entry in $archive.Entries) { $names[$entry.FullName] = $entry.Length }
}
finally {
    $archive.Dispose()
}

foreach ($want in @("pyclean-audio/pyclean-audio.exe", "pyclean-audio/runtime/python/python.exe")) {
    if (-not $names.ContainsKey($want)) { Die "the archive does not contain $want" }
}
if (-not $names.Keys.Where({ $_ -like "pyclean-audio/runtime/python/*" }).Count) {
    Die "the archive has no runtime\python content"
}
$big = $names.Keys | Where-Object { $_ -like "pyclean-audio/runtime/python/*" } |
    Sort-Object { $names[$_] } -Descending | Select-Object -First 1
$archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try {
    $entry = $archive.GetEntry($big)
    $stream = $entry.Open()
    try {
        $buffer = New-Object byte[] 4194304
        $total = 0
        while (($total += $stream.Read($buffer, 0, $buffer.Length)) -gt 0) { }
    }
    finally { $stream.Dispose() }
}
finally { $archive.Dispose() }
if ($total -ne $names[$big]) {
    Die "$big is truncated in the archive: read $total bytes, the directory says $($names[$big])"
}
Write-Host "  ok: $big ($([math]::Round($names[$big] / 1MB, 1)) MB) decompresses completely"

Say "done — dist\$OutputName ($SizeMb MB)"
Write-Host "use:    extract it anywhere, then double-click pyclean-audio\pyclean-audio.exe"
Write-Host "note:   a file extracted from a downloaded ZIP keeps the Mark of the Web, so"
Write-Host "        SmartScreen may block the .exe once: right-click it -> Properties ->"
Write-Host "        Unblock, then run it. (See packaging\README.md.)"
