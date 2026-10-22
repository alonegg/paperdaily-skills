#!/usr/bin/env python3
"""Resolve the bundled runtime, with a source-checkout development fallback."""

from pathlib import Path
import sys

SKILL = Path(__file__).resolve().parents[1]
RUNTIME = SKILL / "lib"
if not (RUNTIME / "pd_commands.py").is_file():
    RUNTIME = SKILL.parent / "_shared"
sys.path.insert(0, str(RUNTIME))
from pd_commands import main

if __name__ == "__main__":
    raise SystemExit(main())
