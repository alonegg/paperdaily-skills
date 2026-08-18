#!/usr/bin/env python3
"""
upload_session.py — paperdaily deep-research Stage 4: upload the local
analysis artifacts of one research session to the paperdaily workbench.

Reads a `pd-research/<slug>/` artifact tree (produced by Stages 1-3), runs a
local phase-gate self-check, assembles a reading-session payload, and POSTs it
to `POST /api/v1/me/reading-sessions` (scope `write:reading`).

────────────────────────────────────────────────────────────────────────────
CONSENT / SCOPE
────────────────────────────────────────────────────────────────────────────
Running this script IS the upload action. Per SKILL.md Stage 4 and
`skills/SKILL_SPEC.md` rule 4, it must only be run after the user has
explicitly agreed — once, for this session — to send their analysis to
paperdaily. It uploads ONLY user-authored derived analysis:

  worklist.jsonl      → fingerprint only (sha256, for idempotency)
  notes/<id>.md       → papers[] (paper_id + depth + note_md)
  synthesis/claims.jsonl → claims[] (four-state evidence ledger)
  report.md / overview.md → inlined markdown

It NEVER reads or uploads `pdfs/` or any binary content — fetched PDFs stay
local forever (copyright red line, AGENT_PROTOCOL §7). Payload strings are
additionally screened for NUL bytes before sending.

────────────────────────────────────────────────────────────────────────────
PHASE GATE (checked locally; failure prints the defect list and exits 2
without uploading)
────────────────────────────────────────────────────────────────────────────
  • worklist.jsonl exists (needed for the idempotency fingerprint)
  • notes/ has ≥1 note, every note is non-empty and carries a
    `**paper_id**` field (reading-note-template; no filename fallback)
  • synthesis/claims.jsonl: every line is valid JSON with
    status ∈ {supported, weak, contested, gap}, every evidence entry has
    non-empty paper_id / location / quote, and the share of claims with
    (complete) evidence is ≥ 80%
  • report.md exists and is non-empty
  • contract limits: papers ≤ 100, note ≤ 64 KB, claims ≤ 200,
    report ≤ 2 MB, each evidence quote ≤ 500 chars

────────────────────────────────────────────────────────────────────────────
CLI
────────────────────────────────────────────────────────────────────────────
  python3 upload_session.py --dir pd-research/<slug>/ [--title T]
                            [--taxonomy STR] [--task-id ID]
                            [--dry-run] [--timeout 60]

  --dir        the session artifact tree (required)
  --title      session title (default: report.md first `# ` heading,
               else worklist.meta.json taxon name, else the dir name)
  --taxonomy   provenance.taxonomy override (default: worklist.meta.json)
  --task-id    inbox task id claimed in Stage 0 (AGENT_PROTOCOL §5) — the
               payload carries it and a successful upload closes the task
               (response `task_linked: true`); `false` means the task does
               not exist / is not yours / is already closed (the session
               itself is still stored)
  --dry-run    run the gate + assemble the payload, print a summary, do NOT post

Auth: PD_BASE / PD_KEY from the environment, else from ~/.paperdaily-cli/env.
Depth per paper: an explicit "abstract-only" marker in the note header wins;
otherwise the fetch ledgers decide — a record whose `carrier` is one of
binary-pdf / html-fulltext / parsed-fulltext (or, for pre-0.5.0 ledgers, whose
status is ok/already) ⇒ `fulltext`, anything else ⇒ `abstract-only`
(honest-ledger downgrade, never upgrade). Ledgers read, and merged, in this
order: fetch_report.jsonl, browser_fetch_report.jsonl, fulltext_report.jsonl —
so a paper landed by the browser layer, or read as page-ordered Markdown parsed
from a public PDF, counts without anyone assembling a side ledger by hand.

Standard library only. Python 3.9+.
Exit codes: 0 uploaded (or deduplicated / dry-run) · 1 usage/arg error ·
2 phase-gate failure · 3 upload/HTTP failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────── constants (server contract, 0.8.0) ───────────────────────────
MAX_PAPERS = 100
MAX_NOTE_BYTES = 64 * 1024
MAX_CLAIMS = 200
MAX_REPORT_BYTES = 2 * 1024 * 1024
MAX_QUOTE_CHARS = 500
CLAIM_STATUSES = ("supported", "weak", "contested", "gap")
MIN_EVIDENCE_RATE = 0.80

SKILL_VER_FALLBACK = "0.4.2"
DEFAULT_TIMEOUT = 60
MAX_POST_ATTEMPTS = 3  # AGENT_PROTOCOL §6: backoff-retry cap

_ABSTRACT_ONLY_RE = re.compile(r"abstract[-_ ]only", re.IGNORECASE)
_PAPER_ID_FIELD_RE = re.compile(r"\*\*paper_id\*\*\s*[:：]\s*`?([^`\n]+?)`?\s*$", re.MULTILINE)
_SKILL_VER_RE = re.compile(r"skill_ver[^0-9]*([0-9]+\.[0-9]+\.[0-9]+)")


# ─────────────────────────── small helpers ───────────────────────────
def _load_cli_env() -> None:
    """Populate PD_BASE / PD_KEY from ~/.paperdaily-cli/env if not already set.

    Parses simple `export KEY="value"` / `KEY=value` lines; the process
    environment always wins over the file.
    """
    path = os.path.expanduser("~/.paperdaily-cli/env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key in ("PD_BASE", "PD_KEY") and key not in os.environ and val:
                    os.environ[key] = val
    except OSError:
        pass


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
_LEDGER_FILES = ("fetch_report.jsonl", "browser_fetch_report.jsonl",
                 "fulltext_report.jsonl")


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
        body = _read_text(fpath)
        if body is None or not body.strip():
            problems.append("notes/%s is empty" % fname)
            continue
        if _has_nul(body):
            problems.append("notes/%s contains binary (NUL byte) content — refused" % fname)
            continue
        if len(body.encode("utf-8")) > MAX_NOTE_BYTES:
            problems.append("notes/%s exceeds the %d KB note limit" % (fname, MAX_NOTE_BYTES // 1024))
            continue
        stem = fname[: -len(".md")]
        # raw paper_id: the note body's **paper_id** field is mandatory —
        # the sanitised filename is lossy (`/` and `:` become `_`), so a
        # silent filename fallback would upload corrupted ids. Gate instead.
        m = _PAPER_ID_FIELD_RE.search(body)
        paper_id = m.group(1).strip() if m else ""
        if not paper_id:
            problems.append("notes/%s is missing the **paper_id** field — add a `**paper_id**: <raw id>` line per the reading-note-template" % fname)
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
    n_with_evidence = 0
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
            problems.append("claims.jsonl line %d: status %r not in %s"
                            % (lineno, status, "/".join(CLAIM_STATUSES)))
            continue
        evidence_in = rec.get("evidence") or []
        evidence: List[dict] = []
        for i, ev in enumerate(evidence_in):
            if not isinstance(ev, dict):
                problems.append("claims.jsonl line %d evidence %d: not a JSON object" % (lineno, i + 1))
                continue
            quote = str(ev.get("quote") or ev.get("quote_or_paraphrase") or "").strip()
            if len(quote) > MAX_QUOTE_CHARS:
                problems.append("claims.jsonl line %d evidence %d: quote is %d chars (limit %d) — shorten it, do not upload long verbatim excerpts"
                                % (lineno, i + 1, len(quote), MAX_QUOTE_CHARS))
                continue
            paper_id = str(ev.get("paper_id") or "").strip()
            location = str(ev.get("location") or "").strip()
            empty_fields = [name for name, val in
                            (("paper_id", paper_id), ("location", location), ("quote", quote))
                            if not val]
            if empty_fields:
                problems.append("claims.jsonl line %d evidence %d: empty %s — every evidence entry needs non-empty paper_id / location / quote"
                                % (lineno, i + 1, ", ".join(empty_fields)))
                continue
            evidence.append({
                "paper_id": paper_id,
                "location": location,
                "quote": quote,
            })
        # the 80% evidence rate counts only complete (paper_id+location+quote)
        # entries — a claim whose evidence all failed validation counts as bare.
        if evidence:
            n_with_evidence += 1
        claims.append({
            "claim": claim,
            "status": status,
            "evidence": evidence,
            "note": str(rec.get("note") or ""),
        })
    if not claims:
        problems.append("claims.jsonl has no valid claim rows")
    else:
        rate = n_with_evidence / len(claims)
        if rate < MIN_EVIDENCE_RATE:
            problems.append("evidence non-empty rate %.0f%% is below the %.0f%% gate (%d/%d claims have evidence)"
                            % (rate * 100, MIN_EVIDENCE_RATE * 100, n_with_evidence, len(claims)))
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
def build_payload(session_dir: str, title: Optional[str], taxonomy: Optional[str]) -> Tuple[Optional[dict], List[str]]:
    """Run the phase gate and assemble the payload. Returns (payload, problems);
    payload is None when the gate fails."""
    problems: List[str] = []

    worklist_path = os.path.join(session_dir, "worklist.jsonl")
    if not os.path.isfile(worklist_path):
        problems.append("worklist.jsonl missing (needed for the idempotency fingerprint)")

    papers = _collect_papers(session_dir, problems)
    claims = _collect_claims(session_dir, problems)

    report_md = _read_text(os.path.join(session_dir, "report.md"))
    if report_md is None or not report_md.strip():
        problems.append("report.md missing or empty")
        report_md = None
    elif len(report_md.encode("utf-8")) > MAX_REPORT_BYTES:
        problems.append("report.md exceeds the %d MB limit" % (MAX_REPORT_BYTES // (1024 * 1024)))
    elif _has_nul(report_md):
        problems.append("report.md contains binary (NUL byte) content — refused")

    overview_md = _read_text(os.path.join(session_dir, "overview.md"))
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
        "title": title or _default_title(session_dir, report_md, meta),
        "provenance": provenance,
        "papers": papers,
        "claims": claims,
        "report_md": report_md,
    }
    if overview_md and overview_md.strip():
        payload["overview_md"] = overview_md
    return payload, []


# ─────────────────────────── HTTP ───────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect while holding the bearer key.

    Two reasons (2026-07-26 audit): stdlib urllib replays custom headers —
    including `Authorization` — across origins, and it rewrites a redirected
    POST into a GET *without* the body. The second one is the nastier of the
    pair: it would return 200 from the redirect target while nothing was
    uploaded. A redirect here is a misconfigured PD_BASE, so surface it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _check_pd_base(pd_base: str) -> Optional[str]:
    """Refuse to send the bearer key over plaintext to a public host.
    http:// stays legal for loopback / RFC1918 (the documented internal
    default of a self-hosted instance is a private address); everything
    else needs TLS."""
    parts = urllib.parse.urlsplit(pd_base)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if scheme == "https":
        return None
    if scheme != "http":
        return "PD_BASE must be an http(s) URL (got %r)" % (scheme or pd_base,)
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return None
    try:
        import ipaddress

        if ipaddress.ip_address(host).is_private:
            return None
    except Exception:
        pass
    return (
        "PD_BASE uses plaintext http:// on a public host (%s) — the API key would "
        "travel in the clear. Use https:// (or a private/loopback address for a "
        "self-hosted instance)." % (host or "?")
    )


def post_session(pd_base: str, pd_key: str, payload: dict, timeout: int) -> Tuple[Optional[int], str]:
    """POST with up to MAX_POST_ATTEMPTS attempts on 429/5xx (honours Retry-After).
    Returns (status, body_text); status None = network failure."""
    bad_base = _check_pd_base(pd_base)
    if bad_base:
        return None, bad_base
    url = pd_base.rstrip("/") + "/me/reading-sessions"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer %s" % pd_key,
        "Content-Type": "application/json",
        "User-Agent": "paperdaily-deep-research/upload_session %s" % _skill_ver(),
    }
    opener = urllib.request.build_opener(_NoRedirect())
    last_err = ""
    for attempt in range(1, MAX_POST_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
                return status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if 300 <= e.code < 400:  # _NoRedirect refused to follow
                loc = e.headers.get("Location") if e.headers else None
                return e.code, (
                    "server redirected the upload (Location: %s) — refused, because "
                    "following it would leak the bearer key across origins and turn "
                    "the POST into a bodyless GET. Point PD_BASE at the final URL."
                    % (loc or "?")
                )
            if e.code == 429 and not (e.headers or {}).get("X-RateLimit-Limit"):
                # Two different 429s live on this endpoint. The rate limiter
                # always attaches X-RateLimit-* headers; the per-user session
                # cap ("reading-session limit reached (100)") does not. Backing
                # off and retrying a cap is pure waste and reads to the user as
                # a flaky network, so return it straight away.
                return e.code, body
            if e.code == 429 or e.code >= 500:
                if attempt < MAX_POST_ATTEMPTS:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = int(retry_after) if retry_after else 5 * (2 ** (attempt - 1))
                    except ValueError:
                        wait = 5 * (2 ** (attempt - 1))
                    sys.stderr.write("HTTP %d — backing off %ds (attempt %d/%d)\n"
                                     % (e.code, wait, attempt, MAX_POST_ATTEMPTS))
                    time.sleep(wait)
                    continue
            return e.code, body
        except Exception as e:  # URLError, timeout, ...
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < MAX_POST_ATTEMPTS:
                wait = 5 * (2 ** (attempt - 1))
                sys.stderr.write("network error (%s) — backing off %ds (attempt %d/%d)\n"
                                 % (last_err[:120], wait, attempt, MAX_POST_ATTEMPTS))
                time.sleep(wait)
                continue
    return None, last_err


# ─────────────────────────── CLI ───────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="upload_session.py",
        description="Upload one deep-research session's analysis (never PDFs) to the paperdaily workbench.")
    ap.add_argument("--dir", required=True, help="session artifact tree, e.g. pd-research/<slug>/")
    ap.add_argument("--title", help="session title (default: report.md H1 / taxon name / dir name)")
    ap.add_argument("--taxonomy", help="provenance.taxonomy override (default: worklist.meta.json)")
    ap.add_argument("--task-id", help="inbox task id claimed in Stage 0 — a successful upload closes it (AGENT_PROTOCOL §5)")
    ap.add_argument("--dry-run", action="store_true", help="gate + assemble + summarise, do not POST")
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
    sys.stderr.write("phase gate OK: %d papers (%d fulltext / %d abstract-only), %d claims (%s), report %.1f KB\n" % (
        len(payload["papers"]), n_full, n_abs, len(payload["claims"]),
        ", ".join("%s=%d" % kv for kv in sorted(by_status.items())) or "none",
        len(payload["report_md"].encode("utf-8")) / 1024.0))
    sys.stderr.write("worklist_fingerprint: %s\n" % payload["provenance"]["worklist_fingerprint"])

    if args.dry_run:
        sys.stderr.write("dry-run: payload is %.1f KB, not posting\n"
                         % (len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) / 1024.0))
        return 0

    _load_cli_env()
    pd_base = os.environ.get("PD_BASE")
    pd_key = os.environ.get("PD_KEY")
    if not pd_base or not pd_key:
        sys.stderr.write(
            "error: missing PD_BASE / PD_KEY.\n"
            "Create ~/.paperdaily-cli/env with:\n"
            "  export PD_BASE=\"https://www.paperdaily.org/api/v1\"\n"
            "  export PD_KEY=\"pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\"\n"
            "No key yet? Issue one from the web UI: /account → API Keys (needs write:reading).\n")
        return 1

    status, body = post_session(pd_base, pd_key, payload, args.timeout)
    web = _web_base(pd_base)

    if status in (200, 201):
        try:
            resp = json.loads(body)
        except json.JSONDecodeError:
            resp = {}
        sid = resp.get("id") or resp.get("session_id") or ""
        if status == 200 and resp.get("deduplicated"):
            sys.stderr.write("already uploaded (deduplicated) — this worklist_fingerprint maps to session %s\n" % (sid or "?"))
        else:
            sys.stderr.write("uploaded — session %s\n" % (sid or "?"))
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
                % len(unknown))
            for pid in unknown[:20]:
                sys.stderr.write("  - %s\n" % pid)
            if len(unknown) > 20:
                sys.stderr.write("  … 另有 %d 个\n" % (len(unknown) - 20))
            sys.stderr.write(
                "常见原因：笔记里的 **paper_id** 与 worklist.jsonl 的 id 写法不一致\n"
                "（如把安全化文件名 arxiv_2409.10897 当成了原始 id arxiv:2409.10897），\n"
                "或论文确实还没进语料。核对后可修正笔记重传——幂等不会产生重复 session。\n")
        if task_id:
            task_linked = resp.get("task_linked")
            if task_linked is True:
                sys.stderr.write("已完结收件箱任务 %s\n" % task_id)
            elif task_linked is False:
                sys.stderr.write("警告：收件箱任务 %s 未完结（任务不存在/不属于你/已完结）——session 已照常入库\n" % task_id)
            else:
                sys.stderr.write("警告：响应未带 task_linked 字段（服务端可能尚未部署 A1 agent-tasks）——收件箱任务 %s 状态未知\n" % task_id)
        return 0
    if status == 403:
        sys.stderr.write(
            "HTTP 403 — your key lacks the write:reading scope.\n"
            "Fix: %s/account → API Keys → issue (or re-issue) a key with write:reading checked,\n"
            "then update PD_KEY in ~/.paperdaily-cli/env and re-run.\n" % web)
        return 3
    if status == 401:
        sys.stderr.write("HTTP 401 — key missing/invalid/revoked. Check PD_KEY in ~/.paperdaily-cli/env.\n")
        return 3
    if status == 429:
        sys.stderr.write(
            "HTTP 429 — %s\n"
            "若提示 'reading-session limit reached'，这是每人 100 个 session 的上限，不是限流：\n"
            "去 %s/workbench?tab=reading 删掉不再需要的会话后重传（重试无用）。\n"
            % (body[:500] if body else "(empty body)", web))
        return 3
    if status is None:
        sys.stderr.write("upload failed after %d attempts: %s\n" % (MAX_POST_ATTEMPTS, body or "network error"))
        return 3
    sys.stderr.write("HTTP %d — %s\n" % (status, body[:2000] if body else "(empty body)"))
    return 3


if __name__ == "__main__":
    sys.exit(main())
