"""Message keys and wording: what the server sends to the UI.

These tests lock the contract with `static/index.html`, whose dictionary is
checked from the page side by `tests/test_i18n.py`.
"""

import json
import re

import pytest

from app.messages import ERRORS, STAGES, MediaError, error_text, stage_text

# ------------------------------------------------------------------ catalogues

def test_les_deux_catalogues_sont_ouis():
    assert STAGES and ERRORS
    assert not set(STAGES) & set(ERRORS), "a key must only serve one purpose"


def _params(template):
    """A set of values for every `{placeholder}` of a text template."""
    return {nom: "X" for nom in re.findall(r"\{(\w+)\}", template)}


@pytest.mark.parametrize("key", sorted(STAGES))
def test_les_textes_d_etape_se_remplissent(key):
    """No template must remain unsubstituted: a stage always displays, even
    without its parameters (and never raises)."""
    assert stage_text(key), key
    assert "{" not in stage_text(key, _params(STAGES[key])), key


@pytest.mark.parametrize("code", sorted(ERRORS))
def test_les_textes_d_erreur_se_remplissent(code):
    assert error_text(code), code
    assert "{" not in error_text(code, _params(ERRORS[code])), code


def test_les_parametres_sont_inseres():
    assert stage_text("file_step", {"index": 2, "total": 9, "name": "a.mp3",
                                    "inner": "Done"}) == "File 2/9 — a.mp3: Done"
    assert error_text("too_long", {"minutes": 180, "max_minutes": 166}) == (
        "File too long: 180 min (max 166 min, PYCLEAN_MAX_DURATION)."
    )


def test_une_cle_inconnue_reste_lisible():
    """Showing the key is better than raising in the middle of a job."""
    assert stage_text("not_a_stage") == "not_a_stage"
    assert error_text("not_a_code") == "not_a_code"


def test_un_parametre_manquant_ne_supprime_pas_le_message():
    assert "{" in stage_text("file_step", {"index": 1})


# ------------------------------------------------------------------ MediaError

def test_media_error_garde_son_code_et_son_texte():
    err = MediaError("no_audio_track")
    assert err.code == "no_audio_track"
    assert str(err) == "No audio track found in the file."
    assert isinstance(err, RuntimeError)  # the historically expected type


def test_media_error_expose_des_parametres_serialisables():
    err = MediaError("unsupported_format", ext=".txt", taille=12.5, chemin=None)
    assert err.params == {"ext": ".txt", "taille": 12.5, "chemin": None}
    json.dumps(err.params)  # must stay JSON (job["error_params"])


def test_media_error_reduit_un_parametre_non_json():
    from pathlib import Path

    err = MediaError("bad_filename", name=Path("/tmp/x.mp3"))
    assert err.params["name"] == str(Path("/tmp/x.mp3"))
    json.dumps(err.params)


def test_media_error_accepte_un_texte_long():
    err = MediaError("ffmpeg_failed", detail="x" * 500)
    assert len(err.params["detail"]) == 500
