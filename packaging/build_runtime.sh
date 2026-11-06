#!/usr/bin/env bash
# pyclean-audio — builds dist/pyclean-audio/, the tree the desktop launcher runs
# on **Linux** (see packaging/README.md).
#
#   packaging/build_runtime.sh                     # CPU runtime (~2.6 GB)
#   FFMPEG_FROM_SYSTEM=1 packaging/build_runtime.sh   # reuse the host's ffmpeg
#   VERSION=1.1.0 packaging/build_runtime.sh
#
#   # CUDA runtime: a different index, and the CPU assertion must stand down
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 REQUIRE_CPU=0 \
#       packaging/build_runtime.sh
#
#   # Embedded window (needs the GTK + WebKit2GTK *development* files here):
#   WITH_WEBVIEW=1 packaging/build_runtime.sh
#
# The tree is *plain files*, not a PyInstaller bundle: torch is ~2.5 GB of
# shared libraries, and a frozen torch is the fragile part. Only the launcher is
# a script (the Windows build freezes it, build_runtime.ps1).
#
# Every check below fails the build on purpose — a runtime that imports but
# runs on the GPU, or that misses ffmpeg, is worse than a build that stops.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST="$ROOT/dist/pyclean-audio"
STAGE="$ROOT/dist/.stage"

VERSION="${VERSION:-1.0.0}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}"
# A *versioned* ffmpeg, pinned by digest: the binary every user ends up with is
# the one a builder reviewed. johnvansickle's rolling `ffmpeg-release-*` file is
# a new build every few weeks under the same name; `old-releases/` is immutable.
FFMPEG_URL="${FFMPEG_URL:-https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-6.0.1-amd64-static.tar.xz}"
FFMPEG_SHA256="${FFMPEG_SHA256:-28268bf402f1083833ea269331587f60a242848880073be8016501d864bd07a5}"
FFMPEG_FROM_SYSTEM="${FFMPEG_FROM_SYSTEM:-0}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"
# Embedded window: opt-in on Linux. pywebview[gtk] compiles PyGObject, so it
# needs the GTK/WebKit2GTK development files on this host and the runtime
# libraries on the user's machine. Default 0: an honest browser-mode build
# (the page's Quit button stops the app, see POST /api/shutdown) beats a
# half-bundled webview that silently degrades.
WITH_WEBVIEW="${WITH_WEBVIEW:-0}"
# The CPU assertion exists to catch a CUDA wheel slipping in through LavaSR's
# own dependency resolution. A deliberate CUDA build (above) has no reason to
# fail it: REQUIRE_CPU=0 stands it down, nothing else.
REQUIRE_CPU="${REQUIRE_CPU:-1}"

DL_TMP=""
# A failing build must not leave 2.6 GB of torch in dist/.stage behind.
cleanup() {
  [ -n "$DL_TMP" ] && rm -rf "$DL_TMP"
  rm -rf "$STAGE"
}
trap cleanup EXIT

say() { printf '\n=== %s\n' "$*"; }
die() { printf '\nBUILD FAILED: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Linux) ;;
  *) die "build_runtime.sh builds the Linux tree; use build_runtime.ps1 on Windows" ;;
esac
command -v uv >/dev/null || die "uv is required (https://docs.astral.sh/uv/)"
command -v curl >/dev/null || die "curl is required to fetch ffmpeg"

# --- 1. a standalone, relocatable CPython -------------------------------------
# NOT `uv venv`: a venv records the absolute path of the interpreter it was
# created from, which breaks as soon as the folder is moved under
# /opt/pyclean-audio. python-build-standalone (what `uv python install`
# downloads) is built to be relocatable, so it is copied as is.
say "standalone CPython $PYTHON_VERSION"
uv python install "$PYTHON_VERSION"
# `uv python find` answers two things we must never copy:
#   - the project's own .venv, when there is one (./run.sh creates one) — and a
#     venv records its base interpreter by absolute path, so copying one ships a
#     tree pointing back at the builder. Asking from a directory that is not a
#     project is the fix; the checks below are the safety net.
#   - the version-less alias `cpython-3.11-linux-x86_64-gnu`, which is a
#     **symlink** to the real directory: `cp -a` keeps symlinks as symlinks, so
#     without `pwd -P` the staged tree would point back at the builder's $HOME
#     and every later check would pass through it.
UV_PY_DIR="$(uv python dir)"
PY_SRC="$(cd / && uv python find "$PYTHON_VERSION")" \
  || die "uv has no managed Python $PYTHON_VERSION (uv python install $PYTHON_VERSION)"
[ -x "$PY_SRC" ] || die "uv python find returned no interpreter: $PY_SRC"
PY_PREFIX="$(cd "$(dirname "$PY_SRC")/.." && pwd -P)"
[ -d "$PY_PREFIX/lib/python3."* ] || die "not a standalone prefix (no lib/python3.x): $PY_PREFIX"
# A managed standalone build, not a venv and not the system Python: only uv's
# downloads are built to be relocatable.
case "$PY_PREFIX" in
  "$UV_PY_DIR"/*) ;;
  *) die "$PY_PREFIX is not a uv-managed interpreter (expected it under $UV_PY_DIR): a venv or a system Python is not relocatable" ;;
esac
"$PY_SRC" -c 'import sys; sys.exit(0 if sys.prefix == sys.base_prefix else 1)' \
  || die "uv python find answered a virtual environment ($PY_SRC): a venv records its base interpreter by absolute path and must never be copied"

rm -rf "$STAGE"
mkdir -p "$STAGE/runtime"
cp -a "$PY_PREFIX" "$STAGE/runtime/python"
# uv refuses to install into an interpreter it manages ("externally managed");
# this copy is ours.
rm -f "$STAGE/runtime/python/lib/python3."*/EXTERNALLY-MANAGED
PY="$STAGE/runtime/python/bin/python3"
[ -x "$PY" ] || die "the copied interpreter does not run: $PY"
# The check that catches a tree pointing anywhere else — BEFORE the installs,
# which would otherwise happily write into the builder's own interpreter.
COPIED_PREFIX="$("$PY" -c 'import sys; print(sys.prefix)')"
[ "$COPIED_PREFIX" = "$STAGE/runtime/python" ] \
  || die "the copied interpreter still points at $COPIED_PREFIX (wanted $STAGE/runtime/python)"
"$PY" -c 'import sys; print(sys.version); print(sys.prefix)'

# --- 2. torch first, and from an explicit index -------------------------------
# LavaSR depends on torch: installed afterwards, it would resolve torch on its
# own and silently replace this one with the default (CUDA) wheel — +3 GB and a
# machine without a GPU.
say "torch from $TORCH_INDEX_URL"
uv pip install --python "$PY" --index-url "$TORCH_INDEX_URL" torch torchaudio

say "base dependencies (requirements-base.txt)"
uv pip install --python "$PY" -r "$ROOT/requirements-base.txt"

# --- 3. the checks that make a runtime trustworthy ----------------------------
say "import checks"
# PYCLEAN_DATA_DIR: importing app.main creates the results directory at import
# time (app/main.py) — keep that out of the source tree.
export PYCLEAN_DATA_DIR="$STAGE/.check-data"
export PYTHONPATH="$ROOT"
REQUIRE_CPU="$REQUIRE_CPU" TORCH_INDEX_URL="$TORCH_INDEX_URL" \
    "$PY" - <<'PYCHECK' || die "the runtime does not import cleanly (see above)"
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
             f"REQUIRE_CPU=0 for a deliberate CUDA build)")
if importlib.util.find_spec("nemo") is not None:
    sys.exit("nemo_toolkit is installed: it weighs several GB and is opt-in "
             "(./run.sh --asr); the desktop build must ship without it")
PYCHECK

# --- 4. the application -------------------------------------------------------
say "application"
mkdir -p "$STAGE/app" "$STAGE/static"
cp -a "$ROOT/app/." "$STAGE/app/"
cp -a "$ROOT/static/." "$STAGE/static/"
rm -rf "$STAGE/app/__pycache__"
cp -a "$ROOT/LICENCE" "$STAGE/LICENCE"
cp -a "$ROOT/packaging/THIRD-PARTY-NOTICES.txt" "$STAGE/THIRD-PARTY-NOTICES.txt"
# the launcher is unfrozen here: this script (see build_runtime.ps1 for the
# Windows .exe, frozen with --noconsole)
cp -a "$ROOT/packaging/launcher/launcher.py" "$STAGE/launcher.py"

cat >"$STAGE/pyclean-audio" <<'LAUNCHER'
#!/bin/sh
# pyclean-audio — starts the bundled server and opens its window.
# Symlinks (/usr/bin/pyclean-audio -> /opt/pyclean-audio/pyclean-audio) are
# followed, so the tree is found from the real location.
self="$0"
while [ -L "$self" ]; do
  link="$(readlink "$self")"
  case "$link" in
    /*) self="$link" ;;
    *) self="$(dirname "$self")/$link" ;;
  esac
done
here="$(CDPATH= cd -- "$(dirname -- "$self")" && pwd -P)"
# Without this the `exec` below fails with "not found" before Python starts, so
# the launcher never logs anything and the failure is invisible from a double
# click or from a Terminal=false .desktop.
if [ ! -x "$here/runtime/python/bin/python3" ]; then
  echo "pyclean-audio: broken installation (no interpreter at $here/runtime/python)" >&2
  exit 1
fi
exec "$here/runtime/python/bin/python3" "$here/launcher.py" "$@"
LAUNCHER
chmod 0755 "$STAGE/pyclean-audio"

# --- 4b. the embedded window (opt-in on Linux) -------------------------------
# A build with pywebview opens its own window and quits when it is closed. One
# without opens the page in the default browser — a first-class experience, the
# page carries the Quit button (POST /api/shutdown).
WEBVIEW=0
if [ "$WITH_WEBVIEW" = "1" ]; then
  say "embedded window: pywebview[gtk]"
  if uv pip install --python "$PY" "pywebview[gtk]" && "$PY" -c 'import gi, webview'; then
    WEBVIEW=1
    echo "webview: bundled (the .deb will depend on libwebkit2gtk-4.1-0)"
  else
    echo "WARNING: pywebview[gtk] could not be installed or imported (GTK/WebKit2GTK development files missing?)." >&2
    echo "WARNING: building in browser mode — the page opens in the default browser" >&2
    echo "WARNING: and carries the Quit button. Install libgtk-3-dev libwebkit2gtk-4.1-dev" >&2
    echo "WARNING: gir1.2-gtk-3.0 and re-run with WITH_WEBVIEW=1 for the embedded window." >&2
  fi
fi

# --- 5. ffmpeg, by bare name --------------------------------------------------
# app/processor.py:38,85 calls `ffprobe` and `ffmpeg` without a path: the
# launcher puts runtime/bin in front of PATH.
say "ffmpeg"
mkdir -p "$STAGE/runtime/bin"
if [ "$FFMPEG_FROM_SYSTEM" = "1" ]; then
  command -v ffmpeg >/dev/null || die "FFMPEG_FROM_SYSTEM=1 but ffmpeg is absent"
  command -v ffprobe >/dev/null || die "FFMPEG_FROM_SYSTEM=1 but ffprobe is absent"
  cp -a "$(command -v ffmpeg)" "$(command -v ffprobe)" "$STAGE/runtime/bin/"
else
  DL_TMP="$(mktemp -d)"
  curl -fsSL "$FFMPEG_URL" -o "$DL_TMP/ffmpeg.tar.xz" || die "cannot download $FFMPEG_URL"
  # Any of the three, or the build stops: shipping an unverified ffmpeg to
  # every user is exactly the defect this pin exists to prevent.
  if command -v sha256sum >/dev/null; then
    got="$(sha256sum "$DL_TMP/ffmpeg.tar.xz" | cut -d" " -f1)"
  elif command -v shasum >/dev/null; then
    got="$(shasum -a 256 "$DL_TMP/ffmpeg.tar.xz" | cut -d" " -f1)"
  elif command -v openssl >/dev/null; then
    got="$(openssl dgst -sha256 "$DL_TMP/ffmpeg.tar.xz" | awk '{print $NF}')"
  else
    die "no sha256 tool (sha256sum, shasum, openssl) to verify the ffmpeg digest: install one, or pass FFMPEG_FROM_SYSTEM=1 after checking that ffmpeg yourself"
  fi
  [ "$got" = "$FFMPEG_SHA256" ] || die "ffmpeg digest mismatch for $FFMPEG_URL: got $got, expected $FFMPEG_SHA256. The binary in every user's install must be the reviewed one: fix the pin, or pass FFMPEG_SHA256=<digest> for another URL."
  tar -xJf "$DL_TMP/ffmpeg.tar.xz" -C "$DL_TMP"
  cp -a "$DL_TMP"/*/ffmpeg "$DL_TMP"/*/ffprobe "$STAGE/runtime/bin/"
fi
chmod 0755 "$STAGE/runtime/bin/ffmpeg" "$STAGE/runtime/bin/ffprobe"
"$STAGE/runtime/bin/ffmpeg" -version >/dev/null || die "the bundled ffmpeg does not run"
"$STAGE/runtime/bin/ffprobe" -version >/dev/null || die "the bundled ffprobe does not run"
# libmp3lame is what app/processor.py:159 encodes with, and MP3 is the default
# output format of /api/enhance_folder: a build without it breaks every job.
"$STAGE/runtime/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -q libmp3lame \
  || die "the bundled ffmpeg has no libmp3lame encoder: pick a build that has it, or pass another FFMPEG_URL"

# --- 6. what was built --------------------------------------------------------
say "BUILD-INFO.txt"
{
  echo "pyclean-audio $VERSION — desktop runtime (linux-x86_64)"
  echo "built on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "python:  $("$PY" -V 2>&1)"
  echo "ffmpeg:  $("$STAGE/runtime/bin/ffmpeg" -version | head -1)"
  echo "ffmpeg source: $FFMPEG_URL"
  echo "ffmpeg sha256: $FFMPEG_SHA256"
  echo "torch index: $TORCH_INDEX_URL (REQUIRE_CPU=$REQUIRE_CPU)"
  echo "embedded window: $([ "$WEBVIEW" = 1 ] && echo "yes (pywebview)" || echo "no (browser mode)")"
  echo
  echo "packages:"
  uv pip list --python "$PY" 2>/dev/null
} >"$ROOT/dist/BUILD-INFO.txt"
# read back by packaging/linux/make-deb.sh (Depends:, description)
printf '%s\n' "$WEBVIEW" >"$ROOT/dist/WEBVIEW"

rm -rf "$STAGE/.check-data" "$DIST"
mkdir -p "$(dirname "$DIST")"
mv "$STAGE" "$DIST"

size="$(du -sh "$DIST" | cut -f1)"
say "done — dist/pyclean-audio ($size)"
if [ "$WEBVIEW" = "1" ]; then
  echo "window: embedded (pywebview); closing it quits the app"
else
  echo "window: browser mode (WITH_WEBVIEW=0) — the page opens in the default browser"
  echo "        and its Quit button stops the app"
fi
echo "next: packaging/linux/make-deb.sh   (.deb, portable .tar.gz)"
echo "      packaging/linux/make-portable.sh"