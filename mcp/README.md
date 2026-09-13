# paperdaily MCP server — quickstart

`paperdaily-mcp` is a stdio Model Context Protocol server that exposes
the paperdaily v1 REST API as **28 agent tools**. Drop it into Claude
Desktop / Claude Code / Cursor / any MCP-compatible runtime and the
agent can auto-discover and call `paperdaily_get_digest_today`,
`paperdaily_get_paper`, `paperdaily_ask`, etc. directly.

It is a thin proxy: every tool call is forwarded as a typed HTTP request
with your bearer key. Accounts, scopes and rate limits are the same ones
the website uses — enforced server-side per key, nothing to configure in
the client.

## Install

From PyPI (recommended):

```bash
uv tool install paperdaily-mcp
# or
pipx install paperdaily-mcp
```

From the public skills repo:

```bash
uv tool install \
    --from 'git+https://github.com/alonegg/paperdaily-skills#subdirectory=mcp' \
    paperdaily-mcp
```

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
you should see `paperdaily` with 28 tools.

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
# paperdaily — stdio — 28 tools
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

## The 28 tools

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
as the REST surface. 429 responses carry `Retry-After` and
`X-RateLimit-*` headers. Full rate-limit and error-code reference:
<https://www.paperdaily.org/docs/api/quickstart.html>.

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
