"""Minimal §5 lesson ledger — append-only LESSONS.jsonl fed into builder prompts.

A lesson is a one-line operational guardrail learned during THIS build (a
classified fix, a seam miss): "symptom -> fix". Relevant lessons are injected
into later wave/fix prompts so the build stops repeating the same mistake. The
full §5.3 evidence-gated promotion pipeline (lessons -> repairs / NN-rules,
across builds, human-gated) is DEFERRED — this is within-build learning only,
JSONL-shaped so the full version can extend the same records.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_FILE = "LESSONS.jsonl"
_LOCK = threading.RLock()  # append is the only write; concurrent-builder safe
MAX_INJECT = 8
MAX_CHARS = 2400  # ~600 tokens, the §5.2 LES_MAX cap

# A small controlled vocabulary that makes a lesson relevant beyond its own
# module (framework tags + the seam/import discipline tags the classifier emits).
_SHARED_TAGS = {"fastapi", "sqlalchemy", "pydantic", "alembic", "pytest", "bcrypt",
                "react", "vite", "typescript", "vitest", "seam", "import", "auth",
                "hallucination", "breach"}


def _read(build_dir: Path) -> list[dict]:
    p = Path(build_dir) / _FILE
    if not p.is_file():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def record(build_dir: Path, module: str, symptom: str, fix: str, tags=()) -> None:
    """Append one lesson; skip an exact-duplicate (module, symptom, fix) so a
    recurring failure doesn't spam the ledger. Lock-safe (concurrent builder)."""
    symptom, fix = symptom.strip()[:240], fix.strip()[:240]
    if not symptom or not fix:
        return
    with _LOCK:
        existing = _read(build_dir)
        key = (module, symptom[:120], fix[:120])
        if any((r.get("module"), (r.get("symptom", ""))[:120],
                (r.get("fix", ""))[:120]) == key for r in existing):
            return
        rec = {"id": f"L-{len(existing) + 1:04d}",
               "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "module": module, "tags": sorted(set(tags))[:5],
               "symptom": symptom, "fix": fix}
        with (Path(build_dir) / _FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")


def for_prompt(build_dir: Path, module: str, extra_tags=()) -> str:
    """Rendered LESSONS block for a module's prompt: this module's own lessons
    plus lessons sharing a controlled tag, newest first, token-capped. '' if
    there is nothing relevant yet."""
    recs = _read(build_dir)
    if not recs:
        return ""
    want = (set(extra_tags) | {module})

    def relevant(r: dict) -> bool:
        return (r.get("module") == module
                or bool(set(r.get("tags", [])) & want & _SHARED_TAGS))

    lines, spent = [], 0
    for r in reversed(recs):  # newest first
        if not relevant(r):
            continue
        line = (f"- {r['id']} [{','.join(r.get('tags', [])[:2])}] "
                f"{r.get('symptom', '')} -> {r.get('fix', '')}")
        if len(lines) >= MAX_INJECT or spent + len(line) > MAX_CHARS:
            break
        lines.append(line)
        spent += len(line)
    if not lines:
        return ""
    return ("=== LESSONS (guardrails from earlier passes in THIS build — obey) ===\n"
            + "\n".join(lines))


def tags_from(message: str, extra=()) -> list[str]:
    """Framework/discipline tags present in an error message, for relevance."""
    low = message.lower()
    return sorted({t for t in _SHARED_TAGS if t in low} | set(extra))
