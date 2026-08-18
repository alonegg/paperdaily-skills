---
name: paperdaily
description: >-
  Query the user's paperdaily research-paper service from the shell — fast,
  read-only lookups that answer in one shot. Use when the user wants the latest
  academic papers in a research field (for example "AI 今天有什么新论文" or
  "show me recent finance papers"), asks about a specific researcher's recent
  work (for example "Bengio 最近发了啥"), wants semantic search over a fuzzy
  research direction that is not a taxonomy node, wants papers similar to one or
  more given seed papers, or asks for an LLM-synthesized digest of a research
  area. Not for deep reading — if the user wants the papers actually downloaded
  and read — 深度调研 / 精读 / a literature review with page-anchored citations,
  or "深读这篇论文" — use the paperdaily-deep-research skill instead, which runs
  the full fetch-and-read pipeline. Routes through ~/.paperdaily-cli/env
  (PD_BASE plus PD_KEY). Query scenarios are read-only; the watchlist scenario
  writes (create/delete/run) and every write asks the user first.
---

# paperdaily skill

> **声明块（SKILL_SPEC §1）**
> - **skill_ver**: `0.3.0`
> - **协议版本**: AGENT_PROTOCOL v1
> - **所需 scopes**: `read:digest, read:paper, synth:ask`（默认，只读）；
>   读用户收藏库（`GET /me/saves`）另需 `read:contrib`（个人数据，
>   刻意不并入人手一份的 `read:paper`）；
>   scenario 3 watchlist 的 create/delete/run 另需 `write:profile`，
>   **按需索取、每次动作前显式确认**

Thin wrapper over the paperdaily v1 Bearer API.

**Read vs write (read this before acting).** Four things here are pure
queries — field digest, author papers, topic find, semantic search. The one
write surface is watchlist (scenario 3), which **mutates server state**:
`create` and `delete` change the user's saved queries, and `run` triggers a
scan that writes `watchlist_entries` and spends quota. Per `SKILL_SPEC.md`
rule 4 every one of those three needs an explicit user OK first — "run is
basically read-only" is not a valid excuse (it was written that way once; it
was wrong).

**检索选路（先看这张表再动手）.** 走错通道是这个 skill 目前最贵的错误——
既慢又召回不足，且**两种症状都不会报错**：

| 用户诉求 | 走哪个 |
|---|---|
| 学科 / 子领域 / Topic 是 taxonomy 节点 | scenario 1 `field-digest.sh` |
| 某个人最近发了什么 | scenario 2 `author-papers.sh` |
| **模糊研究方向（不是 taxonomy 节点）** | **scenario 4 `semantic-search.sh`** |
| **给了论文，要「跟这些像的」** | **scenario 4 `--paper <id>`** |
| 要一段服务端生成的综述 | scenario 1 的 `/ask`（一次工作流最多一次） |

判据与全部实测数字在 `references/semantic-search.md`。三条最容易踩的：
**① 概念型查询必须钉 `mode=semantic`**（默认的 `mode=auto` 会先跑标题层，实测
19.6s 只回 3 条，钉死后 5.1s 回 20 条）；**② 查询要写成一整句**（两词查询实测把
CoT 论文的检索拽进「教育领导力」簇）；**③ `/ask` 是合成端点不是检索端点**——
见下。

**`/ask` 的定位（别读成"agent 不许用"）.** 协议里「agent 不走 /ask」是针对**检索**
说的。它有三种正当用法：**① 合成**——你已经用结构化端点选定了论文，要一段成文的
带引用叙述给用户（必须用 `load_extractions(paper_ids=[…])` 锚定你的清单，否则它会
自己去检索，拿回一批 2018-2022 的综述当材料）；**② 够到 REST 没开的三个工具**——
`find_community_overview` / `find_papers_by_venue` / `lookup_venue`，这三件事目前
只有这一个门；**③ 把合成成本转移到服务端**（烧的是服务端 token 和积分，不是你的
上下文）。代价是 p50 37-57s、无流式、10 积分、2 req/min，所以一次工作流最多一次。

⚠️ **别为了拿抽取去调 `/ask`**：`POST /papers/batch`（≤100 id/次）直接返回
`contributions / key_claims / methods / limitations / open_questions / tldr_zh`。
15 个工具里 12 个都有 REST 门，完整对照表在 `references/semantic-search.md` §9。

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

### 4 — Semantic search（模糊方向 / 种子论文找相似）

用户说的是一个**研究方向**而不是学科名（「多智能体协作做代码审查的工作有哪些」
「有没有人做用 LLM 判作业公平性的」），或者手里已经有几篇论文要「找像的」。
这两件事共用一个脚本，因为它们的正确做法是同一套：钉死语义层 + 多通道并集 +
孪生折叠。

```sh
S=~/.claude/skills/paperdaily/scenarios/semantic-search.sh

# ① 模糊方向：先无 scope 探针，按返回的 subfield 分布自动收窄再跑一遍
"$S" "retrieval augmented generation reduces hallucination in QA" --scope auto --limit 20

# ② 要覆盖面：改写由你来写，中英文/术语白话各一条，脚本按 RRF 融合
"$S" "large language models for personalized learning" \
     --expand "LLM 个性化学习 自适应教学系统" \
     --expand "AI tutor adaptive instruction student outcomes"

# ③ 种子论文找相似：自动并 /similar 与语义两条通道，并排除种子自身与其孪生行
"$S" --paper arxiv:2201.11903 --paper W7165154520 --limit 30

# ④ 限定学科 + 时间窗；⑤ 交给下游
"$S" "text as data methods in empirical economics" --scope 2002 --year-from 2023
"$S" "graph neural networks for fraud detection" --json
```

脚本做的事：`GET /papers/search?mode=semantic`（**永远钉死，不用 auto**）
→ 可选探针定 scope → 可选多改写扇出 → 逐 seed 的 `/papers/{id}/similar` +
标题语义两通道 → RRF 融合 + 归一化标题折叠 → markdown 表或 `--json`。
纯读、0 积分、不碰 `/ask`。

读结果时按这三条判：

1. **`命中通道数 ≥ 2` 的排在最前，优先读那些**。seed 模式下 `/similar` 与语义层
   实测 20 条只重合 1 条，两条通道都投票的才是真核心件。
2. **`sem 分` 和 `sim 近度` 是两个量纲，不要互相比大小**（前者是重排后的复合分，
   0.62-0.76 算命中；后者是 1−余弦距离，0.97+ 是常态）。
3. **先看 `why` 里的 dominant cluster 是不是这个领域**——那是强信号；`sem 分`
   只是弱提示（阈值 0.56，实测 miss ≤0.542、hit ≥0.5936）。⚠️ **分数高不代表
   查询够具体**：`graph` 这种一个词的查询照样拿 0.61 且 cluster 也对。判定没打中
   就改写成更长更具体的一整句重跑，不要靠加 `--limit` 硬凑。

几个会静默坑人的事实（全部实测，详见 `references/semantic-search.md`）：

- **同一问题的多个改写往往零重叠**（实测 4 改写 × 20 篇 = 80 篇全不重复）。
  所以扇出不是冗余劳动；但也别指望「多个改写都命中」当共识信号，那个集合通常是空的。
- **单个种子的 top-30 邻域半径只有 ~0.02，而同一篇论文的两条记录相距 0.04** ——
  所以单种子 `/similar` 给的是「最贴的那一小簇」，不是「相关工作」。要宽就加种子、
  加改写。
- **`k` 加到 40 以上无效**，HNSW 的 `ef_search` 默认把返回截在 ~38 条。
- **中文诉求绝不能落到标题层**（实测标题层 0 条，语义层 20 条）。

配额：`read:paper` 60/min。扇出 + 收窄 + 多种子一次就是 10+ 请求，脚本按 1.05s
自节流并在 429 时按 `Retry-After` 退避。**第一次查询慢（冷缓存 4-25s）是正常的，
不要重试**——重跑同一个查询热态只要 0.4-1.8s。

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
- `scenarios/semantic-search.sh` — scenario 4 (模糊方向语义检索 / 种子论文
  相似扩张)。钉死 `mode=semantic`，多通道并集 + 孪生折叠。
- `references/semantic-search.md` — 语义检索手册：选路决策表、八条铁律、
  延迟与配额、五个场景的具体命令。全部数字为 2026-08-18 生产实测。
- `references/v1-known-issues.md` — user-visible issues + workarounds, by
  stable letter id.
