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
from .messages import MediaError, stage_text

# on_stage(text, progress, key, params): the text is for the CLI and the UI's
# fallback, the key and its parameters for the translation (see
# app/messages.py). The last two arguments are optional: an older caller emits
# a text only.
StageCb = Callable[..., None]

ALLOWED_EXT = {
    ".wav", ".mp3", ".flac", ".ogg", ".oga", ".m4a", ".aac", ".opus",
    ".wma", ".aiff", ".aif", ".mp4", ".mkv", ".webm", ".mov", ".avi",
    ".m4v", ".ts", ".mpg", ".mpeg", ".wmv", ".flv",
}


def iter_media_files(root: Path):
    """Return, sorted, every supported audio/video file under root (recursive)."""
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
        raise MediaError("ffprobe_failed", detail=r.stderr.strip()[:300])
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
    """Kill ffmpeg *and* any child it spawned.

    Children inherit the stdout pipe: without killing the whole process group a
    grandchild survives, the pipe never closes and the stdout read stays blocked
    forever.
    """
    if os.name == "nt":
        # Windows has no process group to kill here: os.killpg, os.getpgid and
        # signal.SIGKILL do not exist (subprocess accepts start_new_session and
        # ignores it), and the except below would not catch the AttributeError
        # they would raise — a cancellation or an ffmpeg failure would blow up
        # instead of reaching "cancelled"/"error". ffmpeg spawns no child of its
        # own on Windows, so killing the process is enough.
        try:
            p.kill()
        except OSError:
            pass
        return
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def run_ffmpeg(args: list, duration: float | None, frac_cb: Callable[[float], None],
               timeout: float | None = None):
    """Run ffmpeg and relay its progress.

    - stderr goes to a temporary file (never to a pipe): an undrained pipe fills
      up at 64 KB, ffmpeg blocks on write and the job hangs for good.
    - `frac_cb` may raise (cancellation): ffmpeg is then killed before the error
      propagates.
    - `timeout` (seconds) arms a watchdog that kills a stuck ffmpeg, even one that
      stopped emitting progress lines entirely.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-y",
           "-loglevel", "error", "-progress", "pipe:1", *map(str, args)]

    timed_out = threading.Event()
    # The temporary file is read back once the process has finished (see below).
    with tempfile.TemporaryFile("w+", encoding="utf-8", errors="replace") as errf:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                             text=True, bufsize=1, start_new_session=True)
        if p.stdout is None:
            _kill(p)
            p.wait()
            raise MediaError("ffmpeg_no_output")

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
            # cancellation or reader error: never leave ffmpeg orphaned
            _kill(p)
            p.wait()
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        errf.seek(0)
        err = errf.read()

    if timed_out.is_set():
        raise MediaError("ffmpeg_timeout", seconds=int(round(timeout)))
    if rc != 0:
        tail = err.strip().splitlines()[-1] if err.strip() else f"code {rc}"
        raise MediaError("ffmpeg_failed", detail=tail)
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
    """Process one file. output_format: "wav" (default) or "mp3" — in the latter
    case an MP3 copy (192 kbit/s) of the enhanced audio is produced too.
    transcribe: also produces `<stem>_transcript.txt` (Parakeet TDT).
    keep_original: keeps the original 48 kHz WAV (A/B comparison). Otherwise it is
    deleted after enhancement — that is 43 % of a job's disk usage.
    cancel: event set by the client; every progress emission is a
    cancellation checkpoint."""
    user_cb = on_stage

    def emit(key: str, prog: float, **args) -> None:
        """Emit a stage: text (CLI, fallback) + key and params (translation)."""
        raise_if_cancelled(cancel)
        user_cb(stage_text(key, args), prog, key, args)

    outdir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    if not info["audio"]:
        raise MediaError("no_audio_track")
    if info["duration"] <= 0:
        raise MediaError("no_duration")
    if info["duration"] > MAX_DURATION:
        # Rejected immediately: no point saturating the GPU for hours.
        raise MediaError(
            "too_long",
            minutes=int(round(info["duration"] / 60)),
            max_minutes=int(round(MAX_DURATION / 60)),
        )
    is_video = info["video"]
    dur = info["duration"]
    stem = src.stem
    wav_orig = outdir / f"{stem}_original.wav"
    wav_enh = outdir / f"{stem}_enhanced.wav"
    make_mp3 = output_format == "mp3"

    decode = "extract_audio" if is_video else "decode"
    emit(decode, 0.02)
    to_mono48(src, wav_orig, dur, lambda f: emit(decode, 0.02 + 0.08 * f))

    emit("enhance", 0.12)
    from .enhancer import get_enhancer
    with GPU_LOCK:  # one model on the card at a time (see app/cancel.py)
        get_enhancer().enhance_wav(
            wav_orig, wav_enh, denoise=denoise, input_sr=input_sr, cutoff=cutoff,
            on_progress=lambda f: emit("enhance", 0.12 + 0.6 * f),
        )
        if not keep_original:
            # The original only fed the model: free the disk right away.
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

    # The post-enhancement steps share the rest of the progress
    # (0.74 → 0.99), whatever the chosen combination is.
    n_post = max(sum([make_mp3, is_video, transcribe]), 1)
    base = 0.74
    span = (0.99 - 0.74) / n_post

    if make_mp3:
        emit("mp3", base)
        out_mp3 = outdir / f"{stem}_pyclean-audio.mp3"
        encode_mp3(wav_enh, out_mp3, dur, lambda f: emit("mp3", base + span * f))
        result["enhanced_mp3"] = str(out_mp3)
        base += span

    if is_video:
        emit("remux", base)
        out_mp4 = outdir / f"{stem}_pyclean-audio.mp4"
        remux_video(src, wav_enh, out_mp4, dur, lambda f: emit("remux", base + span * f))
        result["output"] = str(out_mp4)
        base += span

    if transcribe:
        emit("transcribe", base)
        from .transcriber import get_transcriber, render_srt
        with GPU_LOCK:  # Parakeet reste sur la carte le temps de la transcription
            tr = get_transcriber().transcribe(
                wav_enh,
                on_progress=lambda f: emit("transcribe", base + span * f),
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

    emit("done", 1.0)
    return result
