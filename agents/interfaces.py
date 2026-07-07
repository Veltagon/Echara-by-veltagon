"""Deterministic code index — the second brain for scale builds.

Extracts real signatures from code ALREADY ON DISK (stdlib `ast` for Python,
regex for TS/TSX) so a fresh wave session is handed accurate interfaces instead
of guessing — the drift mechanism at 180+ files (M5 plan breakage #2). Because
the index is derived from disk it can never be hallucinated, and it costs zero
tokens to produce (M5 minimal-burn directive: deterministic over LLM).

Also the seam-conformance checker: does a module actually export the signatures
SEAMS.json promised? (DoD "seam conformance" gate.)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_CODE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx"}


# --- Python (AST) ------------------------------------------------------------

def _fmt_args(a: ast.arguments) -> str:
    parts: list[str] = []
    posonly = getattr(a, "posonlyargs", [])
    for arg in posonly:
        parts.append(_fmt_one(arg))
    if posonly:
        parts.append("/")
    for arg in a.args:
        parts.append(_fmt_one(arg))
    if a.vararg:
        parts.append("*" + _fmt_one(a.vararg))
    elif a.kwonlyargs:
        parts.append("*")
    for arg in a.kwonlyargs:
        parts.append(_fmt_one(arg))
    if a.kwarg:
        parts.append("**" + _fmt_one(a.kwarg))
    return ", ".join(parts)


def _fmt_one(arg: ast.arg) -> str:
    if arg.annotation is not None:
        try:
            return f"{arg.arg}: {ast.unparse(arg.annotation)}"
        except Exception:  # noqa: BLE001
            return arg.arg
    return arg.arg


def _ret(node) -> str:
    if getattr(node, "returns", None) is not None:
        try:
            return " -> " + ast.unparse(node.returns)
        except Exception:  # noqa: BLE001
            return ""
    return ""


def py_signatures(source: str) -> list[str]:
    """Top-level defs/classes (with public methods) and UPPER_CASE constants."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pre = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append(f"{pre} {node.name}({_fmt_args(node.args)}){_ret(node)}")
        elif isinstance(node, ast.ClassDef):
            bases = ", ".join(
                b.id for b in node.bases if isinstance(b, ast.Name)
            )
            out.append(f"class {node.name}({bases}):" if bases else f"class {node.name}:")
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    pre = "async def" if isinstance(m, ast.AsyncFunctionDef) else "def"
                    out.append(f"    {pre} {m.name}({_fmt_args(m.args)}){_ret(m)}")
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and (t.id.isupper() or t.id[0].isupper()):
                    out.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.append(node.target.id)
    return out


# --- TypeScript / JS (regex — no TS parser dep) ------------------------------

_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:default\s+)?"
    r"(?:async\s+)?(function|const|let|var|class|interface|type|enum)\s+"
    r"([A-Za-z_$][\w$]*)"
    r"([^\n{=;]*)",
    re.MULTILINE,
)


def ts_exports(source: str) -> list[str]:
    out: list[str] = []
    for kind, name, tail in _TS_EXPORT.findall(source):
        sig = f"{kind} {name}{tail.rstrip()}".strip()
        out.append(re.sub(r"\s+", " ", sig))
    return out


# --- file / module level -----------------------------------------------------

def file_interface(path: Path) -> list[str]:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.suffix == ".py":
        return py_signatures(src)
    if path.suffix in (".ts", ".tsx", ".js", ".jsx"):
        return ts_exports(src)
    return []


# Dirs that must NEVER be indexed — dependency/build output, not source. Without
# this a frontend module whose path_root is `code/frontend` sweeps `node_modules`
# (where npm install lands): the index exploded 421KB -> 3.8MB (518 files, mostly
# @babel/*), 94% of a frontend wave prompt, blowing past every model's context and
# making frontend modules unbuildable (E3-v2, 2026-07-07). Size-capped as a second
# guard so no single module can ever dominate a wave's context.
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", "dist", "build", ".venv",
                        ".verify_venv", ".next", ".turbo", "coverage", "out", ".git"})
_MAX_INDEX_BYTES = 60_000  # ~15k tokens; a real module's signatures are far under this


def module_interface_md(module_dir: Path, module_name: str) -> str:
    """Markdown interface index for one module: per-file exported signatures.
    Only real source files are listed (dependency/build dirs excluded) and the
    whole index is byte-capped, so one module can't blow up a wave's context."""
    if not module_dir.is_dir():
        return f"## {module_name}\n(no files yet)\n"
    lines = [f"## module: {module_name}"]
    size = len(lines[0])
    for f in sorted(module_dir.rglob("*")):
        if f.suffix not in _CODE_SUFFIXES:
            continue
        rel = f.relative_to(module_dir)
        if _SKIP_DIRS & set(rel.parts):  # skip only dirs WITHIN the module (not ancestors)
            continue
        sigs = file_interface(f)
        if not sigs:
            continue
        block = f"\n### {module_name}/{rel.as_posix()}\n" + "\n".join(sigs)
        if size + len(block) > _MAX_INDEX_BYTES:
            lines.append(f"\n### … index truncated at {_MAX_INDEX_BYTES} bytes")
            break
        lines.append(block)
        size += len(block)
    return "\n".join(lines) + "\n"


def write_module_interface(build_dir: Path, module_name: str, module_dir: Path) -> Path:
    """Regenerate interfaces/<module>.md from disk. Called after every wave."""
    dest = build_dir / "interfaces" / f"{module_name}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(module_interface_md(module_dir, module_name), encoding="utf-8")
    return dest


def read_interfaces(build_dir: Path, module_names: list[str]) -> str:
    """Concatenate the interface indexes for the named modules (a wave's own
    module + its declared dependencies). Missing ones are skipped."""
    out = []
    for name in module_names:
        p = build_dir / "interfaces" / f"{name}.md"
        if p.is_file():
            out.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(out)


# --- seam conformance --------------------------------------------------------

def _norm(sig: str) -> str:
    return re.sub(r"\s+", "", sig)


def check_seams(build_dir: Path, seams: dict) -> list[str]:
    """Deterministic DoD gate: every export SEAMS.json promises for a module
    must appear (by name) in that module's on-disk interface. Returns a list of
    mismatch strings (empty = conformant).

    SEAMS.json shape: {"<module>": [{"name": "...", "signature": "..."}, ...]}.
    Name presence is required; signature is advisory (reported, not failed, to
    avoid false negatives on formatting differences)."""
    mismatches: list[str] = []
    for module, exports in (seams or {}).items():
        p = build_dir / "interfaces" / f"{module}.md"
        index = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
        index_names = set(re.findall(r"\b([A-Za-z_$][\w$]*)\b", index))
        for exp in exports or []:
            name = exp.get("name") if isinstance(exp, dict) else str(exp)
            if not name:
                continue
            if name not in index_names:
                mismatches.append(f"{module}: missing declared export {name!r}")
    return mismatches
