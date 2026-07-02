"""Workspace path clamp — the single trust boundary for every file tool.

The model emits arbitrary path strings. Without this, `read_file("../../.env")`
or an absolute `C:\\Windows\\...` walks straight out of the workspace. Lifted
from opencode's `assertExternalDirectory` (tool/external-directory.ts): resolve
the model's path against the workspace root, then reject anything whose real
location is not inside that root.
"""
from __future__ import annotations

import os
from pathlib import Path


class PathEscape(Exception):
    """Raised when a model-supplied path resolves outside the workspace."""


def clamp_path(workspace_root: Path | str, model_path: str) -> Path:
    """Resolve `model_path` (relative to the workspace) and guarantee the
    result stays inside `workspace_root`. Symlinks are resolved before the
    check so a link can't be used to escape. Raises PathEscape on violation."""
    root = Path(workspace_root).resolve()
    raw = Path(model_path)
    candidate = raw if raw.is_absolute() else root / raw
    resolved = Path(os.path.realpath(candidate))
    # `is_relative_to` (3.9+) plus the equality case for the root itself.
    if resolved != root and not resolved.is_relative_to(root):
        raise PathEscape(f"path escapes workspace: {model_path!r} -> {resolved}")
    return resolved


def demo() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert clamp_path(root, "a/b.txt") == (root / "a" / "b.txt").resolve()
        assert clamp_path(root, ".") == root.resolve()
        for bad in ["../escape.txt", "../../etc/passwd", os.path.abspath(os.sep)]:
            try:
                clamp_path(root, bad)
            except PathEscape:
                pass
            else:
                raise AssertionError(f"escape not caught: {bad}")
    print("safety.demo OK")


if __name__ == "__main__":
    demo()
