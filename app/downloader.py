"""yt-dlp wrapper: resolve a URL (metadata only), then download it.

Two functions do the work; the rest is an availability guard and a name
sanitiser:

- `resolve(url)`: flat metadata, one dict per entry (a single video yields one
  entry). Called in the request handler so a bad URL, a private video or an
  oversized playlist fails as a **4xx at submit time** instead of a job that
  dies three seconds later;
- `download(url, dest, ...)`: the media (and optionally the site's subtitles)
  into `dest`, one call per entry. The result is then handed to the **unmodified**
  pipeline (`process_file` → LavaSR v2), so a downloaded file behaves exactly
  like an upload.

`yt_dlp` is imported **inside** the functions, like `nemo`/`LavaSR`: the module
must import without the package installed (the test suite does exactly that) and
`is_available()` must stay free — `find_spec` loads nothing, while
`import yt_dlp` costs a few hundred milliseconds and the page polls
`/api/status` every 2 s.

Nothing here bypasses anything: no cookies, no credentials from a browser, no
DRM. yt-dlp runs with a quiet logger so it never prints on the server's stderr,
and failures are `MediaError("download_failed")` with a truncated detail — a URL
can carry a signature token, so it is never echoed whole.
"""

import importlib.util
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cancel import raise_if_cancelled
from .config import MAX_SIZE
from .messages import MediaError

# The on-disk name is yt-dlp's (`%(title).100s [%(id)s]`), this one is what the
# job exposes: same spirit as the upload sanitiser (app/main.py), never a path
# traversal.
MAX_STEM = 80
_OUTTMPL = "%(title).100s [%(id)s].%(ext)s"
_DETAIL_MAX = 300


@dataclass
class Downloaded:
    """One entry fetched from the site."""
    title: str
    path: Path
    subtitle: Path | None = None
    duration: float | None = None


class _Quiet:
    """A yt-dlp logger that says nothing.

    yt-dlp prints on stdout/stderr by default, which would pollute the server's
    output; `error()` is swallowed too — a failure reaches the user through the
    `MediaError` below, with a wording the page can translate.
    """

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


def is_available() -> bool:
    """Is yt-dlp installed? Optional dependency guard (`find_spec` imports
    nothing, unlike `import yt_dlp`)."""
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        return False


@lru_cache(maxsize=1)
def version() -> str | None:
    """The installed yt-dlp version, or None.

    Cached on purpose: `status()` publishes it and the page polls that endpoint
    every 2 s, while the import costs a few hundred milliseconds.
    """
    if not is_available():
        return None
    try:
        from yt_dlp import version as _v
    except Exception:
        return None
    return getattr(_v, "__version__", None)


def stem_for(title: str, entry_id: str = "") -> str:
    """A safe, bounded file stem for a title: no path separator, no `..`, no
    empty string, at most MAX_STEM characters."""
    s = re.sub(r"[^A-Za-z0-9._ -]", "_", title or "")
    s = re.sub(r"\.{2,}", ".", s).strip()[:MAX_STEM].strip()
    if not s.strip(" ."):
        s = f"video-{entry_id}" if entry_id else "video"
    return s


def _base_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "cachedir": False,       # nothing to cache on a server, and it writes
        "logger": _Quiet(),
        "retries": 3,
    }


def _extract(opts: Any, url: str):
    from yt_dlp import YoutubeDL

    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


def _entries(info) -> list[dict]:
    """The entries of an extraction result (a video yields one entry)."""
    if not isinstance(info, dict):
        return []
    if info.get("_type") == "playlist" or info.get("entries") is not None:
        return [e for e in (info.get("entries") or []) if isinstance(e, dict)]
    return [info]


def _failed(exc: Exception, cancel: threading.Event | None) -> Exception:
    """The exception a yt-dlp failure must surface as.

    A cancellation wins over a `DownloadError` raised in the same window: a
    cancelled job must never be reported as a download failure.
    """
    if cancel is not None and cancel.is_set():
        from .cancel import JobCancelled

        return JobCancelled("Processing cancelled by the client.")
    if isinstance(exc, MediaError):
        return exc
    return MediaError("download_failed", detail=str(exc)[:_DETAIL_MAX])


def resolve(url: str) -> list[dict]:
    """Flat metadata of a video or a playlist: one dict per entry, no media.

    Each entry carries at least `id` and `title`; `duration`, `filesize` and
    `filesize_approx` are often absent on a flat extraction (they are used only
    to refuse an oversized playlist before downloading anything).
    """
    opts = _base_opts()
    opts.update({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
    })
    try:
        info = _extract(opts, url)
    except Exception as exc:
        raise _failed(exc, None) from exc
    return _entries(info)


def _fraction(status: dict) -> float:
    """`downloaded_bytes / total_bytes` as 0…1. An unknown total (a live stream,
    an unknown length) only reaches 1.0 once the download is finished."""
    if status.get("status") == "finished":
        return 1.0
    total = status.get("total_bytes") or status.get("total_bytes_estimate")
    done = status.get("downloaded_bytes") or 0
    if not total or total <= 0:
        return 0.0
    return min(max(done / total, 0.0), 1.0)


def _media_path(info: dict) -> Path | None:
    """The file yt-dlp actually wrote (after the merge / post-processing)."""
    for rd in info.get("requested_downloads") or []:
        for key in ("filepath", "_filename", "filename"):
            if rd.get(key):
                return Path(rd[key])
    for key in ("filepath", "_filename", "filename"):
        if info.get(key):
            return Path(info[key])
    return None


def _subtitle_path(info: dict, dest: Path) -> Path | None:
    """The downloaded subtitle, converted to SRT by ffmpeg.

    A subtitle missing in the chosen language is normal (the site has none): the
    artifact is then simply absent, the job still succeeds.
    """
    subs = info.get("requested_subtitles") or {}
    if isinstance(subs, dict):
        for sub in subs.values():
            path = sub.get("filepath") if isinstance(sub, dict) else None
            if path and Path(path).exists():
                return Path(path)
    found = sorted(dest.glob("*.srt"))
    return found[0] if found else None


def download(url: str, dest: Path, fmt: str = "mp4", subtitles: bool = False,
             subtitle_lang: str = "fr",
             on_stage: Callable[[float], None] | None = None,
             cancel: threading.Event | None = None,
             playlist_index: int | None = None) -> list[Downloaded]:
    """Download `url` into `dest` and return what landed there.

    `fmt`: "mp4" (video + audio, merged to MP4) or "mp3" (**audio only**: no
    video track is even fetched, which is what "on ne garde que le son" means).
    `on_stage(fraction)` reports the download's progress; `cancel` is checked on
    every progress line, which is the cancellation checkpoint of this phase.
    `playlist_index` (0-based) downloads a single entry of a playlist — the
    caller processes each entry in turn and deletes its source right after, so a
    500-video playlist never fills the disk.

    `max_filesize` is MAX_SIZE, so yt-dlp itself aborts a single oversized file.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def progress(status: dict) -> None:
        raise_if_cancelled(cancel)
        if on_stage is not None:
            on_stage(_fraction(status))

    opts = _base_opts()
    opts.update({
        "format": ("bestvideo+bestaudio/best" if fmt == "mp4"
                   else "bestaudio/ba"),
        "paths": {"home": str(dest)},
        "outtmpl": _OUTTMPL,
        "overwrites": True,
        "continuedl": True,
        "max_filesize": MAX_SIZE,
        "progress_hook": progress,
    })
    if fmt == "mp4":
        opts["merge_output_format"] = "mp4"
    if subtitles:
        # The site subtitles are written next to the media and converted to SRT;
        # they are never muxed (documented behaviour, as for the transcript).
        opts.update({
            "writesubtitles": True,
            "subtitleslangs": [subtitle_lang or "fr"],
            "writeautomaticsub": False,
        })
        opts["postprocessors"] = [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}]
    if playlist_index is not None:
        # `playlist_items` is 1-based; a single call per entry keeps the disk
        # bounded and the per-entry progress meaningful.
        opts["playlist_items"] = str(int(playlist_index) + 1)

    try:
        info = _extract(opts, url)
    except Exception as exc:
        raise _failed(exc, cancel) from exc

    out = []
    for ent in _entries(info):
        raise_if_cancelled(cancel)
        path = _media_path(ent)
        if path is None or not path.exists():
            continue
        out.append(Downloaded(
            title=ent.get("title") or path.stem,
            path=path,
            subtitle=_subtitle_path(ent, dest) if subtitles else None,
            duration=ent.get("duration"),
        ))
    if not out:
        raise _failed(MediaError("download_failed", detail="no file downloaded"),
                      cancel)
    return out
