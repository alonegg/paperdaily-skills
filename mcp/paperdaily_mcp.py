"""paperdaily-mcp — stdio MCP server bridging an agent to the public v1 API.

Run locally (Claude Desktop, Claude Code, Cursor, …) and proxy each MCP
tool call to `https://www.paperdaily.org/api/v1/*` over HTTP with the
user's bearer key. The agent runtime auto-discovers 28 tools via
`list_tools`; each call is forwarded as a typed HTTP request. Accounts,
scopes, and rate limits are the same ones the website uses — enforced
server-side per key; this binary carries no policy of its own.

This is deliberately a *thin* binary — no paperdaily-app code dependencies
beyond `mcp` and `httpx`. Install from PyPI:

    uv tool install paperdaily-mcp        # or: pipx install paperdaily-mcp

Or from the public skills repo:

    uv tool install \
        --from 'git+https://github.com/alonegg/paperdaily-skills#subdirectory=mcp' \
        paperdaily-mcp

Claude Desktop config:

    "mcpServers": {
      "paperdaily": {
        "command": "paperdaily-mcp",
        "env": {
          "PAPERDAILY_API_KEY": "pd_live_…",
          "PAPERDAILY_BASE_URL": "https://www.paperdaily.org"
        }
      }
    }

Environment:
    PAPERDAILY_API_KEY (required) — bearer key; issue one at
        https://www.paperdaily.org/account (账号设置 → API Keys).
    PAPERDAILY_BASE_URL (default https://www.paperdaily.org) — override
        to point a self-hosted deployment at its own origin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

log = logging.getLogger("paperdaily-mcp")


# ─── tool registry ──────────────────────────────────────────────────
#
# Each entry maps an MCP tool name to:
#   - the REST method + path template
#   - which call args go into the path vs. the query string
#   - the inputSchema (JSON Schema) the agent sees
#
# Keep this table the source of truth — adding a new tool is one entry,
# no extra code path.


def _t(
    name: str,
    method: str,
    path: str,
    *,
    description: str,
    path_params: tuple[str, ...] = (),
    query_params: tuple[str, ...] = (),
    body_params: tuple[str, ...] = (),
    schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "name": name,
        "method": method,
        "path": path,
        "path_params": path_params,
        "query_params": query_params,
        "body_params": body_params,
        "tool": types.Tool(name=name, description=description, inputSchema=schema),
    }


# Repeated verbatim in every dataset tool description (design doc §1 R3 / R4 /
# R6). The agent only ever sees the tool list, so the caveat has to travel with
# the tool, not live in a doc it will never open.
_COVERAGE = (
    "Counts are this corpus's LLM extractions, NOT a literature census — "
    "coverage varies by field and year. Registry entries are human-reviewed "
    "(propose \u2192 a different identity approves); ordering is by usage "
    "frequency, NOT by recommendation or quality."
)


_TOOLS: list[dict[str, Any]] = [
    # ─── digest (read:digest) ───────────────────────────────────────
    _t(
        "paperdaily_get_digest_today",
        "GET",
        "/api/v1/digest/today",
        description="Returns today's 4-tier digest report for the key's owning user. "
        "Fail with 404 if the daily pipeline hasn't built today's report yet "
        "(it runs ~07:30 local time).",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _t(
        "paperdaily_get_digest_by_date",
        "GET",
        "/api/v1/digest/{date}",
        description="Returns the digest for a specific ISO date (YYYY-MM-DD), up to 90 days back.",
        path_params=("date",),
        schema={
            "type": "object",
            "required": ["date"],
            "properties": {"date": {"type": "string", "description": "ISO 8601 date"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_digest_today_papers",
        "GET",
        "/api/v1/digest/today/papers",
        description="Today's digest flattened into a single paper list. Optional `tier` "
        "(must_read|should_read|scan|fyi) filters to one tier.",
        query_params=("tier", "limit"),
        schema={
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["must_read", "should_read", "scan", "fyi"],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
            },
            "additionalProperties": False,
        },
    ),
    # ─── papers (read:paper) ────────────────────────────────────────
    _t(
        "paperdaily_list_papers",
        "GET",
        "/api/v1/papers",
        description="Filter the paper corpus by topic/subfield/field, year window, "
        "completeness flags, or title substring (`q=`). NOTE: `q=` requires at "
        "least one of topic_id/subfield_id/field_id (server returns 422 otherwise) "
        "to avoid seq-scanning 6M+ rows.",
        query_params=(
            "topic_id",
            "subfield_id",
            "field_id",
            "q",
            "year_from",
            "year_to",
            "has_extraction",
            "has_pdf",
            "limit",
            "cursor",
        ),
        schema={
            "type": "object",
            "properties": {
                "topic_id": {"type": "string", "description": "OpenAlex Topic id, e.g. T11000"},
                "subfield_id": {"type": "string", "description": "OpenAlex Subfield id, e.g. 1702"},
                "field_id": {"type": "string", "description": "OpenAlex Field id, e.g. 17"},
                "q": {"type": "string", "maxLength": 80, "description": "title substring; requires a facet"},
                "year_from": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "year_to": {"type": "integer", "minimum": 1900, "maximum": 2100},
                "has_extraction": {"type": "boolean"},
                "has_pdf": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "cursor": {"type": "string", "description": "next_cursor from previous page"},
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_paper",
        "GET",
        "/api/v1/papers/{paper_id}",
        description="Full paper detail: metadata, 4-layer topic chain, LLM extraction "
        "(tldr_zh, key_claims, methods, datasets, identification, sample, …) if "
        "computed, and data-quality flags. `identification` "
        "({strategy: DiD|IV|RDD|RCT|event-study|structural|matching|panel-FE|other|none, "
        "description_zh, description_en}) and `sample` ({period, region, unit}) come from "
        "the v2 extraction schema only — null on most rows, and null means unknown, "
        "not \"no identification strategy\".",
        path_params=("paper_id",),
        schema={
            "type": "object",
            "required": ["paper_id"],
            "properties": {"paper_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_paper_by_doi",
        "GET",
        "/api/v1/papers/by-doi/{doi}",
        description="Look up a paper by DOI. DOIs containing `/` are accepted as-is.",
        path_params=("doi",),
        schema={
            "type": "object",
            "required": ["doi"],
            "properties": {"doi": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_paper_by_arxiv",
        "GET",
        "/api/v1/papers/by-arxiv/{arxiv_id}",
        description="Look up a paper by bare arXiv id (no `arXiv:` prefix, no version).",
        path_params=("arxiv_id",),
        schema={
            "type": "object",
            "required": ["arxiv_id"],
            "properties": {"arxiv_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_paper_authors",
        "GET",
        "/api/v1/papers/{paper_id}/authors",
        description="Author list for a paper, sorted by `position` (0-indexed).",
        path_params=("paper_id",),
        schema={
            "type": "object",
            "required": ["paper_id"],
            "properties": {"paper_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_paper_citations",
        "GET",
        "/api/v1/papers/{paper_id}/citations",
        description="Citation list in either direction (`out` = seed cites these; `in` = these "
        "cite seed). Unresolved citations are returned as `is_stub=true`.",
        path_params=("paper_id",),
        query_params=("direction", "limit"),
        schema={
            "type": "object",
            "required": ["paper_id"],
            "properties": {
                "paper_id": {"type": "string"},
                "direction": {"type": "string", "enum": ["out", "in"], "default": "out"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 50},
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_similar_papers",
        "GET",
        "/api/v1/papers/{paper_id}/similar",
        description="K-nearest neighbours by BGE-M3 / SPECTER2 cosine distance.",
        path_params=("paper_id",),
        query_params=("k",),
        schema={
            "type": "object",
            "required": ["paper_id"],
            "properties": {
                "paper_id": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "additionalProperties": False,
        },
    ),
    # ─── authors (read:paper) ───────────────────────────────────────
    _t(
        "paperdaily_search_authors",
        "GET",
        "/api/v1/authors",
        description="Search authors by name (case-sensitive substring). Sorted by paper count desc.",
        query_params=("q", "limit"),
        schema={
            "type": "object",
            "properties": {
                "q": {"type": "string", "maxLength": 80},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_author",
        "GET",
        "/api/v1/authors/{author_id}",
        description="Author detail by OpenAlex / internal id.",
        path_params=("author_id",),
        schema={
            "type": "object",
            "required": ["author_id"],
            "properties": {"author_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_author_by_orcid",
        "GET",
        "/api/v1/authors/by-orcid/{orcid}",
        description="Look up an author by ORCID (bare or URL form).",
        path_params=("orcid",),
        schema={
            "type": "object",
            "required": ["orcid"],
            "properties": {"orcid": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_author_papers",
        "GET",
        "/api/v1/authors/{author_id}/papers",
        description="Papers authored by this author, most recent first.",
        path_params=("author_id",),
        query_params=("limit",),
        schema={
            "type": "object",
            "required": ["author_id"],
            "properties": {
                "author_id": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 30},
            },
            "additionalProperties": False,
        },
    ),
    # ─── taxonomy (read:paper) ──────────────────────────────────────
    _t(
        "paperdaily_list_fields",
        "GET",
        "/api/v1/taxonomy/fields",
        description="All 26 top-level Fields with paper counts.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _t(
        "paperdaily_list_subfields",
        "GET",
        "/api/v1/taxonomy/subfields",
        description="Subfields, optionally filtered by parent `field_id`.",
        query_params=("field_id",),
        schema={
            "type": "object",
            "properties": {"field_id": {"type": "string", "description": "e.g. 17"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_list_topics",
        "GET",
        "/api/v1/taxonomy/topics",
        description="Leaf-level Topics, optionally filtered by `subfield_id`.",
        query_params=("subfield_id", "limit"),
        schema={
            "type": "object",
            "properties": {
                "subfield_id": {"type": "string", "description": "e.g. 1702"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 100},
            },
            "additionalProperties": False,
        },
    ),
    # ─── profile mutation (write:profile) ───────────────────────────
    _t(
        "paperdaily_get_profile",
        "GET",
        "/api/v1/me/profile",
        description="Full interest-profile snapshot — followed topics, authors, recent feedback.",
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    _t(
        "paperdaily_follow_topics",
        "POST",
        "/api/v1/me/topics",
        description="Follow one or more Topic/Subfield/Field nodes. Idempotent; the same id "
        "can be re-submitted to update the weight.",
        body_params=("items",),
        schema={
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["topic_id"],
                        "properties": {
                            "topic_id": {"type": "string"},
                            "weight": {"type": "number", "minimum": 0, "maximum": 2, "default": 1.0},
                        },
                    },
                }
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_unfollow_topic",
        "DELETE",
        "/api/v1/me/topics/{topic_id}",
        description="Unfollow a topic. 404 if not followed.",
        path_params=("topic_id",),
        schema={
            "type": "object",
            "required": ["topic_id"],
            "properties": {"topic_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_follow_authors",
        "POST",
        "/api/v1/me/authors",
        description="Follow one or more authors by author_id. Idempotent.",
        body_params=("items",),
        schema={
            "type": "object",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["author_id"],
                        "properties": {"author_id": {"type": "string"}},
                    },
                }
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_unfollow_author",
        "DELETE",
        "/api/v1/me/authors/{author_id}",
        description="Unfollow an author. 404 if not followed.",
        path_params=("author_id",),
        schema={
            "type": "object",
            "required": ["author_id"],
            "properties": {"author_id": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    # ─── synthesis (synth:ask) ──────────────────────────────────────
    _t(
        "paperdaily_ask",
        "POST",
        "/api/v1/ask",
        description="Ask paperdaily a natural-language question. The server runs a "
        "graph-aware tool loop (find_papers_by_topics, find_similar_papers, "
        "get_citation_context, …) and returns a synthesised answer with cited "
        "paper ids. Costs internal LLM tokens; tight quota (free tier: 5/day). "
        "Requires `synth:ask` scope.",
        body_params=("question", "max_iters"),
        schema={
            "type": "object",
            "required": ["question"],
            "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 4000},
                "max_iters": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5},
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_record_feedback",
        "POST",
        "/api/v1/me/feedback",
        description="Append a feedback event (like/dislike/save/skip/open). Used by the "
        "candidate-pool builder to bias future recommendations.",
        body_params=("paper_id", "action", "reason", "report_date"),
        schema={
            "type": "object",
            "required": ["paper_id", "action"],
            "properties": {
                "paper_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["like", "dislike", "save", "skip", "open"],
                },
                "reason": {"type": "string"},
                "report_date": {"type": "string", "description": "ISO date, optional"},
            },
            "additionalProperties": False,
        },
    ),
    # ─── datasets (read:paper) ──────────────────────────────────────
    #
    # Every description carries the same three caveats on purpose. An agent
    # sees the tool list, not this file, and the caveats are exactly the
    # design doc's R3 / R4 / R6 gates: a count that arrives without its
    # basis gets quoted as "the literature says", and a frequency ranking
    # without its label gets quoted as "the best dataset for X".
    _t(
        "paperdaily_find_datasets",
        "GET",
        "/api/v1/datasets/search",
        description="Question-first dataset search: give it a research question "
        "(\"minimum wage and employment\", \"数字金融与家庭消费\") and it returns the "
        "datasets that the most semantically relevant papers in this corpus actually "
        "used, each with `n_hits` (how many of the retrieved papers used it), a "
        "template `why` sentence, and up to 3 example papers. `unregistered` lists "
        "dataset tags seen in those papers that have no registry entry yet. "
        "Optional `field` / `subfield` are OpenAlex ids that narrow the retrieval. "
        + _COVERAGE
        + " Returns 503 (not an empty list) when the embedding service is down.",
        query_params=("q", "field", "subfield", "limit"),
        schema={
            "type": "object",
            "required": ["q"],
            "properties": {
                "q": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 300,
                    "description": "research question or keywords",
                },
                "field": {"type": "string", "description": "OpenAlex Field id, e.g. 20"},
                "subfield": {"type": "string", "description": "OpenAlex Subfield id, e.g. 2003"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_dataset",
        "GET",
        "/api/v1/datasets/{registry_id}",
        description="One dataset's asset card by registry slug (e.g. `chfs`, `cfps`, "
        "`pisa`): FAIR fields (owner, coverage period, access mode + URL, licence, how "
        "to cite, campus availability), wave/version children, the paper_tags aliases "
        "that feed the counts, and aggregates — per-year usage with an extraction-rate "
        "baseline, subfield mix, sample period/region/unit histograms, identification "
        "strategies, co-occurring methods and datasets, and ranked exemplar papers. "
        "`methodology` spells out how every number was produced. "
        + _COVERAGE,
        path_params=("registry_id",),
        schema={
            "type": "object",
            "required": ["registry_id"],
            "properties": {
                "registry_id": {
                    "type": "string",
                    "description": "registry slug, e.g. chfs (get it from paperdaily_find_datasets)",
                }
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_get_dataset_narrative",
        "GET",
        "/api/v1/datasets/{registry_id}/narrative",
        description="**AI synthesis, not extracted facts** — what research questions "
        "this dataset has been used to answer, the recurring research designs "
        "(dataset x identification strategy x sample unit), and an origin -> classic "
        "-> recent reading path. Written by an LLM over the dataset's member papers "
        "and regenerated weekly; served from cache. Every claim carries the papers it "
        "was drawn from, and claims whose citations could not be verified against "
        "those papers are dropped before storage — so an empty section means "
        "\"nothing verifiable\", never \"nothing exists\". **Quote `ai_label_en` "
        "whenever you pass any of this on, and defer to the original papers**; the N "
        "in that label is how many papers were fed to the model, not the dataset's "
        "total. `status` is `published` / `absent` (never generated) / `hidden` "
        "(withdrawn by a human) — the last two return 200 with empty arrays. "
        + _COVERAGE,
        path_params=("registry_id",),
        schema={
            "type": "object",
            "required": ["registry_id"],
            "properties": {
                "registry_id": {
                    "type": "string",
                    "description": "registry slug, e.g. chfs (get it from paperdaily_find_datasets)",
                }
            },
            "additionalProperties": False,
        },
    ),
    _t(
        "paperdaily_list_datasets_for_subfield",
        "GET",
        "/api/v1/datasets/by-subfield/{subfield_id}",
        description="Reverse entry point: which registered datasets the papers of one "
        "OpenAlex Subfield actually use. Each row carries `n_in_scope` (papers of that "
        "dataset sitting in this subfield — the sort key), `n_papers` (its total here) "
        "and `share`. Read all three: `n_in_scope` alone puts a global dataset like WDI "
        "first in every field, `share` alone lets a 3-paper dataset impersonate a "
        "workhorse. "
        + _COVERAGE,
        path_params=("subfield_id",),
        query_params=("limit",),
        schema={
            "type": "object",
            "required": ["subfield_id"],
            "properties": {
                "subfield_id": {"type": "string", "description": "OpenAlex Subfield id, e.g. 2003"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "additionalProperties": False,
        },
    ),
]


_TOOL_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in _TOOLS}


# ─── HTTP plumbing ──────────────────────────────────────────────────


def _client() -> httpx.AsyncClient:
    base = os.environ.get("PAPERDAILY_BASE_URL", "https://www.paperdaily.org")
    key = os.environ.get("PAPERDAILY_API_KEY")
    if not key:
        raise RuntimeError(
            "PAPERDAILY_API_KEY env var is required (issue one at /api/me/keys)"
        )
    return httpx.AsyncClient(
        base_url=base,
        headers={"Authorization": f"Bearer {key}"},
        timeout=30.0,
    )


async def _dispatch(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    """Translate an MCP tool call into a paperdaily REST request, return JSON as text."""
    entry = _TOOL_BY_NAME.get(name)
    if entry is None:
        return [types.TextContent(type="text", text=f"unknown tool: {name}")]

    path = entry["path"]
    for p in entry["path_params"]:
        if p not in arguments:
            return [types.TextContent(type="text", text=f"missing required path param: {p}")]
        path = path.replace(f"{{{p}}}", str(arguments[p]))

    query: dict[str, Any] = {
        k: arguments[k] for k in entry["query_params"] if k in arguments and arguments[k] is not None
    }

    body: dict[str, Any] | None = None
    if entry["body_params"]:
        body = {k: arguments[k] for k in entry["body_params"] if k in arguments}

    method = entry["method"]
    async with _client() as cli:
        try:
            r = await cli.request(method, path, params=query, json=body)
        except httpx.HTTPError as e:
            return [types.TextContent(type="text", text=f"transport error: {e}")]

    body_text: str
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = r.json()
            body_text = json.dumps(payload, ensure_ascii=False, indent=2)
        except ValueError:
            body_text = r.text
    else:
        body_text = r.text

    if r.status_code >= 400:
        # Pass the error through to the agent so it can decide whether to
        # surface, retry, or pick a different tool.
        return [
            types.TextContent(
                type="text",
                text=f"HTTP {r.status_code}\n{body_text}",
            )
        ]
    return [types.TextContent(type="text", text=body_text)]


# ─── MCP server wiring ──────────────────────────────────────────────


server: Server = Server("paperdaily")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [t["tool"] for t in _TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
    return await _dispatch(name, arguments)


async def _amain() -> None:
    logging.basicConfig(
        level=os.environ.get("PAPERDAILY_MCP_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
