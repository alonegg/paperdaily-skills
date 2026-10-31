#!/usr/bin/env python3
"""Verify a release and link both Skills into selected local agent directories."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

SKILLS = ("paperdaily", "paperdaily-deep-research")
AGENTS = {"codex": ".codex/skills", "claude": ".claude/skills", "agents": ".agents/skills", "workbuddy": ".workbuddy/skills"}


def verify(root: Path) -> dict:
    manifest = json.loads((root / "release-manifest.json").read_text())
    for rel, expected in manifest["files"].items():
        name = Path(rel)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("invalid manifest path")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("missing or symlinked release file: " + rel)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("release hash mismatch: " + rel)
    for skill in SKILLS:
        required = ("SKILL.md", "lib/pd_runtime.py", "lib/pd_commands.py")
        if any(skill + "/" + name not in manifest["files"] for name in required):
            raise ValueError("release manifest misses required Skill files: " + skill)
        text = (root / skill / "SKILL.md").read_text()
        version = re.search(r"skill_ver[^0-9]*([0-9]+\.[0-9]+\.[0-9]+)", text)
        if version is None or version[1] != manifest["versions"][skill]:
            raise ValueError("version mismatch: " + skill)
        if not (root / skill / "lib" / "pd_runtime.py").is_file():
            raise ValueError("bundled runtime missing: " + skill)
    return manifest


def install(root: Path, home: Path, agents: list[str], apply: bool) -> dict:
    manifest = verify(root)
    results = []
    run = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    for agent in agents:
        folder = home / AGENTS[agent]
        for skill in SKILLS:
            dest, target = folder / skill, root / skill
            current = dest.is_symlink() and dest.resolve() == target.resolve()
            row = {
                "agent": agent,
                "skill": skill,
                "version": manifest["versions"][skill],
                "path": str(dest),
                "status": "current" if current else "update_needed",
            }
            if apply and not current:
                folder.mkdir(parents=True, exist_ok=True)
                if dest.exists() or dest.is_symlink():
                    backup = home / ".paperdaily-skill-backups" / run / agent / skill
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    dest.rename(backup)
                    row["backup"] = str(backup)
                dest.symlink_to(target.resolve(), target_is_directory=True)
                row["status"] = "updated"
            results.append(row)
    return {"verified": True, "applied": apply, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", choices=(*AGENTS, "all"), default="codex")
    parser.add_argument(
        "--home", type=Path, default=Path.home(), help="home directory (for isolated validation)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply", action="store_true", help="back up existing copies and update symlinks"
    )
    mode.add_argument("--check", action="store_true", help="read-only drift check (default)")
    args = parser.parse_args(argv)
    try:
        selected = list(AGENTS) if args.agent == "all" else [args.agent]
        result = install(Path(__file__).resolve().parent, args.home, selected, args.apply)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return int(any(r["status"] == "update_needed" for r in result["results"]))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write("Installation stopped: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
