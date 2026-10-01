"""Packaging scripts: the file names they produce are the ones the docs promise.

Source-level on purpose: building an artifact needs a 1.5 GB runtime tree (and, for
the Windows ones, a Windows host we do not have). What *can* be checked anywhere
is that a name did not drift between the script that writes it, the script that
reads it, and the documentation that tells a user what to download.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
APPIMAGE_SH = PACKAGING / "linux" / "make-appimage.sh"
PORTABLE_PS1 = PACKAGING / "make-portable.ps1"
MAKE_DEB = PACKAGING / "linux" / "make-deb.sh"
MAKE_TARBALL = PACKAGING / "linux" / "make-portable.sh"
APP_RUN = 'exec "$here/pyclean-audio/pyclean-audio" "$@"'
BUNDLED_TOOLS = ("build_runtime.sh", "build_runtime.ps1", "make-appimage.sh")


@pytest.fixture(scope="module")
def readme():
    return (PACKAGING / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def root_readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def test_les_scripts_qui_emballent_le_meme_arbre_sont_cites():
    """One tree, five formats: a script nobody mentions is a format nobody
    builds."""
    readme = (PACKAGING / "README.md").read_text(encoding="utf-8")
    for script in BUNDLED_TOOLS:
        assert script in readme, script
    for script in ("installer/pyclean-audio.iss", "linux/make-deb.sh",
                   "linux/make-portable.sh", "make-portable.ps1"):
        assert script in readme, script


def test_noms_de_fichiers_coherents_avec_la_documentation(readme):
    """The versioned names the scripts write, exactly as the docs spell them."""
    pairs = [
        (MAKE_DEB, r'PKG="pyclean-audio_\$\{VERSION\}_amd64\.deb"',
         "pyclean-audio_1.0.0_amd64.deb"),
        (MAKE_TARBALL, r'TARBALL="\$DIST/pyclean-audio-\$\{VERSION\}-\$\{ARCH\}\.tar\.gz"',
         "pyclean-audio-1.0.0-linux-x86_64.tar.gz"),
        (APPIMAGE_SH, r'APPIMAGE="\$DIST/pyclean-audio-\$\{VERSION\}-x86_64\.AppImage"',
         "pyclean-audio-1.0.0-x86_64.AppImage"),
        (PORTABLE_PS1, r'"pyclean-audio-\$Version-windows-x86_64\.zip"',
         "pyclean-audio-1.0.0-windows-x86_64.zip"),
    ]
    for path, pattern, documented in pairs:
        assert re.search(pattern, path.read_text(encoding="utf-8")), path.name
        assert documented in readme, f"{path.name} -> {documented} is not documented"


def test_les_scripts_qui_lisent_le_marqueur_webview(readme):
    """`dist/WEBVIEW` is written by build_runtime and read by make-deb: the two
    must agree on the spelling or the .deb describes a build that does not exist."""
    build = (PACKAGING / "build_runtime.sh").read_text(encoding="utf-8")
    deb = MAKE_DEB.read_text(encoding="utf-8")
    assert '"$ROOT/dist/WEBVIEW"' in build
    assert '"$DIST/WEBVIEW"' in deb
    assert "dist/WEBVIEW" in readme


def test_le_appimage_delegue_au_wrapper_existant():
    """AppRun must not re-implement the symlink loop: one implementation, in the
    tree's own `pyclean-audio`."""
    script = APPIMAGE_SH.read_text(encoding="utf-8")
    apprun = script[script.index('cat >"$STAGE/AppRun"'):script.index("APPRUN\nchmod")]
    assert APP_RUN in apprun
    assert 'readlink -f -- "$0"' in apprun       # resolves the AppImage mount point
    assert "while [ -L" not in apprun            # no second copy of the wrapper's loop


def test_le_appimage_echoue_sans_icone_plutot_que_d_en_produire_une():
    """appimagetool only warns about a missing icon; the build must refuse."""
    script = APPIMAGE_SH.read_text(encoding="utf-8")
    assert 'die "the AppImage does not contain $want' in script
    assert "pyclean-audio.svg" in script
    assert (PACKAGING / "linux" / "pyclean-audio.svg").is_file()


def test_appimagetool_est_eingle_par_digest():
    script = APPIMAGE_SH.read_text(encoding="utf-8")
    assert "releases/download/1.9.1/appimagetool-x86_64.AppImage" in script
    assert re.search(r'APPIMAGETOOL_SHA256="\$\{APPIMAGETOOL_SHA256:-[0-9a-f]{64}\}"', script)
    assert "cannot run as root" in script       # appimagetool refuses; so do we


def test_le_zip_windows_n_utilise_pas_compress_archive():
    """Compress-Archive refuses > 2 GB and this tree is bigger: it would leave a
    truncated archive behind."""
    script = PORTABLE_PS1.read_text(encoding="utf-8")
    assert "ZipFile]::CreateFromDirectory" in script
    # mentioned in a comment explaining why, never called
    assert not re.search(r"Compress-Archive\s+-(?:Path|LiteralPath)", script)
    assert "Mark of the Web" in (PACKAGING / "README.md").read_text(encoding="utf-8")


def test_chaque_format_est_decrit_sans_mensonge(readme, root_readme):
    """"What is verified" is the point of the per-artifact section: the two
    Windows formats must not claim a run."""
    for heading, verdict in (
        ("### `pyclean-audio_1.0.0_amd64.deb`", "built and inspected"),
        ("### `pyclean-audio-1.0.0-linux-x86_64.tar.gz`", "built, extracted and run"),
        ("### `pyclean-audio-1.0.0-x86_64.AppImage`", "built, mounted, run and stopped"),
        ("### `pyclean-audio-1.0.0-setup.exe`", "NOT built here"),
        ("### `pyclean-audio-1.0.0-windows-x86_64.zip`", "NOT built here"),
    ):
        assert heading in readme, heading
        line = readme[readme.index(heading):readme.index("\n", readme.index(heading))]
        assert verdict in line, line
    # the user's README mentions both new formats
    assert "AppImage" in root_readme
    assert "windows-x86_64.zip" in root_readme
    assert "Mark of the Web" in root_readme
