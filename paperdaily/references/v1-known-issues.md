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

### Q — `mode=auto` 的标题层吃掉概念型查询（又慢又少）

**Symptom.** `GET /papers/search?q=<一个研究方向>` 用默认 `mode=auto` 时，
响应 `layer_used="title"`、只有三五条字面撞词的结果，而同一查询钉
`mode=semantic` 能回 20 条切题的。即使最终仍落到语义层，也已经白付了标题层的
墙钟。实测（2026-08-18 生产）：

| q | `mode=auto` | `mode=semantic` |
|---|---|---|
| `mixture of experts routing` | title 层，19.6s / 3 条 | 5.1s / 20 条 |
| `multi agent LLM collaboration for automated code review` | 23.2s（走完标题层才落语义） | 1.65s，结果集相同 |
| `causal inference with panel data staggered adoption` | 23.6s | 1.5s |

**Affected.** 0.8.0 起全部版本。成因是瀑布的采纳阈值——标题层只要 ≥3 条命中
就被接受，语义层不再执行；而标题层是 trgm 相似度扫描，冷缓存 15-30s。

**Workaround.** 概念型/方向型查询一律显式 `mode=semantic`。只在「用户给的就是
一个完整标题」时用 `mode=title`，`mode=auto` 留给分不清输入形态的通用入口。
两个 skill 自 paperdaily 0.3.0 / deep-research 0.5.1 起已默认钉死语义层。

**Status.** **服务端已修（v0.8.93）**，客户端规避保留。两处改动：① 采纳判据由
「≥3 条命中」改为「≥3 条命中 **且** 最佳命中的 trgm 相似度 ≥0.60」——L1 的 WHERE
是整串包含（`title ILIKE '%q%'`），所以命中数分不出「用户贴了标题」和「用户描述了
一个恰好出现在几个标题里的概念」（两者都落在 3 条），相似度分得出：真标题 1.000，
`mixture of experts routing` 0.422；② 标题层加 3s 墙钟上限
（`PD_SEARCH_TITLE_TIMEOUT_MS`，置 0 恢复旧行为），auto 超时即降级到语义层，
`mode=title` 超时回 503 而不是静默空列表。**仍然建议钉 `mode=semantic`**：修复
只是把税从 20 秒降到 3 秒，钉死是一分不付。

### R — `/similar?k=` 超过 ~38 静默截断

**Symptom.** `GET /papers/{id}/similar?k=50` 只回 38 条；`k=40` 回 38，
`k=30` 正常回 30。没有任何提示。

**Affected.** 全部版本。是 HNSW `ef_search` 默认值的效果，不是语料边界。

**Workaround.** 想要更宽的邻域，加种子或并上
`GET /papers/search?q=<该篇标题>&mode=semantic`，不要加 `k`。

**Status.** **服务端已修（v0.8.93）**：响应新增 `truncated: bool`（`len(items) < k`
时为 true），语义是「索引先停了，不是语料没了」。截断本身没消除——那要动 `ef_search`，
是索引级参数，会同时改变延迟与召回，不该为一个端点单独调。

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
