"""Windowed 16 kHz reading — equivalence with global resampling.

This is the non-regression test of the memory refactoring: loading the whole
file in one go gives the reference signal, windowed reading must return exactly
the same result.
"""

import numpy as np
import pytest
import soundfile as sf
import torch
import torchaudio

from app.enhancer import (
    CHUNK_SEC,
    OVERLAP_SEC,
    SAMPLES_PER_SEC,
    Stream16k,
    _resample_stride,
    plan_ranges,
)

CHUNK = CHUNK_SEC * SAMPLES_PER_SEC
OV = OVERLAP_SEC * SAMPLES_PER_SEC
STEP = CHUNK - OV


def global_16k(path, input_sr=16000):
    """Reference: the old method, the whole file in one go."""
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    t = torch.from_numpy(np.ascontiguousarray(x.mean(axis=1))).unsqueeze(0)
    if sr != input_sr:
        t = torchaudio.functional.resample(t, sr, input_sr)
    if input_sr != SAMPLES_PER_SEC:
        t = torchaudio.functional.resample(t, input_sr, SAMPLES_PER_SEC)
    return t[0]


def write_wav(path, seconds, rate=48000, seed=0):
    rng = np.random.default_rng(seed)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    # mix of tones: sensitive to any phase shift
    x = (0.4 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 1310 * t)
         + 0.05 * rng.standard_normal(n)).astype(np.float32)
    sf.write(str(path), x, rate, subtype="PCM_16")
    return path


def test_stride_de_rechantillonnage():
    assert _resample_stride(48000, 16000) == 3
    assert _resample_stride(16000, 16000) == 1
    assert _resample_stride(48000, 8000) == 6
    assert _resample_stride(48000, 24000) == 2
    assert _resample_stride(24000, 16000) == 3


@pytest.mark.parametrize("seconds", [1.0, 2.5, 3.7])
@pytest.mark.parametrize("input_sr", [16000, 8000, 24000])
def test_longueur_totale_exacte(tmp_path, seconds, input_sr):
    """The reported length must be the one of the global resampling."""
    p = write_wav(tmp_path / f"a{seconds}_{input_sr}.wav", seconds)
    with Stream16k(p, input_sr) as rd:
        assert rd.n16 == global_16k(p, input_sr).numel()


def test_lecture_fenetree_identique_au_global(tmp_path):
    """The heart of the refactoring: same signal, window by window."""
    p = write_wav(tmp_path / "b.wav", 3.0, seed=1)
    ref = global_16k(p)
    with Stream16k(p) as rd:
        assert rd.n16 == ref.numel()
        # a single window covering everything: no join, must be exact
        assert torch.equal(rd.read(0, rd.n16), ref)


def test_lecture_fenetree_egale_au_global_aux_jonctions(tmp_path):
    """4 min 7 s signal: the windows are aligned like the real chunking in
    blocs de 60 s (recouvrements de 2 s), donc traversant 3 blocs."""
    p = write_wav(tmp_path / "c.wav", 4 * 60 + 7.0, seed=2)
    ref = global_16k(p)
    with Stream16k(p) as rd:
        assert rd.n16 == ref.numel()
        ranges = plan_ranges(rd.n16, CHUNK, STEP)
        blocks = [rd.read(s, e) for s, e in ranges]
    for (s, e), block in zip(ranges, blocks, strict=True):
        assert block.numel() == e - s
        assert torch.equal(block, ref[s:e]), f"block [{s},{e}) differs from the global read"
    # the overlap zones seen by two consecutive blocks agree:
    # that is what guarantees a clean crossfade (no shift between blocks)
    for (s0, e0), (s1, _) in zip(ranges, ranges[1:], strict=False):
        assert e0 - s1 == OV
        assert torch.equal(blocks[ranges.index((s0, e0))][e0 - s0 - OV:],
                           blocks[ranges.index((s0, e0)) + 1][:OV])


def test_lecture_aux_bords_de_fenetre(tmp_path):
    """Reads that start/end anywhere stay exact."""
    p = write_wav(tmp_path / "d.wav", 2.0, seed=3)
    ref = global_16k(p)
    with Stream16k(p) as rd:
        for start, stop in [(0, 1), (0, 1000), (999, 1001), (1000, 2000),
                            (rd.n16 - 10, rd.n16), (rd.n16, rd.n16), (5, 5),
                            (12345, 12345 + 4096), (0, rd.n16)]:
            out = rd.read(start, stop)
            assert out.numel() == stop - start
            assert torch.equal(out, ref[start:stop]), (start, stop)


@pytest.mark.parametrize("input_sr", [8000, 24000])
def test_entrees_non_defaut(tmp_path, input_sr):
    p = write_wav(tmp_path / f"e{input_sr}.wav", 2.0, seed=4)
    ref = global_16k(p, input_sr)
    with Stream16k(p, input_sr) as rd:
        assert rd.n16 == ref.numel()
        for start, stop in [(0, 5000), (5000, 15000), (15000, rd.n16)]:
            # the result is bit-for-bit identical, including for input_sr != 16 kHz
            assert torch.equal(rd.read(start, stop), ref[start:stop])


def test_source_multicanale(tmp_path):
    """The pipeline produces mono, but reading must average the channels."""
    p = tmp_path / "st.wav"
    n = 48000
    t = np.arange(n) / 48000
    left = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    right = (0.2 * np.sin(2 * np.pi * 440 * t + 0.3)).astype(np.float32)
    sf.write(str(p), np.stack([left, right], axis=1), 48000, subtype="PCM_16")
    with Stream16k(p) as rd:
        assert rd._f.channels == 2
        assert rd.n16 == global_16k(p).numel()
        assert torch.equal(rd.read(0, 1000), global_16k(p)[:1000])


def test_taux_non_48k(tmp_path):
    """A 44.1 kHz WAV stays manageable (exact length)."""
    p = tmp_path / "cd.wav"
    n = 44100
    t = np.arange(n) / 44100
    sf.write(str(p), (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32),
             44100, subtype="PCM_16")
    with Stream16k(p) as rd:
        assert rd.src_sr == 44100
        assert rd.n16 == global_16k(p).numel()
        assert rd.read(0, rd.n16).numel() == rd.n16


def test_fichier_vide(tmp_path):
    p = tmp_path / "vide.wav"
    sf.write(str(p), np.zeros(0, dtype=np.float32), 48000, subtype="PCM_16")
    with Stream16k(p) as rd:
        assert rd.n16 == 0
        assert rd.read(0, 0).numel() == 0
        assert plan_ranges(rd.n16, CHUNK, STEP) == []


def test_fermeture_du_fichier(tmp_path):
    p = write_wav(tmp_path / "f.wav", 0.5)
    rd = Stream16k(p)
    rd.read(0, 100)
    rd.close()
    assert rd._f.closed
