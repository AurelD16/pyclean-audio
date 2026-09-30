"""Configuration through environment variables — all optional.

A missing or unreadable value silently falls back to the default: a bad
variable must never stop the server from starting.
"""

import os


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
