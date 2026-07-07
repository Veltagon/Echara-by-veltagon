"""Verifier robustness (E3-v2 post-mortem blocker #1): discover the app entry
instead of hardcoding `from app.main import app`."""
from agents.verifier import _discover_app_import


def test_discovers_factory_app_at_nonstandard_path(tmp_path):
    be = tmp_path / "backend"
    (be / "bootstrap").mkdir(parents=True)
    (be / "bootstrap" / "__init__.py").write_text("")
    (be / "bootstrap" / "main.py").write_text(
        "from bootstrap.app_factory import create_app\napp: FastAPI = create_app()\n")
    (be / "app").mkdir()
    (be / "app" / "config.py").write_text("SETTINGS = 1\n")  # decoy, no app var
    assert _discover_app_import(be) == "from bootstrap.main import app"


def test_prefers_main_py_and_plain_assignment(tmp_path):
    be = tmp_path / "backend"
    (be / "app").mkdir(parents=True)
    (be / "app" / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert _discover_app_import(be) == "from app.main import app"


def test_skips_node_modules_and_falls_back(tmp_path):
    be = tmp_path / "backend"
    nm = be / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "server.py").write_text("app = FastAPI()\n")  # must be ignored
    assert _discover_app_import(be) == "from app.main import app"  # default fallback
