# AGENTS.md — pyclean-audio

Local web app (FastAPI + single page) that restores the audio of audio/video
files with LavaSR v2 (`LavaEnhance2`, HF model `YatharthS/LavaSR`). It can also
transcribe the cleaned audio (Parakeet TDT via NeMo, optional).

## Commands

```bash
# environment (once) — this is what run.sh does
uv venv .venv
uv pip install "LavaSR @ git+https://github.com/ysharma3501/LavaSR.git" fastapi "uvicorn[standard]" python-multipart
# extra for --transcribe / the "Transcribe" checkbox: ./run.sh --asr
# (or: uv pip install -c requirements-asr.txt "nemo_toolkit[asr]", or -r requirements.txt)
# NeMo is deliberately out of the base install: several Go, and it pins its own
# torch version. --asr adds it to an existing .venv without recreating it.
# The -c (requirements-asr.txt) is required, not a preference: unpinned, the ASR
# install resolves to transformers 4.x / tokenizers 0.10.3 and dies with
# "can't find Rust compiler" (no cp311 wheel). See "ASR constraints" below.

# web server (port 8787)
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
# or: ./run.sh            (creates .venv, installs the 4 base packages)
#     ./run.sh --asr       (idempotent: installs NeMo only if missing)
#     ./run.sh --help
# run.sh also honours PORT=9000 and PYCLEAN_WITH_ASR=1 (same as --asr), and
# pins --workers 1 on purpose: jobs are serialized in-process, several workers
# would only duplicate the models in VRAM.

# CLI
.venv/bin/python -m app.cli FILE [--denoise] [--transcribe] [--input-sr 8000|16000|24000] [--cutoff Hz] [--format wav|mp3] [-o OUTDIR]
.venv/bin/python -m app.cli DIR ...   # recursive; default output is <DIR>_pyclean-audio next to it (mirrored tree)

# tests and lint
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # ~220 tests, ~6 s, no model loaded
.venv/bin/python -m ruff check .      # `ruff check` is the only lint that counts (no ruff format)

# container (see the "Docker" section below)
docker compose up -d cpu                            # pulls ghcr.io/…:cpu, no build
docker compose up -d gpu                            # …:gpu, needs the NVIDIA toolkit
docker build --target cpu -t pyclean-audio:cpu .    # local build; `--target gpu` for CUDA
docker build --target test .                        # runs the shipped env + pytest, exits
docker run --rm -p 127.0.0.1:8787:8787 ghcr.io/aureld16/pyclean-audio:cpu
```

No **model** is loaded by the tests: no LavaSR weights, no NeMo. What *is*
imported is torch/numpy/soundfile — nearly every test module imports `app.main`
(or `app.enhancer` / `app.transcriber`) transitively, so the suite pays for
`import torch` (~1 s per test file) even though nothing is downloaded. `nemo`
and `LavaSR` are only imported *inside* functions, which is why the pipeline
tests can run with fake `app.enhancer` / `app.transcriber` modules injected in
`sys.modules` (`tests/conftest.py`); `processor` does its own imports in the body
of `process_file`. The `probe`/`process_file` tests are skipped when `ffmpeg` /
`ffprobe` are not in the PATH (`requires_ffmpeg`); `run_ffmpeg` is tested with a
fake `ffmpeg` (shell script) to force errors, hangs and cancellation. Real
behaviour (audio quality, VRAM) is validated by running the commands below.

## Architecture

The whole repository is in English: this file, README.md, the code (comments,
docstrings, CLI and API wording). The **French only lives in the UI
translations** (`static/index.html`) and in one product string, the transcript
placeholder `(aucune parole détectée)`. Write new comments and messages in
English.

- `app/enhancer.py` — model wrapper (singleton, inference lock, lazy load +
  preload at startup) **and windowed reading**: `Stream16k` converts the input
  to 16 kHz window by window. **The chunking lives here**: 60 s blocks at
  16 kHz, 2 s overlap, equal-power cosine crossfade, sequential WAV writing
  (O(window) memory, ~40 MB whatever the file length). `plan_ranges()` is a pure
  function, tested without a model.
  - `Stream16k` is **bit-for-bit equivalent** to resampling the whole file, under
    two conditions that must not break: (a) the window start is aligned on a
    multiple of the `lcm` of the convolution steps of the two possible
    resamplings (`_resample_stride`), otherwise a one-sample offset is audible
    at the joins; (b) the read margin (`READ_MARGIN_SEC`) must be on **both**
    sides, otherwise the first ~6 samples of each window read a padding zero
    instead of the signal. `tests/test_stream.py` locks that equivalence in.
- `app/processor.py` — ffmpeg pipeline: decode to mono 48 kHz, audio track
  extraction (videos), video remux (`-c:v copy`, AAC 192 k audio), MP3 encoding
  (libmp3lame 192 k); `process_file(..., output_format="wav")` also writes a
  `<stem>_pyclean-audio.mp3` when `output_format="mp3"` (always accompanied by
  the WAV, videos included); `ALLOWED_EXT` + `iter_media_files()` (recursive
  enumeration shared by the CLI and the API).
  - `run_ffmpeg`: stderr goes to a **temporary file**, never to a pipe (an
    undrained pipe blocks ffmpeg at 64 KB and hangs the job); the process is
    started with `start_new_session` and killed **by process group** (`_kill`),
    otherwise a grandchild survives and the stdout pipe never closes; optional
    `timeout` (watchdog). If the progress callback raises (cancellation), ffmpeg
    is killed before the error propagates.
  - `MAX_DURATION` is checked as soon as `probe()` returns: an over-long file is
    rejected within seconds, before any inference.
  - `on_stage` takes **four** arguments: `(text, progress, key, params)`. The
    text is English (CLI, `job["stage"]`, the UI's fallback), the key and its
    params are what the UI translates (`stage_key`/`stage_args`) —
    `process_file` never emits a hardcoded string (`emit("enhance", 0.12)`),
    otherwise one progress line would stay in a single language. The last two
    arguments are optional: an older caller, or a test, can stick to
    `lambda stage, prog: …`.
  - user-visible failures are `MediaError(code, **params)` (`app/messages.py`),
    not `RuntimeError` with a free-form message: `str(e)` is the English
    wording, but the UI can translate the `code`.
- `app/main.py` — API: `POST /api/enhance` (single-file upload; always produces
  WAV **and** MP3 so both downloads can be offered),
  `POST /api/enhance_folder` (folder upload, files sent with their multipart
  relative path, `output_format` parameter "mp3" (default) or "wav" driving the
  audio artifact and the ZIP), `GET /api/jobs/{id}`,
  `POST /api/jobs/{id}/cancel`, `DELETE /api/jobs/{id}`,
  `GET /api/jobs/{id}/file/{original_wav|enhanced_wav|enhanced_mp3|video|transcript|transcript_srt}`,
  `GET /api/jobs/{id}/file/{index}/{...}` (folder job),
  `GET /api/jobs/{id}/zip` (results archive, folder job),
  `GET /api/status` (LavaSR model state + `transcriber` + `queue` +
  `retention`). Both endpoints accept `transcribe` (bool, default false), which
  produces a `transcript` artifact (`<stem>_transcript.txt`, with
  `(aucune parole détectée)` when nothing is recognised) and `transcript_srt`
  (`<stem>_pyclean-audio.srt`) **only if the model returned timestamps**; both
  are included in the ZIP when they exist.
  - **Served names ≠ on-disk names**: `DOWNLOADS` renames on download
    (original → `{stem}.wav`, enhanced → `{stem}_pyclean-audio.wav|.mp3`), and
    `_build_folder_zip` writes the SRT as `{stem}.srt` (without the
    `_pyclean-audio` suffix). Stem collisions inside one subfolder are suffixed
    `_2`, `_3`… both for the output folder and for the ZIP.
  - **Name sanitising**: `_safe_relpath()` strips `..` and absolute paths from
    multipart names; in single-file mode the upload name is additionally reduced
    to `[A-Za-z0-9._-]` and 80 chars (regex in `enhance()`), while `job["stem"]`
    — hence the download names — keeps the original name. `_save_upload()` writes
    in 1 MB chunks, accounts a **shared** budget across the files of one request,
    and skips an empty file (folder mode) instead of failing.
  - **Queue**: a bounded `queue.Queue` (`PYCLEAN_QUEUE_MAX`) and a **single**
    worker thread. The target receives the job as its **first** argument
    (`_enqueue` prepends it) — otherwise `_run_job`/`_run_folder_job` fails. The
    returned `queue_position` is a snapshot taken at submission, never
    recomputed. `_worker` catches exceptions so a failed job does not vanish
    from the UI (`state="error"`).
  - **Retention**: `_purge_expired()` (asyncio task of the `lifespan`) deletes
    finished jobs after `PYCLEAN_JOB_TTL`; `_purge_orphans()` empties
    `data/jobs/` at startup (jobs live in memory, anything else is
    unreachable); `_make_room()` evicts the oldest finished jobs when
    `PYCLEAN_JOB_MAX` is reached, and returns 503 only if everything is active.
  - **Transcription unavailable**: `_check_transcribe()` returns **400** if
    `transcribe` is requested while NeMo is not installed (`is_available()`),
    instead of accepting a job that would fail mid-flight with
    `ModuleNotFoundError: nemo`. The UI uses the same signal to disable the
    checkbox before submitting.
  - **Translatable keys**: every job and every folder entry carries
    `stage_key` / `stage_args` (plus `error_code` / `error_params` on failure)
    next to the English `stage` / `error` text, see `app/messages.py`. A folder
    job nests the current file's stage in
    `stage_args = {index, total, name, inner_key, inner_args}` (`_file_stage`),
    otherwise the “File 2/12 — a.mp3: …” line would stay half in French.
    `_stage` takes the lock, `_stage_locked` must be called **under**
    `JOBS_LOCK` (`_run_folder_job` already holds it: calling it from an
    already-locked closure deadlocks); `_fail(job, exc)` writes the error and its
    key in one go.
  - **HTTP errors**: `ApiError(status, code, **params)` (an `HTTPException`
    subclass) carries `code`/`params`; the `_api_error_handler` adds them to the
    body (`{"detail", "code", "params"}`) because FastAPI's default handler
    only serialises `detail`. Never match on the wording of a `detail` to decide
    a behaviour (the old `if "vide" in str(e.detail)` is now
    `if e.code == "empty_file"`).
  - `GET /api/jobs/{id}` returns `_snapshot(job)`: a **deep** copy of the
    `artifacts`, because the worker thread keeps mutating them while FastAPI
    encodes the response (“dictionary changed size during iteration”).
    `_`-prefixed keys and server paths are never exposed (artifacts are reduced
    to their file name, `zip` to a boolean).
  - A folder job has `kind="folder"` and a `files[]` list (state, artifacts and
    progress per file); files are processed sequentially and the source/output
    paths are passed **outside** the entry dict (parallel `sources` list).
    `keep_original=False`: there is no A/B in folder mode, so the original
    48 kHz WAV is deleted (−43 % of disk).
- `app/messages.py` — the **translation contract** between the server and the
  page: `STAGES` (progress stages) and `ERRORS` (error codes) are the reference
  catalogues, their wording is **English** (what the CLI prints, and what
  `GET /api/jobs/{id}` returns in `stage`/`error`), and `MediaError` carries
  `code` + serialisable `params`. Unknown key -> the key itself is returned
  (`stage_text`/`error_text` never raise). Any key added here must also be added
  to the dictionary of `static/index.html` — with the *same English wording* and
  a French translation — otherwise `tests/test_i18n.py` fails. Keep one
  catalogue per side: English here, English + French on the page.
- `app/cancel.py` — `JobCancelled` + `raise_if_cancelled()` (checkpoints between
  blocks, between chunks, on every progress line) and **`GPU_LOCK`**, the global
  lock taken around `enhance_wav` **and** transcription: per-model locks are not
  enough. `main.py`'s single worker already serializes jobs, so this lock is
  never contended today — it stays as a safety net for anyone calling
  `process_file` directly (script, test): without it, enhancement and
  transcription could overlap and both models would be resident on the 8 GB
  card at once.
- `app/config.py` — every limit and the retention settings, read from the
  environment (see the “Configuration” table in README.md); an unreadable or
  zero value falls back to the default.
- `app/transcriber.py` — wrapper around the Parakeet TDT transcription model
  (`nvidia/parakeet-tdt-0.6b-v3`, NeMo checkpoint loaded with
  `nemo.collections.asr.models.ASRModel.restore_from` from the local HuggingFace
  cache, downloaded if absent; `half()` on CUDA). Singleton + lock like
  `enhancer.py` (shared VRAM), lazy load, `transcribe(wav) -> Transcript`. The
  `nemo` import stays in `_load()` (heavy). `is_available()` (free function)
  answers “is NeMo installed?” with `importlib.util.find_spec` — no import, so
  it is nearly free even when called on every `GET /api/status` (the page polls
  every 2 s); the result is published in `status()["available"]` and consumed by
  `_check_transcribe()` (`app/main.py`) and by the UI checkbox.
  **The chunking lives here too**: `CHUNK_SEC = 30` s blocks overlapping by
  `OVERLAP_SEC = 10` s, read in streaming on a single `sf.SoundFile` (`seek`),
  resampled to 16 kHz per chunk, `model.transcribe([arr_numpy],
  return_hypotheses=True, num_workers=0, timestamps=True)`, progress emitted per
  chunk. `plan_chunks()` returns four bounds per chunk
  `(read_start, read_stop, kept_start, kept_stop)`: the beginning of a blob is
  where Parakeet is least reliable, so it is not kept in the current chunk (it
  was already transcribed at the end of the previous one); the kept windows tile
  `[0, frames)` with no gap **and no duplicate**. `to_cues(ts, offset, t0, t1)`
  applies the window — `t0`/`t1` are **relative to the chunk read**, which
  starts `OVERLAP_SEC` before the kept window, and `offset` shifts it back to
  file time. The `.txt` text is built from the same words as the subtitles (falling
  back to the model's raw text when there are no timestamps). `timestamps=True`
  fills `hypothesis.timestamp` (a `word` / `segment` dict): `to_cues()` turns it
  into readable subtitles (sentences if the model gives any, otherwise word
  grouping; times forced to increase, TDT alignment is not monotonic) and
  `render_srt()` produces the `.srt`.
- `app/cli.py` — command line interface (reuses `processor`); prints the English
  `stage_text()` (it translates nothing), so its `_cb` accepts the four
  arguments of `on_stage`; accepts a file OR a folder (recursive, continues on
  error, final recap); `--format wav|mp3` (default: wav); `--transcribe`
  (default: off) adds `<stem>_transcript.txt` and `<stem>_pyclean-audio.srt`.
- `static/index.html` — **multilingual** UI (English by default, French second)
  with: drag & drop of a file or a folder (recursive reading via
  `webkitGetAsEntry` / `webkitdirectory`), **linked** A/B (both players follow
  each other on play/pause/seek, “▶ Source” / “▶ Enhanced” buttons, “linked”
  checkbox — “▶ Origine” / “▶ Amélioré” in French), “Transcribe the cleaned
  audio” checkbox (off by default, **disabled together with the `./run.sh --asr`
  command when NeMo is missing** — `setTranscribeEnabled()` on the same
  `available` as the API), queue position, “Cancel processing” button, “Delete
  results” button, job polling, per-file result list + ZIP download.
  - **i18n**: two flags 🇬🇧 / 🇫🇷 **under the LavaSR badge** (`applyLang`).
    Default language `en`, remembered in `localStorage` (`pyclean.lang`);
    `document.documentElement.lang` and `<title>` follow. Translations live in a
    `<script type="application/json" id="i18n">` block: a `server` section
    (mirror of `app/messages.py`) plus a `ui` section (keys prefixed with
    `ui.`), merged into one flat table per language (`buildTables`; `ui` wins on
    a collision). Fixed labels: attribute `data-i18n="key"` (the **English text
    is hardcoded in the page**, it is what shows before the script runs —
    `tests/test_i18n.py` compares it with the dictionary). Dynamic labels:
    `T(el, key, params)` or `bind(el, fn)`; the folder **title** needs the whole
    job, so it is recomputed by `paintFolderTitle()`, while the folder **note**
    is two concatenated keys and uses `bind` (never `T` with a built string —
    that would leave a raw key in the UI). Switching language replays the
    bindings (`replayBindings`) **without rebuilding the DOM**, so A/B playback
    and the transcript are preserved. `bind()` keeps at most one binding per
    node and `pruneBindings()` drops the ones of removed nodes (folder rows,
    download buttons).
  - **API keys**: `stageText({key, args, raw})` renders a job stage (`raw` = the
    server's text, used when the key is unknown) and handles the nested
    `inner_key`/`inner_args` stage of a folder job; `errorText(code, params, raw)`
    does the same for `error_code`/`error_params` and for the body of an error
    response (`code`/`params`, see `ApiError`); `detailText(detail)` flattens
    FastAPI's 422 body (a list of objects) so the error box never shows
    `[object Object]`; `jobStage(job)` is the `{key, args, raw}` triple for a job
    or a folder entry.
- `tests/` — pytest (no model loaded: `app.enhancer` / `app.transcriber` are
  replaced by fake modules in `conftest.py`); `ruff check` is the only lint that
  counts. `tests/test_messages.py` and `tests/test_i18n.py` cover the dictionary
  (server keys present in both languages, no key shadowing across sections, same
  parameter names in both languages, the page's hardcoded English up to date,
  flags located under the badge and in order).
  `tests/test_asr_constraints.py` reads the source of the watched
  `nemo_toolkit[asr]` install sites (Dockerfile, `run.sh`, `requirements.txt`,
  the two docs) and fails if one of them does not carry the
  `requirements-asr.txt` constraint — it is a source-level test, no network and
  no JS runtime, see "ASR constraints". Its scope is that list: a new site in a
  **new** file is not watched, so widen the list in the same commit that adds
  the installer.

## Model constraints to respect

- The model always consumes **16 kHz mono** (`Stream16k`: source sample rate →
  `input_sr` → 16 kHz) and produces **48 kHz** (factor 3). Any change to the
  resampling path must preserve that chain.
- Before every inference, set the refiner:
  `model.bwe_model.lr_refiner = FastLRMerge(device, cutoff=cutoff,
  transition_bins=1024)` with `cutoff = input_sr // 2` by default.
- A chunk's output must be brought back to **exactly 3 × n_in** samples
  (trim/pad) — the library can drift by a few samples.
- Do NOT use the library's `model.enhance(..., batch=True)`: it pads without
  trimming the end. `enhancer.py`'s own chunking replaces that mode.
- Inference is serialized by `LavaEnhancer._lock` (global refiner state + VRAM).
  Never run inference outside that lock, nor outside `GPU_LOCK` (which also
  covers Parakeet).
- `model.enhance` expects a **[batch, time]** tensor: passing a 1-D vector
  crashes vocos (`Padding size 2 is not supported for 1D input`).
- **Parakeet (transcription) — three things not to break:**
  - *Short, overlapping chunks.* Two opposing reasons: (a) **VRAM** — the
    encoder's activations grow linearly with duration: one hour in a single blob
    peaks at ~7.5 GB and OOMs on an 8 GB card; (b) **accuracy** — the checkpoint
    is trained on `max_duration: 40 s` utterances (`model_config.yaml`). Beyond
    that, Parakeet loses the extremities and *condenses* whole passages. Measured
    on the same 300 s audio (enhanced audio checked bit for bit, chunks read in
    full):

    | chunk | 1st word | words in [0,30 s] | words in 120-300 s | end |
    |---|---|---|---|---|
    | 300 s | 51 s | 62 | — | 299 s |
    | 150 s | 26 s | 92 | — | 300 s |
    | 60 s  | 0.8 s (max 15.9 s) | 109 | 190 | **284 s (16 s lost)** |
    | 30 s  | 0.7 s (max 6.5 s) | 95 | **254** | 300 s |

    `CHUNK_SEC = 30` with `OVERLAP_SEC = 10`: each chunk is read with 10 s of
    upstream context and that window is discarded, so the beginning of a chunk
    is always taken from the previous one, and the tail (which 60 s chunks lost)
    is taken from the next one. `OVERLAP_SEC` must stay **≥ the head silence**
    (6.5 s measured at 30 s), otherwise the join loses the first words. Do not
    lengthen the chunks to “save time”: 30 s costs 1.5× more audio through the
    model (~20 s of GPU transcription for 54 min) and brings the VRAM peak down
    from 5.0 GB to 2.4 GB. Never switch to a single blob.
  - *No device hopping.* Looping `cuda → cpu → cuda` corrupts the model:
    `illegal memory access`, then `⁇` output. The device is chosen at load time
    and never moved again. (NeMo builds the model on CPU and places it on CUDA at
    the first inference: that is normal, 0 MB allocated at load time does not
    indicate a failure.)
  - *`gc.collect()` + `torch.cuda.empty_cache()` after `_load()`*: without it
    the fp32 residue (~2.4 GB) stays reserved and the card is unusable for a
    second job. After gc: ~1.2 GB (fp16 weights).
- `soundfile.SoundFile`: open for writing with an explicit `mode="w"`
  (`sf.SoundFile(path, "w", samplerate=48000, channels=1, subtype="PCM_16")`)
  and compare lengths with `numel()` (not `size()`).

## Environment

- Python 3.11.16 in `.venv` (torch 2.14.0+cu130); the system Python (3.14) is only
  used for utilities. Always run through `.venv/bin/python`.
- NVIDIA GPU + CUDA detected automatically (`torch.cuda.is_available()`), CPU
  otherwise. The first download of the LavaSR model (**~115 MB**, blobs under
  `~/.cache/huggingface/hub/models--YatharthS--LavaSR`) comes from HuggingFace
  and is cached; the `lifespan` preloads it in a thread.
- Measured LavaSR throughput (RTX 2000 Ada, 8 GB, torch 2.14+cu130): **~200×
  real time** — 300 s of audio (5 chunks of 60 s) enhanced in 1.41 s without the
  denoiser, 1.57 s with it. The wall-clock time of a short job is therefore
  mostly ffmpeg (48 kHz decode, MP3, remux), not inference.
- Transcription (`--transcribe` option / web checkbox): Parakeet TDT 0.6B v3 via
  NeMo (`nemo_toolkit[asr]`, **`./run.sh --asr`**, absent from the base install),
  `.nemo` checkpoint `nvidia/parakeet-tdt-0.6b-v3` (**~2.4 GB** on disk) loaded
  from the local HuggingFace cache (`~/.cache/huggingface/hub`, overridable with
  `HF_HOME`) when already there, downloaded otherwise. `half()` on CUDA (~1.2 GB
  of weights); transcription in 30 s chunks overlapping by 10 s (1.5× more audio
  through the model), bounded VRAM — **2.4 GB peak measured** with LavaSR
  loaded, whatever the number of chunks, against 5.0 GB with 300 s chunks. 54
  min of audio transcribes in ~20 s on GPU. CPU only: measured at ~13× real time
  (230 s of audio in 18 s on 20 cores) — the GPU is still preferable. NeMo 3.0:
  a local path is loaded with `ASRModel.restore_from` (`from_pretrained` only
  accepts HuggingFace repo ids).
- `ffmpeg` / `ffprobe` required in the PATH.
- LSP/IDEs often use the system Python: “Import "torch" could not be resolved”
  errors are expected and irrelevant.
- To restart the server from a shell: `pkill -f "uvicorn app[.]main"` (the
  bracket notation avoids killing your own command line) then start it detached:
  `setsid bash -c '(.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 >> /tmp/server.log 2>&1 < /dev/null &)'`.

## Docker

Two published images, `ghcr.io/aureld16/pyclean-audio:{cpu,gpu}`, both with
transcription. `Dockerfile` (multi-stage `base` + `builder-cpu` / `builder-gpu`
→ `cpu` / `gpu`, plus an opt-in `test`), `docker-entrypoint.sh`,
`.dockerignore`, `compose.yaml` (two services, no `build:` — it pulls),
`requirements-base.txt`, `.github/workflows/docker-publish.yml`. It serves the
same app on `0.0.0.0`; it never calls `run.sh` (that installs at start-up and
hardcodes `--host 127.0.0.1`). Rules to keep if you touch it:

- **The variant is a `--target`, never a tag guess**: `--target cpu` builds the
  CPU wheels, `--target gpu` the CUDA ones, so a published tag cannot disagree
  with its content. **`cpu` is the default target** — `docker build .` must keep
  producing the image that runs anywhere. Because a stage can only derive from a
  stage declared *before* it, that default is the last stage of the file: the
  `FROM cpu AS default` alias. Same filesystem, same config, no extra layer —
  only the image *id* differs, because the history records the extra stage.
- **Everything shared is written once, in `base`**: `ENV`, `USER`, `WORKDIR`,
  `COPY app/ static/`, `EXPOSE`, `HEALTHCHECK`, `ENTRYPOINT` and `CMD`. The two
  variants differ only by `COPY --from=builder-{cpu,gpu} /opt/venv`. Do not
  duplicate that block: two copies of `--host`/`--workers`/`HEALTHCHECK` are two
  chances to disagree (that class of bug has already been corrected twice here).
- **`--host 0.0.0.0` and `--workers 1`, always.** Not an optimization: the queue
  is one in-process `queue.Queue` with a single consumer, job state is a dict,
  and both models are lock-guarded singletons — a second worker duplicates the
  models in VRAM and loses the jobs. Never change the worker's count.
- **The entrypoint must `exec` uvicorn.** The exec-form `CMD` cannot expand
  `${PORT}`, so the script resolves it and then *replaces itself*; uvicorn is
  then PID 1 and `docker stop`'s SIGTERM reaches it. Drop the `exec` and every
  stop becomes a 10 s timeout + SIGKILL with a possibly orphaned ffmpeg. The
  script must also not create its own process group (`start_new_session` /
  `os.killpg` in `app/processor.py`). Note that the `exec` only guarantees the
  signal *arrives*: tearing the app down takes up to ~7 s with both models
  loaded (measured 7.2 / 4.2 / 1.9 s), so Docker's 10 s grace is not generous —
  on slower hardware a stop right after a transcription can still be SIGKILLed.
  The damage is limited (the job in flight is lost, which "results are
  ephemeral" already says).
- **`torch` is installed first, from `$TORCH_INDEX_URL`.** LavaSR depends on
  torch itself, so letting it resolve pulls the default CUDA wheel from PyPI and
  silently replaces it. The `builder-cpu` index is the **CPU** one, `builder-gpu`
  the **cu130** one (override with `--build-arg TORCH_INDEX_URL=…/cu126` for an
  older driver). Nothing else configures the GPU: the device is decided at model
  load by `torch.cuda.is_available()`, and the host needs the NVIDIA Container
  Toolkit (`--gpus all`, or the `deploy` block in `compose.yaml`).
- **Transcription is unconditional now.** `nemo_toolkit[asr]` is installed in both
  builders; there is no `INSTALL_ASR` arg and no image without it. It drags
  NeMo 3.0 + `transformers`, which constrains `huggingface-hub` to **1.33.0** in
  both images (2.x when NeMo is absent). That combination — LavaSR against
  hub 1.33.0 — is verified (imports, and a full enhancement + transcription job);
  re-verify it after bumping anything, and never paper over a conflict with
  `--no-deps`. The install also passes `-c requirements-asr.txt` (see "ASR
  constraints" below) — both builders `COPY` it next to `requirements-base.txt`,
  and dropping it breaks the build.
- **No volume on `/app/data`.** `_purge_orphans()` wipes `data/jobs/` at every
  start (jobs live in memory), so a volume there would look persistent and be
  empty after each restart. The only volume is the HuggingFace cache
  (`HF_HOME=/cache/huggingface`, mounted at `/cache`): ~115 MB for LavaSR,
  ~2.4 GB more once Parakeet has been downloaded.
- **Non-root (uid 1000) is required**, not decoration: `app/main.py:33-35`
  creates `data/jobs` at *import* time and HF needs a writable cache.
- **ffmpeg in `base`, so in every variant** (Debian bookworm, not Alpine: glibc
  for torch, `libmp3lame` for `libmp3lame0`, and `ffprobe` by bare name in
  `PATH`). It is what lets the `test` stage derive from `cpu` without installing
  anything: without ffmpeg, `requires_ffmpeg` silently skips.
- **`test` derives from `cpu`, not from the builder.** It installs
  `requirements-dev.txt` as root (`USER root`, then back to `pyclean`), so the
  suite runs as the runtime user on the environment that is actually shipped.
  It is slow: NeMo has to be there.
- **Healthcheck is `GET /api/status`**, shell form so it reads the live `PORT` —
  `HEAD` returns 404 on every route of this app. The check runs *inside* the
  container, so it cannot see a `-p` mapping: with plain `docker run`, a `-e PORT`
  change must be matched on both sides of the mapping or the container looks
  healthy and is unreachable.
- **Published on `127.0.0.1`**: there is no authentication at all, so
  `0.0.0.0:8787` would expose an open upload/processing service.
- **The ghcr package is public**: both tags pull anonymously, no login (verified
  from a cold pull after dropping the local copies). A *new* package would be
  private by default and need the same one-click change on the package page —
  neither `GITHUB_TOKEN` nor a `write:packages` PAT can do it through the REST
  API (the visibility endpoint answers 404).
- Nothing is pinned (base tag, deps, LavaSR ref) — consistent with the rest of
  the project. The images are not bit-reproducible; don't invent a lock file.
  `linux/amd64` only. The single exception is `requirements-asr.txt`, and it is
  an exception because the unpinned resolution no longer builds at all — see
  "ASR constraints" below.

- **Never write an exact count of packages or wheels in the documentation** —
  "+91 packages", "16 CUDA wheels", "208 tests" all rot on the next build or the
  next test. Write "several", `~N`, or the exact version of a single package
  *when it has been read out of the image*. This is a recurring failure here:
  three such numbers have already had to be corrected.

### Publishing / updating the images

Two tags are always published, `:cpu` and `:gpu`, both public on `ghcr.io`; no
`:latest`. The README documents how to *use* them, not how they get there — this
is the whole procedure.

- `.github/workflows/docker-publish.yml` rebuilds and pushes them, on
  `workflow_dispatch` (pick one variant) or on a `docker-v*` tag (both).
- **A versioned run also publishes `:<version>-<variant>`** (e.g. `:v1.01-cpu`),
  so the release is visible on the package page next to its images. The version
  is read from the git tag on a tag push (`docker-v1.01` → `v1.01`), or typed in
  the `version` input of a manual run — which is how a version is re-released
  without moving its tag (a dispatch runs the workflow file *of the dispatched
  ref*, so dispatching on an old tag re-runs that old file). The suffix is not
  cosmetic: the two matrix legs build two different images, so a bare `v1.01`
  would belong to whichever leg pushed last.
- **The image reference is assembled lower-cased, never inlined as
  `ghcr.io/${{ github.repository }}`**: that expression keeps the case of the
  repository URL (`AurelD16/pyclean-audio`) and an OCI reference must be lower
  case, so buildx rejects it before building anything ("repository name must be
  lowercase"). It is derived from `github.repository` in the `ref` step so a
  repository rename cannot republish under a stale name.
- It authenticates with the repository's own `GITHUB_TOKEN`
  (`permissions: packages: write`) — no secret to configure — and builds
  `linux/amd64` only, one matrix leg per variant.
- **`GITHUB_TOKEN` is enough, but two things outside the workflow file decide
  whether a run can actually publish**, and neither fails where you would look:
  1. the repository's default workflow token must be allowed to write (Settings →
     Actions → General → Workflow permissions; this repository is set to *Read and
     write permissions*, the workflow only ever asks for `contents: read` +
     `packages: write`);
  2. the **package** must grant this repository access: package settings →
     **Manage Actions access** → Add repository → role **Write**.

  Miss (2) and the image builds perfectly, layers and all, then the push is
  refused with `denied: permission_denied: write_package` — which reads like a
  registry, token or visibility problem and is none of them. A package whose
  first image was pushed *by hand* (a PAT, the local procedure below) does not
  get Actions access for free; only a package created by a workflow run does.
  There is no REST API for (2) on a package scoped to a personal account: it is
  a click in the settings page, so check it before trusting a release.
- Locally: `docker login ghcr.io` then `docker tag` + `docker push` the target
  you built. Verify with a cold pull after dropping the local copy.
- A package is private until its visibility is set to public, once, by hand on
  the package page; the workflow only warns when that has not been done.

## ASR constraints

`requirements-asr.txt` holds the two floors of the ASR install —
`transformers>=5` and `tokenizers>=0.21` — and it is the only file they are
written in. Every install site passes it with `-c requirements-asr.txt`: the two
builders of the `Dockerfile`, the two `uv pip install` of `run.sh` (`--asr`, on
an existing `.venv` and on a fresh one — `run.sh` starts with `cd
"$(dirname "$0")"`, so the relative name works), and `requirements.txt`, which
carries the `-c` line itself so `pip install -r requirements.txt` is covered.
`tests/test_asr_constraints.py` reads all of them and fails if one site ever
loses the constraint, so a new site cannot reintroduce the hazard silently.

Why a floor is needed at all, when nothing else is pinned: an **unpinned**
`nemo_toolkit[asr]` does not resolve to a set that can be installed on cp311.
The resolver takes the newest `huggingface-hub` (2.x) first, then the newest
`transformers` still compatible with hub 2.x — **4.12.2** — which drags
`tokenizers` back to **0.10.3**, a 2021 release with no cp311 wheel. The install
then builds it from source and stops with

```
error: Failed to build `tokenizers==0.10.3`
       running build_rust
       error: can't find Rust compiler
```

The symptom names neither the cause nor the remedy, which is why it is written
down here. With the floors, the same resolution gives `transformers` 5.x +
`tokenizers` >= 0.21 + `huggingface-hub` 1.33.0 — the combination this file
already documents as verified. Reproduce either side with:

```bash
printf 'nemo_toolkit[asr]\n' > /tmp/asr.in
uv pip compile --python-version 3.11 -c requirements-asr.txt /tmp/asr.in   # constrained
uv pip compile --python-version 3.11 /tmp/asr.in                         # the break
```

Two floors rather than one: either alone is enough today, and keeping both
means neither package can walk back into the 2021/2022 set on its own. This is
**not** a lock file — a floor per package, no other version touched, no
`--no-deps`, no `--resolution`, no Rust in the builders. If a floor ever becomes
unsatisfiable the install must fail **loudly**: a silent fallback to
`transformers` 4.x is worse than a red build, because the image would still claim
to ship NeMo 3.0.

## Quick tests

```bash
# degraded 8 kHz signal + test video
ffmpeg -f lavfi -i "sine=frequency=350:duration=6" -af "lowpass=2400,aresample=8000" test_8k.wav
ffmpeg -f lavfi -i "testsrc=duration=5:size=320x240:rate=25" -f lavfi -i "sine=frequency=300:duration=5" -af lowpass=2500 -c:v libx264 -c:a aac test.mp4

# web (single file: WAV and MP3 are both offered)
JOB=$(curl -s -X POST -F "file=@test_8k.wav" http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB
curl -s -o out.wav http://127.0.0.1:8787/api/jobs/$JOB/file/enhanced_wav
curl -s -o out.mp3 http://127.0.0.1:8787/api/jobs/$JOB/file/enhanced_mp3

# CLI (default: wav ; --format mp3 adds <stem>_pyclean-audio.mp3)
.venv/bin/python -m app.cli test.mp4 -o out/
.venv/bin/python -m app.cli test.mp4 -o out_mp3/ --format mp3

# folder (recursive) — CLI
mkdir -p tdir/under && cp test_8k.wav tdir/ && cp test.mp4 tdir/under/
.venv/bin/python -m app.cli tdir -o out_dir/
.venv/bin/python -m app.cli tdir -o out_dir_mp3/ --format mp3

# folder (recursive) — web (default output format: MP3 ; -F "output_format=wav" for WAV)
JOB=$(curl -s -X POST \
  -F "files=@test_8k.wav;filename=root/a.mp3" \
  -F "files=@test.mp4;filename=under/b.mp4" \
  http://127.0.0.1:8787/api/enhance_folder | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB            # files[]: state per file, output_format
curl -s -o a.mp3  http://127.0.0.1:8787/api/jobs/$JOB/file/0/enhanced_mp3
curl -s -o all.zip http://127.0.0.1:8787/api/jobs/$JOB/zip   # SRT included as <stem>.srt

# transcription (single file): -F "transcribe=true" → transcript and
# transcript_srt artifacts (the .srt only if the model returned timestamps)
JOB=$(curl -s -X POST -F "file=@test_8k.wav" -F "transcribe=true" \
  http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB | python3 -m json.tool

# error paths (now carry a translatable code next to the English detail)
curl -s -X POST -F "file=@test_8k.wav" -F "input_sr=abc" http://127.0.0.1:8787/api/enhance   # 422
curl -s http://127.0.0.1:8787/api/jobs/inconnu                                                # 404 + code

# OOM regression: long file (1 h) + transcription, watch VRAM
nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits -l 2 > /tmp/vram.log &
JOB=$(curl -s -X POST -F "file=@long.mp3" -F "transcribe=true" \
  http://127.0.0.1:8787/api/enhance | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB          # state=done, artifacts
awk '{if($NF+0>m)m=$NF+0} END{print "peak VRAM: " m " MiB"}' /tmp/vram.log
```

These test commands write `test_8k.wav` and `test.mp4` (~426 MB) at the repo
root: `.gitignore` covers `*.mp4` but **not** `*.wav` (it lists `*.mp3`,
`*.mp4`, `data`, `.venv`, `__pycache__`, `.pytest_cache/`, `.ruff_cache/`), and
the other outputs (`out*/`, `tdir/`) have to be deleted by hand. Note that
`.gitignore` itself is not committed yet.

Validity criteria:
- output duration == input duration (to within < 1 frame at 48 kHz);
- MP3: same duration as the WAV, 48 kHz mono, ~192 kbit/s;
- no discontinuity at the chunk joins (local max|Δ| ≤ 99.99th percentile of the
  whole signal);
- the high band (> input Nyquist) must hold more energy than a plain sinc
  upsampling of the same input (proof that the BWE happened);
- for videos: identical video stream (`-c:v copy`), AAC audio at 48 kHz;
- transcription of a long file: `state=done`, non-empty transcript, timestamped
  and increasing SRT, **first word < 5 s and last word within < 1 s of the
  end** (otherwise the chunking loses the extremities of the chunks), no gap
  > 8 s in the SRT where the audio energy is > 5 % of the median, VRAM peak
  ≤ ~2.5 GB on an 8 GB card and **constant** whatever the number of chunks;
- queue: two consecutive submissions → the second stays `queued` while the first
  runs, then goes `done`;
- cancellation: `POST /api/jobs/{id}/cancel` → `state=cancelled` and the job
  folder disappears from `data/jobs/`;
- retention: `PYCLEAN_JOB_TTL=20 PYCLEAN_PURGE_INTERVAL=5` → the job becomes a
  404 and `data/jobs/` empties.

## Known limitations (also documented in the “Limitations” section of README.md)

- 48 kHz mono output; videos → MP4 (existing subtitles and extra tracks lost).
- Max duration ~10 000 s, max file size 2 GB, folder 500 files / 8 GB.
- HEAD returns 404 on the FastAPI routes (no browser impact: the page uses GET).
- One job at a time (queue); `keep_original=False` in folder mode (no A/B), so
  `original_wav` is never produced there.
- The web UI is **English by default**, French second: both flags sit under the
  LavaSR badge and the choice is remembered per browser. Everything else
  (server wording, CLI, docs) is English with no language switch; the only
  French left is the UI's translations and the transcript placeholder
  `(aucune parole détectée)` when the model recognises nothing — an artefact of
  the file, not of the interface.
- MP3 encoding and video remux stay **sequential**: parallelising them gains
  only ~0.4 s on a 5-minute video (measured), for a far more verbose progress
  multiplexing. Revisit if the codec changes.
- Transcription is unavailable without `nemo_toolkit[asr]`: `./run.sh --asr`
  adds it to the existing `.venv`. While it is missing, the API refuses
  `transcribe=true` with **400** and the UI disables the checkbox — no more
  `ModuleNotFoundError: nemo` mid-job. The `.srt` is only produced if the model
  returns timestamps; the `.txt` always exists (possibly with
  `(aucune parole détectée)`). Subtitles are not **muxed** into the MP4: it is
  a separate file to load.
