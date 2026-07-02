"""Ensures the repo root is importable as top-level packages (skills, providers,
harness) when pytest collects from tests/."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
