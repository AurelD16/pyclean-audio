#requires -Version 5.1
<#
.SYNOPSIS
    pyclean-audio — builds dist\pyclean-audio, the tree the desktop launcher runs
    on **Windows** (see packaging\README.md).

.DESCRIPTION
    The tree is plain files, not a PyInstaller bundle: torch is ~2.5 GB of shared
    libraries and a frozen torch is the fragile part. Only the launcher is
    compiled (PyInstaller, --noconsole, --onefile), because a per-user install
    under %LOCALAPPDATA% must expose one double-clickable .exe.

    Every check below fails the build on purpose: a runtime that imports but
    runs on the GPU, or that misses ffmpeg, is worse than a build that stops.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\build_runtime.ps1

.EXAMPLE
    # CUDA runtime: a different index, and the CPU assertion stands down
    powershell -ExecutionPolicy Bypass -File packaging\build_runtime.ps1 `
        -TorchIndexUrl "https://download.pytorch.org/whl/cu130" -RequireCpu:$false
#>
[CmdletBinding()]
param(
    [string]$Version = "1.0.0",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cpu",
    [string]$FfmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    [string]$PythonVersion = "3.11",
    # The CPU assertion exists to catch a CUDA wheel slipping in through LavaSR's
    # own dependency resolution; a deliberate CUDA build stands it down.
    [bool]$RequireCpu = $true
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$DistRoot = Join-Path $Root "dist"
$Stage = Join-Path $DistRoot ".stage"
$Dist = Join-Path $DistRoot "pyclean-audio"
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("pyclean-build-" + [guid]::NewGuid().ToString("N"))

function Say([string]$Message) { Write-Host ""; Write-Host "=== $Message" -ForegroundColor Cyan }
function Die([string]$Message) { throw "BUILD FAILED: $Message" }

# External tools do not raise on a non-zero exit code: check every one of them.
function Run([string]$Exe, [string[]]$Arguments, [string]$What) {
    Write-Host "  $Exe $($Arguments -join ' ')"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Die "$What (exit $LASTEXITCODE)" }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Die "uv is required (https://docs.astral.sh/uv/)"
}
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
if (Test-Path $Tmp) { Remove-Item -Recurse -Force $Tmp }
New-Item -ItemType Directory -Force -Path $Stage, $Tmp | Out-Null

try {
    # --- 1. a standalone, relocatable CPython ---------------------------------
    # NOT `uv venv`: a venv records the absolute path of the interpreter it was
    # created from, which breaks as soon as the folder is copied under
    # %LOCALAPPDATA%\Programs. python-build-standalone (what `uv python install`
    # downloads) is built to be relocatable, so it is copied as is.
    Say "standalone CPython $PythonVersion"
    Run "uv" @("python", "install", $PythonVersion) "uv python install"
    $PySrc = (& uv python find $PythonVersion).Trim()
    if (-not (Test-Path $PySrc)) { Die "uv python find returned nothing: $PySrc" }
    $PyPrefix = Split-Path -Parent $PySrc          # <prefix>\python.exe
    if (-not (Test-Path (Join-Path $PyPrefix "Lib"))) { Die "not a standalone prefix: $PyPrefix" }

    Copy-Item -Recurse -Force $PyPrefix (Join-Path $Stage "runtime\python")
    $Py = Join-Path $Stage "runtime\python\python.exe"
    if (-not (Test-Path $Py)) { Die "the copied interpreter is missing: $Py" }
    Run $Py @("-c", "import sys; print(sys.version); print(sys.prefix)") "the copied interpreter"

    # --- 2. torch first, and from an explicit index ---------------------------
    # LavaSR depends on torch: installed afterwards, it would resolve torch on its
    # own and silently replace this one with the default (CUDA) wheel.
    Say "torch from $TorchIndexUrl"
    Run "uv" @("pip", "install", "--python", $Py, "--index-url", $TorchIndexUrl, "torch", "torchaudio") "torch install"

    Say "base dependencies (requirements-base.txt)"
    Run "uv" @("pip", "install", "--python", $Py, "-r", (Join-Path $Root "requirements-base.txt")) "base dependencies"

    # --- 3. the checks that make a runtime trustworthy ------------------------
    Say "import checks"
    # PYCLEAN_DATA_DIR: importing app.main creates the results directory at
    # import time — keep that out of the source tree.
    $env:PYCLEAN_DATA_DIR = Join-Path $Tmp "check-data"
    $env:PYTHONPATH = $Root
    $env:REQUIRE_CPU = if ($RequireCpu) { "1" } else { "0" }
    $env:TORCH_INDEX_URL = $TorchIndexUrl
    $Check = @'
import importlib.util
import os
import sys

for name in ("torch", "torchaudio", "numpy", "soundfile", "fastapi", "uvicorn",
             "LavaSR", "app.main"):
    __import__(name)
    print(f"  ok  {name}")

import torch

if os.environ.get("REQUIRE_CPU", "1") == "1" and torch.version.cuda is not None:
    sys.exit(f"CUDA torch {torch.version} slipped in: a CPU runtime is expected "
             f"(TORCH_INDEX_URL={os.environ.get('TORCH_INDEX_URL', 'unset')}; "
             f"-RequireCpu:$false for a deliberate CUDA build)")
if importlib.util.find_spec("nemo") is not None:
    sys.exit("nemo_toolkit is installed: it weighs several GB and is opt-in "
             "(./run.sh --asr); the desktop build must ship without it")
'@
    $CheckFile = Join-Path $Tmp "check_runtime.py"
    Set-Content -Path $CheckFile -Value $Check -Encoding UTF8
    Run $Py @($CheckFile) "the runtime does not import cleanly"

    # --- 4. the application ----------------------------------------------------
    Say "application"
    Copy-Item -Recurse -Force (Join-Path $Root "app") (Join-Path $Stage "app")
    Copy-Item -Recurse -Force (Join-Path $Root "static") (Join-Path $Stage "static")
    Get-ChildItem -Recurse -Directory -Filter "__pycache__" (Join-Path $Stage "app") |
        Remove-Item -Recurse -Force
    Copy-Item -Force (Join-Path $Root "LICENCE") (Join-Path $Stage "LICENCE")
    Copy-Item -Force (Join-Path $Root "packaging\THIRD-PARTY-NOTICES.txt") (Join-Path $Stage "THIRD-PARTY-NOTICES.txt")
    Copy-Item -Force (Join-Path $Root "packaging\launcher\launcher.py") (Join-Path $Stage "launcher.py")

    # --- 5. the launcher, frozen without a console window ---------------------
    # PyInstaller is installed in a throwaway venv, not in the runtime: the
    # runtime stays clean. --noconsole: the app must not flash a black window.
    Say "freezing the launcher (--noconsole)"
    $PyiVenv = Join-Path $Tmp "pyinstaller"
    Run "uv" @("venv", $PyiVenv, "--python", $PythonVersion) "venv for pyinstaller"
    $PyiPy = Join-Path $PyiVenv "Scripts\python.exe"
    Run "uv" @("pip", "install", "--python", $PyiPy, "pyinstaller") "pyinstaller install"
    Run $PyiPy @(
        "-m", "PyInstaller",
        "--noconsole", "--onefile", "--clean",
        "--name", "pyclean-audio",
        "--distpath", $Stage,
        "--workpath", (Join-Path $Tmp "pyi-build"),
        "--specpath", (Join-Path $Tmp "pyi-spec"),
        (Join-Path $Root "packaging\launcher\launcher.py")
    ) "pyinstaller"
    $Exe = Join-Path $Stage "pyclean-audio.exe"
    if (-not (Test-Path $Exe)) { Die "pyclean-audio.exe was not produced" }

    # --- 6. ffmpeg, by bare name ----------------------------------------------
    # app\processor.py calls `ffprobe` and `ffmpeg` without a path: the launcher
    # puts runtime\bin in front of PATH.
    Say "ffmpeg"
    $FfmpegDir = Join-Path $Stage "runtime\bin"
    New-Item -ItemType Directory -Force -Path $FfmpegDir | Out-Null
    $Zip = Join-Path $Tmp "ffmpeg.zip"
    Write-Host "  downloading $FfmpegUrl"
    Invoke-WebRequest -UseBasicParsing -Uri $FfmpegUrl -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath (Join-Path $Tmp "ffmpeg") -Force
    Get-ChildItem -Recurse -Path (Join-Path $Tmp "ffmpeg") -Filter "ffmpeg.exe" |
        Select-Object -First 1 | ForEach-Object { Copy-Item -Force $_.FullName $FfmpegDir }
    Get-ChildItem -Recurse -Path (Join-Path $Tmp "ffmpeg") -Filter "ffprobe.exe" |
        Select-Object -First 1 | ForEach-Object { Copy-Item -Force $_.FullName $FfmpegDir }
    foreach ($exe in @("ffmpeg.exe", "ffprobe.exe")) {
        $path = Join-Path $FfmpegDir $exe
        if (-not (Test-Path $path)) { Die "$exe was not found in the downloaded build" }
        Run $path @("-version") "the bundled $exe"
    }

    # --- 7. what was built -----------------------------------------------------
    Say "BUILD-INFO.txt"
    $TorchVersion = (& $Py -c "import torch; print(torch.__version__)").Trim()
    $FfmpegVersion = (& (Join-Path $FfmpegDir "ffmpeg.exe") -version)[0]
    $Packages = (& uv pip list --python $Py) -join "`n"
    @(
        "pyclean-audio $Version - desktop runtime (windows-x86_64)"
        "built on $((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
        ""
        "python:  $(& $Py -V 2>&1)"
        "torch:   $TorchVersion"
        "ffmpeg:  $FfmpegVersion"
        "torch index: $TorchIndexUrl (REQUIRE_CPU=$($RequireCpu.ToString().ToLower()))"
        ""
        "packages:"
        $Packages
    ) | Set-Content -Path (Join-Path $DistRoot "BUILD-INFO.txt") -Encoding UTF8

    if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
    Move-Item -Path $Stage -Destination $Dist
    Say "done - dist\pyclean-audio"
    Write-Host "next: compile packaging\installer\pyclean-audio.iss with Inno Setup 6"
}
finally {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    Remove-Item -Env:PYCLEAN_DATA_DIR, Env:REQUIRE_CPU, Env:TORCH_INDEX_URL -ErrorAction SilentlyContinue
}