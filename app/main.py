import asyncio
import contextlib
import ipaddress
import queue
import re
import shutil
import socket
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import downloader
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
from .messages import MediaError, error_text, jsonable, stage_text
from .processor import ALLOWED_EXT, process_file
from .transcriber import get_transcriber, is_available

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()

# One job at a time: a single worker consumes the queue, so a job's inference
# and ffmpeg phases never overlap (several ffmpeg would only fight over the
# disk anyway).
QUEUE = queue.Queue(maxsize=QUEUE_MAX)
WORKER_LOCK = threading.Lock()
WORKER_STARTED = False

ACTIVE_STATES = ("queued", "running")

# process_file() keys that become downloadable artifacts
ARTIFACT_KEYS = frozenset({
    "original_wav", "enhanced_wav", "enhanced_mp3", "transcript", "transcript_srt",
    # the subtitle yt-dlp downloaded (never muxed, always .srt)
    "subtitle",
})


class ApiError(HTTPException):
    """An HTTP error that carries a translatable key next to its wording.

    `detail` is the English wording (what a client that does not translate
    reads, and what the CLI prints); `code` and `params` are added to the body
    by `_api_error_handler` so the UI can translate it.
    """

    def __init__(self, status_code: int, code: str, **params):
        super().__init__(status_code, error_text(code, params))
        self.code = code
        self.params = {k: jsonable(v) for k, v in params.items()}


def _stage(job, stage, progress, done=False, key=None, args=None):
    with JOBS_LOCK:
        _stage_locked(job, stage, progress, done, key, args)


def _stage_locked(job, stage, progress, done=False, key=None, args=None):
    """`_stage` without the lock: to be used under JOBS_LOCK only.

    `key` and `args` are the translatable form of `stage`: the UI shows its own
    translation and falls back to `stage` when it does not know the key.
    """
    job["stage"] = stage
    job["stage_key"] = key
    job["stage_args"] = args or {}
    job["progress"] = round(min(max(progress, 0.0), 1.0), 3)
    if done:
        job["state"] = "done"


def _fail(job, exc: Exception):
    """Moves a job (or a folder entry) to the error state, translatable key
    included.

    To be called under JOBS_LOCK. An exception that is not a MediaError (a bug,
    CUDA, a third-party library) has no code: the UI then shows `error` as is.
    """
    job["state"] = "error"
    job["error"] = str(exc)
    job["error_code"] = getattr(exc, "code", None)
    job["error_params"] = getattr(exc, "params", None) or {}
    job["stage"] = stage_text("error")
    job["stage_key"] = "error"
    job["stage_args"] = {}


def _public_artifacts(artifacts):
    """Return only the artifacts actually produced, with their file name: the
    client builds its URLs from the key, server paths have no business in the
    response."""
    return {k: Path(v).name for k, v in (artifacts or {}).items() if v}


def _snapshot(job):
    """A copy of the job that is safe to serialise.

    The worker thread keeps mutating `job` while FastAPI encodes the response:
    without copying the nested dicts, an `artifacts` that grows in the middle
    of the encoding fails the request (“dictionary changed size during
    iteration”). Internal keys (`_cancel`, `_src`…) and server paths are never
    exposed.
    """
    snap = {k: v for k, v in job.items() if not k.startswith("_")}
    snap["artifacts"] = _public_artifacts(job.get("artifacts"))
    if "zip" in snap:
        # the client builds the URL from the id: the path is not needed
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


# ---------------------------------------------------------------- retention

def _drop_job(job_id):
    """Forget a job and delete all its files. True if the job existed."""
    with JOBS_LOCK:
        job = JOBS.pop(job_id, None)
    if job is None:
        return False
    shutil.rmtree(DATA / job_id, ignore_errors=True)
    return True


def _purge_expired():
    """Delete finished jobs whose TTL has expired (never running ones)."""
    now = time.time()
    with JOBS_LOCK:
        stale = [jid for jid, j in JOBS.items()
                 if j.get("state") not in ACTIVE_STATES
                 and j.get("expires_at", 0) <= now]
    return sum(1 for jid in stale if _drop_job(jid))


def _purge_orphans():
    """At startup the registry is empty: whatever is left on disk is
    unreachable (the API would answer 404). Free it right away."""
    n = 0
    if not DATA.exists():
        return 0
    for d in DATA.iterdir():
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


def _make_room():
    """Keep the registry under JOB_MAX by evicting the oldest finished jobs;
    503 only if the registry is entirely made of active jobs."""
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
            raise ApiError(503, "too_many_jobs", max=JOB_MAX)


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
        job["stage"] = stage_text("cancelled")
        job["stage_key"] = "cancelled"
        job["stage_args"] = {}
        job["error"] = None
    shutil.rmtree(DATA / job["id"], ignore_errors=True)


def _worker():
    """The queue's single consumer: one GPU job at a time.

    A `None` in the queue stops the worker (server shutdown, tests).
    """
    while True:
        item = QUEUE.get()
        if item is None:
            return
        target, args, job = item
        try:
            target(*args)
        except Exception as e:  # safety net: a job that crashes must not
            # silently vanish from the UI
            with JOBS_LOCK:
                if job["state"] in ACTIVE_STATES:
                    _fail(job, e)


def _start_worker():
    global WORKER_STARTED
    with WORKER_LOCK:
        if not WORKER_STARTED:
            threading.Thread(target=_worker, daemon=True, name="pyclean-worker").start()
            WORKER_STARTED = True


def _enqueue(job, target, args):
    """Queue a job and return its position (0 = next).

    The job is passed as the first argument of the target (it needs it to
    report progress) and remembered separately so the worker can report an
    unexpected failure.
    """
    _start_worker()
    try:
        QUEUE.put_nowait((target, (job, *args), job))
    except queue.Full as exc:
        _drop_job(job["id"])  # the upload is of no use any more
        raise ApiError(
            429, "queue_full", max=QUEUE_MAX,
        ) from exc
    with JOBS_LOCK:
        job["queue_position"] = max(0, QUEUE.qsize() - 1)
    return job["queue_position"]


# ------------------------------------------------------------------ processing

def _run_job(job, up, denoise, input_sr, cutoff, transcribe, cancel):
    up = Path(up)
    with JOBS_LOCK:
        job["state"] = "running"
    outdir = up.parent / "out"
    try:
        raise_if_cancelled(cancel)
        # single file: both formats (WAV + MP3) are offered for download
        res = process_file(
            up, outdir, denoise, input_sr, cutoff,
            lambda s, p, k=None, a=None: _stage(job, s, p, key=k, args=a),
            output_format="mp3", transcribe=transcribe, cancel=cancel,
            keep_original=True,
        )
        job["kind"] = res["kind"]
        job["artifacts"] = {k: v for k, v in res.items()
                            if k in ARTIFACT_KEYS and v}
        if res["output"]:
            job["artifacts"]["video"] = res["output"]
        _stage(job, stage_text("done"), 1.0, done=True, key="done")
    except JobCancelled:
        _finish_cancelled(job)
    except Exception as e:
        with JOBS_LOCK:
            _fail(job, e)


def _file_stage(index, total, name, text, key, args):
    """(text, key, params) of the current stage of a folder job.

    The inner stage is nested in the params (`inner_key`/`inner_args`): the UI
    translates it too, otherwise the “File 2/12 — a.mp3: …” line would stay
    half in the server's language.
    """
    params = {"index": index, "total": total, "name": name,
              "inner_key": key, "inner_args": args or {}}
    return stage_text("file_step", {**params, "inner": text}), "file_step", params


def _safe_relpath(name: str):
    """Reliable relative paths (no '..', no absolute) from a multipart name."""
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
            text, key, args = _file_stage(i + 1, total, ent["relpath"], "", None, None)
            _stage_locked(job, text, job["progress"], key=key, args=args)

        def cb(stage, p, key=None, args=None, _i=i, _ent=ent):
            with JOBS_LOCK:
                _ent["stage"] = stage
                _ent["stage_key"] = key
                _ent["stage_args"] = args or {}
                _ent["progress"] = round(min(max(p, 0.0), 1.0), 3)
                text, key, sparams = _file_stage(
                    _i + 1, total, _ent["relpath"], stage, key, args)
                _stage_locked(job, text, (_i + p) / total, key=key, args=sparams)

        try:
            raise_if_cancelled(cancel)
            res = process_file(Path(src), Path(outdir), denoise, input_sr, cutoff,
                               cb, output_format=output_format,
                               transcribe=transcribe, cancel=cancel,
                               # the original only feeds the A/B comparison, which the
                               # folder listing has no use for: no need to keep it
                               keep_original=False)
            ent["kind"] = res["kind"]
            # only the keys actually produced (no original in folder mode)
            ent["artifacts"] = {k: v for k, v in res.items()
                                if k in ARTIFACT_KEYS and v}
            if res["output"]:
                ent["artifacts"]["video"] = res["output"]
            with JOBS_LOCK:
                ent["state"] = "done"
                ent["stage"] = stage_text("done")
                ent["stage_key"] = "done"
                ent["stage_args"] = {}
                ent["progress"] = 1.0
        except JobCancelled:
            _finish_cancelled(job)
            return
        except Exception as e:
            with JOBS_LOCK:
                _fail(ent, e)

    done = sum(1 for e in entries if e["state"] == "done")
    zip_path = None
    if done:
        try:
            zip_path = _build_folder_zip(job, entries, output_format)
        except Exception:
            zip_path = None
    with JOBS_LOCK:
        if done:
            _stage_locked(job, stage_text("folder_done", {"done": done, "total": total}),
                          1.0, done=True, key="folder_done",
                          args={"done": done, "total": total})
            if zip_path:
                job["zip"] = zip_path
        else:
            _fail(job, MediaError("no_file_done"))


def _build_folder_zip(job, entries, output_format="mp3"):
    """ZIP archive (stored, the files are already compressed) of the successful
    results.

    The audio format is the one the user picked (output_format); videos are
    always MP4."""
    zip_path = DATA / job["id"] / "results.zip"
    seen = {}
    added = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for ent in entries:
            if ent["state"] != "done":
                continue
            rel = Path(ent["relpath"])
            prefix = "" if str(rel.parent) == "." else str(rel.parent) + "/"
            # same stem twice in one subfolder (e.g. a.mp3 + a.mkv): suffix
            # _2, _3… so zip entries do not overwrite each other
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
            elif arts.get("subtitle"):
                # yt-dlp's own subtitle, converted to SRT. Mutually exclusive
                # with transcript_srt (a download job never transcribes), and
                # the "elif" keeps one <stem>.srt per entry in the archive.
                zf.write(arts["subtitle"], f"{prefix}{stem}.srt")
                added += 1
    if added == 0:
        if zip_path.exists():
            zip_path.unlink()
        return None
    return str(zip_path)


# ------------------------------------------------------- téléchargement (yt-dlp)

# Part of an entry's slice the download takes before process_file takes over.
FETCH_SHARE = 0.3


def _run_download_job(job, plan, denoise, input_sr, cutoff, cancel):
    """Worker of a yt-dlp job: download then process, exactly like an upload.

    One entry → the flat single-file shape (A/B kept, `renderResults` works
    unchanged). Several → the folder shape: one row per entry, `keep_original`
    false, the downloaded source deleted right after it fed the model, then the
    single ZIP.
    """
    with JOBS_LOCK:
        job["state"] = "running"
    jobdir = DATA / job["id"]
    try:
        raise_if_cancelled(cancel)
        if len(plan["entries"]) == 1:
            _download_single(job, plan, denoise, input_sr, cutoff, cancel, jobdir)
        else:
            _download_playlist(job, plan, denoise, input_sr, cutoff, cancel, jobdir)
        with JOBS_LOCK:
            # a failed playlist (no_file_done) keeps its error state
            if job["state"] == "running":
                _stage_locked(job, stage_text("done"), 1.0, done=True, key="done")
    except JobCancelled:
        _finish_cancelled(job)
    except Exception as e:
        with JOBS_LOCK:
            _fail(job, e)


def _fetch(job, ent_index, total, plan, cancel, jobdir, cb_stage):
    """Download entry `ent_index` (0-based) and report its progress.

    `cb_stage(text, fraction, key, args)` nests the progress in the folder job's
    "File i/n" line, or writes it directly on a single-file job.
    """
    src = jobdir / "src" / str(ent_index)
    stage = stage_text("fetch")
    raise_if_cancelled(cancel)
    got = downloader.download(
        plan["url"], src, plan["fmt"], plan["subtitles"], plan["subtitle_lang"],
        on_stage=lambda p: cb_stage(stage, p, "fetch", {}), cancel=cancel,
        # one call per entry: the source of a playlist is processed (then
        # deleted) before the next one is fetched, so the disk stays bounded
        playlist_index=ent_index if total > 1 else None,
    )
    return got[0]


def _download_single(job, plan, denoise, input_sr, cutoff, cancel, jobdir):
    outdir = jobdir / "out"
    got = _fetch(job, 0, 1, plan, cancel, jobdir,
                 lambda text, p, key, args: _stage(job, text, FETCH_SHARE * p,
                                                  key=key, args=args))
    res = process_file(
        got.path, outdir, denoise, input_sr, cutoff,
        lambda s, p, k=None, a=None: _stage(
            job, s, FETCH_SHARE + (1 - FETCH_SHARE) * p, key=k, args=a),
        output_format="mp3", transcribe=False, cancel=cancel,
        keep_original=True,   # the A/B comparison needs the source audio
    )
    job["kind"] = res["kind"]
    job["artifacts"] = {k: v for k, v in res.items() if k in ARTIFACT_KEYS and v}
    if res["output"]:
        job["artifacts"]["video"] = res["output"]
    if got.subtitle is not None:
        job["artifacts"]["subtitle"] = str(got.subtitle)


def _download_playlist(job, plan, denoise, input_sr, cutoff, cancel, jobdir):
    entries = job["files"]
    total = len(entries)
    for i, ent in enumerate(entries):
        outdir = jobdir / "out" / str(i)
        with JOBS_LOCK:
            ent["state"] = "running"
            text, key, args = _file_stage(i + 1, total, ent["relpath"], "", None, None)
            _stage_locked(job, text, job["progress"], key=key, args=args)

        def nested(text, p, key, args, _i=i, _ent=ent):
            with JOBS_LOCK:
                _ent["stage"] = text
                _ent["stage_key"] = key
                _ent["stage_args"] = args or {}
                _ent["progress"] = round(min(max(p, 0.0), 1.0), 3)
                line, k, sparams = _file_stage(_i + 1, total, _ent["relpath"],
                                               text, key, args)
                _stage_locked(job, line,
                              (_i + FETCH_SHARE * p) / total, key=k, args=sparams)

        def pipeline(s, p, k=None, a=None, _nested=nested, _i=i):
            _nested(s, FETCH_SHARE + (1 - FETCH_SHARE) * p, k, a)

        try:
            raise_if_cancelled(cancel)
            got = _fetch(job, i, total, plan, cancel, jobdir, nested)
            res = process_file(
                got.path, outdir, denoise, input_sr, cutoff, pipeline,
                output_format="mp3", transcribe=False, cancel=cancel,
                keep_original=False,   # no A/B in folder mode: −43 % of disk
            )
            ent["kind"] = res["kind"]
            ent["artifacts"] = {k: v for k, v in res.items()
                                if k in ARTIFACT_KEYS and v}
            if res["output"]:
                ent["artifacts"]["video"] = res["output"]
            # the subtitle lives in the source folder, which is deleted below:
            # it becomes an artifact of the job, next to the other results
            if got.subtitle is not None:
                kept = outdir / f"{got.path.stem}.srt"
                outdir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(got.subtitle), kept)
                ent["artifacts"]["subtitle"] = str(kept)
            # the downloaded file only fed the model: free the disk now
            shutil.rmtree(jobdir / "src" / str(i), ignore_errors=True)
            with JOBS_LOCK:
                ent["state"] = "done"
                ent["stage"] = stage_text("done")
                ent["stage_key"] = "done"
                ent["stage_args"] = {}
                ent["progress"] = 1.0
        except JobCancelled:
            _finish_cancelled(job)
            return
        except Exception as e:
            with JOBS_LOCK:
                _fail(ent, e)

    done = sum(1 for e in entries if e["state"] == "done")
    zip_path = None
    if done:
        try:
            zip_path = _build_folder_zip(job, entries, "mp3")
        except Exception:
            zip_path = None
    with JOBS_LOCK:
        if done:
            _stage_locked(job, stage_text("folder_done", {"done": done, "total": total}),
                          1.0, done=True, key="folder_done",
                          args={"done": done, "total": total})
            if zip_path:
                job["zip"] = zip_path
        else:
            _fail(job, MediaError("no_file_done"))


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


@app.exception_handler(ApiError)
async def _api_error_handler(_request, exc: ApiError):
    """Adds `code`/`params` to the error body, next to `detail`.

    FastAPI's default handler only serialises `detail`: without this one the UI
    would have nothing to translate.
    """
    body = {"detail": exc.detail, "code": exc.code}
    if exc.params:
        body["params"] = exc.params
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


@app.get("/api/status")
def status():
    s = get_enhancer().status()
    s["transcriber"] = get_transcriber().status()
    s["downloader"] = {
        "available": downloader.is_available(),
        "version": downloader.version(),
    }
    s["retention"] = {
        "ttl_s": JOB_TTL,
        "max_jobs": JOB_MAX,
        "jobs": len(JOBS),
    }
    s["queue"] = {"waiting": QUEUE.qsize(), "max": QUEUE_MAX}
    return s


def _check_input_sr(input_sr):
    if input_sr not in (8000, 16000, 24000):
        raise ApiError(400, "bad_input_sr")


def _check_download_fmt(fmt: str) -> None:
    """Output format of a download job: "mp4" (default) or "mp3" (audio only)."""
    if fmt not in ("mp3", "mp4"):
        raise ApiError(400, "bad_download_format")


def _host_addresses(host: str):
    """Every IP address a hostname resolves to (empty when it cannot be
    resolved). A seam for the tests: no test resolves a real name."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    out = []
    for *_, sockaddr in infos:
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return out


def _is_local_ip(ip) -> bool:
    """Loopback, private, link-local, reserved or multicast: never fetchable."""
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast)


def _check_url(url: str) -> None:
    """Refuses anything but a public http(s) URL, **before** any network call.

    The server fetches a client-supplied URL on a LAN with no authentication:
    without this, `http://127.0.0.1:8787/…` or an internal host name would be a
    server-side request forgery. A *public* URL that redirects to an internal
    address stays inside yt-dlp (documented in README's Limitations).
    """
    raw = (url or "").strip()
    try:
        parsed = urlparse(raw)
        host = parsed.hostname
    except ValueError:
        parsed, host = None, None
    if not raw or parsed is None or parsed.scheme not in ("http", "https") or not host:
        # the URL itself is echoed (truncated): a URL can carry a token
        raise ApiError(400, "bad_url", url=raw[:200])
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _is_local_ip(literal):
        raise ApiError(400, "blocked_url")
    for ip in _host_addresses(host):
        if _is_local_ip(ip):
            raise ApiError(400, "blocked_url")


def _download_plan(entries: list[dict]):
    """Sanitised stems for the resolved entries, duplicates suffixed `_2`, `_3`…

    Same rule as a folder upload (two videos of a playlist share a title there),
    otherwise two entries would answer the same download name.
    """
    seen = {}
    out = []
    for ent in entries:
        raw = ent.get("title") or ent.get("id") or "video"
        stem = downloader.stem_for(raw, str(ent.get("id") or ""))
        n = seen.get(stem.lower(), 0) + 1
        seen[stem.lower()] = n
        if n > 1:
            stem = f"{stem}_{n}"
        out.append({"id": ent.get("id"), "title": raw, "stem": stem})
    return out


def _declared_total(entries: list[dict]) -> int:
    """Total size the site announces (flat metadata often has none: the check is
    then skipped, `max_filesize` remains the backstop for a single entry)."""
    total = 0
    for ent in entries:
        size = ent.get("filesize") or ent.get("filesize_approx") or 0
        try:
            total += int(size)
        except (TypeError, ValueError):
            continue
    return total


def _check_transcribe(transcribe: bool) -> None:
    """Refuses transcription when NeMo is not installed.

    Without this the request would be accepted and the job would then fail
    mid-flight on `ModuleNotFoundError: nemo`; the UI already disables the
    checkbox in that case, but an API client may ignore the state.
    """
    if transcribe and not is_available():
        raise ApiError(400, "transcribe_unavailable")


async def _save_upload(upload: UploadFile, dest: Path, budget: list[int]):
    """Write an upload in chunks, checking its size.

    `budget` is a one-element list [bytes already written for this request], so
    the limit is shared across all the files of a single submission.
    """
    written = 0
    with open(dest, "wb") as fh:
        while True:
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > MAX_SIZE:
                raise ApiError(413, "file_too_large", name=dest.name)
            budget[0] += len(chunk)
            if budget[0] > MAX_FOLDER_TOTAL:
                raise ApiError(413, "folder_too_large")
            fh.write(chunk)
    if written == 0:
        raise ApiError(400, "empty_file", name=dest.name)
    return written


@app.post("/api/enhance")
async def enhance(file: UploadFile = File(...),
                  denoise: bool = Form(False),
                  input_sr: int = Form(16000),
                  cutoff: int = Form(None),
                  transcribe: bool = Form(False)):
    filename = file.filename or "file"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ApiError(400, "unsupported_format", ext=ext)
    _check_input_sr(input_sr)
    _check_transcribe(transcribe)
    _make_room()

    job_id = uuid.uuid4().hex[:12]
    jobdir = DATA / job_id
    jobdir.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).stem)[:80] or "file"
    up = jobdir / f"{safe_stem}{ext}"
    try:
        await _save_upload(file, up, [0])
    except Exception:
        shutil.rmtree(jobdir, ignore_errors=True)  # no leftover from a failed upload
        raise

    job = _register({
        "id": job_id,
        "filename": filename,
        "stem": Path(filename).stem,
        "state": "queued",
        "stage": stage_text("queued"),
        "stage_key": "queued",
        "stage_args": {},
        "progress": 0.0,
        "error": None,
        "error_code": None,
        "error_params": {},
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
    """Process a whole folder (files are sent with their relative path).

    output_format: audio output format, "mp3" (default) or "wav". Videos stay
    MP4 either way.
    transcribe: adds a Parakeet transcript (TXT + SRT) of the cleaned audio."""
    _check_input_sr(input_sr)
    _check_transcribe(transcribe)
    if output_format not in ("wav", "mp3"):
        raise ApiError(400, "bad_output_format")
    if not files:
        raise ApiError(400, "no_files")
    if len(files) > MAX_FOLDER_FILES:
        raise ApiError(400, "too_many_files", max=MAX_FOLDER_FILES)
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
            raw = f.filename or "file"
            rel = _safe_relpath(raw)
            if rel is None:
                raise ApiError(400, "bad_filename", name=raw)
            if rel.suffix.lower() not in ALLOWED_EXT:
                continue  # the client already filters; ignore the rest
            up = jobdir / "uploads" / rel
            up.parent.mkdir(parents=True, exist_ok=True)
            try:
                await _save_upload(f, up, budget)
            except ApiError as e:
                if e.code == "empty_file":  # an empty file is skipped, not fatal
                    up.unlink(missing_ok=True)
                    continue
                raise
            # stem collisions in the same subfolder: suffix _2, _3…
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
                "stage": stage_text("queued"),
                "stage_key": "queued",
                "stage_args": {},
                "progress": 0.0,
                "error": None,
                "error_code": None,
                "error_params": {},
                "kind": None,
                "artifacts": {},
            })
            sources.append((str(up), str(outdir)))
    except Exception:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise

    if not entries:
        shutil.rmtree(jobdir, ignore_errors=True)
        raise ApiError(400, "no_media_in_folder")

    job = _register({
        "id": job_id,
        "kind": "folder",
        "filename": f"{len(entries)} files (folder)",
        "stem": None,
        "state": "queued",
        "stage": stage_text("folder_queued", {"count": len(entries)}),
        "stage_key": "folder_queued",
        "stage_args": {"count": len(entries)},
        "progress": 0.0,
        "error": None,
        "error_code": None,
        "error_params": {},
        "output_format": output_format,
        "files": entries,
        "queue_position": 0,
    })
    pos = _enqueue(job, _run_folder_job,
                   (entries, sources, denoise, input_sr, cutoff, output_format,
                    transcribe, job["_cancel"]))
    return {"job_id": job_id, "queue_position": pos}


@app.post("/api/download")
async def download_media(url: str = Form(...),
                         fmt: str = Form("mp4"),
                         subtitles: bool = Form(False),
                         subtitle_lang: str = Form("fr"),
                         denoise: bool = Form(False),
                         input_sr: int = Form(16000),
                         cutoff: int = Form(None)):
    """Download a video or a playlist with yt-dlp, then enhance it exactly like
    an upload.

    fmt: "mp4" (default, video + enhanced audio) or "mp3" (audio only).
    subtitles: download the **site's** subtitles (converted to SRT, never muxed).
    They *replace* the Parakeet transcription, so `transcribe` is deliberately
    **not** a parameter here: a client that sends it is ignored, and no
    `transcript`/`transcript_srt` artifact is ever produced.
    subtitle_lang: any yt-dlp language code (the page offers fr and en).
    Answers `{"job_id", "queue_position", "entries"}`: `entries == 1` means the
    single-file result view, more means a folder job with a ZIP.
    """
    _check_input_sr(input_sr)
    _check_download_fmt(fmt)
    if not downloader.is_available():
        raise ApiError(400, "ytdlp_unavailable")
    _check_url(url)

    # Metadata first: a bad URL, a private video or an oversized playlist is a
    # 4xx now, not a job that dies a few seconds later.
    try:
        entries = downloader.resolve(url)
    except MediaError as e:
        raise ApiError(400, e.code, **e.params) from e
    if not entries:
        raise ApiError(400, "download_failed", detail="no entry found")
    if len(entries) > MAX_FOLDER_FILES:
        raise ApiError(413, "playlist_too_large", entries=len(entries),
                       max=MAX_FOLDER_FILES)
    total_size = _declared_total(entries)
    if total_size > MAX_FOLDER_TOTAL:
        raise ApiError(413, "download_too_large",
                       gb=MAX_FOLDER_TOTAL // (1024 ** 3))
    _make_room()

    job_id = uuid.uuid4().hex[:12]
    plan = {
        "url": url.strip(),
        "fmt": fmt,
        "subtitles": subtitles,
        "subtitle_lang": subtitle_lang or "fr",
        "entries": _download_plan(entries),
    }
    common = {
        "id": job_id,
        "source": "ytdlp",
        "state": "queued",
        "stage": stage_text("queued"),
        "stage_key": "queued",
        "stage_args": {},
        "progress": 0.0,
        "error": None,
        "error_code": None,
        "error_params": {},
        "queue_position": 0,
    }
    if len(plan["entries"]) == 1:
        job = _register({**common,
                         "kind": None,
                         "filename": plan["entries"][0]["title"],
                         "stem": plan["entries"][0]["stem"],
                         "artifacts": {}})
    else:
        files = [{
            "index": i,
            "relpath": ent["title"],
            "stem": ent["stem"],
            "state": "queued",
            "stage": stage_text("queued"),
            "stage_key": "queued",
            "stage_args": {},
            "progress": 0.0,
            "error": None,
            "error_code": None,
            "error_params": {},
            "kind": None,
            "artifacts": {},
        } for i, ent in enumerate(plan["entries"])]
        job = _register({**common,
                         "kind": "folder",
                         "filename": f"{len(files)} files (playlist)",
                         "stem": None,
                         "output_format": "mp3",
                         "files": files})
    # the job folder is created by the worker: a refused submission leaves
    # nothing behind in data/jobs/
    pos = _enqueue(job, _run_download_job,
                   (plan, denoise, input_sr, cutoff, job["_cancel"]))
    return {"job_id": job_id, "queue_position": pos, "entries": len(plan["entries"])}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ApiError(404, "job_not_found")
        return _snapshot(job)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    """Interrupts a job: the worker stops at the next checkpoint (inference
    block, transcription chunk, ffmpeg progress line)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ApiError(404, "job_not_found")
        if job["state"] not in ACTIVE_STATES:
            raise ApiError(409, "job_already_done")
        job["_cancel"].set()
    return {"ok": True, "job_id": job_id}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Forget the job and delete its files (uploads, results, ZIP)."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            raise ApiError(404, "job_not_found")
        if job["state"] in ACTIVE_STATES:
            raise ApiError(409, "job_running")
    _drop_job(job_id)
    return {"ok": True, "job_id": job_id}


DOWNLOADS = {
    "original_wav": ("{stem}.wav", "audio/wav"),
    "enhanced_wav": ("{stem}_pyclean-audio.wav", "audio/wav"),
    "enhanced_mp3": ("{stem}_pyclean-audio.mp3", "audio/mpeg"),
    "video": ("{stem}_pyclean-audio.mp4", "video/mp4"),
    "transcript": ("{stem}_transcript.txt", "text/plain"),
    "transcript_srt": ("{stem}_pyclean-audio.srt", "application/x-subrip"),
    "subtitle": ("{stem}.srt", "application/x-subrip"),
}


@app.get("/api/jobs/{job_id}/file/{index}/{name}")
def get_job_file(job_id: str, index: int, name: str):
    """Download an artifact of one specific file of a folder job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or "files" not in job:
        raise ApiError(404, "job_not_found")
    files = job["files"]
    if not (0 <= index < len(files)) or name not in DOWNLOADS:
        raise ApiError(404, "file_not_found")
    ent = files[index]
    path = ent["artifacts"].get(name)
    if not path or not Path(path).exists():
        raise ApiError(404, "file_not_found")
    fname, media = DOWNLOADS[name]
    return FileResponse(
        path, media_type=media,
        filename=fname.format(stem=ent["stem"]),
    )


@app.get("/api/jobs/{job_id}/zip")
def get_job_zip(job_id: str):
    """ZIP archive of every result of a folder job."""
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or job.get("kind") != "folder":
        raise ApiError(404, "job_not_found")
    zip_path = job.get("zip")
    if not zip_path or not Path(zip_path).exists():
        raise ApiError(404, "zip_unavailable")
    return FileResponse(
        zip_path, media_type="application/zip", filename="pyclean-audio_dossier.zip",
    )


@app.get("/api/jobs/{job_id}/file/{name}")
def get_file(job_id: str, name: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None or name not in DOWNLOADS:
        raise ApiError(404, "file_not_found")
    path = job["artifacts"].get(name)
    if not path or not Path(path).exists():
        raise ApiError(404, "file_not_found")
    fname, media = DOWNLOADS[name]
    return FileResponse(
        path, media_type=media,
        filename=fname.format(stem=job["stem"]),
    )


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
