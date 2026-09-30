"""Aide de l'API : chemins sûrs, progression, archive ZIP des résultats."""

import json
import time
import zipfile
from pathlib import Path

import pytest

from app import main as m


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirige data/jobs vers un répertoire temporaire et vide le registre."""
    d = tmp_path / "jobs"
    d.mkdir()
    monkeypatch.setattr(m, "DATA", d)
    with m.JOBS_LOCK:
        m.JOBS.clear()
    yield d
    with m.JOBS_LOCK:
        m.JOBS.clear()


# --------------------------------------------------------------- _safe_relpath

def test_relpath_simple():
    assert m._safe_relpath("a.mp3") == Path("a.mp3")
    assert m._safe_relpath("dossier/sous/b.mp4") == Path("dossier/sous/b.mp4")


def test_relpath_ne_traverse_pas_hors_du_arborescence():
    """Régression sécurité : un nom hostile ne doit jamais remonter la hiérarchie."""
    for hostile in ("../../etc/passwd", "..\\..\\win.ini", "/etc/shadow",
                    "dossier/../../../x.mp3", "a/../../b.mp3"):
        rel = m._safe_relpath(hostile)
        assert ".." not in rel.parts
        assert not rel.is_absolute()
        assert ".." not in str(rel)


def test_relpath_normalise_separateurs_et_points():
    assert m._safe_relpath("dossier\\sous\\b.mp4") == Path("dossier/sous/b.mp4")
    assert m._safe_relpath("./dossier//b.mp4") == Path("dossier/b.mp4")
    assert m._safe_relpath("/dossier/b.mp4") == Path("dossier/b.mp4")


def test_relpath_vide_ou_invalide():
    for vide in (None, "", "   ".strip(), "/", "..", "./", "../.."):
        assert m._safe_relpath(vide) is None


# --------------------------------------------------------------------- _stage

def test_stage_borne_la_progression():
    job = {"stage": "", "progress": 0.0, "state": "running"}
    m._stage(job, "test", 1.8)
    assert job["progress"] == 1.0
    m._stage(job, "test", -0.5)
    assert job["progress"] == 0.0
    m._stage(job, "test", 0.45678)
    assert job["progress"] == 0.457
    m._stage(job, "test", 0.5)
    assert job["progress"] == 0.5


def test_stage_done_bascule_l_etat():
    job = {"stage": "", "progress": 0.0, "state": "running"}
    m._stage(job, "Terminé", 1.0, done=True)
    assert job["state"] == "done"


# --------------------------------------------------------- _build_folder_zip

def _entry(root: Path, rel: str, *, mp3=True, wav=True, video=False, text=False):
    art = {}
    if wav:
        p = root / f"{Path(rel).stem}_enhanced.wav"
        p.write_bytes(b"wav")
        art["enhanced_wav"] = str(p)
    if mp3:
        p = root / f"{Path(rel).stem}_pyclean-audio.mp3"
        p.write_bytes(b"mp3")
        art["enhanced_mp3"] = str(p)
    if video:
        p = root / f"{Path(rel).stem}_pyclean-audio.mp4"
        p.write_bytes(b"mp4")
        art["video"] = str(p)
    if text:
        p = root / f"{Path(rel).stem}_transcript.txt"
        p.write_text("texte", encoding="utf-8")
        art["transcript"] = str(p)
    return {"relpath": rel, "stem": Path(rel).stem, "state": "done",
            "artifacts": art}


def test_zip_mp3_conserve_arborescence(data_dir):
    job = {"id": "abc123"}
    out = data_dir / "abc123" / "out"
    out.mkdir(parents=True)
    entries = [_entry(out, "racine.mp3"),
              _entry(out, "sous/enfant.mp4", video=True, text=True)]
    zip_path = m._build_folder_zip(job, entries, "mp3")

    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    assert names == [
        "racine_pyclean-audio.mp3",
        "sous/enfant_pyclean-audio.mp3",
        "sous/enfant_pyclean-audio.mp4",
        "sous/enfant_transcript.txt",
    ]


def test_zip_wav_choisit_le_bon_format(data_dir):
    job = {"id": "w1"}
    out = data_dir / "w1" / "out"
    out.mkdir(parents=True)
    zip_path = m._build_folder_zip(job, [_entry(out, "a.mp3")], "wav")
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["a_pyclean-audio.wav"]


def test_zip_suffixe_les_stems_en_collision(data_dir):
    """a.mp3 et a.mkv dans le même dossier : le second devient a_2."""
    job = {"id": "c1"}
    out = data_dir / "c1" / "out"
    out.mkdir(parents=True)
    entries = [_entry(out, "a.mp3"), _entry(out, "a.mkv")]
    zip_path = m._build_folder_zip(job, entries, "mp3")
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
    assert names == ["a_2_pyclean-audio.mp3", "a_pyclean-audio.mp3"]


def test_zip_ignore_les_fichiers_en_echec(data_dir):
    job = {"id": "e1"}
    out = data_dir / "e1" / "out"
    out.mkdir(parents=True)
    ok = _entry(out, "bon.mp3")
    ko = _entry(out, "mauvais.mp3")
    ko["state"] = "error"
    zip_path = m._build_folder_zip(job, [ok, ko], "mp3")
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["bon_pyclean-audio.mp3"]


def test_zip_absent_si_aucun_succes(data_dir):
    job = {"id": "z1"}
    out = data_dir / "z1" / "out"
    out.mkdir(parents=True)
    ko = _entry(out, "mauvais.mp3")
    ko["state"] = "error"
    assert m._build_folder_zip(job, [ko], "mp3") is None
    assert not (data_dir / "z1" / "results.zip").exists()


# ------------------------------------------------------------------ snapshot

def test_snapshot_isole_le_job_vivant(data_dir):
    """Régression : le worker continue de muter le job pendant l'encodage JSON.
    Une copie superficielle ('dict(job)') fait échouer la requête."""
    job = {"id": "s1", "state": "running", "artifacts": {"enhanced_wav": "/a.wav"}}
    snap = m._snapshot(job)
    job["artifacts"]["enhanced_mp3"] = "/a.mp3"
    job["artifacts"]["enhanced_wav"] = "/b.wav"
    # copie isolée, et seule l'information utile est exposée (le nom)
    assert snap["artifacts"] == {"enhanced_wav": "a.wav"}


def test_snapshot_ne_divulgue_pas_les_chemins_serveur(data_dir):
    job = {"id": "s5", "state": "done",
           "artifacts": {"enhanced_wav": "/srv/data/jobs/s5/out/x_enhanced.wav"},
           "files": [{"relpath": "a.mp3",
                      "artifacts": {"video": "/srv/data/jobs/s5/out/x.mp4"}}]}
    snap = m._snapshot(job)
    assert "/srv/" not in json.dumps(snap)
    assert snap["artifacts"]["enhanced_wav"] == "x_enhanced.wav"
    assert snap["files"][0]["artifacts"]["video"] == "x.mp4"


def test_snapshot_isole_les_entrees_dossier(data_dir):
    job = {"id": "s2", "state": "running", "artifacts": {},
           "files": [{"relpath": "a.mp3", "state": "done",
                      "artifacts": {"enhanced_wav": "/a.wav"}}]}
    snap = m._snapshot(job)
    job["files"][0]["artifacts"]["enhanced_mp3"] = "/a.mp3"
    assert snap["files"][0]["artifacts"] == {"enhanced_wav": "a.wav"}


def test_snapshot_cache_les_cles_internes(data_dir):
    """Aucun Event, aucun chemin serveur ne doit fuiter vers le client."""
    import threading

    job = {"id": "s3", "state": "queued", "artifacts": {},
           "_cancel": threading.Event(),
           "files": [{"relpath": "a.mp3", "artifacts": {},
                      "_src": "/data/jobs/s3/uploads/a.mp3",
                      "_outdir": "/data/jobs/s3/out"}]}
    snap = m._snapshot(job)
    assert "_cancel" not in snap
    assert "_src" not in snap["files"][0]
    assert "_outdir" not in snap["files"][0]


def test_snapshot_reste_serialisable(data_dir):
    import json

    job = {"id": "s4", "state": "done", "artifacts": {"enhanced_wav": "/a.wav"},
           "files": [{"relpath": "a.mp3", "artifacts": {"video": "/a.mp4"}}]}
    # la réponse doit rester du JSON pur
    assert json.loads(json.dumps(m._snapshot(job)))["files"][0]["relpath"] == "a.mp3"


# ------------------------------------------------------------------ rétention

def _register(data_dir, job_id, **fields):
    job = {"id": job_id, "state": "done", "stage": "", "progress": 1.0,
           "error": None, "artifacts": {}, **fields}
    (data_dir / job_id).mkdir(parents=True, exist_ok=True)
    return m._register(job)


def test_register_pose_une_expiration(data_dir):
    job = _register(data_dir, "r1")
    assert job["expires_at"] == pytest.approx(job["created"] + m.JOB_TTL)
    assert m.JOBS["r1"] is job


def test_purge_expire_les_jobs_termines(data_dir):
    job = _register(data_dir, "p1")
    (data_dir / "p1" / "out").mkdir(parents=True)
    job["expires_at"] = time.time() - 1
    assert m._purge_expired() == 1
    assert "p1" not in m.JOBS
    assert not (data_dir / "p1").exists()


def test_purge_ignore_un_job_en_cours(data_dir):
    job = _register(data_dir, "p2")
    job["state"] = "running"
    job["expires_at"] = time.time() - 1
    assert m._purge_expired() == 0
    assert "p2" in m.JOBS
    assert (data_dir / "p2").exists()


def test_purge_ignore_un_job_non_expire(data_dir):
    _register(data_dir, "p3")
    assert m._purge_expired() == 0
    assert "p3" in m.JOBS


def test_purge_orphans_au_demarrage(data_dir):
    """Après un redémarrage, plus aucun job n'est en mémoire : tout est mort."""
    (data_dir / "vieux1").mkdir()
    (data_dir / "vieux2" / "out").mkdir(parents=True)
    (data_dir / "fichier.txt").write_text("gardé")
    assert m._purge_orphans() == 2
    assert not (data_dir / "vieux1").exists()
    assert not (data_dir / "vieux2").exists()
    assert (data_dir / "fichier.txt").exists()


def test_make_room_evince_les_termines(data_dir, monkeypatch):
    """Avant d'enregistrer un nouveau job, il faut au moins une place libre."""
    monkeypatch.setattr(m, "JOB_MAX", 2)
    _register(data_dir, "v1")
    time.sleep(0.01)
    _register(data_dir, "v2")
    time.sleep(0.01)
    _register(data_dir, "v3")
    m._make_room()
    assert "v1" not in m.JOBS  # les plus anciens sont évincés en premier
    assert "v2" not in m.JOBS
    assert "v3" in m.JOBS
    assert len(m.JOBS) < m.JOB_MAX  # de la place pour le prochain


def test_make_room_refuse_que_si_tout_est_actif(data_dir, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(m, "JOB_MAX", 1)
    job = _register(data_dir, "w1")
    job["state"] = "running"
    with pytest.raises(HTTPException) as exc:
        m._make_room()
    assert exc.value.status_code == 503
    assert "w1" in m.JOBS


# --------------------------------------------------------------- annulation

def test_cancel_leve_jobcancelled():
    import threading

    from app.cancel import JobCancelled, raise_if_cancelled

    ev = threading.Event()
    raise_if_cancelled(None)       # no-op sans event
    raise_if_cancelled(ev)         # no-op si l'event n'est pas posé
    ev.set()
    with pytest.raises(JobCancelled):
        raise_if_cancelled(ev)


def test_finish_cancelled_purge_le_dossier(data_dir):
    job = _register(data_dir, "c1", state="running")
    (data_dir / "c1" / "out").mkdir(parents=True)
    m._finish_cancelled(job)
    assert job["state"] == "cancelled"
    assert job["stage"] == "Annulé"
    assert not (data_dir / "c1").exists()



# ------------------------------------------------------------------ file d'attente

@pytest.fixture
def small_queue(monkeypatch):
    """File d'attente locale, sans worker (les tests n'en lancent pas)."""
    import queue as _queue

    q = _queue.Queue(maxsize=2)
    monkeypatch.setattr(m, "QUEUE", q)
    monkeypatch.setattr(m, "WORKER_STARTED", True)  # pas de thread consumeur
    return q


def test_enqueue_place_et_positionne(data_dir, small_queue):
    job = _register(data_dir, "q1")
    pos = m._enqueue(job, lambda *a: None, ("x",))
    assert small_queue.qsize() == 1
    assert pos == 0
    assert job["queue_position"] == 0


def test_enqueue_position_croissant(data_dir, small_queue):
    for i in range(2):
        m._enqueue(_register(data_dir, f"p{i}"), lambda *a: None, ())
    assert small_queue.qsize() == 2
    assert m.JOBS["p0"]["queue_position"] == 0
    assert m.JOBS["p1"]["queue_position"] == 1


def test_enqueue_refuse_si_file_pleine(data_dir, small_queue):
    from fastapi import HTTPException

    for i in range(2):
        m._enqueue(_register(data_dir, f"f{i}"), lambda *a: None, ())
    job = _register(data_dir, "f3")
    with pytest.raises(HTTPException) as exc:
        m._enqueue(job, lambda *a: None, ())
    assert exc.value.status_code == 429
    # le job refusé est purgé : pas de résidu sur disque
    assert "f3" not in m.JOBS
    assert not (data_dir / "f3").exists()


def test_worker_remonte_une_erreur_inattendue(data_dir, monkeypatch):
    """Un job qui plante hors du try/except de _run_job reste visible dans l'UI."""
    import threading

    class OneShotQueue:
        """Sert un unique job puis envoie le sentinelle d'arrêt du worker."""

        def __init__(self, item):
            self.item = item
            self.calls = 0

        def get(self):
            self.calls += 1
            return self.item if self.calls == 1 else None

        def qsize(self):
            return self.real.qsize()

    job = _register(data_dir, "w9", state="queued")

    def boom(_a):
        raise RuntimeError("peut-être plus de VRAM")

    monkeypatch.setattr(m, "QUEUE", OneShotQueue((boom, (1,), job)))
    t = threading.Thread(target=m._worker, daemon=True)
    t.start()  # le worker s'arrête sur la sentinelle None (voir OneShotQueue)
    t.join(timeout=5)
    assert not t.is_alive()
    assert job["state"] == "error"
    assert "VRAM" in job["error"]
    assert job["stage"] == "Erreur"


def test_worker_laisse_passer_un_job_reussi(data_dir, monkeypatch):
    import threading

    class OneShotQueue:
        def __init__(self, item):
            self.item = item
            self.calls = 0

        def get(self):
            self.calls += 1
            return self.item if self.calls == 1 else None

        def qsize(self):
            return self.real.qsize()

    job = _register(data_dir, "w8", state="queued")
    seen = []
    monkeypatch.setattr(m, "QUEUE", OneShotQueue((seen.append, (7,), job)))
    t = threading.Thread(target=m._worker, daemon=True)
    t.start()
    t.join(timeout=5)
    assert seen == [7]
    assert job["state"] == "queued"  # le worker ne réécrit pas l'état du job


def test_cancel_refuse_un_job_termine(data_dir):
    from fastapi import HTTPException

    _register(data_dir, "c9")  # état "done" par défaut
    with pytest.raises(HTTPException) as exc:
        m.cancel_job("c9")
    assert exc.value.status_code == 409


def test_cancel_posed_event(data_dir):
    job = _register(data_dir, "c8", state="running")
    m.cancel_job("c8")
    assert job["_cancel"].is_set()


def test_cancel_job_inconnu(data_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        m.cancel_job("inexistant")
    assert exc.value.status_code == 404


def test_delete_refuse_un_job_en_cours(data_dir):
    from fastapi import HTTPException

    _register(data_dir, "d8", state="running")
    with pytest.raises(HTTPException) as exc:
        m.delete_job("d8")
    assert exc.value.status_code == 409
    assert "d8" in m.JOBS


def test_delete_supprime_job_et_fichiers(data_dir):
    _register(data_dir, "d9", state="done")
    (data_dir / "d9" / "out").mkdir(parents=True)
    (data_dir / "d9" / "out" / "x.wav").write_bytes(b"wav")
    m.delete_job("d9")
    assert "d9" not in m.JOBS
    assert not (data_dir / "d9").exists()


def test_delete_job_inconnu(data_dir):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        m.delete_job("inexistant")
    assert exc.value.status_code == 404


def test_downloads_couvre_les_artefacts():
    for name in ("original_wav", "enhanced_wav", "enhanced_mp3", "video",
                 "transcript", "transcript_srt"):
        assert name in m.DOWNLOADS
        assert m.DOWNLOADS[name][0].format(stem="x")


def test_status_expose_file_et_retenue(data_dir):
    s = m.status()
    assert s["retention"]["ttl_s"] == m.JOB_TTL
    assert s["retention"]["max_jobs"] == m.JOB_MAX
    assert s["queue"]["max"] == m.QUEUE_MAX
    assert "waiting" in s["queue"]


def test_status_donne_la_disponibilite_de_nemo(data_dir):
    """L'interface en déduit qu'il faut désactiver la case « Transcrire »."""
    s = m.status()
    assert isinstance(s["transcriber"]["available"], bool)


def test_transcribe_refuse_si_nemo_absent(data_dir, monkeypatch):
    """Mieux vaut un 400 explicite qu'un job qui échoue sur ModuleNotFound."""
    from fastapi import HTTPException

    monkeypatch.setattr(m, "is_available", lambda: False)
    with pytest.raises(HTTPException) as e:
        m._check_transcribe(True)
    assert e.value.status_code == 400
    assert "--asr" in e.value.detail
    m._check_transcribe(False)  # inactif : toujours accepté


# --------------------------------------------- enchaînement file -> _run_job


@pytest.fixture
def harness(monkeypatch):
    """File locale + worker lancé par le test (aucun thread global)."""
    import queue as _queue
    import threading as _th

    q = _queue.Queue(maxsize=10)
    monkeypatch.setattr(m, "QUEUE", q)
    monkeypatch.setattr(m, "WORKER_STARTED", True)  # _enqueue ne lance rien
    started = []

    def start():
        t = _th.Thread(target=m._worker, daemon=True)
        t.start()
        started.append(t)

    yield q, start
    q.put_nowait(None)  # sentinelle d'arrêt du worker
    for t in started:
        t.join(timeout=5)


def wait_state(job, states, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if job["state"] in states:
            return True
        time.sleep(0.01)
    return False


def test_enqueue_puis_worker_execute_le_vrai_run_job(data_dir, harness, monkeypatch):
    """Régression : la cible doit recevoir le job en premier argument, sinon
    _run_job échoue sur une TypeError et le job part en erreur."""
    calls = []

    def fake_process_file(up, outdir, denoise, input_sr, cutoff, on_stage,
                          output_format="wav", transcribe=False, cancel=None,
                          keep_original=True):
        calls.append({"up": up, "output_format": output_format,
                      "transcribe": transcribe, "keep_original": keep_original,
                      "cancel": cancel})
        on_stage("Test…", 0.5)
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.wav").write_bytes(b"wav")
        return {"kind": "audio", "original_wav": str(out / "o.wav"),
                "enhanced_wav": str(out / "x.wav"), "enhanced_mp3": None,
                "transcript": None, "transcript_srt": None, "duration": 1.0,
                "output": None}

    monkeypatch.setattr(m, "process_file", fake_process_file)
    job = _register(data_dir, "j1", state="queued")
    m._enqueue(job, m._run_job, ("/tmp/entree.mp3", False, 16000, None, False,
                                 job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert len(calls) == 1, "process_file n'a pas été appelé"
    assert str(calls[0]["up"]) == "/tmp/entree.mp3"  # normalisé en Path
    assert calls[0]["cancel"] is job["_cancel"]
    assert job["state"] == "done"
    assert job["artifacts"]["enhanced_wav"].endswith("x.wav")
    assert job["progress"] == 1.0


def test_enqueue_puis_worker_execute_le_vrai_run_folder_job(data_dir, harness, monkeypatch):
    import zipfile

    seen = {}

    def fake_process_file(src, outdir, denoise, input_sr, cutoff, on_stage,
                          output_format="wav", transcribe=False, cancel=None,
                          keep_original=True):
        seen.setdefault("srcs", []).append(str(src))
        seen.setdefault("keep", []).append(keep_original)
        seen.setdefault("fmt", []).append(output_format)
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "x.wav").write_bytes(b"wav")
        (out / "x.mp3").write_bytes(b"mp3")
        on_stage("Test…", 1.0)
        return {"kind": "audio", "original_wav": None,
                "enhanced_wav": str(out / "x.wav"),
                "enhanced_mp3": str(out / "x.mp3"), "transcript": None,
                "transcript_srt": None, "duration": 1.0, "output": None}

    monkeypatch.setattr(m, "process_file", fake_process_file)
    entries = [{"relpath": "a.mp3", "stem": "a", "state": "queued",
                "stage": "", "progress": 0.0, "error": None, "kind": None,
                "artifacts": {}}]
    job = _register(data_dir, "j2", state="queued", kind="folder",
                    output_format="mp3", files=entries)
    sources = [("/tmp/uploads/a.mp3", str(data_dir / "j2" / "out"))]
    m._enqueue(job, m._run_folder_job,
               (entries, sources, False, 16000, None, "mp3", False,
                job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert seen["srcs"] == ["/tmp/uploads/a.mp3"]
    # en mode dossier l'original n'est pas conservé (gain disque)
    assert seen["keep"] == [False]
    assert entries[0]["state"] == "done"
    assert job["state"] == "done"
    assert job["zip"], "le ZIP du dossier doit être produit"
    with zipfile.ZipFile(job["zip"]) as zf:
        assert "a_pyclean-audio.mp3" in zf.namelist()


def test_worker_annule_un_job_en_attente(data_dir, harness):
    """Un job annulé avant son passage dans le worker ne traite rien."""
    entries = [{"relpath": "a.mp3", "stem": "a", "state": "queued",
                "stage": "", "progress": 0.0, "error": None, "kind": None,
                "artifacts": {}}]
    job = _register(data_dir, "j3", state="queued", kind="folder", files=entries)
    m._enqueue(job, m._run_folder_job,
               (entries, [("/tmp/uploads/a.mp3", str(data_dir / "j3" / "out"))],
                False, 16000, None, "mp3", False, job["_cancel"]))
    m.cancel_job("j3")
    harness[1]()

    assert wait_state(job, ("cancelled", "done", "error")), job["state"]
    assert job["state"] == "cancelled"
    # les fichiers du job sont libérés (l'état est posé avant le rmtree)
    end = time.time() + 5
    while (data_dir / "j3").exists() and time.time() < end:
        time.sleep(0.01)
    assert not (data_dir / "j3").exists()


def test_snapshot_zip_est_un_bouton(data_dir):
    """Le chemin de l'archive ne doit pas sortir du serveur."""
    job = {"id": "z9", "state": "done", "artifacts": {},
           "zip": "/srv/data/jobs/z9/results.zip"}
    snap = m._snapshot(job)
    assert snap["zip"] is True
    assert "/srv/" not in json.dumps(snap)


def test_snapshot_ignore_les_artefacts_absents(data_dir):
    job = {"id": "n1", "state": "done",
           "artifacts": {"original_wav": None, "enhanced_wav": "/srv/x.wav"}}
    snap = m._snapshot(job)
    assert snap["artifacts"] == {"enhanced_wav": "x.wav"}


def test_artifact_keys_couvre_les_telechargements():
    assert m.ARTIFACT_KEYS == frozenset(m.DOWNLOADS) - {"video"}
