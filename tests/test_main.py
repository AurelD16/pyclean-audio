"""API helpers: safe paths, progress, ZIP archive of the results."""

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import main as m
from app.cancel import JobCancelled


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirects data/jobs to a temporary directory and empties the registry."""
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
    """Security regression: a hostile name must never climb out of the tree."""
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
    m._stage(job, "Done", 1.0, done=True)
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
    """a.mp3 and a.mkv in the same folder: the second becomes a_2."""
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
    """Regression: the worker keeps mutating the job during JSON encoding. A
    shallow copy ('dict(job)') makes the request fail."""
    job = {"id": "s1", "state": "running", "artifacts": {"enhanced_wav": "/a.wav"}}
    snap = m._snapshot(job)
    job["artifacts"]["enhanced_mp3"] = "/a.mp3"
    job["artifacts"]["enhanced_wav"] = "/b.wav"
    # isolated copy, and only the useful information is exposed (the name)
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
    """No Event and no server path may leak to the client."""
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
    # the response must stay pure JSON
    assert json.loads(json.dumps(m._snapshot(job)))["files"][0]["relpath"] == "a.mp3"


# ------------------------------------------------------------------ retention

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
    """After a restart no job is in memory any more: they are all dead."""
    (data_dir / "vieux1").mkdir()
    (data_dir / "vieux2" / "out").mkdir(parents=True)
    (data_dir / "fichier.txt").write_text("kept")
    assert m._purge_orphans() == 2
    assert not (data_dir / "vieux1").exists()
    assert not (data_dir / "vieux2").exists()
    assert (data_dir / "fichier.txt").exists()


def test_make_room_evince_les_termines(data_dir, monkeypatch):
    """Before registering a new job, at least one free slot is needed."""
    monkeypatch.setattr(m, "JOB_MAX", 2)
    _register(data_dir, "v1")
    time.sleep(0.01)
    _register(data_dir, "v2")
    time.sleep(0.01)
    _register(data_dir, "v3")
    m._make_room()
    assert "v1" not in m.JOBS  # the oldest are evicted first
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
    raise_if_cancelled(ev)         # no-op while the event is unset
    ev.set()
    with pytest.raises(JobCancelled):
        raise_if_cancelled(ev)


def test_finish_cancelled_purge_le_dossier(data_dir):
    job = _register(data_dir, "c1", state="running")
    (data_dir / "c1" / "out").mkdir(parents=True)
    m._finish_cancelled(job)
    assert job["state"] == "cancelled"
    assert job["stage"] == "Cancelled"
    assert not (data_dir / "c1").exists()



# ------------------------------------------------------------------ file d'attente

@pytest.fixture
def small_queue(monkeypatch):
    """Local queue, no worker (no test starts one)."""
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
    # the refused job is purged: nothing left on disk
    assert "f3" not in m.JOBS
    assert not (data_dir / "f3").exists()


def test_worker_remonte_une_erreur_inattendue(data_dir, monkeypatch):
    """A job that crashes outside _run_job's try/except stays visible in the UI."""
    import threading

    class OneShotQueue:
        """Serves a single job then sends the worker's shutdown sentinel."""

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
        raise RuntimeError("maybe out of VRAM")

    monkeypatch.setattr(m, "QUEUE", OneShotQueue((boom, (1,), job)))
    t = threading.Thread(target=m._worker, daemon=True)
    t.start()  # the worker stops on the None sentinel (see OneShotQueue)
    t.join(timeout=5)
    assert not t.is_alive()
    assert job["state"] == "error"
    assert "VRAM" in job["error"]
    assert job["stage"] == "Error"


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
    assert job["state"] == "queued"  # the worker does not rewrite the job state


def test_cancel_refuse_un_job_termine(data_dir):
    from fastapi import HTTPException

    _register(data_dir, "c9")  # state "done" by default
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
                 "transcript", "transcript_srt", "subtitle"):
        assert name in m.DOWNLOADS
        assert m.DOWNLOADS[name][0].format(stem="x")


def test_sous_titre_telecharge_en_srt():
    """Le sous-titre de yt-dlp est servi en .srt, comme celui de Parakeet."""
    assert m.DOWNLOADS["subtitle"] == ("{stem}.srt", "application/x-subrip")
    assert m.DOWNLOADS["subtitle"][0].format(stem="Ma video") == "Ma video.srt"


def test_status_expose_file_et_retenue(data_dir):
    s = m.status()
    assert s["retention"]["ttl_s"] == m.JOB_TTL
    assert s["retention"]["max_jobs"] == m.JOB_MAX
    assert s["queue"]["max"] == m.QUEUE_MAX
    assert "waiting" in s["queue"]


def test_status_donne_la_disponibilite_de_nemo(data_dir):
    """The UI deduces it must disable the "Transcribe" checkbox."""
    s = m.status()
    assert isinstance(s["transcriber"]["available"], bool)


def test_transcribe_refuse_si_nemo_absent(data_dir, monkeypatch):
    """An explicit 400 beats a job failing on ModuleNotFound."""
    from fastapi import HTTPException

    monkeypatch.setattr(m, "is_available", lambda: False)
    with pytest.raises(HTTPException) as e:
        m._check_transcribe(True)
    assert e.value.status_code == 400
    assert "--asr" in e.value.detail
    m._check_transcribe(False)  # inactive: always accepted


# --------------------------------------------- queue -> _run_job chain


@pytest.fixture
def harness(monkeypatch):
    """Local queue + worker started by the test (no global thread)."""
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
    q.put_nowait(None)  # the worker's shutdown sentinel
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
    """Regression: the target must receive the job as its first argument,
    otherwise _run_job fails with a TypeError and the job errors out."""
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
    assert len(calls) == 1, "process_file was not called"
    assert str(calls[0]["up"]) == "/tmp/entree.mp3"  # normalised to a Path
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
    # in folder mode the original is not kept (saves disk)
    assert seen["keep"] == [False]
    assert entries[0]["state"] == "done"
    assert job["state"] == "done"
    assert job["zip"], "the folder's ZIP must be produced"
    with zipfile.ZipFile(job["zip"]) as zf:
        assert "a_pyclean-audio.mp3" in zf.namelist()


def test_worker_annule_un_job_en_attente(data_dir, harness):
    """A job cancelled before the worker reaches it processes nothing."""
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
    # the job's files are released (the state is set before the rmtree)
    end = time.time() + 5
    while (data_dir / "j3").exists() and time.time() < end:
        time.sleep(0.01)
    assert not (data_dir / "j3").exists()


def test_snapshot_zip_est_un_bouton(data_dir):
    """The archive path must not leak out of the server."""
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


# ------------------------------------------------------- translatable keys

def test_stage_expose_la_cle_et_ses_params(data_dir):
    """The UI translates `stage_key`/`stage_args`; `stage` stays the wording."""
    job = {"stage": "", "progress": 0.0, "state": "running"}
    m._stage(job, "Enhancing the audio (LavaSR v2)…", 0.5,
             key="enhance", args={})
    assert job["stage_key"] == "enhance"
    assert job["stage_args"] == {}
    assert job["stage"] == "Enhancing the audio (LavaSR v2)…"


def test_stage_sans_cle_laisse_le_repli(data_dir):
    """A caller that emits a text only (or an older version) stays displayable:
    the UI then falls back to `stage`."""
    job = {"stage": "", "progress": 0.0, "state": "running"}
    m._stage(job, "Unknown stage…", 0.5)
    assert job["stage_key"] is None
    assert job["stage_args"] == {}


def test_file_stage_emborique_l_etape_interne():
    text, key, params = m._file_stage(2, 12, "sous/a.mp3",
                                     "Enhancing the audio (LavaSR v2)…",
                                     "enhance", {})
    assert key == "file_step"
    assert text == "File 2/12 — sous/a.mp3: Enhancing the audio (LavaSR v2)…"
    # without inner_key the UI could not translate the nested stage
    assert params["inner_key"] == "enhance"
    assert params["index"] == 2 and params["total"] == 12


def test_fail_garde_le_code_et_les_params(data_dir):
    from app.messages import MediaError

    job = _register(data_dir, "e9", state="running")
    with m.JOBS_LOCK:
        m._fail(job, MediaError("no_audio_track"))
    assert job["state"] == "error"
    assert job["error"] == "No audio track found in the file."
    assert job["error_code"] == "no_audio_track"
    assert job["error_params"] == {}
    assert job["stage_key"] == "error"


def test_fail_sans_code_laisse_le_message_brut(data_dir):
    """A third-party exception (CUDA, a bug) has no key: the UI shows `error`
    as is rather than losing the information."""
    job = _register(data_dir, "e8", state="running")
    with m.JOBS_LOCK:
        m._fail(job, RuntimeError("maybe out of VRAM"))
    assert job["error_code"] is None
    assert "VRAM" in job["error"]


def test_api_error_porte_un_code_et_un_texte():
    err = m.ApiError(413, "file_too_large", name="a.wav")
    assert err.status_code == 413
    assert err.code == "file_too_large"
    assert err.params == {"name": "a.wav"}
    assert err.detail == "File too large (2 GB max): a.wav"


def test_reponse_d_erreur_expose_code_et_params():
    """FastAPI's default handler only serialises `detail`: without ours the UI
    would have nothing to translate."""
    from fastapi.testclient import TestClient

    # without `with`: the lifespan would preload LavaSR and NeMo (no test
    # loads a model)
    r = TestClient(m.app).get("/api/jobs/inexistant")
    assert r.status_code == 404
    body = r.json()
    assert body["detail"] == "Job not found"
    assert body["code"] == "job_not_found"
    assert "params" not in body          # no parameter, hence nothing to send


def test_reponse_d_erreur_parametree():
    from fastapi.testclient import TestClient

    r = TestClient(m.app).get("/api/jobs/inexistant/file/enhanced_wav")
    assert r.status_code == 404
    assert r.json()["code"] == "file_not_found"


# ------------------------------------------------------------- yt-dlp (download)

VIDEO_URL = "https://www.example.com/watch?v=abc"


@pytest.fixture
def no_dns(monkeypatch):
    """Aucun test ne résout un nom de domaine : la résolution est simulée."""
    monkeypatch.setattr(m, "_host_addresses", lambda host: [])


@pytest.fixture
def yt_ready(monkeypatch):
    """yt-dlp « installé » et une résolution d'un seul élément."""
    from app import downloader

    monkeypatch.setattr(downloader, "is_available", lambda: True)
    monkeypatch.setattr(downloader, "resolve",
                        lambda url: [{"id": "v1", "title": "Ma video",
                                      "duration": 30.0}])
    return downloader


def _post(**data):
    from fastapi.testclient import TestClient

    return TestClient(m.app).post("/api/download", data=data)


def _code(reponse):
    """Le code traduisible de la réponse (jamais une 500 sans corps)."""
    assert 400 <= reponse.status_code < 500, reponse.text
    body = reponse.json()
    assert body.get("code"), body
    return body["code"]


# --------------------------------------------------------------- URL (SSRF)

def test_url_invalide_refusee_avant_tout_appel(data_dir, no_dns, yt_ready):
    for mauvais in ("", "   ", "pas une url", "file:///etc/passwd",
                    "ftp://example.com/x", "javascript:alert(1)"):
        with pytest.raises(HTTPException) as exc:
            m._check_url(mauvais)
        assert exc.value.status_code == 400
        assert exc.value.code == "bad_url"


def test_url_privee_refusee(data_dir):
    """SSRF : le serveur ne doit jamais aller chercher une adresse interne."""
    for prive in ("http://127.0.0.1:8787/api/status",
                  "http://[::1]:8787/", "http://192.168.1.10/x",
                  "http://169.254.169.254/latest/meta-data",
                  "http://10.0.0.5/x", "http://0.0.0.0/"):
        with pytest.raises(HTTPException) as exc:
            m._check_url(prive)
        assert exc.value.status_code == 400, prive
        assert exc.value.code == "blocked_url", prive


def test_url_privee_refusee_apres_resolution(data_dir, monkeypatch):
    """Un nom public qui résout vers une adresse interne est refusé aussi."""
    import ipaddress

    monkeypatch.setattr(m, "_host_addresses",
                        lambda host: [ipaddress.ip_address("10.1.2.3")])
    with pytest.raises(HTTPException) as exc:
        m._check_url("https://interne.example.com/secret")
    assert exc.value.code == "blocked_url"


def test_url_publique_acceptee(data_dir, monkeypatch):
    import ipaddress

    monkeypatch.setattr(m, "_host_addresses",
                        lambda host: [ipaddress.ip_address("93.184.216.34")])
    m._check_url(VIDEO_URL)      # ne lève rien


def test_format_de_telechargement_refuse(data_dir):
    with pytest.raises(HTTPException) as exc:
        m._check_download_fmt("avi")
    assert exc.value.status_code == 400
    assert exc.value.code == "bad_download_format"
    m._check_download_fmt("mp3")
    m._check_download_fmt("mp4")


# -------------------------------------------------------------- POST /api/download

def test_api_download_refuse_si_ytdlp_absent(data_dir, no_dns, monkeypatch):
    monkeypatch.setattr(m.downloader, "is_available", lambda: False)
    assert _code(_post(url=VIDEO_URL)) == "ytdlp_unavailable"


def test_api_download_refuse_un_format_inconnu(data_dir, no_dns, yt_ready):
    assert _code(_post(url=VIDEO_URL, fmt="avi")) == "bad_download_format"


def test_api_download_refuse_une_url_invalide(data_dir, no_dns, yt_ready):
    assert _code(_post(url="ceci n'est pas une url")) == "bad_url"
    assert _code(_post(url="file:///etc/passwd")) == "bad_url"
    assert _code(_post(url="   ")) == "bad_url"


def test_api_download_refuse_une_url_privee(data_dir, yt_ready):
    assert _code(_post(url="http://127.0.0.1:8787/")) == "blocked_url"


def test_api_download_refuse_une_playlist_trop_grande(data_dir, no_dns,
                                                     monkeypatch, yt_ready):
    monkeypatch.setattr(m, "MAX_FOLDER_FILES", 2)
    monkeypatch.setattr(m.downloader, "resolve", lambda url: [
        {"id": f"v{i}", "title": f"Video {i}"} for i in range(3)])
    reponse = _post(url=VIDEO_URL)
    assert reponse.status_code == 413
    assert reponse.json()["code"] == "playlist_too_large"
    assert reponse.json()["params"]["entries"] == 3
    assert reponse.json()["params"]["max"] == 2


def test_api_download_refuse_un_volume_trop_gros(data_dir, no_dns,
                                                 monkeypatch, yt_ready):
    monkeypatch.setattr(m, "MAX_FOLDER_TOTAL", 100)
    monkeypatch.setattr(m.downloader, "resolve", lambda url: [
        {"id": "v1", "title": "Geante", "filesize": 5_000_000_000}])
    reponse = _post(url=VIDEO_URL)
    assert reponse.status_code == 413
    assert reponse.json()["code"] == "download_too_large"


def test_api_download_rend_le_poste_et_le_nombre_d_entrees(data_dir, no_dns,
                                                           small_queue, yt_ready):
    reponse = _post(url=VIDEO_URL)
    assert reponse.status_code == 200
    corps = reponse.json()
    assert set(corps) == {"job_id", "queue_position", "entries"}
    assert corps["entries"] == 1
    assert corps["queue_position"] == 0
    job = m.JOBS[corps["job_id"]]
    assert job["source"] == "ytdlp"
    assert job["filename"] == "Ma video"
    assert job["stem"] == "Ma video"
    # rien n'est écrit sur le disque avant que le worker ne démarre
    assert list(data_dir.iterdir()) == []


def test_api_download_ignore_transcribe(data_dir, no_dns, yt_ready):
    """Les sous-titres du site remplacent Parakeet : le paramètre n'existe pas."""
    schema = m.app.openapi()
    corps = schema["paths"]["/api/download"]["post"]["requestBody"]
    ref = corps["content"]["application/x-www-form-urlencoded"]["schema"]["$ref"]
    props = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
    assert {"url", "fmt", "subtitles", "subtitle_lang", "denoise",
            "input_sr", "cutoff"} <= set(props)
    assert "transcribe" not in props


def test_plan_sanitise_les_titres_et_suffixe_les_doublons(data_dir):
    """Deux vidéos de même titre ne doivent pas partager un nom de téléchargement."""
    plan = m._download_plan([
        {"id": "a", "title": "Concert/2024 (live)"},
        {"id": "b", "title": "Concert/2024 (live)"},
        {"id": "c", "title": ""},
    ])
    stems = [e["stem"] for e in plan]
    assert stems[0] == "Concert_2024 _live_"
    assert stems[1] == "Concert_2024 _live__2"
    assert len(set(stems)) == 3
    for e in plan:
        assert "/" not in e["stem"] and ".." not in e["stem"]


# ------------------------------------------------- _run_download_job (worker)

@pytest.fixture
def fake_download(monkeypatch):
    """yt-dlp-download(): écrit un fichier source et renvoie un Downloaded.

    `mp3`/`mp4` ne change rien ici : c'est le **choix de format** de yt-dlp qui
    décide si le fichier contient une piste vidéo, le faux se contente de créer
    ce que le pipeline attending.
    """
    from app.downloader import Downloaded

    calls = []

    def _download(url, dest, fmt="mp4", subtitles=False, subtitle_lang="fr",
                  on_stage=None, cancel=None, playlist_index=None):
        calls.append({"url": url, "fmt": fmt, "subtitles": subtitles,
                      "subtitle_lang": subtitle_lang, "cancel": cancel,
                      "playlist_index": playlist_index, "dest": str(dest)})
        if on_stage is not None:
            on_stage(0.5)
        dest.mkdir(parents=True, exist_ok=True)
        src = dest / f"source{playlist_index if playlist_index is not None else ''}.m4a"
        src.write_bytes(b"media")
        sub = None
        if subtitles:
            sub = dest / "source.fr.srt"
            sub.write_text("1\n00:00:00,000 --> 00:00:01,000\nbonjour\n",
                           encoding="utf-8")
        return [Downloaded(title="Ma video", path=src, subtitle=sub,
                           duration=30.0)]

    monkeypatch.setattr(m.downloader, "download", _download)
    return calls


@pytest.fixture
def fake_pipeline(monkeypatch):
    """process_file() sans modèle ni ffmpeg: on ne teste que le job."""
    seen = {}

    def _process_file(src, outdir, denoise, input_sr, cutoff, on_stage,
                      output_format="wav", transcribe=False, cancel=None,
                      keep_original=True):
        seen.setdefault("srcs", []).append(Path(src))
        seen.setdefault("keep", []).append(keep_original)
        seen.setdefault("transcribe", []).append(transcribe)
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        on_stage("Test…", 0.5)
        wav = out / "x_enhanced.wav"
        wav.write_bytes(b"wav")
        mp3 = out / "x_pyclean-audio.mp3"
        mp3.write_bytes(b"mp3")
        original = out / "x_original.wav"
        if keep_original:
            original.write_bytes(b"wav")
        video = None
        if seen.get("with_video"):
            video = out / "x_pyclean-audio.mp4"
            video.write_bytes(b"mp4")
        return {"kind": "video" if video else "audio",
                "original_wav": str(original) if keep_original else None,
                "enhanced_wav": str(wav), "enhanced_mp3": str(mp3),
                "transcript": None, "transcript_srt": None, "duration": 30.0,
                "output": str(video) if video else None}

    monkeypatch.setattr(m, "process_file", _process_file)
    return seen


def _download_job(data_dir, job_id, entries, **fields):
    job = _register(data_dir, job_id, state="queued", **fields)
    plan = {"url": VIDEO_URL, "fmt": "mp4", "subtitles": False,
            "subtitle_lang": "fr", "entries": entries}
    return job, plan


def test_run_download_job_video_unique(data_dir, harness, fake_download,
                                       fake_pipeline):
    """Une vidéo : la forme « fichier unique », A/B comprise."""
    job, plan = _download_job(
        data_dir, "d1", [{"id": "v1", "title": "Ma video", "stem": "Ma video"}],
        kind=None, source="ytdlp", stem="Ma video", filename="Ma video",
        artifacts={})
    m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                          job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    assert job["kind"] == "audio"
    # l'original est gardé (A/B), la transcription n'est jamais demandée
    assert fake_pipeline["keep"] == [True]
    assert fake_pipeline["transcribe"] == [False]
    assert "original_wav" in job["artifacts"]
    assert job["artifacts"]["enhanced_mp3"].endswith(".mp3")
    assert job["progress"] == 1.0
    assert fake_download[0]["playlist_index"] is None


def test_run_download_job_expose_le_sous_titre(data_dir, harness, fake_download,
                                               fake_pipeline):
    job, plan = _download_job(
        data_dir, "d2", [{"id": "v1", "title": "Ma video", "stem": "Ma video"}],
        kind=None, source="ytdlp", stem="Ma video", filename="Ma video",
        artifacts={})
    plan["subtitles"] = True
    m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                          job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["artifacts"]["subtitle"].endswith(".srt")
    # jamais de transcription Parakeet à côté
    assert "transcript" not in job["artifacts"]
    assert "transcript_srt" not in job["artifacts"]
    assert fake_download[0]["subtitles"] is True


def test_run_download_job_mp3_ne_produit_pas_de_video(data_dir, harness,
                                                      fake_download, fake_pipeline):
    """Mode MP3 : `bestaudio` ne fournit aucune piste vidéo, donc aucun
    artefact vidéo n'est produit (le faux pipeline le vérifie via `with_video`)."""
    job, plan = _download_job(
        data_dir, "d3", [{"id": "v1", "title": "Ma video", "stem": "Ma video"}],
        kind=None, source="ytdlp", stem="Ma video", filename="Ma video",
        artifacts={})
    plan["fmt"] = "mp3"
    m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                          job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert fake_download[0]["fmt"] == "mp3"
    assert "video" not in job["artifacts"]
    assert job["kind"] == "audio"


def test_run_download_job_playlist_devient_un_dossier(data_dir, harness,
                                                      fake_download,
                                                      fake_pipeline):
    entries = [{"index": i, "relpath": f"Video {i}", "stem": f"Video_{i}",
                "state": "queued", "stage": "", "stage_key": None,
                "stage_args": {}, "progress": 0.0, "error": None,
                "error_code": None, "error_params": {}, "kind": None,
                "artifacts": {}} for i in range(2)]
    job, plan = _download_job(data_dir, "d4",
                              [{"id": f"v{i}", "title": f"Video {i}",
                                "stem": f"Video_{i}"} for i in range(2)],
                              kind="folder", source="ytdlp", stem=None,
                              filename="2 files (playlist)", output_format="mp3",
                              files=entries)
    plan["subtitles"] = True
    m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                          job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("done", "error")), job.get("error")
    assert job["state"] == "done"
    assert job["kind"] == "folder"
    # une entrée de playlist à la fois, et l'original n'est pas gardé
    assert [c["playlist_index"] for c in fake_download] == [0, 1]
    assert fake_pipeline["keep"] == [False, False]
    assert all(ent["state"] == "done" for ent in entries)
    assert all(ent["artifacts"]["subtitle"].endswith(".srt") for ent in entries)
    assert job["zip"]
    with zipfile.ZipFile(job["zip"]) as zf:
        names = sorted(zf.namelist())
    # comme pour un envoi de dossier, le nom dans l'archive suit le titre
    assert "Video 0_pyclean-audio.mp3" in names, names
    assert "Video 0.srt" in names, names
    assert "Video 1.srt" in names, names
    # la source téléchargée est supprimée après le traitement
    assert not (data_dir / "d4" / "src" / "0").exists()
    assert not (data_dir / "d4" / "src" / "1").exists()


def test_run_download_job_signale_un_echec_par_entree(data_dir, harness,
                                                       fake_download,
                                                       fake_pipeline):
    """Un fichier en échec ne fait pas échouer le dossier entier."""
    boom = {"n": 0}

    def process(src, outdir, *a, **k):
        boom["n"] += 1
        if boom["n"] == 1:
            raise RuntimeError("pas de piste audio")
        out = Path(outdir)
        out.mkdir(parents=True, exist_ok=True)
        wav = out / "x_enhanced.wav"
        wav.write_bytes(b"wav")
        return {"kind": "audio", "original_wav": None,
                "enhanced_wav": str(wav), "enhanced_mp3": None,
                "transcript": None, "transcript_srt": None, "duration": 1.0,
                "output": None}

    m.process_file = process
    try:
        entries = [{"index": i, "relpath": f"Video {i}", "stem": f"Video_{i}",
                    "state": "queued", "stage": "", "stage_key": None,
                    "stage_args": {}, "progress": 0.0, "error": None,
                    "error_code": None, "error_params": {}, "kind": None,
                    "artifacts": {}} for i in range(2)]
        job, plan = _download_job(
            data_dir, "d5",
            [{"id": f"v{i}", "title": f"Video {i}", "stem": f"Video_{i}"}
             for i in range(2)],
            kind="folder", source="ytdlp", stem=None, output_format="mp3",
            files=entries)
        m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                              job["_cancel"]))
        harness[1]()

        assert wait_state(job, ("done", "error")), job.get("error")
        assert entries[0]["state"] == "error"
        assert entries[1]["state"] == "done"
        assert job["state"] == "done"
    finally:
        del m.process_file


def test_run_download_job_annule_supprime_le_dossier(data_dir, harness,
                                                     fake_download,
                                                     fake_pipeline):
    """Annuler pendant le téléchargement : état « cancelled », dossier purgé."""
    def annuler(url, dest, *a, on_stage=None, **k):
        m.cancel_job(job["id"])
        if on_stage is not None:
            on_stage(0.5)
        raise JobCancelled("annulé")

    m.downloader.download = annuler
    job, plan = _download_job(
        data_dir, "d6", [{"id": "v1", "title": "Ma video", "stem": "Ma video"}],
        kind=None, source="ytdlp", stem="Ma video", filename="Ma video",
        artifacts={})
    m._enqueue(job, m._run_download_job, (plan, False, 16000, None,
                                          job["_cancel"]))
    harness[1]()

    assert wait_state(job, ("cancelled", "done", "error")), job["state"]
    assert job["state"] == "cancelled"
    # l'état est écrit avant le rmtree (voir _finish_cancelled)
    end = time.time() + 5
    while (data_dir / "d6").exists() and time.time() < end:
        time.sleep(0.01)
    assert not (data_dir / "d6").exists()


def test_status_expose_le_telechargeur(data_dir):
    s = m.status()
    assert isinstance(s["downloader"]["available"], bool)
    assert s["downloader"]["version"] is None or isinstance(
        s["downloader"]["version"], str)
