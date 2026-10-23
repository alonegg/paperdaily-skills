"""Single-paper supplement commands; selected source files never leave the client."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
from datetime import datetime, timezone
import subprocess
from pathlib import Path
from typing import Any

from pd_runtime import ApiError, Client, endpoint, path_segment, utc_now, write_json


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ApiError("expected JSON object")
    return value


def save_private(path: Path, value: dict[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def selected_file(path: Path) -> tuple[str, int]:
    if path.is_symlink():
        raise ApiError("selected source must not be a symlink")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 50 * 1024 * 1024:
            raise ApiError("selected source must be a regular file of at most 50 MiB")
        digest = hashlib.sha256()
        while True:
            block = stream.read(65536)
            if not block:
                break
            digest.update(block)
        if os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
            raise ApiError("source changed while reading")
        return digest.hexdigest(), info.st_size


def pdf_pages(path: Path) -> int:
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ApiError(
                "prepare accepts a selected PDF; other carriers may be registered explicitly"
            )
    result = subprocess.run(
        ["pdfinfo", str(path.resolve())], capture_output=True, text=True, timeout=20, check=True
    )
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            count = int(line.split(":", 1)[1])
            if not 1 <= count <= 1000:
                raise ApiError("source page count is outside 1..1000")
            return count
    raise ApiError("unable to determine PDF page count")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="paperdaily supplement")
    result.add_argument(
        "action",
        choices=(
            "context",
            "create",
            "claim",
            "renew",
            "prepare",
            "validate",
            "submit",
            "receipt",
            "revise",
            "list",
            "cancel",
            "source-propose",
        ),
    )
    result.add_argument("--paper")
    result.add_argument("--task")
    result.add_argument("--out", type=Path)
    result.add_argument("--dir", type=Path)
    result.add_argument("--baseline")
    result.add_argument("--goals", default="methods,results")
    result.add_argument("--source", type=Path)
    result.add_argument("--json", action="store_true")
    result.add_argument("--verify", action="store_true")
    result.add_argument(
        "--reclaim", action="store_true", help="explicitly acquire a new lease after expiry"
    )
    result.add_argument("--dry-run", action="store_true")
    result.add_argument(
        "--allow-upload",
        action="store_true",
        help="use only after the user authorized uploading this derived analysis",
    )
    return result


def main(client: Client | None, argv: list[str]) -> dict[str, Any]:
    args = parser().parse_args(argv)
    if args.action == "source-propose":
        raise ApiError("public-source contributions are not enabled; no source submitted")
    if args.action in {"validate", "submit"} and args.dry_run or args.action == "validate":
        return validate_local(args.dir or args.out)
    if client is None:
        raise ApiError("API credentials required for this action")
    capability = client.get("/capabilities")
    if not capability.get("paper_supplements", {}).get(
        "enabled"
    ) or "supplement.v1" not in capability["paper_supplements"].get("schema_versions", []):
        raise ApiError("server does not support supplement.v1; local artifacts retained")
    if args.action == "list":
        return client.get(endpoint("/me/paper-supplements", paper_id=args.paper))
    if args.action == "context":
        if not args.paper:
            raise ApiError("--paper is required")
        data = client.get("/papers/" + path_segment(args.paper) + "/supplement-context")
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            write_json(args.out / "baseline.json", data)
        return data
    folder = args.dir or args.out
    if folder is None:
        raise ApiError("--out or --dir is required")
    folder.mkdir(parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ApiError("session directory must not be a symlink")
    task_path = folder / "task.json"
    if args.action in {"create", "revise"}:
        if task_path.exists():
            raise ApiError("task.json already exists; use a new session directory")
        prior = None
        paper = args.paper
        if args.action == "revise":
            if not args.task:
                raise ApiError("--task is required")
            prior = client.get("/me/paper-supplements/" + path_segment(args.task))
            paper = prior["paper_id"]
        if not paper:
            raise ApiError("--paper is required")
        baseline = client.get("/papers/" + path_segment(paper) + "/supplement-context")
        if args.baseline and args.baseline != baseline["baseline_hash"]:
            raise ApiError("baseline changed; inspect the new context")
        body = {
            "paper_id": paper,
            "baseline_hash": baseline["baseline_hash"],
            "goals": args.goals.split(","),
        }
        if prior:
            body["revision_of"] = prior["id"]
        task = client.request(
            "POST",
            "/me/paper-supplements",
            body,
            idempotent=True,
            extra_headers={"Idempotency-Key": secrets.token_hex(16)},
        )
        write_json(folder / "baseline.json", task["baseline"])
        write_json(task_path, {"origin": client.settings.base, "task": task})
        return {"id": task["id"], "status": task["status"], "local_directory": str(folder)}
    if args.action == "claim" and not task_path.exists():
        if not args.task:
            raise ApiError("--task is required")
        task = client.get("/me/paper-supplements/" + path_segment(args.task))
        write_json(task_path, {"origin": client.settings.base, "task": task})
        write_json(folder / "baseline.json", task["baseline"])
    saved = read_json(task_path)
    if saved["origin"] != client.settings.base:
        raise ApiError("session belongs to a different API origin")
    task = saved["task"]
    if args.task and args.task != task["id"]:
        raise ApiError("session already belongs to a different task")
    url = "/me/paper-supplements/" + path_segment(task["id"])
    lease_path = folder / ".lease"
    if args.action == "claim":
        if lease_path.exists() and not args.reclaim:
            lease = read_json(lease_path)
        else:
            lease = {"secret": secrets.token_hex(32), "claim_idem": secrets.token_hex(16)}
            save_private(lease_path, lease)
        response = client.request(
            "POST",
            url + "/claim",
            {},
            idempotent=True,
            extra_headers={
                "Idempotency-Key": lease["claim_idem"],
                "X-Supplement-Lease": lease["secret"],
            },
        )
        if datetime.fromisoformat(
            response["lease_expires_at"].replace("Z", "+00:00")
        ) <= datetime.now(timezone.utc):
            raise ApiError("previous lease expired; use claim --reclaim explicitly")
        lease["generation"] = response["lease_generation"]
        save_private(lease_path, lease)
        return {
            "id": task["id"],
            "status": response["status"],
            "lease_expires_at": response["lease_expires_at"],
        }
    if args.action == "prepare":
        if not args.source:
            raise ApiError("--source is required")
        source_hash, size = selected_file(args.source)
        count = pdf_pages(args.source)
        if (folder / "local-sources.json").exists() or (folder / "supplement.json").exists():
            raise ApiError("source artifacts already exist; preserve them or use a new revision")
        save_private(
            folder / "local-sources.json",
            {
                "sources": [
                    {
                        "source_id": "src1",
                        "path": str(args.source.resolve()),
                        "sha256": source_hash,
                        "page_count": count,
                        "bytes": size,
                    }
                ]
            },
        )
        write_json(
            folder / "supplement.json",
            {
                "schema_version": "supplement.v1",
                "paper_id": task["paper_id"],
                "baseline_hash": task["baseline_hash"],
                "outcome": "insufficient_material",
                "reason": "尚未完成阅读；没有可核查补充。",
                "provenance": {
                    "model": "REPLACE_WITH_ACTUAL_MODEL",
                    "skill_version": "0.8.0",
                    "executed_at": utc_now(),
                },
                "note_md": "尚未完成阅读。请按实际材料与已读范围填写补充分析。",
                "sources": [],
                "findings": [
                    {
                        "field": "methods",
                        "operation": "add",
                        "statement": "当前尚无可核查补充。",
                        "status": "gap",
                        "evidence": [],
                    }
                ],
            },
        )
        return {
            "status": "prepared_not_read",
            "page_count": count,
            "source_id": "src1",
            "next": "Read the selected source and fill supplement.json; no reading record has been created.",
        }
    if args.action == "cancel":
        return client.request(
            "POST",
            url + "/cancel",
            {},
            idempotent=True,
            extra_headers={"Idempotency-Key": secrets.token_hex(16)},
        )
    if args.action == "receipt":
        return verify_receipt(client, folder)
    lease = read_json(lease_path)
    headers = {
        "Idempotency-Key": secrets.token_hex(16),
        "X-Supplement-Lease": lease["secret"],
        "X-Supplement-Generation": str(lease["generation"]),
    }
    if args.action == "renew":
        return client.request("POST", url + "/renew", {}, idempotent=True, extra_headers=headers)
    if args.action == "submit":
        validate_local(folder)
        if not args.allow_upload:
            raise ApiError(
                "upload not authorized; review the derived payload and use --allow-upload only after user authorization"
            )
        body = read_json(folder / "supplement.json")
        headers["Idempotency-Key"] = hashlib.sha256(
            canonical({"task": task["id"], "payload": body})
        ).hexdigest()
        receipt = client.request(
            "POST", url + "/submissions", body, idempotent=True, extra_headers=headers
        )
        write_json(
            folder / "upload_receipt.json",
            {"origin": client.settings.base, "receipt": receipt, "verified": False},
        )
        result = verify_receipt(client, folder)
        lease_path.unlink(missing_ok=True)
        return result
    raise ApiError("unsupported action")


def validate_local(folder: Path | None) -> dict[str, Any]:
    if folder is None:
        raise ApiError("--dir is required")
    try:
        import jsonschema
        from pd_supplement_contract import validate_request
    except ImportError as exc:
        raise ApiError(
            "supplement validation requires jsonschema; run with uv run --with jsonschema"
        ) from exc
    saved = read_json(folder / "task.json")
    task = saved["task"]
    body = read_json(folder / "supplement.json")
    error = validate_request(
        "Submission",
        body,
        {
            "paper_id": task["paper_id"],
            "baseline_hash": task["baseline_hash"],
            "baseline": task["baseline"],
        },
    )
    if error:
        raise ApiError("supplement validation failed: " + error)
    if body["provenance"]["model"].startswith("REPLACE_"):
        raise ApiError("record the actual model and actual reading before uploading")
    local = (
        read_json(folder / "local-sources.json")
        if (folder / "local-sources.json").exists()
        else {"sources": []}
    )
    registered = {item["source_id"]: item for item in local["sources"]}
    for source in body["sources"]:
        if source["source_id"] not in registered:
            raise ApiError("source has no explicitly selected local file")
        entry = registered[source["source_id"]]
        actual, _ = selected_file(Path(entry["path"]))
        if actual != source["sha256"] or actual != entry["sha256"]:
            raise ApiError("source changed; reading record must be reviewed again")
        if (
            source["carrier"] == "binary-pdf"
            and pdf_pages(Path(entry["path"])) != source["page_count"]
        ):
            raise ApiError("PDF page count does not match the source")
    return {
        "status": "structure_checked",
        "verification_level": "client_declared_structure_checked",
        "source_count": len(body["sources"]),
        "outcome": body["outcome"],
    }


def verify_receipt(client: Client, folder: Path) -> dict[str, Any]:
    saved = read_json(folder / "upload_receipt.json")
    task = read_json(folder / "task.json")["task"]
    if saved["origin"] != client.settings.base:
        raise ApiError("receipt belongs to a different API origin")
    detail = client.get("/me/paper-supplements/" + path_segment(task["id"]))
    body = read_json(folder / "supplement.json")
    expected = hashlib.sha256(
        canonical(
            {
                "task_binding": {
                    "paper_id": task["paper_id"],
                    "baseline_hash": task["baseline_hash"],
                    "revision_of": task.get("revision_of"),
                },
                "payload": body,
            }
        )
    ).hexdigest()
    if (
        detail.get("submission") != body
        or detail.get("receipt", {}).get("artifact_hash") != expected
        or saved["receipt"].get("artifact_hash") != expected
    ):
        raise ApiError("saved content differs from the local supplement; receipt not verified")
    write_json(folder / "upload_receipt.json", {**saved, "verified": True})
    return {"status": "saved_and_read_back", "task_id": task["id"], "receipt": detail["receipt"]}
