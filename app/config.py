"""Configuration through environment variables — all optional.

A missing or unreadable value silently falls back to the default: a bad
variable must never stop the server from starting.
"""

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ[name])
    except (KeyError, ValueError):
        return default
    return v if v > 0 else default


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "oui", "yes", "on")


def data_dir() -> Path:
    """Where the jobs (results) are written.

    `BASE/data/jobs` by default, unchanged; `PYCLEAN_DATA_DIR` overrides it.
    The desktop launcher sets it, because its install directory is read-only
    for a normal user (`C:\\Program Files`) and uninstalling must not take the
    results away. A function, not a constant: a test exercises it without
    reloading `app.main`.
    """
    override = os.environ.get("PYCLEAN_DATA_DIR", "").strip()
    return Path(override) if override else BASE / "data" / "jobs"


# --- processing limits -------------------------------------------------------
MAX_SIZE = env_int("PYCLEAN_MAX_SIZE", 2 * 1024**3)        # bytes, per file
MAX_DURATION = env_int("PYCLEAN_MAX_DURATION", 10_000)     # seconds (~2 h 47)
MAX_FOLDER_FILES = env_int("PYCLEAN_MAX_FILES", 500)       # files per folder
MAX_FOLDER_TOTAL = env_int("PYCLEAN_MAX_FOLDER_TOTAL", 8 * 1024**3)  # bytes
FFMPEG_TIMEOUT = env_int("PYCLEAN_FFMPEG_TIMEOUT", 0) or None  # 0 = unlimited

# --- results lifecycle ------------------------------------------------------
JOB_TTL = env_int("PYCLEAN_JOB_TTL", 6 * 3600)   # seconds before purge (6 h)
JOB_MAX = env_int("PYCLEAN_JOB_MAX", 200)        # jobs kept in memory
PURGE_INTERVAL = env_int("PYCLEAN_PURGE_INTERVAL", 60)  # sweep, seconds
QUEUE_MAX = env_int("PYCLEAN_QUEUE_MAX", 20)     # max waiting requests

# --- models -------------------------------------------------------------------
PRELOAD_ASR = env_flag("PYCLEAN_PRELOAD_ASR", False)  # load Parakeet at boot

# --- distribution --------------------------------------------------------------
# Set by the desktop launcher (packaging/launcher/launcher.py). The packaged
# build has no shell script to run: the page reads `desktop` in GET /api/status
# and drops its "./run.sh --asr" wording. False everywhere else, so the CLI and
# the API are untouched.
DESKTOP = env_flag("PYCLEAN_DESKTOP", False)
