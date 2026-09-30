import gc
import importlib.util
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio

from .cancel import raise_if_cancelled

MODEL_REPO = "nvidia/parakeet-tdt-0.6b-v3"
NEMO_FILENAME = "parakeet-tdt-0.6b-v3.nemo"
TARGET_SR = 16000   # le modèle consomme 16 kHz mono
CHUNK_SEC = 30      # le checkpoint est entraîné sur des énoncés de max_duration=40 s
                    # (dans model_config.yaml) et se dégrade au-delà : mesuré sur le
                    # même audio (contrôle bit à bit de l'audio amélioré), en bloc de
                    # 300 s le premier mot sort à 51 s et le texte est condensé ; en
                    # 60 s on perd encore 16 s en tête et ~16 s en queue ; en 30 s les
                    # deux extrémités sont propres. Un blob de 30 s reste sous la
                    # limite d'entraînement, le pic VRAM est ~5× plus bas qu'à 300 s.
OVERLAP_SEC = 10    # contexte lu en amont de chaque bloc, jeté ensuite : le début
                    # d'un blob est sa zone la moins fiable (jusqu'à 6,5 s de blanc
                    # mesurés en 30 s), on le prend chez le bloc précédent, qui le
                    # transcrit en fin de blob. Doit rester ≥ ce blanc, sinon la
                    # jointure perd les premiers mots du bloc suivant.
SRT_MAX_CHARS = 42  # largeur d'un sous-titre lisible (2 lignes max)
SRT_MAX_SEC = 6.0   # durée max d'un sous-titre

# NeMo est logueux (OneLogger, avertissements de dataloader) : on garde l'essentiel.
logging.getLogger("nemo").setLevel(logging.ERROR)
logging.getLogger("one_logger").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)


@dataclass
class Transcript:
    """Texte transcrit et sous-titres horodatés (Cue)."""
    text: str
    cues: list = field(default_factory=list)

    def __bool__(self):
        return bool(self.text)


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _fmt_ts(seconds):
    """Secondes -> HH:MM:SS,mmm (format SRT)."""
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(cues, max_chars=SRT_MAX_CHARS, max_sec=SRT_MAX_SEC):
    """Rend une liste de Cue en sous-titres SRT lisibles.

    Les horodatages de NeMo (TDT) sont par mot ; on les regroupe en sous-titres
    de deux lignes au plus, de largeur et de durée bornées.
    """
    lines = []
    for i, cue in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_ts(cue.start)} --> {_fmt_ts(cue.end)}")
        lines.extend(_wrap(cue.text, max_chars))
        lines.append("")
    return "\n".join(lines)


def _wrap(text, max_chars):
    """Découpe un texte en lignes de max_chars, sur les frontiers de mots."""
    words = text.split()
    if not words:
        return [text]
    out, cur = [], words[0]
    for w in words[1:]:
        if len(cur) + 1 + len(w) <= max_chars:
            cur += " " + w
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    return out


def _norm_words(raw):
    """Normalise les horodatages NeMo en (début, fin, texte) croissants.

    L'alignement TDT n'est pas parfaitement monotone (un mot peut finir avant
    le précédent sur un silence) : sans correction, le SRT produit est
    illisible. Accepte un dictionnaire `timestamp['word']` de NeMo comme une
    liste de triplets.
    """
    words = []
    prev_end = 0.0
    for item in raw or ():
        try:
            if isinstance(item, dict):
                start, end = float(item["start"]), float(item["end"])
                text = str(item.get("word", "")).strip()
            else:
                start, end = float(item[0]), float(item[1])
                text = str(item[2]).strip() if len(item) > 2 else ""
        except (TypeError, IndexError, KeyError, ValueError):
            continue
        if not text:
            continue
        if end < start:
            start, end = end, start
        start = max(start, prev_end, 0.0)
        if end <= start:
            end = start + 0.2
        words.append([start, end, text])
        prev_end = end
    return words


def _norm_segments(raw):
    """Phrases de NeMo (`timestamp['segment']`) normalisées, ou [] si absentes."""
    segs = []
    for item in raw or ():
        try:
            if isinstance(item, dict):
                start, end = float(item["start"]), float(item["end"])
                text = str(item.get("segment", "")).strip()
            else:
                start, end = float(item[0]), float(item[1])
                text = str(item[2]).strip() if len(item) > 2 else ""
        except (TypeError, IndexError, KeyError, ValueError):
            continue
        if text and end > start:
            segs.append((start, end, text))
    return segs


def to_cues(timestamp, offset=0.0, t0=0.0, t1=math.inf,
            max_chars=SRT_MAX_CHARS, max_sec=SRT_MAX_SEC):
    """Construit des sous-titres lisibles à partir des horodatages NeMo.

    On préfère les phrases (`timestamp['segment']`, déjà ponctuées par le
    modèle) ; une phrase trop longue pour deux lignes est redécoupée sur les
    horodatages de mots. Sans phrases, on regroupe les mots directement.

    Seuls les mots/phrases démarrant dans [t0, t1) — temps **relatifs au bloc
    lu** — sont conservés : c'est la fenêtre qui exclut le chevauchement avec le
    bloc précédent (voir `plan_chunks`).

    `timestamp` accepte le dict renvoyé par NeMo (hypothesis.timestamp) comme
    une simple liste de triplets [début, fin, mot].
    """
    if isinstance(timestamp, dict):
        words = [w for w in _norm_words(timestamp.get("word")) if t0 <= w[0] < t1]
        segs = [s for s in _norm_segments(timestamp.get("segment")) if t0 <= s[0] < t1]
    else:
        words = [w for w in _norm_words(timestamp) if t0 <= w[0] < t1]
        segs = []

    cues = []
    if segs:
        for start, end, text in segs:
            if len(text) <= max_chars * 2 and end - start <= max_sec:
                cues.append(Cue(start, end, text))
                continue
            # phrase trop longue : on la recoupe sur les mots qu'elle contient
            inside = [w for w in words if start <= w[0] < end]
            joined = " ".join(w[2] for w in inside)
            if inside and " ".join(joined.split()) == " ".join(text.split()):
                cues.extend(_group(inside, max_chars, max_sec))
            else:
                # les mots ne couvrent pas la phrase (ponctuation, tokens
                # fusionnés) : on découpe le texte en répartissant le temps au
                # prorata, plutôt que perdre des mots
                cues.extend(_split_text(text, start, end, max_chars, max_sec))
    else:
        cues = _group(words, max_chars, max_sec)
    return [Cue(c.start + offset, c.end + offset, c.text) for c in cues]


def _split_text(text, start, end, max_chars, max_sec):
    """Découpe un texte long en sous-titres, temps répartis au prorata du nombre
    de mots (l'alignement n'étant pas disponible, on reste approximatif)."""
    words = text.split()
    if not words:
        return [Cue(start, end, text)]
    groups, cur = [], [words[0]]
    for w in words[1:]:
        if len(" ".join(cur)) + 1 + len(w) > max_chars * 2:
            groups.append(cur)
            cur = [w]
        else:
            cur.append(w)
    groups.append(cur)
    cues, idx, total = [], 0, len(words)
    for g in groups:
        f0, f1 = idx / total, (idx + len(g)) / total
        cues.append(Cue(start + f0 * (end - start), start + f1 * (end - start),
                        " ".join(g)))
        idx += len(g)
    return cues


def _group(words, max_chars, max_sec):
    """Regroupe des mots en sous-titres de deux lignes max, en coupant de
    préférence sur la ponctuation."""
    cues = []
    cur, cur_start, cur_end = [], 0.0, 0.0
    for start, end, text in words:
        if cur:
            joined = len(" ".join(cur + [text]))
            full = len(" ".join(cur)) + 1 + len(text) > max_chars * 2
            too_long = (end - cur_start) > max_sec
            ends_sentence = cur[-1].endswith((".", "?", "!", "…"))
            if full or too_long or (ends_sentence and joined > max_chars):
                cues.append(Cue(cur_start, cur_end, " ".join(cur)))
                cur = []
        if not cur:
            cur_start = start
        cur.append(text)
        cur_end = end
    if cur:
        cues.append(Cue(cur_start, cur_end, " ".join(cur)))
    return cues


def plan_chunks(frames, samplerate, chunk_sec=CHUNK_SEC, overlap_sec=OVERLAP_SEC):
    """Découpe [0, frames) en blocs (début, fin, début_gardé, fin_gardée).

    Chaque bloc est lu avec `overlap_sec` secondes de contexte en amont, mais ses
    horodatages ne sont retenus que sur la fenêtre [début_gardé, fin_gardée) : la
    partie chevauchée a déjà été transcrite — en fin de bloc, donc de façon fiable
    — par le bloc précédent. Les fenêtres gardées sont adjacentes
    (`fin_gardée[n] == début_gardé[n+1]`) : le texte et les sous-titres ne se
    doublent pas, et rien n'est perdu.

    Fonction pure (testée sans modèle) : le dernier bloc est tronqué à frames.
    """
    chunk = max(1, int(chunk_sec * samplerate))
    ov = max(0, min(int(overlap_sec * samplerate), chunk - 1))
    step = chunk - ov
    ranges = []
    i = 0
    while True:
        start = max(0, i * step - ov)
        stop = min(start + chunk, frames)
        keep_end = frames if stop >= frames else min((i + 1) * step, frames)
        ranges.append((start, stop, min(i * step, frames), keep_end))
        if stop >= frames:
            break
        i += 1
    return ranges


def _has_timestamps(timestamp) -> bool:
    """Le modèle a-t-il rendu des horodatages (donc des sous-titres) ?"""
    if isinstance(timestamp, dict):
        return bool(timestamp.get("word")) or bool(timestamp.get("segment"))
    return bool(timestamp)


def is_available() -> bool:
    """NeMo est-il installé ? Dépendance facultative : `run.sh` n'installe que
    les 4 paquets de base, `nemo_toolkit[asr]` arrive avec `./run.sh --asr`.

    `find_spec` ne charge rien (microseconde) alors qu'un `import nemo`
    coûterait plusieurs secondes : l'interface appelle ceci à chaque rafraîchissement
    de l'état pour désactiver la case « Transcrire » et indiquer la commande.
    """
    try:
        return importlib.util.find_spec("nemo") is not None
    except (ImportError, ValueError):
        return False


def local_nemo_path() -> str | None:
    """Chemin du .nemo dans le cache local HuggingFace, s'il existe déjà."""
    hub = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
    repo_dir = "models--" + MODEL_REPO.replace("/", "--")
    hits = sorted(hub.glob(f"{repo_dir}/snapshots/*/{NEMO_FILENAME}"))
    return str(hits[0]) if hits else None


class ParakeetTranscriber:
    def __init__(self):
        self._lock = threading.Lock()
        self._model = None
        self._status = "idle"
        self._error = None
        self._device = None
        self._loaded_at = None

    def _load(self):
        from nemo.collections.asr.models import ASRModel

        # Le device est choisi une fois pour toutes : boucler cuda→cpu→cuda
        # corrompt le modèle (illegal memory access, sortie « ⁇ »).
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._device = device
        t0 = time.time()
        path = local_nemo_path()
        if path is not None:
            model = ASRModel.restore_from(path)
        else:
            model = ASRModel.from_pretrained(MODEL_REPO)
        model.eval()
        if device == "cuda":
            model.half()  # ~1,2 Go de poids en fp16 au lieu de 2,4 Go en fp32
        # Libère le résidu fp32 (~2,4 Go) et le cache de chargement : sans ça
        # le processus reste à ~3,6 Go de VRAM pour 1,2 Go utiles.
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        self._loaded_at = time.time() - t0
        return model

    def _ensure_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._status = "loading"
                    try:
                        self._model = self._load()
                        self._status = "ready"
                        self._error = None
                    except Exception as e:
                        self._status = "error"
                        self._error = str(e)
                        raise
        return self._model

    def preload(self):
        try:
            self._ensure_model()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def transcribe(self, wav_path, on_progress=None, cancel=None) -> Transcript:
        """Transcrit un WAV mono (tout taux d'échantillonnage) par blocs de
        CHUNK_SEC chevauchés de OVERLAP_SEC, en lecture streaming (mémoire
        O(bloc)).

        Retourne le texte et, si NeMo a rendu les horodatages, les sous-titres.
        Le texte est bâti sur les mêmes mots que les sous-titres (hors
        chevauchement) pour qu'ils ne se contredisent pas ; sans horodatages on
        retombe sur le texte brut du modèle. Sérialisé par verrou : état global
        du modèle + VRAM partagée avec LavaSR (voir aussi app.cancel.GPU_LOCK).
        """
        model = self._ensure_model()
        with self._lock:
            if on_progress:
                on_progress(0.0)
            info = sf.info(str(wav_path))
            if info.frames == 0:
                return Transcript("", [])
            src_sr = info.samplerate
            ranges = plan_chunks(info.frames, src_sr)
            n_chunks = len(ranges)
            parts = []
            cues = []
            with sf.SoundFile(str(wav_path)) as src:
                for i, (start, stop, keep_start, keep_stop) in enumerate(ranges):
                    raise_if_cancelled(cancel)
                    src.seek(start)
                    data = src.read(stop - start, dtype="float32", always_2d=True)
                    mono = data.mean(axis=1)
                    if src_sr != TARGET_SR:
                        t = torch.from_numpy(np.ascontiguousarray(mono)).unsqueeze(0)
                        t = torchaudio.functional.resample(t, src_sr, TARGET_SR)
                        arr = t.squeeze(0).numpy()
                    else:
                        arr = mono
                    out = model.transcribe(
                        [np.ascontiguousarray(arr, dtype=np.float32)],
                        return_hypotheses=True, num_workers=0, timestamps=True,
                    )
                    if out and out[0].text.strip():
                        # les horodatages sont relatifs au bloc *lu*, qui commence
                        # `OVERLAP_SEC` avant la fenêtre gardée : on translate les
                        # deux repères, puis on décale sur le début de lecture
                        ts = getattr(out[0], "timestamp", None)
                        kept = to_cues(ts, start / src_sr,
                                       (keep_start - start) / src_sr,
                                       (keep_stop - start) / src_sr)
                        cues.extend(kept)
                        if kept:
                            parts.append(" ".join(c.text for c in kept))
                        elif not _has_timestamps(ts):
                            parts.append(out[0].text.strip())
                    if on_progress:
                        on_progress((i + 1) / n_chunks)
        return Transcript(" ".join(parts), cues)

    def status(self):
        return {
            "status": self._status,
            "device": self._device,
            "error": self._error,
            "load_time": round(self._loaded_at, 1) if self._loaded_at else None,
            # false = nemo_toolkit[asr] absent : la transcription échouerait,
            # l'interface le montre au lieu de laisser une erreur ModuleNotFound
            "available": is_available(),
        }


_transcriber = None
_transcriber_init = threading.Lock()


def get_transcriber() -> ParakeetTranscriber:
    global _transcriber
    if _transcriber is None:
        with _transcriber_init:
            if _transcriber is None:
                _transcriber = ParakeetTranscriber()
    return _transcriber
