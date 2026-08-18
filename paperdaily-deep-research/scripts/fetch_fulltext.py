#!/usr/bin/env python3
"""
fetch_fulltext.py — paperdaily deep-research full-text PDF fetcher.

Given a `worklist.jsonl` (one paper per line, produced by an upstream stage) this
script downloads each paper's full-text PDF into an output directory, trying a
layered waterfall of *open* and *user-authorised* sources and stopping at the
first hit.

────────────────────────────────────────────────────────────────────────────
COMPLIANCE / SCOPE
────────────────────────────────────────────────────────────────────────────
This tool only retrieves PDFs through:
  • Open-access / green-OA channels (arXiv, Unpaywall, Europe PMC, publisher
    open-access links, `citation_pdf_url` landing-page metadata), and
  • The user's OWN licensed access — Text-and-Data-Mining (TDM) API keys the
    user holds (L4), or the user's own institutional subscription network
    (L5), which is OFF by default and must be explicitly opted into.

It contains NO paywall-circumvention: no Sci-Hub / LibGen / shadow mirrors, no
credential sharing, no cookie/session theft, no CAPTCHA solving. The optional
institutional layer (L5) is lawful direct access that only makes sense when the
machine running this script is inside a network your institution has licensed
(e.g. campus-IP authentication). You are responsible for confirming you are
entitled to the content you fetch; enable L5 only if you are on such an
authorised network. The headless-browser layer (L6) is a separate opt-in that
renders a landing page whose PDF link only appears after JS — common on OA
repositories — and is not tied to any subscription. When a source returns a
paywall page instead of a PDF, the layer is recorded as `denied` and the script
moves on — it never tries to defeat the wall. A layer that simply does not hold
the paper (404, or a lookup API with no record) is recorded as `miss` instead:
conflating the two made every absent paper look like a permission failure.

Standard library only. `playwright` is the single optional dependency (L6) and
is imported behind a try/except guard, so a machine without it never crashes.
Python 3.9+.

────────────────────────────────────────────────────────────────────────────
CLI
────────────────────────────────────────────────────────────────────────────
  python3 fetch_fulltext.py --worklist worklist.jsonl --out pdfs/ [--report r.jsonl]
  python3 fetch_fulltext.py --doi 10.1038/xxx --out pdfs/          # single paper
  python3 fetch_fulltext.py --arxiv 2501.01234 --out pdfs/

  --report        default: <out>/../fetch_report.jsonl
  --email / env UNPAYWALL_EMAIL   contact email for the polite-pool APIs that
                  require one. Sent ONLY to api.unpaywall.org / api.crossref.org /
                  NCBI-PMC / EBI — never in the User-Agent seen by publishers,
                  CDNs or landing pages, and masked out of fetch_report.jsonl.
  --timeout       per-request timeout, seconds (default 30)
  --throttle      min seconds between requests to the SAME host (default 1.0)
  --title-search  api (default) | off — see L4b below

Env toggles:
  PD_FETCH_INSTITUTIONAL=1   enable L5 (only if on an authorised network)
  PD_FETCH_BROWSER=1         enable L6 headless-browser fallback (needs playwright;
                             independent of L5 since 0.4.0)
  ELSEVIER_TDM_KEY           Elsevier TDM API key (L4)
  WILEY_TDM_TOKEN            Wiley TDM client token (L4)
  S2_API_KEY                 Semantic Scholar key (L4b sub-layer; keyless S2 429s)

────────────────────────────────────────────────────────────────────────────
WHY THERE IS A TITLE LAYER (L4b, 1.1)
────────────────────────────────────────────────────────────────────────────
Every layer above L4b is keyed by DOI or arXiv id. For a closed-access
economics / finance / law article that is a dead end by construction: the DOI
is registered only against the paywalled version of record, so Unpaywall, PMC
and the article's own OpenAlex `best_oa_location` all answer `miss` — correctly.
Measured on the 8 failed papers of the 2026-08-02 hhag020 run: 5 had no OA
location at all, 2 pointed at an SSRN landing page, 1 at a repository landing
page. Configuring UNPAYWALL_EMAIL would have rescued at most one of the eight.

The openly readable copy nonetheless existed for most of them — as a *separate
work*: the SSRN / NBER / conference / author-homepage working paper, with its
own id, its own (different) title and its own OA location. Title is the only
key that reaches it, which is what L4b searches on, guarded by author-surname
overlap rather than by a tight similarity threshold (see TITLE_SIM_WITH_AUTHOR).

Anything L4b or L1b returns is therefore a DIFFERENT manuscript version from
the one requested. The ledger stamps `version: "alternate"` plus the matched
title and id, and the reading stage must record which version it read — page
numbers, sample sizes and table numbers do not carry across versions.

Scholarly aggregators do not index every such copy (the general web does — a
plain title search finds copies OpenAlex/OpenAIRE/S2 miss), but no script can
run a general web search unattended: DDG/Bing soft-block scripted clients and
keyless S2 429s on the first request. So when every layer is exhausted the run
writes `needs_web_search.jsonl` naming the papers and a suggested query; the
agent driving the script runs those searches, appends the URLs it finds to the
worklist row as `"urls_extra": [...]`, and re-runs (fetched papers are skipped).

Ledger outcomes per attempt (references/fulltext-sources.md):
  ok · already · skipped_no_config · skipped_optin · miss · blocked · denied · error
`miss` (0.4.2) is "this layer does not have the paper" — a 404, or a lookup API
that answered fine but lists nothing. It used to be recorded as `denied`, which
promises "asked and refused, do not retry" and made triage read the ledger wrong.
`blocked` (1.2) is the same mistake one level down: a bot challenge is not an
access decision. Measured 2026-08-18 from a campus network that *does* subscribe
to OUP, Wiley, Taylor & Francis and Elsevier — all four answer their article
pages with a Cloudflare 403 challenge. Filing those as `denied` marks subscribed
journals unavailable, systematically and invisibly.

Per-record fields beyond status/layer/url (1.1): `carrier` (binary-pdf here;
the reading stage may add html-fulltext / parsed-fulltext), `sha256`, `version`,
and on failure `needs_web_search`.

Exit codes: 0 all fetched/already · 2 one or more failed · 1 usage/arg error.
"""

from __future__ import annotations

import argparse
import hashlib
import html as html_mod
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ─────────────────────────── constants ───────────────────────────
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # engineering rule #3: 50 MB cap
PDF_MAGIC = b"%PDF"
DEFAULT_TIMEOUT = 30
DEFAULT_THROTTLE = 1.0
VERSION = "1.2"

# Title-search guard (L4b). A working-paper copy legitimately carries a
# DIFFERENT title from the version of record — "Do Mutual Funds Walk the Talk?
# A Textual Analysis of Risk Disclosure by Mutual Funds" (WP) vs "… Evidence
# from Fund Risk Disclosure" (RFS). Token Jaccard between those two is 0.6, so
# a strict threshold would reject exactly the copy we are looking for, while a
# loose one would happily fetch a *different* paper on the same topic. The
# resolution is not a cleverer threshold: it is the author check. Shared author
# surnames + a moderately similar title is a version of the same work; a
# similar title with no shared author is someone else's paper.
TITLE_SIM_WITH_AUTHOR = 0.45   # accept if ≥ this AND ≥1 author surname matches
TITLE_SIM_NO_AUTHOR = 0.75     # accept on title alone only if near-identical
MAX_TITLE_CANDIDATES = 4       # bound the fan-out; this is a layer, not a crawl
_TITLE_STOPWORDS = frozenset("""
a an the of on in for from to and or by with without via using at as is are be
do does did how what why when evidence new towards toward into over under
""".split())

# TDM keys are publisher-specific; only attempt (and only report skipped_no_config)
# for DOIs whose prefix belongs to that publisher.
ELSEVIER_PREFIXES = {"10.1016"}
WILEY_PREFIXES = {"10.1002", "10.1111"}

_SSL_CTX = ssl.create_default_context()

_CHALLENGE_MARKERS = (
    b"just a moment",
    b"cf-browser-verification",
    b"checking your browser",
    b"enable javascript and cookies",
    b"cf-challenge",
    b"captcha-delivery",
    b"px-captcha",
    b"access denied",
)

_META_TAG_RE = re.compile(rb"<meta\b[^>]*>", re.IGNORECASE)
_META_NAME_RE = re.compile(rb"""(?:name|property)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_META_CONTENT_RE = re.compile(rb"""content\s*=\s*["']([^"']*)["']""", re.IGNORECASE)

# ── credential / privacy hygiene (2026-07-26 audit) ──────────────────
# URLs reach this script from three places we do not control: the server's
# worklist, publisher landing pages (`citation_pdf_url`), and API responses.
# Only ever speak http(s) — stdlib urlopen would happily read `file:///…`
# on a first hop (its redirect handler blocks non-http schemes, the opener
# does not) and hand local file bytes to the downstream reading stages.
_ALLOWED_SCHEMES = ("http", "https")

# Headers that authenticate US and must never cross an origin boundary.
# stdlib `urllib` — unlike requests/urllib3 — replays every custom header
# verbatim on a cross-host 30x, so this has to be enforced by hand.
_CRED_HEADER_NAMES = frozenset(
    ("authorization", "x-els-apikey", "wiley-tdm-client-token", "x-api-key")
)

# Hosts whose API contract asks for a contact email (polite pools). The
# user's address goes to THESE and nowhere else — never into the global
# User-Agent, which would broadcast it to every publisher/CDN in the
# waterfall.
_POLITE_HOSTS = (
    "api.unpaywall.org",
    "api.crossref.org",
    "api.openalex.org",
    "pmc.ncbi.nlm.nih.gov",
    "www.ncbi.nlm.nih.gov",
    "eutils.ncbi.nlm.nih.gov",
    "www.ebi.ac.uk",
)

# Query params that carry contact/credential material and must be masked
# before a URL is written to the on-disk ledger.
_SENSITIVE_PARAMS = frozenset(
    ("email", "mailto", "api_key", "apikey", "key", "token", "access_token")
)


def _origin(url: str) -> Tuple[str, str, Optional[int]]:
    p = urllib.parse.urlsplit(url)
    return (p.scheme.lower(), p.hostname or "", p.port)


def _netloc(scheme: str, host: str, port: Optional[int]) -> str:
    return "%s://%s%s" % (scheme, host or "?", ":%d" % port if port else "")


def _is_polite_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in _POLITE_HOSTS


def _redact_url(url: str) -> str:
    """Mask contact/credential query params for ledger writes."""
    if not url or "?" not in url:
        return url
    parts = urllib.parse.urlsplit(url)
    kept = []
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        kept.append((k, "<redacted>" if k.lower() in _SENSITIVE_PARAMS else v))
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment)
    )


class _CredSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry credentials across an origin.

    Two rules, both learned from the 2026-07-26 external audit:
      • origin change (scheme/host/port) → strip every credential header
        and record it, so a resulting 401/403 is diagnosable instead of
        looking like a publisher outage;
      • https → http downgrade → refuse the redirect outright (returning
        None makes urllib surface the 30x as an HTTPError, which the
        caller records as a normal non-2xx attempt).
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: List[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_scheme, old_host, old_port = _origin(req.full_url)
        new_scheme, new_host, new_port = _origin(new.full_url)
        if new_scheme not in _ALLOWED_SCHEMES:
            self.events.append("blocked_redirect_scheme:%s" % new_scheme)
            return None
        if old_scheme == "https" and new_scheme == "http":
            self.events.append("blocked_https_downgrade:%s" % (new_host or "?"))
            return None
        if (new_scheme, new_host, new_port) != (old_scheme, old_host, old_port):
            dropped = [
                k for k in list(new.headers) if k.lower() in _CRED_HEADER_NAMES
            ]
            for k in dropped:
                del new.headers[k]
            if dropped:
                self.events.append(
                    "cred_stripped_on_redirect:%s→%s"
                    % (_netloc(old_scheme, old_host, old_port),
                       _netloc(new_scheme, new_host, new_port))
                )
        return new


# ─────────────────────────── small helpers ───────────────────────────
class HttpResult:
    __slots__ = ("status", "final_url", "content_type", "body", "error", "redirect_notes")

    def __init__(self, status, final_url, content_type, body, error, redirect_notes=None):
        self.status: Optional[int] = status
        self.final_url: str = final_url
        self.content_type: str = content_type or ""
        self.body: bytes = body or b""
        self.error: Optional[str] = error
        # Credential/redirect events worth surfacing in the ledger (see
        # _CredSafeRedirectHandler). Empty list = nothing unusual happened.
        self.redirect_notes: List[str] = list(redirect_notes or ())


class Throttle:
    """Per-host minimum interval between requests.

    Thread-safe since 1.1: with --jobs > 1 several papers are in flight at once
    and they routinely share a host (two Springer DOIs, two arXiv ids). The
    politeness guarantee is per-host, so it has to hold across threads — an
    unsynchronised dict would let N threads all read the same `prev` and fire
    simultaneously, which is precisely the burst the throttle exists to stop.
    Reserving the slot *before* sleeping (rather than after) keeps concurrent
    callers to the same host queued one interval apart instead of all waking
    together.
    """

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.min_interval <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc.lower()
        with self._lock:
            now = time.monotonic()
            prev = self._last.get(host)
            slot = now if prev is None else max(now, prev + self.min_interval)
            self._last[host] = slot
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


class Config:
    def __init__(self, args):
        self.email: Optional[str] = args.email or os.environ.get("UNPAYWALL_EMAIL")
        self.timeout: int = args.timeout
        self.throttle = Throttle(args.throttle)
        self.elsevier_key: Optional[str] = os.environ.get("ELSEVIER_TDM_KEY")
        self.wiley_token: Optional[str] = os.environ.get("WILEY_TDM_TOKEN")
        self.institutional: bool = os.environ.get("PD_FETCH_INSTITUTIONAL") == "1"
        self.browser: bool = os.environ.get("PD_FETCH_BROWSER") == "1"
        # L4b. `api` = scholarly aggregators only (default, keyless, polite).
        # `off` = skip the layer entirely.
        self.title_search: str = getattr(args, "title_search", None) or "api"
        # S2 aggregates author-homepage PDFs more aggressively than Unpaywall,
        # but keyless it 429s on the first request from most IPs — so it is a
        # with-key-only sub-layer rather than a default.
        self.s2_key: Optional[str] = (os.environ.get("S2_API_KEY")
                                      or os.environ.get("SEMANTIC_SCHOLAR_API_KEY"))
        # L6b — borrow the Chrome session the user already has (pd_browser_fetch.py).
        self.cdp: bool = os.environ.get("PD_FETCH_CDP") == "1"
        self.cdp_port: int = int(os.environ.get("PD_CDP_PORT") or 9222)
        self.cdp_wait_human: int = int(os.environ.get("PD_CDP_WAIT_HUMAN") or 180)
        # Default UA carries NO personal data. The polite-pool variant (with
        # the user's mailto) is used only for _POLITE_HOSTS — see ua_for().
        self.user_agent = "paperdaily-deep-research/%s (+https://www.paperdaily.org)" % VERSION
        self.polite_user_agent = (
            "paperdaily-deep-research/%s (mailto:%s)" % (VERSION, self.email)
            if self.email
            else self.user_agent
        )

    def ua_for(self, url: str) -> str:
        """Contact email only goes to hosts that ask for one (Unpaywall,
        Crossref, NCBI/EBI). Every other host — publishers, CDNs, landing
        pages harvested from third-party HTML — sees the neutral UA."""
        return self.polite_user_agent if _is_polite_host(url) else self.user_agent


def _is_pdf_bytes(body: bytes) -> bool:
    """Engineering rule #1: trust the %PDF magic, never Content-Type."""
    if not body:
        return False
    if body[:4] == PDF_MAGIC:
        return True
    head = body[:1024]
    # tolerate a leading BOM / whitespace some servers emit before the stream
    if head[:3] == b"\xef\xbb\xbf":
        head = head[3:]
    head = head.lstrip(b" \t\r\n")
    if head[:4] == PDF_MAGIC:
        return True
    return b"%PDF-" in body[:1024]


def _looks_like_challenge(body: bytes) -> bool:
    low = body[:8192].lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def _pdf_pages(blob: bytes) -> int:
    """Page count without a PDF library.

    Counting `/Type /Page` (excluding `/Pages`) matched pypdf exactly on every
    file in a 12-PDF corpus check. Returns 0 on PDFs that keep their page tree
    in object streams — callers must treat 0 as "unknown", never as "empty".
    """
    return len(re.findall(rb"/Type\s*/Page[^s]", blob))


def _teaser_note(pdf: bytes, doi: str, config: "Config",
                 attempts: List[dict]) -> Optional[str]:
    """Is this PDF a publisher teaser rather than the article?

    The `%PDF` magic rule (engineering rule #1) catches paywall HTML served as
    application/pdf. It does not catch the other substitution: a *real* PDF that
    is only the first page or two of the article. Measured 2026-08-18 — Berghahn
    answered a request for a 15-page article (pp. 48-62) with a 2-page extract:
    valid header, clean text layer, opens fine. Every byte-level check passes.

    That file is worse than no file. The reading stage will take notes from it
    and cite page numbers that do not exist in the article, while the ledger says
    full text was obtained.

    Crossref knows the page range. To keep this free in the common case, the
    lookup only runs when the PDF is already suspiciously short — a normal
    20-page download never costs a request.
    """
    pages = _pdf_pages(pdf)
    if not pages or pages > 4 or not doi:
        return None
    data = _fetch_json("https://api.crossref.org/works/%s" % urllib.parse.quote(doi, safe=""),
                       config, "teaser_check", attempts)
    rng = (((data or {}).get("message") or {}).get("page") or "") if isinstance(data, dict) else ""
    m = re.match(r"\s*(\d+)\s*[-\u2013]+\s*(\d+)\s*$", str(rng))
    if not m:
        return None
    first, last = int(m.group(1)), int(m.group(2))
    want = last - first + 1
    if want < 4 or pages >= max(3, want * 0.6):
        return None
    return "%d pages, article is %d (pp. %s) — looks like a teaser extract" % (
        pages, want, rng)


def _safe_filename(paper_id: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", paper_id.strip())
    name = name.strip("._") or "paper"
    return name[:200] + ".pdf"


def _normalize_arxiv_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    if low.startswith("arxiv:"):
        s = s[6:]
    return s.strip() or None


def _norm_title_tokens(title: Optional[str]) -> frozenset:
    """Content-word token set of a title, for cross-version matching."""
    if not title:
        return frozenset()
    low = re.sub(r"[^a-z0-9\s]+", " ", str(title).lower())
    return frozenset(t for t in low.split() if t and t not in _TITLE_STOPWORDS)


def _title_similarity(a: Optional[str], b: Optional[str]) -> float:
    ta, tb = _norm_title_tokens(a), _norm_title_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _surnames(authorships: Any) -> frozenset:
    """Lowercased author surnames out of an OpenAlex `authorships` block."""
    out = set()
    if not isinstance(authorships, list):
        return frozenset()
    for a in authorships:
        if not isinstance(a, dict):
            continue
        name = ((a.get("author") or {}).get("display_name")) or a.get("raw_author_name") or ""
        parts = re.sub(r"[^A-Za-z\s'-]+", " ", str(name)).split()
        if parts:
            out.add(parts[-1].lower())
    return frozenset(out)


def _is_doi_landing(url: Optional[str], doi: Optional[str]) -> bool:
    """True when `url` is just the DOI resolver — i.e. NOT an open-access link.

    The upstream API fills `oa_url` with `https://doi.org/<doi>` whenever it has
    nothing better. Trying it at L1 is a guaranteed publisher landing page (403
    behind Cloudflare for most closed-access journals), and it is *already*
    what the institutional layer does deliberately at L5. Spending the request
    at L1 buys nothing and — the reason this check exists — puts a wall of 403s
    at the top of the ledger, which reads as "we are blocked everywhere" when
    the truth is "we never asked anyone who had it".
    """
    if not url:
        return False
    p = urllib.parse.urlsplit(url)
    if (p.hostname or "").lower() not in ("doi.org", "dx.doi.org", "www.doi.org"):
        return False
    if not doi:
        return True
    return p.path.lstrip("/").lower() == doi.strip().lower()


def _extract_citation_pdf_url(html: bytes, base_url: str) -> Optional[str]:
    """Pull <meta name=citation_pdf_url content=...> (order-independent)."""
    for tag in _META_TAG_RE.findall(html):
        mname = _META_NAME_RE.search(tag)
        if not mname or mname.group(1).lower() != b"citation_pdf_url":
            continue
        mcontent = _META_CONTENT_RE.search(tag)
        if not mcontent:
            continue
        raw = mcontent.group(1).decode("utf-8", "ignore")
        raw = html_mod.unescape(raw).strip()
        if raw:
            return urllib.parse.urljoin(base_url, raw)
    return None


# ─────────────────────────── HTTP core ───────────────────────────
def _http_get(
    url: str,
    config: Config,
    accept: str = "application/pdf,*/*",
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: Optional[int] = None,
) -> HttpResult:
    timeout = timeout or config.timeout
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        # file:// / ftp:// / data: never come from a source we trust.
        return HttpResult(None, url, "", b"", "blocked_scheme: %s" % (scheme or "(none)",))
    config.throttle.wait(url)
    headers = {"User-Agent": config.ua_for(url), "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    # Fresh handler per call: urllib keeps no connection pool, so the only
    # thing an opener carries is redirect state — which we want per-request.
    redirects = _CredSafeRedirectHandler()
    opener = urllib.request.build_opener(
        redirects, urllib.request.HTTPSHandler(context=_SSL_CTX)
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            final_url = resp.geturl()
            ctype = resp.headers.get("Content-Type", "") if resp.headers else ""
            body = resp.read(MAX_DOWNLOAD_BYTES + 1)
            return HttpResult(status, final_url, ctype, body, None, redirects.events)
    except urllib.error.HTTPError as e:  # 4xx/5xx — still a response
        try:
            body = e.read(MAX_DOWNLOAD_BYTES + 1)
        except Exception:
            body = b""
        ctype = e.headers.get("Content-Type", "") if getattr(e, "headers", None) else ""
        return HttpResult(e.code, url, ctype, body, None, redirects.events)
    except urllib.error.URLError as e:
        return HttpResult(None, url, "", b"", "urlerror: %s" % (getattr(e, "reason", e),),
                          redirects.events)
    except (TimeoutError, ssl.SSLError, ConnectionError) as e:
        return HttpResult(None, url, "", b"", "%s: %s" % (type(e).__name__, e),
                          redirects.events)
    except Exception as e:  # never let one URL crash the run
        return HttpResult(None, url, "", b"", "%s: %s" % (type(e).__name__, e),
                          redirects.events)


def _fetch_json(url: str, config: Config, layer: str, attempts: List[dict]) -> Optional[Any]:
    r = _http_get(url, config, accept="application/json")
    if r.error is not None:
        attempts.append(_attempt(layer, url, "error", None, r.error[:200]))
        return None
    if r.status is not None and 200 <= r.status < 300:
        try:
            return json.loads(r.body.decode("utf-8", "ignore"))
        except Exception as e:
            attempts.append(_attempt(layer, url, "error", r.status, "json parse: %s" % str(e)[:120]))
            return None
    attempts.append(_attempt(layer, url, _classify(r.status), r.status, "http %s" % r.status))
    return None


def _classify(status: Optional[int], body: bytes = b"") -> str:
    """HTTP status → ledger outcome.

    `denied` carries a promise in references/fulltext-sources.md — "we asked and
    were refused, do not retry" — and triage acts on it. A 404 does not mean
    that: it means this layer simply does not have the paper (Unpaywall has
    never heard of the DOI, the PDF URL is stale). Recording those as `denied`
    made the ledger read like a wall of permission failures. They are `miss`.
    """
    if status is None or status >= 500 or status == 429:
        return "error"
    if status in (404, 410):
        return "miss"
    if 200 <= status < 300:
        return "ok"
    # A challenge page is not a permission answer — see the module docstring.
    # It arrives as a 403 exactly like a paywall does, so the body is the only
    # thing that separates them.
    if body and _looks_like_challenge(body):
        return "blocked"
    return "denied"


def _attempt(layer: str, url: str, outcome: str, http_status: Optional[int], note: str) -> dict:
    # URLs land in fetch_report.jsonl on disk — mask contact/credential
    # query params first (Unpaywall/PMC put the user's email in the URL).
    return {
        "layer": layer,
        "url": _redact_url(url),
        "outcome": outcome,
        "http_status": http_status,
        "note": note,
    }


def _with_redirect_notes(r: HttpResult, note: str) -> str:
    """Append credential/redirect events so a 401 after a cross-origin hop
    reads as 'we dropped the key on purpose', not 'publisher flaked'."""
    if not r.redirect_notes:
        return note
    return "%s [%s]" % (note, "; ".join(r.redirect_notes))


def _record_pdf_attempt(r: HttpResult, layer: str, url: str, attempts: List[dict]) -> Optional[bytes]:
    """Turn a raw HttpResult into a PDF (bytes) or an attempt record. Never trusts
    Content-Type; validates the %PDF magic."""
    if r.error is not None:
        attempts.append(_attempt(layer, url, "error", None,
                                 _with_redirect_notes(r, r.error[:200])))
        return None
    if r.status is not None and 200 <= r.status < 300:
        if len(r.body) > MAX_DOWNLOAD_BYTES:
            attempts.append(_attempt(layer, url, "error", r.status, "exceeds_50mb"))
            return None
        if _is_pdf_bytes(r.body):
            attempts.append(_attempt(layer, url, "ok", r.status,
                                     _with_redirect_notes(r, "pdf %d bytes" % len(r.body))))
            return r.body
        note = "not_pdf"
        if r.content_type:
            note += " (%s)" % r.content_type.split(";")[0][:40]
        challenged = _looks_like_challenge(r.body)
        if challenged:
            note += " challenge (not an access decision)"
        attempts.append(_attempt(layer, url, "blocked" if challenged else "denied",
                                 r.status, _with_redirect_notes(r, note)))
        return None
    # non-2xx
    attempts.append(_attempt(layer, url, _classify(r.status, r.body), r.status,
                             _with_redirect_notes(r, "http %s" % r.status)))
    return None


# ─────────────────────────── L5 publisher routing ───────────────────────────
def _institutional_template_url(doi: str) -> Optional[str]:
    """DOI-prefix → direct PDF URL template for the user's own subscription access.

    Coverage (all listed prefixes handled; those returning None fall through to
    the doi.org landing-page `citation_pdf_url` step ②):
      direct template : 10.1038 Nature, 10.1126 Science, 10.1002/10.1111 Wiley,
                        10.1007/10.1057 Springer, 10.1073 PNAS, 10.1021 ACS,
                        10.1080 T&F, 10.1103 APS, 10.1145 ACM, 10.18653 ACL
      landing-only    : 10.1016 Elsevier, 10.1093 OUP, 10.1109 IEEE, 10.2139 SSRN
                        (need internal PII/arnumber/session → resolved via landing)
    """
    doi = doi.strip()
    if "/" in doi:
        prefix, suffix = doi.split("/", 1)
    else:
        prefix, suffix = doi, ""
    if prefix == "10.1038":  # Nature
        return "https://www.nature.com/articles/%s.pdf" % suffix
    if prefix == "10.1126":  # Science / AAAS (Atypon)
        return "https://www.science.org/doi/pdf/%s" % doi
    if prefix in ("10.1002", "10.1111"):  # Wiley
        return "https://onlinelibrary.wiley.com/doi/pdfdirect/%s" % doi
    if prefix in ("10.1007", "10.1057"):  # Springer / Palgrave
        return "https://link.springer.com/content/pdf/%s.pdf" % doi
    if prefix == "10.1073":  # PNAS
        return "https://www.pnas.org/doi/pdf/%s" % doi
    if prefix == "10.1021":  # ACS
        return "https://pubs.acs.org/doi/pdf/%s" % doi
    if prefix == "10.1080":  # Taylor & Francis
        return "https://www.tandfonline.com/doi/pdf/%s" % doi
    if prefix == "10.1103":  # APS
        return "https://link.aps.org/pdf/%s" % doi
    if prefix == "10.1145":  # ACM
        return "https://dl.acm.org/doi/pdf/%s" % doi
    if prefix == "10.18653":  # ACL Anthology (10.18653/v1/<anth-id>)
        anth = suffix[3:] if suffix.startswith("v1/") else suffix
        return "https://aclanthology.org/%s.pdf" % anth
    # 10.1016 / 10.1093 / 10.1109 / 10.2139 → landing-page fallback (step ②)
    return None


# ─────────────────────────── L3 Europe PMC PDF resolution ───────────────────────────
def _europepmc_pdf_urls(pmcid: str, config: Config, attempts: List[dict]) -> List[str]:
    """Candidate OA PDF URLs for a PMCID.

    The old one-shot render endpoint (europepmc.org/articles/<id>?pdf=render) is
    unreliable now (404s for many articles), so we prefer the Europe PMC REST
    `fullTextUrlList`, which points at the publisher's own open-access PDF, and
    keep the render endpoint as a secondary try.
    """
    urls: List[str] = []
    rest = ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=PMCID:%s"
            "&resultType=core&format=json" % urllib.parse.quote(pmcid))
    data = _fetch_json(rest, config, "L3_pmc", attempts)
    if isinstance(data, dict):
        for res in ((data.get("resultList") or {}).get("result") or []):
            ftl = (res.get("fullTextUrlList") or {}).get("fullTextUrl") or []
            for u in ftl:
                if not isinstance(u, dict):
                    continue
                if u.get("documentStyle") == "pdf" and \
                        "open access" in (u.get("availability") or "").lower():
                    url = u.get("url")
                    if url and url not in urls:
                        urls.append(url)
    render = "https://europepmc.org/articles/%s?pdf=render" % pmcid
    if render not in urls:
        urls.append(render)
    return urls


# ─────────────────────────── L4b title search ───────────────────────────
def _try_url_with_meta_hop(url: str, layer: str, config: Config,
                           attempts: List[dict]) -> Optional[Tuple[bytes, str]]:
    """GET `url`; if it answers HTML, chase its `citation_pdf_url` one hop.

    A title-search hit is usually a *record page* (SSRN, an institutional
    repository, a conference programme), not the file itself. Without this hop
    the layer reports `denied not_pdf (text/html)` on pages that are one
    documented meta tag away from the PDF.
    """
    r = _http_get(url, config, accept="text/html,application/pdf,*/*")
    pdf = _record_pdf_attempt(r, layer, url, attempts)
    if pdf:
        return pdf, url
    if r.error is None and r.status and 200 <= r.status < 300 and r.body:
        cu = _extract_citation_pdf_url(r.body, r.final_url)
        if cu and cu != url:
            pdf = _record_pdf_attempt(_http_get(cu, config), layer + "_meta", cu, attempts)
            if pdf:
                return pdf, cu
    return None


def _openalex_json(url: str, config: Config, attempts: List[dict]) -> Optional[Any]:
    """OpenAlex with one 429 backoff — the polite pool throttles per-IP."""
    data = _fetch_json(url, config, "L4b_title", attempts)
    if data is None and attempts and attempts[-1].get("http_status") == 429:
        time.sleep(2.5)
        data = _fetch_json(url, config, "L4b_title", attempts)
    return data


# OpenAlex `type` values that are not the manuscript we want. A replication
# package sits in the index as a sibling work with the SAME authors and a
# near-identical title, so it sails through the author guard and gets fetched
# ahead of the actual working paper — observed on the first run of this layer
# (hhag020 → 10.7910/dvn/liziip, a Harvard Dataverse deposit).
_NON_MANUSCRIPT_TYPES = frozenset(
    ("dataset", "peer-review", "grant", "retraction", "paratext",
     "editorial", "erratum", "letter", "libguides", "supplementary-materials")
)


def _oa_urls_of_work(work: dict, doi: Optional[str] = None) -> List[Tuple[str, bool]]:
    """[(url, is_landing)] for every open location of an OpenAlex work.

    Bare DOI-resolver URLs are dropped: OpenAlex records `https://doi.org/<doi>`
    as the landing page of a great many "open" locations, and following it just
    re-enters the publisher wall that the layers above already hit. Same reason
    L1 skips them — see _is_doi_landing.
    """
    out: List[Tuple[str, bool]] = []
    locs = list(work.get("locations") or [])
    best = work.get("best_oa_location")
    if isinstance(best, dict):
        locs.insert(0, best)
    for loc in locs:
        if not isinstance(loc, dict) or not loc.get("is_oa"):
            continue
        pdf, landing = loc.get("pdf_url"), loc.get("landing_page_url")
        if pdf and not _is_doi_landing(pdf, None) and (pdf, False) not in out:
            out.append((pdf, False))
        elif landing and not _is_doi_landing(landing, None) and (landing, True) not in out:
            out.append((landing, True))
    return out


def _title_candidates(paper: dict, config: Config,
                      attempts: List[dict]) -> List[Dict[str, Any]]:
    """Open copies of the SAME work found by title rather than by DOI.

    Why this layer exists (measured 2026-08-05 against the 8 papers of the
    hhag020 run): for a closed-access finance / econ / law article the DOI is
    registered only against the paywalled version of record, so every DOI-keyed
    layer above — Unpaywall, PMC, and even this work's own OpenAlex
    `best_oa_location` — correctly answers `miss`. 5 of those 8 had no OA
    location at all and 2 more pointed at an SSRN landing page. The openly
    readable copy exists, but as a *separate work*: the SSRN / NBER /
    conference / author-homepage working paper, with its own id, its own title
    and its own OA location. Title is the only key that reaches it.

    The matching guard is the author check, not a tighter threshold — see
    TITLE_SIM_WITH_AUTHOR. Every hit here is by construction a DIFFERENT
    manuscript version from the one requested, so the caller stamps
    `version: "alternate"` and the reading stage must say which version it read.
    """
    title = (paper.get("title") or "").strip()
    doi = (paper.get("doi") or "").strip()
    self_id = ""
    surnames: frozenset = frozenset()
    cands: List[Dict[str, Any]] = []

    # ① our own work — for author surnames (the guard) and, as a freebie, any
    #    OA location Unpaywall did not report.
    if doi:
        w = _openalex_json("https://api.openalex.org/works/doi:%s"
                           % urllib.parse.quote(doi, safe=""), config, attempts)
        if isinstance(w, dict):
            self_id = str(w.get("id") or "")
            surnames = _surnames(w.get("authorships"))
            title = title or (w.get("title") or "")
            for url, is_landing in _oa_urls_of_work(w, doi):
                cands.append({"url": url, "is_landing": is_landing, "version": "same-work",
                              "matched_id": self_id, "matched_title": w.get("title"),
                              "similarity": 1.0, "shared_authors": None})

    if not title:
        attempts.append(_attempt("L4b_title", "", "skipped_no_config", None,
                                 "no title to search on"))
        return cands

    # ② sibling works with the same title
    clean = re.sub(r"[^\w\s-]+", " ", title)
    clean = re.sub(r"\s+", " ", clean).strip()
    search = ("https://api.openalex.org/works?filter=title.search:%s&per-page=8"
              % urllib.parse.quote(clean))
    data = _openalex_json(search, config, attempts)
    n_seen = n_kept = 0
    if isinstance(data, dict):
        for w in (data.get("results") or []):
            if not isinstance(w, dict) or str(w.get("id") or "") == self_id:
                continue
            if str(w.get("type") or "").lower() in _NON_MANUSCRIPT_TYPES:
                continue
            n_seen += 1
            sim = _title_similarity(title, w.get("title"))
            shared = surnames & _surnames(w.get("authorships")) if surnames else frozenset()
            if shared:
                ok = sim >= TITLE_SIM_WITH_AUTHOR
            else:
                ok = sim >= TITLE_SIM_NO_AUTHOR
            if not ok:
                continue
            oa = _oa_urls_of_work(w, None)
            if not oa:
                continue
            n_kept += 1
            for url, is_landing in oa:
                cands.append({
                    "url": url, "is_landing": is_landing, "version": "alternate",
                    "matched_id": str(w.get("id") or ""), "matched_title": w.get("title"),
                    "similarity": round(sim, 3),
                    "shared_authors": sorted(shared) or None,
                })
    if n_seen and not n_kept:
        attempts.append(_attempt("L4b_title", search, "miss", 200,
                                 "%d title matches, none passed the author/similarity guard"
                                 % n_seen))

    # ③ Semantic Scholar — indexes author-homepage PDFs the aggregators above
    #    miss, but keyless it 429s immediately, so it is a with-key sub-layer.
    if config.s2_key:
        u = ("https://api.semanticscholar.org/graph/v1/paper/search?query=%s&limit=5"
             "&fields=title,externalIds,openAccessPdf" % urllib.parse.quote(clean))
        r = _http_get(u, config, accept="application/json",
                      extra_headers={"x-api-key": config.s2_key})
        if r.status is not None and 200 <= r.status < 300:
            try:
                d = json.loads(r.body.decode("utf-8", "ignore"))
            except Exception:
                d = None
            for p in ((d or {}).get("data") or []):
                pdf = (p.get("openAccessPdf") or {}).get("url")
                if not pdf:
                    continue
                sim = _title_similarity(title, p.get("title"))
                if sim < (TITLE_SIM_WITH_AUTHOR if surnames else TITLE_SIM_NO_AUTHOR):
                    continue
                cands.append({"url": pdf, "is_landing": False, "version": "alternate",
                              "matched_id": "S2:%s" % (p.get("paperId") or "?"),
                              "matched_title": p.get("title"),
                              "similarity": round(sim, 3), "shared_authors": None})
        else:
            attempts.append(_attempt("L4b_title_s2", _redact_url(u),
                                     _classify(r.status), r.status,
                                     "http %s" % r.status))
    elif not config.s2_key:
        attempts.append(_attempt("L4b_title_s2", "", "skipped_no_config", None,
                                 "no S2_API_KEY (keyless S2 429s immediately)"))

    # De-dup, then direct PDFs ahead of record pages (a landing page costs an
    # extra hop and often ends in a login form), and cap the fan-out: this
    # layer must not turn into a crawl. Ties keep insertion order — our own
    # work's locations first, then siblings in OpenAlex relevance order.
    seen, out = set(), []
    for c in cands:
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        out.append(c)
    out.sort(key=lambda c: 1 if c.get("is_landing") else 0)
    return out[:MAX_TITLE_CANDIDATES]


# ─────────────────────────── fetch waterfall ───────────────────────────
def _run_waterfall(paper: dict, config: Config, attempts: List[dict]) -> Tuple[Optional[bytes], Optional[str], Optional[str], Dict[str, Any]]:
    """Return (pdf_bytes, layer, url, provenance) on the first hit."""
    doi = (paper.get("doi") or "").strip() or None
    oa_url = (paper.get("oa_url") or "").strip() or None
    paper_id = str(paper.get("id") or "")
    arxiv_id = _normalize_arxiv_id(paper.get("arxiv_id"))
    if not arxiv_id and paper_id.lower().startswith("arxiv:"):
        arxiv_id = _normalize_arxiv_id(paper_id)

    challenge_landing: Optional[str] = None  # for L6
    nil: Dict[str, Any] = {}

    # ── L0 arXiv ──────────────────────────────────────────────
    if arxiv_id:
        for base in ("https://arxiv.org/pdf/", "https://export.arxiv.org/pdf/"):
            url = base + urllib.parse.quote(arxiv_id, safe="/.")
            pdf = _record_pdf_attempt(_http_get(url, config), "L0_arxiv", url, attempts)
            if pdf:
                return pdf, "L0_arxiv", url, nil

    # ── L0b paperdaily's own resolved PDF url ─────────────────
    # Free in the sense that costs nothing to *find*: the server already did
    # this resolution when its pdf-worker processed the paper, and the answer
    # rode along in the worklist. Tried before oa_url because it is an actual
    # PDF the platform reached, whereas oa_url degrades to a landing page.
    pd_pdf = (paper.get("pdf_url") or "").strip() or None
    if pd_pdf and not _is_doi_landing(pd_pdf, doi):
        hit = _try_url_with_meta_hop(pd_pdf, "L0b_pd_cache", config, attempts)
        if hit:
            return hit[0], "L0b_pd_cache", hit[1], {"found_by": "paperdaily_resolved"}

    # ── L1 oa_url ─────────────────────────────────────────────
    if oa_url and _is_doi_landing(oa_url, doi):
        # Not an OA link — see _is_doi_landing. Skipping it here removes a
        # guaranteed 403 from the head of the ledger; L5 still resolves the
        # very same landing page on purpose when the user opts in.
        attempts.append(_attempt("L1_oa_url", oa_url, "skipped_no_config", None,
                                 "oa_url is only the doi resolver, not an oa link"))
        oa_url = None
    if oa_url:
        r = _http_get(oa_url, config)
        pdf = _record_pdf_attempt(r, "L1_oa_url", oa_url, attempts)
        if pdf:
            return pdf, "L1_oa_url", oa_url, nil
        # returned HTML → chase citation_pdf_url one hop
        if r.error is None and r.status and 200 <= r.status < 300 and r.body:
            cu = _extract_citation_pdf_url(r.body, r.final_url)
            if cu and cu != oa_url:
                url2 = cu
                pdf = _record_pdf_attempt(_http_get(url2, config), "L1_oa_url_meta", url2, attempts)
                if pdf:
                    return pdf, "L1_oa_url_meta", url2, nil

    # ── L1b agent-supplied candidate URLs ─────────────────────
    # The web-search loop writes these back into the worklist (see
    # needs_web_search.jsonl). They are high-confidence — a human or an agent
    # looked at the search result — so they run ahead of every lookup API, but
    # they are still validated by %PDF magic like anything else.
    for u in (paper.get("urls_extra") or []):
        u = str(u or "").strip()
        if not u:
            continue
        hit = _try_url_with_meta_hop(u, "L1b_extra", config, attempts)
        if hit:
            return hit[0], "L1b_extra", hit[1], {"version": "alternate",
                                                 "found_by": "agent_web_search"}

    # ── L2 Unpaywall ──────────────────────────────────────────
    if doi:
        if not config.email:
            attempts.append(_attempt("L2_unpaywall", "", "skipped_no_config", None,
                                     "no email (--email / UNPAYWALL_EMAIL)"))
        else:
            api = "https://api.unpaywall.org/v2/%s?email=%s" % (
                urllib.parse.quote(doi, safe=""), urllib.parse.quote(config.email))
            data = _fetch_json(api, config, "L2_unpaywall", attempts)
            if isinstance(data, dict):
                candidates: List[str] = []
                best = data.get("best_oa_location") or {}
                if isinstance(best, dict) and best.get("url_for_pdf"):
                    candidates.append(best["url_for_pdf"])
                for loc in (data.get("oa_locations") or []):
                    if isinstance(loc, dict):
                        u = loc.get("url_for_pdf")
                        if u and u not in candidates:
                            candidates.append(u)
                if not candidates:
                    attempts.append(_attempt("L2_unpaywall", api, "miss", 200,
                                             "unpaywall knows the doi but lists no oa pdf"))
                for u in candidates:
                    pdf = _record_pdf_attempt(_http_get(u, config), "L2_unpaywall", u, attempts)
                    if pdf:
                        return pdf, "L2_unpaywall", u, nil

    # ── L3 Europe PMC (via NCBI/PMC ID Converter) ─────────────
    if doi:
        # NOTE: the classic idconv host (www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0)
        # now 301-redirects to the pmc.ncbi.nlm.nih.gov API below; hit it directly.
        # `email=` only when the user actually supplied one — a fake contact
        # address is worse than none for a polite-pool API.
        conv = ("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=%s&format=json"
                "&tool=paperdaily-deep-research" % urllib.parse.quote(doi, safe=""))
        if config.email:
            conv += "&email=%s" % urllib.parse.quote(config.email)
        data = _fetch_json(conv, config, "L3_pmc", attempts)
        pmcid = None
        if isinstance(data, dict):
            for rec in (data.get("records") or []):
                if isinstance(rec, dict) and rec.get("pmcid"):
                    pmcid = rec["pmcid"]
                    break
        if pmcid:
            for url in _europepmc_pdf_urls(pmcid, config, attempts):
                pdf = _record_pdf_attempt(_http_get(url, config), "L3_pmc", url, attempts)
                if pdf:
                    return pdf, "L3_pmc", url, nil
        elif isinstance(data, dict):
            attempts.append(_attempt("L3_pmc", conv, "miss", 200, "no PMCID for this doi"))

    # ── L4 TDM (publisher API keys) ───────────────────────────
    if doi:
        prefix = doi.split("/", 1)[0]
        # Elsevier
        if prefix in ELSEVIER_PREFIXES:
            if not config.elsevier_key:
                attempts.append(_attempt("L4_elsevier", "", "skipped_no_config", None,
                                         "no ELSEVIER_TDM_KEY"))
            else:
                url = ("https://api.elsevier.com/content/article/doi/%s"
                       "?httpAccept=application%%2Fpdf" % urllib.parse.quote(doi, safe="/"))
                r = _http_get(url, config, extra_headers={"X-ELS-APIKey": config.elsevier_key})
                pdf = _record_pdf_attempt(r, "L4_elsevier", url, attempts)
                if pdf:
                    return pdf, "L4_elsevier", url, nil
        # Wiley
        if prefix in WILEY_PREFIXES:
            if not config.wiley_token:
                attempts.append(_attempt("L4_wiley", "", "skipped_no_config", None,
                                         "no WILEY_TDM_TOKEN"))
            else:
                url = "https://api.wiley.com/onlinelibrary/tdm/v1/articles/%s" % \
                    urllib.parse.quote(doi, safe="")
                r = _http_get(url, config, extra_headers={"Wiley-TDM-Client-Token": config.wiley_token})
                pdf = _record_pdf_attempt(r, "L4_wiley", url, attempts)
                if pdf:
                    return pdf, "L4_wiley", url, nil

    # ── L4b title search (open copies of the SAME work) ───────
    # Runs ahead of L5/L6 on purpose: it is keyless, needs no opt-in and has no
    # compliance question attached, whereas both layers below are opt-in and
    # therefore usually skipped. For closed-access econ/finance/law papers this
    # is the layer that actually lands the file.
    if config.title_search != "off":
        for c in _title_candidates(paper, config, attempts):
            hit = _try_url_with_meta_hop(c["url"], "L4b_title", config, attempts)
            if hit:
                prov = {k: c[k] for k in
                        ("version", "matched_id", "matched_title", "similarity",
                         "shared_authors") if c.get(k) is not None}
                prov["found_by"] = "title_search"
                return hit[0], "L4b_title", hit[1], prov
    else:
        attempts.append(_attempt("L4b_title", "", "skipped_optin", None,
                                 "--title-search off"))

    # ── L5 institutional (opt-in) ─────────────────────────────
    if config.institutional:
        if doi:
            tmpl = _institutional_template_url(doi)
            if tmpl:
                pdf = _record_pdf_attempt(_http_get(tmpl, config), "L5_template", tmpl, attempts)
                if pdf:
                    return pdf, "L5_template", tmpl, nil
            # step ②: resolve doi.org landing → citation_pdf_url
            landing = "https://doi.org/%s" % urllib.parse.quote(doi, safe="/")
            r = _http_get(landing, config, accept="text/html,application/pdf,*/*")
            if r.error is None and r.status and 200 <= r.status < 300 and r.body:
                if _is_pdf_bytes(r.body):  # some resolvers land straight on a PDF
                    attempts.append(_attempt("L5_landing", r.final_url, "ok", r.status,
                                             "pdf %d bytes" % len(r.body)))
                    return r.body, "L5_landing", r.final_url, nil
                if _looks_like_challenge(r.body):
                    challenge_landing = r.final_url
                cu = _extract_citation_pdf_url(r.body, r.final_url)
                if cu:
                    pdf = _record_pdf_attempt(_http_get(cu, config), "L5_landing", cu, attempts)
                    if pdf:
                        return pdf, "L5_landing", cu, nil
                    if not challenge_landing:
                        challenge_landing = r.final_url
                else:
                    attempts.append(_attempt("L5_landing", r.final_url, "miss", r.status,
                                             "no citation_pdf_url on landing page"))
            else:
                outcome = "error" if r.error else _classify(r.status)
                attempts.append(_attempt("L5_landing", landing, outcome, r.status,
                                         (r.error or "http %s" % r.status)[:120]))
                challenge_landing = r.final_url or landing
        else:
            attempts.append(_attempt("L5_institutional", "", "skipped_no_config", None,
                                     "no doi to route"))
    elif doi or oa_url:
        attempts.append(_attempt("L5_institutional", "", "skipped_optin", None,
                                 "set PD_FETCH_INSTITUTIONAL=1 (only on an authorised network)"))

    # ── L6b your own Chrome session, over CDP (opt-in) ────────────
    # Ahead of L6: a headless throwaway browser sees exactly what urllib sees
    # (no cookies, no clearance, no institutional session), whereas this one
    # reuses the browsing session the user already established — which is the
    # only thing that gets past a Cloudflare challenge or an SSO wall. It also
    # needs no install. Ordering them the other way round would spend the
    # weaker layer's failure first for no reason.
    if config.cdp:
        target = challenge_landing or oa_url or (("https://doi.org/%s" % doi) if doi else None)
        if target:
            pdf, url = _cdp_fetch(target, config, attempts)
            if pdf:
                return pdf, "L6b_cdp", url, {"found_by": "browser_session"}
        else:
            attempts.append(_attempt("L6b_cdp", "", "skipped_no_config", None,
                                     "nothing to open (no oa_url and no doi)"))
    elif challenge_landing or doi or oa_url:
        attempts.append(_attempt("L6b_cdp", "", "skipped_optin", None,
                                 "set PD_FETCH_CDP=1 to use your own Chrome session"))

    # ── L6 headless browser (opt-in) ──────────────────────────────
    # PD_FETCH_BROWSER stands on its own. Plenty of *open-access* repositories
    # and journal landing pages only reveal their PDF link after JS runs, and
    # rendering a public page is not an access-control question. This layer
    # additionally required PD_FETCH_INSTITUTIONAL=1 until 0.4.0, which made it
    # unreachable for OA-only users and contradicted the documented trigger in
    # references/fulltext-sources.md. The compliance line is held where it
    # actually is, one level down: a rendered page that exposes no PDF link, or
    # whose link returns a non-%PDF body, is recorded `denied` and we move on —
    # nothing here tries to get past a wall.
    if config.browser:
        target = challenge_landing or oa_url or (("https://doi.org/%s" % doi) if doi else None)
        if target:
            pdf, url = _browser_fetch(target, config, attempts)
            if pdf:
                return pdf, "L6_browser", url, nil
        else:
            attempts.append(_attempt("L6_browser", "", "skipped_no_config", None,
                                     "nothing to render (no oa_url and no doi)"))
    elif challenge_landing or doi or oa_url:
        attempts.append(_attempt("L6_browser", "", "skipped_optin", None,
                                 "set PD_FETCH_BROWSER=1 to render the landing page"))

    return None, None, None, nil


def _cdp_fetch(landing_url: str, config: Config,
               attempts: List[dict]) -> Tuple[Optional[bytes], Optional[str]]:
    """L6b: hand the URL to pd_browser_fetch.py, which drives the user's Chrome.

    Imported lazily and from this script's own directory: fetch_fulltext.py is
    meant to stay runnable on its own, so a missing sibling degrades to
    `skipped_no_config` rather than an ImportError at startup.
    """
    import tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import pd_browser_fetch as pbf  # type: ignore
    except Exception as e:
        attempts.append(_attempt("L6b_cdp", landing_url, "skipped_no_config", None,
                                 "pd_browser_fetch.py not importable: %s" % str(e)[:100]))
        return None, None
    try:
        cdp = pbf.CDP(config.cdp_port, timeout=config.timeout)
    except Exception as e:
        # The setup hint is multi-line and worth keeping intact — this is the
        # one failure a user can fix in ten seconds if they are told how.
        attempts.append(_attempt("L6b_cdp", landing_url, "skipped_no_config", None,
                                 str(e).replace("\n", " ")[:220]))
        return None, None
    tmpdir = tempfile.mkdtemp(prefix="pd-cdp-")
    try:
        rec = pbf.fetch_one(cdp, landing_url, tmpdir, config.cdp_wait_human,
                            float(config.timeout), False)
        pdf = rec.pop("_pdf", None)
        if pdf:
            attempts.append(_attempt("L6b_cdp", rec.get("pdf_url") or landing_url,
                                     "ok", None, "pdf %d bytes via %s"
                                     % (len(pdf), rec.get("mechanism"))))
            return pdf, rec.get("pdf_url") or landing_url
        # `blocked` passes through: the browser layer reports it when a human
        # gate was never cleared, which is a statement about the human, not
        # about entitlement. Flattening it to `denied` here would re-introduce
        # exactly the conflation the outcome was split out to prevent.
        attempts.append(_attempt("L6b_cdp", landing_url,
                                 rec.get("status") if rec.get("status") in
                                 ("denied", "blocked", "error") else "denied",
                                 None, (rec.get("note") or "no pdf")[:160]))
        return None, None
    except Exception as e:
        attempts.append(_attempt("L6b_cdp", landing_url, "error", None,
                                 "%s: %s" % (type(e).__name__, str(e)[:120])))
        return None, None
    finally:
        try:
            cdp.close()
        except Exception:
            pass
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)


def _browser_fetch(landing_url: str, config: Config, attempts: List[dict]) -> Tuple[Optional[bytes], Optional[str]]:
    """L6: render the landing page with headless chromium and download the PDF it
    exposes. Import is guarded so a machine without playwright never crashes."""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except Exception as e:
        attempts.append(_attempt("L6_browser", landing_url, "skipped_no_config", None,
                                 "playwright not importable: %s" % str(e)[:100]))
        return None, None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=config.user_agent)
                page = ctx.new_page()
                page.goto(landing_url, wait_until="domcontentloaded",
                          timeout=config.timeout * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                content = page.content().encode("utf-8", "ignore")
                pdf_url = _extract_citation_pdf_url(content, page.url)
                if not pdf_url:
                    href = page.evaluate(
                        """() => {
                            const a = document.querySelector(
                                'a[href$=".pdf"], a[href*=\\'/pdf\\'], a[href*=\\'pdf\\']');
                            return a ? a.href : null;
                        }""")
                    if href:
                        pdf_url = href
                if not pdf_url:
                    attempts.append(_attempt("L6_browser", page.url, "denied", None,
                                             "no pdf link on rendered page"))
                    return None, None
                resp = page.request.get(pdf_url, timeout=config.timeout * 1000)
                body = resp.body()
                if len(body) > MAX_DOWNLOAD_BYTES:
                    attempts.append(_attempt("L6_browser", pdf_url, "error", resp.status, "exceeds_50mb"))
                    return None, None
                if _is_pdf_bytes(body):
                    attempts.append(_attempt("L6_browser", pdf_url, "ok", resp.status,
                                             "pdf %d bytes" % len(body)))
                    return body, pdf_url
                attempts.append(_attempt("L6_browser", pdf_url, "denied", resp.status,
                                         "rendered link not a pdf"))
                return None, None
            finally:
                browser.close()
    except Exception as e:
        attempts.append(_attempt("L6_browser", landing_url, "error", None,
                                 "%s: %s" % (type(e).__name__, str(e)[:120])))
        return None, None


# ─────────────────────────── per-paper driver ───────────────────────────
def fetch_one(paper: dict, out_dir: str, config: Config) -> dict:
    paper_id = str(paper.get("id") or "").strip()
    if not paper_id:
        # synthesise an id so the file/report is still keyed
        paper_id = (paper.get("doi") or paper.get("arxiv_id") or "unknown").strip()
        paper["id"] = paper_id
    title = paper.get("title")
    fname = _safe_filename(paper_id)
    fpath = os.path.join(out_dir, fname)

    record: Dict[str, Any] = {
        "id": paper_id, "title": title, "status": None, "layer": None,
        "url": None, "file": None, "bytes": None,
        # `carrier` (1.1) separates "we have the full text" from "we saved a PDF
        # binary". A page-ordered Markdown parse of a public PDF is full text;
        # the downstream reading + upload stages must be able to say so instead
        # of inferring depth from whether a .pdf sits on disk.
        "carrier": None, "sha256": None, "version": None,
        "attempts": [],
    }

    # ── idempotency: existing valid PDF ──
    if os.path.isfile(fpath):
        try:
            with open(fpath, "rb") as fh:
                head = fh.read(1024)
            if _is_pdf_bytes(head):
                sz = os.path.getsize(fpath)
                record.update(status="already", file=fname, bytes=sz,
                              carrier="binary-pdf")
                return record
        except OSError:
            pass  # unreadable → re-fetch

    attempts: List[dict] = []
    pdf, layer, url, prov = _run_waterfall(paper, config, attempts)
    record["attempts"] = attempts

    if pdf:
        os.makedirs(out_dir, exist_ok=True)
        tmp = fpath + ".part"
        with open(tmp, "wb") as fh:
            fh.write(pdf)
        os.replace(tmp, fpath)
        teaser = _teaser_note(pdf, (paper.get("doi") or "").strip(), config, attempts)
        record.update(status="ok", layer=layer, url=url, file=fname, bytes=len(pdf),
                      carrier="binary-pdf",
                      sha256=hashlib.sha256(pdf).hexdigest())
        if teaser:
            # Keep the file — it is evidence, and a human may want to look. But
            # strip the carrier so the phase gate cannot count it as full text,
            # and say so loudly enough that it never reaches a reading agent.
            record.update(partial=True, carrier=None, note=teaser)
        # A hit found by title is, by construction, a different manuscript
        # version from the DOI that was asked for. Say so in the ledger — the
        # reading stage has to record which version it read, and page numbers
        # do not carry across versions.
        for k, v in (prov or {}).items():
            record[k] = v
        if not record.get("version") and layer == "L0_arxiv" and (paper.get("doi") or "").strip():
            record["version"] = "preprint"
    else:
        record.update(status="failed")
        # Surface a challenge-blocked failure distinctly. "All layers exhausted"
        # and "the publisher put a bot check in front of content you are entitled
        # to" need opposite next moves: the first wants a web search for an open
        # copy, the second wants the user to open the page in their own browser,
        # where it will very likely just work.
        blocked_at = sorted({_origin(a["url"])[1] for a in attempts
                             if a.get("outcome") == "blocked" and a.get("url")})
        if blocked_at:
            record["blocked_by_challenge"] = blocked_at
        doi = (paper.get("doi") or "").strip()
        if doi:
            record["manual_url"] = "https://doi.org/%s" % doi
        elif (paper.get("oa_url") or "").strip():
            record["manual_url"] = paper["oa_url"].strip()
        # Hand the one remaining move back to the caller in machine-readable
        # form. A general web search reaches working-paper copies that no
        # scholarly aggregator indexes, but no script can run one unattended
        # (DDG/Bing soft-block scripted clients, S2 keyless 429s) — the agent
        # driving this script can. See needs_web_search.jsonl in main().
        if title:
            record["needs_web_search"] = True
    return record


# ─────────────────────────── triage ───────────────────────────
def triage_one(paper: dict, config: Config) -> dict:
    """Classify reachability WITHOUT downloading anything.

    Stage 1.5 has to choose a corpus on relevance × obtainability, and the only
    way it learned obtainability before was to run the whole waterfall and read
    the wreckage. That is backwards: by then the choice has already been made.
    This runs the *lookup* layers only — no PDF bytes cross the wire — and
    labels each paper so the selection can be made with both axes in hand.
    """
    attempts: List[dict] = []
    doi = (paper.get("doi") or "").strip() or None
    oa_url = (paper.get("oa_url") or "").strip() or None
    arxiv_id = _normalize_arxiv_id(paper.get("arxiv_id"))
    pid = str(paper.get("id") or "")
    if not arxiv_id and pid.lower().startswith("arxiv:"):
        arxiv_id = _normalize_arxiv_id(pid)

    out: Dict[str, Any] = {"id": pid, "title": paper.get("title"),
                           "class": None, "why": None, "candidate": None}
    if paper.get("urls_extra"):
        out.update({"class": "direct-pdf", "why": "urls_extra supplied",
                    "candidate": list(paper["urls_extra"])[0]})
        return out
    if arxiv_id:
        out.update({"class": "direct-pdf", "why": "arXiv id",
                    "candidate": "https://arxiv.org/pdf/%s" % arxiv_id})
        return out
    if doi and config.email:
        api = "https://api.unpaywall.org/v2/%s?email=%s" % (
            urllib.parse.quote(doi, safe=""), urllib.parse.quote(config.email))
        data = _fetch_json(api, config, "triage_unpaywall", attempts)
        if isinstance(data, dict):
            best = data.get("best_oa_location") or {}
            if isinstance(best, dict) and best.get("url_for_pdf"):
                out.update({"class": "oa-pdf", "why": "unpaywall best_oa_location",
                            "candidate": best["url_for_pdf"]})
                return out
    if config.title_search != "off":
        cands = _title_candidates(paper, config, attempts)
        direct = [c for c in cands if not c.get("is_landing")]
        if direct:
            out.update({"class": "title-search", "why": "open sibling work (%s)"
                        % (direct[0].get("matched_title") or "?")[:60],
                        "candidate": direct[0]["url"]})
            return out
        if cands:
            out.update({"class": "repo-landing", "why": "sibling work, landing page only",
                        "candidate": cands[0]["url"]})
            return out
    if oa_url and not _is_doi_landing(oa_url, doi):
        out.update({"class": "browser-needed", "why": "publisher url, no open copy located",
                    "candidate": oa_url})
        return out
    out.update({"class": "paywalled-only",
                "why": "no open copy in any lookup; needs a web search, your "
                       "own browser session, or manual retrieval",
                "candidate": ("https://doi.org/%s" % doi) if doi else None})
    return out


# ─────────────────────────── worklist / CLI ───────────────────────────
def _load_worklist(path: str) -> Tuple[List[dict], int]:
    papers: List[dict] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    papers.append(obj)
                else:
                    bad += 1
            except json.JSONDecodeError:
                bad += 1
    return papers, bad


def _default_report_path(out_dir: str) -> str:
    return os.path.normpath(os.path.join(out_dir, "..", "fetch_report.jsonl"))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_fulltext.py",
        description="Fetch open-access / user-authorised full-text PDFs for a worklist of papers.")
    ap.add_argument("--worklist", help="path to worklist.jsonl (one paper per line)")
    ap.add_argument("--doi", help="single-paper mode: fetch this DOI")
    ap.add_argument("--arxiv", help="single-paper mode: fetch this arXiv id")
    ap.add_argument("--out", required=True, help="output directory for PDFs")
    ap.add_argument("--report", help="report jsonl path (default <out>/../fetch_report.jsonl)")
    ap.add_argument("--email", help="polite email for Unpaywall/Crossref (or env UNPAYWALL_EMAIL)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="per-request timeout seconds")
    ap.add_argument("--throttle", type=float, default=DEFAULT_THROTTLE,
                    help="min seconds between requests to the same host")
    ap.add_argument("--title-search", choices=("api", "off"), default="api",
                    help="L4b: find open copies of the same work by title "
                         "(default api; 'off' disables the layer)")
    ap.add_argument("--triage", action="store_true",
                    help="classify reachability without downloading; writes triage.jsonl")
    ap.add_argument("--jobs", type=int, default=4,
                    help="papers in flight at once (default 4; per-host throttle "
                         "still applies, so this only parallelises across hosts)")
    args = ap.parse_args(argv)

    # ── validate mode ──
    modes = sum(bool(x) for x in (args.worklist, args.doi, args.arxiv))
    if modes == 0:
        sys.stderr.write("error: need one of --worklist / --doi / --arxiv\n")
        return 1
    if modes > 1:
        sys.stderr.write("error: --worklist / --doi / --arxiv are mutually exclusive\n")
        return 1

    config = Config(args)
    out_dir = args.out
    report_path = args.report or _default_report_path(out_dir)

    # ── build paper list ──
    if args.worklist:
        if not os.path.isfile(args.worklist):
            sys.stderr.write("error: worklist not found: %s\n" % args.worklist)
            return 1
        papers, bad = _load_worklist(args.worklist)
        if bad:
            sys.stderr.write("warning: skipped %d malformed worklist line(s)\n" % bad)
        if not papers:
            sys.stderr.write("error: worklist has no valid rows\n")
            return 1
    elif args.arxiv:
        aid = _normalize_arxiv_id(args.arxiv)
        papers = [{"id": "arxiv:%s" % aid, "title": None, "arxiv_id": aid,
                   "doi": None, "oa_url": None}]
    else:  # args.doi
        papers = [{"id": args.doi.strip(), "title": None, "doi": args.doi.strip(),
                   "arxiv_id": None, "oa_url": None}]

    os.makedirs(out_dir, exist_ok=True)
    report_dir = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(report_dir, exist_ok=True)

    n = len(papers)

    # ── triage mode: lookups only, no PDF bytes ──
    if args.triage:
        tri_path = os.path.join(report_dir, "triage.jsonl")
        rows: List[dict] = []
        with open(tri_path, "w", encoding="utf-8") as fh:
            for i, paper in enumerate(papers, 1):
                sys.stderr.write("[%d/%d] triage %s\n" % (i, n, paper.get("id") or "?"))
                sys.stderr.flush()
                row = triage_one(paper, config)
                rows.append(row)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
        buckets: Dict[str, int] = {}
        for r in rows:
            buckets[r["class"]] = buckets.get(r["class"], 0) + 1
        sys.stderr.write("\ntriage: %s\n" % ", ".join(
            "%s %d" % (k, v) for k, v in sorted(buckets.items())))
        sys.stderr.write("  → %s\n" % tri_path)
        sys.stderr.write("  pick the corpus on relevance × obtainability before "
                         "fetching; paywalled-only papers cost a web search each.\n")
        return 0

    fetched = already = failed = blocked = 0
    needs_search: List[dict] = []
    # The per-host throttle is what keeps us polite, and it is enforced inside
    # each request — so running several papers at once only overlaps *different*
    # hosts, which is free. Serialised when the CDP layer is on: that one opens
    # browser tabs and may stop to ask a human, and interleaving those prompts
    # across threads would be unreadable.
    jobs = max(1, int(args.jobs or 1))
    if config.cdp and jobs > 1:
        sys.stderr.write("note: PD_FETCH_CDP=1 → forcing --jobs 1 "
                         "(the browser layer may pause for a human)\n")
        jobs = 1
    done = [0]
    done_lock = threading.Lock()

    def _run(item: Tuple[int, dict]) -> Tuple[dict, dict]:
        i, paper = item
        rec = fetch_one(paper, out_dir, config)
        with done_lock:
            done[0] += 1
            sys.stderr.write("[%d/%d] %-8s %s%s\n" % (
                done[0], n, rec["status"], rec["id"],
                (" via %s" % rec["layer"]) if rec.get("layer") else ""))
            # Flush explicitly: Python block-buffers stderr when it is not a
            # tty, so under `> log 2>&1` the log stays empty until exit and a
            # 20-minute fetch looks hung.
            sys.stderr.flush()
        return rec, paper

    import contextlib
    from concurrent.futures import ThreadPoolExecutor

    # `with` on the executor matters: without it an exception mid-iteration
    # leaves non-daemon worker threads running and the process hangs at exit
    # instead of reporting the error.
    pool_cm = (ThreadPoolExecutor(max_workers=jobs) if jobs > 1
               else contextlib.nullcontext(None))
    with open(report_path, "w", encoding="utf-8") as rep, pool_cm as pool:
        items = list(enumerate(papers, 1))
        results = pool.map(_run, items) if pool else (_run(x) for x in items)
        for rec, paper in results:
            rep.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rep.flush()
            status = rec["status"]
            if status == "ok":
                fetched += 1
            elif status == "already":
                already += 1
            else:
                failed += 1
                if rec.get("blocked_by_challenge"):
                    blocked += 1
                if rec.get("needs_web_search"):
                    needs_search.append({
                        "id": rec["id"], "title": rec.get("title"),
                        "doi": (paper.get("doi") or None),
                        "venue": paper.get("venue"),
                        "suggested_query": '"%s" filetype:pdf' % (rec.get("title") or ""),
                        "reason": "all automatic layers exhausted; a general web "
                                  "search reaches working-paper copies that "
                                  "scholarly aggregators do not index",
                    })

    sys.stderr.write("fetched %d / already %d / failed %d of %d\n" %
                     (fetched, already, failed, n))
    sys.stderr.write("report: %s\n" % report_path)
    if blocked:
        # Say this before the web-search hand-back: for these papers a web
        # search is the wrong next move.
        sys.stderr.write(
            "%d paper(s) were stopped by a bot challenge, NOT by a paywall.\n"
            "  A challenge says nothing about entitlement — if your institution\n"
            "  subscribes, opening `manual_url` in your own browser usually just\n"
            "  works. See `blocked_by_challenge` in the report for the hosts.\n"
            "  Run this from inside the entitled network with PD_FETCH_CDP=1 and\n"
            "  clear the challenge in your own browser when it asks.\n"
            % blocked)

    # The web-search hand-back. Written even when empty (as a 0-byte file) so
    # the caller can tell "the loop ran and found nothing to do" apart from
    # "the loop never ran" — the same reason `miss` was split out of `denied`.
    nws_path = os.path.join(report_dir, "needs_web_search.jsonl")
    with open(nws_path, "w", encoding="utf-8") as fh:
        for row in needs_search:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    if needs_search:
        sys.stderr.write(
            "%d paper(s) need a web search → %s\n"
            "  next: search each `suggested_query`, add the PDF/record-page URLs to that\n"
            "  paper's worklist row as \"urls_extra\": [...], then re-run this script\n"
            "  (already-fetched papers are skipped).\n" % (len(needs_search), nws_path))
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
