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
    (L5/L6), which are OFF by default and must be explicitly opted into.

It contains NO paywall-circumvention: no Sci-Hub / LibGen / shadow mirrors, no
credential sharing, no cookie/session theft, no CAPTCHA solving. The optional
institutional (L5) and headless-browser (L6) layers are lawful direct access
that only makes sense when the machine running this script is inside a network
your institution has licensed (e.g. campus-IP authentication). You are
responsible for confirming you are entitled to the content you fetch; enable
L5/L6 only if you are on such an authorised network. When a source returns a
paywall page instead of a PDF, the layer is recorded as `denied` and the script
moves on — it never tries to defeat the wall.

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
  --email / env UNPAYWALL_EMAIL   polite email for Unpaywall/Crossref (L2 needs it)
  --timeout       per-request timeout, seconds (default 30)
  --throttle      min seconds between requests to the SAME host (default 1.0)

Env toggles:
  PD_FETCH_INSTITUTIONAL=1   enable L5 (only if on an authorised network)
  PD_FETCH_BROWSER=1         enable L6 headless-browser fallback (needs playwright)
  ELSEVIER_TDM_KEY           Elsevier TDM API key (L4)
  WILEY_TDM_TOKEN            Wiley TDM client token (L4)

Exit codes: 0 all fetched/already · 2 one or more failed · 1 usage/arg error.
"""

from __future__ import annotations

import argparse
import html as html_mod
import json
import os
import re
import ssl
import sys
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
VERSION = "1.0"

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


# ─────────────────────────── small helpers ───────────────────────────
class HttpResult:
    __slots__ = ("status", "final_url", "content_type", "body", "error")

    def __init__(self, status, final_url, content_type, body, error):
        self.status: Optional[int] = status
        self.final_url: str = final_url
        self.content_type: str = content_type or ""
        self.body: bytes = body or b""
        self.error: Optional[str] = error


class Throttle:
    """Per-host minimum interval between requests."""

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, min_interval)
        self._last: Dict[str, float] = {}

    def wait(self, url: str) -> None:
        if self.min_interval <= 0:
            return
        host = urllib.parse.urlsplit(url).netloc.lower()
        now = time.monotonic()
        prev = self._last.get(host)
        if prev is not None:
            delta = now - prev
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
        self._last[host] = time.monotonic()


class Config:
    def __init__(self, args):
        self.email: Optional[str] = args.email or os.environ.get("UNPAYWALL_EMAIL")
        self.timeout: int = args.timeout
        self.throttle = Throttle(args.throttle)
        self.elsevier_key: Optional[str] = os.environ.get("ELSEVIER_TDM_KEY")
        self.wiley_token: Optional[str] = os.environ.get("WILEY_TDM_TOKEN")
        self.institutional: bool = os.environ.get("PD_FETCH_INSTITUTIONAL") == "1"
        self.browser: bool = os.environ.get("PD_FETCH_BROWSER") == "1"
        contact = self.email or "https://www.paperdaily.org"
        self.user_agent = "paperdaily-deep-research/%s (mailto:%s)" % (VERSION, contact)


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
    config.throttle.wait(url)
    headers = {"User-Agent": config.user_agent, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            final_url = resp.geturl()
            ctype = resp.headers.get("Content-Type", "") if resp.headers else ""
            body = resp.read(MAX_DOWNLOAD_BYTES + 1)
            return HttpResult(status, final_url, ctype, body, None)
    except urllib.error.HTTPError as e:  # 4xx/5xx — still a response
        try:
            body = e.read(MAX_DOWNLOAD_BYTES + 1)
        except Exception:
            body = b""
        ctype = e.headers.get("Content-Type", "") if getattr(e, "headers", None) else ""
        return HttpResult(e.code, url, ctype, body, None)
    except urllib.error.URLError as e:
        return HttpResult(None, url, "", b"", "urlerror: %s" % (getattr(e, "reason", e),))
    except (TimeoutError, ssl.SSLError, ConnectionError) as e:
        return HttpResult(None, url, "", b"", "%s: %s" % (type(e).__name__, e))
    except Exception as e:  # never let one URL crash the run
        return HttpResult(None, url, "", b"", "%s: %s" % (type(e).__name__, e))


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
    outcome = "error" if (r.status is None or r.status >= 500 or r.status == 429) else "denied"
    attempts.append(_attempt(layer, url, outcome, r.status, "http %s" % r.status))
    return None


def _attempt(layer: str, url: str, outcome: str, http_status: Optional[int], note: str) -> dict:
    return {
        "layer": layer,
        "url": url,
        "outcome": outcome,
        "http_status": http_status,
        "note": note,
    }


def _record_pdf_attempt(r: HttpResult, layer: str, url: str, attempts: List[dict]) -> Optional[bytes]:
    """Turn a raw HttpResult into a PDF (bytes) or an attempt record. Never trusts
    Content-Type; validates the %PDF magic."""
    if r.error is not None:
        attempts.append(_attempt(layer, url, "error", None, r.error[:200]))
        return None
    if r.status is not None and 200 <= r.status < 300:
        if len(r.body) > MAX_DOWNLOAD_BYTES:
            attempts.append(_attempt(layer, url, "error", r.status, "exceeds_50mb"))
            return None
        if _is_pdf_bytes(r.body):
            attempts.append(_attempt(layer, url, "ok", r.status, "pdf %d bytes" % len(r.body)))
            return r.body
        note = "not_pdf"
        if r.content_type:
            note += " (%s)" % r.content_type.split(";")[0][:40]
        if _looks_like_challenge(r.body):
            note += " challenge"
        attempts.append(_attempt(layer, url, "denied", r.status, note))
        return None
    # non-2xx
    outcome = "error" if (r.status is None or r.status >= 500 or r.status == 429) else "denied"
    attempts.append(_attempt(layer, url, outcome, r.status, "http %s" % r.status))
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


# ─────────────────────────── fetch waterfall ───────────────────────────
def _run_waterfall(paper: dict, config: Config, attempts: List[dict]) -> Tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (pdf_bytes, layer, url) on the first hit, else (None, None, None)."""
    doi = (paper.get("doi") or "").strip() or None
    oa_url = (paper.get("oa_url") or "").strip() or None
    paper_id = str(paper.get("id") or "")
    arxiv_id = _normalize_arxiv_id(paper.get("arxiv_id"))
    if not arxiv_id and paper_id.lower().startswith("arxiv:"):
        arxiv_id = _normalize_arxiv_id(paper_id)

    challenge_landing: Optional[str] = None  # for L6

    # ── L0 arXiv ──────────────────────────────────────────────
    if arxiv_id:
        for base in ("https://arxiv.org/pdf/", "https://export.arxiv.org/pdf/"):
            url = base + urllib.parse.quote(arxiv_id, safe="/.")
            pdf = _record_pdf_attempt(_http_get(url, config), "L0_arxiv", url, attempts)
            if pdf:
                return pdf, "L0_arxiv", url

    # ── L1 oa_url ─────────────────────────────────────────────
    if oa_url:
        r = _http_get(oa_url, config)
        pdf = _record_pdf_attempt(r, "L1_oa_url", oa_url, attempts)
        if pdf:
            return pdf, "L1_oa_url", oa_url
        # returned HTML → chase citation_pdf_url one hop
        if r.error is None and r.status and 200 <= r.status < 300 and r.body:
            cu = _extract_citation_pdf_url(r.body, r.final_url)
            if cu and cu != oa_url:
                url2 = cu
                pdf = _record_pdf_attempt(_http_get(url2, config), "L1_oa_url_meta", url2, attempts)
                if pdf:
                    return pdf, "L1_oa_url_meta", url2

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
                    attempts.append(_attempt("L2_unpaywall", api, "denied", 200,
                                             "no oa pdf location in unpaywall"))
                for u in candidates:
                    pdf = _record_pdf_attempt(_http_get(u, config), "L2_unpaywall", u, attempts)
                    if pdf:
                        return pdf, "L2_unpaywall", u

    # ── L3 Europe PMC (via NCBI/PMC ID Converter) ─────────────
    if doi:
        # NOTE: the classic idconv host (www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0)
        # now 301-redirects to the pmc.ncbi.nlm.nih.gov API below; hit it directly.
        conv = ("https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/?ids=%s&format=json"
                "&tool=paperdaily-deep-research&email=%s" % (
                    urllib.parse.quote(doi, safe=""),
                    urllib.parse.quote(config.email or "anonymous@example.org")))
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
                    return pdf, "L3_pmc", url
        elif isinstance(data, dict):
            attempts.append(_attempt("L3_pmc", conv, "denied", 200, "no PMCID for doi"))

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
                    return pdf, "L4_elsevier", url
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
                    return pdf, "L4_wiley", url

    # ── L5 institutional (opt-in) ─────────────────────────────
    if config.institutional:
        if doi:
            tmpl = _institutional_template_url(doi)
            if tmpl:
                pdf = _record_pdf_attempt(_http_get(tmpl, config), "L5_template", tmpl, attempts)
                if pdf:
                    return pdf, "L5_template", tmpl
            # step ②: resolve doi.org landing → citation_pdf_url
            landing = "https://doi.org/%s" % urllib.parse.quote(doi, safe="/")
            r = _http_get(landing, config, accept="text/html,application/pdf,*/*")
            if r.error is None and r.status and 200 <= r.status < 300 and r.body:
                if _is_pdf_bytes(r.body):  # some resolvers land straight on a PDF
                    attempts.append(_attempt("L5_landing", r.final_url, "ok", r.status,
                                             "pdf %d bytes" % len(r.body)))
                    return r.body, "L5_landing", r.final_url
                if _looks_like_challenge(r.body):
                    challenge_landing = r.final_url
                cu = _extract_citation_pdf_url(r.body, r.final_url)
                if cu:
                    pdf = _record_pdf_attempt(_http_get(cu, config), "L5_landing", cu, attempts)
                    if pdf:
                        return pdf, "L5_landing", cu
                    if not challenge_landing:
                        challenge_landing = r.final_url
                else:
                    attempts.append(_attempt("L5_landing", r.final_url, "denied", r.status,
                                             "no citation_pdf_url on landing page"))
            else:
                outcome = "error" if (r.error or r.status is None or r.status >= 500) else "denied"
                attempts.append(_attempt("L5_landing", landing, outcome, r.status,
                                         (r.error or "http %s" % r.status)[:120]))
                challenge_landing = r.final_url or landing
        else:
            attempts.append(_attempt("L5_institutional", "", "skipped_no_config", None,
                                     "no doi to route"))
    elif doi or oa_url:
        attempts.append(_attempt("L5_institutional", "", "skipped_optin", None,
                                 "set PD_FETCH_INSTITUTIONAL=1 (only on an authorised network)"))

    # ── L6 headless browser (opt-in; only useful after an L5 challenge) ──
    if config.browser:
        target = challenge_landing or (("https://doi.org/%s" % doi) if doi else oa_url)
        if config.institutional and target:
            pdf, url = _browser_fetch(target, config, attempts)
            if pdf:
                return pdf, "L6_browser", url
        elif not config.institutional:
            attempts.append(_attempt("L6_browser", "", "skipped_optin", None,
                                     "L6 needs PD_FETCH_INSTITUTIONAL=1 too"))
    elif (challenge_landing) and (doi or oa_url):
        attempts.append(_attempt("L6_browser", "", "skipped_optin", None,
                                 "landing needs rendering; set PD_FETCH_BROWSER=1"))

    return None, None, None


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
    rel = os.path.relpath(fpath, os.path.dirname(os.path.abspath(out_dir.rstrip("/"))) or ".")

    record: Dict[str, Any] = {
        "id": paper_id, "title": title, "status": None, "layer": None,
        "url": None, "file": None, "bytes": None, "attempts": [],
    }

    # ── idempotency: existing valid PDF ──
    if os.path.isfile(fpath):
        try:
            with open(fpath, "rb") as fh:
                head = fh.read(1024)
            if _is_pdf_bytes(head):
                sz = os.path.getsize(fpath)
                record.update(status="already", file=fname, bytes=sz)
                return record
        except OSError:
            pass  # unreadable → re-fetch

    attempts: List[dict] = []
    pdf, layer, url = _run_waterfall(paper, config, attempts)
    record["attempts"] = attempts

    if pdf:
        os.makedirs(out_dir, exist_ok=True)
        tmp = fpath + ".part"
        with open(tmp, "wb") as fh:
            fh.write(pdf)
        os.replace(tmp, fpath)
        record.update(status="ok", layer=layer, url=url, file=fname, bytes=len(pdf))
    else:
        record.update(status="failed")
        doi = (paper.get("doi") or "").strip()
        if doi:
            record["manual_url"] = "https://doi.org/%s" % doi
        elif (paper.get("oa_url") or "").strip():
            record["manual_url"] = paper["oa_url"].strip()
    return record


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

    fetched = already = failed = 0
    n = len(papers)
    with open(report_path, "w", encoding="utf-8") as rep:
        for i, paper in enumerate(papers, 1):
            rec = fetch_one(paper, out_dir, config)
            rep.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rep.flush()
            status = rec["status"]
            if status == "ok":
                fetched += 1
            elif status == "already":
                already += 1
            else:
                failed += 1
            sys.stderr.write("[%d/%d] %-8s %s%s\n" % (
                i, n, status, rec["id"],
                (" via %s" % rec["layer"]) if rec.get("layer") else ""))

    sys.stderr.write("fetched %d / already %d / failed %d of %d\n" %
                     (fetched, already, failed, n))
    sys.stderr.write("report: %s\n" % report_path)
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
