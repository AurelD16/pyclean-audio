"""Découpage en tranches, sous-titres et découverte du checkpoint — sans NeMo."""

import importlib.util
import math

import pytest

from app.transcriber import (
    CHUNK_SEC,
    NEMO_FILENAME,
    OVERLAP_SEC,
    Cue,
    ParakeetTranscriber,
    Transcript,
    _fmt_ts,
    _has_timestamps,
    _wrap,
    is_available,
    local_nemo_path,
    plan_chunks,
    render_srt,
    to_cues,
)


def test_plan_chunks_couvre_tout_sans_trou():
    sr = 48000
    frames = int(CHUNK_SEC * sr) * 3 + 1234
    ranges = plan_chunks(frames, sr)

    assert ranges[0][0] == 0
    assert ranges[-1][1] == frames
    for (_, e0, _, _), (s1, _, _, _) in zip(ranges, ranges[1:], strict=False):
        assert s1 <= e0, "les blocs lus doivent se chevaucher"
    # les fenêtres gardées, elles, se touchent : ni trou, ni doublon de mots
    for (_, _, _, k1), (_, _, k0p, _) in zip(ranges, ranges[1:], strict=False):
        assert k1 == k0p


def test_plan_chunks_ventilation_sans_doublon():
    """Chaque échantillon du fichier tombe dans une et une seule fenêtre gardée."""
    sr = 16000
    frames = int((CHUNK_SEC * 5.7) * sr)
    windows = [(k0, k1) for _, _, k0, k1 in plan_chunks(frames, sr)]

    assert windows[0][0] == 0
    assert windows[-1][1] == frames
    for (_, k1), (k0p, _) in zip(windows, windows[1:], strict=False):
        assert k1 == k0p
    assert sum(k1 - k0 for k0, k1 in windows) == frames


def test_plan_chunks_garde_une_fenetre_dans_le_bloc_lu():
    """La fenêtre gardée doit tenir dans le bloc lu (chevauchement compris)."""
    sr = 44100
    for frames in (int(s * sr) for s in (5, 63, 130, 600)):
        for start, stop, k0, k1 in plan_chunks(frames, sr):
            assert start <= k0 < k1 <= stop


def test_plan_chunks_dernier_bloc_tronque():
    sr = 16000
    chunk = int(CHUNK_SEC * sr)
    step = int((CHUNK_SEC - OVERLAP_SEC) * sr)
    ov = int(OVERLAP_SEC * sr)
    frames = 2 * step + 42
    ranges = plan_chunks(frames, sr)
    assert ranges == [
        (0, chunk, 0, step),
        (step - ov, step + chunk - ov, step, 2 * step),
        (2 * step - ov, frames, 2 * step, frames),
    ]


def test_plan_chunks_fichier_vide():
    assert plan_chunks(0, 48000) == [(0, 0, 0, 0)]


def test_plan_chunks_exactement_une_tranche():
    """Un fichier d'une seule tranche garde tout : pas de contexte à jeter."""
    sr = 8000
    frames = CHUNK_SEC * sr
    assert plan_chunks(frames, sr) == [(0, frames, 0, frames)]


def test_plan_chunks_sans_chevauchement_reste_adiacente():
    sr = 16000
    chunk = int(CHUNK_SEC * sr)
    frames = 3 * chunk + 10
    assert plan_chunks(frames, sr, overlap_sec=0) == [
        (0, chunk, 0, chunk), (chunk, 2 * chunk, chunk, 2 * chunk),
        (2 * chunk, 3 * chunk, 2 * chunk, 3 * chunk), (3 * chunk, frames, 3 * chunk, frames),
    ]


def test_plan_chunks_nombre_de_tranches():
    sr = 48000
    frames = int(CHUNK_SEC * sr) * 7
    step = (CHUNK_SEC - OVERLAP_SEC) * sr
    assert len(plan_chunks(frames, sr)) == math.ceil(frames / step)


def test_local_nemo_path_trouve_le_checkpoint(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    snap = hub / "models--nvidia--parakeet-tdt-0.6b-v3" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / NEMO_FILENAME).write_bytes(b"x")
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    found = local_nemo_path()
    assert found is not None
    assert found.endswith(NEMO_FILENAME)
    assert "abc123" in found


def test_local_nemo_path_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "vide"))
    assert local_nemo_path() is None


# ------------------------------------------------------------- is_available

def test_is_available_vrai_si_nemo_est_installe():
    # Le poste de développement a NeMo ; l'important est que la valeur soit un
    # booléen et non une exception si l'import est impossible plus tard.
    assert isinstance(is_available(), bool)


def test_is_available_faux_si_nemo_absent(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert is_available() is False


def test_is_available_renvoie_faux_si_la_recherche_echoue(monkeypatch):
    def boom(name):
        raise ValueError("pas de sys.path")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert is_available() is False


def test_status_expose_available():
    s = ParakeetTranscriber().status()
    assert s["status"] == "idle"
    assert isinstance(s["available"], bool)


# ------------------------------------------------------------------- câblage

class _FakeHypothesis:
    def __init__(self, text, timestamp):
        self.text = text
        self.timestamp = timestamp


class _FakeModel:
    """Un mot par seconde, étiqueté `b<bloc>w<temps relatif au bloc>`.

    Les horodatages de NeMo sont relatifs à l'audio passé à `transcribe` : c'est
    exactement ce que le câblage doit retranslater, donc le test le vérifie.
    """

    def __init__(self, samplerate, avec_timestamps=True):
        self.samplerate = samplerate
        self.avec_timestamps = avec_timestamps
        self.blocs = []

    def transcribe(self, arrays, **kwargs):
        arr = arrays[0]
        n = int(len(arr) / self.samplerate)
        self.blocs.append(n)
        mots = [f"b{len(self.blocs) - 1}w{i}" for i in range(n)]
        ts = None
        if self.avec_timestamps:
            ts = {"word": [[i, i + 0.5, m] for i, m in enumerate(mots)]}
        return [_FakeHypothesis(" ".join(mots), ts)]


def _mots_absolus(transcript, ranges, samplerate):
    """Remet chaque mot à sa place dans le fichier, d'après son étiquette."""
    out = []
    for c in transcript.cues:
        for mot in c.text.split():
            bloc, relatif = mot[1:].split("w")
            out.append(ranges[int(bloc)][0] / samplerate + int(relatif))
    return out


def _fake_transcriber(monkeypatch, model):
    tra = ParakeetTranscriber()
    monkeypatch.setattr(tra, "_ensure_model", lambda: model)
    return tra


def test_transcribe_chaque_mot_une_seule_fois(tmp_path, monkeypatch):
    """Le chevauchement ne doit ni perdre ni doubler un mot (2,5 tranches)."""
    import numpy as np
    import soundfile as sf

    sr = 16000
    dur = CHUNK_SEC * 2 + CHUNK_SEC // 2  # 150 s -> 4 blocs lus (le dernier est court)
    path = tmp_path / "a.wav"
    sf.write(path, np.zeros(dur * sr, dtype="float32"), sr, subtype="PCM_16")
    ranges = plan_chunks(dur * sr, sr)

    model = _FakeModel(sr)
    tr = _fake_transcriber(monkeypatch, model)

    t = tr.transcribe(path)

    # chaque seconde du fichier a rendu un mot, à sa place, une seule fois
    assert _mots_absolus(t, ranges, sr) == [float(i) for i in range(dur)]
    assert t.text.split() == [f"b{b}w{w}" for b, w in _blocs_mots(ranges)]
    assert t.cues[0].start == pytest.approx(0.0)
    assert t.cues[-1].end == pytest.approx(dur, abs=0.6)
    assert all(a.end <= b.start for a, b in zip(t.cues, t.cues[1:], strict=False))
    # le contexte amont est bien relu une fois par bloc
    assert sum(model.blocs) == pytest.approx(dur + len(ranges) * OVERLAP_SEC, abs=1.5)


def _blocs_mots(ranges, sr=16000):
    """(bloc, position dans le fichier) de chaque mot, dans l'ordre attendu."""
    for i, (start, _, k0, k1) in enumerate(ranges):
        for j in range(int((k0 - start) / sr), int((k1 - start) / sr)):
            yield i, j


def test_transcribe_hors_timestamps_garde_le_texte_brut(tmp_path, monkeypatch):
    import numpy as np
    import soundfile as sf

    sr = 16000
    dur = CHUNK_SEC + 30
    path = tmp_path / "b.wav"
    sf.write(path, np.zeros(dur * sr, dtype="float32"), sr, subtype="PCM_16")

    tr = _fake_transcriber(monkeypatch, _FakeModel(sr, avec_timestamps=False))

    t = tr.transcribe(path)

    # sans horodatages on ne peut pas dédoublonner : le texte de chaque bloc est
    # concaténé tel quel, chevauchement compris
    assert t.cues == []
    n_blocs = len(plan_chunks(dur * sr, sr))
    mots = t.text.split()
    assert mots[0] == "b0w0"
    assert mots[CHUNK_SEC] == "b1w0"  # le 2e bloc recommence à son propre début
    assert mots[-1].startswith(f"b{n_blocs - 1}w")


# ------------------------------------------------------------------ sous-titres

def tokens(*words):
    """Simule la sortie TDT : [[début, fin, mot], ...]."""
    out = []
    t = 0.0
    for w in words:
        out.append([t, t + 0.3, w])
        t += 0.4
    return out


def test_transcript_vide_est_falsy():
    assert not Transcript("", [])
    assert Transcript("du texte", [])


def test_fmt_ts():
    assert _fmt_ts(0) == "00:00:00,000"
    assert _fmt_ts(1.5) == "00:00:01,500"
    assert _fmt_ts(61.25) == "00:01:01,250"
    assert _fmt_ts(3661.001) == "01:01:01,001"
    assert _fmt_ts(-3) == "00:00:00,000"  # pas d'horodatage négatif


def test_wrap_sur_les_frontieres_de_mots():
    assert _wrap("un deux trois", 7) == ["un deux", "trois"]
    assert _wrap("mot", 42) == ["mot"]
    assert _wrap("", 42) == [""]


def test_to_cues_groupe_les_mots():
    cues = to_cues(tokens("bonjour", "le", "monde"), 0.0)
    assert len(cues) == 1
    assert cues[0].text == "bonjour le monde"
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(1.1)  # fin du dernier mot


def test_to_cues_decale_le_temps_de_la_tranche():
    cues = to_cues(tokens("oui"), 300.0)
    assert cues[0].start == pytest.approx(300.0)


def test_to_cues_jette_le_chevauchement():
    """La fenêtre [t0, t1) exclut le début de tranche, transcrit deux fois."""
    mots = [f"m{i}" for i in range(10)]  # un mot toutes les 0,4 s
    ts = tokens(*mots)
    t0 = ts[3][0]  # début du 4e mot
    t1 = ts[7][0]  # début du 8e mot

    gardes = to_cues(ts, 0.0, t0, t1)

    assert [c.text for c in gardes] == ["m3 m4 m5 m6"]


def test_to_cues_fenetre_vide_pas_de_sous_titre():
    ts = tokens("un", "deux", "trois")
    assert to_cues(ts, 0.0, 10.0, 20.0) == []


def test_to_cues_fenetre_decalee_les_horodatages():
    """offset (début de tranche) et t0/t1 (temps locaux) se cumulent."""
    ts = tokens("un", "deux", "trois", "quatre")
    gardes = to_cues(ts, 100.0, 0.0, ts[2][0])
    assert [c.text for c in gardes] == ["un deux"]
    assert gardes[0].start == pytest.approx(100.0)


def test_to_cues_fenetre_ignore_les_phrases_hors_fenetre():
    ts = {
        "word": tokens("alpha", "beta", "gamma"),
        "segment": [[0.0, 1.0, "Alpha."], [1.2, 3.0, "Beta gamma."]],
    }
    gardes = to_cues(ts, 0.0, 1.0, 3.0)
    assert [c.text for c in gardes] == ["Beta gamma."]


def test_has_timestamps():
    assert not _has_timestamps(None)
    assert not _has_timestamps({})
    assert not _has_timestamps({"word": None, "segment": None})
    assert _has_timestamps({"word": tokens("a")})
    assert _has_timestamps({"segment": [[0.0, 1.0, "a"]]})
    assert _has_timestamps(tokens("a"))
    assert not _has_timestamps([])


def test_to_cues_force_la_monotonie():
    """L'alignement TDT peut désordonner les fins de mots : on corrige."""
    cues = to_cues([[1.0, 2.0, "un"], [1.5, 1.2, "deux"], [3.0, 3.4, "trois"]], 0.0)
    assert all(c.end > c.start for c in cues)
    assert " ".join(c.text for c in cues) == "un deux trois"


def test_to_cues_coupe_sur_la_ponctuation():
    mots = ["une", "phrase", "courte.", "Puis", "une", "autre", "qui", "dure"]
    cues = to_cues(tokens(*mots), 0.0, max_chars=20)
    assert len(cues) >= 2
    assert cues[0].text.endswith("courte.")


def test_to_cues_borne_la_duree():
    longs = [f"mot{i}" for i in range(40)]
    cues = to_cues(tokens(*longs), 0.0, max_sec=3.0)
    assert len(cues) > 1
    for c in cues:
        assert c.end - c.start <= 3.0 + 0.4  # tolérance : le dernier mot déborde


def test_to_cues_ignore_les_entrees_abses():
    assert to_cues(None, 0.0) == []
    assert to_cues([], 0.0) == []
    assert to_cues([[0.0, 1.0, ""]], 0.0) == []      # mot vide
    assert to_cues([[0.0, 1.0]], 0.0) == []         # pas de texte


def test_render_srt_format():
    cues = [Cue(0.0, 2.5, "Bonjour le monde"), Cue(2.5, 5.0, "Deuxième phrase")]
    srt = render_srt(cues)
    # chaque bloc est suivi d'une ligne vide, y compris le dernier
    assert srt == (
        "1\n00:00:00,000 --> 00:00:02,500\nBonjour le monde\n\n"
        "2\n00:00:02,500 --> 00:00:05,000\nDeuxième phrase\n"
    )
    assert srt.count("\n\n") == 1


def test_render_srt_vide():
    assert render_srt([]) == ""


def test_srt_tient_en_deux_lignes():
    mots = [f"m{i}" for i in range(30)]
    cues = to_cues(tokens(*mots), 0.0)
    for bloc in render_srt(cues).split("\n\n"):
        lignes = [ln for ln in bloc.splitlines() if not ln[:1].isdigit() and "-->" not in ln]
        assert 1 <= len(lignes) <= 2, lignes
        assert all(len(ln) <= 42 for ln in lignes), lignes


# ------------------------------------- structure réellement renvoyée par NeMo

def nemo_ts(words, segments=None):
    """Reproduit hypothesis.timestamp de NeMo 3.x (dict 'word'/'segment')."""
    ts = {"word": [{"word": w, "start": s, "end": e, "start_offset": i,
                    "end_offset": i + 1}
                   for i, (s, e, w) in enumerate(words)],
          "segment": [{"segment": t, "start": s, "end": e, "start_offset": 0,
                       "end_offset": 0}
                      for s, e, t in (segments or [])]}
    return ts


def test_to_cues_accepte_le_dict_nemo():
    ts = nemo_ts([(0.0, 0.3, "Bonjour"), (0.4, 0.9, "monde")])
    cues = to_cues(ts)
    assert len(cues) == 1
    assert cues[0].text == "Bonjour monde"
    assert cues[0].start == pytest.approx(0.0)
    assert cues[0].end == pytest.approx(0.9)


def test_to_cues_prefere_les_phrases():
    ts = nemo_ts(
        words=[(0.0, 0.3, "Bonjour"), (0.4, 0.9, "monde"),
               (1.0, 1.4, "Encore"), (1.5, 1.9, "une fois")],
        segments=[(0.0, 0.9, "Bonjour monde."), (1.0, 1.9, "Encore une fois.")],
    )
    cues = to_cues(ts)
    assert [c.text for c in cues] == ["Bonjour monde.", "Encore une fois."]
    assert cues[1].start == pytest.approx(1.0)


def test_to_cues_recoupe_une_phrase_trop_longue():
    mots = [(0.1 * i, 0.1 * i + 0.08, f"m{i}") for i in range(30)]
    longue = " ".join(f"m{i}" for i in range(30)) + "."
    ts = nemo_ts(mots, [(0.0, 3.0, longue)])
    cues = to_cues(ts)
    assert len(cues) > 1
    assert " ".join(c.text for c in cues) == longue
    for c in cues:
        assert len(c.text) <= 2 * 42


def test_to_cues_phrase_sans_mots_utilise_le_texte():
    ts = nemo_ts([], [(0.0, 2.0, "Une phrase sans horodatage de mots.")])
    assert [c.text for c in to_cues(ts)] == ["Une phrase sans horodatage de mots."]


def test_to_cues_segments_invalides_ignores():
    ts = {"word": [], "segment": [{"segment": "x", "start": 2.0, "end": 1.0}]}
    assert to_cues(ts) == []
    assert to_cues({"word": "pas une liste", "segment": None}) == []
