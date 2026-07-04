"""Phase A — deterministic index layer + flat wave context. No model calls."""
from __future__ import annotations

from pathlib import Path

import pytest

from agents import interfaces


# --- Python signature extraction --------------------------------------------

def test_py_signatures():
    src = (
        "import os\n"
        "SECRET_KEY = 'x'\n"
        "def make(a: int, b: str = 'y') -> bool:\n    return True\n"
        "async def fetch(url: str) -> dict:\n    ...\n"
        "class Svc(Base):\n"
        "    def get(self, id: int) -> 'Note':\n        ...\n"
        "    async def add(self, n: 'Note') -> None:\n        ...\n"
        "    def _private(self):\n        ...\n"
    )
    sigs = interfaces.py_signatures(src)
    # Index is a signature sketch: names + annotations + return type (default
    # VALUES are intentionally omitted — the model reads the file for those).
    assert "def make(a: int, b: str) -> bool" in sigs
    assert "async def fetch(url: str) -> dict" in sigs
    assert "class Svc(Base):" in sigs
    assert "    def get(self, id: int) -> 'Note'" in sigs
    assert "    async def add(self, n: 'Note') -> None" in sigs
    assert "SECRET_KEY" in sigs
    assert not any("_private" in s for s in sigs)  # private methods excluded


def test_py_signatures_syntax_error_safe():
    assert interfaces.py_signatures("def broken(:\n") == []


# --- TypeScript export extraction -------------------------------------------

def test_ts_exports():
    src = (
        "import x from 'y';\n"
        "export interface Note { id: number; title: string }\n"
        "export type NoteList = Note[];\n"
        "export const client = (base: string) => ({});\n"
        "export async function getNotes(): Promise<Note[]> { return []; }\n"
        "const internal = 1;\n"  # not exported
    )
    ex = interfaces.ts_exports(src)
    joined = " | ".join(ex)
    assert "interface Note" in joined
    assert "type NoteList" in joined
    assert "const client" in joined
    assert "function getNotes" in joined
    assert "internal" not in joined


# --- module interface index --------------------------------------------------

def _mk(root: Path, rel: str, content: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_module_interface_and_readback(tmp_path):
    build = tmp_path / "build"
    code = build / "code"
    _mk(code, "backend/app/services/note.py",
        "def list_notes(user_id: int) -> list:\n    return []\n")
    _mk(code, "backend/app/models/note.py", "class Note(Base):\n    pass\n")
    interfaces.write_module_interface(build, "app", code)
    idx = interfaces.read_interfaces(build, ["app"])
    assert "def list_notes(user_id: int) -> list" in idx
    assert "class Note(Base):" in idx
    assert "services/note.py" in idx
    assert interfaces.read_interfaces(build, ["nonexistent"]) == ""


# --- seam conformance --------------------------------------------------------

def test_check_seams(tmp_path):
    build = tmp_path / "build"
    code = build / "code"
    _mk(code, "backend/app/services/auth.py",
        "def hash_password(pw: str) -> str:\n    return pw\n"
        "def verify(pw: str, h: str) -> bool:\n    return True\n")
    interfaces.write_module_interface(build, "auth", code)
    seams = {"auth": [{"name": "hash_password"}, {"name": "verify"},
                      {"name": "make_token"}]}  # last one not implemented
    miss = interfaces.check_seams(build, seams)
    assert len(miss) == 1 and "make_token" in miss[0]
    # fully-implemented seam is clean
    assert interfaces.check_seams(build, {"auth": [{"name": "verify"}]}) == []


# --- flat context + empty-file guard (builder) ------------------------------

def test_wave_prompt_uses_interfaces_not_guessing():
    from agents.builder import _wave_prompt, _wave_context
    prompt = _wave_prompt(2, 5, ["code/backend/app/x.py"], "CTX")
    # The drift-inducing "READ earlier waves and guess" language is gone.
    assert "READ them" not in prompt
    assert "INTERFACES section" in prompt


def test_wave_context_injects_interface_index(tmp_path):
    from agents.builder import _wave_context
    build = tmp_path / "b"
    code = build / "code"
    _mk(code, "backend/app/db.py", "def get_session() -> object:\n    return object()\n")
    interfaces.write_module_interface(build, "app", code)
    ctx = _wave_context(build, "app", [], "PLAN", "CONTRACT", None)
    assert "INTERFACES ALREADY ON DISK" in ctx
    assert "def get_session() -> object" in ctx


def test_empty_file_list_guard(tmp_path, monkeypatch):
    from agents import builder
    build = tmp_path / "b"
    build.mkdir()
    # A plan whose manifest uses a non-code/backend root -> _implementation_order
    # returns [] -> must RAISE, not dispatch an empty wave (#5).
    (build / "PLAN.md").write_text("## File manifest\nsrc/main.py — entry\n", encoding="utf-8")
    (build / "CONTRACT_REGISTRY.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(builder.skills_router, "DEFAULT_POOL_ROOT", tmp_path / "nopool")
    with pytest.raises(builder.BuildDispatchFailed, match="empty file list"):
        builder.run_builder(build)
