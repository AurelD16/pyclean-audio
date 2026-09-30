#!/usr/bin/env python3
"""pyclean-audio desktop launcher.

One file, standard library only, **no import-time side effect** (every helper is
a plain function, so `tests/test_launcher.py` can drive them without starting a
server, a network or a window).

The runtime tree it drives is *plain files*, deliberately not a PyInstaller
bundle: torch is ~2.5 GB of shared libraries and data files, and the frozen
build is the fragile part, not this ~300-line launcher. See
`packaging/README.md`.

    <root>/
      pyclean-audio.exe          # this file, frozen (Windows)
      pyclean-audio              # this file, run by the runtime python (Linux)
      launcher.py                # this file (Linux, unfrozen)
      runtime/python/            # standalone CPython 3.11 + site-packages
      runtime/bin/               # ffmpeg + ffprobe, by bare name
      app/  static/  LICENCE  THIRD-PARTY-NOTICES.txt

State (results, logs, model cache) never lives in `<root>`: the launcher passes
`PYCLEAN_DATA_DIR` / `HF_HOME` under the per-user state directory, because the
install directory is read-only (`C:\\Program Files`) for a normal user and
because uninstalling must not delete the user's results.

The server is bound to 127.0.0.1 only: the API has **no authentication**
(plain upload + download), so it must never be reachable from the network.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "pyclean-audio"
LAUNCHER_VERSION = "1.0"

# /api/status answers before the model is loaded (the LavaSR weights are
# preloaded in a daemon thread, app/main.py:428): waiting for `status == "ready"`
# would mean staring at a console with no feedback for the ~115 MB download.
READY_TIMEOUT = 180.0
POLL_INTERVAL = 0.4
SHUTDOWN_TIMEOUT = 10.0


# ----------------------------------------------------------------- locations

def install_root(start: Path | str | None = None) -> Path:
    """The install tree: the folder that holds `app/`, `static/` and `runtime/`.

    Frozen (Windows) that is the directory of `pyclean-audio.exe`; from the
    source it is the parent of `packaging/launcher/`, and in the payload
    (`<root>/launcher.py`) simply the directory of this file.
    """
    if start is not None:
        base = Path(start).resolve()
    elif getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    for candidate in (base, base.parent, base.parent.parent):
        if (candidate / "app").is_dir() and (candidate / "static").is_dir():
            return candidate
    return base


def data_dir() -> Path:
    """Per-user state directory: results, logs, model cache, instance lock.

    `PYCLEAN_HOME` overrides it; otherwise `%LOCALAPPDATA%\\pyclean-audio` on
    Windows and `$XDG_DATA_HOME/pyclean-audio` (`~/.local/share/pyclean-audio`)
    elsewhere. Never inside the install directory.
    """
    override = os.environ.get("PYCLEAN_HOME", "").strip()
    if override:
        return Path(os.path.expanduser(override))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_NAME


def jobs_dir(state: Path) -> Path:
    """Value given to `PYCLEAN_DATA_DIR` (what `app/config.py:data_dir` reads)."""
    return Path(state) / "jobs"


def logs_dir(state: Path) -> Path:
    return Path(state) / "logs"


def cache_dir(state: Path) -> Path:
    """`HF_HOME`: where the LavaSR weights (~115 MB) are downloaded, once."""
    return Path(state) / "cache" / "huggingface"


def lock_path(state: Path) -> Path:
    return Path(state) / "instance.lock"


def make_state_dirs(state: Path) -> Path:
    """Create the state tree and return it (the install tree stays untouched)."""
    state = Path(state)
    for sub in (state, jobs_dir(state), logs_dir(state), cache_dir(state)):
        sub.mkdir(parents=True, exist_ok=True)
    return state


# ------------------------------------------------------------------ network

def pick_port(preferred: int = 8787, tries: int = 20, host: str = "127.0.0.1") -> int:
    """First free TCP port at or above `preferred`.

    A socket is really bound (then closed): checking with a connect() instead
    would report a free port for a server that is not listening *yet*, and the
    second instance would collide with the first one. `SO_REUSEADDR` is
    deliberately not set, so a port in TIME_WAIT counts as taken and the walk
    moves on.
    """
    for offset in range(max(tries, 1)):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port in {preferred}..{preferred + tries - 1}")


# --------------------------------------------------------- single instance

class InstanceLock:
    """Handle on the `O_CREAT|O_EXCL` lock file; releasing it removes the file."""

    def __init__(self, path: Path, pid: int, port: int | None = None):
        self.path = Path(path)
        self.pid = pid
        self.port = port

    def release(self) -> None:
        try:
            if self.pid == os.getpid():
                self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> "InstanceLock":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # no signal 0 on Windows
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    except OSError:
        return False
    return True


def _lock_text(pid: int, port: int | None) -> str:
    return f"{pid}\n{port}\n" if port else f"{pid}\n"


def _read_lock(path: Path) -> tuple[int, int | None]:
    """`<pid>` or `<pid>\\n<port>`; (0, None) when unreadable or corrupt."""
    try:
        raw = path.read_text(encoding="utf-8").split()
        pid = int(raw[0])
        port = int(raw[1]) if len(raw) > 1 else None
    except (OSError, ValueError, IndexError):
        return 0, None
    return pid, port


def acquire_lock(path: Path | str, port: int | None = None) -> InstanceLock | None:
    """Single-instance guard, atomic: returns None when another instance owns it.

    A lock left by a dead process (power loss, task manager) is reclaimed: the
    pid it holds is probed and the file replaced. Two launches racing on the same
    instant cannot both win, `O_EXCL` is what makes the creation atomic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            pid, _port = _read_lock(path)
            if _pid_alive(pid):
                return None
            try:  # stale: reclaim it
                path.unlink()
            except OSError:
                return None
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_lock_text(os.getpid(), port))
        return InstanceLock(path, os.getpid(), port)
    return None


# ----------------------------------------------------------------- the server

def python_exe(root: Path | str) -> Path:
    """The bundled interpreter: `<root>/runtime/python/python.exe` on Windows,
    `<root>/runtime/python/bin/python3` elsewhere."""
    root = Path(root)
    if os.name == "nt":
        return root / "runtime" / "python" / "python.exe"
    return root / "runtime" / "python" / "bin" / "python3"


def server_command(root: Path | str, port: int) -> list[str]:
    """Exact argv of the server.

    `--workers 1` is mandatory, like in `run.sh:66` and in the Dockerfile: jobs
    are serialized by one in-process queue and one worker thread, job state
    lives in a dict, and the two model singletons are lock-guarded — a second
    worker would duplicate ~2.5 GB of model and lose the job states. `-m
    uvicorn` puts the CWD (the install root, passed as `cwd`) first on
    `sys.path`, which is how `app.main` is found.
    """
    return [
        str(python_exe(root)),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--workers",
        "1",
    ]


def build_env(root: Path | str, port: int, state: Path | str) -> dict:
    """Environment of the server process.

    `runtime/bin` is **prepended** to `PATH` because `app/processor.py:38,85`
    calls `ffprobe`/`ffmpeg` by bare name — the bundled ones must win over
    anything else installed on the machine. The state directory is redirected
    (`PYCLEAN_DATA_DIR`, `HF_HOME`) so nothing is written under the install
    directory, and     `PYCLEAN_DESKTOP=1` is what makes the page drop its
    "./run.sh --asr" advice. `PYCLEAN_PRELOAD_ASR=0` keeps the (absent) Parakeet
    from being probed at boot. `port` is part of the environment of the same
    run: the server receives it on its argv (`server_command`), not here.
    """
    root, state = Path(root), Path(state)
    env = dict(os.environ)
    bindir = str(root / "runtime" / "bin")
    sep = os.pathsep
    path = env.get("PATH", "")
    env["PATH"] = f"{bindir}{sep}{path}" if path else bindir
    env["PYCLEAN_DATA_DIR"] = str(jobs_dir(state))
    env["HF_HOME"] = str(cache_dir(state))
    env["PYCLEAN_DESKTOP"] = "1"
    env["PYCLEAN_PRELOAD_ASR"] = "0"
    # the server's output goes to a log file: unbuffered, so a crash is readable.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def wait_ready(url: str, timeout: float = READY_TIMEOUT, proc=None,
               interval: float = POLL_INTERVAL, opener=None) -> dict | None:
    """Poll `GET /api/status` until it answers 200; None on timeout or death.

    `GET`, never `HEAD` (every FastAPI route of this app answers 404 to HEAD).
    The status dict is returned because it tells the launcher whether the model
    is still downloading — the window opens either way.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    fetch = opener or urllib.request.urlopen
    while True:
        if proc is not None and proc.poll() is not None:
            return None  # the server died before answering
        try:
            with fetch(url, timeout=4) as r:
                if getattr(r, "status", 200) == 200:
                    try:
                        return json.loads(r.read().decode("utf-8"))
                    except (ValueError, OSError):
                        return {}
        except (urllib.error.URLError, OSError, ValueError):
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


def start_server(root: Path | str, port: int, state: Path | str, log_path: Path | str,
                 verbose: bool = False) -> subprocess.Popen:
    """Start the server, its output going to `log_path` (truncated at start).

    `start_new_session=True` puts it in its own process group: `app/processor.py`
    kills ffmpeg *by group*, and the launcher must not share that group or it
    would be killed with the job. Windows ignores the flag (and needs no group).
    """
    root, state = Path(root), Path(state)
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = build_env(root, port, state)
    cmd = server_command(root, port)
    if verbose:
        _say(f"{APP_NAME}: {' '.join(cmd)}\n{APP_NAME}: cwd={root} log={log}")
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        return subprocess.Popen(
            cmd, cwd=str(root), env=env, stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )


def terminate(proc, timeout: float = SHUTDOWN_TIMEOUT) -> None:
    """Stop the server: its process group first (ffmpeg included), then the
    process itself — `terminate()`, wait 10 s, `kill()`."""
    if proc is None or proc.poll() is not None:
        return
    if os.name == "posix":
        # start_new_session made the server the leader of its own group, which
        # also holds the ffmpeg it spawned: killing the group leaves no orphan.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (AttributeError, OverflowError, ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.terminate()
    except OSError:
        return
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


# --------------------------------------------------------------------- window

def open_ui(url: str) -> str:
    """Open `url` in a webview, or in the default browser when there is none.

    pywebview is **optional** and never a dependency of the payload: on a minimal
    Linux (no GTK / WebKit2GTK) the import fails and the page opens in the
    default browser. Returns "webview", "browser" or "" when nothing opened.
    """
    if os.environ.get("PYCLEAN_NO_WINDOW"):
        return ""
    try:
        import webview  # optional: never a dependency of the payload
    except Exception:
        pass
    else:
        try:
            webview.create_window(APP_NAME, url, width=1100, height=820)
            webview.start()
            return "webview"
        except Exception:
            pass  # headless or no display: fall back to the browser
    try:
        if webbrowser.open(url):
            return "browser"
    except Exception:
        pass
    return ""


# ----------------------------------------------------------------------- run

def _say(message: str) -> None:
    """Print, unless the launcher was frozen without a console (Windows)."""
    try:
        if sys.stdout is not None:
            print(message, flush=True)
    except Exception:
        pass


def _already_running(state: Path) -> int:
    """A second launch: open the window of the running instance, if it answers."""
    pid, port = _read_lock(lock_path(state))
    if port:
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(f"{url}/api/status", timeout=1):
                open_ui(url)
        except (urllib.error.URLError, OSError, ValueError):
            pass
    return pid


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Starts the pyclean-audio server and opens its window.",
    )
    p.add_argument("--port", type=int, default=int(os.environ.get("PYCLEAN_PORT", 8787)),
                   help="preferred port (8787); the next free one is taken")
    p.add_argument("--no-window", action="store_true",
                   help="do not open any window (server only)")
    p.add_argument("--verbose", action="store_true", help="print the server command")
    p.add_argument("--version", action="version",
                   version=f"{APP_NAME} launcher {LAUNCHER_VERSION}")
    return p.parse_args(argv)


def _install_signal_handlers() -> None:
    """Ctrl+C already raises KeyboardInterrupt; a `kill` must clean up too — the
    default SIGTERM action would leave the server and its ffmpeg behind."""
    def _stop(_signum, _frame):
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass


def main(argv=None) -> int:
    args = _parse_args(argv)
    _install_signal_handlers()
    root = install_root()
    try:
        state = make_state_dirs(data_dir())
        lock = acquire_lock(lock_path(state))
    except OSError as exc:
        # Never fall back to the install directory: it is read-only at run time.
        _say(f"{APP_NAME}: the state directory is not usable ({exc}).")
        return 1
    if lock is None:
        pid = _already_running(state)
        _say(f"{APP_NAME} is already running (pid {pid}); nothing else started.")
        return 0
    proc = None
    try:
        port = pick_port(args.port)
        url = f"http://127.0.0.1:{port}"
        _say(f"{APP_NAME} — {root}\nStarting the server on {url}…")
        proc = start_server(root, port, state, logs_dir(state) / "server.log", args.verbose)
        lock.port = port
        _write_lock(lock)
        status = wait_ready(f"{url}/api/status", READY_TIMEOUT, proc)
        if status is None:
            _say(f"The server did not answer on {url}; see {logs_dir(state) / 'server.log'}.")
            return 1
        if status.get("status") != "ready":
            # The weights (~115 MB) arrive in the background: the window opens
            # now and the badge shows the download.
            _say("First run: downloading the AI model (~115 MB), once.")
        _say(f"Ready on {url}")
        if args.no_window:
            _say("No window (--no-window); press Ctrl+C to stop.")
            _wait_forever(proc)
        elif open_ui(url) == "browser":
            # A browser tab cannot be followed: the launcher keeps serving until
            # it is stopped (Ctrl+C, a `kill`, the window being closed by the
            # desktop environment).
            _say("Opened in your browser; stop the server with Ctrl+C.")
            _wait_forever(proc)
        # "webview": open_ui() has returned, which happens when the window is
        # closed (webview.start() blocks until then) — closing the window *is*
        # "quit the app": fall through to `finally` -> terminate + release.
        # "": nothing could be opened at all, same thing.
    except KeyboardInterrupt:
        _say("\nStopping…")
    finally:
        terminate(proc)
        lock.release()
    return 0


def _wait_forever(proc) -> None:
    while proc.poll() is None:
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            continue


def _write_lock(lock: InstanceLock) -> None:
    """Record the listening port once it is known, for the second launch."""
    try:
        with open(lock.path, "w", encoding="utf-8") as f:
            f.write(_lock_text(lock.pid, lock.port))
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
