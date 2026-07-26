# Security policy

## Reporting a vulnerability

Email **security@paperdaily.org** (or open a GitHub **private** security
advisory on this repository). Please do **not** open a public issue for a
suspected vulnerability.

Include: what you did, what happened, and what you expected. A minimal
reproduction beats a long description. Do not include real API keys,
cookies, or someone else's personal data in the report — redact them, we
can reproduce from the shape.

We aim to acknowledge within 3 working days. If a fix ships, the entry
lands in `paperdaily/references/v1-known-issues.md` in the
symptom → affected versions → workaround → status form, without
server-side internals.

## Scope

In scope: the scripts and skill definitions in this repository — anything
that mishandles your credentials, sends your data somewhere unexpected, or
executes content it fetched.

Out of scope for this repository (report to the operator of the instance
you use): the paperdaily server itself, its web UI, and its deployment.

## What these skills do with your credentials

Worth knowing before you audit — these are deliberate properties, not
accidents:

- **API key (`pd_live_…`)** is read from `~/.paperdaily-cli/env` and sent
  only as an `Authorization: Bearer` header to your configured `PD_BASE`.
  Upload refuses to follow redirects, so the key never crosses to another
  origin; plaintext `http://` is refused for public hosts (allowed for
  loopback / private addresses so self-hosted instances still work).
- **Publisher TDM keys** (`ELSEVIER_TDM_KEY`, `WILEY_TDM_TOKEN`) go to
  that publisher's API only. On any cross-origin redirect they are
  stripped from the request and the event is recorded in
  `fetch_report.jsonl` as `cred_stripped_on_redirect`.
- **`UNPAYWALL_EMAIL`** goes only to polite-pool APIs that require a
  contact address (Unpaywall, Crossref, NCBI-PMC, EBI). It is never put
  in the `User-Agent`, so publishers, CDNs and landing pages harvested
  from third-party HTML do not receive it; it is masked out of URLs
  written to `fetch_report.jsonl`.
- **Fetched PDFs never leave your machine.** Upload sends only your own
  derived analysis (notes, claims ledger, report), and only after you
  explicitly agree.
- **Only `http`/`https` URLs are ever fetched** — a worklist row or page
  metadata pointing at `file://` is refused rather than read.

## Keeping your own data out of git

Running the deep-research skill writes `pd-research/<slug>/` next to your
working directory: PDFs, private notes, and research questions. The
shipped `.gitignore` covers it, but if you copy the scripts elsewhere,
carry that ignore rule with them.
