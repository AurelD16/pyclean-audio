#!/usr/bin/env bash
# pyclean-audio — lance le serveur web (voir README.md).
#
#   ./run.sh            audio enhancement only (default, light install)
#   ./run.sh --asr      also installs nemo_toolkit[asr] → Parakeet transcription
#   ./run.sh --help     this help
#
# PORT=9000 ./run.sh changes the port, PYCLEAN_WITH_ASR=1 is the same as --asr.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
WITH_ASR=0

usage() {
  sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --asr|--transcribe) WITH_ASR=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (see ./run.sh --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ "${PYCLEAN_WITH_ASR:-0}" = "1" ]; then WITH_ASR=1; fi

# NeMo is out of the base install: it weighs several GB and pins its own torch
# versions, so it is only added on request. Detection uses importlib (no import),
# hence a few milliseconds.
asr_installed() {
  [ -x .venv/bin/python ] && .venv/bin/python -c \
    'import importlib.util as u, sys; sys.exit(0 if u.find_spec("nemo") else 1)' 2>/dev/null
}

if [ "$WITH_ASR" = "1" ] && [ -d .venv ] && ! asr_installed; then
  echo "Transcription requested: adding nemo_toolkit[asr] to .venv…"
  uv pip install -q "nemo_toolkit[asr]"
fi

if [ ! -d .venv ]; then
  echo "Creating the virtual environment (.venv)…"
  uv venv .venv
  uv pip install -q \
    "LavaSR @ git+https://github.com/ysharma3501/LavaSR.git" \
    fastapi "uvicorn[standard]" python-multipart
  if [ "$WITH_ASR" = "1" ]; then
    echo "Adding nemo_toolkit[asr] for transcription (slower)…"
    uv pip install -q "nemo_toolkit[asr]"
  fi
fi

if asr_installed; then
  echo "Transcription: available (Parakeet TDT, ~2.4 GB checkpoint downloaded on first use)."
else
  echo "Transcription: disabled — ./run.sh --asr to enable it (~2.4 GB download)."
fi

echo "Starting pyclean-audio on http://127.0.0.1:${PORT}"
echo "First run: the model is downloaded from HuggingFace (once)."
if [ -n "${DISPLAY:-}" ]; then
  ( sleep 3; xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
fi
# --workers 1: jobs are serialized application-side (queue), several workers
# would only duplicate the models in VRAM for nothing.
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --workers 1
