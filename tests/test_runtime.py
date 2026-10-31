"""Offline contract tests shared by the source and assembled public packages."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "paperdaily").is_dir())
RUNTIME = ROOT / "_shared"
if not RUNTIME.is_dir():
    RUNTIME = ROOT / "paperdaily" / "lib"
sys.path.insert(0, str(RUNTIME))
import pd_commands as commands
from pd_runtime import ApiError, Client, Settings, collapse, retry_after


class Handler(BaseHTTPRequestHandler):
    routes: dict = {}
    calls: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        query = parse_qs(urlsplit(self.path).query)
        self.calls.append((self.command, self.path, json.loads(body) if body else None))
        route = self.routes.get(urlsplit(self.path).path, (404, {"detail": "fixture miss"}, {}))
        status, data, headers = (
            route(query, json.loads(body) if body else None) if callable(route) else route
        )
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, str(v))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%s" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.routes = {}
        Handler.calls = []
        self.client = Client(Settings(self.base, "fixture-only", throttle=0, retries=0))

    def route(self, path, data, status=200, headers=None):
        Handler.routes[path] = (status, data, headers or {})

    def args(self, command, *values):
        return commands.parser_for(command).parse_args(list(values))

    def test_field_503_is_not_empty_success(self):
        self.route("/papers", {"detail": "timeout"}, 503)
        with self.assertRaises(ApiError):
            commands.field(self.client, self.args("field", "17", "--no-synth"))

    def test_field_valid_empty_is_success(self):
        self.route("/papers", {"items": [], "next_cursor": None})
        result = commands.field(self.client, self.args("field", "17"))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["items"], [])

    def test_error_envelope_with_200_is_not_empty(self):
        self.route("/papers", {"detail": "bad response"})
        with self.assertRaises(ApiError):
            commands.field(self.client, self.args("field", "17"))

    def test_topic_429_is_reported(self):
        self.route("/taxonomy/topics", {"detail": "quota"}, 429)
        with self.assertRaises(ApiError):
            commands.topics(self.client, self.args("topics", "AI"))

    def test_deep_topic_scan_marks_partial(self):
        self.route("/taxonomy/subfields", [{"id": "1702"}, {"id": "1703"}])
        Handler.routes["/taxonomy/topics"] = lambda q, _: (
            (200, [{"id": "T1", "display_name": "AI", "paper_count": 1}], {})
            if q["subfield_id"] == ["1702"]
            else (429, {}, {})
        )
        result = commands.topics(self.client, self.args("topics", "AI", "--deep-topic-scan"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["items"]), 1)

    def test_pacing_survives_calls(self):
        self.route("/ok", {"ok": True})
        waits = []
        now = [1.0]

        def sleep(value):
            waits.append(value)
            now[0] += value

        client = Client(
            Settings(self.base, "fixture", throttle=1.05, retries=0),
            sleep=sleep,
            clock=lambda: now[0],
        )
        for _ in range(3):
            client.get("/ok")
        self.assertEqual(len(waits), 2)
        self.assertTrue(all(abs(v - 1.05) < 0.001 for v in waits))

    def test_retry_after_respected(self):
        count = [0]

        def route(*_):
            count[0] += 1
            return (429, {}, {"Retry-After": "7"}) if count[0] == 1 else (200, {"ok": True}, {})

        Handler.routes["/retry"] = route
        waits = []
        client = Client(Settings(self.base, "fixture", throttle=0, retries=1), sleep=waits.append)
        self.assertEqual(client.get("/retry"), {"ok": True})
        self.assertEqual(waits, [7.0])

    def test_long_retry_after_does_not_retry_early(self):
        self.route("/retry", {}, 503, {"Retry-After": "600"})
        client = Client(Settings(self.base, "fixture", throttle=0, retries=2))
        with self.assertRaises(ApiError):
            client.get("/retry")
        self.assertEqual(len(Handler.calls), 1)

    def test_http_date_retry_after(self):
        value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=15), usegmt=True)
        self.assertGreater(retry_after(value, 1), 12)

    def test_write_not_retried(self):
        self.route("/me/watchlists", {}, 503)
        client = Client(Settings(self.base, "fixture", throttle=0))
        with self.assertRaises(ApiError):
            client.request("POST", "/me/watchlists", {"name": "fixture"})
        self.assertEqual(len(Handler.calls), 1)

    def test_redirect_refused(self):
        self.route("/redirect", {}, 302, {"Location": self.base + "/leak"})
        with self.assertRaises(ApiError):
            self.client.get("/redirect")
        self.assertEqual(len(Handler.calls), 1)

    def test_html_success_refused(self):
        self.route("/html", b"<html>proxy login</html>")
        with self.assertRaises(ApiError):
            self.client.get("/html")

    def test_plaintext_public_base_refused(self):
        with self.assertRaises(ApiError):
            Client(Settings("http://example.com/api/v1", "fixture"))

    def test_path_escape_refused(self):
        for path in ("//example.com", "/%2e%2e/admin"):
            with self.assertRaises(ApiError):
                self.client.get(path)

    def test_seed_filters_all_channels_and_auto_is_not_sent(self):
        self.route("/papers/resolve", {"id": "W0", "title": "Seed"})
        self.route(
            "/papers/W0/similar",
            {"items": [{"paper": {"id": "W1", "title": "Old"}, "distance": 0.01}]},
        )
        self.route(
            "/papers/search", {"items": [{"id": "W2", "title": "New"}], "layer_used": "semantic"}
        )
        self.route(
            "/papers/batch",
            {
                "items": [
                    {"id": "W1", "title": "Old", "publication_date": "2010-01-01"},
                    {"id": "W2", "title": "New", "publication_date": "2026-01-01"},
                ]
            },
        )
        result = commands.search(
            self.client,
            self.args("search", "--paper", "W0", "--scope", "auto", "--year-from", "2023"),
        )
        self.assertEqual([r["id"] for r in result["items"]], ["W2"])
        self.assertFalse(any("subfield_id=auto" in c[1] for c in Handler.calls))

    def test_batch_hydration_keeps_group_identity(self):
        self.route(
            "/papers/search", {"items": [{"id": "W1"}, {"id": "W2"}], "layer_used": "semantic"}
        )
        self.route(
            "/papers/batch",
            {
                "items": [
                    {
                        "id": "W1",
                        "title": "Working paper",
                        "paper_group_id": "G",
                        "is_canonical": False,
                    },
                    {
                        "id": "W2",
                        "title": "Final paper",
                        "paper_group_id": "G",
                        "is_canonical": True,
                    },
                ]
            },
        )
        result = commands.search(self.client, self.args("search", "a research question"))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["id"], "W2")
        self.assertEqual(result["items"][0]["all_ids"], ["W1", "W2"])

    def test_same_title_not_merged_without_identity(self):
        rows = [
            {"id": "W1", "title": "Introduction", "arxiv_id": "2401.00001"},
            {"id": "W2", "title": "Introduction", "arxiv_id": "2401.00002"},
        ]
        self.assertEqual(len(collapse(rows)), 2)

    def test_hard_identity_collapse_is_transitive(self):
        rows = [
            {"id": "A", "paper_group_id": "G1"},
            {"id": "B", "doi": "10.1/x"},
            {"id": "C", "paper_group_id": "G1", "doi": "10.1/x"},
        ]
        self.assertEqual(len(collapse(rows)), 1)

    def test_author_http_error_is_not_an_author_match(self):
        self.route("/authors", {"detail": "invalid name"}, 400)
        with self.assertRaises(ApiError) as caught:
            commands.author(self.client, self.args("author", "Dell'Acqua"))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("--author-id", str(caught.exception))
        self.assertIn("does not mean no author exists", str(caught.exception))
        self.assertEqual(len(Handler.calls), 1)

    def test_author_error_object_with_200_is_rejected(self):
        self.route("/authors", {"detail": "fixture error"})
        with self.assertRaisesRegex(ApiError, "valid items array"):
            commands.author(self.client, self.args("author", "Dell'Acqua"))
        self.assertEqual(len(Handler.calls), 1)

    def test_author_affiliation_shapes_are_not_jq_program_input(self):
        for affiliations in (["University"], "University", None):
            self.route("/authors", [{"id": "A1", "name": "Dell'Acqua", "affiliations": affiliations}])
            self.route("/authors/A1/papers", {"items": []})
            result = commands.author(self.client, self.args("author", "Dell'Acqua"))
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["authors"][0]["affiliations"], affiliations)

    def test_author_ambiguity_requires_selection(self):
        self.route("/authors", [{"id": "A1", "name": "Li"}, {"id": "A2", "name": "Li"}])
        result = commands.author(self.client, self.args("author", "Li"))
        self.assertEqual(result["status"], "needs_selection")
        self.assertEqual(len(Handler.calls), 1)

    def test_author_orcid_route(self):
        self.route("/authors/by-orcid/0000-0000-0000-000X", {"id": "A1"})
        self.route("/authors/A1/papers", {"items": []})
        self.assertEqual(
            commands.author(self.client, self.args("author", "--orcid", "0000-0000-0000-000X"))[
                "status"
            ],
            "complete",
        )

    def test_no_default_synthesis(self):
        self.route("/papers", {"items": []})
        commands.dispatch("field", self.args("field", "17"), self.client)
        self.assertFalse(any(c[1] == "/ask" for c in Handler.calls))

    def test_exact_date_filter(self):
        args = self.args("search", "question", "--since", "2026-10-20", "--until", "2026-10-22")
        rows = [
            {"id": "1", "publication_date": "2026-10-19"},
            {"id": "2", "publication_date": "2026-10-21"},
            {"id": "3", "publication_date": None},
        ]
        kept, excluded = commands.filtered(rows, args)
        self.assertEqual([r["id"] for r in kept], ["2"])
        self.assertEqual(excluded, 2)

    def test_batch_chunks_at_100(self):
        Handler.routes["/papers/batch"] = lambda _, body: (
            200,
            {"items": [{"id": i} for i in body["ids"]]},
            {},
        )
        result = self.client.batch(["W%d" % i for i in range(105)])
        self.assertEqual(len(result), 105)
        self.assertEqual([len(c[2]["ids"]) for c in Handler.calls], [100, 5])

    def test_main_partial_returns_nonzero(self):
        self.route("/papers/search", {}, 503)
        with (
            patch.object(commands, "load_settings", return_value=self.client.settings),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = commands.main("search", ["a question", "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output.getvalue())["status"], "partial")

    def test_scope_resolves_real_detail_topic_names(self):
        self.route(
            "/taxonomy/subfields", [{"id": "1702", "display_name": "Artificial Intelligence"}]
        )
        rows = [{"id": "W1", "primary_topic": {"subfield_name": "Artificial Intelligence"}}]
        args = self.args("search", "question", "--scope", "1702")
        resolved = commands.scope_rows(self.client, rows, args)
        self.assertEqual(commands.filtered(resolved, args)[0][0]["id"], "W1")
        self.assertNotIn("subfield_id", rows[0])

    def test_worklist_stores_details_context_and_no_default_ask(self):
        self.route(
            "/papers/resolve", {"id": "S", "title": "Seed", "publication_date": "2020-01-01"}
        )
        self.route(
            "/papers/S/similar",
            {"items": [{"paper": {"id": "W1"}, "distance": 0.1}], "truncated": True},
        )
        self.route("/papers/search", {"items": [], "layer_used": "semantic"})
        self.route(
            "/papers/batch",
            {
                "items": [
                    {
                        "id": "W1",
                        "paper_group_id": "g1",
                        "is_canonical": True,
                        "abstract": "actual detail",
                        "publication_date": "2025-01-01",
                    }
                ]
            },
        )
        self.route("/papers/S/context-bundle", {"paper": {"id": "S"}})
        with tempfile.TemporaryDirectory() as directory:
            args = self.args("worklist", "--paper", "S", "--year-from", "2023", "--out", directory)
            result = commands.worklist(self.client, args)
            self.assertEqual({r["id"] for r in result["items"]}, {"S", "W1"})
            self.assertEqual(result["status"], "complete")
            details = json.loads((Path(directory) / "worklist.extractions.json").read_text())
            self.assertTrue(any(r.get("abstract") == "actual detail" for r in details["items"]))
            self.assertTrue((Path(directory) / "context" / "S.json").is_file())
            with self.assertRaises(ApiError):
                commands.worklist(self.client, args)
        self.assertFalse(any(c[1] == "/ask" for c in Handler.calls))

    def test_append_preserves_earlier_extractions(self):
        self.route("/papers", {"items": []})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worklist.jsonl").write_text('{"id":"OLD","source":"primary"}\n')
            (root / "worklist.extractions.json").write_text(
                '{"items":[{"id":"OLD","abstract":"kept","methods":[{"name":"M"}]}]}'
            )
            args = self.args("worklist", "17", "--append", "--out", directory)
            commands.worklist(self.client, args)
            detail = json.loads((root / "worklist.extractions.json").read_text())["items"][0]
            self.assertEqual(detail["abstract"], "kept")
            self.assertEqual(detail["methods"][0]["name"], "M")

    def test_bibliography_error_envelope_rejected(self):
        self.route("/papers/match", {"detail": "failure"})
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "request.json"
            file.write_text('{"items":[{"ref_id":"r1","title":"Fixture"}]}')
            with self.assertRaises(ApiError):
                commands.dispatch(
                    "bibliography",
                    self.args("bibliography", "match", "--input", str(file)),
                    self.client,
                )

    def test_bibliography_text_keeps_ambiguity(self):
        output = commands.render(
            {
                "items": [
                    {"ref_id": "r1", "status": "ambiguous", "candidates": [{"record_id": "W1"}]}
                ]
            }
        )
        self.assertIn("ambiguous", output)
        self.assertIn("W1", output)

    def test_no_auth_required_for_help(self):
        with self.assertRaises(SystemExit) as exc:
            with contextlib.redirect_stdout(io.StringIO()):
                commands.main("worklist", ["--help"])
        self.assertEqual(exc.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
