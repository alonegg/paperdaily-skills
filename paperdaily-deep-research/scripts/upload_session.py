#!/usr/bin/env python3
"""Validate and upload derived research artifacts; originals remain local.

Requires worklist(.selected).jsonl, notes, claims, report and reading_manifest.
Non-gap claims require registered reading anchors. Fulltext depth requires a
local source hash and an explicit reading record, not merely a fetch success.
--dry-run validates without a network call. --revise ID saves an immutable
revision; creation deduplication is verified by reading the existing content.
A local upload_receipt.json prevents edited content being silently re-created.
Exit codes: 0 verified success, 1 configuration, 2 gate failure, 3 HTTP/unverified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

_SKILL_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = _SKILL_ROOT / "lib"
if not (_RUNTIME / "pd_runtime.py").is_file():
    _RUNTIME = _SKILL_ROOT.parent / "_shared"
sys.path.insert(0, str(_RUNTIME))
from pd_runtime import (
    ApiError,
    Client,
    Settings,
    content_hash,
    load_settings,
    path_segment,
    utc_now,
    write_json,
)

# ─────────────────────────── constants (server contract, 0.8.0) ───────────────────────────
MAX_PAPERS = 100
MAX_NOTE_BYTES = 64 * 1024
MAX_CLAIMS = 200
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_QUOTE_CHARS = 500
CLAIM_STATUSES = ("supported", "weak", "contested", "gap")

SKILL_VER_FALLBACK = "0.7.0"
DEFAULT_TIMEOUT = 60
MAX_POST_ATTEMPTS = 3  # AGENT_PROTOCOL §6: backoff-retry cap

_ABSTRACT_ONLY_RE = re.compile(r"abstract[-_ ]only", re.IGNORECASE)
_PAPER_ID_FIELD_RE = re.compile(r"\*\*paper_id\*\*\s*[:：]\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)
_SKILL_VER_RE = re.compile(r"skill_ver[^0-9]*([0-9]+\.[0-9]+\.[0-9]+)")


# ─────────────────────────── small helpers ───────────────────────────
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _skill_ver() -> str:
    """The skill_ver declared in ../SKILL.md next to this script."""
    skill_md = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "SKILL.md")
    text = _read_text(os.path.normpath(skill_md))
    if text:
        m = _SKILL_VER_RE.search(text)
        if m:
            return m.group(1)
    return SKILL_VER_FALLBACK


def _model_from_env() -> Optional[str]:
    for var in ("PD_MODEL", "ANTHROPIC_MODEL", "CLAUDE_MODEL"):
        val = os.environ.get(var)
        if val:
            return val
    return None


def _worklist_fingerprint(path: str) -> str:
    """sha256 over worklist.jsonl lines, each stripped, joined by '\\n'."""
    text = _read_text(path) or ""
    canon = "\n".join(line.strip() for line in text.splitlines())
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


try:
    # The notes/ and pdfs/ filenames are produced by the sibling Stage-2
    # script's fetch_fulltext._safe_filename — reuse it so the two sanitisers
    # can never drift. Importable when this script runs directly (sys.path[0]
    # is this scripts/ directory).
    from fetch_fulltext import _safe_filename as _ff_safe_filename

    def _safe_stem(paper_id: str) -> str:
        """fetch_fulltext._safe_filename minus its .pdf suffix."""
        return _ff_safe_filename(paper_id)[: -len(".pdf")]
except ImportError:
    # Fallback for when this module is imported from outside scripts/ and the
    # sibling is not on sys.path: a local mirror of
    # fetch_fulltext._safe_filename minus the .pdf suffix. Keep the two
    # implementations byte-identical.
    def _safe_stem(paper_id: str) -> str:
        name = re.sub(r"[^A-Za-z0-9._-]", "_", paper_id.strip())
        return (name.strip("._") or "paper")[:200]


def _has_nul(s: str) -> bool:
    return "\x00" in s


def _web_base(pd_base: str) -> str:
    base = pd_base.rstrip("/")
    if base.endswith("/api/v1"):
        base = base[: -len("/api/v1")]
    return base


# ─────────────────────────── artifact loading ───────────────────────────
# A record counts as full text if the *content* is full text, whatever shape it
# arrived in. Before 0.5.0 this was inferred from "did fetch_fulltext.py save a
# .pdf", so a paper whose PDF had been fully parsed to page-ordered Markdown —
# a perfectly good close read — was graded `abstract-only` and the operator had
# to hand-build a side ledger to say otherwise. Carrier is the field that says
# what was actually read; status stays as the fallback for older ledgers.
_FULLTEXT_CARRIERS = frozenset(("binary-pdf", "html-fulltext", "parsed-fulltext"))

# Every ledger a stage may have written. They are merged, later files winning,
# so the browser layer's results count without anyone merging them by hand.
_LEDGER_FILES = ("fetch_report.jsonl", "browser_fetch_report.jsonl", "fulltext_report.jsonl")


def _load_fetch_report(session_dir: str) -> Dict[str, str]:
    """Map raw paper_id AND its sanitised stem → 'fulltext' / 'partial'.

    Reads every ledger in _LEDGER_FILES; a paper that reaches full text in any
    of them is full text.
    """
    depth_by_key: Dict[str, str] = {}
    for fname in _LEDGER_FILES:
        text = _read_text(os.path.join(session_dir, fname))
        if not text:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            pid = str(rec.get("id") or "").strip()
            if not pid:
                continue
            carrier = str(rec.get("carrier") or "").strip()
            status = str(rec.get("status") or "")
            is_full = carrier in _FULLTEXT_CARRIERS or status in ("ok", "already")
            # A teaser extract is a real PDF with a real text layer, so it carries
            # `binary-pdf` and would otherwise be graded full text. It is not: the
            # page numbers in a note taken from it do not exist in the article.
            # Any ledger may flag it; the gate refuses to count it either way.
            if rec.get("partial") is True or str(rec.get("outcome") or "") == "partial":
                is_full = False
            if not is_full and depth_by_key.get(pid) == "fulltext":
                continue  # never downgrade a paper another ledger already landed
            depth_by_key[pid] = "fulltext" if is_full else "partial"
            depth_by_key[_safe_stem(pid)] = depth_by_key[pid]
    return depth_by_key


def _collect_papers(session_dir: str, problems: List[str]) -> List[dict]:
    """notes/*.md → papers[] with depth inference."""
    notes_dir = os.path.join(session_dir, "notes")
    papers: List[dict] = []
    if not os.path.isdir(notes_dir):
        problems.append("notes/ directory missing (Stage 3 not run?)")
        return papers
    fetch_status = _load_fetch_report(session_dir)
    note_files = sorted(f for f in os.listdir(notes_dir) if f.endswith(".md"))
    if not note_files:
        problems.append("notes/ has no *.md files")
        return papers
    for fname in note_files:
        fpath = os.path.join(notes_dir, fname)
        if os.path.islink(fpath) or os.path.commonpath(
            (os.path.realpath(session_dir), os.path.realpath(fpath))
        ) != os.path.realpath(session_dir):
            problems.append("notes/%s must stay inside the session directory" % fname)
            continue
        body = _read_text(fpath)
        if body is None or not body.strip():
            problems.append("notes/%s is empty" % fname)
            continue
        if _has_nul(body):
            problems.append("notes/%s contains binary (NUL byte) content — refused" % fname)
            continue
        if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
            problems.append(
                "notes/%s exceeds the %d KB note limit" % (fname, MAX_NOTE_BYTES // 1024)
            )
            continue
        stem = fname[: -len(".md")]
        # raw paper_id: the note body's **paper_id** field is mandatory —
        # the sanitised filename is lossy (`/` and `:` become `_`), so a
        # silent filename fallback would upload corrupted ids. Gate instead.
        m = _PAPER_ID_FIELD_RE.search(body)
        paper_id = m.group(1).strip() if m else ""
        if not paper_id:
            problems.append(
                "notes/%s is missing the **paper_id** field — add a `**paper_id**: <raw id>` line per the reading-note-template"
                % fname
            )
            continue
        # depth: explicit note-header marker wins; else fetch ledger; never upgrade.
        header = "\n".join(body.splitlines()[:60])
        if _ABSTRACT_ONLY_RE.search(header):
            depth = "abstract-only"
        else:
            status = fetch_status.get(paper_id) or fetch_status.get(stem)
            depth = "fulltext" if status == "fulltext" else "abstract-only"
        papers.append({"paper_id": paper_id, "depth": depth, "note_md": body})
    if len(papers) > MAX_PAPERS:
        problems.append("papers count %d exceeds the limit of %d" % (len(papers), MAX_PAPERS))
    return papers


def _collect_claims(session_dir: str, problems: List[str]) -> List[dict]:
    """synthesis/claims.jsonl → claims[] (four-state ledger)."""
    path = os.path.join(session_dir, "synthesis", "claims.jsonl")
    if not os.path.isfile(path):
        path = os.path.join(session_dir, "claims.jsonl")  # tolerated fallback
    claims: List[dict] = []
    text = _read_text(path)
    if text is None:
        problems.append("synthesis/claims.jsonl missing")
        return claims
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            problems.append("claims.jsonl line %d: invalid JSON (%s)" % (lineno, str(e)[:80]))
            continue
        if not isinstance(rec, dict):
            problems.append("claims.jsonl line %d: not a JSON object" % lineno)
            continue
        claim = str(rec.get("claim") or "").strip()
        status = rec.get("status")
        if not claim:
            problems.append("claims.jsonl line %d: empty claim" % lineno)
            continue
        if status not in CLAIM_STATUSES:
            problems.append(
                "claims.jsonl line %d: status %r not in %s"
                % (lineno, status, "/".join(CLAIM_STATUSES))
            )
            continue
        evidence_in = rec.get("evidence") or []
        if not isinstance(evidence_in, list):
            problems.append("claims.jsonl line %d: evidence must be an array" % lineno)
            continue
        evidence: List[dict] = []
        for i, ev in enumerate(evidence_in):
            if not isinstance(ev, dict):
                problems.append(
                    "claims.jsonl line %d evidence %d: not a JSON object" % (lineno, i + 1)
                )
                continue
            quote = str(ev.get("quote") or ev.get("quote_or_paraphrase") or "").strip()
            if len(quote) > MAX_QUOTE_CHARS:
                problems.append(
                    "claims.jsonl line %d evidence %d: quote is %d chars (limit %d) — shorten it, do not upload long verbatim excerpts"
                    % (lineno, i + 1, len(quote), MAX_QUOTE_CHARS)
                )
                continue
            paper_id = str(ev.get("paper_id") or "").strip()
            location = str(ev.get("location") or "").strip()
            empty_fields = [
                name
                for name, val in (("paper_id", paper_id), ("location", location), ("quote", quote))
                if not val
            ]
            if empty_fields:
                problems.append(
                    "claims.jsonl line %d evidence %d: empty %s — every evidence entry needs non-empty paper_id / location / quote"
                    % (lineno, i + 1, ", ".join(empty_fields))
                )
                continue
            evidence.append(
                {
                    "paper_id": paper_id,
                    "location": location,
                    "quote": quote,
                }
            )
        claims.append(
            {
                "claim": claim,
                "status": status,
                "evidence": evidence,
                "note": str(rec.get("note") or ""),
            }
        )
    if not claims:
        problems.append("claims.jsonl has no valid claim rows")
    else:
        eligible = [c for c in claims if c["status"] != "gap"]
        if any(not c["evidence"] for c in eligible):
            problems.append(
                "every non-gap claim must carry evidence; unsupported claims must be gap"
            )
    if len(claims) > MAX_CLAIMS:
        problems.append("claims count %d exceeds the limit of %d" % (len(claims), MAX_CLAIMS))
    return claims


def _load_meta(session_dir: str) -> dict:
    text = _read_text(os.path.join(session_dir, "worklist.meta.json"))
    if text:
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return {}


def _default_title(session_dir: str, report_md: Optional[str], meta: dict) -> str:
    if report_md:
        for line in report_md.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    name = meta.get("taxon_name")
    if name:
        return "Deep research: %s" % name
    return os.path.basename(os.path.normpath(session_dir))


# ─────────────────────────── payload assembly ───────────────────────────
def build_payload(
    session_dir: str, title: Optional[str], taxonomy: Optional[str]
) -> Tuple[Optional[dict], List[str]]:
    """Run the phase gate and assemble the payload. Returns (payload, problems);
    payload is None when the gate fails."""
    problems: List[str] = []

    worklist_path = os.path.join(session_dir, "worklist.jsonl")
    if not os.path.isfile(worklist_path):
        problems.append("worklist.jsonl missing (needed for the idempotency fingerprint)")

    papers = _collect_papers(session_dir, problems)
    claims = _collect_claims(session_dir, problems)
    from pd_evidence import validate_session

    papers, evidence_problems, coverage = validate_session(session_dir, papers, claims)
    problems.extend(evidence_problems)

    report_md = _read_text(os.path.join(session_dir, "report.md"))
    if report_md is None or not report_md.strip():
        problems.append("report.md missing or empty")
        report_md = None
    elif len(report_md.encode("utf-8")) > MAX_REPORT_BYTES:
        problems.append("report.md exceeds the %d MB limit" % (MAX_REPORT_BYTES // (1024 * 1024)))
    elif _has_nul(report_md):
        problems.append("report.md contains binary (NUL byte) content — refused")

    overview_md = _read_text(os.path.join(session_dir, "overview.md"))
    if overview_md and len(overview_md.encode("utf-8")) > MAX_REPORT_BYTES:
        problems.append("overview.md exceeds the 2 MB limit")
    if overview_md is not None and _has_nul(overview_md):
        problems.append("overview.md contains binary (NUL byte) content — refused")

    if problems:
        return None, problems

    meta = _load_meta(session_dir)
    provenance: Dict[str, Any] = {
        "kind": "deep-research",
        "skill_ver": _skill_ver(),
        "taxonomy": taxonomy or meta.get("taxonomy") or "",
        "worklist_fingerprint": _worklist_fingerprint(worklist_path),
    }
    # provenance.model is required server-side (min_length=1) and by
    # SKILL_SPEC rule 3; "unknown" is the honest default when no model env
    # var (PD_MODEL / ANTHROPIC_MODEL / CLAUDE_MODEL) is set.
    provenance["model"] = _model_from_env() or "unknown"

    payload: Dict[str, Any] = {
        "title": (title or _default_title(session_dir, report_md, meta)).strip(),
        "provenance": provenance,
        "papers": papers,
        "claims": claims,
        "report_md": report_md,
    }
    if not payload["title"] or len(payload["title"]) > 200 or _has_nul(payload["title"]):
        return None, ["session title must be 1..200 characters without NUL"]
    if overview_md and overview_md.strip():
        payload["overview_md"] = overview_md
    return payload, []


# ─────────────────────────── HTTP ───────────────────────────
def post_session(
    pd_base: str, pd_key: str, payload: dict, timeout: int, revision_of: Optional[str] = None
) -> Tuple[Optional[int], str]:
    """Idempotent create/revision POST; no redirects or fallback to creation."""
    try:
        client = Client(Settings(pd_base.rstrip("/"), pd_key, timeout=timeout))
        path = "/me/reading-sessions"
        if revision_of:
            path += "/" + path_segment(revision_of) + "/revisions"
        result = client.request("POST", path, payload, idempotent=True)
        return client.calls[-1]["status"], json.dumps(result, ensure_ascii=False)
    except ApiError as exc:
        return exc.status, str(exc)


def artifact_content(payload: dict) -> dict:
    """Same content normalization used by the revision API."""
    return {
        "title": payload["title"],
        "papers": sorted(payload["papers"], key=lambda p: p["paper_id"]),
        "claims": payload["claims"],
        "report_md": payload.get("report_md"),
        "overview_md": payload.get("overview_md"),
    }


def verify_duplicate(pd_base: str, pd_key: str, sid: str, payload: dict, timeout: int) -> bool:
    """A duplicate create response is not an acknowledgement of edited content."""
    remote = Client(Settings(pd_base.rstrip("/"), pd_key, timeout=timeout)).get(
        "/me/reading-sessions/" + path_segment(sid)
    )
    candidate = {
        "title": remote.get("title"),
        "papers": [
            {"paper_id": n["paper_id"], "depth": n["depth"], "note_md": n["body_md"]}
            for n in remote.get("notes", [])
        ],
        "claims": [
            {
                k: (c.get(k) or "") if k == "note" else c.get(k)
                for k in ("claim", "status", "evidence", "note")
            }
            for c in remote.get("claims", [])
        ],
        "report_md": remote.get("report_md"),
        "overview_md": remote.get("overview_md"),
    }
    return content_hash(artifact_content(candidate)) == content_hash(artifact_content(payload))


# ─────────────────────────── CLI ───────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="upload_session.py",
        description="Upload one deep-research session's analysis (never PDFs) to the paperdaily workbench.",
    )
    ap.add_argument("--dir", required=True, help="session artifact tree, e.g. pd-research/<slug>/")
    ap.add_argument("--title", help="session title (default: report.md H1 / taxon name / dir name)")
    ap.add_argument("--taxonomy", help="provenance.taxonomy override (default: worklist.meta.json)")
    ap.add_argument(
        "--task-id",
        help="inbox task id claimed in Stage 0 — a successful upload closes it (AGENT_PROTOCOL §5)",
    )
    ap.add_argument("--revise", help="create an immutable revision of this owned session id")
    ap.add_argument(
        "--dry-run", action="store_true", help="gate + assemble + summarise, do not POST"
    )
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="request timeout seconds")
    args = ap.parse_args(argv)

    session_dir = args.dir.rstrip("/")
    if not os.path.isdir(session_dir):
        sys.stderr.write("error: not a directory: %s\n" % session_dir)
        return 1

    payload, problems = build_payload(session_dir, args.title, args.taxonomy)
    if payload is None:
        sys.stderr.write("phase gate FAILED — nothing uploaded. Fix these first:\n")
        for p in problems:
            sys.stderr.write("  - %s\n" % p)
        return 2

    # Stage-0 inbox task link (AGENT_PROTOCOL §5): the server closes the
    # task and back-links the session when the upload succeeds.
    task_id = (args.task_id or "").strip()
    if task_id:
        payload["task_id"] = task_id

    n_full = sum(1 for p in payload["papers"] if p["depth"] == "fulltext")
    n_abs = len(payload["papers"]) - n_full
    by_status: Dict[str, int] = {}
    for c in payload["claims"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    sys.stderr.write(
        "phase gate OK: %d papers (%d fulltext / %d abstract-only), %d claims (%s), report %.1f KB\n"
        % (
            len(payload["papers"]),
            n_full,
            n_abs,
            len(payload["claims"]),
            ", ".join("%s=%d" % kv for kv in sorted(by_status.items())) or "none",
            len(payload["report_md"].encode("utf-8")) / 1024.0,
        )
    )
    sys.stderr.write("worklist_fingerprint: %s\n" % payload["provenance"]["worklist_fingerprint"])

    if args.dry_run:
        sys.stderr.write(
            "dry-run: payload is %.1f KB, not posting\n"
            % (len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) / 1024.0)
        )
        return 0

    try:
        settings = load_settings()
        pd_base, pd_key = settings.base, settings.key
    except (ApiError, ValueError, OSError) as exc:
        sys.stderr.write("configuration error: %s\n" % exc)
        return 1

    artifact_hash = content_hash(artifact_content(payload))
    receipt_path = Path(session_dir) / "upload_receipt.json"
    if receipt_path.exists():
        try:
            previous = json.loads(receipt_path.read_text())
        except (OSError, ValueError):
            sys.stderr.write(
                "invalid upload receipt; preserve it and investigate before uploading\n"
            )
            return 3
        if not isinstance(previous, dict) or not previous.get("session_id"):
            sys.stderr.write("invalid upload receipt; preserve it and investigate\n")
            return 3
        if (
            previous.get("base") == pd_base.rstrip("/")
            and previous.get("artifact_hash") != artifact_hash
            and not args.revise
        ):
            sys.stderr.write(
                "Research content changed. The existing session is unchanged. Use --revise %s to save an immutable revision.\n"
                % previous.get("session_id", "<id>")
            )
            return 3
    status, body = post_session(pd_base, pd_key, payload, args.timeout, revision_of=args.revise)
    web = _web_base(pd_base)

    if status in (200, 201):
        try:
            resp = json.loads(body)
        except json.JSONDecodeError:
            resp = {}
        sid = (resp.get("id") or resp.get("session_id") or "") if isinstance(resp, dict) else ""
        if not isinstance(resp, dict) or not sid:
            sys.stderr.write("upload response missing session id; outcome unverified\n")
            return 3
        if status == 200 and resp.get("deduplicated"):
            try:
                matches = verify_duplicate(pd_base, pd_key, sid, payload, args.timeout)
            except (ApiError, KeyError, ValueError, TypeError):
                sys.stderr.write(
                    "Existing session returned, but content could not be verified (read:reading required). No update is confirmed.\n"
                )
                return 3
            if not matches:
                sys.stderr.write(
                    "Existing session differs from local content; nothing updated. Use --revise %s.\n"
                    % sid
                )
                return 3
            sys.stderr.write(
                "already uploaded (deduplicated) — this worklist_fingerprint maps to session %s\n"
                % (sid or "?")
            )
        else:
            sys.stderr.write("uploaded — session %s\n" % (sid or "?"))
        write_json(
            receipt_path,
            {
                "base": pd_base.rstrip("/"),
                "session_id": sid,
                "artifact_hash": artifact_hash,
                "revision_of": args.revise,
                "worklist_fingerprint": payload["provenance"]["worklist_fingerprint"],
                "created_at": utc_now(),
            },
        )
        if sid:
            sys.stderr.write("view it at: %s/workbench?tab=reading&s=%s\n" % (web, sid))
        # The server tells us which paper_ids it could not find in the graph
        # ("surfaced so the caller can flag them"). Swallowing that would break
        # the skill's own honest-ledger rule: a paper_id that drifted between
        # worklist.jsonl and notes/ shows up here and nowhere else, and every
        # claim citing it is then pointing at nothing.
        unknown = resp.get("unknown_paper_ids") or []
        if unknown:
            sys.stderr.write(
                "\n警告：服务端在图谱里找不到这 %d 个 paper_id（笔记与 claims 照常入库，但引用悬空）：\n"
                % len(unknown)
            )
            for pid in unknown[:20]:
                sys.stderr.write("  - %s\n" % pid)
            if len(unknown) > 20:
                sys.stderr.write("  … 另有 %d 个\n" % (len(unknown) - 20))
            sys.stderr.write(
                "常见原因：笔记里的 **paper_id** 与 worklist.jsonl 的 id 写法不一致\n"
                "（如把安全化文件名 arxiv_2409.10897 当成了原始 id arxiv:2409.10897），\n"
                "或论文确实还没进语料。修改内容后需 --revise；重复首传不会更新既有 session。\n"
            )
        if task_id:
            task_linked = resp.get("task_linked")
            if task_linked is True:
                sys.stderr.write("已完结收件箱任务 %s\n" % task_id)
            elif task_linked is False:
                sys.stderr.write(
                    "警告：收件箱任务 %s 未完结（任务不存在/不属于你/已完结）——session 已照常入库\n"
                    % task_id
                )
            else:
                sys.stderr.write(
                    "警告：响应未带 task_linked 字段（服务端可能尚未部署 A1 agent-tasks）——收件箱任务 %s 状态未知\n"
                    % task_id
                )
        return 0
    if args.revise and status in (404, 405):
        sys.stderr.write(
            "Revision not saved: endpoint unavailable or parent absent. Local files retained; no fallback create was attempted.\n"
        )
        return 3
    if status == 403:
        sys.stderr.write(
            "HTTP 403 — your key lacks the write:reading scope.\n"
            "Fix: %s/account → API Keys → issue (or re-issue) a key with write:reading checked,\n"
            "then update PD_KEY in ~/.paperdaily-cli/env and re-run.\n" % web
        )
        return 3
    if status == 401:
        sys.stderr.write(
            "HTTP 401 — key missing/invalid/revoked. Check PD_KEY in ~/.paperdaily-cli/env.\n"
        )
        return 3
    if status == 429:
        sys.stderr.write(
            "HTTP 429 — %s\n"
            "若提示 'reading-session limit reached'，这是每人 100 个 session 的上限，不是限流：\n"
            "去 %s/workbench?tab=reading 删掉不再需要的会话后重传（重试无用）。\n"
            % (body[:500] if body else "(empty body)", web)
        )
        return 3
    if status is None:
        sys.stderr.write(
            "upload failed after %d attempts: %s\n" % (MAX_POST_ATTEMPTS, body or "network error")
        )
        return 3
    sys.stderr.write("HTTP %d — %s\n" % (status, body[:2000] if body else "(empty body)"))
    return 3


if __name__ == "__main__":
    sys.exit(main())
