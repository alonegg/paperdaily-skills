"""Validate local research artifacts without uploading or judging entailment."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

FULLTEXT = frozenset(("binary-pdf", "html-fulltext", "parsed-fulltext"))


def read_json(path: Path, problems: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append("%s: %s" % (path.name, type(exc).__name__))
        return None


def read_rows(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("expected objects")
        return rows
    except (OSError, ValueError) as exc:
        problems.append("%s: %s" % (path.name, type(exc).__name__))
        return []


def local_file(root: Path, name: Any, problems: list[str]) -> Path | None:
    if not isinstance(name, str) or not name:
        problems.append("reading source requires a relative path")
        return None
    if Path(name).is_absolute():
        problems.append("reading source requires a relative path")
        return None
    path = root / name
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve())
        if path.is_symlink() or not resolved.is_file():
            raise ValueError("not a regular local file")
        return resolved
    except (OSError, ValueError):
        problems.append("reading source must be a regular file inside the session directory")
        return None


def validate_session(
    directory: str, papers: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    root = Path(directory)
    problems: list[str] = []
    selected_file = root / "worklist.selected.jsonl"
    if not selected_file.exists():
        selected_file = root / "worklist.jsonl"
    selected = read_rows(selected_file, problems)
    selected_ids = [r.get("id") for r in selected]
    if any(not isinstance(pid, str) or not pid for pid in selected_ids):
        problems.append("selected papers require nonempty ids")
    if len(set(str(p) for p in selected_ids)) != len(selected_ids):
        problems.append("selected paper ids are duplicated")
    if selected_file.name == "worklist.selected.jsonl":
        pool_ids = {
            r.get("id")
            for r in read_rows(root / "worklist.jsonl", problems)
            if isinstance(r.get("id"), str)
        }
        if any(pid not in pool_ids for pid in selected_ids if isinstance(pid, str)):
            problems.append(
                "selected list must be a subset of worklist; register external sources separately"
            )
    external_file = root / "external_sources.jsonl"
    external = read_rows(external_file, problems) if external_file.exists() else []
    external_ids = {r.get("id") for r in external if isinstance(r.get("id"), str)}
    if any(not r.get("id") or not r.get("title") or not r.get("reason") for r in external):
        problems.append("external sources require id, title and registration reason")
    manifest = read_json(root / "reading_manifest.json", problems)
    if not isinstance(manifest, dict):
        manifest = {}
        problems.append(
            "reading_manifest.json required: materials and actual reading must be recorded"
        )
    entries = manifest.get("papers", [])
    skips = manifest.get("skipped", [])
    if not isinstance(entries, list) or any(not isinstance(r, dict) for r in entries):
        entries = []
        problems.append("reading manifest papers must be an object array")
    if not isinstance(skips, list) or any(not isinstance(r, dict) for r in skips):
        skips = []
        problems.append("reading manifest skipped must be an object array")
    read_by_id = {r.get("paper_id"): r for r in entries if isinstance(r.get("paper_id"), str)}
    skipped = {r.get("paper_id"): r for r in skips if isinstance(r.get("paper_id"), str)}
    if len(read_by_id) != len(entries):
        problems.append("reading manifest has duplicate or invalid paper ids")
    if len(skipped) != len(skips) or any(not r.get("reason") for r in skips):
        problems.append("skips require unique paper ids and explicit reasons")
    note_ids = {r["paper_id"] for r in papers}
    if len(note_ids) != len(papers):
        problems.append("multiple notes have the same paper_id")
    registered = {pid for pid in selected_ids if isinstance(pid, str)} | external_ids
    for pid in registered - note_ids - set(skipped):
        problems.append("selected/registered paper has neither note nor skip: %s" % pid)
    for pid in (note_ids | set(read_by_id) | set(skipped)) - registered:
        problems.append("unregistered paper in artifacts: %s" % pid)
    for pid in set(read_by_id) - note_ids:
        problems.append("reading manifest has no matching note: %s" % pid)
    for pid in note_ids & set(skipped):
        problems.append("paper is both read and skipped: %s" % pid)
    updated = []
    fulltext_ids: set[str] = set()
    anchors: dict[str, set[tuple[str, str]]] = {}
    for paper in papers:
        pid = paper["paper_id"]
        record = read_by_id.get(pid)
        if record is None:
            problems.append("note has no reading manifest entry: %s" % pid)
            continue
        depth = record.get("depth")
        if depth not in ("fulltext", "abstract-only"):
            problems.append("invalid reading depth: %s" % pid)
            continue
        if not all(record.get(k) for k in ("model", "created_at")):
            problems.append("reading entry requires model and created_at: %s" % pid)
        try:
            created = datetime.fromisoformat(
                str(record.get("created_at", "")).replace("Z", "+00:00")
            )
            if created.tzinfo is None or not isinstance(record.get("model"), str):
                raise ValueError("timezone/model required")
        except ValueError:
            problems.append(
                "reading entry requires a model and timezone-aware ISO timestamp: %s" % pid
            )
        source = record.get("source")
        if not isinstance(source, dict):
            source = {}
        source_path = local_file(root, source.get("path"), problems)
        if source_path is not None:
            hasher = hashlib.sha256()
            with source_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            digest = hasher.hexdigest()
            if digest != source.get("sha256"):
                problems.append("source hash mismatch: %s" % pid)
        if depth == "fulltext":
            if re.search(r"\*\*depth\*\*\s*:\s*abstract-only", paper.get("note_md", ""), re.I):
                problems.append("note depth contradicts the reading manifest: %s" % pid)
            if source.get("carrier") not in FULLTEXT or source.get("partial"):
                problems.append("fulltext requires a complete fulltext carrier: %s" % pid)
            if not record.get("reading_complete"):
                problems.append("fulltext requires an explicit completed reading record: %s" % pid)
            if source.get("carrier") == "binary-pdf":
                pages, count = record.get("pages_read"), record.get("page_count")
                if (
                    not isinstance(count, int)
                    or isinstance(count, bool)
                    or not 1 <= count <= 10000
                    or not isinstance(pages, list)
                    or any(not isinstance(p, int) or isinstance(p, bool) for p in pages)
                    or set(pages) != set(range(1, count + 1))
                ):
                    problems.append(
                        "fulltext PDF requires all pages read (1..page_count): %s" % pid
                    )
            fulltext_ids.add(pid)
        raw_anchors = record.get("anchors", [])
        if not isinstance(raw_anchors, list) or any(not isinstance(a, dict) for a in raw_anchors):
            raw_anchors = []
            problems.append("anchors must be objects: %s" % pid)
        if not raw_anchors:
            problems.append("reading entry needs at least one source anchor: %s" % pid)
        anchor_set = set()
        for anchor in raw_anchors:
            location, quote = anchor.get("location"), anchor.get("quote")
            if (
                not isinstance(location, str)
                or not location.strip()
                or not isinstance(quote, str)
                or not quote.strip()
            ):
                problems.append("anchor requires location and quote: %s" % pid)
                continue
            if depth == "abstract-only" and re.search(r"\bp(?:age)?\.?\s*\d+", location, re.I):
                problems.append("abstract-only source cannot assert PDF page anchors: %s" % pid)
            if depth == "fulltext" and source.get("carrier") == "binary-pdf":
                pages = record.get("pages_read", [])
                count = record.get("page_count")
                located = re.search(r"\bp(?:age)?\.?\s*(\d+)", location, re.I)
                if not isinstance(count, int) or count < 1 or not isinstance(pages, list):
                    problems.append("PDF reading requires page_count and pages_read: %s" % pid)
                elif any(not isinstance(p, int) or not 1 <= p <= count for p in pages):
                    problems.append("invalid pages_read: %s" % pid)
                elif located and (int(located[1]) not in pages or int(located[1]) > count):
                    problems.append("evidence page was not read or is out of range: %s" % pid)
                elif located is None:
                    problems.append("PDF anchor requires a page number: %s" % pid)
            anchor_set.add((location.strip(), quote.strip()))
        anchors[pid] = anchor_set
        updated.append({**paper, "depth": depth})
    for i, claim in enumerate(claims, 1):
        evidence = claim.get("evidence") or []
        if claim.get("status") != "gap" and not evidence:
            problems.append("non-gap claim %d needs evidence" % i)
        if len(evidence) > 20:
            problems.append("claim %d exceeds 20 evidence entries" % i)
        for ev in evidence:
            pid = ev.get("paper_id")
            if pid not in note_ids:
                problems.append("claim %d cites a source without a registered note: %s" % (i, pid))
            elif (ev.get("location", "").strip(), ev.get("quote", "").strip()) not in anchors.get(
                pid, set()
            ):
                problems.append("claim %d evidence has no matching reading anchor: %s" % (i, pid))
    coverage = {
        "selected": len(selected_ids),
        "external_sources": len(external_ids),
        "notes": len(note_ids),
        "skipped": len(skipped),
        "fulltext_read": len(fulltext_ids),
        "abstract_only": len(note_ids - fulltext_ids),
        "claim_count": len(claims),
        "validation": "structural consistency and source hashes; not entailment or truth verification",
    }
    return updated, problems, coverage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True)
    args = parser.parse_args()
    from upload_session import build_payload

    payload, problems = build_payload(args.dir, None, None)
    sys.stdout.write(
        json.dumps(
            {"passed": payload is not None, "problems": problems}, ensure_ascii=False, indent=2
        )
        + "\n"
    )
    return 0 if payload is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
