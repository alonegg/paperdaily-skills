"""Portable commands used by the thin and deep-research Skill entrypoints."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from pd_runtime import (
    ApiError,
    Client,
    collapse,
    endpoint,
    identity_keys,
    items,
    load_settings,
    object_result,
    path_segment,
    utc_now,
    write_json,
)

log = logging.getLogger(__name__)


def positive(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return number


def iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def parser_for(command: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="paperdaily " + command)
    p.add_argument("--json", action="store_true", help="structured output with coverage/errors")
    p.add_argument("--throttle", type=float, help="seconds between requests (default 1.05)")
    p.add_argument("--limit", type=positive, default=20)
    if command in ("search", "field", "worklist"):
        p.add_argument("target", nargs="*", help="research question, taxonomy name or id")
        p.add_argument("--year-from", type=int)
        p.add_argument("--year-to", type=int)
        p.add_argument("--since", type=iso_date)
        p.add_argument("--until", type=iso_date)
        p.add_argument("--days", type=positive, help="publication window ending today")
        p.add_argument(
            "--scope", default="", help="subfield id, field:<id>, or auto (discovery only)"
        )
        p.add_argument("--paper", action="append", default=[], help="seed identifier; repeatable")
        p.add_argument("--expand", action="append", default=[])
        p.add_argument("--search-mode", choices=("semantic", "title", "auto"), default="semantic")
        p.add_argument("--synth", action="store_true", help="opt in to one charged /ask synthesis")
        p.add_argument(
            "--no-synth",
            action="store_true",
            help="compatibility flag; synthesis is off by default",
        )
        p.add_argument("--similar", type=int, default=0 if command == "field" else 2)
        p.add_argument("--no-extraction", action="store_true")
        p.add_argument("--deep-topic-scan", action="store_true")
        if command == "worklist":
            p.add_argument("--out", type=Path)
            p.add_argument("--append", action="store_true")
            p.add_argument("--replace", action="store_true")
            p.add_argument("--context", action="store_true")
            p.add_argument("--citation-neighborhood", action="store_true")
    elif command == "resolve":
        p.add_argument("identifier")
    elif command == "context":
        p.add_argument("identifier")
    elif command == "author":
        p.add_argument("name", nargs="*")
        p.add_argument("--author-id")
        p.add_argument("--orcid")
        p.add_argument("--affiliation")
        p.add_argument("--all-matches", action="store_true")
        p.add_argument("--synth", action="store_true")
        p.add_argument("--no-synth", action="store_true")
    elif command == "topics":
        p.add_argument("needle")
        p.add_argument("--field")
        p.add_argument("--deep-topic-scan", action="store_true")
    elif command == "bibliography":
        p.add_argument("kind", choices=("match", "related", "citation-neighborhood", "journals"))
        p.add_argument(
            "--input", type=Path, required=True, help="JSON body; research text is sent to the API"
        )
    elif command == "inbox":
        p.add_argument("--claim")
    elif command == "watchlist":
        p.add_argument("action", choices=("list", "show", "create", "delete", "run", "entries"))
        p.add_argument("id", nargs="?")
        p.add_argument("--name")
        p.add_argument("--terms", default="")
        p.add_argument("--arxiv", default="")
        p.add_argument("--anchors", default="")
        p.add_argument("--threat", choices=("high", "med", "low"))
        p.add_argument("--since", type=iso_date)
    elif command == "doctor":
        pass
    else:
        p.error("unknown command")
    return p


def scope_params(scope: str) -> dict[str, str]:
    if not scope or scope == "auto":
        return {}
    if re.fullmatch(r"field:[0-9]{1,2}", scope):
        return {"field_id": scope.split(":")[1]}
    if re.fullmatch(r"[0-9]{4}", scope):
        return {"subfield_id": scope}
    raise ApiError("--scope expects a subfield id, field:<id>, or auto")


def date_bounds(args: argparse.Namespace) -> tuple[str | None, str | None]:
    since, until = args.since, args.until
    if args.days:
        if since:
            raise ApiError("--days and --since cannot be combined")
        since = (date.today() - timedelta(days=args.days - 1)).isoformat()
        until = until or date.today().isoformat()
    if args.year_from is not None:
        if not 1900 <= args.year_from <= 2100:
            raise ApiError("--year-from must be 1900..2100")
        since = max(since or "", "%04d-01-01" % args.year_from)
    if args.year_to is not None:
        if not 1900 <= args.year_to <= 2100:
            raise ApiError("--year-to must be 1900..2100")
        until = min(until or "9999", "%04d-12-31" % args.year_to)
    if since and until and since > until:
        raise ApiError("date interval is reversed")
    return since, until


def filtered(
    rows: list[dict[str, Any]], args: argparse.Namespace
) -> tuple[list[dict[str, Any]], int]:
    since, until = date_bounds(args)
    scopes = scope_params(args.scope)
    kept = []
    for row in rows:
        publication = str(row.get("publication_date") or "")[:10]
        if (since or until) and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication):
            continue
        if since and publication < since or until and publication > until:
            continue
        tax = row.get("primary_topic") or {}
        if any(str(row.get(key) or tax.get(key) or "") != value for key, value in scopes.items()):
            continue
        kept.append(row)
    return kept, len(rows) - len(kept)


def scope_rows(
    client: Client, rows: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Resolve names on PaperDetail, whose topic chain omits parent IDs."""
    scoped = [dict(r) for r in rows]
    for key in scope_params(args.scope):
        if not any(not r.get(key) for r in scoped):
            continue
        level = key.removesuffix("_id")
        taxonomy = items(client.get("/taxonomy/" + level + "s"))
        by_name: dict[str, list[str]] = {}
        for entry in taxonomy:
            by_name.setdefault(str(entry.get("display_name") or ""), []).append(str(entry["id"]))
        for row in scoped:
            name = (row.get("primary_topic") or {}).get(level + "_name")
            matches = by_name.get(name, [])
            if not row.get(key) and len(matches) == 1:
                row[key] = matches[0]
    return scoped


def taxon(client: Client, target: str) -> tuple[str, str] | None:
    for pattern, level in ((r"T\d+", "topic"), (r"\d{1,2}", "field"), (r"\d{4}", "subfield")):
        if re.fullmatch(pattern, target):
            return level, target
    for level in ("field", "subfield"):
        rows = items(client.get("/taxonomy/" + level + "s"))
        exact = [r for r in rows if str(r.get("display_name", "")).casefold() == target.casefold()]
        if len(exact) == 1:
            return level, str(exact[0]["id"])
        if len(exact) > 1:
            raise ApiError("ambiguous taxonomy name; use an explicit id")
    return None


def hydrate(client: Client, rows: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
    if not rows:
        return []
    details = client.batch([str(object_result(r)["id"]) for r in rows])
    by_id = {str(object_result(d)["id"]): d for d in details}
    out = []
    missing = set()
    for row in rows:
        detail = by_id.get(str(row["id"]))
        if detail is None:
            missing.add(str(row["id"]))
            continue
        out.append({**row, **detail})
    if missing:
        errors.append("batch detail missing for: " + ", ".join(sorted(missing)))
    return out


def search(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    question = " ".join(args.target).strip()
    if not question and not args.paper:
        raise ApiError("provide a question or --paper")
    if args.limit > 50:
        raise ApiError("search/seed limit is at most 50")
    since, until = date_bounds(args)
    params = {
        **scope_params(args.scope),
        "year_from": int(since[:4]) if since else None,
        "year_to": int(until[:4]) if until else None,
    }
    rows: list[dict[str, Any]] = []
    seeds: list[dict[str, Any]] = []
    errors: list[str] = []
    queries: list[str] = []
    channel_coverage: list[dict[str, Any]] = []

    def semantic(query: str) -> None:
        queries.append(query)
        data = client.get(
            endpoint("/papers/search", q=query, mode=args.search_mode, limit=args.limit, **params)
        )
        channel_coverage.append(
            {
                k: data.get(k)
                for k in ("layer_used", "truncated", "cluster_rerank_applied", "coverage")
            }
        )
        for rank, row in enumerate(items(data), 1):
            rows.append(
                {
                    **row,
                    "channel": str(data.get("layer_used", "search")),
                    "query": query,
                    "rrf": 1.0 / (60 + rank),
                }
            )

    for identifier in args.paper:
        try:
            seed = object_result(client.get(endpoint("/papers/resolve", id=identifier)))
            seeds.append(seed)
        except ApiError as exc:
            errors.append("seed resolution failed: " + str(exc))
            continue
        try:
            data = client.get(
                endpoint("/papers/" + path_segment(seed["id"]) + "/similar", k=args.limit)
            )
            channel_coverage.append(
                {"channel": "similar", "seed_id": seed["id"], "truncated": data.get("truncated")}
            )
            for rank, record in enumerate(items(data), 1):
                paper = object_result(record.get("paper"))
                rows.append(
                    {
                        **paper,
                        "channel": "similar",
                        "query": "similar:" + seed["id"],
                        "distance": record.get("distance"),
                        "rrf": 1.0 / (60 + rank),
                    }
                )
        except ApiError as exc:
            errors.append("seed similarity failed: " + str(exc))
        if seed.get("title"):
            try:
                semantic(seed["title"])
            except ApiError as exc:
                errors.append("seed title channel failed: " + str(exc))
    for query in ([question] if question else []) + args.expand:
        try:
            semantic(query)
        except ApiError as exc:
            errors.append("search channel failed: " + str(exc))
    rows = hydrate(client, rows, errors)
    seed_keys = set().union(*(identity_keys(s) for s in seeds)) if seeds else set()
    rows = [r for r in collapse(rows) if not identity_keys(r) & seed_keys]
    rows, excluded = filtered(scope_rows(client, rows, args), args)
    rows.sort(key=lambda r: (-r.get("hits", 0), -r.get("rrf", 0), str(r["id"])))
    return {
        "status": "partial" if errors else "complete",
        "items": rows,
        "seeds": seeds,
        "queries": queries,
        "errors": errors,
        "coverage": {
            "returned": len(rows),
            "excluded_by_filters": excluded,
            "scope": args.scope,
            "since": since,
            "until": until,
            "channels": channel_coverage,
            "note": "Bounded candidates; not corpus coverage or evidence support.",
        },
    }


def field(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    target = " ".join(args.target)
    resolved = taxon(client, target)
    if resolved is None:
        raise ApiError("no exact taxonomy match; use topics or semantic search")
    level, identifier = resolved
    since, until = date_bounds(args)
    # The list API has year granularity. Exact-day filtering is over this
    # bounded, explicitly disclosed page, never presented as an exhaustive search.
    data = client.get(
        endpoint(
            "/papers",
            **{level + "_id": identifier},
            limit=args.limit,
            year_from=int(since[:4]) if since else None,
            year_to=int(until[:4]) if until else None,
            has_extraction=None if args.no_extraction else True,
        )
    )
    errors: list[str] = []
    rows = hydrate(client, items(data), errors)
    rows, excluded = filtered(scope_rows(client, collapse(rows), args), args)
    neighbors = []
    if not 0 <= args.similar <= 50:
        raise ApiError("--similar must be 0..50")
    if args.similar:
        for row in rows:
            try:
                response = client.get(
                    endpoint("/papers/" + path_segment(row["id"]) + "/similar", k=args.similar)
                )
                neighbors.extend(object_result(r.get("paper")) for r in items(response))
            except ApiError as exc:
                errors.append("similar expansion failed: " + str(exc))
        neighbors = hydrate(client, neighbors, errors)
        neighbors, _ = filtered(scope_rows(client, collapse(neighbors), args), args)
    return {
        "status": "partial" if errors else "complete",
        "items": rows,
        "neighbors": neighbors,
        "errors": errors,
        "taxonomy": {level: identifier},
        "coverage": {
            "returned": len(rows),
            "excluded_by_filters": excluded,
            "since": since,
            "until": until,
            "next_cursor": data.get("next_cursor"),
            "has_extraction": not args.no_extraction,
            "note": "Publication dates; a bounded page, not all newly ingested papers.",
        },
    }


def topics(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    errors = []
    if args.deep_topic_scan or args.field:
        subfields = items(client.get(endpoint("/taxonomy/subfields", field_id=args.field)))
        rows = []
        for subfield in subfields:
            try:
                rows.extend(
                    items(
                        client.get(
                            endpoint("/taxonomy/topics", subfield_id=subfield["id"], limit=200)
                        )
                    )
                )
            except ApiError as exc:
                errors.append("subfield %s: %s" % (subfield["id"], exc))
    else:
        rows = items(client.get("/taxonomy/topics?limit=500"))
    matches = {
        str(r["id"]): r
        for r in rows
        if args.needle.casefold() in str(r.get("display_name", "")).casefold()
    }
    return {
        "status": "partial" if errors else "complete",
        "errors": errors,
        "items": sorted(matches.values(), key=lambda r: -int(r.get("paper_count") or 0)),
        "coverage": {
            "bounded": True,
            "deep_scan": bool(args.deep_topic_scan or args.field),
            "note": "Unscoped lookup covers top 500 topics; use --deep-topic-scan for all subfields.",
        },
    }


def author(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    if args.author_id:
        authors = [object_result(client.get("/authors/" + path_segment(args.author_id)))]
    elif args.orcid:
        authors = [object_result(client.get("/authors/by-orcid/" + path_segment(args.orcid)))]
    else:
        if not args.name:
            raise ApiError("provide a name, --author-id or --orcid")
        authors = items(client.get(endpoint("/authors", q=" ".join(args.name), limit=100)))
    if args.affiliation:
        authors = [
            a
            for a in authors
            if args.affiliation.casefold()
            in json.dumps(a.get("affiliations") or [], ensure_ascii=False).casefold()
        ]
    if len(authors) > 1 and not args.all_matches:
        return {
            "status": "needs_selection",
            "items": authors,
            "errors": [],
            "note": "Names are ambiguous; choose --author-id using ORCID, affiliation and research context.",
        }
    rows, errors = [], []
    for a in authors:
        try:
            response = client.get(
                endpoint("/authors/" + path_segment(a["id"]) + "/papers", limit=args.limit)
            )
            rows.extend({**r, "author_match_id": a["id"]} for r in items(response))
        except ApiError as exc:
            errors.append("author paper list failed: " + str(exc))
    return {
        "status": "partial" if errors else "complete",
        "items": rows,
        "authors": authors,
        "errors": errors,
        "note": "Authorship attribution is source metadata; review mismatches before attributing work.",
    }


def synthesize(client: Client, rows: list[dict[str, Any]]) -> Any:
    ids = [str(r["id"]) for r in rows[:15]]
    if not ids:
        return None
    return client.request(
        "POST",
        "/ask?stream=true",
        {
            "question": "Use load_extractions(paper_ids=%s) for these papers only. "
            "Summarize findings and limitations with paper ids. Distinguish "
            "abstract/extraction evidence from actual full-text reading. "
            "Do not infer relevance or support from ranking scores." % json.dumps(ids),
            "max_iters": 3,
        },
    )


def worklist(client: Client, args: argparse.Namespace) -> dict[str, Any]:
    target = " ".join(args.target).strip()
    if target and args.paper:
        raise ApiError("use a target or --paper seeds, not both")
    slug = re.sub(r"[^\w.-]+", "-", target or "-".join(args.paper), flags=re.UNICODE).strip("-")[
        :80
    ]
    out = args.out or Path("pd-research") / (slug or "research")
    if args.append and args.replace:
        raise ApiError("--append and --replace cannot be combined")
    existing = out / "worklist.jsonl"
    if existing.exists() and not (args.append or args.replace):
        raise ApiError("worklist exists; choose --append, --replace, or a new --out")
    if not args.paper and not target:
        raise ApiError("provide a target or --paper")
    resolved = taxon(client, target) if target else None
    if resolved:
        result = field(client, args)
        rows = [{**r, "source": "primary"} for r in result["items"]]
        rows += [{**r, "source": "similar"} for r in result["neighbors"]]
    else:
        result = search(client, args)
        rows = [{**r, "source": "seed"} for r in result["seeds"]]
        rows += [{**r, "source": "similar" if args.paper else "primary"} for r in result["items"]]
    if args.citation_neighborhood and rows:
        try:
            graph = client.request(
                "POST",
                "/papers/citation-neighborhood",
                {"paper_ids": [r["id"] for r in collapse(rows)[:100]], "limit": 50},
            )
            graph_rows = [
                object_result(r.get("paper"))
                for k in ("cocited", "common_refs")
                for r in graph.get(k, [])
            ]
            rows += [
                {**r, "source": "citation"} for r in hydrate(client, graph_rows, result["errors"])
            ]
            if graph.get("partial"):
                result["errors"].append("citation neighborhood is partial; see coverage artifact")
            write_json(out / "citation-neighborhood.json", graph)
        except ApiError as exc:
            result["errors"].append("citation neighborhood failed: " + str(exc))
    if args.append and existing.exists():
        old_rows = [json.loads(line) for line in existing.read_text().splitlines() if line.strip()]
        old_detail_file = out / "worklist.extractions.json"
        old_details = (
            items(json.loads(old_detail_file.read_text())) if old_detail_file.exists() else []
        )
        old_by_id = {r["id"]: r for r in old_details}
        rows += [{**old_by_id.get(r["id"], {}), **r} for r in old_rows]
    rows = collapse(rows)
    # Seeds are explicitly requested reading targets, even outside expansion filters.
    seeds = [r for r in rows if r.get("source") == "seed"]
    others, excluded = filtered(
        scope_rows(client, [r for r in rows if r.get("source") != "seed"], args), args
    )
    rows = seeds + others
    out.mkdir(parents=True, exist_ok=True)
    compact = []
    for row in rows:
        compact.append(
            {
                k: row.get(k)
                for k in (
                    "id",
                    "title",
                    "publication_date",
                    "doi",
                    "arxiv_id",
                    "paper_group_id",
                    "is_canonical",
                    "all_ids",
                    "primary_topic",
                    "source",
                )
            }
            | {
                "oa_url": row.get("external_full_text_url") or row.get("oa_url"),
                "pdf_url": row.get("resolved_pdf_url") or row.get("pdf_url"),
                "venue": row.get("venue_name") or row.get("venue"),
            }
        )
    tmp = existing.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in compact))
    tmp.replace(existing)
    write_json(out / "worklist.extractions.json", {"items": rows})
    if args.context or args.paper:
        context_targets = seeds or (rows if args.context else [])
        for seed in context_targets[:10]:
            try:
                bundle = client.get("/papers/" + path_segment(seed["id"]) + "/context-bundle")
                object_result(bundle, "paper")
                write_json(
                    out / "context" / (re.sub(r"[^\w.-]", "_", seed["id"]) + ".json"), bundle
                )
            except ApiError as exc:
                result["errors"].append("context unavailable: " + str(exc))
    result["items"] = compact
    result["status"] = "partial" if result["errors"] else "complete"
    result["coverage"]["excluded_after_expansion"] = excluded
    meta = {
        "skill_ver": "0.7.0",
        "model": "deterministic-retrieval",
        "created_at": utc_now(),
        "taxonomy": target or "seed",
        "query": target,
        "seeds": args.paper,
        "coverage": result["coverage"],
        "status": result["status"],
        "errors": result["errors"],
        "requests": client.calls,
    }
    meta_path = out / "worklist.meta.json"
    if args.append and meta_path.exists():
        previous = json.loads(meta_path.read_text())
        meta["previous_runs"] = previous.get("previous_runs", []) + [
            {k: v for k, v in previous.items() if k != "previous_runs"}
        ]
    if args.synth and not args.no_synth:
        try:
            synthesis = synthesize(client, rows)
            write_json(out / "synthesis-background.json", synthesis)
        except ApiError as exc:
            result["errors"].append("optional synthesis failed: " + str(exc))
            result["status"] = "partial"
    meta["status"] = result["status"]
    meta["requests"] = client.calls
    write_json(meta_path, meta)
    result["out"] = str(out)
    return result


def watchlist(client: Client, args: argparse.Namespace) -> Any:
    base = "/me/watchlists"
    if args.action == "list":
        return client.get(base)
    if args.action == "create":
        if not args.name:
            raise ApiError("--name is required")
        split = lambda s: [v.strip() for v in s.split(",") if v.strip()]
        return client.request(
            "POST",
            base,
            {
                "name": args.name,
                "query_terms": split(args.terms),
                "arxiv_categories": split(args.arxiv),
                "anchor_paper_ids": split(args.anchors),
            },
        )
    if not args.id:
        raise ApiError("watchlist id required")
    path = base + "/" + path_segment(args.id)
    if args.action == "show":
        return client.get(path)
    if args.action == "entries":
        return client.get(
            endpoint(
                path + "/entries", threat_level=args.threat, since=args.since, limit=args.limit
            )
        )
    if args.action == "delete":
        return client.request("DELETE", path)
    return client.request("POST", path + "/run-now", {})


def dispatch(command: str, args: argparse.Namespace, client: Client) -> Any:
    if command in ("search", "field", "author", "topics", "worklist", "watchlist"):
        result = globals()[command](client, args)
        if (
            command in ("search", "field", "author")
            and args.synth
            and not getattr(args, "no_synth", False)
        ):
            if result.get("status") == "complete":
                try:
                    result["synthesis"] = synthesize(client, result["items"])
                except ApiError as exc:
                    result["errors"].append("optional synthesis failed: " + str(exc))
                    result["status"] = "partial"
        return result
    if command in ("resolve", "context"):
        paper = object_result(client.get(endpoint("/papers/resolve", id=args.identifier)))
        if command == "resolve":
            return paper
        return object_result(
            client.get("/papers/" + path_segment(paper["id"]) + "/context-bundle"), "paper"
        )
    if command == "bibliography":
        body = json.loads(args.input.read_text(encoding="utf-8"))
        path = "/journals/lookup" if args.kind == "journals" else "/papers/" + args.kind
        result = client.request("POST", path, body)
        if args.kind in ("match", "journals"):
            items(result)
        else:
            required = (
                ("cocited", "common_refs")
                if args.kind == "citation-neighborhood"
                else ("per_unit", "doc")
            )
            if not isinstance(result, dict) or any(
                not isinstance(result.get(k), list) for k in required
            ):
                raise ApiError("bibliography response has no valid candidate arrays")
        return result
    if command == "inbox":
        if args.claim:
            return client.request("POST", "/me/agent-tasks/" + path_segment(args.claim) + "/claim")
        result = client.get("/me/agent-tasks?status=pending")
        items(result)
        return result
    if command == "doctor":
        response = client.get("/taxonomy/fields")
        return {
            "status": "authenticated",
            "taxonomy_fields": len(items(response)),
            "base": client.settings.base,
            "note": "read:paper verified; other scopes are task-specific.",
        }
    raise ApiError("unknown command")


def render(result: Any) -> str:
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return json.dumps(result, ensure_ascii=False, indent=2)
    if any("ref_id" in row for row in result["items"]):
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = ["Status: " + str(result.get("status", "complete"))]
    for error in result.get("errors", []):
        lines.append("INCOMPLETE: " + error)
    if result.get("note"):
        lines.append(str(result["note"]))
    if result.get("coverage"):
        lines.append("Coverage: " + json.dumps(result["coverage"], ensure_ascii=False))
    for row in result["items"]:
        title = str(row.get("title") or row.get("name") or row.get("display_name") or row.get("id"))
        lines.append(
            "- %s | %s | %s"
            % (
                title.replace("\n", " "),
                row.get("id", ""),
                row.get("publication_date") or "date unknown",
            )
        )
        if row.get("orcid") or row.get("affiliations"):
            lines.append(
                "  "
                + json.dumps({k: row.get(k) for k in ("orcid", "affiliations")}, ensure_ascii=False)
            )
    if not result["items"] and not result.get("errors"):
        lines.append("No matches within this request's filters and coverage.")
    if result.get("synthesis"):
        lines.append(json.dumps(result["synthesis"], ensure_ascii=False, indent=2))
    return "\n".join(lines)


def main(command: str | None = None, argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    values = list(sys.argv[1:] if argv is None else argv)
    if command is None:
        if not values or values[0] in ("-h", "--help"):
            sys.stdout.write(
                "Commands: resolve search field author topics context bibliography worklist inbox watchlist doctor supplement\n"
            )
            return 0
        command = values.pop(0)
    try:
        if command == "supplement":
            from pd_supplement import main as supplement_main
            local_only = bool(values) and (values[0] == "validate" or "--help" in values or (values[0] == "submit" and "--dry-run" in values))
            result = supplement_main(None if local_only else Client(load_settings()), values)
            sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0
        args = parser_for(command).parse_args(values)
        settings = load_settings()
        if args.throttle is not None:
            settings = replace(settings, throttle=args.throttle)
        result = dispatch(command, args, Client(settings))
        sys.stdout.write(
            (json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result))
            + "\n"
        )
        if isinstance(result, dict):
            if result.get("status") == "needs_selection":
                return 5
            if result.get("status") == "partial" or result.get("partial"):
                return 4
        return 0
    except (ApiError, ValueError, OSError) as exc:
        log.error("%s", exc)
        return 3
