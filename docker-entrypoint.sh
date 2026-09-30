#!/bin/sh
# pyclean-audio container entrypoint.
#
# It exists for one reason: the `exec` at the end. The exec-form CMD cannot
# expand ${PORT}, so the port is resolved here, and then this script *replaces
# itself* with uvicorn, which makes uvicorn PID 1. Without the exec, PID 1 would
# be /bin/sh, `docker stop`'s SIGTERM would never reach uvicorn, and every stop
# would be a 10 s timeout then a SIGKILL with a possibly orphaned ffmpeg.
#
# Nothing here may put itself in another process group: app/processor.py starts
# ffmpeg with start_new_session and kills it with os.killpg.
set -e

PORT="${PORT:-8787}"

# Same detection as run.sh:32-35 (importlib, no import), same wording.
if python -c 'import importlib.util as u, sys; sys.exit(0 if u.find_spec("nemo") else 1)' 2>/dev/null; then
  echo "Transcription: available (Parakeet TDT, ~2.4 GB checkpoint downloaded on first use)."
else
  echo "Transcription: disabled — rebuild with --build-arg INSTALL_ASR=true (~2.4 GB download)."
fi

echo "Starting pyclean-audio on http://127.0.0.1:${PORT}"
echo "First run: the model is downloaded from HuggingFace (once, into \$HF_HOME)."
echo "No authentication: keep the published port on 127.0.0.1."

# The default CMD is the uvicorn line: rebuild it with the resolved $PORT. Any
# other command (`docker run pyclean-audio sh`, `python -m app.cli …`) is run
# untouched, so the image stays debuggable.
case "$*" in
  "" | "python -m uvicorn app.main:app"*)
    set -- python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --workers 1
    ;;
esac

exec "$@"
