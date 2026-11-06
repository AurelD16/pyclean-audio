"""The ASR floors must ride along with every *watched* `nemo_toolkit[asr]` site.

An unpinned `nemo_toolkit[asr]` resolves to transformers 4.x / tokenizers 0.10.3,
a 2021 release with no cp311 wheel, so the install stops with "can't find Rust
compiler" (see "ASR constraints" in AGENTS.md). The floors themselves live in
requirements-asr.txt; this module only checks that the install sites carry that
file, and that it is the only place a version is written.

**Scope — this is not a tree-wide guarantee.** The watched set is the
`Dockerfile`, `run.sh`, the `requirements*.txt` of the root, and the two docs
that repeat the command (`AGENTS.md`, `README.md`). A site in a *new* file — a
`scripts/setup-asr.sh`, say — is not covered, and would not fail this module: the
tree is not globbed. Widening the set is a one-line change in the two call sites
below, and belongs with the file that introduces the new installer.

Source-level, like the other tests in this suite: no network, no pip
resolution, no JS runtime. The sites are *extracted* (logical lines, code blocks,
uncommented shell) rather than matched as whole files, so reformatting a
command does not break the test.
"""

import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
ASR = "nemo_toolkit[asr]"
CONTRAINTES = "requirements-asr.txt"
FLOORS = {"transformers": ">=5", "tokenizers": ">=0.21"}
PIP_INSTALL = re.compile(r"\bpip\s+install\b")
CONTINUATION = re.compile(r"\\\s*$")
COMMENTAIRE = re.compile(r"#.*$")
BLOC_CODE = re.compile(r"^```[^\n]*\n(.*?)^```", re.S | re.M)


# ------------------------------------------------------------------ extraction

def _lignes_logiques(source):
    """(numéro de ligne, ligne logique): une ligne physique terminée par `\\`
    n'est qu'un début de commande, donc les deux moitiés sont recollées."""
    lignes, tampon, debut = [], "", 1
    for no, brute in enumerate(source.splitlines(), start=1):
        if not tampon:
            debut = no
        tampon += brute
        if CONTINUATION.search(tampon):
            tampon = CONTINUATION.sub("", tampon) + " "
            continue
        lignes.append((debut, tampon))
        tampon = ""
    if tampon:
        lignes.append((debut, tampon))
    return lignes


def _lignes_shell(source):
    """Comme `_lignes_logiques`, commentaires retirés: une commande commentée
    n'est pas un site d'installation (dans la documentation, en revanche, une
    ligne commentée reste une commande à recopier)."""
    return [(no, COMMENTAIRE.sub("", ligne)) for no, ligne in _lignes_logiques(source)]


def _lignes_blocs(source):
    """Les seules lignes des blocs de code d'un Markdown: la prose qui parle de
    `nemo_toolkit[asr]` n'est pas un site d'installation."""
    for bloc in BLOC_CODE.finditer(source):
        offset = source[:bloc.start()].count("\n") + 1
        for i, ligne in enumerate(bloc.group(1).splitlines(), start=1):
            yield offset + i, ligne


def _sources(nom):
    source = (RACINE / nom).read_text(encoding="utf-8")
    if nom.endswith(".md"):
        return list(_lignes_blocs(source))
    if nom.endswith(".sh"):
        return _lignes_shell(source)
    return _lignes_logiques(source)


def _etages(source):
    """(nom d'étage, lignes logiques sans commentaire) : le Dockerfile est
    découpé sur ses `FROM`, pas lu d'un bloc, parce qu'un `COPY` n'appartient
    qu'à l'étage qui le déclare. Une étape sans `AS` porte le nom de son image,
    et une ligne commentée n'est ni une étape ni une instruction.

    Limite connue, et elle échoue du bon côté: un `FROM` porteur d'un drapeau
    (`FROM --platform=$BUILDPLATFORM python:3.11-slim AS builder-cpu`) est nommé
    d'après ce drapeau, donc l'étage sort de la comparaison et l'assertion sur
    les noms échoue bruyamment, en rouge. `docker-publish.yml` passe les
    plateformes par `platforms:` et non par un `FROM --platform`, donc rien ne
    le déclenche aujourd'hui; le fix serait `(?:--platform=\\S+\\s+)*` avant le
    groupe image."""
    nom, lignes = None, []
    for no, ligne in _lignes_logiques(source):
        nue = COMMENTAIRE.sub("", ligne).strip()
        if not nue:
            continue
        if re.match(r"(?i)FROM\s", nue):
            if nom is not None:
                yield nom, lignes
            m = re.match(r"(?i)FROM\s+(\S+)(?:\s+AS\s+(\S+))?", nue)
            nom, lignes = (m.group(2) or m.group(1)).lower(), []
            continue
        if nom is not None:
            lignes.append((no, nue))
    if nom is not None:
        yield nom, lignes


def _sites(nom):
    """Les lignes qui installent réellement NeMo : ni un `echo`, ni une prose,
    ni une mention dans un message d'erreur ne comptent."""
    return [
        (no, ligne)
        for no, ligne in _sources(nom)
        if ASR in ligne and PIP_INSTALL.search(ligne)
    ]


def _exigences(nom):
    """Les exigences d'un requirements.txt, sans ses commentaires ni ses options
    (`-c`, `--index-url`…). `LavaSR @ git+…` est une exigence, pas une URL."""
    return [
        ligne
        for _, ligne in _lignes_logiques((RACINE / nom).read_text(encoding="utf-8"))
        if ligne.strip() and not COMMENTAIRE.match(ligne.strip()) and not ligne.startswith("-")
    ]


def _contrainte(ligne):
    """Le fichier passé à `-c` / `--constraint`, s'il y en a un."""
    m = re.search(r"(?:^|\s)(?:-c|--constraints?|--constraint)\s+(\S+)", ligne)
    return m.group(1) if m else None


# ------------------------------------------------------------- the constraints

def _lignes_contraintes():
    """Les exigences du fichier de contraintes, sans ses commentaires."""
    return [
        ligne.strip()
        for ligne in (RACINE / CONTRAINTES).read_text(encoding="utf-8").splitlines()
        if ligne.strip() and not COMMENTAIRE.match(ligne.strip())
    ]


def test_les_planchers_sont_dans_le_fichier_de_contraintes():
    """Les deux floors, et eux seuls : `requirements-asr.txt` est la source
    unique, sinon un site pourrait être contraint avec un autre jeu."""
    lignes = _lignes_contraintes()
    assert lignes == [f"{nom}{spec}" for nom, spec in FLOORS.items()], lignes


def test_aucun_echantillon_exact_dans_les_contraintes():
    """Un `==` ferait de ce fichier un lock file miniature — ce qu'AGENTS.md
    interdit explicitement. Un plancher qui devient insatisfaisable doit faire
    échouer l'installation bruyamment, pas la dégrader."""
    lignes = _lignes_contraintes()
    assert not [x for x in lignes if re.search(r"==|~=", x)], lignes


def test_le_fichier_de_contraintes_est_lisible_depuis_la_racine():
    """`run.sh` fait `cd "$(dirname "$0")"` et les constructeurs copient le
    fichier dans /src : un chemin relatif ne fonctionne que depuis la racine."""
    assert not CONTRAINTES.startswith("/")
    assert (RACINE / CONTRAINTES).is_file()


# ------------------------------------------------------------- every call site

@pytest.mark.parametrize(
    ("nom", "attendu"),
    [
        ("Dockerfile", 2),  # builder-cpu et builder-gpu
        ("run.sh", 2),      # --asr sur un .venv existant, et sur un .venv neuf
    ],
)
def test_chaque_site_d_execution_transporte_la_contrainte(nom, attendu):
    sites = _sites(nom)
    assert len(sites) == attendu, f"{nom}: {len(sites)} install(s) de {ASR}, {attendu} attendu(s)"
    for no, ligne in sites:
        assert _contrainte(ligne) == CONTRAINTES, f"{nom}:{no} installs {ASR} sans -c : {ligne}"


@pytest.mark.parametrize("nom", ["AGENTS.md", "README.md"])
def test_la_documentation_donne_la_bonne_commande(nom):
    """Les deux documents répètent la commande : une copie sans `-c` est une
    recipe cassée que l'utilisateur recopie telle quelle."""
    sites = _sites(nom)
    assert sites, f"{nom} ne documente plus l'installation de {ASR}"
    for no, ligne in sites:
        assert _contrainte(ligne) == CONTRAINTES, f"{nom}:{no} sans -c : {ligne}"


def test_requirements_txt_est_contraint_pour_pip_et_pour_uv():
    """`pip install -r requirements.txt` et `uv pip install -r requirements.txt`
    lisent tous deux un `-c` placé dans le fichier, et le chemin est résolu
    depuis le répertoire du requirements — donc le nom relatif suffit."""
    lignes = _lignes_logiques((RACINE / "requirements.txt").read_text(encoding="utf-8"))
    exige_asr = [no for no, ligne in lignes if ligne.strip() == ASR]
    cites = [no for no, ligne in lignes if _contrainte(ligne) == CONTRAINTES]
    assert exige_asr, f"requirements.txt n'installe plus {ASR}"
    assert cites, f"requirements.txt n'a pas de -c {CONTRAINTES}"
    assert min(cites) < max(exige_asr), (
        "le -c doit précéder l'exigence qu'il contraint, comme dans pip"
    )


def test_tout_requirements_qui_installe_nemo_est_contraint():
    """Le garde-fou suit les fichiers : un nouveau requirements qui installe
    NeMo doit venir contraint lui aussi (requirements-base.txt n'en a pas
    besoin, il n'installe pas NeMo)."""
    avec_nemo = {
        p.name for p in RACINE.glob("requirements*.txt") if ASR in " ".join(_exigences(p.name))
    }
    assert avec_nemo == {"requirements.txt"}, avec_nemo
    lignes = _lignes_logiques((RACINE / "requirements.txt").read_text(encoding="utf-8"))
    cites = [_contrainte(ligne) for _, ligne in lignes]
    assert CONTRAINTES in cites, f"requirements.txt installe {ASR} sans {CONTRAINTES}"


def test_chaque_constructeur_copie_le_fichier_de_contraintes():
    """`-c requirements-asr.txt` échoue à la construction si le COPY manque:
    `-c` ne va pas chercher le fichier dans le contexte de build.

    Le contrôle est **par étage**, et un compte global ne suffirait pas : les
    deux constructeurs sont deux `FROM` distincts, et l'étage `test` copie
    lui aussi les sources gardées. Un `COPY` dans une seule étape ne couvre
    donc pas l'autre constructeur — c'est ce que la version précédente laissait
    passer (3 COPY comptés pour 2 installs).

    Autre limite, celle-là non vérifiée: l'**ordre** des instructions. Un `COPY`
    placé après le `RUN` de NeMo satisferait ce test et casserait quand même la
    construction, à la construction de l'image. Rouge, donc dans la bonne
    direction, mais ce n'est pas une couverture complète."""
    source = (RACINE / "Dockerfile").read_text(encoding="utf-8")
    concernes = {}
    for nom, lignes in _etages(source):
        sites = [no for no, ligne in lignes if ASR in ligne and PIP_INSTALL.search(ligne)]
        if not sites:
            continue
        copies = [
            no
            for no, ligne in lignes
            if ligne.upper().startswith("COPY") and CONTRAINTES in ligne
        ]
        assert copies, (
            f"{nom}:{sites} installe {ASR} sans copier {CONTRAINTES} — "
            "le -c échouerait à la construction"
        )
        concernes[nom] = copies
    assert set(concernes) == {"builder-cpu", "builder-gpu"}, sorted(concernes)


def test_le_contexte_de_construction_n_exclut_pas_le_fichier_de_contraintes():
    """Le COPY ci-dessus échoue aussi si `.dockerignore` exclut le fichier."""
    motifs = [
        ligne.strip()
        for ligne in (RACINE / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if ligne.strip() and not COMMENTAIRE.match(ligne.strip())
    ]
    exclus = [m for m in motifs if CONTRAINTES.startswith(m.rstrip("/"))]
    assert not exclus, f".dockerignore exclut {CONTRAINTES} : {exclus}"


# ------------------------------------------ la propriété qui a cassé (regression)

def test_les_planchers_empchent_le_retour_aux_versions_de_2021():
    """La résolution qui échouait : sans les floors, le résolveur prend
    huggingface-hub 2.x, puis le transformers le plus récent qui l'accepte
    (4.12.2), qui ramène tokenizers à 0.10.3 — sans roue cp311. Avec les floors,
    il faut transformers >= 5 et tokenizers >= 0.21 *et* huggingface-hub 1.x,
    parce que transformers 5.x refuse hub 2.x. Le test ne résout rien (ni réseau
    ni PyPI ici) : il vérifie que les floors qui l'interdisent sont bien celles
    que le fichier de contraintes déclare.
    """
    contenu = (RACINE / CONTRAINTES).read_text(encoding="utf-8")
    floors = re.findall(r"^(\S+)\s*(>=[\d.]+)$", contenu, re.M)
    interdits = {nom for nom, _ in floors} - {"transformers", "tokenizers"}
    assert not interdits, f"contraintes inattendues : {interdits}"
    # transformers 5.x annonce `huggingface-hub<2`: la borne haute de hub n'a
    # pas à être écrite, elle découle du plancher transformers.
    assert FLOORS["transformers"] == ">=5", FLOORS
    assert FLOORS["tokenizers"] == ">=0.21", FLOORS
