"""Block planning and crossfade — without loading the model."""

import numpy as np
import pytest

from app.enhancer import CHUNK_SEC, OVERLAP_SEC, SAMPLES_PER_SEC, LavaEnhancer, plan_ranges

CHUNK = CHUNK_SEC * SAMPLES_PER_SEC
OV = OVERLAP_SEC * SAMPLES_PER_SEC
STEP = CHUNK - OV


def test_plan_ranges_couvre_tout_sans_trou():
    n = 3 * CHUNK + 12345
    ranges = plan_ranges(n, CHUNK, STEP)

    assert ranges[0][0] == 0
    assert ranges[-1][1] == n
    for (_, e0), (s1, _) in zip(ranges, ranges[1:], strict=False):
        assert s1 == e0 - OV, "le chevauchement doit valoir exactement OVERLAP_SEC"
        assert s1 < e0, "a block must be strictly larger than the overlap"


def test_plan_ranges_fichier_plus_court_qu_un_bloc():
    assert plan_ranges(1000, CHUNK, STEP) == [(0, 1000)]


def test_plan_ranges_exactement_un_bloc():
    assert plan_ranges(CHUNK, CHUNK, STEP) == [(0, CHUNK)]


def test_plan_ranges_vide():
    assert plan_ranges(0, CHUNK, STEP) == []


def test_plan_ranges_dernier_bloc_tronque():
    n = 3 * STEP + 7  # stop in the middle of the 3rd block
    ranges = plan_ranges(n, CHUNK, STEP)
    assert len(ranges) == 3
    assert ranges[-1] == (2 * STEP, n)


def test_crossfade_egalite_de_puissance():
    """w_prev² + w_out² == 1: a linear crossfade would break the level (-3 dB)."""
    w_prev, w_out = LavaEnhancer._crossfade_weights(4096)
    total = (w_prev.double() ** 2 + w_out.double() ** 2).numpy()
    assert np.allclose(total, 1.0, atol=1e-12)
    # ends: closed then open crossfade (cos(pi/2) is not exactly 0 in float32)
    assert w_prev[0].item() == pytest.approx(1.0, abs=1e-6)
    assert w_out[0].item() == pytest.approx(0.0, abs=1e-6)
    assert w_prev[-1].item() == pytest.approx(0.0, abs=1e-6)
    assert w_out[-1].item() == pytest.approx(1.0, abs=1e-6)


def test_croissance_monotone():
    """Each block starts further along than the previous one: we move forward only."""
    n = 5 * CHUNK
    ranges = plan_ranges(n, CHUNK, STEP)
    for (_, _), (s1, _) in zip(ranges, ranges[1:], strict=False):
        assert s1 > 0
    starts = [s for s, _ in ranges]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts)


def test_to_int16_borne_et_echelle():
    import torch

    x = torch.tensor([-2.0, -1.0, -1.0 / 32767.0, 0.0, 1.0 / 32767.0, 1.0, 2.0])
    out = LavaEnhancer._to_int16(x)
    assert out.dtype == np.int16
    assert out.shape == x.shape
    # clipping at ±1 and the 32767 scale
    assert out[0] == -32767
    assert out[-1] == 32767
    assert out[3] == 0
    assert abs(int(out[2])) <= 1
