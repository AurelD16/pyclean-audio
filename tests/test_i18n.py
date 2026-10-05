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


# --------------------------------- l'étape courante (régression i18n)

# Le libellé affiché au-dessus de la barre de progression avait disparu après la
# refonte i18n : `jobStage` rendait une étape **déjà traduite**, `showProgress`
# la traduisait une seconde fois, et `stageText` sur une chaîne retombait sur
# `raw` — soit "". `jobStage` doit donc rendre le triple `{key, args, raw}`
# (son contrat, documenté dans AGENTS.md) et `stageText` doit accepter les deux
# formes. La CI n'a pas de runtime JS : on vérifie la forme des sources, sans
# geler la mise en forme.

BLOC_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
LIGNE_COMMENT_RE = re.compile(r"//[^\n]*")


def _js(source):
    """Sans commentaires ni espaces superflus : ce qui est comparé est le code,
    pas sa façon de s'écrire."""
    nu = LIGNE_COMMENT_RE.sub(" ", BLOC_COMMENT_RE.sub(" ", source))
    return re.sub(r"\s+", " ", nu)


def fonction_js(html, signature):
    """Le source d'une `function`, jusqu'à l'accolade qui la referme."""
    start = html.index(signature)
    i, profonds = html.index("{", start), 0
    while True:
        if html[i] == "{":
            profonds += 1
        elif html[i] == "}":
            profonds -= 1
            if profonds == 0:
                break
        i += 1
    return _js(html[start:i + 1])


def affectation_js(html, declaration):
    """Le source d'une affectation, jusqu'au `;` qui la termine."""
    start = html.index(declaration)
    return _js(html[start:html.index(";", start)])


def test_job_stage_rend_le_triple_sans_le_traduire(html):
    """`jobStage` rend `{key, args, raw}` : c'est `showProgress` qui traduit.
    Le prétraduire appliquait `stageText` deux fois et vidait le libellé."""
    src = affectation_js(html, "const jobStage")
    assert "stageText(" not in src, f"jobStage ne doit plus traduire : {src}"
    for champ, valeur in (
        ("key", "j.stage_key"),
        ("args", "j.stage_args"),
        ("raw", "j.stage"),
    ):
        assert re.search(rf"\b{champ}\s*:\s*{re.escape(valeur)}\b", src), src


def test_stage_text_accepte_une_etape_deja_traduite(html):
    """Une chaîne est renvoyée telle quelle : sans ce cas, le repli `raw` du cas
    `{key, args, raw}` la réduirait à "" (et le changement de langue, qui rejoue
    `stageText(currentStage)`, aussi)."""
    src = fonction_js(html, "function stageText(")
    garde = re.search(r"typeof\s+st\s*===?\s*[\"']string[\"']", src)
    assert garde, f"stageText doit accepter une chaîne déjà traduite : {src}"
    assert garde.start() < src.index("!st.key"), (
        "le cas « chaîne » doit précéder le repli `raw`, qui la viderait"
    )


def test_le_libelle_vient_du_triple_jusqu_a_la_barre(html):
    """Toute la chaîne : `poll` → `jobStage(job)` → `showProgress` → `stageText`
    → `#stageText`. `showProgress` doit continuer à traduire, sinon un changement
    de langue ne réécrirait plus l'étape affichée."""
    assert re.search(
        r"showProgress\(\s*jobStage\(job\)\s*,\s*job\.progress\s*\)", html
    )
    assert "stageText(stage)" in fonction_js(html, "function showProgress(")


def test_aucune_cle_json_en_double(html):
    """`JSON.parse` et `json.loads` gardent la **dernière** occurrence d'une clé
    dupliquée : les tests passeraient, l'éditeur serait trompé. Le dictionnaire
    doit donc être sans doublon, dans les deux sections et les deux langues."""
    def paires(repere):
        def hook(pairs):
            vus, doublons = set(), set()
            for cle, _ in pairs:
                if cle in vus:
                    doublons.add(cle)
                vus.add(cle)
            assert not doublons, f"{repere}: clé(s) en double {sorted(doublons)}"
            return dict(pairs)

        return hook

    m = DICT_RE.search(html)
    assert m, "the translations JSON block is missing from index.html"
    json.loads(m.group(1), object_pairs_hook=paires("i18n"))
