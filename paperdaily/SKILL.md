---
name: paperdaily
description: Query the user's paperdaily research-paper service from the shell. Use when the user wants the latest academic papers in a research field (e.g. "AI 今天有什么新论文", "show me recent finance papers"), asks about a specific researcher's recent work (e.g. "Bengio 最近发了啥"), wants paper recommendations with related-paper expansion, or asks for an LLM-synthesized digest of a research area. Routes through ~/.paperdaily-cli/env (PD_BASE + PD_KEY); read-only against /api/v1, will never modify the user's profile (follows/feedback) without explicit re-confirmation.
---

# paperdaily skill

Thin wrapper over the paperdaily v1 Bearer API. Two end-user scenarios
implemented; both are read-only.

## Pre-flight (do once per environment)

The skill expects `~/.paperdaily-cli/env` with:

```sh
export PD_BASE="${PD_BASE:-https://www.paperdaily.org/api/v1}"
export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # placeholder — issue yours at /account
```

If the file is missing, **stop and tell the user to issue a key first**
(web UI → Settings → API keys → Issue key, scopes
`read:digest,read:paper,write:profile,synth:ask`). Don't try to mint
one — that needs cookie session, this skill is bearer-only.

Verify reachability with a one-shot smoke before doing anything heavy:

```sh
source ~/.paperdaily-cli/env
curl -sS -o /dev/null -w "%{http_code}\n" "$PD_BASE/openapi.json"   # expect 200
```

`PD_BASE` should point at the public host:
`https://www.paperdaily.org/api/v1`.

## Scenarios

### 1 — Field / Subfield / Topic digest

The user says something like "今天 AI 有什么新论文", "show me recent
blockchain papers", "what's interesting in Computer Vision this week".

```sh
~/.claude/skills/paperdaily/scenarios/field-digest.sh "Artificial Intelligence" --limit 5 --similar 3
~/.claude/skills/paperdaily/scenarios/field-digest.sh 1702                       # subfield id
~/.claude/skills/paperdaily/scenarios/field-digest.sh T10270 --year-from 2024    # topic id directly
~/.claude/skills/paperdaily/scenarios/field-digest.sh blockchain --year-from 2024  # topic name (slow path)
~/.claude/skills/paperdaily/scenarios/field-digest.sh "Finance" --limit 3 --no-synth   # skip /ask
```

What it does:

1. Resolves the first arg by shape:
   - `T\d+` → Topic id (direct)
   - 1-2 digit numeric → Field id
   - 4 digit numeric → Subfield id
   - exact case-insensitive name → Field or Subfield (in that order)
   - substring → Subfield substring (sorted by paper_count desc)
   - **last resort** → calls `topic-find.sh` to walk all subfields' topics
     in parallel (~3-15s; can hit the `read:paper` 60/min cap and lose
     a few subfields to 429, but usually finds a hit). Picks the
     highest-paper-count match.

   For niche or noisy topic names, prefer calling `topic-find.sh` first,
   pick a Topic id, then re-invoke field-digest with the T-id. Faster
   and lets the user pick among multiple matches.
2. `GET /papers?{level}_id=…&year_from=…&has_extraction=true&limit=N` —
   the recency window defaults to current year; the `has_extraction`
   filter keeps only papers the LLM-extraction pipeline has finished.
3. For each paper: `GET /papers/{id}` for detail, `GET /papers/{id}/similar?k=K`
   for cosine neighbours (SPECTER2 / BGE-M3).
4. **One** `POST /ask` call at the end, instructed to use
   `load_extractions(paper_ids=[…the listed ids…])` — this is the trick
   that makes the synthesis actually about the listed papers rather than
   whatever `find_papers_by_keyword_semantic` happens to return for the
   field name (it has no year filter and tends to surface 2018-2022 surveys).
5. Output: a single markdown document. Stream it to the user verbatim;
   no further re-formatting needed.

Flags: `--limit N` (default 10), `--year-from YYYY` (default current year),
`--similar K` (default 3), `--no-extraction` (relax the `has_extraction`
filter), `--no-synth` (skip the /ask call to save ~3-12k LLM tokens).

Cost guidance:

- without `--no-synth`: 1 `/ask` call, typically ~3-12k input + ~500-1k
  output tokens. Free tier rate cap is 2/min so back-to-back invocations
  may 429.
- with `--no-synth`: pure REST, typically sub-second per call, no
  token cost.

If the user asks for multiple fields in one session, default to `--no-synth`
for all but the most important one to stay under the rate cap.

### 3 — Watchlists (user-defined weekly topic tracking)

The user says "建一个 watchlist 追 blockchain governance 论文", "看看
我的 watchlist 有什么新的", "停掉那个 GAN watchlist".

```sh
~/.claude/skills/paperdaily/scenarios/watchlist.sh list
~/.claude/skills/paperdaily/scenarios/watchlist.sh create \
  --name "blockchain governance" \
  --terms "DAO,governance,voting" \
  --arxiv "cs.CR,cs.CY" \
  --anchors "W4416083181,arxiv:2604.25959"
~/.claude/skills/paperdaily/scenarios/watchlist.sh run 1     # manual scan
~/.claude/skills/paperdaily/scenarios/watchlist.sh entries 1 --threat high
~/.claude/skills/paperdaily/scenarios/watchlist.sh delete 1
```

What it does:

- Stores a saved query (terms ∪ arxiv categories ∪ anchor paper neighbors)
  with a scoring rubric (default v0.1).
- The weekly cron (Mon 08:00 Asia/Shanghai) scans all active watchlists,
  scores each candidate paper into high/med/low threat tier per the
  rubric, writes `watchlist_entries`, and emails a weekly digest.
- v0.1 author-dogfood only; v0.2 will GA at ≤5 watchlists/user.

Hard rules:
- This scenario uses `write:profile` scope, but it CREATES watchlists
  (mutates state). Confirm with the user before calling `create` or
  `delete`. `run` is mostly safe (read-only retrieval + write to
  watchlist_entries which is an append-only hit ledger).
- `run` is synchronous and can take 10-60s. Show a spinner if rendering
  to a TTY; agents should treat it as a long-poll call.
- `anchor_paper_ids` need to be **W- or arxiv: paper IDs that have a
  SPECTER2 embedding**. Most recent W- papers do; arxiv-only papers may
  not yet. If retrieval returns 0 candidates, suspect anchor mismatch.

### 2 — Author's recent papers

The user says "Bengio 最近发了啥", "show me recent papers by Donoho",
"what is X working on lately".

```sh
~/.claude/skills/paperdaily/scenarios/author-papers.sh "Yoshua Bengio"
~/.claude/skills/paperdaily/scenarios/author-papers.sh "Donoho" --limit 5 --no-synth
```

What it does:

1. `GET /authors?q=<name>` — name lookup. Returns rows with real A-IDs
   (`id` field, e.g. `A5086198262`), sorted by `paper_count` desc.
   Common names like "Yoshua Bengio" will surface 3-5 distinct people.
2. `GET /authors/{top-id}/papers?limit=N` — recent papers for the
   most-prolific match. Add `--all-matches` to also fetch papers for
   each runner-up (different people sharing the name).
3. (optional) `POST /ask` only if `--synth`: LLM narrative summary
   built via `load_extractions([those paper ids])`.

**OpenAlex data-quality caveat.** An A-id's paper_count and linked
papers come from OpenAlex's per-author attribution, which is imperfect.
The famous Bengio (A5086198262, paper_count=55) currently links to
HVAC datasets, Li-ion cathodes, and a single consciousness paper rather
than his deep-learning canon — OpenAlex hasn't fully merged his author
records. Show what's there; don't editorialize the gap unless the user
asks.

Flags: `--limit N` (default 8), `--all-matches`, `--synth`.

## What this skill does NOT do

- **Mutate the user's profile** (follow topic, follow author, post
  feedback). All `POST /me/*` calls require `write:profile` scope which
  the skill does not exercise. If the user explicitly says "follow this
  topic for me", run the command directly with `curl` and a manual scope
  check — do NOT add a write helper to this skill.
- **Manage API keys.** Issuance / revocation goes through the cookie
  session at `/api/me/keys`. Tell the user to use the web UI.
- **Touch the cookie-session `/api/*` paths.** Different auth, different
  rate caps, different contract. This skill is v1-only.
- **Render to anything other than markdown.** Output is stream-friendly
  markdown the user can paste into a notebook or read in-terminal.

## Bug reporting protocol

When a scenario surfaces something that looks like an API bug:

1. **Don't fix it.** This skill is read-only; backend fixes live in the
   paperdaily server, which is not part of this repository.
2. **Record the symptom.** Capture: request URL, HTTP code, response
   body excerpt, repro count (how many paper_ids / author names this
   pattern affects). Add to `references/v1-known-issues.md` as a new
   entry with a stable letter id (current taxonomy goes A→F).
3. **Surface to the user.** Tell them you saw the bug and where it's
   tracked. Don't bury it in the output.

Currently tracked open issue:

- **D — AUTHORED + CITES edges sparse for non-W papers.** `/papers/{id}/authors`
  and `/papers/{id}/citations` return empty/404 for `arxiv:…` and journal-stub
  paper id types because the ETL only writes those edges for OpenAlex W- papers.
  Workaround for authors: `?include=authors_full` on the detail call (reads
  the relational `paper_authors` table). Don't surface this as a bug to the
  user — it's a known data state.

## Files

- `_lib.sh` — `pd_get` / `pd_post` / `pd_delete` curl wrappers (sourced
  by every scenario). Loads `~/.paperdaily-cli/env` if `$PD_KEY` is unset.
- `scenarios/field-digest.sh` — scenario 1 (Field / Subfield / Topic).
- `scenarios/author-papers.sh` — scenario 2.
- `scenarios/topic-find.sh` — substring search across all Topics
  (helper used by field-digest's slow path; also runnable standalone
  to discover Topic ids).
- `scenarios/watchlist.sh` — CRUD + run-now for user-defined weekly
  topic-tracking jobs. Mutates state — see the hard rules in scenario 3.
- `references/v1-known-issues.md` — bugs we've seen, by stable letter id.
