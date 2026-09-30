import json
import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from .cancel import GPU_LOCK, raise_if_cancelled
from .config import FFMPEG_TIMEOUT, MAX_DURATION

StageCb = Callable[[str, float], None]

ALLOWED_EXT = {
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus",
    ".wma", ".aiff", ".aif", ".mp4", ".mkv", ".webm", ".mov", ".avi",
    ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".flv",
}


def iter_media_files(root: Path):
    """Renvoie, triés, tous les fichiers audio/vidéo pris en charge sous root (récursif)."""
    root = root.resolve()
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in ALLOWED_EXT
    )


def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe a échoué : {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout)
    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    astream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "video": vstream is not None,
        "audio": astream is not None,
        "duration": duration,
    }


def _kill(p: subprocess.Popen) -> None:
    """Tue ffmpeg *et* ses éventuels enfants.

    Les enfants hérite du tube de sortie : sans kill du groupe de processus, un
    petit-enfant survive, le tube ne se ferme jamais et la lecture stdout reste
    bloquée pour toujours.
    """
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def run_ffmpeg(args: list, duration: float | None, frac_cb: Callable[[float], None],
               timeout: float | None = None):
    """Lance ffmpeg et relaie sa progression.

    - stderr va dans un fichier temporaire (jamais dans un pipe) : un PIPE non
      drainé se remplit à 64 Ko, ffmpeg se bloque en écriture et le job reste
      suspendu indéfiniment.
    - `frac_cb` peut lever (annulation) : ffmpeg est alors tué avant de remonter.
    - `timeout` (secondes) arme un chien de garde qui tue un ffmpeg bloqué,
      même s'il n'émet plus la moindre ligne de progression.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y",
           "-loglevel", "error", "-progress", "pipe:1", *map(str, args)]

    timed_out = threading.Event()
    # Le fichier temporaire est relu après la fin du processus (voir plus bas).
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                             text=True, bufsize=1, start_new_session=True)
        if p.stdout is None:
            _kill(p)
            p.wait()
            raise RuntimeError("Impossible de lire la sortie de ffmpeg")

        watchdog = None
        if timeout:
            def _on_timeout():
                timed_out.set()
                _kill(p)

            watchdog = threading.Timer(timeout, _on_timeout)
            watchdog.daemon = True
            watchdog.start()
        try:
            for line in p.stdout:
                line = line.strip()
                if duration and line.startswith("out_time_ms="):
                    try:
                        ms = int(line.split("=", 1)[1])
                    except ValueError:
                        ms = None
                    if ms is not None:
                        frac_cb(min(ms / 1_000_000 / duration, 1.0))
                if line == "progress=end":
                    break
            rc = p.wait()
        except BaseException:
            # annulation ou erreur du lecteur : ne laisse pas ffmpeg orphelin
            _kill(p)
            p.wait()
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        errf.seek(0)
        err = errf.read()

    if timed_out.is_set():
        raise RuntimeError(f"ffmpeg a expiré après {timeout:.0f} s")
    if rc != 0:
        tail = err.strip().splitlines()[-1] if err.strip() else f"code {rc}"
        raise RuntimeError(f"ffmpeg a échoué : {tail}")
    frac_cb(1.0)



def to_mono48(src: Path, dst: Path, duration: float, frac_cb: Callable[[float], None]):
    run_ffmpeg(
        ["-i", src, "-vn", "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", dst],
        duration, frac_cb, timeout=FFMPEG_TIMEOUT,
    )


def remux_video(src: Path, wav_enhanced: Path, out_path: Path,
                duration: float, frac_cb: Callable[[float], None]):
    run_ffmpeg(
        ["-i", src, "-i", wav_enhanced,
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_path],
        duration, frac_cb, timeout=FFMPEG_TIMEOUT,
    )


def encode_mp3(src: Path, dst: Path, duration: float, frac_cb: Callable[[float], None]):
    run_ffmpeg(
        ["-i", src, "-c:a", "libmp3lame", "-b:a", "192k", dst],
        duration, frac_cb, timeout=FFMPEG_TIMEOUT,
    )


def process_file(src: Path, outdir: Path, denoise: bool, input_sr: int,
                 cutoff: int | None, on_stage: StageCb,
                 output_format: str = "wav", transcribe: bool = False,
                 cancel: threading.Event | None = None,
                 keep_original: bool = True) -> dict:
    """Traite un fichier. output_format : "wav" (défaut) ou "mp3" — dans ce
    dernier cas, une copie MP3 (192 kbit/s) de l'audio amélioré est aussi produite.
    transcribe : produit en plus `<stem>_transcript.txt` (Parakeet TDT).
    keep_original : conserve le WAV 48 kHz d'origine (comparaison A/B). Sinon il
    est supprimé après amélioration — c'est 43 % du disque d'un traitement.
    cancel : Event posé par le client ; chaque émission de progression est un
    point de contrôle d'annulation."""
    user_cb = on_stage

    def on_stage(stage: str, prog: float) -> None:
        raise_if_cancelled(cancel)
        user_cb(stage, prog)

    outdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    if not info["audio"]:
        raise RuntimeError("Aucune piste audio détectée dans le fichier.")
    if info["duration"] <= 0:
        raise RuntimeError("Impossible de déterminer la durée du fichier.")
    if info["duration"] > MAX_DURATION:
        # Rejet immédiat : inutile de saturer le GPU pendant des heures.
        raise RuntimeError(
            f"Fichier trop long : {info['duration'] / 60:.0f} min "
            f"(maximum {MAX_DURATION / 60:.0f} min, PYCLEAN_MAX_DURATION)."
        )
    is_video = info["video"]
    dur = info["duration"]
    stem = src.stem
    wav_orig = outdir / f"{stem}_original.wav"
    wav_enh = outdir / f"{stem}_enhanced.wav"
    make_mp3 = output_format == "mp3"

    on_stage("Extraction de la piste audio…" if is_video else "Décodage de l'audio…", 0.02)
    to_mono48(src, wav_orig, dur,
              lambda f: on_stage("Extraction de la piste audio…" if is_video
                                 else "Décodage de l'audio…", 0.02 + 0.08 * f))

    on_stage("Amélioration de l'audio (LavaSR v2)…", 0.12)
    from .enhancer import get_enhancer
    with GPU_LOCK:  # un seul modèle en VRAM à la fois (voir app/cancel.py)
        get_enhancer().enhance_wav(
            wav_orig, wav_enh, denoise=denoise, input_sr=input_sr, cutoff=cutoff,
            on_progress=lambda f: on_stage("Amélioration de l'audio (LavaSR v2)…",
                                           0.12 + 0.6 * f),
        )
        if not keep_original:
            # L'original n'a servi qu'au modèle : on libère le disque tout de suite.
            wav_orig.unlink(missing_ok=True)

    result = {
        "kind": "video" if is_video else "audio",
        "original_wav": str(wav_orig) if keep_original else None,
        "enhanced_wav": str(wav_enh),
        "enhanced_mp3": None,
        "transcript": None,
        "transcript_srt": None,
        "duration": dur,
        "output": None,
    }

    # Les étapes post-amélioration se partagent le reste de la progression
    # (0.74 → 0.99), quelle que soit la combinaison choisie.
    n_post = max(sum([make_mp3, is_video, transcribe]), 1)
    base = 0.74
    span = (0.99 - 0.74) / n_post

    if make_mp3:
        on_stage("Encodage MP3…", base)
        out_mp3 = outdir / f"{stem}_pyclean-audio.mp3"
        encode_mp3(wav_enh, out_mp3, dur, lambda f: on_stage("Encodage MP3…", base + span * f))
        result["enhanced_mp3"] = str(out_mp3)
        base += span

    if is_video:
        on_stage("Recodage de la vidéo…", base)
        out_mp4 = outdir / f"{stem}_pyclean-audio.mp4"
        remux_video(src, wav_enh, out_mp4, dur, lambda f: on_stage("Recodage de la vidéo…", base + span * f))
        result["output"] = str(out_mp4)
        base += span

    if transcribe:
        on_stage("Transcription (Parakeet)…", base)
        from .transcriber import get_transcriber, render_srt
        with GPU_LOCK:  # Parakeet reste sur la carte le temps de la transcription
            tr = get_transcriber().transcribe(
                wav_enh,
                on_progress=lambda f: on_stage("Transcription (Parakeet)…", base + span * f),
                cancel=cancel,
            )
        txt = outdir / f"{stem}_transcript.txt"
        txt.write_text((tr.text or "(aucune parole détectée)") + "\n", encoding="utf-8")
        result["transcript"] = str(txt)
        if tr.cues:
            srt = outdir / f"{stem}_pyclean-audio.srt"
            srt.write_text(render_srt(tr.cues), encoding="utf-8")
            result["transcript_srt"] = str(srt)
        base += span

    on_stage("Terminé", 1.0)
    return result
