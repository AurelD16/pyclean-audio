"""Revue QA de la catégorie yt-dlp : les trous que la suite du projet ne couvre pas.

Ni réseau ni modèle (mêmes fakes que `tests/test_main.py`) : tout passe par
`app.main` / `app.downloader` avec un `process_file` et un `yt_dlp` factices.
"""

import io
import time
import wave
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import downloader
from app import main as m
from app.cancel import JobCancelled

VIDEO_URL = "https://www.example.com/watch?v=abc"


# ------------------------------------------------------------------- fixtures

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """`data/jobs` temporaire et registre vide (même fixture que test_main)."""
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(m, "DATA", d)
    with m.JOBS_LOCK:
        m.JOBS.clear()
    yield d
    with m.JOBS_LOCK:
        m.JOBS.clear()


@pytest.fixture
def no_dns(monkeypatch):
    monkeypatch.setattr(m, "_host_addresses", lambda host: [])


@pytest.fixture
def yt_ready(monkeypatch):
    monkeypatch.setattr(downloader, "is_available", lambda: True)
    monkeypatch.setattr(downloader, "resolve",
                        lambda url: [{"id": "v1", "title": "Ma video"}])
    return downloader


@pytest.fixture
def harness(monkeypatch):
    import queue as _queue
    import threading as _th

    q = _queue.Queue(maxsize=10)
    monkeypatch.setattr(m, "QUEUE", q)
    monkeypatch.setattr(m, "WORKER_STARTED", True)
    started = []

    def start():
        t = _th.Thread(target=m._worker, daemon=True)
        t.start()
        started.append(t)

    yield q, start
    q.put_nowait(None)
    for t in started:
        t.join(timeout=5)


def wait_state(job, states, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if job["state"] in states:
            return True
        time.sleep(0.01)
    return False


def wait_gone(path, timeout=5):
    end = time.time() + timeout
    while path.exists() and time.time() < end:
        time.sleep(0.01)
    return not path.exists()


def petit_wav(secondes=1, sr=8000):
    """Un WAV mono silence, construit en mémoire."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * sr * secondes)
    return buf.getvalue()


@pytest.fixture
def fake_download(monkeypatch):
    """`downloader.download()` : écrit un faux média (+ un faux `.srt`)."""
    calls = []

    def _download(url, dest, fmt="mp4", subtitles=False, subtitle_lang="fr",
                  on_stage=None, cancel=None, playlist_index=None):
        calls.append({"url": url, "fmt": fmt, "subtitles": subtitles,
                      "cancel": cancel, "playlist_index": playlist_index})
        if on_stage is not None:
            on_stage(0.5)
        dest.mkdir(parents=True, exist_ok=True)
        src = dest / "source.m4a"
        src.write_bytes(b"media")
        sub = None
        if subtitles:
            sub = dest / "source.fr.srt"
            sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nbonjour\n",
                           encoding="utf-8")
        return [downloader.Downloaded(title="Ma video", path=src, subtitle=sub,
                                      duration=30.0)]

    monkeypatch.setattr(m.downloader, "download", _download)
    return calls


def faux_pipeline(seen, echoue_au=None):
    """Un `process_file` qui écrit ses fichiers et journalise ses arguments."""
    def _process_file(src, outdir, denoise, input_sr, cutoff, on_stage,
                      output_format="wav", transcribe=False, cancel=None,
                      keep_original=True):
        seen.setdefault("calls", []).append(Path(src))
        seen.setdefault("keep", []).append(keep_original)
        seen.setdefault("transcribe", []).append(transcribe)
        seen.setdefault("output_format", []).append(output_format)
        if echoue_au is not None and len(seen["calls"]) == echoue_au:
            raise RuntimeError("pas de piste audio")
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        on_stage("Test…", 1.0)
        (out / "x_enhanced.wav").write_bytes(b"wav")
        (out / "x_pyclean-audio.mp3").write_bytes(b"mp3")
        original = out / "x_original.wav"
        if keep_original:
            original.write_bytes(b"wav")
        video = out / "x_pyclean-audio.mp4" if seen.get("with_video") else None
        if video:
            video.write_bytes(b"mp4")
        return {"kind": "video" if video else "audio",
                "original_wav": str(original) if keep_original else None,
                "enhanced_wav": str(out / "x_enhanced.wav"),
                "enhanced_mp3": str(out / "x_pyclean-audio.mp3"),
                "transcript": None, "transcript_srt": None, "duration": 30.0,
                "output": str(video) if video else None}

    return _process_file


@pytest.fixture
def fake_pipeline(monkeypatch):
    seen = {"with_video": False}
    monkeypatch.setattr(m, "process_file", faux_pipeline(seen))
    return seen


def _plan(entries, **over):
    plan = {"url": VIDEO_URL, "fmt": "mp4", "subtitles": False,
            "subtitle_lang": "fr", "entries": entries}
    plan.update(over)
    return plan


def _job(data_dir, job_id, **fields):
    job = {"id": job_id, "state": "queued", "stage": "", "progress": 0.0,
           "error": None, "error_code": None, "error_params": {},
           "artifacts": {}, **fields}
    (data_dir / job_id).mkdir(parents=True, exist_ok=True)
    return m._register(job)


def _single(data_dir, job_id, title="Ma video"):
    ent = m._download_plan([{"id": "v1", "title": title}])[0]
    job = _job(data_dir, job_id, kind=None, source="ytdlp", stem=ent["stem"],
               filename=title)
    return job, _plan([ent])


def _playlist(data_dir, job_id, titles):
    ents = m._download_plan([{"id": f"v{i}", "title": t}
                             for i, t in enumerate(titles)])
    files = [{"index": i, "relpath": e["relpath"], "stem": e["stem"],
              "state": "queued", "stage": "", "stage_key": None,
              "stage_args": {}, "progress": 0.0, "error": None,
              "error_code": None, "error_params": {}, "kind": None,
              "artifacts": {}} for i, e in enumerate(ents)]
    job = _job(data_dir, job_id, kind="folder", source="ytdlp", stem=None,
               output_format="mp3", filename=f"{len(files)} files (playlist)",
               files=files)
    return job, _plan(ents), files


def _boom(exc):
    """Un faux `downloader.download` qui leve : `_fetch` passe 5 positionnels."""
    def _download(url, dest, fmt="mp4", subtitles=False, subtitle_lang="fr",
                  on_stage=None, cancel=None, playlist_index=None):
        raise exc
    return _download


def _lancer(job, plan):
    m._enqueue(job, m._run_download_job,
               (plan, False, 16000, None, job["_cancel"]))


# ------------------------------------------------- SSRF : vecteurs non couverts

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://127.0.0.1:8787/api/status", "http://localhost/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/",
    "http://2130706433/", "http://0177.0.0.1/", "http://0x7f.0.0.1/",
    "http://127.1/", "http://0.0.0.0/", "http://10.0.0.5/", "http://172.16.0.1/",
    "http://192.168.1.10/x", "http://169.254.169.254/latest/meta-data/",
    "http://[fe80::1%25eth0]/", "http://[fd00::1]/", "http://[::]/",
    "http://224.0.0.1/", "http://240.0.0.1/", "http://255.255.255.255/",
    "http://198.18.0.1/", "http://192.0.2.1/", "http://198.51.100.1/",
    "http://①②⑦.0.0.1/",
    "http://user@127.0.0.1/", "http://example.com@127.0.0.1/",
    "HTTP://127.0.0.1/",
])
def test_check_url_refuse_chaque_forme_de_boucle_ou_Adresse_interne(url):
    """Boucle locale, IPv4-mapped IPv6, notations décimale/octale/hexadécimale,
    plages de documentation, lien-local avec zone, chiffres Unicode pleine
    largeur, et un userinfo qui masque l'hôte réel."""
    with pytest.raises(HTTPException) as exc:
        m._check_url(url)
    assert exc.value.status_code == 400
    assert exc.value.code in ("blocked_url", "bad_url"), url


@pytest.mark.parametrize("url", [
    "", "   ", "pas une url", "file:///etc/passwd", "ftp://example.com/x",
    "gopher://example.com/x", "http:/example.com", "//example.com/x",
    "example.com/watch?v=1", "http://", "https:///path",
    "javascript:alert(1)",
])
def test_check_url_refuse_ce_qui_nest_pas_une_url_http(url):
    with pytest.raises(HTTPException) as exc:
        m._check_url(url)
    assert exc.value.status_code == 400
    assert exc.value.code == "bad_url", url


def test_check_url_utilise_le_hote_apres_le_credentiel():
    """`http://127.0.0.1@example.com/` est un accès à example.com : autorisé.
    L'inverse est un accès à la boucle locale : refusé. Le userinfo ne doit
    jamais sauter la vérification."""
    m._check_url("http://127.0.0.1@example.com/x")   # ne lève rien
    with pytest.raises(HTTPException) as exc:
        m._check_url("http://example.com@127.0.0.1/x")
    assert exc.value.code == "blocked_url"


def test_check_url_refuse_des_qu_une_adresse_privee_est_melangee(monkeypatch):
    """Un nom qui résout en public ET en privé est refusé : la boucle teste
    toutes les adresses renvoyées, pas seulement la première."""
    import ipaddress

    monkeypatch.setattr(m, "_host_addresses", lambda host: [
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("10.0.0.1")])
    with pytest.raises(HTTPException) as exc:
        m._check_url("https://exemple.example/x")
    assert exc.value.code == "blocked_url"


def test_check_url_autorise_un_hote_public(monkeypatch):
    import ipaddress

    monkeypatch.setattr(m, "_host_addresses",
                        lambda host: [ipaddress.ip_address("93.184.216.34")])
    m._check_url(VIDEO_URL)


def test_check_url_ne_leve_pas_pour_un_nom_qui_ne_resout_pas(monkeypatch):
    """Un nom inconnu ne doit pas faire lever : c'est `resolve()` qui rendra
    un `download_failed` traduisible, jamais une 500."""
    monkeypatch.setattr(m, "_host_addresses", lambda host: [])
    m._check_url("http://nexiste-pas-du-tout.example/")


def test_un_hote_qui_ne_resout_pas_repond_4xx_avec_un_code(data_dir, no_dns,
                                                           yt_ready, monkeypatch):
    def boom(url):
        raise downloader.MediaError("download_failed", detail="Video unavailable")

    monkeypatch.setattr(downloader, "resolve", boom)
    r = TestClient(m.app).post("/api/download", data={"url": VIDEO_URL})
    assert r.status_code == 400
    assert r.json()["code"] == "download_failed"


def test_post_sans_url_repond_4xx_sans_500(data_dir, no_dns, yt_ready):
    r = TestClient(m.app).post("/api/download", data={})
    assert 400 <= r.status_code < 500
    assert "detail" in r.json()


def test_cutoff_non_entier_repond_4xx_comme_les_autres_endpoints(data_dir, no_dns,
                                                                 yt_ready):
    r = TestClient(m.app).post("/api/download",
                               data={"url": VIDEO_URL, "cutoff": "abc"})
    assert 400 <= r.status_code < 500


# ------------------------------------------------- titres hostiles -> nom de ZIP

@pytest.mark.parametrize("titre", [
    "../../../../tmp/pwned", "/etc/absolute", "..", ".", "...", ".cache",
    "C:\\Windows\\System32\\evil", "a/../../b", "CON", "nul.txt", " ",
    "Café ☕ ünïcode", "日本語のタイトル", "x" * 400, "-dash", "tab\there",
    "sp ace.mp4", "under_score.mp4", "%2e%2e/x", "‮exe.mp4",
])
def test_plan_relpath_ne_sort_jamais_de_l_arbre(titre):
    """`_download_plan` alimente `relpath`, dont `_build_folder_zip` tire les
    noms d'entrées du ZIP : ni `..`, ni absolu, ni séparateur Windows."""
    ent = m._download_plan([{"id": "v1", "title": titre}])[0]
    rel = ent["relpath"]
    assert ".." not in Path(rel).parts, titre
    assert not Path(rel).is_absolute(), titre
    assert "\\" not in rel, titre
    assert "/" not in Path(rel).name, titre
    assert "/" not in ent["stem"] and "\\" not in ent["stem"], titre
    assert ".." not in ent["stem"], titre
    assert len(ent["stem"]) <= downloader.MAX_STEM, titre
    assert ent["stem"].strip(), titre


def test_plan_deduplique_les_titres_identiques():
    plan = m._download_plan([{"id": str(i), "title": "Chanson"} for i in range(3)])
    assert [e["stem"] for e in plan] == ["Chanson", "Chanson_2", "Chanson_3"]


def test_zip_d_un_titre_hostile_ne_peut_pas_ecrire_hors_du_repertoire(
        data_dir, no_dns, yt_ready, harness, fake_download, fake_pipeline,
        monkeypatch):
    """Bout en bout : POST /api/download -> worker -> ZIP. Le titre est choisi
    par celui qui met la vidéo en ligne."""
    titres = ["../../../../tmp/pwned", "/etc/absolute", "C:\\evil\\x",
              "Café ☕", ".cache", "..", "x" * 300, "日本語"]
    monkeypatch.setattr(downloader, "resolve",
                        lambda url: [{"id": f"v{i}", "title": t}
                                     for i, t in enumerate(titres)])
    r = TestClient(m.app).post("/api/download", data={"url": VIDEO_URL})
    assert r.status_code == 200
    job = m.JOBS[r.json()["job_id"]]
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done", job.get("error")
    with zipfile.ZipFile(job["zip"]) as zf:
        names = zf.namelist()
    assert names
    for name in names:
        assert ".." not in Path(name).parts, name
        assert not name.startswith("/"), name
        assert "\\" not in name, name
        assert not Path(name).is_absolute(), name


# ------------------------------------------------------- sous-titres : contrat

def test_sous_titres_on_ne_produit_jamais_de_transcription(data_dir, harness,
                                                          fake_download,
                                                          fake_pipeline):
    """Le serveur appelle toujours `process_file(transcribe=False)` : les
    sous-titres du site *remplacent* Parakeet, aucun artefact transcript*."""
    job, plan = _single(data_dir, "s1")
    plan["subtitles"] = True
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    assert fake_pipeline["transcribe"] == [False]
    assert "transcript" not in job["artifacts"]
    assert "transcript_srt" not in job["artifacts"]
    assert job["artifacts"]["subtitle"].endswith(".srt")
    snap = m._snapshot(job)
    assert "/" not in snap["artifacts"]["subtitle"]
    assert not any(k.startswith("_") for k in snap)


def test_sous_titres_off_laisse_le_comportement_precedent_intact(data_dir,
                                                                harness,
                                                                fake_download,
                                                                fake_pipeline):
    job, plan = _single(data_dir, "s2")
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    assert fake_download[0]["subtitles"] is False
    assert "subtitle" not in job["artifacts"]
    assert fake_pipeline["transcribe"] == [False]
    assert fake_pipeline["keep"] == [True]        # A/B conservé
    assert fake_pipeline["output_format"] == ["mp3"]


def test_le_sous_titre_est_servi_en_srt(data_dir, harness, fake_download,
                                        fake_pipeline):
    job, plan = _single(data_dir, "s3", title="Ma video")
    plan["subtitles"] = True
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    r = TestClient(m.app).get(f"/api/jobs/{job['id']}/file/subtitle")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-subrip")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and cd.endswith(".srt'") or cd.endswith(".srt"), cd
    assert "Ma%20video.srt" in cd or "Ma video.srt" in cd, cd
    assert "bonjour" in r.text


def test_le_sous_titre_d_une_playlist_est_servi_par_index(data_dir, harness,
                                                          fake_download,
                                                          fake_pipeline):
    job, plan, files = _playlist(data_dir, "s4", ["A", "B"])
    plan["subtitles"] = True
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    for i in (0, 1):
        r = TestClient(m.app).get(f"/api/jobs/{job['id']}/file/{i}/subtitle")
        assert r.status_code == 200, i
        assert r.headers["content-type"].startswith("application/x-subrip")
        assert r.text


def test_zip_de_playlist_ne_contient_qu_un_srt_par_entree(data_dir, harness,
                                                          fake_download,
                                                          monkeypatch):
    """`transcript_srt` et `subtitle` sont mutuellement exclusifs par le
    `elif` : même forcés tous les deux, un seul `<stem>.srt` par entrée."""
    job, plan, files = _playlist(data_dir, "s5", ["A", "B"])
    plan["subtitles"] = True

    def pipeline(src, outdir, denoise, input_sr, cutoff, on_stage,
                 output_format="wav", transcribe=False, cancel=None,
                 keep_original=True):
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        on_stage("Test…", 1.0)
        (out / "x_enhanced.wav").write_bytes(b"wav")
        (out / "x_pyclean-audio.mp3").write_bytes(b"mp3")
        (out / "t.srt").write_text("transcript", encoding="utf-8")
        return {"kind": "audio", "original_wav": None,
                "enhanced_wav": str(out / "x_enhanced.wav"),
                "enhanced_mp3": str(out / "x_pyclean-audio.mp3"),
                "transcript": None, "transcript_srt": str(out / "t.srt"),
                "duration": 1.0, "output": None}

    monkeypatch.setattr(m, "process_file", pipeline)
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    with zipfile.ZipFile(job["zip"]) as zf:
        srts = [n for n in zf.namelist() if n.endswith(".srt")]
    assert sorted(srts) == ["A.srt", "B.srt"], srts


# ---------------------------------------------------- annulation et échecs

def test_annulation_pendant_le_traitement_supprime_le_dossier(data_dir, harness,
                                                              fake_download,
                                                              monkeypatch):
    """Annuler **pendant** `process_file` (et pas pendant le téléchargement)."""
    job, plan = _single(data_dir, "c1")

    def pipeline(src, outdir, denoise, input_sr, cutoff, on_stage, **k):
        m.cancel_job(job["id"])            # le client annule pendant le traitement
        on_stage("Test…", 0.5)
        raise JobCancelled("annulé")

    monkeypatch.setattr(m, "process_file", pipeline)
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("cancelled", "done", "error")), job["state"]
    assert job["state"] == "cancelled"
    assert job["error"] is None
    assert wait_gone(data_dir / "c1")


def test_annulation_pendant_une_playlist_supprime_le_dossier(data_dir, harness,
                                                             fake_download,
                                                             fake_pipeline,
                                                             monkeypatch):
    """Annuler à la 2e entrée : `cancelled`, dossier purgé, aucune entrée
    marquée `done` après le point d'annulation."""
    job, plan, files = _playlist(data_dir, "c2", ["A", "B", "C"])

    def annuler(url, dest, fmt="mp4", subtitles=False, subtitle_lang="fr",
                on_stage=None, cancel=None, playlist_index=None):
        if playlist_index == 1:
            m.cancel_job(job["id"])
            if on_stage is not None:
                on_stage(0.5)
            raise JobCancelled("annulé")
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "source.m4a").write_bytes(b"media")
        return [downloader.Downloaded("A", dest / "source.m4a", None, 1.0)]

    monkeypatch.setattr(m.downloader, "download", annuler)
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("cancelled", "done", "error")), job["state"]
    assert job["state"] == "cancelled"
    assert files[0]["state"] == "done"
    assert files[1]["state"] != "done"
    assert files[2]["state"] != "done"
    assert not job.get("zip")
    assert wait_gone(data_dir / "c2")


def test_une_playlist_ou_rien_ne_reussit_echoue(data_dir, harness, monkeypatch):
    """Toutes les entrées en échec : le job est `error` (`no_file_done`), pas
    `done` avec un ZIP vide."""
    job, plan, files = _playlist(data_dir, "e1", ["A", "B"])

    monkeypatch.setattr(m.downloader, "download",
                        _boom(downloader.MediaError("download_failed",
                                                     detail="privée")))
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job["state"]
    assert job["state"] == "error"
    assert job["error_code"] == "no_file_done"
    assert all(f["state"] == "error" for f in files)
    assert all(f["error_code"] == "download_failed" for f in files)
    assert not job.get("zip")


def test_une_entree_en_echec_n_abat_pas_la_playlist(data_dir, harness,
                                                     fake_download, monkeypatch):
    job, plan, files = _playlist(data_dir, "e2", ["A", "B", "C"])
    seen = {}
    monkeypatch.setattr(m, "process_file", faux_pipeline(seen, echoue_au=2))
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job["state"]
    assert [f["state"] for f in files] == ["done", "error", "done"]
    assert job["state"] == "done"
    with zipfile.ZipFile(job["zip"]) as zf:
        names = sorted(zf.namelist())
    assert names == ["A_pyclean-audio.mp3", "C_pyclean-audio.mp3"], names


def test_echec_du_telechargement_unique_met_le_job_en_erreur(data_dir, harness,
                                                             monkeypatch):
    job, plan = _single(data_dir, "e3")

    monkeypatch.setattr(m.downloader, "download",
                        _boom(downloader.MediaError("download_failed",
                                                     detail="404")))
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job["state"]
    assert job["state"] == "error"
    assert job["error_code"] == "download_failed"
    assert job["error_params"]["detail"] == "404"


def test_l_annulation_l_emporte_sur_l_echec_du_telechargement(data_dir, harness,
                                                               monkeypatch):
    """Un `JobCancelled` levé par `downloader.download()` (le cas réel :
    l'event est positionné et yt-dlp lève une DownloadError que `_failed()`
    convertit) doit poser l'état `cancelled`, pas `error`."""
    job, plan = _single(data_dir, "c3")

    def boom(url, dest, fmt="mp4", subtitles=False, subtitle_lang="fr",
             on_stage=None, cancel=None, playlist_index=None):
        cancel.set()
        raise JobCancelled("Processing cancelled by the client.")

    monkeypatch.setattr(m.downloader, "download", boom)
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("cancelled", "done", "error")), job["state"]
    assert job["state"] == "cancelled"
    assert job["error"] is None
    assert wait_gone(data_dir / "c3")


# ------------------------------------------------------- formats mp3 / mp4

def test_mp3_ne_produit_aucun_fichier_video(data_dir, harness, fake_download,
                                             fake_pipeline):
    job, plan = _single(data_dir, "f1")
    plan["fmt"] = "mp3"
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert fake_download[0]["fmt"] == "mp3"
    assert "video" not in job["artifacts"]
    assert job["kind"] == "audio"
    assert not list((data_dir / "f1").rglob("*.mp4"))


def test_mp4_garde_la_video_dans_le_zip_de_playlist(data_dir, harness,
                                                    fake_download,
                                                    fake_pipeline):
    fake_pipeline["with_video"] = True
    job, plan, files = _playlist(data_dir, "f2", ["A", "B"])
    plan["fmt"] = "mp4"
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    with zipfile.ZipFile(job["zip"]) as zf:
        names = sorted(zf.namelist())
    assert "A_pyclean-audio.mp4" in names, names
    assert "A_pyclean-audio.mp3" in names, names


def test_le_mode_mp3_repose_entierement_sur_le_selecteur_yt_dlp(data_dir,
                                                                 harness,
                                                                 fake_download,
                                                                 fake_pipeline):
    """Le serveur ne filtre rien : c'est `bestaudio/ba` qui ne fournit jamais
    de piste vidéo, donc `process_file` ne produit pas de MP4. Le test fixe
    cette dépendance (un site qui rendrait une piste vidéo trahirait le
    selecteur, pas le code du serveur)."""
    seen = fake_pipeline
    job, plan, files = _playlist(data_dir, "f3", ["A", "B"])
    plan["fmt"] = "mp3"
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert [c["fmt"] for c in fake_download] == ["mp3", "mp3"]
    assert seen["output_format"] == ["mp3", "mp3"]
    with zipfile.ZipFile(job["zip"]) as zf:
        names = sorted(zf.namelist())
    assert names == ["A_pyclean-audio.mp3", "B_pyclean-audio.mp3"], names


def test_la_source_telechargee_est_supprimee_apres_traitement(data_dir, harness,
                                                              fake_download,
                                                              fake_pipeline):
    job, plan, files = _playlist(data_dir, "f4", ["A", "B"])
    _lancer(job, plan)
    harness[1]()
    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    for i in (0, 1):
        assert not (data_dir / "f4" / "src" / str(i)).exists()
    # le repertoire src lui-meme reste (vide) : sans conséquence, le dossier du
    # job est purgé avec lui
    assert list((data_dir / "f4" / "src").glob("*")) == []


# ------------------------------------------------- non-régression des uploads

def test_un_envoi_de_dossier_garde_son_srt_de_transcription(data_dir, fake_models,
                                                             monkeypatch):
    """Regression : le `elif arts.get("subtitle")` ajouté à
    `_build_folder_zip` ne doit pas empêcher le `.srt` de transcription."""
    from app.transcriber import Cue

    _enh, tra = fake_models
    tra.cues = [Cue(0.0, 0.5, "bonjour"), Cue(0.6, 1.0, "monde")]
    monkeypatch.setattr(m, "is_available", lambda: True)
    client = TestClient(m.app)
    r = client.post("/api/enhance_folder",
                    files=[("files", ("a.wav", petit_wav(), "audio/wav"))],
                    data={"transcribe": "true", "output_format": "mp3"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    fin = time.time() + 60
    snap = {}
    while time.time() < fin:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    assert snap["state"] == "done", snap.get("error")
    z = client.get(f"/api/jobs/{job_id}/zip")
    assert z.status_code == 200
    with zipfile.ZipFile(io.BytesIO(z.content)) as zf:
        names = sorted(zf.namelist())
    assert "a_pyclean-audio.mp3" in names, names
    assert "a_transcript.txt" in names, names
    assert "a.srt" in names, names          # le .srt de transcription, intact


def test_un_envoi_de_fichier_unique_garde_ses_artefacts(data_dir, fake_models):
    """Non-régression du chemin « fichier » : WAV + MP3 + original, pas de
    sous-titre de site."""
    client = TestClient(m.app)
    r = client.post("/api/enhance", files={"file": ("b.wav", petit_wav(),
                                                   "audio/wav")},
                    data={"transcribe": "false"})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    fin = time.time() + 60
    snap = {}
    while time.time() < fin:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    assert snap["state"] == "done", snap.get("error")
    assert "enhanced_wav" in snap["artifacts"]
    assert "enhanced_mp3" in snap["artifacts"]
    assert "original_wav" in snap["artifacts"]
    assert "subtitle" not in snap["artifacts"]


# ------------------------------------------------------------------ endpoint

def test_status_expose_le_telechargeur_avec_sa_version(data_dir):
    s = m.status()
    assert isinstance(s["downloader"]["available"], bool)
    assert s["downloader"]["version"] is None or isinstance(
        s["downloader"]["version"], str)


def test_artifact_keys_couvre_telechargements(data_dir):
    assert m.ARTIFACT_KEYS == frozenset(m.DOWNLOADS) - {"video"}
