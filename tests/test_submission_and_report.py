"""Submitted-claim immutability, supplements, persistence, and reporting."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from shipment_claims.archive import ArchiveError, ClaimArchive
from shipment_claims.models import build_source
from shipment_claims.report import render_case, render_index


def archive_with_sources() -> ClaimArchive:
    archive = ClaimArchive()
    for item in (
        {
            "id": "d1",
            "shipment_id": "SHP-9",
            "kind": "driver_note",
            "received_at": "2026-11-02T09:00:00+08:00",
            "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。",
        },
        {
            "id": "c1",
            "shipment_id": "SHP-9",
            "kind": "carrier_message",
            "received_at": "2026-11-02T11:00:00+08:00",
            "text": "Manifest shows handover at East Depot, not proof of delivery to consignee.",
        },
    ):
        archive.add_source(build_source(item))
    return archive


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.archive = archive_with_sources()
        self.claim = self.archive.submit_claim(
            claim_id="CLM-1",
            shipment_id="SHP-9",
            summary="仓库暂存、未见签收；等待承运商说明交接后去向。",
            created_by="handler",
        )

    def test_original_version_is_immutable(self):
        with self.assertRaises(ArchiveError):
            self.archive.submit_claim(
                claim_id="CLM-1", shipment_id="SHP-9", summary="改摘要", created_by="other"
            )
        self.assertEqual(self.archive.submissions_for("SHP-9")[0].summary if False else
                         self.archive.submissions_for("SHP-9")[0].original.summary,
                         "仓库暂存、未见签收；等待承运商说明交接后去向。")

    def test_version_digest_detects_tampering(self):
        claim = self.archive.submissions_for("SHP-9")[0]
        self.assertTrue(claim.original.digest_is_valid())
        tampered = claim.original.__class__(
            version=claim.original.version,
            kind=claim.original.kind,
            material=claim.original.material,
            summary="静默改写后的摘要",
            extractor_version=claim.original.extractor_version,
            digest=claim.original.digest,
            created_at=claim.original.created_at,
            created_by=claim.original.created_by,
        )
        self.assertFalse(tampered.digest_is_valid())

    def test_supplement_appends_without_rewriting(self):
        self.archive.add_source(
            build_source(
                {
                    "id": "s1",
                    "shipment_id": "SHP-9",
                    "kind": "scan_correction",
                    "received_at": "2026-11-04T10:00:00+08:00",
                    "text": "Corrected scan: transfer recorded at 19:05.",
                }
            )
        )
        updated = self.archive.supplement_claim(
            "CLM-1", summary="补入承运商更正扫描。", created_by="handler"
        )
        self.assertEqual(len(updated.versions), 2)
        self.assertEqual(updated.original.summary, "仓库暂存、未见签收；等待承运商说明交接后去向。")
        self.assertEqual(updated.latest.kind, "supplement")
        self.assertEqual([item.source_id for item in updated.latest.material], ["s1"])

    def test_supplement_with_no_new_material_is_refused(self):
        with self.assertRaises(ArchiveError):
            self.archive.supplement_claim(
                "CLM-1", summary="重复材料", created_by="handler", source_ids=["d1"]
            )

    def test_drift_reports_later_sources_without_editing_version(self):
        self.archive.add_source(
            build_source(
                {
                    "id": "r1",
                    "shipment_id": "SHP-9",
                    "kind": "recipient_email",
                    "received_at": "2026-11-05T10:00:00+08:00",
                    "text": "收货方称仍未收到。",
                }
            )
        )
        claim = self.archive.submissions_for("SHP-9")[0]
        drift = self.archive.submission_drift(claim)
        self.assertTrue(any("r1" in note for note in drift))
        self.assertEqual(len(claim.versions), 1)


class PersistenceTests(unittest.TestCase):
    def test_roundtrip_preserves_sources_links_reviews_and_submissions(self):
        archive = archive_with_sources()
        archive.add_source(
            build_source(
                {
                    "id": "map",
                    "shipment_id": "SHP-9-OLD",
                    "kind": "carrier_mapping",
                    "text": "Renumber: SHP-9-OLD replaced by SHP-9.",
                }
            )
        )
        link = archive.proposed_link(
            "SHP-9-OLD", "SHP-9", evidence_ids=["map"], rationale="notice", created_by="h"
        )
        archive.verify_link(link.link_id, verified_by="reviewer")
        archive.record_review(
            shipment_id="SHP-9", kind="deadline", note="核对", actor="reviewer"
        )
        archive.submit_claim(
            claim_id="CLM-9", shipment_id="SHP-9", summary="摘要", created_by="handler"
        )
        archive.add_pending(
            shipment_id="SHP-9",
            question="是否签收？",
            target_party="carrier",
            created_by="handler",
            evidence_ids=["d1"],
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.json"
            archive.save(path)
            loaded = ClaimArchive.from_export(path)

        self.assertEqual(len(loaded.sources), 3)
        self.assertEqual(loaded.resolve_id("SHP-9-OLD"), "SHP-9")
        self.assertEqual(len(loaded.submissions_for("SHP-9")), 1)
        self.assertEqual(len(loaded.pending_for("SHP-9")), 1)
        self.assertEqual(len(loaded.events_for("SHP-9")), 4)
        self.assertEqual(loaded.case_view("SHP-9").to_dict()["canonical_id"], "SHP-9")

    def test_rebuild_events_is_deterministic_across_loads(self):
        archive = archive_with_sources()
        document = archive.to_document()
        first = ClaimArchive.from_document(document).events_for("SHP-9")
        second = ClaimArchive.from_document(document).events_for("SHP-9")
        self.assertEqual(
            [(event.event_id, event.role, event.status) for event in first],
            [(event.event_id, event.role, event.status) for event in second],
        )


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.archive = archive_with_sources()
        self.archive.add_source(
            build_source(
                {
                    "id": "r1",
                    "shipment_id": "SHP-9",
                    "kind": "recipient_email",
                    "received_at": "2026-11-03T08:00:00+08:00",
                    "text": "邮件称货物未到，猜测司机漏送；尚无开箱或卸货记录。",
                }
            )
        )
        self.view = self.archive.case_view("SHP-9", today=date(2026, 11, 6))
        self.text = render_case(self.view, self.archive)

    def test_report_marks_inference_and_source(self):
        self.assertIn("推断", self.text)
        self.assertIn("不可当已确认事实", self.text)
        self.assertIn("r1", self.text)

    def test_report_labels_deadline_basis(self):
        self.assertIn("时限复核", self.text)
        self.assertIn("起点依据", self.text)
        self.assertIn("不是法律时限意见", self.text)

    def test_report_lists_evidence_and_pending(self):
        self.assertIn("证据来源", self.text)
        self.assertIn("待承运商确认", self.text)

    def test_index_reports_counts_and_warnings(self):
        index = render_index(self.archive)
        self.assertIn("SHP-9", index)
        self.assertIn("来源 3", index)


if __name__ == "__main__":
    unittest.main()
