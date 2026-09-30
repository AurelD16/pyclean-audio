#!/usr/bin/env bash
# pyclean-audio — lance le serveur web (voir README.md).
#
#   ./run.sh            amélioreur audio seulement (défaut, installation légère)
#   ./run.sh --asr      installe en plus nemo_toolkit[asr] → transcription Parakeet
#   ./run.sh --help     cette aide
#
# PORT=9000 ./run.sh pour changer de port, PYCLEAN_WITH_ASR=1 équivaut à --asr.
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
    *) echo "Option inconnue : $1 (voir ./run.sh --help)" >&2; exit 2 ;;
  esac
  shift
done
if [ "${PYCLEAN_WITH_ASR:-0}" = "1" ]; then WITH_ASR=1; fi

# NeMo est absent de l'installation de base : il pèse plusieurs Go et impose ses
# propres versions de torch, on ne l'ajoute que sur demande. La détection se fait
# avec importlib (pas d'import), donc quelques millisecondes.
asr_installed() {
  [ -x .venv/bin/python ] && .venv/bin/python -c \
    'import importlib.util as u, sys; sys.exit(0 if u.find_spec("nemo") else 1)' 2>/dev/null
}

if [ "$WITH_ASR" = "1" ] && [ -d .venv ] && ! asr_installed; then
  echo "Transcription demandée : ajout de nemo_toolkit[asr] dans .venv…"
  uv pip install -q "nemo_toolkit[asr]"
fi

if [ ! -d .venv ]; then
  echo "Création de l'environnement virtuel (.venv)…"
  uv venv .venv
  uv pip install -q \
    "LavaSR @ git+https://github.com/ysharma3501/LavaSR.git" \
    fastapi "uvicorn[standard]" python-multipart
  if [ "$WITH_ASR" = "1" ]; then
    echo "Ajout de nemo_toolkit[asr] pour la transcription (plus long)…"
    uv pip install -q "nemo_toolkit[asr]"
  fi
fi

if asr_installed; then
  echo "Transcription : disponible (Parakeet TDT, checkpoint ~2,4 Go téléchargé au 1er usage)."
else
  echo "Transcription : désactivée — ./run.sh --asr pour l'activer (téléchargement ~2,4 Go)."
fi

echo "Démarrage de pyclean-audio sur http://127.0.0.1:${PORT}"
echo "Premier lancement : le modèle est téléchargé depuis HuggingFace (une seule fois)."
if [ -n "${DISPLAY:-}" ]; then
  ( sleep 3; xdg-open "http://127.0.0.1:${PORT}" >/dev/null 2>&1 || true ) &
fi
# --workers 1 : les jobs sont sérialisés côté application (file d'attente),
# plusieurs workers dupliqueraient les modèles en VRAM pour rien.
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" --workers 1
