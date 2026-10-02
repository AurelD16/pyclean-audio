# pyclean-audio — container images.
#
# Two officially supported images, both with transcription (nemo_toolkit[asr]):
#
#   docker build --target cpu -t pyclean-audio:cpu .     # default target
#   docker build --target gpu -t pyclean-audio:gpu .     # CUDA (needs --gpus all)
#   docker build --target test .                         # runs pytest -rs, exits
#
# `cpu` is the last stage on purpose: `docker build .` without --target must
# produce the image that runs anywhere. The published images are
# ghcr.io/aureld16/pyclean-audio:{cpu,gpu} (see .github/workflows/).
#
# The base dependency set is the 4 packages installed by run.sh:45-47, copied
# as requirements-base.txt (requirements.txt keeps its "everything, ASR
# included" meaning). The container never calls run.sh (that installs at
# start-up and hardcodes --host 127.0.0.1); its venv is built at build time.
#
# Tags and dependencies are unpinned on purpose (like the rest of the project):
# the images are not bit-reproducible, and "rebuild next year" can bring a new
# torch. A lock file would contradict run.sh, which installs the same
# unpinned set.


# --- runtime, shared by both variants ------------------------------------------
# Everything that must exist in both images lives here, once: ENTRYPOINT, CMD,
# ENV, HEALTHCHECK, the non-root user and the app files are written a single
# time, so the two variants cannot drift apart.
FROM python:3.11-slim-bookworm AS base

# Debian, not alpine: torch needs glibc, and Debian's ffmpeg ships the
# libmp3lame encoder app/processor.py:159 asks for, plus aac (:152) and
# pcm_s16le (:142).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates ffmpeg \
 && rm -rf /var/lib/apt/lists/*

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


# --- venvs ----------------------------------------------------------------------
# build-essential is a safety net for a dependency without a cp311 manylinux
# wheel; git is mandatory, LavaSR is a git dependency; ca-certificates for the
# https clone. None of them reach the runtime image.
FROM python:3.11-slim-bookworm AS builder-cpu

# CPU wheels: the image stays small and runs on any host, since the device is
# decided once at model load by torch.cuda.is_available() (app/enhancer.py:149,
# app/transcriber.py:306).
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

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

# Transcription is not optional any more: it is in both published images. NeMo
# pulls transformers, which constrains huggingface-hub — see README.md.
RUN uv pip install --python /opt/venv/bin/python "nemo_toolkit[asr]"

RUN rm -rf /root/.cache /tmp/*


# Identical, with the CUDA wheels. cu130 matches the torch the project is
# measured on (AGENTS.md); override with --build-arg TORCH_INDEX_URL=…/cu126
# when an older driver needs it.
FROM python:3.11-slim-bookworm AS builder-gpu

ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu130

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv && uv venv /opt/venv

WORKDIR /src
COPY requirements-base.txt ./

RUN uv pip install --python /opt/venv/bin/python --index-url "$TORCH_INDEX_URL" \
      torch torchaudio

RUN uv pip install --python /opt/venv/bin/python -r requirements-base.txt

RUN uv pip install --python /opt/venv/bin/python "nemo_toolkit[asr]"

RUN rm -rf /root/.cache /tmp/*


# --- variants -------------------------------------------------------------------
FROM base AS cpu
COPY --from=builder-cpu /opt/venv /opt/venv

FROM base AS gpu
COPY --from=builder-gpu /opt/venv /opt/venv

# Opt-in: tests the environment the CPU image actually ships, NeMo included
# (so `--target test` is noticeably slower than a plain `pytest`). ffmpeg is in
# `base`, which is the point: tests/conftest.py:16-21 silently skips the
# probe()/process_file() tests when ffmpeg or ffprobe is missing, so a suite
# without it reports green while covering much less.
FROM cpu AS test
USER root
COPY pytest.ini requirements-dev.txt ./
COPY tests/ ./tests/
RUN pip install --no-cache-dir uv \
 && uv pip install --python /opt/venv/bin/python -r requirements-dev.txt
USER pyclean
# -rs: print the reason of every skip, so an ffmpeg-less run cannot pass silently.
RUN python -m pytest -rs

# The default target is the last stage of the file, and it has to be the CPU
# image. `test` can only derive from a stage declared *before* it, so the
# default is this alias of `cpu` — same image, same digest, no extra layer.
FROM cpu AS default