# paperdaily MCP server — quickstart

`paperdaily-mcp` is a stdio Model Context Protocol server that exposes
the paperdaily v1 REST API as **32 agent tools**. Drop it into Claude
Desktop / Claude Code / Cursor / any MCP-compatible runtime and the
agent can auto-discover and call `paperdaily_get_digest_today`,
`paperdaily_get_paper`, `paperdaily_ask`, etc. directly.

It is a thin proxy: every tool call is forwarded as a typed HTTP request
with your bearer key. Accounts, scopes and rate limits are the same ones
the website uses — enforced server-side per key, nothing to configure in
the client.

## Install

From the public skills repo (recommended for MCP 0.1.1 and profile sync):

```bash
uv tool install --upgrade \
    --from 'git+https://github.com/alonegg/paperdaily-skills#subdirectory=mcp' \
    paperdaily-mcp
```

Then restart your MCP connection. `paperdaily_update_profile` should appear
among the 32 tools.

PyPI still carries 0.1.0 as checked on 2026-10-13; that version does not
include `paperdaily_update_profile`. Use the Git installation above for
natural-language profile sync. The older package remains installable with
`uv tool install paperdaily-mcp` or `pipx install paperdaily-mcp`.

Confirm the binary is reachable:

```bash
which paperdaily-mcp
```

## Get your API key

Sign in at <https://www.paperdaily.org>, then go to **账号设置 → API
Keys** (<https://www.paperdaily.org/account>) and issue a key. Pick the
scopes you need — `read:digest, read:paper` cover all read tools, add
`write:profile` for follow/feedback tools and `synth:ask` for
`paperdaily_ask`. Keep the `pd_live_…` string — it is shown only once,
and the MCP server reads it from the env on launch.

## Configure Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and
add a `paperdaily` entry under `mcpServers`:

```json
{
  "mcpServers": {
    "paperdaily": {
      "command": "paperdaily-mcp",
      "env": {
        "PAPERDAILY_API_KEY": "pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        "PAPERDAILY_BASE_URL": "https://www.paperdaily.org"
      }
    }
  }
}
```

> `PAPERDAILY_BASE_URL` defaults to `https://www.paperdaily.org`; a
> self-hosted deployment can point it at its own origin instead.

Restart Claude Desktop. In a new conversation, open the tools list —
you should see `paperdaily` with 32 tools.

## Configure Claude Code

```bash
claude mcp add paperdaily \
    -e PAPERDAILY_API_KEY=pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
    -e PAPERDAILY_BASE_URL=https://www.paperdaily.org \
    -- paperdaily-mcp
```

Verify:

```bash
claude mcp list
# paperdaily — stdio — 32 tools
```

## Sanity check from the terminal

The MCP server only relays HTTP, so the fastest end-to-end check is the
API itself:

```bash
curl -sS -H "Authorization: Bearer pd_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
    https://www.paperdaily.org/api/v1/taxonomy/fields | head -c 400
```

You should see the top OpenAlex fields with paper counts in the
millions. If that works but the agent sees no tools, the problem is on
the MCP config side (env not reaching the process, binary not on PATH).

The three supplement tools are available in this Git source release. PyPI publication is separate; an older installed PyPI package may still expose 29 tools. Use the Git installation below when these three tools are needed. Supplement writes use the explicit Skill CLI workflow, not automatic MCP actions.

## The 32 tools

Mirror the v1 REST surface 1:1. Names follow the
`paperdaily_<verb>_<noun>` convention.

| Tool | Scope | REST |
|---|---|---|
| `paperdaily_get_digest_today` | read:digest | `GET /digest/today` |
| `paperdaily_get_digest_by_date` | read:digest | `GET /digest/{date}` |
| `paperdaily_get_digest_today_papers` | read:digest | `GET /digest/today/papers` |
| `paperdaily_list_papers` | read:paper | `GET /papers` |
| `paperdaily_get_paper` | read:paper | `GET /papers/{id}` |
| `paperdaily_get_paper_by_doi` | read:paper | `GET /papers/by-doi/{doi}` |
| `paperdaily_get_paper_by_arxiv` | read:paper | `GET /papers/by-arxiv/{id}` |
| `paperdaily_get_paper_authors` | read:paper | `GET /papers/{id}/authors` |
| `paperdaily_get_paper_citations` | read:paper | `GET /papers/{id}/citations` |
| `paperdaily_get_similar_papers` | read:paper | `GET /papers/{id}/similar` |
| `paperdaily_search_authors` | read:paper | `GET /authors` |
| `paperdaily_get_author` | read:paper | `GET /authors/{id}` |
| `paperdaily_get_author_by_orcid` | read:paper | `GET /authors/by-orcid/{orcid}` |
| `paperdaily_get_author_papers` | read:paper | `GET /authors/{id}/papers` |
| `paperdaily_list_fields` | read:paper | `GET /taxonomy/fields` |
| `paperdaily_list_subfields` | read:paper | `GET /taxonomy/subfields` |
| `paperdaily_list_topics` | read:paper | `GET /taxonomy/topics` |
| `paperdaily_get_profile` | write:profile | `GET /me/profile` |
| `paperdaily_update_profile` | write:profile | `PUT /me/profile` |
| `paperdaily_follow_topics` | write:profile | `POST /me/topics` |
| `paperdaily_unfollow_topic` | write:profile | `DELETE /me/topics/{id}` |
| `paperdaily_follow_authors` | write:profile | `POST /me/authors` |
| `paperdaily_unfollow_author` | write:profile | `DELETE /me/authors/{id}` |
| `paperdaily_record_feedback` | write:profile | `POST /me/feedback` |
| `paperdaily_ask` | synth:ask | `POST /ask` |
| `paperdaily_find_datasets` | read:paper | `GET /datasets/search` |
| `paperdaily_get_dataset` | read:paper | `GET /datasets/{registry_id}` |
| `paperdaily_get_dataset_narrative` | read:paper | `GET /datasets/{registry_id}/narrative` |
| `paperdaily_list_datasets_for_subfield` | read:paper | `GET /datasets/by-subfield/{id}` |
| `paperdaily_get_supplement_context` | read:paper | `GET /papers/{id}/supplement-context` |
| `paperdaily_list_supplements` | read:reading | `GET /me/paper-supplements` |
| `paperdaily_get_supplement` | read:reading | `GET /me/paper-supplements/{id}` |

The four dataset tools read the **curated dataset registry** (human
review gate: nothing enters without a proposal approved by a different
identity). Their counts come from this corpus's LLM extractions — they
are *not* a literature census, and coverage varies by field and year.
Ordering is by **usage frequency, not recommendation**: `n_hits` /
`n_in_scope` say how often a dataset shows up, never how good it is for
your question. Each tool description repeats these caveats so an agent
that only sees the tool list still gets them.

`paperdaily_get_dataset_narrative` is the one exception to "everything
here is an extracted fact": it returns an **AI synthesis** — the research
questions the dataset has been used to answer, the recurring research
designs, and a reading path — generated weekly by a local LLM over the
dataset's member papers. Every claim carries the papers it came from, and
claims whose citations could not be verified against those papers are
discarded before storage, so an empty section means *nothing verifiable*,
never *nothing exists*. Pass `ai_label_en` along with anything you quote
from it, and treat the member papers, not the synthesis, as the source.
`status` is `published` / `absent` (never generated — too few member
papers, or the weekly job hasn't reached it) / `hidden` (withdrawn by a
human); the last two return 200 with empty arrays.

Quotas are enforced server-side per (key tier, scope) — the same table
as the REST surface. 432 responses carry `Retry-After` and
`X-RateLimit-*` headers. Full rate-limit and error-code reference:
<https://www.paperdaily.org/docs/api/quickstart.html>.

## Sync research interests

With `write:profile` on your key, tell the agent: “把我的研究兴趣同步到 PaperDaily：我关注因果推断在劳动经济学中的应用，特别是工具变量和断点回归。” The agent calls `paperdaily_update_profile` with your description as `natural_language`; it does not need to select taxonomy IDs first.

The request authorizes this save. If the agent already has your stated interests, “把我的研究兴趣同步到 PaperDaily” is enough; otherwise it should ask for the description rather than invent one. Saving **replaces** your narrative, topic interests and method keywords, preserving authors, saves and feedback. Nonblank text, at most 8000 characters.

The response returns the saved profile and `report_rebuild`: `rebuilding` is queued/in progress (including an existing job), not complete; `throttled` and `unavailable` still mean the profile was saved. Rebuild uses the website's user lock and five-minute throttle, and sends no email. This tool waits up to 180 seconds for extraction. It never automatically retries a write; if transport fails, `paperdaily_get_profile` should first check whether the narrative was saved. A changed profile does not prove today's report has already regenerated.

The new tool requires an MCP client package containing `paperdaily_update_profile` and a server version exposing `PUT /api/v1/me/profile`. Restart the MCP connection after upgrading; old clients continue to offer the original tools.

## Troubleshooting

- **`PAPERDAILY_API_KEY env var is required`** — the env var isn't
  reaching the server. Claude Desktop only honours `env:` inside the
  `mcpServers` block; shell exports won't propagate.
- **All tools return `HTTP 401`** — the key is rejected. Check the
  prefix (`pd_live_…`), or list your keys on the account page to confirm
  it isn't revoked.
- **`HTTP 403` with `missing scope: …`** — the key wasn't issued with
  that scope; add the scope on the account page (existing keys can be
  edited) or issue a new key.
- **`HTTP 422` on `paperdaily_list_papers` with `q=`** — `q=` requires
  one of `topic_id` / `subfield_id` / `field_id`; add a facet to scope.
- **`HTTP 503` on `paperdaily_find_datasets`** — the embedding service is
  down. The endpoint deliberately fails loudly instead of returning an
  empty list: "no dataset matches this question" is a claim, and it would
  be a false one.
- **`paperdaily_find_datasets` returns few items but a long
  `unregistered` list** — the retrieved papers do use data, it just is
  not registered yet. The registry's first batch covers economics /
  social science / education / psychology only; CS benchmarks stay on the
  legacy `/api/tags/datasets` view.
- **`transport error`** — the binary couldn't reach
  `PAPERDAILY_BASE_URL`. Test with the curl sanity check above.


### 流式问答

`paperdaily_ask` 使用 `/api/v1/ask?stream=true`，逐事件读取进度、正文与重试事件，
以最终 `done` 的完整答案和引用返回结果。支持 MCP progressToken 的客户端可显示阶段进度；
它不意味着每个 MCP 客户端都会逐字显示正文。流中 error 或提前 EOF 会作为失败返回，
不会把半截内容当成功，也不会自动重新提交合成。升级 Git 安装的客户端后生效。


`paperdaily_get_supplement_context` 只读取 `/supplement-context` 的补充任务 baseline/材料范围，不能替代 `/context-bundle` 的引用邻域、版本链、topic与用户历史。当前32个MCP工具没有独立context-bundle工具；需要该上下文时，可使用Skill的deep-read入口（运行时会读取context-bundle）或按Agent协议调用REST，不要把supplement返回值改名为bundle。
