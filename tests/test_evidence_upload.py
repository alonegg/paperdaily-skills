"""Synthetic local artifacts exercise evidence gates and upload receipts offline."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "paperdaily").is_dir())
sys.path.insert(0, str(ROOT / "paperdaily-deep-research" / "scripts"))
import upload_session as upload
from pd_runtime import content_hash


class EvidenceUpload(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "notes").mkdir()
        (self.root / "synthesis").mkdir()
        self.write("worklist.jsonl", {"id": "EX1", "title": "Synthetic example"})
        (self.root / "notes" / "EX1.md").write_text(
            "**paper_id**: EX1\n**depth**: fulltext\nSynthetic finding."
        )
        source = b"%PDF-1.4\nSynthetic fixture, not research evidence."
        (self.root / "source.pdf").write_bytes(source)
        self.manifest = {
            "papers": [
                {
                    "paper_id": "EX1",
                    "depth": "fulltext",
                    "reading_complete": True,
                    "model": "test-fixture",
                    "created_at": "2026-10-22T00:00:00Z",
                    "source": {
                        "path": "source.pdf",
                        "sha256": hashlib.sha256(source).hexdigest(),
                        "carrier": "binary-pdf",
                    },
                    "page_count": 2,
                    "pages_read": [1, 2],
                    "anchors": [{"location": "p.2", "quote": "Synthetic finding"}],
                }
            ],
            "skipped": [],
        }
        self.claim = {
            "claim": "Synthetic claim",
            "status": "supported",
            "evidence": [{"paper_id": "EX1", "location": "p.2", "quote": "Synthetic finding"}],
        }
        (self.root / "report.md").write_text("# Synthetic report\nOnly a test fixture.")
        self.save()

    def write(self, name, value):
        (self.root / name).write_text(json.dumps(value) + "\n")

    def save(self):
        self.write("reading_manifest.json", self.manifest)
        self.write("synthesis/claims.jsonl", self.claim)

    def build(self):
        self.save()
        return upload.build_payload(str(self.root), None, None)

    def rejected(self, match):
        payload, problems = self.build()
        self.assertIsNone(payload)
        self.assertTrue(any(match in p for p in problems), problems)

    def run_upload(self, args=()):
        self.save()
        with patch.dict(os.environ, {"PD_BASE": "https://example.org/api/v1", "PD_KEY": "fixture"}):
            with contextlib.redirect_stderr(io.StringIO()):
                return upload.main(["--dir", str(self.root), *args])

    def test_valid_fulltext(self):
        payload, problems = self.build()
        self.assertEqual(problems, [])
        self.assertEqual(payload["papers"][0]["depth"], "fulltext")

    def test_missing_selected_note(self):
        (self.root / "worklist.jsonl").write_text('{"id":"EX1"}\n{"id":"EX2"}\n')
        self.rejected("neither note nor skip")

    def test_explicit_skip_is_reported_not_silently_dropped(self):
        (self.root / "worklist.jsonl").write_text('{"id":"EX1"}\n{"id":"EX2"}\n')
        self.manifest["skipped"] = [{"paper_id": "EX2", "reason": "No usable material"}]
        self.assertEqual(self.build()[1], [])

    def test_selected_subset_is_the_coverage_denominator(self):
        (self.root / "worklist.jsonl").write_text('{"id":"EX1"}\n{"id":"EX2"}\n')
        self.write("worklist.selected.jsonl", {"id": "EX1"})
        self.assertEqual(self.build()[1], [])

    def test_foreign_claim_id(self):
        self.claim["evidence"][0]["paper_id"] = "EX_OTHER"
        self.rejected("without a registered note")

    def test_anchor_must_match_reading_record(self):
        self.claim["evidence"][0]["quote"] = "Invented claim"
        self.rejected("no matching reading anchor")

    def test_download_alone_is_not_fulltext_reading(self):
        self.write("fetch_report.jsonl", {"id": "EX1", "status": "ok", "carrier": "binary-pdf"})
        self.manifest["papers"][0]["reading_complete"] = False
        self.rejected("completed reading record")

    def test_fulltext_requires_all_pdf_pages(self):
        self.manifest["papers"][0]["pages_read"] = [2]
        self.rejected("all pages read")

    def test_partial_is_not_fulltext(self):
        self.manifest["papers"][0]["source"]["partial"] = True
        self.rejected("complete fulltext carrier")

    def test_source_hash_change(self):
        (self.root / "source.pdf").write_bytes(b"changed")
        self.rejected("source hash mismatch")

    def test_source_cannot_escape_session(self):
        self.manifest["papers"][0]["source"]["path"] = "../elsewhere.pdf"
        self.rejected("inside the session")

    def test_abstract_cannot_cite_pdf_pages(self):
        self.manifest["papers"][0]["depth"] = "abstract-only"
        self.rejected("cannot assert PDF page")

    def test_registered_external_note_allowed(self):
        self.write("worklist.jsonl", {"id": "EX0"})
        self.write("worklist.selected.jsonl", {"id": "EX0"})
        self.write(
            "external_sources.jsonl", {"id": "EX1", "title": "Fixture", "reason": "External source"}
        )
        self.manifest["skipped"] = [{"paper_id": "EX0", "reason": "No abstract"}]
        self.assertEqual(self.build()[1], [])

    def test_stale_selected_list_is_not_accepted_after_replace(self):
        self.write("worklist.selected.jsonl", {"id": "STALE"})
        self.rejected("subset of worklist")

    def test_gap_does_not_need_fabricated_evidence(self):
        self.claim.update(status="gap", evidence=[])
        self.assertEqual(self.build()[1], [])

    def test_nongap_without_evidence_fails(self):
        self.claim["evidence"] = []
        self.rejected("non-gap")

    def test_bad_evidence_shape_reports_gate_error(self):
        self.claim["evidence"] = 42
        self.rejected("evidence must be an array")

    def test_dry_run_never_posts(self):
        with patch.object(upload, "post_session") as post:
            self.assertEqual(self.run_upload(["--dry-run"]), 0)
            post.assert_not_called()

    def test_receipt_written_only_after_success(self):
        with patch.object(upload, "post_session", return_value=(201, '{"id":"saved"}')):
            self.assertEqual(self.run_upload(), 0)
        receipt = json.loads((self.root / "upload_receipt.json").read_text())
        self.assertEqual(receipt["session_id"], "saved")
        self.assertEqual(
            receipt["artifact_hash"], content_hash(upload.artifact_content(self.build()[0]))
        )

    def test_changed_artifacts_require_revision_before_any_post(self):
        self.write(
            "upload_receipt.json",
            {"base": "https://example.org/api/v1", "session_id": "prior", "artifact_hash": "old"},
        )
        with patch.object(upload, "post_session") as post:
            self.assertEqual(self.run_upload(), 3)
            post.assert_not_called()

    def test_duplicate_different_content_is_failure(self):
        with patch.object(
            upload, "post_session", return_value=(200, '{"id":"old","deduplicated":true}')
        ):
            with patch.object(upload, "verify_duplicate", return_value=False):
                self.assertEqual(self.run_upload(), 3)
        self.assertFalse((self.root / "upload_receipt.json").exists())

    def test_duplicate_same_content_is_success(self):
        with patch.object(
            upload, "post_session", return_value=(200, '{"id":"old","deduplicated":true}')
        ):
            with patch.object(upload, "verify_duplicate", return_value=True):
                self.assertEqual(self.run_upload(), 0)

    def test_revision_unavailable_does_not_fallback(self):
        with patch.object(upload, "post_session", return_value=(404, "not found")) as post:
            self.assertEqual(self.run_upload(["--revise", "parent"]), 3)
            post.assert_called_once()
            self.assertEqual(post.call_args.kwargs["revision_of"], "parent")
        self.assertFalse((self.root / "upload_receipt.json").exists())

    def test_duplicate_reads_actual_body_md_field(self):
        payload, _ = self.build()
        remote = dict(payload)
        remote["notes"] = [
            {"paper_id": p["paper_id"], "depth": p["depth"], "body_md": p["note_md"]}
            for p in payload["papers"]
        ]
        with patch.object(upload.Client, "get", return_value=remote):
            self.assertTrue(
                upload.verify_duplicate(
                    "https://example.org/api/v1", "fixture", "parent", payload, 30
                )
            )

    def test_invalid_receipt_does_not_crash(self):
        self.write("upload_receipt.json", [])
        self.assertEqual(self.run_upload(), 3)


if __name__ == "__main__":
    unittest.main()
