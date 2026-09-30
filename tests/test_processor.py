"""ffmpeg pipeline: probe, progress, error handling, produced artifacts.

No torch is loaded: the enhancer and the transcriber are replaced by stubs
(`fake_models` fixture).
"""

import json
import os
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from app.messages import STAGES
from app.processor import (
    ALLOWED_EXT,
    _kill,
    iter_media_files,
    probe,
    process_file,
    run_ffmpeg,
)

from .conftest import fake_ffmpeg, requires_ffmpeg


def noop(stage, prog, key=None, args=None):
    """A process_file progress callback, with no side effect.

    The last two parameters are optional: that is the shape `process_file`
    emits (text for the CLI, key + params for the UI).
    """


def collect(log):
    """Callback that records (text, progress, key)."""
    def cb(stage, prog, key=None, args=None):
        log.append((stage, prog, key))
    return cb


def make_wav(path, seconds=1.0, rate=48000, freq=440):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={seconds}",
         "-ac", "1", "-ar", str(rate), str(path)],
        check=True, capture_output=True,
    )
    return path


def make_mp4(path, seconds=1.0):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x120:rate=10",
         "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def streams_of(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(r.stdout)["streams"]


# ---------------------------------------------------------------- run_ffmpeg

def test_run_ffmpeg_remonte_la_progression(tmp_path, monkeypatch):
    fake_ffmpeg(tmp_path, monkeypatch,
                'echo out_time_ms=5000000\necho progress=continue\n'
                'echo out_time_ms=10000000\necho progress=end\n')
    fracs = []
    run_ffmpeg(["-i", "in", str(tmp_path / "x.bin")], 10.0, fracs.append)
    assert fracs == [0.5, 1.0, 1.0]


def test_run_ffmpeg_progression_bornee_a_un(tmp_path, monkeypatch):
    fake_ffmpeg(tmp_path, monkeypatch, 'echo out_time_ms=99999999\necho progress=end\n')
    fracs = []
    run_ffmpeg(["-i", "in", str(tmp_path / "x")], 10.0, fracs.append)
    assert max(fracs) <= 1.0


def test_run_ffmpeg_progression_sans_duree(tmp_path, monkeypatch):
    """Without a known duration the step still completes and ends at 1.0."""
    fake_ffmpeg(tmp_path, monkeypatch, 'echo out_time_ms=5000000\necho progress=end\n')
    fracs = []
    run_ffmpeg(["-i", "in", str(tmp_path / "x")], None, fracs.append)
    assert fracs == [1.0]


def test_run_ffmpeg_passe_nostdin(tmp_path, monkeypatch):
    """-nostdin: ffmpeg must never be able to steal the server's terminal/pipe."""
    spy = tmp_path / "args.txt"
    fake_ffmpeg(tmp_path, monkeypatch, f'echo "$@" > {spy}\necho progress=end\n')
    run_ffmpeg(["-i", "in", str(tmp_path / "x")], 1.0, lambda f: None)
    args = spy.read_text().split()
    assert "-nostdin" in args
    assert "-y" in args


def test_run_ffmpeg_echec_remonte_la_queue_stderr(tmp_path, monkeypatch):
    fake_ffmpeg(tmp_path, monkeypatch, 'echo "Conversion invalide" >&2\nexit 1\n')
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_ffmpeg(["-i", "in", str(tmp_path / "x")], 10.0, lambda f: None)


@pytest.mark.timeout(30)
def test_run_ffmpeg_ne_se_bloque_pas_sur_un_stderr_vaste(tmp_path, monkeypatch):
    """Regression: with an undrained stderr=PIPE, ffmpeg blocks after 64 KB and
    the job hangs forever."""
    fake_ffmpeg(tmp_path, monkeypatch,
                'i=0; while [ $i -lt 4000 ]; do '
                'echo "diagnostic line number $i -------------------" >&2; '
                'i=$((i+1)); done; exit 1\n')
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        run_ffmpeg(["-i", "in", str(tmp_path / "x")], 10.0, lambda f: None)


@pytest.mark.timeout(30)
def test_run_ffmpeg_tue_un_processus_bloque(tmp_path, monkeypatch):
    """The watchdog must kill an ffmpeg that no longer emits progress."""
    fake_ffmpeg(tmp_path, monkeypatch, "sleep 60\n")
    with pytest.raises(RuntimeError, match="timed out"):
        run_ffmpeg(["-i", "in", str(tmp_path / "x")], 10.0, lambda f: None,
                   timeout=2)


@pytest.mark.timeout(30)
def test_run_ffmpeg_annulation_tue_le_processus(tmp_path, monkeypatch):
    """If the progress callback raises, ffmpeg must not be left orphaned."""
    spy = tmp_path / "alive.txt"
    fake_ffmpeg(tmp_path, monkeypatch,
                'echo start\necho out_time_ms=1000000\n'
                f'trap "echo ok > {spy}; exit 0" TERM\nsleep 60\n')

    def boom(frac):
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        run_ffmpeg(["-i", "in", str(tmp_path / "x")], 10.0, boom)
    # the process was killed: the trap file could not be written
    assert not spy.exists()


# -------------------------------------------------------------------- _kill

class FakePopen:
    """Records what _kill() does to an ffmpeg process."""

    def __init__(self, pid=4242, raises=None):
        self.pid = pid
        self.killed = 0
        self.raises = raises

    def kill(self):
        self.killed += 1
        if self.raises:
            raise self.raises


def test_kill_sur_windows_appelle_p_kill(monkeypatch):
    """os.killpg / os.getpgid / signal.SIGKILL do not exist on Windows: without
    the branch, a cancellation or an ffmpeg timeout raises AttributeError
    instead of reaching "cancelled"/"error"."""
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.delattr(os, "killpg", raising=False)
    monkeypatch.delattr(os, "getpgid", raising=False)
    p = FakePopen()
    _kill(p)  # must not raise
    assert p.killed == 1


def test_kill_sur_windows_avale_une_erreur_oserror(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    p = FakePopen(raises=OSError("already dead"))
    _kill(p)  # must not raise
    assert p.killed == 1


def test_kill_sur_posix_tue_le_groupe_de_processus(monkeypatch):
    """The POSIX behaviour is unchanged: ffmpeg's children share the group."""
    monkeypatch.setattr(os, "name", "posix")
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    p = FakePopen(pid=77)
    _kill(p)
    assert calls == [(77, signal.SIGKILL)]
    assert p.killed == 0


def test_kill_sur_posix_retombe_sur_p_kill(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def _gone(_pid):
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(os, "getpgid", _gone)
    p = FakePopen()
    _kill(p)
    assert p.killed == 1


# -------------------------------------------------------------------- probe

@requires_ffmpeg
def test_probe_wav(tmp_path):
    info = probe(make_wav(tmp_path / "a.wav", seconds=1.0))
    assert info["audio"] is True
    assert info["video"] is False
    assert info["duration"] == pytest.approx(1.0, abs=0.05)


@requires_ffmpeg
def test_probe_mp4(tmp_path):
    info = probe(make_mp4(tmp_path / "a.mp4", seconds=1.0))
    assert info["audio"] is True
    assert info["video"] is True
    assert info["duration"] == pytest.approx(1.0, abs=0.1)


def test_probe_fichier_invalide(tmp_path):
    bad = tmp_path / "not-a-media.txt"
    bad.write_text("bonjour")
    with pytest.raises(RuntimeError, match="ffprobe failed"):
        probe(bad)


# ------------------------------------------------------------- process_file

@requires_ffmpeg
def test_process_file_wav(tmp_path, fake_models):
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    log = []
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       collect(log))

    assert res["kind"] == "audio"
    assert res["output"] is None
    assert res["enhanced_mp3"] is None
    for key in ("original_wav", "enhanced_wav"):
        assert Path(res[key]).exists()
    assert res["duration"] == pytest.approx(1.0, abs=0.05)

    progs = [p for _, p, _ in log]
    assert progs == sorted(progs), "progress must be monotonic"
    assert progs[0] >= 0.0
    assert progs[-1] == 1.0


@requires_ffmpeg
def test_process_file_emets_des_cles_d_etape(tmp_path, fake_models):
    """The UI translates the emitted keys: no stage may be a hardcoded text,
    otherwise a progress line would stay in a single language."""
    log = []
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    process_file(src, tmp_path / "out", False, 16000, None, collect(log),
                 output_format="mp3")
    keys = [k for _, _, k in log]
    assert keys[0] == "decode"
    assert keys[-1] == "done"
    assert {"enhance", "mp3"} <= set(keys)
    assert all(k in STAGES for k in keys), keys


@requires_ffmpeg
def test_process_file_video_etape_extraction_piste_audio(tmp_path, fake_models):
    src = make_mp4(tmp_path / "a.mp4", seconds=1.0)
    log = []
    process_file(src, tmp_path / "out", False, 16000, None, collect(log),
                 output_format="mp3")
    keys = [k for _, _, k in log]
    assert keys[0] == "extract_audio"
    assert "remux" in keys


@requires_ffmpeg
def test_process_file_mp3_produit_les_deux_formats(tmp_path, fake_models):
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       noop, output_format="mp3")
    assert Path(res["enhanced_mp3"]).exists()
    assert Path(res["enhanced_wav"]).exists()
    assert res["enhanced_mp3"].endswith("_pyclean-audio.mp3")


@requires_ffmpeg
def test_process_file_transcription(tmp_path, fake_models):
    _, tra = fake_models
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       noop, transcribe=True)
    txt = Path(res["transcript"])
    assert txt.exists()
    assert txt.read_text(encoding="utf-8").strip() == "bonjour le monde"
    assert len(tra.calls) == 1


@requires_ffmpeg
def test_process_file_transcription_vide(tmp_path, fake_models):
    _, tra = fake_models
    tra.text = ""
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       noop, transcribe=True)
    assert "aucune parole" in Path(res["transcript"]).read_text(encoding="utf-8")


@requires_ffmpeg
def test_process_file_video_copies_the_video_stream(tmp_path, fake_models):
    src = make_mp4(tmp_path / "a.mp4", seconds=1.0)
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       noop, output_format="mp3")
    assert res["kind"] == "video"
    assert res["output"].endswith(".mp4")
    assert Path(res["output"]).exists()

    src_v = next(s for s in streams_of(src) if s["codec_type"] == "video")
    out_v = next(s for s in streams_of(res["output"]) if s["codec_type"] == "video")
    # -c:v copy: no re-encoding, hence identical codec and dimensions
    assert out_v["codec_name"] == src_v["codec_name"]
    assert out_v["width"] == src_v["width"]
    out_a = next(s for s in streams_of(res["output"]) if s["codec_type"] == "audio")
    assert out_a["codec_name"] == "aac"
    assert int(out_a["sample_rate"]) == 48000


@requires_ffmpeg
def test_process_file_sans_piste_audio(tmp_path, fake_models):
    src = tmp_path / "muet.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=duration=1:size=160x120:rate=10", "-c:v", "libx264", str(src)],
        check=True, capture_output=True,
    )
    with pytest.raises(RuntimeError, match="No audio track"):
        process_file(src, tmp_path / "out", False, 16000, None, noop)


@requires_ffmpeg
def test_process_file_refuse_un_fichier_trop_long(tmp_path, fake_models, monkeypatch):
    """Rejected immediately (before any inference) when the duration is over the
    limit."""
    from app import processor

    monkeypatch.setattr(processor, "MAX_DURATION", 2)
    src = make_wav(tmp_path / "long.wav", seconds=3.0)
    with pytest.raises(RuntimeError, match="too long"):
        process_file(src, tmp_path / "out", False, 16000, None, noop)
    assert fake_models[0].calls == [], "the model must not be called"


@requires_ffmpeg
def test_process_file_keep_original_supprime_le_wav(tmp_path, fake_models):
    """Without the original being asked for, the 48 kHz WAV does not stay on disk."""
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    res = process_file(src, tmp_path / "out", False, 16000, None,
                       noop, keep_original=False)
    assert res["original_wav"] is None
    assert not list((tmp_path / "out").glob("*_original.wav"))
    assert Path(res["enhanced_wav"]).exists()


@requires_ffmpeg
def test_process_file_annulation_leve_jobcancelled(tmp_path, fake_models):
    from app.cancel import JobCancelled

    event = threading.Event()
    event.set()
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    with pytest.raises(JobCancelled):
        process_file(src, tmp_path / "out", False, 16000, None,
                     noop, cancel=event)


@requires_ffmpeg
def test_process_file_reporte_les_options_au_modele(tmp_path, fake_models):
    enh, _ = fake_models
    src = make_wav(tmp_path / "a.wav", seconds=1.0)
    process_file(src, tmp_path / "out", True, 8000, 3000, noop)
    call = enh.calls[0]
    assert call["denoise"] is True
    assert call["input_sr"] == 8000
    assert call["cutoff"] == 3000


# -------------------------------------------------------- folder enumeration

def test_iter_media_files_recursif_et_filtre(tmp_path):
    (tmp_path / "sous").mkdir()
    (tmp_path / "a.wav").write_bytes(b"")
    (tmp_path / "sous" / "b.MP4").write_bytes(b"")
    (tmp_path / "sous" / "note.txt").write_bytes(b"")
    (tmp_path / "sous" / "c.zzz").write_bytes(b"")

    found = [p.name for p in iter_media_files(tmp_path)]
    assert found == ["a.wav", "b.MP4"]  # sorted, recursive, extensions filtered


def test_allowed_ext_couvre_les_formats_du_readme():
    for ext in (".mp3", ".wav", ".flac", ".m4a", ".mp4", ".mkv", ".webm", ".mov"):
        assert ext in ALLOWED_EXT
    assert ".exe" not in ALLOWED_EXT
    assert ".sh" not in ALLOWED_EXT
