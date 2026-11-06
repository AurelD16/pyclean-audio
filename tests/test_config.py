r"""Configuration: where the results go, and the packaged-build flag.

`data_dir()` and `DESKTOP` are what the desktop launcher drives
(`packaging/launcher/launcher.py` sets `PYCLEAN_DATA_DIR` and `PYCLEAN_DESKTOP`):
without them a per-user install would try to write under `C:\Program Files` and
the page would keep telling the user to run a shell script.
"""

from app import config


def test_data_dir_par_defaut_dans_le_depot(monkeypatch):
    monkeypatch.delenv("PYCLEAN_DATA_DIR", raising=False)
    assert config.data_dir() == config.BASE / "data" / "jobs"


def test_data_dir_suit_pyclean_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("PYCLEAN_DATA_DIR", str(tmp_path / "state" / "jobs"))
    assert config.data_dir() == tmp_path / "state" / "jobs"


def test_data_dir_ignore_une_valeur_vide(monkeypatch):
    """An empty variable is not an override: the default must stay writable."""
    monkeypatch.setenv("PYCLEAN_DATA_DIR", "   ")
    assert config.data_dir() == config.BASE / "data" / "jobs"


def test_data_dir_hors_du_repertoire_installation(monkeypatch, tmp_path):
    """The install tree is read-only at run time: nothing is written in it."""
    root = tmp_path / "install"
    (root / "app").mkdir(parents=True)
    monkeypatch.setenv("PYCLEAN_DATA_DIR", str(tmp_path / "state" / "jobs"))
    d = config.data_dir()
    assert root not in d.parents
    assert d.is_relative_to(tmp_path / "state")


def test_desktop_vaut_faux_par_defaut(monkeypatch):
    monkeypatch.delenv("PYCLEAN_DESKTOP", raising=False)
    assert config.env_flag("PYCLEAN_DESKTOP", False) is False


def test_desktop_vient_de_l_environnement(monkeypatch):
    monkeypatch.setenv("PYCLEAN_DESKTOP", "1")
    assert config.env_flag("PYCLEAN_DESKTOP", False) is True
    monkeypatch.setenv("PYCLEAN_DESKTOP", "oui")
    assert config.env_flag("PYCLEAN_DESKTOP", False) is True


def test_main_predit_data_par_data_dir():
    """app/main.py must not build the path itself any more."""
    import inspect

    from app import main as m

    source = inspect.getsource(m)
    assert "DATA = data_dir()" in source
    assert m.DATA == config.data_dir()
    assert m.BASE is config.BASE
