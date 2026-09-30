"""Fixtures partagées : ffmpeg factice, modules de modèle factices.

Les tests n'importent jamais torch/LavaSR/NeMo : `app.processor` fait ses imports
de `enhancer`/`transcriber` *dans* le corps des fonctions, on peut donc injecter
des modules factices dans sys.modules.
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
    """Remplace LavaEnhancer : copie l'entrée 48 kHz (le pipeline suppose que
    enhance_wav produit du 48 kHz mono de même durée)."""

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
    """Renvoie un Transcript comme le vrai (texte + sous-titres horodatés)."""

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
    """Installe app.enhancer / app.transcriber factices (aucun torch chargé).

    Le module de transcription factique réexporte les helpers purs du vrai
    (render_srt, Transcript) : processor les importe au moment de l'appel.
    """
    import app.transcriber as real

    enh = FakeEnhancer()
    tra = FakeTranscriber()
    _install(monkeypatch, "app.enhancer", get_enhancer=lambda: enh)
    _install(monkeypatch, "app.transcriber", get_transcriber=lambda: tra,
             render_srt=real.render_srt, Transcript=real.Transcript, Cue=real.Cue)
    return enh, tra


def fake_ffmpeg(tmp_path, monkeypatch, script):
    """Met un exécutable `ffmpeg` factice (script sh) en tête de PATH."""
    import os

    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "ffmpeg"
    exe.write_text("#!/bin/sh\n" + script)
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return exe
