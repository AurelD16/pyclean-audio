"""Shared fixtures: fake ffmpeg, fake model modules.

No **model** is loaded (no LavaSR weights, no NeMo); torch on the other hand is
imported by almost every test module (through app.main / app.enhancer /
app.transcriber), which costs ~1 s per file. `app.processor` imports
`enhancer`/`transcriber` *inside* its functions, so fakes can be injected into
sys.modules instead of loading the real ones.
"""

import shutil
import sys
import types

import pytest

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

requires_ffmpeg = pytest.mark.skipif(
    not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe absents du PATH"
)


class FakeEnhancer:
    """Stands in for LavaEnhancer: copies the 48 kHz input (the pipeline assumes
    enhance_wav produces 48 kHz mono of the same duration)."""

    def __init__(self):
        self.calls = []

    def enhance_wav(self, in_path, out_path, denoise=False, input_sr=16000,
                    cutoff=None, on_progress=None):
        import shutil as _sh

        self.calls.append({
            "in": str(in_path), "out": str(out_path), "denoise": denoise,
            "input_sr": input_sr, "cutoff": cutoff,
        })
        _sh.copyfile(in_path, out_path)
        if on_progress:
            on_progress(1.0)
        return str(out_path)


class FakeTranscriber:
    """Returns a Transcript like the real one (text + timestamped subtitles)."""

    def __init__(self, text="bonjour le monde", cues=None):
        self.text = text
        self.cues = cues or []
        self.calls = []
        self.cancel = None

    def transcribe(self, wav_path, on_progress=None, cancel=None):
        self.calls.append(str(wav_path))
        self.cancel = cancel
        if on_progress:
            on_progress(1.0)
        from app.transcriber import Transcript

        return Transcript(self.text, self.cues)


def _install(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture
def fake_models(monkeypatch):
    """Install fake app.enhancer / app.transcriber modules (no torch loaded).

    The fake transcription module re-exports the real module's pure helpers
    (render_srt, Transcript): processor imports them at call time.
    """
    import app.transcriber as real

    enh = FakeEnhancer()
    tra = FakeTranscriber()
    _install(monkeypatch, "app.enhancer", get_enhancer=lambda: enh)
    _install(monkeypatch, "app.transcriber", get_transcriber=lambda: tra,
             render_srt=real.render_srt, Transcript=real.Transcript, Cue=real.Cue)
    return enh, tra


def fake_ffmpeg(tmp_path, monkeypatch, script):
    """Puts a fake `ffmpeg` executable (a sh script) first in PATH."""
    import os

    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "ffmpeg"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return exe
