"""Persistent state for the ECHARA v2 orchestrator.

PROJECT_STATE.json lives at the project root and is rewritten after every phase
transition. A run that crashes or is interrupted resumes from the last
completed phase.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

PHASES = ["INTAKE", "PLAN", "BUILD", "REPAIR", "VERIFY", "DELIVER"]
DONE = "DONE"
STATE_FILE = Path("PROJECT_STATE.json")
MAX_RETRIES = 3


@dataclass
class ProjectState:
    build_id: str
    build_dir: str
    current_phase: str
    completed_phases: list[str] = field(default_factory=list)
    started_at: str = ""
    last_updated: str = ""
    verdict: str = ""
    user_prompt: str = ""
    retry_count: int = 0
    last_error: str = ""  # exact verify failure fed back to Builder on retry

    @classmethod
    def new(cls) -> "ProjectState":
        # Microsecond suffix: second-resolution IDs collide when two builds
        # start back-to-back (eval chains do), silently sharing one build dir.
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        build_id = f"build_{ts}"
        now = datetime.now().isoformat(timespec="seconds")
        return cls(
            build_id=build_id,
            build_dir=f"builds/{build_id}",
            current_phase=PHASES[0],
            started_at=now,
            last_updated=now,
        )

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "ProjectState | None":
        if not path.exists():
            return None
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path = STATE_FILE) -> None:
        self.last_updated = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def mark_done(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}; expected one of {PHASES}")
        if phase not in self.completed_phases:
            self.completed_phases.append(phase)
        i = PHASES.index(phase)
        self.current_phase = PHASES[i + 1] if i + 1 < len(PHASES) else DONE

    def retry_build(self, error: str) -> bool:
        """VERIFY failed. If retries remain: record the exact error, rewind to
        BUILD, return True. Retries exhausted: return False (caller stamps the
        failed verdict)."""
        if self.retry_count >= MAX_RETRIES:
            return False
        self.retry_count += 1
        self.last_error = error
        self.current_phase = "BUILD"
        return True
