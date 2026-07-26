# paperdaily `/api/v1/*` — known issues (user-facing)

What an agent or CLI user can observe, plus the workaround. Server-side
root causes, table/query internals and post-mortems are **not** in this
file — they live in the paperdaily server's own issue register, and the
detail is of no use to a client anyway.

Format per entry: symptom you can see → affected versions → workaround →
status. Stable letter ids (A→P); entries are never silently renumbered.

Found something new? Follow SKILL.md "Bug reporting protocol" — notes go
to `~/.paperdaily-cli/bug-reports/`, redacted, never into this file.
Suspected **security** problem → `SECURITY.md`, private channel, not a
public issue.

## Open

### D — citations / authors sparse for non-`W…` paper ids

**Symptom.** `GET /papers/{id}/citations` returns `items: []` and
`GET /papers/{id}/authors` returns 404 for `arxiv:…` ids and journal-stub
ids. The same calls work on OpenAlex `W…` ids.

**Affected.** All versions to date.

**Workaround.** For authors, ask the detail endpoint instead:
`GET /papers/{id}?include=authors_full`. For citations on a freshly
ingested paper, retry later — that part of the graph is filled
asynchronously.

**Status.** Open, and it is a data state rather than a request bug. An
empty citation list on a paper published this week is expected; don't
report it.

## Closed

### P — `GET /papers?q=` missed papers that plain listing returned

**Symptom.** Searching a distinctive fragment of a paper's own title
returned nothing, while listing the same corpus slice showed that paper.
Broad scopes without a year filter could also time out.

**Affected.** Server < 0.8.0.

**Status.** Fixed in 0.8.0 — `q=` now runs against the same search
projection the web UI uses. Prefer `GET /papers/search?q=&mode=auto`,
which walks identifier → title → semantic and tells you which layer
matched.

### G — `POST /ask` rejected long questions

**Symptom.** Questions past a few thousand characters were refused.

**Affected.** Server < 0.6.x.

**Status.** Fixed; the ceiling is far higher and the error message states
it. Long questions still cost tokens — trim for cost, not for the limit.

### A — `/me/profile` answered without authentication

**Symptom.** The endpoint returned data with no valid key.

**Affected.** Server < 0.5.1.

**Status.** Fixed 2026-05-22. Every `/me/*` endpoint now requires a key
carrying the matching scope. On an older self-hosted build, upgrade —
this is the one worth upgrading for.

### B — `/papers/by-doi/{doi}` and `/papers/by-arxiv/{id}` returned 500

**Symptom.** 500 instead of a paper or a clean 404.

**Affected.** Server < 0.5.1.

**Status.** Fixed. Both are superseded by `GET /papers/resolve?id=…`,
which accepts any identifier shape and reports `resolved_by` — prefer it
in new code.

## Withdrawn (misdiagnosis, kept so the ids stay stable)

- **C** — "`/papers/{id}/authors` 404s for every paper": it 404s only for
  id types that have no author edges; see D.
- **E** — "`/authors?q=` returns null author_id for every row": the field
  is `id`, not `author_id`.
- **F** — "`find_papers_by_authors` returns unrelated papers": upstream
  author attribution is imperfect for common names; disambiguate to one
  `A…` id first.
