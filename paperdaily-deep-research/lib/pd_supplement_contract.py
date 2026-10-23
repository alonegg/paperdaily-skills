"""Shared strict supplement contract; published as a standalone Skill module.

Validation checks structure and declared evidence relationships, never evidence truth.
"""

from __future__ import annotations

import ipaddress
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

BYTE_LIMITS = {"Empty": 1024, "CreateTask": 8192, "Submission": 524288, "SourceCandidate": 4096}


def canonical_bytes(value: Any) -> bytes:
    """Serialize request data deterministically."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_nul(item) for item in value)
    return False


def depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(item) for item in value), default=0)
    return 0


def pointer_exists(document: Any, pointer: str, field: str) -> bool:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
    if parts[:2] != ["analysis", field]:
        return False
    current = document
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return False
    return True


def public_url_shape(url: str) -> bool:
    """Check syntax only; DNS/provider/network verification is not performed."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in url
            or "#" in url
            or "\\" in url
            or "." not in host
            or host.endswith(".")
            or not re.fullmatch(r"[a-zA-Z0-9.-]+", host)
        ):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return True
        return False
    except ValueError:
        return False


def semantic_error(model: str, body: dict[str, Any], context: dict[str, Any] | None) -> str | None:
    """Validate declared cross-field relationships; authorization is handled separately."""
    if contains_nul(body):
        return "invalid_text"
    if depth(body) > 16:
        return "invalid_json"
    if len(canonical_bytes(body)) > BYTE_LIMITS[model]:
        return "body_too_large"
    if model == "SourceCandidate":
        return None if public_url_shape(body["public_url"]) else "source_url_invalid"
    if model != "Submission":
        return None
    if len(body["note_md"].encode("utf-8")) > 65536:
        return "body_too_large"
    if context is not None and (
        body["paper_id"] != context.get("paper_id")
        or body["baseline_hash"] != context.get("baseline_hash")
    ):
        return "task_binding_invalid"
    source_ids: set[str] = set()
    anchor_refs: set[tuple[str, str]] = set()
    quote_total = 0
    for source in body["sources"]:
        source_id = source["source_id"]
        if source_id in source_ids:
            return "duplicate_id"
        source_ids.add(source_id)
        if source["identity_relation"] == "uncertain":
            return "identity_unconfirmed"
        extent = source["extent"]
        carrier = source["carrier"]
        if carrier == "binary-pdf":
            if (
                "page_count" not in source
                or not source.get("pages_read")
                or "sections_read" in source
                or extent == "abstract"
            ):
                return "reading_scope_invalid"
            pages = set(source["pages_read"])
            if max(pages) > source["page_count"]:
                return "reading_scope_invalid"
            if extent == "full" and pages != set(range(1, source["page_count"] + 1)):
                return "reading_scope_invalid"
        else:
            if "page_count" in source or "pages_read" in source or not source.get("sections_read"):
                return "reading_scope_invalid"
            if (carrier == "abstract") != (extent == "abstract"):
                return "reading_scope_invalid"
            if carrier == "abstract" and source["sections_read"] != ["Abstract"]:
                return "reading_scope_invalid"
        if source["reading_complete"] != (extent == "full"):
            return "reading_scope_invalid"
        for anchor in source["anchors"]:
            key = source_id, anchor["anchor_id"]
            if key in anchor_refs:
                return "duplicate_id"
            anchor_refs.add(key)
            quote_total += len(anchor["quote"])
            if carrier == "binary-pdf":
                if anchor.get("page") not in source["pages_read"]:
                    return "evidence_invalid"
            elif "page" in anchor or anchor.get("section") not in source["sections_read"]:
                return "evidence_invalid"
            if carrier == "abstract" and not anchor["location"].startswith("Abstract"):
                return "reading_scope_invalid"
    if quote_total > 2000:
        return "evidence_quote_budget"
    findings = body["findings"]
    for finding in findings:
        if (finding["status"] == "gap") != (not finding["evidence"]):
            return "evidence_invalid"
        if any(
            (ref["source_id"], ref["anchor_id"]) not in anchor_refs for ref in finding["evidence"]
        ):
            return "evidence_invalid"
        if finding["operation"] == "correct":
            if context is not None and not pointer_exists(
                context.get("baseline", {}), finding.get("prior_locator", ""), finding["field"]
            ):
                return "baseline_pointer_invalid"
        elif "prior_locator" in finding:
            return "baseline_pointer_invalid"
    has_content = any(finding["status"] != "gap" for finding in findings)
    if body["outcome"] == "insufficient_material":
        if has_content or not body.get("reason", "").strip():
            return "outcome_invalid"
    elif not has_content:
        return "outcome_invalid"
    if body["outcome"] == "supplemented" and any(
        source["extent"] == "partial" for source in body["sources"]
    ):
        return "outcome_invalid"
    return None


@lru_cache(maxsize=1)
def request_schema() -> dict[str, Any]:
    here = Path(__file__).resolve().parent
    path = here / "schemas" / "paper_supplement_requests.json"
    if not path.is_file():
        path = here / "paper_supplement_requests.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("supplement schema must be an object")
    return result


def validate_request(
    model: str, body: dict[str, Any], context: dict[str, Any] | None = None
) -> str | None:
    from jsonschema import Draft202012Validator, FormatChecker

    schema = request_schema()
    validator = Draft202012Validator(
        {"$defs": schema["$defs"], "$ref": "#/$defs/" + model}, format_checker=FormatChecker()
    )
    if not validator.is_valid(body):
        return "schema_invalid"
    return semantic_error(model, body, context)


def inline_schema(model: str) -> dict[str, Any]:
    schema = request_schema()

    def expand(value: Any) -> Any:
        if isinstance(value, dict):
            if "$ref" in value:
                return expand(schema["$defs"][value["$ref"].rsplit("/", 1)[1]])
            return {key: expand(item) for key, item in value.items()}
        if isinstance(value, list):
            return [expand(item) for item in value]
        return value

    result = expand(schema["$defs"][model])
    if not isinstance(result, dict):
        raise ValueError("request schema must be an object")
    return result
