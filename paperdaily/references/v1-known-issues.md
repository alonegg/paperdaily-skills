# paperdaily `/api/v1/*` — known issues (skill-side mirror)

Skill-side mirror of the canonical issue register maintained by the paperdaily server team.

Letter ids are stable; entries are never silently renumbered.

## Open

### P — `GET /papers?q=` title search misses papers plain listing returns

The `q=` title search under-recalls badly: papers that a plain (no-`q`)
listing in the same scope returns are not found when you add a `q=` that
matches their title. Not a data-state issue — the papers ARE in the
corpus and reachable by direct id and by `/ask` semantic retrieval.

Repro (2026-07-20, scope `subfield_id=1702` = Artificial Intelligence):

| request | result |
|---|---|
| `?subfield_id=1702&year_from=2026` (no q, plain list) | OK — `ssrn:7123198` is row 2 |
| `?q=Harnessing LLMs&subfield_id=1702&year_from=2026` | **0 hits** — yet `ssrn:7123198`'s title starts "Harnessing LLMs…" and is in scope |
| `?q=Large Language Models&subfield_id=1702&year_from=2026` | **1 hit only** — while `_count` for that scope = 98,322 |
| `?q=Harnessing&subfield_id=1702` (drop year filter) | 60s timeout, 0 bytes |
| direct `GET /papers/ssrn:7123198` | 200, full detail |
| `POST /ask` semantic route | found it (`find_papers_by_keyword_semantic`) |

So within one scope, a paper the listing enumerates cannot be retrieved
by a `q=` matching its own title. The `q=` path also requires a
topic/subfield/field scope (400 otherwise) and times out on broad
scopes without a year filter.

Same-day repro on a second title: searching for "A Comprehensive Review
of Large Language Models: Taxonomy, Architectures, Data, Adaptation, and
Evaluation (2013-2026)" (`ssrn:7063638`, pub 2026-07-16) returned 0 hits
across every title-substring variant tried; the paper was only located
via `/ask` semantic retrieval, then confirmed by direct id fetch.

**Workarounds** (both used above):
- Locate by id you already have: `GET /papers/{id}`.
- Discover by content: `POST /ask` with an instruction to use
  `find_papers_by_keyword_semantic`, then confirm with a direct id fetch.

Backend fix is out of scope for this read-only skill; report new repros
upstream instead.

### D — citation graph and AUTHORED edges sparse for non-W papers

Not a code bug, a data-state observation. Two related symptoms:

- `GET /papers/{id}/citations` returns `items: []` for freshly-ingested
  arxiv and most W- papers. Older W- papers (e.g. `W3197929691`, 2021)
  populate. Backfill is async per-paper.
- `GET /papers/{id}/authors` returns 404 for `arxiv:…` paper ids and
  journal-stub ids (`tpds.2026.3666309`-style). It works correctly on
  W- papers that have AUTHORED edges populated by the OpenAlex ingest
  (e.g. `W4416083181` returns 20 authors with id/orcid/position). For
  paper id types that the ETL doesn't write AUTHORED edges for, the
  workaround is `GET /papers/{id}?include=authors_full` which reads
  from the relational `paper_authors` table instead.

Tracking the AUTHORED-edge backfill for non-W paper types separately
from this issue list.

## Closed (fixed 2026-05-22)

### G — `POST /ask` `question` was hard-capped at 4000 characters

Pydantic `AskRequest.question` had `max_length=4000`. Tripped when
packing 24 paper-id batches plus context into one prompt during the
blockchain timeline scenario. Raised to `max_length=32000` (~8k tokens
of question, still well under the internal LLM's input budget after
system prompt + tools + retrieval). Sanity-tested with a 5513-char
question → 200 OK + answer.

### A — `/me/profile` was unauthenticated

Route was bound to the raw `build_profile_snapshot(user_id: str)`
helper; no `Depends(...)` meant any bearer key could read any
user_id's profile. Fixed by binding to `get_profile(identity)`.

### B — `/papers/by-doi/{doi}` and `/papers/by-arxiv/{id}` both 500

Handlers called `get_paper(row["external_id"], identity)`, but
`get_paper(paper_id, include, identity)` has `include` in second
position — so `identity` was treated as `include` and
`identity.split(",")` raised. Fixed by passing `include=None,
identity=identity` as keyword args.

## Withdrawn (was misdiagnosis, not a bug)

### C (withdrawn) — "/papers/{id}/authors returns 404 for every paper"

The original repro was on 5 paper ids spanning `arxiv:`, `W…`, and
journal-stub formats — all returned 404. Subsequent test on `W4416083181`
returned the full 20-author list with ids, ORCIDs, and positions. The
v1 handler is correct; AUTHORED edges just aren't populated in AGE for
`arxiv:` and journal-stub paper id types. Tracking this as data-state
issue D above.

### E (withdrawn) — "`/authors?q=` returns null author_id for every row"

Misdiagnosis caused by the original CLI script using the wrong jq key
(`author_id` instead of `id`). The API returns a non-nullable `id`
field with real A-IDs (e.g. `A5086198262` for the famous Yoshua Bengio).
Verified by direct curl + raw response inspection.

### F (withdrawn) — "find_papers_by_authors returns unrelated papers"

The LLM tool's `find_papers_by_authors` uses the same `(a:Author)-[:AUTHORED]->(p:Paper)`
Cypher as the REST endpoint. The returned papers really are linked to
the given A-IDs in OpenAlex — but OpenAlex's per-author paper
attribution is imperfect, so the famous Bengio's A-id (A5086198262)
links to only 55 papers spanning HVAC datasets, antibacterial compounds,
Li-ion cathodes, and the consciousness paper, instead of his deep-learning
canon. Not a paperdaily bug.
