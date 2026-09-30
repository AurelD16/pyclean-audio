"""Multilingual UI: the page's dictionary and the server's one.

The page embeds its translations in an `<script type="application/json">` block
so they can be read here: these tests check that both sides speak the same keys
in both languages, and that the English text written in the page is up to date
(it is what shows before the script runs).
"""

import json
import re
from pathlib import Path

import pytest

from app import messages

PAGE = Path(__file__).resolve().parent.parent / "static" / "index.html"
LANGS = ["en", "fr"]
DICT_RE = re.compile(
    r'<script type="application/json" id="i18n">(.*?)</script>', re.S
)


@pytest.fixture(scope="module")
def html():
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_dict(html):
    m = DICT_RE.search(html)
    assert m, "the translations JSON block is missing from index.html"
    return json.loads(m.group(1))


# ------------------------------------------------------------------ structure

def test_la_page_est_en_anglais_par_defaut(html):
    assert '<html lang="en">' in html


def test_le_selecteur_de_langue_suit_le_badge(html):
    """Both flags sit under the LavaSR badge, in the order EN then FR."""
    badge = html.index('id="modelBadge"')
    flags = [html.index(f'data-lang="{lang}"') for lang in LANGS]
    assert badge < flags[0] < flags[1]
    assert html.index('class="langs"') > badge


@pytest.mark.parametrize("lang", LANGS)
def test_chaque_section_a_les_deux_langues(page_dict, lang):
    for section in ("server", "ui"):
        assert page_dict[section][lang], f"section {section} vide pour {lang}"


# ------------------------------------------------- server keys <-> page

def test_la_page_couvre_toutes_les_etapes_du_serveur(page_dict):
    attendu = set(messages.STAGES)
    for lang in LANGS:
        assert attendu <= set(page_dict["server"][lang]), lang


def test_la_page_couvre_toutes_les_erreurs_du_serveur(page_dict):
    attendu = set(messages.ERRORS)
    for lang in LANGS:
        assert attendu <= set(page_dict["server"][lang]), lang


def test_la_page_ne_declasse_pas_les_textes_du_serveur(page_dict):
    """A page key must not shadow a server key: it would then be
    translated in the wrong language (the server translates nothing)."""
    assert not set(page_dict["server"]["en"]) & set(page_dict["ui"]["en"])


def test_memes_espaces_de_noms_entre_les_deux_langues(page_dict):
    for section in ("server", "ui"):
        assert set(page_dict[section]["en"]) == set(page_dict[section]["fr"]), section


def test_le_texte_anglais_est_le_meme_des_deux_cotes(page_dict):
    """`app/messages.py` is authoritative (it is the server's fallback): the
    page's English must be word for word the same, or page and API diverge."""
    for key, text in messages.STAGES.items():
        assert page_dict["server"]["en"][key] == text, f"stage {key}"
    for code, text in messages.ERRORS.items():
        assert page_dict["server"]["en"][code] == text, f"error {code}"


@pytest.mark.parametrize("section", ["server", "ui"])
def test_memes_parametrage_dans_les_deux_langues(page_dict, section):
    for key in page_dict[section]["en"]:
        en = set(re.findall(r"\{(\w+)\}", page_dict[section]["en"][key]))
        fr = set(re.findall(r"\{(\w+)\}", page_dict[section]["fr"][key]))
        assert en == fr, f"{section}/{key}"


@pytest.mark.parametrize("section", ["server", "ui"])
@pytest.mark.parametrize("lang", LANGS)
def test_aucune_traduction_vide(page_dict, section, lang):
    for key, text in page_dict[section][lang].items():
        assert text.strip(), f"{section}/{lang}/{key} est vide"


def test_les_libelles_existent_en_francais(page_dict):
    """A page rendered in French must not leave an English label behind."""
    for key in ("ui.go", "ui.cancel", "ui.delete", "ui.again", "ui.state_done"):
        assert page_dict["ui"]["fr"][key] != page_dict["ui"]["en"][key], key


# ---------------------------------------------- le texte en dur de la page

def ui_section(key):
    """`server` et `ui` are two distinct sections of the dictionary; the
    interface keys are prefixed with `ui.`."""
    return "ui" if key.startswith("ui.") else "server"


def test_chaque_data_i18n_existe_dans_les_deux_langues(html, page_dict):
    cles = set(re.findall(r'data-i18n="([\w.]+)"', html))
    assert cles, "no translatable element: is the data-i18n attribute there?"
    for key in cles:
        for lang in LANGS:
            assert key in page_dict[ui_section(key)][lang], f"{key} / {lang}"


def test_le_texte_par_defaut_de_la_page_est_anglais(html, page_dict):
    """What the page shows before the script must be the dictionary's English:
    otherwise the first paint is not in the announced language."""
    for key, text in re.findall(r'data-i18n="([\w.]+)">([^<]*)</', html):
        assert text.strip() == page_dict["ui"]["en"][key].strip(), key


# ------------------------------------------------------------- desktop build

DESKTOP_KEYS = ("ui.asr_missing_desktop", "ui.opt_transcribe_missing_desktop")


@pytest.mark.parametrize("lang", LANGS)
def test_libelles_du_bureau_dans_les_deux_langues(page_dict, lang):
    """The packaged run shows these two instead of the "./run.sh --asr" ones."""
    for key in DESKTOP_KEYS:
        assert page_dict["ui"][lang][key].strip(), key


@pytest.mark.parametrize("lang", LANGS)
def test_aucun_libelle_du_bureau_ne_renvoie_au_terminal(page_dict, lang):
    """Nobody who double-clicked an .exe can run ./run.sh: the wording must not
    tell them to."""
    for key in DESKTOP_KEYS:
        assert "run.sh" not in page_dict["ui"][lang][key], key


def test_la_page_bascule_sur_les_libelles_du_bureau(html):
    """`status.desktop` drives the choice: without the switch the new keys would
    be dead weight in the dictionary."""
    assert "s.desktop === true" in html
    for key in DESKTOP_KEYS:
        # once in the EN dictionary, once in the FR one, once in the script
        assert html.count(f'"{key}"') == 3, key


def test_libelle_bureau_ne_masque_pas_une_cle_du_serveur(page_dict):
    """They are page keys: `ui` wins over `server`, they never shadow one."""
    for key in DESKTOP_KEYS:
        assert key not in page_dict["server"]["en"]
