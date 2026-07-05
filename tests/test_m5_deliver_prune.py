"""Phase-deliver guard: a model's stray recursive copy nests code/ into itself
(code/backend/code/backend/... until copytree hits WinError 1921). The prune
removes it; real files survive. No model calls."""
from __future__ import annotations

from phases import _prune_nested_code


def test_prune_removes_nested_code_keeps_real(tmp_path):
    code = tmp_path / "code"
    (code / "backend" / "app" / "core").mkdir(parents=True)
    (code / "backend" / "app" / "core" / "config.py").write_text("X = 1\n")
    (code / "frontend" / "src" / "api").mkdir(parents=True)
    (code / "frontend" / "src" / "api" / "client.ts").write_text("export {};\n")
    # the junk: a full copy of code/ nested under code/backend/, two levels deep
    junk = code / "backend" / "code" / "backend" / "code" / "backend"
    junk.mkdir(parents=True)
    (junk / "dupe.py").write_text("junk\n")

    removed = _prune_nested_code(code)

    assert removed >= 1
    assert not (code / "backend" / "code").exists()          # junk gone
    assert (code / "backend" / "app" / "core" / "config.py").is_file()  # real kept
    assert (code / "frontend" / "src" / "api" / "client.ts").is_file()


def test_prune_noop_on_clean_tree(tmp_path):
    code = tmp_path / "code"
    (code / "backend" / "app").mkdir(parents=True)
    (code / "backend" / "app" / "main.py").write_text("x = 1\n")
    assert _prune_nested_code(code) == 0
    assert (code / "backend" / "app" / "main.py").is_file()
