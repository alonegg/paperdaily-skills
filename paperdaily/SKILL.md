---
name: paperdaily
description: Query the user's paperdaily research-paper service from the shell — fast, read-only lookups that answer in one shot. Use when the user wants the latest academic papers in a research field (e.g. "AI 今天有什么新论文", "show me recent finance papers"), asks about a specific researcher's recent work (e.g. "Bengio 最近发了啥"), wants paper recommendations with related-paper expansion, or asks for an LLM-synthesized digest of a research area. Not for deep reading: if the user wants the papers actually downloaded and read — 深度调研 / 精读 / a literature review with page-anchored citations, or "深读这篇论文" — use the `paperdaily-deep-research` skill instead, which runs the full fetch-and-read pipeline. Routes through ~/.paperdaily-cli/env (PD_BASE + PD_KEY). Query scenarios are read-only; the watchlist scenario writes (create/delete/run) and every write asks the user first.
---

# paperdaily skill

> **声明块（SKILL_SPEC §1）**
> - **skill_ver**: `0.2.0`
> - **协议版本**: AGENT_PROTOCOL v1
> - **所需 scopes**: `read:digest, read:paper, synth:ask`（默认，只读）；
>   读用户收藏库（`GET /me/saves`）另需 `read:contrib`（个人数据，
>   刻意不并入人手一份的 `read:paper`）；
>   scenario 3 watchlist 的 create/delete/run 另需 `write:profile`，
>   **按需索取、每次动作前显式确认**

Thin wrapper over the paperdaily v1 Bearer API.

**Read vs write (read this before acting).** Three scenarios are pure
queries — field digest, author papers, topic find. The fourth, watchlist,
**mutates server state**: `create` and `delete` change the user's saved
queries, and `run` triggers a scan that writes `watchlist_entries` and
spends quota. Per `SKILL_SPEC.md` rule 4 every one of those three needs an
explicit user OK first — "run is basically read-only" is not a valid excuse
(it was written that way once; it was wrong).

## Pre-flight (do once per environment)

The skill expects `~/.paperdaily-cli/env` with:

```sh
export PD_BASE="${PD_BASE:-https://www.paperdaily.org/api/v1}"
export PD_KEY="pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

If the file is missing, **stop and tell the user to issue a key first**
(web UI → Settings → API keys → Issue key). Ask for the **read-only** set
by default:

```
read:digest,read:paper,synth:ask
```

要读「用户收藏了哪些论文」再加 `read:contrib`。**收藏库只能从
`GET /me/saves` 读**——不要拿 `GET /me/feedback`（行为流水）反推：流水只记
经反馈路径产生的动作，用户在库页直接点收藏的论文根本不在里面，反推出来的
清单会系统性偏少（实地案例：库里 20 篇、反推只得 6 篇）。旧 key 没勾
`read:contrib` 会 403，让用户重签一把。

`write:profile` is **not** part of the default ask — request it
just-in-time, only when the user actually wants to create/delete/run a
watchlist, and say why. A key that can only read cannot be talked into
writing. Don't try to mint a key here — that needs a cookie session, this
skill is bearer-only.

Verify reachability with a one-shot smoke before doing anything heavy:

```sh
source ~/.paperdaily-cli/env
curl -sS -o /dev/null -w "%{http_code}\n" "$PD_BASE/openapi.json"   # expect 200
```

Use `https://www.paperdaily.org/api/v1` unless you run your own instance.
If every call comes back as a 302 to a login page, the deployment has an
access proxy in front of `/api/v1/*` — that's a server-side setting, ask
the operator.

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
- with `--no-synth`: pure REST, sub-second on internal endpoint, no
  token cost.

If the user asks for multiple fields in one session, default to `--no-synth`
for all but the most important one to stay under the rate cap.

### 3 — Watchlists (user-defined weekly topic tracking) — **WRITE scenario**

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
  with a scoring rubric (rubric version is returned by the API).
- The weekly cron (Mon 08:00 Asia/Shanghai) scans all active watchlists,
  scores each candidate paper into high/med/low threat tier per the
  rubric, writes `watchlist_entries`, and emails a weekly digest.
- Server-side cap: ≤5 watchlists per user.

Hard rules:
- **`create`, `delete` AND `run` all need an explicit user OK before the
  call** — they need `write:profile` and each one changes server state.
  `run` is not an exception: it writes `watchlist_entries` and consumes
  scan quota. (An earlier version of this file called `run` "mostly
  safe". That was wrong and contradicted `SKILL_SPEC.md` rule 4.)
- `list`, `show` and `entries` are pure reads — no confirmation needed.
- If the key lacks `write:profile`, do NOT push the user to re-issue a
  broader key mid-flow as if it were a bug: say which action needs it,
  and let them decide.
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

- **Mutate the user's interest profile** (follow topic, follow author,
  post feedback). Those `POST /me/*` calls also sit under `write:profile`,
  but this skill ships no helper for them. If the user explicitly says
  "follow this topic for me", run the call directly with `curl` after
  confirming — do NOT add a write helper here. (Watchlist management is
  the one write surface this skill does implement; see scenario 3.)
- **Manage API keys.** Issuance / revocation goes through the cookie
  session at `/api/me/keys`. Tell the user to use the web UI.
- **Touch the cookie-session `/api/*` paths.** Different auth, different
  rate caps, different contract. This skill is v1-only.
- **Render to anything other than markdown.** Output is stream-friendly
  markdown the user can paste into a notebook or read in-terminal.

## Bug reporting protocol

When a scenario surfaces something that looks like an API bug:

1. **Don't fix it.** Backend fixes live in the paperdaily server, which
   is not part of this package. Don't go hunting for that tree unless
   the user asks for a fix.
2. **Record the symptom — outside the install directory.** Append to
   `~/.paperdaily-cli/bug-reports/<YYYY-MM-DD>.md`, creating it if
   needed. **Never write into this skill's own directory**: it is a
   shared/installed package (often a git checkout), and notes written
   there get committed by accident.
3. **Redact before writing.** Record the request path, the HTTP code,
   the affected id(s), and a ≤200-char body excerpt — with the
   following stripped: `Authorization` / `Cookie` headers, any
   `pd_live_…` string, email addresses, and any user-private research
   content. If you cannot redact it confidently, describe it instead of
   pasting it.
4. **Surface to the user.** Tell them what you saw and where you wrote
   it. Don't bury it in the output. For a suspected *security* problem,
   stop and follow `SECURITY.md` (private report) rather than writing a
   public issue.

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
  topic-tracking jobs. Mutates state — see the
  hard rules in scenario 3.
- `references/v1-known-issues.md` — user-visible issues + workarounds, by
  stable letter id.
