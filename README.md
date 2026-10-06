# pyclean-audio

**Local, single-user audio restoration for audio and video files.** Runs entirely
on your machine: a FastAPI server and one HTML page, no cloud, no account.

On top of that, it can **transcribe the cleaned audio** with
[Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
(NVIDIA, via NeMo): a plain-text transcript, plus timed `.srt` subtitles when
the model returns timestamps — about 20 s of GPU time for 54 min of audio. It
is one flag away: `./run.sh --asr` → [Transcription](#transcription-optional).

Uses [LavaSR v2](https://github.com/ysharma3501/LavaSR) (`LavaEnhance2`) to
extend the bandwidth of degraded recordings up to **48 kHz**, with optional
denoising (UL-UNAS). An NVIDIA GPU is used automatically when available.

Inference is cheap: **~200× real time** on GPU (measured: 300 s of audio
enhanced in 1.4 s, 1.6 s with the denoiser, on an 8 GB card). For a short file
the wall-clock time is therefore dominated by ffmpeg decoding/encoding, not by
the model.

| Input     | Pipeline                                                            | Outputs                                                  |
| --------- | ------------------------------------------------------------------- | -------------------------------------------------------- |
| Audio     | decode → mono 48 kHz → LavaSR v2                                    | Enhanced MP3 192 kbit/s + WAV 48 kHz, A/B against source  |
| Video     | extract audio → LavaSR v2 → remux (video stream copied, untouched) | MP4 with the enhanced audio, plus standalone MP3 and WAV  |

Both kinds of input can additionally yield a transcript and timed subtitles
(`--transcribe`, or the web checkbox).

## Contents

- [Quick start](#quick-start)
- [Docker](#docker)
- [Local](#local)
- [Transcription (optional)](#transcription-optional)
- [Output files and download names](#output-files-and-download-names)
- [Command line](#command-line)
- [Whole folders](#whole-folders)
- [Web interface](#web-interface)
- [Configuration](#configuration)
- [Queue, cancellation, retention](#queue-cancellation-retention)
- [How it works](#how-it-works)
- [Measured performance](#measured-performance)
- [Limitations](#limitations)
- [Development](#development)
- [Project layout](#project-layout)

## Quick start

Two ways to run it — both entirely on your machine, and neither asks for an
account:

- **[Docker](#docker)** — two ready-made images (`:cpu` for any machine, `:gpu`
  for an NVIDIA card), pulled from a public registry. Nothing to install.
- **[Local](#local)** — `ffmpeg`, `uv` and Python 3.11 on your machine, then
  `./run.sh` builds the environment and starts the server.

Both serve the same page on <http://127.0.0.1:8787>.

## Docker

Two images on GitHub Container Registry, pulled without any account or token.
Compose pulls them from the registry; it builds nothing.

```bash
docker compose up -d cpu     # or: docker compose up -d gpu
docker compose logs -f
docker compose down          # add -v to drop the model cache too
```

Then open <http://127.0.0.1:8787>.

| Image | Size | Use |
| ----- | ---- | --- |
| `ghcr.io/aureld16/pyclean-audio:cpu` | ~0.7 GB (3.2 GB unpacked) | any machine, no GPU — **the default** |
| `ghcr.io/aureld16/pyclean-audio:gpu` | ~3.5 GB (10.7 GB unpacked) | NVIDIA GPU + NVIDIA Container Toolkit |

### The two images, and how they differ

- **Transcription is in both.** There is no “without transcription” image any
  more: enhancement *and* Parakeet transcription work out of the box.
- **The only technical difference is the engine** — `:cpu` carries CPU `torch`,
  `:gpu` carries CUDA 13.0 `torch`. Same application, same page, same
  environment variables, same behaviour.
- **Nothing to configure.** The app picks the device itself at load
  (`torch.cuda.is_available()`), so `:gpu` only needs `--gpus all`, which
  compose passes for you.
- **The real difference is speed** — see
  [Measured performance](#measured-performance): enhancement runs at **~200×
  real time** on GPU, and Parakeet transcribes 54 min of audio in ~20 s on GPU
  against **~13× real time** on CPU. `:cpu` is therefore perfectly usable; it is
  a choice, not a fallback.
- `:gpu` needs a driver that supports CUDA 13.0, validated on an RTX 2000 Ada.
  On an older driver, see [Development](#development).
- **One container at a time**: both services listen on 127.0.0.1:8787.

Without compose:

```bash
docker run -d --name pyclean-audio \
  -p 127.0.0.1:8787:8787 \
  -v pyclean-hf:/cache \
  ghcr.io/aureld16/pyclean-audio:cpu

# the GPU variant is the same command plus --gpus all (NVIDIA Container Toolkit
# on the host) and the :gpu tag
docker run -d --name pyclean-audio --gpus all \
  -p 127.0.0.1:8787:8787 -v pyclean-hf:/cache \
  ghcr.io/aureld16/pyclean-audio:gpu
```

> [!WARNING]
> **There is no authentication anywhere in the app.** Keep the published port on
> `127.0.0.1`. `0.0.0.0:8787` would expose an unauthenticated upload, processing
> and download service to your whole network. To reach it from another machine,
> put an authenticating reverse proxy in front of it.

> [!NOTE]
> **Results are ephemeral, like the bare app**: jobs live in memory and
> `data/jobs/` is wiped at every start, so nothing is mounted on `/app/data` and
> `docker compose down` / a restart discards every result. The only volume worth
> having is the HuggingFace cache (`/cache`, `HF_HOME=/cache/huggingface`): the
> LavaSR weights (**~115 MB**) and, on the first transcription, the Parakeet
> checkpoint (**~2.4 GB**) are downloaded once into it and reused across restarts
> and recreations.

The image has a `HEALTHCHECK` on `GET /api/status`:

```bash
curl -sf http://127.0.0.1:8787/api/status   # {"status":"ready","device":"cpu",…}
docker inspect -f '{{.State.Health.Status}}' pyclean-audio   # healthy
```

A few things worth knowing before you change anything:

- `PORT=9000 docker compose up -d cpu` publishes and listens on 9000. With plain
  `docker run`, change **both** sides of the mapping
  (`-e PORT=9000 -p 127.0.0.1:9000:9000`): a mismatch still reports `healthy`,
  because the healthcheck runs inside the container and cannot see the publish.
- The Parakeet checkpoint (2.4 GB) arrives on the first job that asks for a
  transcript; `PYCLEAN_PRELOAD_ASR=1` fetches it at boot instead.
- The image runs as a **non-root user** (uid 1000), and `ffmpeg`/`ffprobe` are
  already inside it — the app calls them by bare name.

## Local

The path without Docker: a Python environment on your machine, created by
`run.sh`.

**Requirements:** [`ffmpeg`](https://ffmpeg.org/) and `ffprobe` on your `PATH`,
[`uv`](https://docs.astral.sh/uv/), and Python 3.11 (created for you by `uv venv`).
A CUDA GPU is optional.

```bash
git clone <this-repo> pyclean-audio
cd pyclean-audio
./run.sh
```

`run.sh` creates `.venv`, installs LavaSR + FastAPI + uvicorn on first run
(~4 GB of dependencies, a few minutes), and starts the server on
<http://127.0.0.1:8787> (it opens your browser if it can).

The first launch downloads the LavaSR weights from HuggingFace
(`YatharthS/LavaSR`, ~115 MB) and caches them; later launches are offline-ready.

```bash
./run.sh --help          # usage
./run.sh --asr           # also install the transcription models (optional)
PORT=9000 ./run.sh       # custom port

# run the server without the wrapper (e.g. under systemd)
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Then drop an audio or video file on the page, and compare the result with the
source using the two linked players.

> [!TIP]
> Transcription needs an extra dependency (NeMo), which is **not** installed by
> default because it weighs several GB and pins its own `torch` version.
> `./run.sh --asr` adds it to the existing `.venv` without recreating it.
> Until then, the “Transcribe the cleaned audio” checkbox is disabled and says why —
> see [Transcription](#transcription-optional).

## Transcription (optional)

Adds a plain-text transcript and, when the model returns timestamps, timed
subtitles — from the **enhanced** audio, using
[Parakeet TDT 0.6B v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
(NVIDIA) via NeMo, in fp16 on GPU (~1.2 GB of weights).

```bash
./run.sh --asr                       # installs nemo_toolkit[asr] into .venv
# equivalent, without the wrapper:
uv pip install -c requirements-asr.txt "nemo_toolkit[asr]"   # or: uv pip install -r requirements.txt
```

`-c requirements-asr.txt` is required: it keeps the install on `transformers`
5.x / `tokenizers` >= 0.21. Without it the resolver walks back to a 2021
`tokenizers` with no Python 3.11 wheel and the install stops with
`can't find Rust compiler` (the same constraint is used by the Docker images, see
[AGENTS.md](https://github.com/AurelD16/pyclean-audio/blob/main/AGENTS.md)).

`--asr` (or `PYCLEAN_WITH_ASR=1`) adds the dependency to the existing
environment — it never recreates `.venv`, so LavaSR stays installed. The
**~2.4 GB** Parakeet checkpoint is downloaded from HuggingFace on first
transcription and cached; preload it at boot with `PYCLEAN_PRELOAD_ASR=1`.

`GET /api/status` reports `transcriber.available`. When it is `false`, the
checkbox is disabled, the status badge reads “Parakeet not installed”, and the
API answers **400** instead of accepting a job that would fail on
`ModuleNotFoundError: nemo`.

If no speech is detected, the `.txt` contains the literal `(aucune parole
détectée)` and **no** `.srt` is produced. Subtitles are not muxed into the MP4:
they are a separate file to load in your player.

In the [Docker](#docker) images this dependency is already there: both variants
ship NeMo, so there is no `--asr` equivalent to pass.

## Output files and download names

On disk, inside the job folder:

| File                        | When                                              |
| --------------------------- | ------------------------------------------------- |
| `<name>_original.wav`       | single-file mode (A/B); deleted in folder mode    |
| `<name>_enhanced.wav`       | always                                            |
| `<name>_pyclean-audio.mp3`  | MP3 output (single file, or folder mode set to MP3) |
| `<name>_pyclean-audio.mp4`  | videos                                            |
| `<name>_transcript.txt`     | `--transcribe` / web checkbox                     |
| `<name>_pyclean-audio.srt`  | `--transcribe`, only if timestamps were returned  |

Downloaded files are renamed: the source is served as `<name>.wav`, the enhanced
audio as `<name>_pyclean-audio.wav` / `.mp3`. In folder mode the ZIP keeps the
original tree structure and subtitles are named `<name>.srt` (without the
`_pyclean-audio` suffix); two files with the same stem in the same subdirectory
(`a.mp3` + `a.mkv`) are suffixed `_2`, `_3`, … so nothing is overwritten.

## Command line

English output (no language switch: the page is the multilingual part).

```bash
.venv/bin/python -m app.cli musique.mp3 --denoise
.venv/bin/python -m app.cli video.mkv -o sortie/ --input-sr 8000
.venv/bin/python -m app.cli musique.mp3 --format mp3    # MP3 192 kbit/s
.venv/bin/python -m app.cli musique.mp3 --transcribe    # TXT + SRT
.venv/bin/python -m app.cli mon_dossier/                # recursive
```

| Flag | Default | Effect |
| ---- | ------- | ------ |
| `-o`, `--outdir` | `<name>_pyclean-audio/` next to the input | destination directory |
| `--denoise` | off | run the UL-UNAS denoiser before band extension |
| `--input-sr` | `16000` | simulated source bandwidth (`8000`, `16000`, `24000`) |
| `--cutoff` | auto (≈ half of `input_sr`) | refinement stage cutoff, in Hz |
| `--format` | `wav` | audio output: `wav` or `mp3` |
| `--transcribe` | off | also write `<name>_transcript.txt` and `<name>_pyclean-audio.srt` (needs NeMo, see [Transcription](#transcription-optional)) |

A failing file does not stop the others: folder mode ends with a recap and exit
code 1 if anything failed.

## Whole folders

**Web** — the “Folder (recursive)” tab: drop a directory (or a subdirectory) on
the page, or browse for one. Every audio/video file is detected **in all
subdirectories**. The output format is chosen there (MP3 192 kbit/s by default,
or uncompressed WAV 48 kHz) and applies to audio files; videos stay MP4. Files
are processed sequentially, each with its own download link, plus a
“Download everything (ZIP)” button that bundles the results in the original tree.

**CLI** — pass a directory instead of a file:

```bash
.venv/bin/python -m app.cli mon_dossier/            # → mon_dossier_pyclean-audio/
.venv/bin/python -m app.cli mon_dossier/ -o out/    # → out/ (tree mirrored)
```

> [!NOTE]
> There is **no A/B comparison in folder mode**: the source WAV is deleted right
> after enhancement (it only existed for the A/B players), which is about 43 %
> less disk. Only the enhanced audio, in the chosen format, is downloadable.

## Web interface

- **Two languages**, **English by default**: the two flags (🇬🇧 / 🇫🇷) under the
  LavaSR badge switch the whole page, including the progress stage, the error
  messages, the result list and the download buttons. The choice is remembered
  in the browser (`localStorage`); a fresh browser therefore starts in English.
- **Drag & drop** a single file, or a whole folder (recursive directory
  enumeration via `webkitGetAsEntry` / `webkitdirectory`).
- **Options**: “Reduce noise”, input bandwidth (8/16/24 kHz), “Cutoff (Hz,
  advanced)”, output format (folder mode), and “Transcribe the cleaned audio”
  (off by default; disabled with an explanation when NeMo is not installed).
- **A/B comparison**: the two players (source / enhanced) are **linked** — play,
  pause and seek are mirrored, and the “▶ Source” / “▶ Enhanced” buttons switch
  versions at the same timecode. Untick “linked” to decouple them.
- **Queue position**, “Cancel processing”, “Delete results”, polling of job
  progress, and a per-file result list with a ZIP download.

Switching language retranslates what is already on screen (results included)
without rebuilding the page, so playback is not interrupted.

The page never displays the server's wording directly: the API sends a **key**
plus its parameters (`stage_key` / `stage_args`, `error_code` / `error_params`,
and `code` / `params` in error bodies) and the page translates them, falling
back to the server's English text (`stage`, `error`, `detail`) for a key it does
not know. A client that ignores those fields — `curl`, a script — simply reads
the English wording.

## Configuration

Everything is optional; an unreadable or zero value falls back to the default
(the one exception is `PYCLEAN_FFMPEG_TIMEOUT=0`, meaning “no limit”). Values are
read once, at server start.

| Variable | Default | Role |
| -------- | ------- | ---- |
| `PORT` | `8787` | listen port (used by `run.sh`, and by the container entrypoint — it also serves the healthcheck) |
| `HF_HOME` | `~/.cache/huggingface` | HuggingFace cache (LavaSR, Parakeet). The Docker image sets it to `/cache/huggingface`, where the `pyclean-hf` volume is mounted |
| `PYCLEAN_JOB_TTL` | `21600` | seconds before a finished job is purged (6 h) |
| `PYCLEAN_JOB_MAX` | `200` | jobs kept in memory |
| `PYCLEAN_QUEUE_MAX` | `20` | max waiting requests (beyond: 429) |
| `PYCLEAN_PURGE_INTERVAL` | `60` | retention sweep period, seconds |
| `PYCLEAN_MAX_SIZE` | `2147483648` | max size per file (2 GB) |
| `PYCLEAN_MAX_DURATION` | `10000` | max duration of a file (s, ~2 h 47) |
| `PYCLEAN_MAX_FILES` | `500` | max files per folder |
| `PYCLEAN_MAX_FOLDER_TOTAL` | `8589934592` | max total volume per folder (8 GB) |
| `PYCLEAN_FFMPEG_TIMEOUT` | `0` | watchdog for a single ffmpeg step (0 = unlimited) |
| `PYCLEAN_PRELOAD_ASR` | `0` | load Parakeet at boot (`1`/`true`/`oui`/`yes`/`on`) |

```bash
PYCLEAN_JOB_TTL=3600 PYCLEAN_MAX_DURATION=7200 ./run.sh
```

## Queue, cancellation, retention

Jobs are **serialized**: a single worker thread consumes the queue, so one job
runs at a time and nothing overlaps — including its ffmpeg steps. A second
request waits in the queue and the page shows how many jobs are ahead of it
(the rank is a snapshot taken at submission; it is not recomputed while waiting).

- **429** once more than `PYCLEAN_QUEUE_MAX` requests are waiting.
- **503** if the registry exceeds `PYCLEAN_JOB_MAX` and every job is still
  active (oldest finished jobs are evicted first).
- **409** when deleting a job that is still running (cancel it first), or when
  cancelling one that has already finished.

A running job can be **cancelled** (button “Cancel processing”): work stops
at the next checkpoint — inference block, transcription chunk, ffmpeg progress
line — and the job folder is removed. Once finished, “Delete results” frees the
job and its files.

Uploads, WAV/MP3/MP4 results and ZIPs are **purged after 6 h**, and
**everything is wiped on restart** (jobs live in memory, so the API would
answer 404 anyway).

## How it works

```
input ──ffmpeg──> mono 48 kHz WAV ──LavaSR v2──> 48 kHz WAV ──ffmpeg──> MP3 / MP4
                                            └──Parakeet (optional)──> TXT / SRT
```

- **Long recordings** are processed in **60 s blocks at 16 kHz**, with a 2 s
  overlap and an equal-power cosine crossfade, so no click or discontinuity is
  audible at the joins. The source is read **window by window**, so memory stays
  proportional to one block (~40 MB) whatever the file duration — a 2 h 47 file
  costs no more RAM than a 5 minute one.
- **Transcription** is chunked differently: **30 s blocks with 10 s of upstream
  context**, because Parakeet is trained on utterances of at most 40 s and
  degrades badly beyond that. The beginning of a blob is the least reliable part
  of the output, so it is discarded and taken from the previous block instead.
  The kept windows tile the file with no gap and no duplicate (see `AGENTS.md`
  for the measurements that set those constants).
- **Video** streams are copied, never re-encoded (`-c:v copy`); only the audio
  is re-encoded to AAC 192 kbit/s.

## Measured performance

RTX 2000 Ada (8 GB), torch 2.14+cu130, Python 3.11:

| Stage | Throughput |
| ----- | ---------- |
| LavaSR enhancement | **~200× real time** — 300 s enhanced in 1.41 s (1.57 s with the denoiser) |
| Parakeet transcription (GPU) | 54 min of audio in ~20 s |
| Parakeet transcription (CPU only) | ~13× real time (230 s in 18 s on 20 cores) |
| Peak VRAM, both models loaded | **~2.4 GB**, constant regardless of chunk count |

## Limitations

- Output is **48 kHz mono**; videos come out as MP4 (existing subtitles and
  extra audio tracks are not carried over).
- Max **~10 000 s** (~2 h 47), **2 GB** per file; folder uploads are limited to
  **500 files / 8 GB**. An over-long file is rejected within seconds, before any
  inference.
- LavaSR is optimized for speech; results on pure music vary (the v2 model is
  mainly trained on VCTK).
- Transcription requires `./run.sh --asr`; without it the option is refused up
  front (400) rather than failing mid-job.
- One job at a time, and one model resident on the GPU at a time.
- The web page is **English by default**, French as a second language (the two
  flags under the LavaSR badge). Everything else — server messages, command
  line, documentation — is English, with no flag to switch. The transcript file
  is French when the model recognises nothing (`(aucune parole détectée)`); that
  is an artefact of the file, not of the interface.
- The SRT is only written if the model returns timestamps; it is never muxed
  into the MP4.
- `HEAD` requests return 404 on the FastAPI routes (no browser impact: the page
  uses `GET`).

## Development

```bash
uv pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # ~220 tests, ~6 s, no model weights loaded
.venv/bin/python -m ruff check .  # lint
```

No model weights are loaded by the test suite: `app.enhancer` and
`app.transcriber` are replaced by fake modules for pipeline tests, and `ffmpeg`
by a shell script when a failure or a hang must be simulated. (`tests/test_stream.py`
does import `torch`/`torchaudio` to check that windowed reading is bit-identical
to global resampling.) Tests touching `probe`/`process_file` are skipped when
`ffmpeg`/`ffprobe` are not on `PATH`.

Coverage: block planning and crossfade, windowed resampling equivalence,
transcription chunking and subtitle generation, the ffmpeg wrapper and its
error paths, processing limits, ZIP building, retention, cancellation, and the
queue.

`ruff format` is **not** used — `ruff check` is the only source of truth.

Building the images yourself (only needed to change something):

```bash
docker build --target cpu -t pyclean-audio:cpu .   # `docker build .` is the same
docker build --target gpu -t pyclean-audio:gpu .   # CUDA 13.0 wheels
docker build --target test .                       # runs pytest -rs, then exits
```

The GPU variant is built against **CUDA 13.0** (`cu130`), so it needs an NVIDIA
driver that supports it — validated on an RTX 2000 Ada. On an older driver,
rebuild with the index that matches it, for example
`--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126`;
`nvidia-smi` reports the driver version.

The same suite also runs inside the image, on the environment the CPU variant
actually ships (ffmpeg present, so the `requires_ffmpeg` tests are not silently
skipped; NeMo present, so it is slower than a local run):

```bash
docker build --target test .        # installs requirements-dev.txt, runs pytest -rs, exits

# smoke test of an image pulled from the registry
docker run --rm -d --name pyclean-audio -p 127.0.0.1:8787:8787 \
  ghcr.io/aureld16/pyclean-audio:cpu
curl -sf http://127.0.0.1:8787/api/status
docker inspect -f '{{.State.Health.Status}}' pyclean-audio   # healthy
docker stop pyclean-audio
```

```bash
# test assets
ffmpeg -f lavfi -i "sine=frequency=350:duration=6" -af "lowpass=2400,aresample=8000" test_8k.wav
ffmpeg -f lavfi -i "testsrc=duration=5:size=320x240:rate=25" -f lavfi -i "sine=frequency=300:duration=5" -af lowpass=2500 -c:v libx264 -c:a aac test.mp4

# a single file through the API (WAV and MP3 are both offered)
JOB=$(curl -s -X POST -F "file=@test_8k.wav" http://127.0.0.1:8787/api/enhance \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
curl -s http://127.0.0.1:8787/api/jobs/$JOB

# model + transcription availability
curl -s http://127.0.0.1:8787/api/status
```

The commands above write `test_8k.wav` and `test.mp4` (~426 MB) to the repo root;
neither is covered by `.gitignore`, so delete them afterwards.

Restarting a detached server:

```bash
pkill -f "uvicorn app[.]main"    # the bracket avoids killing your own shell command
setsid bash -c '(.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8787 >> /tmp/server.log 2>&1 < /dev/null &)'
```

## Project layout

| Path | Role |
| ---- | ---- |
| `run.sh` | launcher: creates `.venv`, installs dependencies, starts the server (`--asr` for transcription) |
| `Dockerfile` | the two images: `base` + `builder-cpu` / `builder-gpu` -> `cpu` / `gpu`, opt-in `test` (runs the suite) |
| `.github/workflows/docker-publish.yml` | rebuilds and pushes the two images to ghcr.io |
| `docker-entrypoint.sh` | container entrypoint: resolves `$PORT`, then `exec`s uvicorn so it is PID 1 |
| `compose.yaml` | the two variants (`up -d cpu` / `up -d gpu`): loopback-only port, `pyclean-hf` model cache, every `PYCLEAN_*` variable |
| `.dockerignore` | keeps the build context small (`tests/` stays: the `test` stage needs it) |
| `requirements-base.txt` | the 4 base packages, copied from `run.sh:45-47` (no NeMo) |
| `app/main.py` | FastAPI API: uploads, queue, cancellation, retention, downloads |
| `app/processor.py` | ffmpeg pipeline: decode, extract, remux, MP3, job orchestration |
| `app/enhancer.py` | `LavaEnhance2` wrapper, 16 kHz windowed reader, block planning |
| `app/transcriber.py` | Parakeet/NeMo wrapper, chunk planning, SRT rendering |
| `app/cancel.py` | cooperative cancellation + global GPU lock |
| `app/messages.py` | message keys (`STAGES`, `ERRORS`), English wording, `MediaError` |
| `app/config.py` | limits and retention, read from the environment |
| `app/cli.py` | command line interface (English output, no translation) |
| `static/index.html` | the whole web UI (drag & drop, A/B, downloads) + its EN/FR dictionary |
| `tests/` | pytest suite |
