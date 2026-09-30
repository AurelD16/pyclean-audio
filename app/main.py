import asyncio
import contextlib
import queue
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .cancel import JobCancelled, raise_if_cancelled
from .config import (
    JOB_MAX,
    JOB_TTL,
    MAX_FOLDER_FILES,
    MAX_FOLDER_TOTAL,
    MAX_SIZE,
    PRELOAD_ASR,
    PURGE_INTERVAL,
    QUEUE_MAX,
)
from .enhancer import get_enhancer
from .processor import ALLOWED_EXT, process_file
from .transcriber import get_transcriber, is_available

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()

# Un seul traitement à la fois : un worker unique consomme la file, donc
# l'inférence et les phases ffmpeg d'un job n'ont jamais lieu en parallèle
# (plusieurs ffmpeg ne feraient de toute façon que se disputer le disque).
QUEUE = queue.Queue(maxsize=QUEUE_MAX)
WORKER_LOCK = threading.Lock()
WORKER_STARTED = False

ACTIVE_STATES = ("queued", "running")

# clés de process_file() qui deviennent des artefacts téléchargeables
ARTIFACT_KEYS = frozenset({
    "original_wav", "enhanced_wav", "enhanced_mp3", "transcript", "transcript_srt",
})


def _stage(job, stage, progress, done=False):
    with JOBS_LOCK:
        job["stage"] = stage
        job["progress"] = round(min(max(progress, 0.0), 1.0), 3)
        if done:
            job["state"] = "done"


def _public_artifacts(artifacts):
    """Ne renvoie que les artefacts réellement produits, avec leur nom de
    fichier : le client construit ses URLs à partir de la clé, le chemin
    serveur n'a rien à faire dans la réponse."""
    return {k: Path(v).name for k, v in (artifacts or {}).items() if v}


def _snapshot(job):
    """Copie du job sûre à sérialiser.

    Le thread de traitement continue de muter `job` pendant que FastAPI encode
    la réponse : sans copie des dictionnaires imbriqués, un `artifacts` qui
    grandit au milieu de l'encodage fait échouer la requête
    (« dictionary changed size during iteration »). Les clés internes
    (`_cancel`, `_src`…) et les chemins serveur ne sont jamais exposés.
    """
    snap = {k: v for k, v in job.items() if not k.startswith("_")}
    snap["artifacts"] = _public_artifacts(job.get("artifacts"))
    if "zip" in snap:
        # le client construit l'URL à partir de l'id : pas besoin du chemin
        snap["zip"] = bool(snap["zip"])
    files = job.get("files")
    if files is not None:
        out = []
        for ent in files:
            e = {k: v for k, v in ent.items() if not k.startswith("_")}
            e["artifacts"] = _public_artifacts(ent.get("artifacts"))
            out.append(e)
        snap["files"] = out
    return snap


# ---------------------------------------------------------------- rétention

def _drop_job(job_id):
    """Oublie un job et supprime tous ses fichiers. Vrai si le job existait."""
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if job is None:
        return False
    shutil.rmtree(DATA / job_id, ignore_errors=True)
    return True


def _purge_expired():
    """Supprime les jobs terminés dont le TTL est écoulé (jamais ceux en cours)."""
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if j.get("state") not in ACTIVE_STATES
                 and j.get("expires_at", 0) <= now]
    return sum(1 for jid in stale if _drop_job(jid))


def _purge_orphans():
    """Au démarrage, le registre est vide : tout ce qui reste sur disque est
    injoignable (l'API répondrait 404). On libère immédiatement."""
    n = 0
    if not DATA.exists():
        return 0
    for d in DATA.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def _make_room():
    """Garde le registre sous JOB_MAX en écartant les jobs terminés les plus
    anciens ; 503 seulement si la file est entièrement composée de jobs actifs."""
    with JOBS_LOCK:
        if len(JOBS) < JOB_MAX:
            return
        victims = sorted((j for j in JOBS.values()
                          if j.get("state") not in ACTIVE_STATES),
                         key=lambda j: j.get("created", 0))
        surplus = len(JOBS) - JOB_MAX + 1
    for job in victims[:surplus]:
        _drop_job(job["id"])
    with JOBS_LOCK:
        if len(JOBS) >= JOB_MAX:
            raise HTTPException(
                503, f"Trop de traitements en cours ou en attente (max {JOB_MAX}).")


def _register(job):
    with JOBS_LOCK:
        job["created"] = time.time()
        job["expires_at"] = job["created"] + JOB_TTL
        job["_cancel"] = threading.Event()
        JOBS[job["id"]] = job
    return job


def _finish_cancelled(job):
    with JOBS_LOCK:
        job["state"] = "cancelled"
        job["stage"] = "Annulé"
        job["error"] = None
    shutil.rmtree(DATA / job["id"], ignore_errors=True)


def _worker():
    """Consommateur unique de la file : un traitement GPU à la fois.

    Un `None` dans la file arrête le worker (arrêt du serveur, tests).
    """
    while True:
        item = QUEUE.get()
        if item is None:
            return
        target, args, job = item
        try:
            target(*args)
        except Exception as e:  # filet de sécurité : un job qui plante ne doit
            # pas disparaître silencieusement de l'interface
            with JOBS_LOCK:
                if job["state"] in ACTIVE_STATES:
                    job["state"] = "error"
                    job["error"] = str(e)
                    job["stage"] = "Erreur"


def _start_worker():
    global WORKER_STARTED
    with WORKER_LOCK:
        if not WORKER_STARTED:
            threading.Thread(target=_worker, daemon=True, name="pyclean-worker").start()
            WORKER_STARTED = True


def _enqueue(job, target, args):
    """Place un job dans la file et renvoie sa position (0 = prochain).

    Le job est passé en premier argument de la cible (elle en a besoin pour
    rapporter la progression) et mémorisé à part pour que le worker puisse
    signaler un échec inattendu.
    """
    _start_worker()
    try:
        QUEUE.put_nowait((target, (job, *args), job))
    except queue.Full as exc:
        _drop_job(job["id"])  # l'upload ne sert plus à rien
        raise HTTPException(
            429,
            f"File d'attente pleine ({QUEUE_MAX} traitements) : réessayez dans un instant.",
        ) from exc
    with JOBS_LOCK:
        job["queue_position"] = max(0, QUEUE.qsize() - 1)
    return job["queue_position"]


# ------------------------------------------------------------------ traitement

def _run_job(job, up, denoise, input_sr, cutoff, transcribe, cancel):
    up = Path(up)
    with JOBS_LOCK:
        job["state"] = "running"
    outdir = up.parent / "out"
    try:
        raise_if_cancelled(cancel)
        # fichier seul : les deux formats (WAV + MP3) sont proposés au téléchargement
        res = process_file(up, outdir, denoise, input_sr, cutoff,
                           lambda s, p: _stage(job, s, p), output_format="mp3",
                           transcribe=transcribe, cancel=cancel, keep_original=True)
        job["kind"] = res["kind"]
        job["artifacts"] = {k: v for k, v in res.items()
                            if k in ARTIFACT_KEYS and v}
        if res["output"]:
            job["artifacts"]["video"] = res["output"]
        _stage(job, "Terminé", 1.0, done=True)
    except JobCancelled:
        _finish_cancelled(job)
    except Exception as e:
        with JOBS_LOCK:
            job["state"] = "error"
            job["error"] = str(e)
            job["stage"] = "Erreur"


def _safe_relpath(name: str):
    """Chemins relatifs fiables (pas de '..', pas d'absolu) depuis un nom multipart."""
    name = (name or "").replace("\\", "/").lstrip("/")
    parts = [p for p in name.split("/") if p and p not in (".", "..")]
    return Path(*parts) if parts else None


def _run_folder_job(job, entries, sources, denoise, input_sr, cutoff,
                    output_format, transcribe, cancel):
    total = len(entries)
    with JOBS_LOCK:
        job["state"] = "running"
    for i, ent in enumerate(entries):
        src, outdir = sources[i]
        with JOBS_LOCK:
            ent["state"] = "running"
            job["stage"] = f"Fichier {i + 1}/{total} — {ent['relpath']}"

        def cb(stage, p, _i=i, _ent=ent):
            with JOBS_LOCK:
                _ent["stage"] = stage
                _ent["progress"] = round(min(max(p, 0.0), 1.0), 3)
                job["stage"] = f"Fichier {_i + 1}/{total} — {_ent['relpath']} : {stage}"
                job["progress"] = round(min((_i + p) / total, 1.0), 3)

        try:
            raise_if_cancelled(cancel)
            res = process_file(Path(src), Path(outdir), denoise, input_sr, cutoff,
                               cb, output_format=output_format,
                               transcribe=transcribe, cancel=cancel,
                               # l'original ne sert qu'à la comparaison A/B, absente
                               # du listing dossier : inutile de le garder sur disque
                               keep_original=False)
            ent["kind"] = res["kind"]
            # seules les clés réellement produites (pas d'original en dossier)
            ent["artifacts"] = {k: v for k, v in res.items()
                                if k in ARTIFACT_KEYS and v}
            if res["output"]:
                ent["artifacts"]["video"] = res["output"]
            with JOBS_LOCK:
                ent["state"] = "done"
                ent["stage"] = "Terminé"
                ent["progress"] = 1.0
        except JobCancelled:
            _finish_cancelled(job)
            return
        except Exception as e:
            with JOBS_LOCK:
                ent["state"] = "error"
                ent["error"] = str(e)
                ent["stage"] = "Erreur"

    done = sum(1 for e in entries if e["state"] == "done")
    zip_path = None
    if done:
        try:
            zip_path = _build_folder_zip(job, entries, output_format)
        except Exception:
            zip_path = None
    with JOBS_LOCK:
        job["progress"] = 1.0
        if done:
            job["state"] = "done"
            job["stage"] = f"Terminé — {done}/{total} fichiers"
            if zip_path:
                job["zip"] = zip_path
        else:
            job["state"] = "error"
            job["error"] = "Aucun fichier traité avec succès."
            job["stage"] = "Erreur"


def _build_folder_zip(job, entries, output_format="mp3"):
    """Archive ZIP (sans compression, fichiers déjà compressés) des résultats réussis.

    Le format audio est celui choisi par l'utilisateur (output_format) ;
    les vidéos sont toujours en MP4."""
    zip_path = DATA / job["id"] / "results.zip"
    seen = {}
    added = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for ent in entries:
            if ent["state"] != "done":
                continue
            rel = Path(ent["relpath"])
            prefix = "" if str(rel.parent) == "." else str(rel.parent) + "/"
            # même stem deux fois dans un même sous-dossier (ex. a.mp3 + a.mkv) :
            # on suffixe _2, _3… pour ne pas écraser les entrées du zip
            key = (prefix, rel.stem.lower())
            n = seen.get(key, 0) + 1
            seen[key] = n
            stem = f"{rel.stem}_{n}" if n > 1 else rel.stem
            arts = ent["artifacts"]
            if output_format == "mp3":
                if arts.get("enhanced_mp3"):
                    zf.write(arts["enhanced_mp3"], f"{prefix}{stem}_pyclean-audio.mp3")
                    added += 1
            elif arts.get("enhanced_wav"):
                zf.write(arts["enhanced_wav"], f"{prefix}{stem}_pyclean-audio.wav")
                added += 1
            if arts.get("video"):
                zf.write(arts["video"], f"{prefix}{stem}_pyclean-audio.mp4")
                added += 1
            if arts.get("transcript"):
                zf.write(arts["transcript"], f"{prefix}{stem}_transcript.txt")
                added += 1
            if arts.get("transcript_srt"):
                zf.write(arts["transcript_srt"], f"{prefix}{stem}.srt")
                added += 1
    if added == 0:
        if zip_path.exists():
            zip_path.unlink()
        return None
    return str(zip_path)


# ------------------------------------------------------------------ application

async def _purge_loop():
    while True:
        await asyncio.sleep(PURGE_INTERVAL)
        try:
            _purge_expired()
        except Exception:
            pass


@asynccontextmanager
async def lifespan(_app):
    _purge_orphans()
    threading.Thread(target=get_enhancer().preload, daemon=True).start()
    if PRELOAD_ASR:
        threading.Thread(target=get_transcriber().preload, daemon=True).start()
    task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="pyclean-audio", lifespan=lifespan)


@app.get("/api/status")
def status():
    s = get_enhancer().status()
    s["transcriber"] = get_transcriber().status()
    s["retention"] = {
        "ttl_s": JOB_TTL,
        "max_jobs": JOB_MAX,
        "jobs": len(JOBS),
    }
    s["queue"] = {"waiting": QUEUE.qsize(), "max": QUEUE_MAX}
    return s


def _check_input_sr(input_sr):
    if input_sr not in (8000, 16000, 24000):
        raise HTTPException(400, "input_sr doit être 8000, 16000 ou 24000")


def _check_transcribe(transcribe: bool) -> None:
    """Refuse la transcription si NeMo n'est pas installé.

    Sans cela, la demande serait acceptée puis le job échouerait en cours de
    route sur un `ModuleNotFoundError: nemo` ; l'interface désactive déjà la
    case dans ce cas, mais un client API peut ignorer l'état.
    """
    if transcribe and not is_available():
        raise HTTPException(
            400,
            "Transcription indisponible : nemo_toolkit[asr] n'est pas installé. "
            "Lancez ./run.sh --asr puis redémarrez le serveur.",
        )


async def _save_upload(upload: UploadFile, dest: Path, budget: list[int]):
    """Écrit un upload par morceaux en contrôlant la taille.

    `budget` est une liste à un élément [octets déjà écrits sur la demande] :
    la limite est ainsi partagée entre tous les fichiers d'un même envoi.
    """
    written = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_SIZE:
                raise HTTPException(413, f"Fichier trop volumineux (2 Go max) : {dest.name}")
            budget[0] += len(chunk)
            if budget[0] > MAX_FOLDER_TOTAL:
                raise HTTPException(413, "Dossier trop volumineux au total")
            fh.write(chunk)
    if written == 0:
        raise HTTPException(400, f"Fichier vide : {dest.name}")
    return written


@app.post("/api/enhance")
async def enhance(file: UploadFile = File(...),
                  denoise: bool = Form(False),
                  input_sr: int = Form(16000),
                  cutoff: int = Form(None),
                  transcribe: bool = Form(False)):
    filename = file.filename or "fichier"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Format non pris en charge : {ext}")
    _check_input_sr(input_sr)
    _check_transcribe(transcribe)
    _make_room()

    job_id = uuid.uuid4().hex[:12]
    jobdir = DATA / job_id
    jobdir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).stem)[:80] or "fichier"
    up = jobdir / f"{safe_stem}{ext}"
    try:
        await _save_upload(file, up, [0])
    except Exception:
        shutil.rmtree(jobdir, ignore_errors=True)  # pas de résidu d'upload avorté
        raise

    job = _register({
        "id": job_id,
        "filename": filename,
        "stem": Path(filename).stem,
        "state": "queued",
        "stage": "En file d'attente…",
        "progress": 0.0,
        "error": None,
        "kind": None,
        "artifacts": {},
        "queue_position": 0,
    })
    pos = _enqueue(job, _run_job, (up, denoise, input_sr, cutoff, transcribe,
                                   job["_cancel"]))
    return {"job_id": job_id, "queue_position": pos}


@app.post("/api/enhance_folder")
async def enhance_folder(files: list[UploadFile] = File(...),
                         denoise: bool = Form(False),
                         input_sr: int = Form(16000),
                         cutoff: int = Form(None),
                         output_format: str = Form("mp3"),
                         transcribe: bool = Form(False)):
    """Traite un dossier entier (fichiers envoyés avec leur chemin relatif).

    output_format : format de sortie audio, "mp3" (défaut) ou "wav".
    Les vidéos restent en MP4 dans tous les cas.
    transcribe : ajoute une transcription Parakeet (TXT + SRT) de l'audio nettoyé."""
    _check_input_sr(input_sr)
    _check_transcribe(transcribe)
    if output_format not in ("wav", "mp3"):
        raise HTTPException(400, "output_format doit être wav ou mp3")
    if not files:
        raise HTTPException(400, "Aucun fichier reçu")
    if len(files) > MAX_FOLDER_FILES:
        raise HTTPException(400, f"Trop de fichiers (max {MAX_FOLDER_FILES})")
    _make_room()

    job_id = uuid.uuid4().hex[:12]
    jobdir = DATA / job_id
    (jobdir / "uploads").mkdir(parents=True, exist_ok=True)
    (jobdir / "out").mkdir(parents=True, exist_ok=True)

    entries = []
    sources = []
    seen = {}
    budget = [0]
    try:
        for f in files:
            raw = f.filename or "fichier"
            rel = _safe_relpath(raw)
            if rel is None:
                raise HTTPException(400, f"Nom de fichier invalide : {raw}")
            if rel.suffix.lower() not in ALLOWED_EXT:
                continue  # filtrage déjà côté client ; on ignore le reste
            up = jobdir / "uploads" / rel
            up.parent.mkdir(parents=True, exist_ok=True)
            try:
                await _save_upload(f, up, budget)
            except HTTPException as e:
                if "vide" in str(e.detail):
                    up.unlink(missing_ok=True)
                    continue
                raise
            # collisions de stem dans le même sous-dossier : suffixe _2, _3…
            key = (str(rel.parent), rel.stem.lower())
            n = seen.get(key, 0) + 1
            seen[key] = n
            outdir = jobdir / "out" / rel.parent
            if n > 1:
                stem = f"{rel.stem}_{n}"
                outdir = outdir / f"{rel.stem}_{n}"
            else:
                stem = rel.stem
            entries.append({
                "index": len(entries),
                "relpath": str(rel),
                "stem": stem,
                "state": "queued",
                "stage": "En file d'attente…",
                "progress": 0.0,
                "error": None,
                "kind": None,
                "artifacts": {},
            })
            sources.append((str(up), str(outdir)))
    except Exception:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise

    if not entries:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise HTTPException(400, "Aucun fichier audio/vidéo pris en charge dans le dossier")

    job = _register({
        "id": job_id,
        "kind": "folder",
        "filename": f"{len(entries)} fichiers (dossier)",
        "stem": None,
        "state": "queued",
        "stage": f"Dossier : {len(entries)} fichiers en file d'attente…",
        "progress": 0.0,
        "error": None,
        "output_format": output_format,
        "files": entries,
        "queue_position": 0,
    })
    pos = _enqueue(job, _run_folder_job,
                   (entries, sources, denoise, input_sr, cutoff, output_format,
                    transcribe, job["_cancel"]))
    return {"job_id": job_id, "queue_position": pos}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "Job introuvable")
        return _snapshot(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Interrompt un traitement : le worker s'arrête au prochain point de contrôle
    (bloc d'inférence, tranche de transcription, ligne de progression ffmpeg)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "Job introuvable")
        if job["state"] not in ACTIVE_STATES:
            raise HTTPException(409, "Le traitement est déjà terminé.")
        job["_cancel"].set()
    return {"ok": True, "job_id": job_id}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Oublie le job et supprime ses fichiers (uploads, résultats, ZIP)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise HTTPException(404, "Job introuvable")
        if job["state"] in ACTIVE_STATES:
            raise HTTPException(409, "Traitement en cours : annulez-le d'abord.")
    _drop_job(job_id)
    return {"ok": True, "job_id": job_id}


DOWNLOADS = {
    "original_wav": ("{stem}.wav", "audio/wav"),
    "enhanced_wav": ("{stem}_pyclean-audio.wav", "audio/wav"),
    "enhanced_mp3": ("{stem}_pyclean-audio.mp3", "audio/mpeg"),
    "video": ("{stem}_pyclean-audio.mp4", "video/mp4"),
    "transcript": ("{stem}_transcript.txt", "text/plain"),
    "transcript_srt": ("{stem}_pyclean-audio.srt", "application/x-subrip"),
}


@app.get("/api/jobs/{job_id}/file/{index}/{name}")
def get_job_file(job_id: str, index: int, name: str):
    """Téléchargement d'un artefact d'un fichier précis d'un job « dossier »."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or "files" not in job:
        raise HTTPException(404, "Job introuvable")
    files = job["files"]
    if not (0 <= index < len(files)) or name not in DOWNLOADS:
        raise HTTPException(404, "Fichier introuvable")
    ent = files[index]
    path = ent["artifacts"].get(name)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Fichier introuvable")
    fname, media = DOWNLOADS[name]
    return FileResponse(
        path, media_type=media,
        filename=fname.format(stem=ent["stem"]),
    )


@app.get("/api/jobs/{job_id}/zip")
def get_job_zip(job_id: str):
    """Archive ZIP de tous les résultats d'un job « dossier »."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or job.get("kind") != "folder":
        raise HTTPException(404, "Job introuvable")
    zip_path = job.get("zip")
    if not zip_path or not Path(zip_path).exists():
        raise HTTPException(404, "Archive indisponible")
    return FileResponse(
        zip_path, media_type="application/zip", filename="pyclean-audio_dossier.zip",
    )


@app.get("/api/jobs/{job_id}/file/{name}")
def get_file(job_id: str, name: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or name not in DOWNLOADS:
        raise HTTPException(404, "Fichier introuvable")
    path = job["artifacts"].get(name)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Fichier introuvable")
    fname, media = DOWNLOADS[name]
    return FileResponse(
        path, media_type=media,
        filename=fname.format(stem=job["stem"]),
    )


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
