# pyclean-audio — desktop packaging

Builds the **one-click desktop application**: an installer (Windows) or a
package (Linux) that carries everything — Python, torch, LavaSR, ffmpeg — so
that a user without Python, without a terminal and without any configuration
double-clicks one file and gets the working page.

**How the page opens is a build option, and the two modes are equivalent.** A
build with `pywebview` opens the application in its own window, and closing that
window quits it. A build without opens the page in the default browser — where
the **Quit** button in the top-right corner stops the application (it asks for a
confirmation while a job is running). Windows bundles the window by default;
Linux does not (see [WITH_WEBVIEW](#with_webview-the-embedded-window)). Nothing
in the page depends on which one you ship.

- [What is shipped](#what-is-shipped)
- [Layout of an install](#layout-of-an-install)
- [Where the user's data lives](#where-the-users-data-lives)
- [Build chain](#build-chain)
- [Linux: `.deb` and portable `.tar.gz`](#linux-deb-and-portable-targz)
- [Windows: `setup.exe`](#windows-setupexe)
- [Build options](#build-options)
- [Checks the build runs](#checks-the-build-runs)
- [Manual acceptance checklist](#manual-acceptance-checklist)
- [Why not PyInstaller](#why-not-pyinstaller)
- [Licensing](#licensing)
- [Not in v1](#not-in-v1)

Each artifact is built **on its own OS** (no cross-compiling). A GitHub Actions
matrix workflow would be the natural next step — the repository has no CI today.

## What is shipped

| Artifact | Built by | Needs at install time |
| -------- | -------- | --------------------- |
| `dist/pyclean-audio_1.0.0_amd64.deb` | `build_runtime.sh` + `linux/make-deb.sh` (Linux) | `dpkg -i` (root, once) |
| `dist/pyclean-audio-1.0.0-linux-x86_64.tar.gz` | `build_runtime.sh` + `linux/make-portable.sh` | nothing (extract, run) |
| `dist/pyclean-audio-1.0.0-setup.exe` | `build_runtime.ps1` + `installer/pyclean-audio.iss` (Windows) | nothing (double-click) |

Every artifact is ~2.6 GB: CPU torch alone is ~2.5 GB of files. **The download,
not the installation, is the long pole.**

## Layout of an install

Identical on both platforms — one tree, two launchers:

```
<root>/
  pyclean-audio.exe            # Windows launcher, frozen (PyInstaller --noconsole)
  pyclean-audio                # Linux launcher: runs runtime/python with launcher.py
  launcher.py                  # the launcher source (Linux; frozen on Windows)
  runtime/python/              # standalone relocatable CPython 3.11 + site-packages
  runtime/bin/                 # bundled ffmpeg + ffprobe, found by bare name
  app/  static/                # the application
  LICENCE  THIRD-PARTY-NOTICES.txt
```

`<root>` is `%LOCALAPPDATA%\Programs\pyclean-audio` (Windows) or
`/opt/pyclean-audio` (Linux). The tree is **read-only at run time**: the server
is started with the install directory as CWD and never writes into it.

Two details that are easy to get wrong and are asserted by the build:

- **the interpreter must be standalone, not a venv.** A `uv venv` records the
  absolute path of the interpreter it was created from, which breaks as soon as
  the folder is moved; `uv python install` downloads a
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  build, which is relocatable, so it is copied as is.
- **torch is installed first, from an explicit index.** LavaSR depends on
  `torch`; left to its own devices it would resolve the default (CUDA) wheel on
  PyPI and silently replace the CPU one (+3 GB on a machine without a GPU). The
  build then *fails* if `torch.version.cuda` is not `None`.

## Where the user's data lives

Never inside the install directory (it is read-only, and uninstalling must not
delete the user's work):

| | |
| --- | --- |
| Windows | `%LOCALAPPDATA%\pyclean-audio\{jobs,logs,cache\huggingface,instance.lock}` |
| Linux | `$XDG_DATA_HOME/pyclean-audio/…` (`~/.local/share/pyclean-audio/…`) |
| override | `PYCLEAN_HOME=/somewhere` |

`instance.lock` is the single-instance guard: the second launch opens the
address of the running app again (a second webview window, the same browser tab
otherwise) instead of starting a second server. Its pid is probed, so a lock
left by a crash is reclaimed.

## Build chain

```bash
# Linux — runtime (~10 min, ~2.6 GB)
packaging/build_runtime.sh                 # -> dist/pyclean-audio/ + dist/BUILD-INFO.txt
packaging/linux/make-deb.sh                # -> dist/pyclean-audio_1.0.0_amd64.deb
packaging/linux/make-portable.sh           # -> dist/pyclean-audio-1.0.0-linux-x86_64.tar.gz

# Windows — runtime, then the installer
powershell -ExecutionPolicy Bypass -File packaging\build_runtime.ps1
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.0.0 ^
    packaging\installer\pyclean-audio.iss   # -> dist\pyclean-audio-1.0.0-setup.exe
```

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and **git** (LavaSR is a
`git+https` dependency) on both platforms; plus `curl`, `tar` and `dpkg-deb` on
Linux, and **Inno Setup 6** on Windows for the installer step. No Docker, no
cross-compiler, nothing to install in the runtime.

## Linux: `.deb` and portable `.tar.gz`

```bash
sudo dpkg -i dist/pyclean-audio_1.0.0_amd64.deb     # or double-click it
pyclean-audio                                        # also in the app menu
sudo dpkg -r pyclean-audio                           # results and cache are kept
```

The `.deb` installs the tree in `/opt/pyclean-audio`, exposes a symlink
`/usr/bin/pyclean-audio` and a `.desktop` entry (`Terminal=false`,
`Categories=AudioVideo;Audio;`) so the software centre and the file manager both
start it with no terminal. `postinst` only refreshes the desktop cache when
`desktop-file-utils` is installed; it requires nothing privileged and never
asks anything.

The `.deb` `Depends:` and its description follow the tree it was built from
(`dist/WEBVIEW`): an embedded window brings `libwebkit2gtk-4.1-0` and
`gir1.2-gtk-3.0`, browser mode needs neither and never claims to have a window.

No AppImage in v1: libfuse2 is not installed by default on several
distributions, which would break the "double-click and it works" promise.

### WITH_WEBVIEW: the embedded window

`pywebview` gives the app its own window, and closing that window quits it. It is
**on by default on Windows** (pure-Python wheel, WebView2 already present) and
**off by default on Linux**, because `pywebview[gtk]` compiles PyGObject: it
needs `libgtk-3-dev`, `libwebkit2gtk-4.1-dev` and `gir1.2-gtk-3.0` on the build
host, and the runtime libraries on every user's machine.

```bash
WITH_WEBVIEW=1 packaging/build_runtime.sh     # needs the GTK/WebKit2GTK dev stack
```

If the stack is missing, the build prints a loud warning and continues **in
browser mode** rather than failing or half-bundling: `dist/WEBVIEW` then records
`0`, and `make-deb.sh` writes a browser-mode `.deb`. Half-bundling is the one
outcome worth avoiding — a tree that imports `webview` and then fails on the
user's machine is worse than an honest default. And after `POST /api/shutdown`,
browser mode is a first-class experience, not a degraded one.

## Windows: `setup.exe`

Per-user by default (`PrivilegesRequired=lowest`): no administrator rights,
`%LOCALAPPDATA%\Programs\pyclean-audio`, a Start-menu shortcut, an optional
desktop shortcut, and a working `unins000.exe`. The launcher is frozen with
`--noconsole`, so **no console window** flashes at launch.

```powershell
setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART   # unattended (CI, fleet)
```

The installer is **unsigned** in v1. Windows SmartScreen shows "Windows has
protected your PC" on the first launch: *More info → Run anyway*.

WebView2 (needed by the embedded window) ships with Windows 11 and Windows 10
21H2+; where it is missing, the launcher opens the page in the default browser
instead — the app stays usable and the page's Quit button stops it — so no
runtime is downloaded during install. Build with `-WithWebView:$false` for a
browser-mode `setup.exe`.

## Build options

| Variable | Default | Effect |
| -------- | ------- | ------ |
| `VERSION` | `1.0.0` | version in the file names and the installer |
| `TORCH_INDEX_URL` | `https://download.pytorch.org/whl/cpu` | CPU (default) or `…/whl/cu130` for a CUDA build |
| `REQUIRE_CPU` | `1` | the build fails if a CUDA torch slips in; `REQUIRE_CPU=0` (`-RequireCpu:$false`) for a deliberate CUDA build |
| `FFMPEG_URL` | versioned static build (see below) | ffmpeg source |
| `FFMPEG_SHA256` / `-FfmpegSha256` | the digest of that exact file | the build **stops** if the download does not match |
| `FFMPEG_FROM_SYSTEM` | `0` (Linux) | `1` reuses the host's `ffmpeg`/`ffprobe` instead of downloading |
| `WITH_WEBVIEW` | `1` (Windows), `0` (Linux) | bundle `pywebview` for the embedded window |

ffmpeg is **pinned by version and digest**, so the binary every user ends up
with is the one a builder reviewed (the rolling `ffmpeg-release-*` files upstream
are a new build under the same name):

| Platform | Version | sha256 |
| --- | --- | --- |
| Linux | `johnvansickle.com/ffmpeg/old-releases/ffmpeg-6.0.1-amd64-static.tar.xz` | `28268bf402f1083833ea269331587f60a242848880073be8016501d864bd07a5` |
| Windows | `gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip` | `db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec` |

Point `FFMPEG_URL` somewhere else and the build stops on the digest unless you
also pass the matching `FFMPEG_SHA256`. Both builds are checked after unpacking:
`ffmpeg -version` and `ffprobe -version` must run, and the Linux one ships
`libmp3lame` (what `app/processor.py` encodes with).

A CUDA build changes `TORCH_INDEX_URL` **and** sets `REQUIRE_CPU=0` (the CPU
assertion exists to catch a CUDA wheel slipping in through LavaSR's own
dependency resolution, so it has to stand down), and it needs the NVIDIA driver
+ redistributable on the user's machine; that is why v1 ships CPU only.

## Checks the build runs

Both build scripts stop on the first failure — a runtime that imports but runs
on the GPU, or that misses ffmpeg, is worse than a build that refuses to
finish:

1. `import torch, torchaudio, numpy, soundfile, fastapi, uvicorn, LavaSR, app.main`
2. `torch.version.cuda is None`, unless `REQUIRE_CPU=0` (a CUDA wheel slipping
   in is a silent 3 GB regression)
3. `nemo` is **not** importable (transcription is opt-in; the desktop build
   ships without it)
4. `runtime/bin/ffmpeg -version` and `ffprobe -version` both run
5. `THIRD-PARTY-NOTICES.txt` is in the payload (asserted by `make-deb.sh`)
6. the ffmpeg download matches its pinned sha256

`dist/BUILD-INFO.txt` records the interpreter, the package list, the ffmpeg
version *and its source and digest*, and `dist/WEBVIEW` records whether the
embedded window is bundled (`1`) or not (`0`) — the two files the packaging
steps and a bug report read.

## Manual acceptance checklist

To run on a clean VM (Windows and/or Debian/Ubuntu), a human does this once per
release:

1. install → double-click → the window opens on the working page, **no console**;
2. enhance a WAV → download the WAV and the MP3;
3. enhance a video → download the MP4 (video stream untouched);
4. cancel a job mid-run → `cancelled`, and no `ffmpeg` left in the task manager;
5. close the window (embedded-window builds) → no `python`/`pyclean-audio`
   process left; and **Quit** from the page (every build) → none left;
6. on Windows, also check the task manager after closing the window mid-job: in
   v1 `ffmpeg.exe` may outlive it (there is no Job Object, only a process-group
   kill of the launcher). It exits on its own; a later build should attach a Job
   Object to the server process;
7. launch it a second time → its address opens again, no second server;
8. uninstall → the program files are gone, the results and the model cache are kept.

## Why not PyInstaller

torch is ~2.5 GB of thousands of files and shared libraries, and a frozen torch
is a well-known source of broken one-file builds (dynamic CUDA DLLs,
`__file__`-relative data). Shipping the runtime as **plain files** costs nothing
in robustness, and the same tree serves both operating systems. Only the
~300-line launcher is compiled (Windows), because the Start-menu entry has to be
a double-clickable `.exe`.

The launcher itself (`packaging/launcher/launcher.py`) is standard library only
and has **no import-time side effect**, so `tests/test_launcher.py` drives every
helper without a server, a network or a window.

## Licensing

`THIRD-PARTY-NOTICES.txt` ships in the payload and in the installer: CPython
(PSF), PyTorch (BSD-3), LavaSR (its own licence, weights downloaded from
HuggingFace), ffmpeg (LGPL/GPL depending on the build; the static builds shipped
here are GPL), plus the MIT/Apache/BSD packages installed by
`requirements-base.txt`.

Redistribution of the **model weights** is not decided here: like the Docker
image, the desktop build does not bake them in — the first launch downloads
~115 MB from HuggingFace. Sign and notarise the installer before any public
release.

## Not in v1

- **Transcription**: `nemo_toolkit` is not shipped (~2.4 GB checkpoint, and it
  pins its own torch). The checkbox is disabled and the API answers 400 to
  `transcribe=true` — a supported state, not a bug.
- **CUDA / NVIDIA acceleration**: CPU only (see above).
- **AppImage**, **code signing / notarisation**, **CI matrix**.