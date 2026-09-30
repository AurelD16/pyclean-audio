"""Message keys and wording the server sends to the client.

The web UI is multilingual: it never reads `stage` / `error` / `detail` as-is,
but the **key** (`stage_key`, `error_code`) and its parameters, which it
translates with its own dictionary. The wording below is therefore:

- what the CLI prints (it translates nothing);
- what `GET /api/jobs/{id}` returns in `stage` / `error` / `detail`, hence the
  UI's fallback for a key it does not know (an older client, a stage emitted by
  a third-party `process_file`…);
- the language of a client that asks nothing more: English is the default
  language of the product, hardcoded in `static/index.html`.

**English is the canonical wording here**; the page owns every translation, the
French one included (`server.fr` in its dictionary). Keeping both in sync is the
job of `tests/test_i18n.py`, which compares this module with the page in both
directions.
"""

STAGES: dict[str, str] = {
    # pipeline (app/processor.py)
    "decode": "Decoding the audio…",
    "extract_audio": "Extracting the audio track…",
    "enhance": "Enhancing the audio (LavaSR v2)…",
    "mp3": "Encoding MP3…",
    "remux": "Remuxing the video…",
    "transcribe": "Transcribing (Parakeet)…",
    # jobs (app/main.py)
    "queued": "Queued…",
    "folder_queued": "Folder: {count} files queued…",
    "file_step": "File {index}/{total} — {name}: {inner}",
    "folder_done": "Done — {done}/{total} files",
    "done": "Done",
    "cancelled": "Cancelled",
    "error": "Error",
}

# API error codes: `status` is reserved for the HTTP status, only the wording
# lives here (see ApiError in app/main.py).
ERRORS: dict[str, str] = {
    # queue / retention
    "too_many_jobs": "Too many jobs running or waiting (max {max}).",
    "queue_full": "Queue full ({max} jobs): try again in a moment.",
    # request parameters
    "bad_input_sr": "input_sr must be 8000, 16000 or 24000",
    "bad_output_format": "output_format must be wav or mp3",
    "transcribe_unavailable": (
        "Transcription unavailable: nemo_toolkit[asr] is not installed. "
        "Run ./run.sh --asr, then restart the server."
    ),
    # upload
    "unsupported_format": "Unsupported format: {ext}",
    "bad_filename": "Invalid file name: {name}",
    "file_too_large": "File too large (2 GB max): {name}",
    "folder_too_large": "Folder too large in total",
    "empty_file": "Empty file: {name}",
    "no_files": "No file received",
    "too_many_files": "Too many files (max {max})",
    "no_media_in_folder": "No supported audio/video file in the folder",
    # media (app/processor.py)
    "no_audio_track": "No audio track found in the file.",
    "no_duration": "Could not determine the duration of the file.",
    "too_long": "File too long: {minutes} min (max {max_minutes} min, PYCLEAN_MAX_DURATION).",
    "ffprobe_failed": "ffprobe failed: {detail}",
    "ffmpeg_failed": "ffmpeg failed: {detail}",
    "ffmpeg_timeout": "ffmpeg timed out after {seconds} s",
    "ffmpeg_no_output": "Could not read ffmpeg output",
    # jobs
    "job_not_found": "Job not found",
    "file_not_found": "File not found",
    "zip_unavailable": "Archive unavailable",
    "job_already_done": "This job has already finished.",
    "job_running": "Job in progress: cancel it first.",
    "no_file_done": "No file was processed successfully.",
}


def jsonable(value):
    """Reduce a value to what `json` can write (error parameters)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _fill(template: str, params: dict | None) -> str:
    try:
        return template.format(**(params or {}))
    except (KeyError, IndexError, ValueError):
        # a missing parameter must not make the message disappear
        return template


def stage_text(key: str, params: dict | None = None) -> str:
    """Wording of a stage. Unknown key -> the key itself, never an exception."""
    return _fill(STAGES.get(key, key), params)


def error_text(code: str, params: dict | None = None) -> str:
    return _fill(ERRORS.get(code, code), params)


class MediaError(RuntimeError):
    """A processing failure, meant to be shown to the user.

    `RuntimeError` to stay the exception type callers used to expect before
    keys were introduced. Carries the key and its parameters so the UI can
    translate it; `str(e)` is the English wording printed by the CLI and
    returned in `job["error"]`.
    """

    def __init__(self, code: str, **params):
        super().__init__(code)
        self.code = code
        self.params = {k: jsonable(v) for k, v in params.items()}

    def __str__(self) -> str:
        return error_text(self.code, self.params)
