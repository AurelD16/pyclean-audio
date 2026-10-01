"""Desktop launcher: single instance, port, environment, startup, shutdown.

No server is started, no port is really bound for long and no window opens: the
launcher is loaded from its file (`packaging/launcher/launcher.py`, not an
installed package) and every helper is driven directly — it is standard library
only and has no import-time side effect, precisely so this file stays cheap.
"""

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
import types
import urllib.error
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

LAUNCHER_PATH = Path(__file__).resolve().parent.parent / "packaging" / "launcher" / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("pyclean_launcher", LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


@pytest.fixture
def windows(monkeypatch):
    """Simulate a Windows run inside the test.

    The launcher branches on `os.name`, but `pathlib.Path` reads it too and
    refuses to build a `WindowsPath` on this machine — so `Path` is pinned to
    its POSIX flavour while the branch under test takes the Windows side.
    """
    @contextlib.contextmanager
    def _cm():
        with monkeypatch.context() as m:
            m.setattr(os, "name", "nt")
            m.setattr(launcher, "Path", PurePosixPath)
            yield

    return _cm()


class FakeProc:
    """A subprocess.Popen stand-in that records the calls of terminate()."""

    pid = 4242

    def __init__(self, running=True, hangs=False):
        self.calls = []
        self._running = running
        self._hangs = hangs

    def poll(self):
        return None if self._running else 0

    def terminate(self):
        self.calls.append("terminate")
        if not self._hangs:
            self._running = False

    def kill(self):
        self.calls.append("kill")
        self._running = False

    def wait(self, timeout=None):
        self.calls.append(f"wait({timeout})")
        if self._hangs:
            raise subprocess.TimeoutExpired("python", timeout or 0)
        self._running = False
        return 0


class FakeResponse:
    """What urllib.request.urlopen returns: a context manager with .read()."""

    def __init__(self, payload=None, status=200):
        self._body = json.dumps(payload if payload is not None else {}).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def opener_returning(*results):
    """A urlopen replacement: each call pops one result, the last one repeats."""
    queue = list(results)

    def _open(_url, timeout=None):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return _open


# --------------------------------------------------------------- state dirs

def test_data_dir_par_defaut_sur_linux(monkeypatch, tmp_path):
    monkeypatch.delenv("PYCLEAN_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    with monkeypatch.context() as m:
        m.setattr(os, "name", "posix")
        d = launcher.data_dir()
    assert d.parts[-2:] == ("xdg", "pyclean-audio")


def test_data_dir_sans_xdg_va_dans_home(monkeypatch, tmp_path):
    monkeypatch.delenv("PYCLEAN_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    with monkeypatch.context() as m:
        m.setattr(os, "name", "posix")
        d = launcher.data_dir()
    assert d.parts[-3:] == (".local", "share", "pyclean-audio")


def test_data_dir_sur_windows(monkeypatch, tmp_path, windows):
    """Windows keeps its state in %LOCALAPPDATA%, never in Program Files."""
    monkeypatch.delenv("PYCLEAN_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    with windows:
        d = launcher.data_dir()
    assert d.parts[-2:] == ("Local", "pyclean-audio")


@pytest.mark.parametrize("name", ["posix", "nt"])
def test_pyclean_home_prime(monkeypatch, tmp_path, name, windows):
    monkeypatch.setenv("PYCLEAN_HOME", str(tmp_path / "ailleurs"))
    if name == "nt":
        with windows:
            assert launcher.data_dir() == tmp_path / "ailleurs"
    else:
        assert launcher.data_dir() == tmp_path / "ailleurs"


def test_les_repertoires_etat_ne_sont_pas_dans_le_repertoire_installation(tmp_path):
    """Criterion: results, logs and cache live under the state dir, not the root."""
    state = launcher.make_state_dirs(tmp_path / "state")
    root = tmp_path / "install"
    (root / "app").mkdir(parents=True)
    for path in (launcher.jobs_dir(state), launcher.logs_dir(state),
                 launcher.cache_dir(state)):
        assert path.is_dir()
        assert state in path.parents
        assert root not in path.parents
    assert launcher.cache_dir(state).parts[-2:] == ("cache", "huggingface")
    assert list(root.iterdir()) == [root / "app"]  # nothing was written in the root


# -------------------------------------------------------------------- port

def test_main_signale_un_etat_inutilisable(monkeypatch, tmp_path, capsys):
    """main() must exit 1 with a message, not with a traceback (the frozen
    Windows launcher has no console to print one to)."""
    blocker = tmp_path / "etat"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("PYCLEAN_HOME", str(blocker))
    assert launcher.main([]) == 1
    assert "state" in capsys.readouterr().out.lower()


def test_pick_port_renvoie_le_port_prefere():
    """The preferred port itself when it is free.

    Not 8787 literally: this fails whenever a pyclean-audio is running on the
    developer's machine, which is exactly what happened while the AppImage was
    being tested.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert launcher.pick_port(free) == free


def test_pick_port_contourne_un_port_occupe(tmp_path):
    """A port held by another socket is never handed to the server."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        taken = busy.getsockname()[1]
        assert launcher.pick_port(taken) != taken


def test_pick_port_echoue_sans_port_libre(monkeypatch):
    """Everything taken: the launcher must fail loudly, never bind 0.0.0.0."""
    monkeypatch.setattr(launcher.socket, "socket", _AlwaysBusySocket)
    with pytest.raises(OSError):
        launcher.pick_port(8787, tries=3)


class _AlwaysBusySocket:
    """A socket whose bind() always fails (every port already taken)."""

    def __init__(self, *a, **k):
        pass

    def bind(self, addr):
        raise OSError("address already in use")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


# ------------------------------------------------------- single instance

def test_acquire_lock_deuxieme_tentative_refusee(tmp_path):
    p = tmp_path / "instance.lock"
    first = launcher.acquire_lock(p, 8787)
    assert first is not None
    assert launcher.acquire_lock(p, 8787) is None  # another instance owns it
    first.release()
    assert launcher.acquire_lock(p, 8787) is not None  # released


def test_acquire_lock_reclame_un_pid_mort(tmp_path, monkeypatch):
    """A lock left by a crashed instance (power loss) must not block forever."""
    p = tmp_path / "instance.lock"
    p.write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    lock = launcher.acquire_lock(p, 8788)
    assert lock is not None
    assert launcher._read_lock(p)[1] == 8788


def test_acquire_lock_refuse_quand_le_pid_est_vivant(tmp_path, monkeypatch):
    p = tmp_path / "instance.lock"
    p.write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    assert launcher.acquire_lock(p, 8788) is None
    assert launcher._read_lock(p)[0] == 424242  # untouched


def test_lock_illisible_est_reclame(tmp_path):
    p = tmp_path / "instance.lock"
    p.write_text("pas un pid\n", encoding="utf-8")
    assert launcher.acquire_lock(p, 8787) is not None


def test_pid_alive_reconnait_le_pid_courant():
    assert launcher._pid_alive(os.getpid()) is True
    assert launcher._pid_alive(0) is False
    assert launcher._pid_alive(-1) is False


def test_lock_libere_a_la_sortie_du_with(tmp_path):
    p = tmp_path / "instance.lock"
    with launcher.acquire_lock(p, 8787) as lock:
        assert lock.path == p
        assert p.exists()
    assert not p.exists()


# ------------------------------------------------------------- environment

def test_build_env_place_ffmpeg_en_tete_de_path(tmp_path):
    env = launcher.build_env(tmp_path, 8787, tmp_path / "state")
    first = env["PATH"].split(os.pathsep)[0]
    assert first == str(tmp_path / "runtime" / "bin")


def test_build_env_definit_les_quatre_variables(tmp_path):
    state = tmp_path / "state"
    env = launcher.build_env(tmp_path, 8787, state)
    assert env["PYCLEAN_DATA_DIR"] == str(launcher.jobs_dir(state))
    assert env["HF_HOME"] == str(launcher.cache_dir(state))
    assert env["PYCLEAN_DESKTOP"] == "1"
    assert env["PYCLEAN_PRELOAD_ASR"] == "0"


def test_build_env_ne_perd_pas_le_chemin_existant(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))
    env = launcher.build_env(tmp_path, 8787, tmp_path / "state")
    assert env["PATH"].split(os.pathsep)[1:] == ["/usr/bin", "/bin"]


def test_build_env_ne_touche_pas_l_environnement_du_lanceur(tmp_path, monkeypatch):
    monkeypatch.setenv("PYCLEAN_DESKTOP", "0")
    launcher.build_env(tmp_path, 8787, tmp_path / "state")
    assert os.environ["PYCLEAN_DESKTOP"] == "0"  # copy, not in-place edit


# --------------------------------------------------------------- server argv

def test_server_command_sur_linux(tmp_path):
    assert launcher.server_command(tmp_path, 8787) == [
        str(tmp_path / "runtime" / "python" / "bin" / "python3"),
        "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", "8787", "--workers", "1",
    ]


def test_server_command_sur_windows(tmp_path, windows):
    """`python.exe` sits at the root of the standalone prefix, not in bin/."""
    root = Path(tmp_path)  # built before the patch: the flavour stays PosixPath
    with windows:
        cmd = launcher.server_command(root, 9000)
    assert cmd[0] == str(root / "runtime" / "python" / "python.exe")
    assert cmd[-2:] == ["--workers", "1"]
    assert cmd[cmd.index("--host") + 1] == "127.0.0.1"
    assert PureWindowsPath(cmd[0]).name == "python.exe"


def test_server_command_ne_depend_jamais_du_shell(tmp_path):
    """argv only, like app/processor.py: nothing is ever run through a shell."""
    cmd = launcher.server_command(tmp_path, 8787)
    assert all(isinstance(a, str) for a in cmd)
    assert not any(" " in a for a in cmd)


# ------------------------------------------------------------ wait_ready

def test_wait_ready_retourne_le_statut():
    payload = {"status": "loading", "desktop": True}
    got = launcher.wait_ready(
        "http://127.0.0.1:8787/api/status", 5,
        opener=opener_returning(FakeResponse(payload)),
    )
    assert got == payload


def test_wait_ready_accepte_le_premier_echec_puis_reussit():
    got = launcher.wait_ready(
        "http://x/api/status", 5,
        opener=opener_returning(urllib.error.URLError("refused"), FakeResponse({"status": "ready"})),
        interval=0,
    )
    assert got == {"status": "ready"}


def test_wait_ready_rend_la_main_si_le_delai_passe():
    assert launcher.wait_ready(
        "http://x/api/status", 0, opener=opener_returning(urllib.error.URLError("x")),
    ) is None


def test_wait_ready_rend_vite_si_le_serveur_est_mort():
    assert launcher.wait_ready(
        "http://x/api/status", 60, FakeProc(running=False),
        opener=opener_returning(FakeResponse({"status": "ready"})),
    ) is None


def test_wait_ready_ignore_une_reponse_non_200():
    assert launcher.wait_ready(
        "http://x/api/status", 0, opener=opener_returning(FakeResponse(status=500)),
    ) is None


# --------------------------------------------------------------- terminate

def test_terminate_appelle_terminate_puis_attend(monkeypatch):
    killed = []
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(launcher.os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))
    p = FakeProc()
    launcher.terminate(p)
    assert killed == [(FakeProc.pid, signal.SIGKILL)]  # ffmpeg included
    assert p.calls[0] == "terminate"
    assert "kill" not in p.calls


def test_terminate_passe_en_kill_apres_le_delai(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(launcher.os, "killpg", lambda *a: None)
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: pid)
    p = FakeProc(hangs=True)
    launcher.terminate(p, timeout=1)
    assert p.calls[:3] == ["terminate", "wait(1)", "kill"]


def test_terminate_ne_touche_rien_si_le_serveur_est_arrete(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(launcher.os, "killpg", lambda *a: pytest.fail("killpg"))
    p = FakeProc(running=False)
    launcher.terminate(p)
    assert p.calls == []


def test_terminate_sur_windows_ignore_le_group_de_processus(monkeypatch, windows):
    """No os.killpg on Windows: it would raise AttributeError."""
    with windows:
        monkeypatch.delattr(launcher.os, "killpg", raising=False)
    p = FakeProc()
    launcher.terminate(p)
    assert p.calls == ["terminate", "wait(10.0)"]


def test_terminate_resiste_a_un_pid_inconnu(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")

    def _raise(_pid):
        raise ProcessLookupError("gone")

    monkeypatch.setattr(launcher.os, "getpgid", _raise)
    p = FakeProc()
    launcher.terminate(p)  # must not raise
    assert p.calls[0] == "terminate"


# ------------------------------------------------------------------ window

def test_open_ui_retombe_sur_le_navigateur_sans_pywebview(monkeypatch):
    """pywebview is optional: its absence must not stop the app from opening."""
    opened = []
    monkeypatch.setitem(sys.modules, "webview", None)  # import webview -> None
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url) or True)
    assert launcher.open_ui("http://127.0.0.1:8787") == "browser"
    assert opened == ["http://127.0.0.1:8787"]


def test_open_ui_utilise_pywebview_quand_il_est_là(monkeypatch):
    calls = []
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: calls.append(("window", a, k))
    fake.start = lambda *a, **k: calls.append(("start", a, k))
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: pytest.fail("browser"))
    assert launcher.open_ui("http://127.0.0.1:8787") == "webview"
    assert [c[0] for c in calls] == ["window", "start"]


def test_open_ui_eteint_par_pyclean_no_window(monkeypatch):
    monkeypatch.setenv("PYCLEAN_NO_WINDOW", "1")
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: pytest.fail("browser"))
    assert launcher.open_ui("http://127.0.0.1:8787") == ""


# ------------------------------------------------------------- main() lifecycle

def tmp_log(calls):
    """The launcher's own log inside the temporary state directory."""
    return launcher.logs_dir(calls["lock"].parent) / "launcher.log"


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    """Drive `main()` with no server, no port bound and no window.

    Returns `(run, calls)`: `run(*argv)` calls `launcher.main()` and `calls`
    records what it did (`ui` is what `open_ui` answers, and can be changed
    before the call).
    """
    calls = {"ui": "webview", "open_ui": [], "wait_forever": 0, "terminate": [],
             "started": [], "already": [], "lock": launcher.lock_path(tmp_path / "state")}

    proc = FakeProc()
    monkeypatch.setattr(launcher, "_install_signal_handlers", lambda: None)
    monkeypatch.setattr(launcher, "install_root", lambda *_a, **_k: tmp_path)
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(launcher, "pick_port", lambda *_a, **_k: 8787)
    monkeypatch.setattr(launcher, "start_server",
                        lambda *_a, **_k: calls["started"].append(1) or proc)
    monkeypatch.setattr(launcher, "wait_ready", lambda *_a, **_k: {"status": "ready"})
    monkeypatch.setattr(launcher, "open_ui",
                        lambda url: calls["open_ui"].append(url) or calls["ui"])
    monkeypatch.setattr(launcher, "_wait_forever",
                        lambda _p: calls.__setitem__("wait_forever", calls["wait_forever"] + 1))
    monkeypatch.setattr(launcher, "terminate", lambda p, **_k: calls["terminate"].append(p))
    # main() calls _already_running() when the lock is taken: keep it in the
    # recorder instead of letting it touch the network.
    monkeypatch.setattr(launcher, "_already_running",
                        lambda _state: calls["already"].append(1) or (424242, 8787))

    def run(*argv):
        calls["rc"] = launcher.main(list(argv))
        return calls["rc"]

    return run, calls


def test_fermer_la_fenetre_arrete_le_serveur(run_main):
    """The main gesture on Windows: open_ui() returns when the webview window is
    closed, and that must quit the app (server stopped, lock released) — not
    leave a headless server holding the models and the port forever."""
    run, calls = run_main
    assert run() == 0
    assert calls["ui"] == "webview"
    assert calls["open_ui"] == ["http://127.0.0.1:8787"]
    assert calls["wait_forever"] == 0
    assert len(calls["terminate"]) == 1
    assert not calls["lock"].exists()


def test_le_navigateur_continue_de_servir(run_main):
    """A browser tab cannot be followed: the launcher stays until it is stopped."""
    run, calls = run_main
    calls["ui"] = "browser"
    assert run() == 0
    assert calls["wait_forever"] == 1
    assert len(calls["terminate"]) == 1  # ... and terminates on the way out
    assert not calls["lock"].exists()


def test_aucune_fenetre_ouvre_rien_mais_sert_toujours(run_main, capsys):
    """D7.1: nothing could be opened (no browser, headless) is NOT "the window was
    closed". The app says where the page is and keeps serving, so the user is
    never left with nothing — the Quit button on the page stops it."""
    run, calls = run_main
    calls["ui"] = ""
    assert run() == 0
    assert calls["open_ui"] == ["http://127.0.0.1:8787"]
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8787" in out      # actionable: the URL is named
    assert "no window could be opened" in out
    assert "no window could be opened" in (tmp_log(calls)).read_text(encoding="utf-8")
    assert calls["wait_forever"] == 1
    assert len(calls["terminate"]) == 1
    assert not calls["lock"].exists()


def test_le_navigateur_indique_le_bouton_quit(run_main, capsys):
    """Ctrl+C is useless in a --noconsole build and in a Terminal=false
    .desktop: the message must point at the page instead."""
    run, calls = run_main
    calls["ui"] = "browser"
    assert run() == 0
    out = capsys.readouterr().out
    assert "Quit" in out and "http://127.0.0.1:8787" in out
    assert "Ctrl+C" not in out
    assert calls["wait_forever"] == 1


def test_le_message_d_instance_orpheline_ne_mentionne_pas_un_pid_mort(run_main, capsys,
                                                                      monkeypatch):
    """MINOR 5: the pid in the lock is dead and the server is alive. "already
    running (pid 424242)" would be a lie; the port is the fact."""
    run, calls = run_main
    calls["lock"].parent.mkdir(parents=True, exist_ok=True)
    calls["lock"].write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(launcher, "serves_status", lambda port: True)
    assert run() == 0
    out = capsys.readouterr().out
    assert "a server is already running on port 8787" in out
    assert "the launcher's pid 424242 is gone" in out
    assert calls["started"] == []


def test_pas_de_port_libre_est_signale_pas_traceback(run_main, capsys, monkeypatch):
    """MINOR 2: on a --noconsole build a traceback is invisible; the launcher says
    what happened and returns 1."""
    run, calls = run_main

    def _no_port(*_a, **_k):
        raise OSError("no free port in 8787..8806")

    monkeypatch.setattr(launcher, "pick_port", _no_port)
    assert run() == 1
    assert calls["started"] == []
    assert "no free port" in capsys.readouterr().out
    assert "no free port" in tmp_log(calls).read_text(encoding="utf-8")
    assert not calls["lock"].exists()   # the lock is still released


def test_un_serveur_qui_ne_demarre_pas_est_signale(run_main, capsys, monkeypatch):
    run, calls = run_main

    def _no_server(*_a, **_k):
        raise OSError("runtime/python/bin/python3: not found")

    monkeypatch.setattr(launcher, "start_server", _no_server)
    assert run() == 1
    assert "could not be started" in capsys.readouterr().out
    assert "could not be started" in tmp_log(calls).read_text(encoding="utf-8")
    assert not calls["lock"].exists()


def test_un_serveur_orphelin_n_est_pas_declenche(tmp_path, monkeypatch, run_main):
    """MINOR 1: a kill -9'd launcher leaves its server alive in its own session.
    The lock must not be reclaimed (that would load a second copy of the model on
    another port); the page of the live instance is opened instead."""
    run, calls = run_main
    calls["lock"].parent.mkdir(parents=True, exist_ok=True)
    calls["lock"].write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    probed = []
    monkeypatch.setattr(launcher, "serves_status",
                        lambda port: probed.append(port) or True)
    assert run() == 0
    assert probed == [8787]
    assert calls["started"] == []               # no second server
    assert calls["lock"].exists()               # the lock is kept
    assert calls["lock"].read_text().startswith("424242")


def test_no_window_sert_jusqu_a_l_arret_explicite(run_main):
    run, calls = run_main
    assert run("--no-window") == 0
    assert calls["open_ui"] == []
    assert calls["wait_forever"] == 1


def test_un_serveur_muet_arrete_le_lanceur(tmp_path, monkeypatch, run_main):
    """No answer on /api/status: exit 1, and the half-started server is stopped."""
    run, calls = run_main
    monkeypatch.setattr(launcher, "wait_ready", lambda *_a, **_k: None)
    assert run() == 1
    assert len(calls["terminate"]) == 1
    assert not calls["lock"].exists()


def test_main_refuse_un_deuxieme_serveur(tmp_path, monkeypatch, run_main):
    """Single instance: a live lock means no server, no window, exit 0."""
    run, calls = run_main
    calls["lock"].parent.mkdir(parents=True, exist_ok=True)
    # no port on the second line: _already_running() then does no request at all
    calls["lock"].write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert run() == 0
    assert calls["started"] == []
    assert calls["open_ui"] == []


# ------------------------------------------------------------------- misc

def test_install_root_retrouve_le_repertoire_installation(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "static").mkdir()
    assert launcher.install_root(tmp_path / "pyclean-audio.exe") == tmp_path


def test_python_exe_suit_la_plateforme(tmp_path, windows):
    root = Path(tmp_path)
    assert launcher.python_exe(root).parts[-4:] == ("runtime", "python", "bin", "python3")
    with windows:
        assert launcher.python_exe(root).name == "python.exe"


def test_un_etat_inutilisable_echoue_nettement(tmp_path):
    """A state directory that cannot be created is a hard error: falling back to
    the install directory would write under a read-only tree (Program Files)."""
    blocker = tmp_path / "etat"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        launcher.make_state_dirs(blocker)


def test_say_ne_leve_pas_sans_console(monkeypatch):
    """The frozen Windows launcher has no console: sys.stdout is None."""
    monkeypatch.setattr(sys, "stdout", None)
    launcher._say("hello")  # must not raise
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    launcher._say("hello")


def test_sigterm_declenche_le_nettoyage(monkeypatch):
    """Without it, `kill` would leave the server (and its ffmpeg) running."""
    caught = []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: caught.append((sig, handler)))
    launcher._install_signal_handlers()
    assert [sig for sig, _ in caught] == [signal.SIGTERM, signal.SIGHUP]
    assert all(h.__name__ == "_stop" for _, h in caught)


# ------------------------------------------------- verrou et instance orpheline

def test_serves_status_repond_vrai_pour_un_serveur(monkeypatch):
    monkeypatch.setattr(launcher.urllib.request, "urlopen",
                        lambda *_a, **_k: FakeResponse({"status": "ready"}))
    assert launcher.serves_status(8787) is True


@pytest.mark.parametrize("boom", [urllib.error.URLError("refused"), OSError("boom"),
                                  ValueError("bad json")])
def test_serves_status_repond_faux_sinon(monkeypatch, boom):
    def _raise(*_a, **_k):
        raise boom

    monkeypatch.setattr(launcher.urllib.request, "urlopen", _raise)
    assert launcher.serves_status(8787) is False


def test_un_verrou_avec_un_serveur_orphelin_n_est_pas_reclame(tmp_path, monkeypatch):
    """MINOR 1, at the source: pid dead but the port still answers -> keep the
    lock, refuse the launch (a second server would load a second copy of the
    model)."""
    p = tmp_path / "instance.lock"
    p.write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    assert launcher.acquire_lock(p, 9000, probe=lambda port: True) is None
    assert p.read_text(encoding="utf-8") == "424242\n8787\n"


def test_un_verrou_mort_sans_serveur_est_reclame(tmp_path, monkeypatch):
    """The usual crash case: nothing answers on the recorded port, reclaim it."""
    p = tmp_path / "instance.lock"
    p.write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    lock = launcher.acquire_lock(p, 9000, probe=lambda port: False)
    assert lock is not None
    assert launcher._read_lock(p) == (os.getpid(), 9000)


def test_acquire_lock_sans_sonde_ne_touche_a_rien(tmp_path, monkeypatch):
    """No probe: the pid decides, and a live one still wins."""
    p = tmp_path / "instance.lock"
    p.write_text("424242\n8787\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    assert launcher.acquire_lock(p, 9000) is None
    assert p.exists()


# ------------------------------------------------------------ journal du lanceur

def test_note_ajoute_une_ligne_datee(tmp_path):
    launcher._note(tmp_path / "state", "something happened")
    text = tmp_log({"lock": tmp_path / "state" / "instance.lock"}).read_text(encoding="utf-8")
    assert "something happened" in text
    assert str(os.getpid()) in text
    assert text.startswith(time.strftime("%Y"))


def test_note_ne_leve_pas_sur_un_journal_inutilisable(tmp_path):
    """A state directory that cannot be written must not crash the launcher."""
    blocker = tmp_path / "logs"
    blocker.write_text("not a directory", encoding="utf-8")
    launcher._note(tmp_path, "boom")  # must not raise
