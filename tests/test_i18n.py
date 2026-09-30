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

DESKTOP_KEYS = (
    "ui.asr_missing_desktop",
    "ui.opt_transcribe_missing_desktop",
    "ui.transcribe_unavailable_desktop",
)
# The wording of the command-line version (`./run.sh --asr`): untouched, the
# page still runs that way.
CLI_KEYS = ("ui.asr_missing", "ui.opt_transcribe_missing")


@pytest.mark.parametrize("lang", LANGS)
def test_libelles_du_bureau_dans_les_deux_langues(page_dict, lang):
    """The packaged run shows these instead of the "./run.sh --asr" ones."""
    for key in DESKTOP_KEYS:
        assert page_dict["ui"][lang][key].strip(), key


@pytest.mark.parametrize("lang", LANGS)
def test_aucun_libelle_du_bureau_ne_renvoie_au_terminal(page_dict, lang):
    """Nobody who double-clicked an .exe can run ./run.sh: the wording must not
    tell them to."""
    for key in DESKTOP_KEYS:
        assert "run.sh" not in page_dict["ui"][lang][key], key


@pytest.mark.parametrize("lang", LANGS)
def test_les_libelles_de_la_ligne_de_commande_sont_intacts(page_dict, lang):
    """`desktop: false` (./run.sh, uvicorn): the page must keep saying how to
    enable transcription, otherwise the CLI user loses the instruction."""
    for key in CLI_KEYS:
        assert "run.sh" in page_dict["ui"][lang][key], key
    assert page_dict["server"][lang]["transcribe_unavailable"].count("run.sh") == 1


def test_la_page_bascule_sur_les_libelles_du_bureau(html):
    """`status.desktop` drives the choice: without the switch the new keys would
    be dead weight in the dictionary."""
    assert "s.desktop === true" in html
    for key in DESKTOP_KEYS:
        # once in the EN dictionary, once in the FR one, once in the script
        assert html.count(f'"{key}"') == 3, key


def test_le_message_400_est_eclaire_par_la_page_en_mode_bureau(html):
    """The checkbox is enabled until the first /api/status, so transcribe=true
    can still be sent: the 400 must not echo the server's "./run.sh" text."""
    assert 'code === "transcribe_unavailable"' in html
    assert "desktopMode &&" in html


def test_libelle_bureau_ne_masque_pas_une_cle_du_serveur(page_dict):
    """They are page keys: `ui` wins over `server`, they never shadow one."""
    for key in DESKTOP_KEYS:
        assert key not in page_dict["server"]["en"]


# --------------------------------------------------------------- bouton Quit

QUIT_KEYS = ("ui.quit", "ui.quit_confirm", "ui.quit_done", "ui.quit_failed")


@pytest.mark.parametrize("lang", LANGS)
def test_libelles_du_bouton_quit_dans_les_deux_langues(page_dict, lang):
    """EN and FR: this control is what makes browser mode stoppable."""
    for key in QUIT_KEYS:
        assert page_dict["ui"][lang][key].strip(), f"{key}/{lang}"


def test_quit_est_un_libelle_par_onglet(page_dict, html):
    """The button's own label lives in the page (data-i18n, EN + FR); the three
    dynamic labels live in the dictionary and are each used once."""
    assert page_dict["ui"]["fr"]["ui.quit"] != page_dict["ui"]["en"]["ui.quit"]
    assert html.count('"ui.quit"') == 3          # attribute + EN + FR
    assert 'data-i18n="ui.quit">Quit pyclean-audio</button>' in html
    for key in QUIT_KEYS[1:]:
        assert html.count(f'"{key}"') == 3, key  # EN + FR + the script


def test_le_bouton_quit_n_existe_qu_en_mode_bureau(html):
    """Hidden by default, revealed only by `status.desktop`: the command-line
    version must not offer to stop a server the launcher does not own."""
    assert 'class="quit-row hidden"' in html
    assert '$("quitRow").classList.toggle("hidden", !desktopMode)' in html


def test_quit_appelle_le_point_de_terminal_du_bureau(html):
    """POST /api/shutdown, and only that: the endpoint is desktop-only (404
    otherwise) and a GET never reaches the handler."""
    assert html.count('fetch("/api/shutdown"') == 1
    assert 'fetch("/api/shutdown", { method: "POST" })' in html


def test_le_badge_ne_disparait_pas_en_cas_erreur(html):
    """`.error { display: none }` (the error box) also matched `.badge.error`, so
    the model badge vanished exactly when it had something to say."""
    assert ".badge.error { display: inline-block;" in html


def test_la_confirmation_du_quit_cible_sur_un_job_en_cours(html):
    """`jobId` reste armé après un job terminé ("Process another file" le remet à
    zéro) : la confirmation ne doit partir que sur `pollTimer`, le signal que la
    page arme à l'envoi et `stopPolling()` enlève à la fin."""
    quit_app = html.split("async function quitApp()")[1].split("async ")[0]
    line = next(x for x in quit_app.splitlines() if "confirm(" in x)
    assert line.strip() == 'if (pollTimer !== null && !confirm(t("ui.quit_confirm"))) return;'
