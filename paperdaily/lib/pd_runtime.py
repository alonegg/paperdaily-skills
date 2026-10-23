"""Standard-library transport and paper identity helpers, bundled per Skill."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)
READ_POSTS = frozenset(
    (
        "/papers/batch",
        "/papers/match",
        "/papers/related",
        "/papers/citation-neighborhood",
        "/journals/lookup",
    )
)


class ApiError(Exception):
    """An operation failed; an empty result must never represent this state."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


@dataclass(frozen=True)
class Settings:
    base: str
    key: str
    timeout: float = 30.0
    throttle: float = 1.05
    retries: int = 3
    max_retry_wait: float = 65.0


def load_settings() -> Settings:
    """Read literal assignments without executing the user's env file."""
    values = dict(os.environ)
    envfile = Path.home() / ".paperdaily-cli" / "env"
    if envfile.is_file():
        for line in envfile.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:export\s+)?(PD_[A-Z_]+)\s*=\s*(.*)$", line)
            if match and match[1] not in values:
                words = shlex.split(match[2], comments=True)
                if len(words) == 1:
                    values[match[1]] = words[0]
    key = values.get("PD_KEY", "")
    if not key:
        raise ApiError(
            "PD_KEY missing; issue your own key at /account and set ~/.paperdaily-cli/env"
        )
    try:
        return Settings(
            base=values.get("PD_BASE", "https://www.paperdaily.org/api/v1").rstrip("/"),
            key=key,
            timeout=float(values.get("PD_TIMEOUT", "30")),
            throttle=float(values.get("PD_THROTTLE", "1.05")),
        )
    except ValueError as exc:
        raise ApiError("PD_TIMEOUT and PD_THROTTLE must be numbers") from exc


def validate_base(base: str) -> None:
    url = urllib.parse.urlsplit(base)
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ApiError("PD_BASE must be an API URL without credentials, query or fragment")
    local = url.hostname == "localhost"
    try:
        addr = ipaddress.ip_address(url.hostname)
        local = local or addr.is_private or addr.is_loopback
    except ValueError:
        pass
    if url.scheme != "https" and not (url.scheme == "http" and local):
        raise ApiError("PD_BASE requires HTTPS (HTTP allowed only for private/loopback hosts)")


def retry_after(value: str | None, fallback: float) -> float:
    if not value:
        return fallback
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else fallback
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
            return max(0.0, dt.timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return fallback


def path_segment(value: str) -> str:
    if not value.strip() or value in (".", ".."):
        raise ApiError("empty or invalid resource identifier")
    return urllib.parse.quote(value, safe="")


def endpoint(path: str, **params: Any) -> str:
    pairs = {
        k: str(v).lower() if isinstance(v, bool) else v
        for k, v in params.items()
        if v is not None and v != ""
    }
    return path + ("?" + urllib.parse.urlencode(pairs) if pairs else "")


class Client:
    def __init__(
        self,
        settings: Settings,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        validate_base(settings.base)
        if (
            not all(
                math.isfinite(v)
                for v in (settings.timeout, settings.throttle, settings.max_retry_wait)
            )
            or settings.timeout <= 0
            or settings.throttle < 0
            or settings.max_retry_wait < 0
            or settings.retries < 0
        ):
            raise ApiError("timeout must be positive and throttle nonnegative")
        self.settings = settings
        self.sleep = sleep
        self.clock = clock
        self.next_call = 0.0
        self.opener = urllib.request.build_opener(NoRedirect())
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: Any = None, *, idempotent: bool = False, extra_headers: dict[str, str] | None = None) -> Any:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(part == ".." for part in urllib.parse.unquote(path.split("?")[0]).split("/"))
        ):
            raise ApiError("request path must stay inside PD_BASE")
        if set(extra_headers or {}) - {"Idempotency-Key", "X-Supplement-Lease", "X-Supplement-Generation"}:
            raise ApiError("unsupported extra request header")
        can_retry = (
            method == "GET" or (method == "POST" and path.split("?")[0] in READ_POSTS) or idempotent
        )
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode()
        for attempt in range(self.settings.retries + 1):
            wait = self.next_call - self.clock()
            if wait > 0:
                self.sleep(wait)
            self.next_call = self.clock() + self.settings.throttle
            req = urllib.request.Request(
                self.settings.base + path,
                data=payload,
                method=method,
                headers={
                    "Authorization": "Bearer " + self.settings.key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "paperdaily-skills/2",
                    **(extra_headers or {}),
                },
            )
            status: int | None = None
            headers: Any = {}
            try:
                with self.opener.open(req, timeout=self.settings.timeout) as resp:
                    status = resp.status
                    headers = resp.headers
                    raw = resp.read(32 * 1024 * 1024 + 1)
                if len(raw) > 32 * 1024 * 1024:
                    raise ApiError("API response exceeds the 32 MiB client limit", status)
                self.calls.append({"method": method, "path": path.split("?")[0], "status": status})
                if status == 204:
                    return None
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ApiError(
                        "API returned non-JSON content; check PD_BASE/access proxy", status
                    ) from exc
                if not isinstance(data, (dict, list)):
                    raise ApiError("API response must be a JSON object or array", status)
                return data
            except urllib.error.HTTPError as exc:
                status, headers = exc.code, exc.headers
                exc.close()
            except (urllib.error.URLError, TimeoutError, OSError):
                status = None
            self.calls.append({"method": method, "path": path.split("?")[0], "status": status})
            label = "network/timeout failure" if status is None else "HTTP %s" % status
            transient = status is None or status in (429, 500, 502, 503, 504)
            if (
                status == 429
                and "/reading-sessions" in path
                and not headers.get("Retry-After")
                and not headers.get("X-RateLimit-Limit")
            ):
                transient = False
            if not can_retry or not transient or attempt == self.settings.retries:
                raise ApiError(label + "; operation not completed", status)
            delay = retry_after(headers.get("Retry-After"), min(2**attempt, 8))
            if delay > self.settings.max_retry_wait:
                raise ApiError(label + "; Retry-After exceeds wait budget; retry later", status)
            log.warning(
                "%s; retry %d/%d after %.2fs", label, attempt + 1, self.settings.retries, delay
            )
            self.sleep(delay)
        raise ApiError("request not completed")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def batch(self, ids: list[str]) -> list[dict[str, Any]]:
        unique = list(dict.fromkeys(ids))
        found: list[dict[str, Any]] = []
        for start in range(0, len(unique), 100):
            data = self.request("POST", "/papers/batch", {"ids": unique[start : start + 100]})
            found.extend(items(data))
        return found


def items(data: Any) -> list[dict[str, Any]]:
    rows = data if isinstance(data, list) else data.get("items") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ApiError("API response has no valid items array")
    return rows


def object_result(data: Any, required: str = "id") -> dict[str, Any]:
    if not isinstance(data, dict) or not data.get(required):
        raise ApiError("API response missing required field: " + required)
    return data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Replace a local artifact atomically; never partially write valid JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def identity_keys(row: dict[str, Any]) -> set[str]:
    keys = {"id:" + str(row["id"])}
    if row.get("paper_group_id"):
        keys.add("group:" + str(row["paper_group_id"]))
    doi = str(row.get("doi") or "").lower().removeprefix("https://doi.org/").removeprefix("doi:")
    if doi:
        keys.add("doi:" + doi)
    arxiv = str(row.get("arxiv_id") or "")
    for candidate in (
        arxiv,
        str(row.get("id", "")),
        doi,
        str(row.get("oa_url") or ""),
        str(row.get("external_full_text_url") or ""),
    ):
        match = re.search(
            r"(?:arxiv:|arxiv\.|arxiv.org/(?:abs|pdf)/)([0-9]{4}\.[0-9]{4,5}|[a-z-]+/[0-9]{7})",
            candidate,
            re.I,
        )
        if match:
            keys.add("arxiv:" + match[1].lower())
    if arxiv:
        keys.add("arxiv:" + re.sub(r"v\d+$", "", arxiv.removeprefix("arxiv:")).lower())
    return keys


def collapse(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge strong identities transitively; same title alone is not identity."""
    groups: list[tuple[set[str], list[dict[str, Any]]]] = []
    for row in rows:
        object_result(row)
        keys, members = identity_keys(row), [dict(row)]
        remaining = []
        for old_keys, old_rows in groups:
            if keys & old_keys:
                keys |= old_keys
                members.extend(old_rows)
            else:
                remaining.append((old_keys, old_rows))
        remaining.append((keys, members))
        groups = remaining
    out = []
    for _, members in groups:
        preferred = sorted(
            members, key=lambda r: (r.get("is_canonical") is not True, r.get("source") != "seed")
        )[0]
        record = dict(preferred)
        record["all_ids"] = sorted(
            {str(r["id"]) for r in members} | {v for r in members for v in r.get("all_ids", [])}
        )
        record["channels"] = sorted(
            (
                {str(r.get("channel", "")) for r in members}
                | {c for r in members for c in r.get("channels", [])}
            )
            - {""}
        )
        if any(r.get("source") == "seed" for r in members):
            record["source"] = "seed"
        record["hits"] = len({str(r.get("query", r.get("channel", ""))) for r in members} - {""})
        record["rrf"] = sum(float(r.get("rrf", 0)) for r in members)
        out.append(record)
    return out
