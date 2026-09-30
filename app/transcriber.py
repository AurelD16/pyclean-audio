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
TARGET_SR = 16000   # the model consumes 16 kHz mono
CHUNK_SEC = 30      # the checkpoint is trained on utterances of max_duration=40 s
                    # (model_config.yaml) and degrades past it: measured on the same
                    # audio (enhanced audio checked bit for bit), with 300 s blocks
                    # the first word comes out at 51 s and the text is condensed; at
                    # 60 s we still lose 16 s at the head and ~16 s at the tail; at
                    # 30 s both ends are clean. A 30 s blob stays under the training
                    # limit and the VRAM peak is ~5× lower than at 300 s.
OVERLAP_SEC = 10    # context read upstream of each block, then discarded: the start
                    # of a blob is its least reliable zone (up to 6.5 s of silence
                    # measured at 30 s), so it is taken from the previous block, which
                    # transcribes it at its own end. Must stay >= that silence,
                    # otherwise the join loses the first words of the next block.
SRT_MAX_CHARS = 42  # width of a readable subtitle (2 lines max)
SRT_MAX_SEC = 6.0   # max duration of a subtitle

# NeMo is noisy (OneLogger, dataloader warnings): keep only what matters.
logging.getLogger("nemo").setLevel(logging.ERROR)
logging.getLogger("one_logger").setLevel(logging.ERROR)
logging.getLogger("pytorch_lightning").setLevel(logging.ERROR)


@dataclass
class Transcript:
    """Transcribed text and timestamped subtitles (Cue)."""
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
    """Seconds -> HH:MM:SS,mmm (SRT format)."""
    seconds = max(0.0, float(seconds))
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def render_srt(cues, max_chars=SRT_MAX_CHARS, max_sec=SRT_MAX_SEC):
    """Render a list of Cue as readable SRT subtitles.

    NeMo's (TDT) timestamps are per word; they are grouped into subtitles of at
    most two lines, with bounded width and duration.
    """
    lines = []
    for i, cue in enumerate(cues, 1):
        lines.append(str(i))
        lines.append(f"{_fmt_ts(cue.start)} --> {_fmt_ts(cue.end)}")
        lines.extend(_wrap(cue.text, max_chars))
        lines.append("")
    return "\n".join(lines)


def _wrap(text, max_chars):
    """Split a text into lines of max_chars, on word boundaries."""
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
    """Normalise NeMo timestamps into increasing (start, end, text) triplets.

    TDT alignment is not perfectly monotonic (a word can end before the previous
    one on a silence): without correction the resulting SRT is unreadable. Accepts
    NeMo's `timestamp['word']` dict as well as a plain list of triplets.
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
    """Normalised NeMo sentences (`timestamp['segment']`), or [] if absent."""
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
    """Build readable subtitles from NeMo timestamps.

    Sentences are preferred (`timestamp['segment']`, already punctuated by the
    model); a sentence too long for two lines is re-cut on the word timestamps.
    Without sentences, words are grouped directly.

    Only words/sentences *starting* in [t0, t1) — times **relative to the chunk
    read** — are kept: that window is what excludes the overlap with the previous
    chunk (see `plan_chunks`).

    `timestamp` accepts the dict NeMo returns (hypothesis.timestamp) as well as a
    plain list of [start, end, word] triplets.
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
            # sentence too long: re-cut it on the words it contains
            inside = [w for w in words if start <= w[0] < end]
            joined = " ".join(w[2] for w in inside)
            if inside and " ".join(joined.split()) == " ".join(text.split()):
                cues.extend(_group(inside, max_chars, max_sec))
            else:
                # the words do not cover the sentence (punctuation, merged
                # tokens): split the text and share the time pro rata rather
                # than lose words
                cues.extend(_split_text(text, start, end, max_chars, max_sec))
    else:
        cues = _group(words, max_chars, max_sec)
    return [Cue(c.start + offset, c.end + offset, c.text) for c in cues]


def _split_text(text, start, end, max_chars, max_sec):
    """Split a long text into subtitles, times shared pro rata by word count (the
    alignment is unavailable, so this stays approximate)."""
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
    """Group words into subtitles of two lines max, preferring to break on
    punctuation."""
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
    """Split [0, frames) into chunks (start, stop, kept_start, kept_stop).

    Each chunk is read with `overlap_sec` seconds of upstream context, but its
    timestamps are only kept on the [kept_start, kept_stop) window: the overlapping
    part was already transcribed — at the end of a chunk, hence reliably — by the
    previous chunk. Kept windows are adjacent (`kept_stop[n] ==
    kept_start[n+1]`): text and subtitles neither duplicate nor lose anything.

    Pure function (tested without a model): the last chunk is truncated to frames.
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
    """Did the model return timestamps (hence subtitles)?"""
    if isinstance(timestamp, dict):
        return bool(timestamp.get("word")) or bool(timestamp.get("segment"))
    return bool(timestamp)


def is_available() -> bool:
    """Is NeMo installed? Optional dependency: `run.sh` only installs the 4 base
    packages, `nemo_toolkit[asr]` comes with `./run.sh --asr`.

    `find_spec` loads nothing (microsecond) while `import nemo` would cost
    several seconds: the UI calls this on every status refresh to disable the
    “Transcribe” checkbox and show the command to run.
    """
    try:
        return importlib.util.find_spec("nemo") is not None
    except (ImportError, ValueError):
        return False


def local_nemo_path() -> str | None:
    """Path of the .nemo file in the local HuggingFace cache, if it is there."""
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

        # The device is chosen once and for all: looping cuda→cpu→cuda corrupts
        # the model (illegal memory access, “⁇” output).
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
            model.half()  # ~1.2 GB of fp16 weights instead of 2.4 GB in fp32
        # Frees the fp32 residue (~2.4 GB) and the load cache: without this the
        # process stays at ~3.6 GB of VRAM for 1.2 GB of useful weights.
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
        """Transcribe a mono WAV (any sample rate) in CHUNK_SEC blocks overlapping
        by OVERLAP_SEC, read in streaming (O(chunk) memory).

        Returns the text and, if NeMo returned timestamps, the subtitles. The text
        is built from the same words as the subtitles (outside the overlap) so they
        cannot contradict each other; without timestamps it falls back to the
        model's raw text. Serialised by a lock: global model state + VRAM shared
        with LavaSR (see also app.cancel.GPU_LOCK).
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
                        # the timestamps are relative to the chunk *read*, which starts
                        # OVERLAP_SEC before the kept window: shift both marks, then
                        # offset by the read start
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
            # false = nemo_toolkit[asr] missing: transcription would fail, so the
            # UI says so instead of leaving a ModuleNotFound error
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
