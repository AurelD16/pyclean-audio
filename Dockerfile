# pyclean-audio — container image.
#
# The base dependency set is the 4 packages installed by run.sh:45-47, copied
# as requirements-base.txt (requirements.txt keeps its "everything, ASR
# included" meaning). The container never calls run.sh (that installs at
# start-up and hardcodes --host 127.0.0.1); its venv is built at build time.
#
#   docker build -t pyclean-audio .                                  # CPU
#   docker build -t pyclean-audio --build-arg INSTALL_ASR=true .      # + transcription
#   docker build -t pyclean-audio --build-arg \
#       TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130 .     # CUDA
#   docker build --target test .                                     # run the test suite
#
# Tags and dependencies are unpinned on purpose (like the rest of the project):
# the image is not bit-reproducible, and "rebuild next year" can bring a new
# torch. A lock file would contradict run.sh, which installs the same
# unpinned set.


# --- build --------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

# CPU wheels by default: the device is decided once at model load by
# torch.cuda.is_available() (app/enhancer.py:149-152, app/transcriber.py:306-309),
# so a CPU image runs everywhere, and a GPU host only needs a different
# TORCH_INDEX_URL (plus --gpus all at run time).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
# Transcription is opt-in, like run.sh --asr: nemo_toolkit[asr] weighs several GB
# and pins its own torch version (run.sh:29-31).
ARG INSTALL_ASR=false

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

# build-essential is a safety net for a dependency without a cp311 manylinux
# wheel; git is mandatory, LavaSR is a git dependency; ca-certificates for the
# https clone. None of them reach the runtime image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv && uv venv /opt/venv

WORKDIR /src
COPY requirements-base.txt ./

# torch first, and from the index: LavaSR depends on torch itself, so letting it
# resolve would pull the default (CUDA) wheel from PyPI and silently replace the
# one installed here.
RUN uv pip install --python /opt/venv/bin/python --index-url "$TORCH_INDEX_URL" \
      torch torchaudio

RUN uv pip install --python /opt/venv/bin/python -r requirements-base.txt

RUN if [ "$INSTALL_ASR" = "true" ]; then \
      echo "Adding nemo_toolkit[asr] for transcription (several GB)…" ; \
      uv pip install --python /opt/venv/bin/python "nemo_toolkit[asr]" ; \
    fi

# Nothing outside /opt/venv is copied out; drop the caches and the pip/uv build
# residue so they cannot end up in the runtime image by accident.
RUN rm -rf /root/.cache /tmp/*


# --- test (opt-in: docker build --target test .) --------------------------------
# ffmpeg is mandatory here, not optional: tests/conftest.py:16-21 silently skips
# the probe()/process_file() tests when ffmpeg or ffprobe is missing, so a suite
# without it reports green while covering much less.
FROM builder AS test

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pytest.ini requirements-dev.txt ./
COPY app/ ./app/
COPY static/ ./static/
COPY tests/ ./tests/

RUN uv pip install --python /opt/venv/bin/python -r requirements-dev.txt

# -rs: print the reason of every skip, so an ffmpeg-less run cannot pass silently.
RUN python -m pytest -rs


# --- runtime (default target) ---------------------------------------------------
FROM python:3.11-slim-bookworm AS final

# Debian, not alpine: torch needs glibc, and Debian's ffmpeg ships the
# libmp3lame encoder app/processor.py:159 asks for, plus aac (:152) and
# pcm_s16le (:142).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Non-root is required, not decoration: app/main.py:33-35 creates data/jobs at
# import time and HuggingFace needs a writable cache. uid/gid 1000 keeps the
# mounted cache volume usable without a root-owned mess.
RUN groupadd --gid 1000 pyclean \
 && useradd --uid 1000 --gid 1000 --no-log-init --create-home \
            --shell /usr/sbin/nologin pyclean

ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/cache/huggingface \
    PORT=8787 \
    PYTHONUNBUFFERED=1

WORKDIR /app
# Only what the app needs at runtime: app/, static/ (mounted by
# app/main.py:758) and the entrypoint. tests/, run.sh, .git and the
# requirements files stay out of the image.
COPY app/ ./app/
COPY static/ ./static/
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod 0755 /app/docker-entrypoint.sh \
 && mkdir -p /cache/huggingface \
 && chown -R pyclean:pyclean /app /cache

USER pyclean
EXPOSE 8787

# GET /api/status, never HEAD: HEAD returns 404 on every route of this app
# (README.md, "Limitations"). Shell form, so the live PORT is read at run time.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8787') + '/api/status', timeout=4).read()"

# --workers 1 is mandatory: one in-process queue with a single consumer thread
# (app/main.py:43,207-231), job state in a dict, and two lock-guarded model
# singletons — a second worker would duplicate the models in VRAM and lose the
# job states. The port is overridden by the entrypoint from $PORT.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
