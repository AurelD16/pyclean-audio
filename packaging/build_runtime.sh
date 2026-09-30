#!/usr/bin/env bash
# pyclean-audio — builds dist/pyclean-audio/, the tree the desktop launcher runs
# on **Linux** (see packaging/README.md).
#
#   packaging/build_runtime.sh                     # CPU runtime (~2.6 GB)
#   TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 packaging/build_runtime.sh
#   FFMPEG_FROM_SYSTEM=1 packaging/build_runtime.sh   # reuse the host's ffmpeg
#   VERSION=1.1.0 packaging/build_runtime.sh
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
FFMPEG_URL="${FFMPEG_URL:-https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz}"
FFMPEG_FROM_SYSTEM="${FFMPEG_FROM_SYSTEM:-0}"
PYTHON_VERSION="${PYTHON_VERSION:-3.11}"

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
PY_SRC="$(uv python find "$PYTHON_VERSION")"
[ -x "$PY_SRC" ] || die "uv python find returned no interpreter: $PY_SRC"
PY_PREFIX="$(dirname "$(dirname "$PY_SRC")")"
[ -d "$PY_PREFIX/lib" ] || die "not a standalone prefix (no lib/): $PY_PREFIX"

rm -rf "$STAGE"
mkdir -p "$STAGE/runtime"
cp -a "$PY_PREFIX" "$STAGE/runtime/python"
PY="$STAGE/runtime/python/bin/python3"
[ -x "$PY" ] || die "the copied interpreter does not run: $PY"
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
"$PY" - <<'PYCHECK' || die "the runtime does not import cleanly (see above)"
import importlib.util
import sys

for name in ("torch", "torchaudio", "numpy", "soundfile", "fastapi", "uvicorn",
             "LavaSR", "app.main"):
    __import__(name)
    print(f"  ok  {name}")

import torch

if torch.version.cuda is not None:
    sys.exit(f"CUDA torch {torch.version} slipped in: a CPU runtime is expected "
             f"(TORCH_INDEX_URL={sys.argv[1] if len(sys.argv) > 1 else 'unset'})")
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
exec "$here/runtime/python/bin/python3" "$here/launcher.py" "$@"
LAUNCHER
chmod 0755 "$STAGE/pyclean-audio"

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
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "$FFMPEG_URL" -o "$tmp/ffmpeg.tar.xz" || die "cannot download $FFMPEG_URL"
  tar -xJf "$tmp/ffmpeg.tar.xz" -C "$tmp"
  cp -a "$tmp"/*/ffmpeg "$tmp"/*/ffprobe "$STAGE/runtime/bin/"
fi
chmod 0755 "$STAGE/runtime/bin/ffmpeg" "$STAGE/runtime/bin/ffprobe"
"$STAGE/runtime/bin/ffmpeg" -version >/dev/null || die "the bundled ffmpeg does not run"
"$STAGE/runtime/bin/ffprobe" -version >/dev/null || die "the bundled ffprobe does not run"

# --- 6. what was built --------------------------------------------------------
say "BUILD-INFO.txt"
{
  echo "pyclean-audio $VERSION — desktop runtime (linux-x86_64)"
  echo "built on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "python:  $("$PY" -V 2>&1)"
  echo "ffmpeg:  $("$STAGE/runtime/bin/ffmpeg" -version | head -1)"
  echo "torch index: $TORCH_INDEX_URL"
  echo
  echo "packages:"
  uv pip list --python "$PY"
} >"$ROOT/dist/BUILD-INFO.txt"

rm -rf "$STAGE/.check-data" "$DIST"
mkdir -p "$(dirname "$DIST")"
mv "$STAGE" "$DIST"

size="$(du -sh "$DIST" | cut -f1)"
say "done — dist/pyclean-audio ($size)"
echo "next: packaging/linux/make-deb.sh   (.deb, portable .tar.gz)"
echo "      packaging/linux/make-portable.sh"