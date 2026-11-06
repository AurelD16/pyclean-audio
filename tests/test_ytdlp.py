"""yt-dlp wrapper: availability, options, progress, cancellation, stem.

No network, no model: a fake `yt_dlp` module is injected in `sys.modules` (the
pattern of `tests/conftest.py`), and the extraction result is a dict the test
controls. `is_available()` uses `importlib.util.find_spec`, so the fake needs a
`__spec__` — the real package is **not** what is under test here.
"""

import importlib.machinery
import sys
import threading
import types

import pytest

from app import downloader as d
from app.cancel import JobCancelled
from app.config import MAX_SIZE
from app.messages import MediaError

URL = "https://www.example.com/watch?v=abc"


@pytest.fixture(autouse=True)
def _clear_version_cache():
    """`version()` is cached (status() is polled every 2 s): each test starts
    from a clean cache."""
    d.version.cache_clear()
    yield
    d.version.cache_clear()


def _install(monkeypatch, extract):
    """A fake yt_dlp: `extract(url, download)` returns whatever `extract` says,
    and every option dict is recorded in `calls`."""
    calls = []

    class YoutubeDL:
        def __init__(self, params):
            self.params = params
            calls.append(params)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return extract(url, download, self.params)

    class DownloadError(Exception):
        pass

    utils = types.ModuleType("yt_dlp.utils")
    utils.DownloadError = DownloadError
    version = types.ModuleType("yt_dlp.version")
    version.__version__ = "0.0-test"
    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = YoutubeDL
    mod.utils = utils
    mod.version = version
    # find_spec() reads __spec__ on the injected module
    mod.__spec__ = importlib.machinery.ModuleSpec("yt_dlp", None)
    for name, m in (("yt_dlp", mod), ("yt_dlp.utils", utils),
                    ("yt_dlp.version", version)):
        monkeypatch.setitem(sys.modules, name, m)
    return calls, DownloadError


def _video(title="Une video", entry_id="v1", path=None, **extra):
    """The shape `extract_info(download=True)` returns for a finished video:
    `requested_downloads[0].filepath` is the file that was really written."""
    info = {"title": title, "id": entry_id, "duration": 61.5,
            "requested_downloads": [{"filepath": path or f"/dest/{entry_id}.mp4"}]}
    info.update(extra)
    return info


def _noop(stage=None):
    return lambda *a, **k: None


# ------------------------------------------------------------------ is_available

def test_is_available_false_sans_le_paquet(monkeypatch):
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: None)
    assert d.is_available() is False


def test_is_available_true_avec_la_fausse(monkeypatch):
    _install(monkeypatch, lambda *a: {})
    assert d.is_available() is True


def test_version_est_lue_sur_place_et_memoisee(monkeypatch):
    _install(monkeypatch, lambda *a: {})
    assert d.version() == "0.0-test"
    assert d.version() == "0.0-test"


def test_version_absente_sans_le_paquet(monkeypatch):
    monkeypatch.setattr(d.importlib.util, "find_spec", lambda name: None)
    assert d.version() is None


# --------------------------------------------------------------------- resolve

def test_resolve_une_video_donne_une_entree(monkeypatch):
    _install(monkeypatch, lambda *a: {"id": "v1", "title": "Une vidéo",
                                      "duration": 12.0})
    entries = d.resolve(URL)
    assert [e["title"] for e in entries] == ["Une vidéo"]


def test_resolve_une_playlist_donne_une_entree_par_video(monkeypatch):
    _install(monkeypatch, lambda *a: {"_type": "playlist", "entries": [
        {"id": "a", "title": "A", "duration": 1.0},
        {"id": "b", "title": "B", "duration": 2.0},
        {"id": "c", "title": "C", "duration": 3.0},
    ]})
    assert [e["id"] for e in d.resolve(URL)] == ["a", "b", "c"]


def test_resolve_ignore_les_entrees_vides(monkeypatch):
    _install(monkeypatch, lambda *a: {"_type": "playlist",
                                      "entries": [{"id": "a"}, None]})
    assert [e["id"] for e in d.resolve(URL)] == ["a"]


def test_resolve_ne_telecharge_rien(monkeypatch):
    calls, _ = _install(monkeypatch, lambda *a: {"id": "a", "title": "A"})
    d.resolve(URL)
    opts = calls[-1]
    assert opts["skip_download"] is True
    assert opts["extract_flat"] == "in_playlist"
    assert opts["noplaylist"] is False


def test_resolve_echec_devenit_une_erreur_traductible(monkeypatch):
    def boom(*a):
        raise RuntimeError("x" * 500)

    _install(monkeypatch, boom)
    with pytest.raises(MediaError) as exc:
        d.resolve(URL)
    assert exc.value.code == "download_failed"
    # le détail est tronqué : une URL peut porter une signature
    assert len(exc.value.params["detail"]) == 300


# -------------------------------------------------------------------- download

def _download(monkeypatch, extract, dest, fmt="mp3", **kw):
    calls, DownloadError = _install(monkeypatch, extract)
    kw.setdefault("on_stage", _noop())
    kw.setdefault("cancel", threading.Event())
    got = d.download(URL, dest, fmt, **kw)
    return got, calls, DownloadError


def test_download_mp3_ne_ritient_que_du_son(monkeypatch, tmp_path):
    def extract(url, download, opts):
        opts["progress_hook"]({"status": "downloading", "downloaded_bytes": 50,
                               "total_bytes": 100})
        opts["progress_hook"]({"status": "finished", "downloaded_bytes": 100,
                               "total_bytes": 100})
        p = tmp_path / "a.m4a"
        p.write_bytes(b"a")
        return _video(path=str(p))

    got, calls, _ = _download(monkeypatch, extract, tmp_path, "mp3")
    opts = calls[-1]
    assert opts["format"].startswith("bestaudio")
    assert "merge_output_format" not in opts   # aucun merge : pas de vidéo
    assert got[0].path.name == "a.m4a"
    assert got[0].duration == 61.5
    assert got[0].subtitle is None
    assert opts["max_filesize"] == MAX_SIZE


def test_download_mp4_eleve_et_fusionne_en_mp4(monkeypatch, tmp_path):
    def extract(url, download, opts):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"a")
        return _video(path=str(p))

    got, calls, _ = _download(monkeypatch, extract, tmp_path, "mp4")
    opts = calls[-1]
    assert opts["format"] == "bestvideo+bestaudio/best"
    assert opts["merge_output_format"] == "mp4"
    assert opts["max_filesize"] == MAX_SIZE
    assert got[0].path.suffix == ".mp4"


def test_download_demande_les_sous_titres_du_site(monkeypatch, tmp_path):
    def extract(url, download, opts):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"a")
        info = _video(path=str(p))
        info["requested_subtitles"] = {
            "fr": {"ext": "srt", "filepath": str(tmp_path / "a.fr.srt")},
        }
        (tmp_path / "a.fr.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nx\n")
        return info

    got, calls, _ = _download(monkeypatch, extract, tmp_path, "mp4",
                              subtitles=True, subtitle_lang="en")
    opts = calls[-1]
    assert opts["writesubtitles"] is True
    assert opts["subtitleslangs"] == ["en"]
    assert opts["writeautomaticsub"] is False
    assert opts["postprocessors"] == [
        {"key": "FFmpegSubtitlesConvertor", "format": "srt"}]
    assert got[0].subtitle is not None
    assert got[0].subtitle.suffix == ".srt"


def test_download_sans_sous_titres_n_ajoute_aucune_option(monkeypatch, tmp_path):
    def extract(url, download, opts):
        p = tmp_path / "a.mp4"
        p.write_bytes(b"a")
        return _video(path=str(p))

    got, calls, _ = _download(monkeypatch, extract, tmp_path, "mp4")
    opts = calls[-1]
    assert "writesubtitles" not in opts
    assert "postprocessors" not in opts
    assert got[0].subtitle is None


def test_download_rejoue_la_progression(monkeypatch, tmp_path):
    seen = []

    def extract(url, download, opts):
        opts["progress_hook"]({"status": "downloading", "downloaded_bytes": 30,
                               "total_bytes": 60})
        p = tmp_path / "a.mp4"
        p.write_bytes(b"a")
        return _video(path=str(p))

    _download(monkeypatch, extract, tmp_path, "mp4",
              on_stage=lambda p: seen.append(p))
    assert seen == [0.5]


def test_progression_sans_taille_connue_vaut_1_a_la_fin(monkeypatch, tmp_path):
    seen = []

    def extract(url, download, opts):
        opts["progress_hook"]({"status": "downloading", "downloaded_bytes": 30})
        opts["progress_hook"]({"status": "finished"})
        p = tmp_path / "a.mp4"
        p.write_bytes(b"a")
        return _video(path=str(p))

    _download(monkeypatch, extract, tmp_path, "mp4",
              on_stage=lambda p: seen.append(p))
    assert seen == [0.0, 1.0]
    assert all(0.0 <= p <= 1.0 for p in seen)


def test_annulation_leve_jobcancelled(monkeypatch, tmp_path):
    cancel = threading.Event()
    cancel.set()

    def extract(url, download, opts):
        opts["progress_hook"]({"status": "downloading", "downloaded_bytes": 1,
                               "total_bytes": 2})
        return _video()

    _install(monkeypatch, extract)
    with pytest.raises(JobCancelled):
        d.download(URL, tmp_path, "mp4", cancel=cancel)


def test_une_annulation_l_emporte_sur_une_erreur_de_telechargement(
        monkeypatch, tmp_path):
    """Un DownloadError levé dans la même fenêtre que l'annulation doit rester
    une annulation, jamais un échec de téléchargement."""
    def extract(url, download, opts):
        raise RuntimeError("interrompu")

    _install(monkeypatch, extract)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelled):
        d.download(URL, tmp_path, "mp4", cancel=cancel)


def test_echec_de_telechargement_devenit_une_erreur_traductible(monkeypatch, tmp_path):
    def extract(url, download, opts):
        raise RuntimeError("HTTP Error 403")

    _install(monkeypatch, extract)
    with pytest.raises(MediaError) as exc:
        d.download(URL, tmp_path, "mp4", cancel=threading.Event())
    assert exc.value.code == "download_failed"
    assert "403" in exc.value.params["detail"]


def test_download_rien_dans_le_dossier_est_une_erreur(monkeypatch, tmp_path):
    _install(monkeypatch, lambda *a: {"id": "v1", "title": "Sans fichier"})
    with pytest.raises(MediaError):
        d.download(URL, tmp_path, "mp4", cancel=threading.Event())


def _one_file(dest):
    def extract(url, download, opts):
        p = dest / "a.mp4"
        p.write_bytes(b"a")
        return _video(path=str(p))

    return extract


def test_download_une_seule_entree_de_playlist(monkeypatch, tmp_path):
    """`playlist_items` est en base 1 : une entrée à la fois, ce qui borne le
    disque d'une longue playlist."""
    got, calls, _ = _download(monkeypatch, _one_file(tmp_path), tmp_path,
                              "mp4", playlist_index=2)
    assert calls[-1]["playlist_items"] == "3"
    assert len(got) == 1


# ------------------------------------------------------------------ stem

def test_stem_ne_peut_pas_franchir_un_repertoire():
    stem = d.stem_for("../../etc/passwd", "v1")
    assert "/" not in stem
    assert ".." not in stem


def test_stem_borne_accents_et_longueur():
    stem = d.stem_for("Elephant a l.Flow - " * 40, "v1")
    assert len(stem) <= d.MAX_STEM
    assert stem.strip()


def test_stem_jamais_vide():
    for vide in ("", "   ", "...", "///"):
        assert d.stem_for(vide, "v1").strip()
        assert d.stem_for(vide).strip()
    assert d.stem_for("", "v1") == "video-v1"


def test_stem_ne_garde_que_les_caracteres_surs():
    stem = d.stem_for("Ma video (2009) #1", "v1")
    assert "(" not in stem and "#" not in stem
    assert stem == "Ma video _2009_ _1"
