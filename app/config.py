"""Configuration par variables d'environnement — toutes optionnelles.

Une valeur absente ou illisible retombe silencieusement sur le défaut : une
mauvaise variable ne doit jamais empêcher le serveur de démarrer.
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


# --- bornes de traitement -----------------------------------------------------
MAX_SIZE = env_int("PYCLEAN_MAX_SIZE", 2 * 1024**3)        # octets, par fichier
MAX_DURATION = env_int("PYCLEAN_MAX_DURATION", 10_000)     # secondes (~2 h 47)
MAX_FOLDER_FILES = env_int("PYCLEAN_MAX_FILES", 500)       # fichiers par dossier
MAX_FOLDER_TOTAL = env_int("PYCLEAN_MAX_FOLDER_TOTAL", 8 * 1024**3)  # octets
FFMPEG_TIMEOUT = env_int("PYCLEAN_FFMPEG_TIMEOUT", 0) or None  # 0 = illimité

# --- cycle de vie des résultats ----------------------------------------------
JOB_TTL = env_int("PYCLEAN_JOB_TTL", 6 * 3600)   # secondes avant purge (6 h)
JOB_MAX = env_int("PYCLEAN_JOB_MAX", 200)        # jobs conservés en mémoire
PURGE_INTERVAL = env_int("PYCLEAN_PURGE_INTERVAL", 60)  # balayage, secondes
QUEUE_MAX = env_int("PYCLEAN_QUEUE_MAX", 20)     # demandes en attente max

# --- modèles ------------------------------------------------------------------
PRELOAD_ASR = env_flag("PYCLEAN_PRELOAD_ASR", False)  # charger Parakeet au boot
