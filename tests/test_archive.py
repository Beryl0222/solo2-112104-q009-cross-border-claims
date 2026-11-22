"""Archive integrity: immutability, corrections, identity links, reviews."""

import unittest
from datetime import date

from shipment_claims.archive import ArchiveError, ClaimArchive, IdMergeRefused
from shipment_claims.lexicon import EventRole
from shipment_claims.models import (
    DeadlinePolicy,
    EventStatus,
    ReviewState,
    Verification,
    parse_timestamp,
    build_source,
)


def make_archive() -> ClaimArchive:
    archive = ClaimArchive(policy=DeadlinePolicy(days_from_handover=14, due_soon_days=3))
    archive.add_source(
        build_source(
            {
                "id": "driver-1",
                "shipment_id": "SHP-1",
                "kind": "driver_note",
                "lang": "zh",
                "author": "司机 甲",
                "received_at": "2026-11-02T09:30:00+08:00",
                "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。",
            }
        )
    )
    archive.add_source(
        build_source(
            {
                "id": "scan-old",
                "shipment_id": "SHP-1",
                "kind": "scan",
                "lang": "en",
                "received_at": "2026-11-01T20:30:00+08:00",
                "observed_at": "2026-11-01T18:05:00+08:00",
                "text": "Scan: transfer recorded at 18:05, East Depot.",
            }
        )
    )
    return archive


class SourceImmutabilityTests(unittest.TestCase):
    def test_same_content_is_idempotent(self):
        archive = make_archive()
        same = build_source(
            {
                "id": "driver-1",
                "shipment_id": "SHP-1",
                "kind": "driver_note",
                "lang": "zh",
                "author": "司机 甲",
                "received_at": "2026-11-02T09:30:00+08:00",
                "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。",
            }
        )
        archive.add_source(same)
        self.assertEqual(len(archive.sources), 2)

    def test_conflicting_content_is_refused(self):
        archive = make_archive()
        changed = build_source(
            {
                "id": "driver-1",
                "shipment_id": "SHP-1",
                "kind": "driver_note",
                "text": "改口：已签收。",
            }
        )
        with self.assertRaises(ArchiveError):
            archive.add_source(changed)
        self.assertIn("11月1日", archive.source("driver-1").text)


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.archive = make_archive()
        self.archive.add_source(
            build_source(
                {
                    "id": "scan-correction",
                    "shipment_id": "SHP-1",
                    "kind": "scan_correction",
                    "lang": "en",
                    "received_at": "2026-11-04T10:00:00+08:00",
                    "corrects": ["scan-old"],
                    "reassigns_to": "ZX-4021",
                    "text": (
                        "Corrected scan: transfer recorded at 19:05; "
                        "prior 18:05 event belonged to parcel ZX-4021."
                    ),
                }
            )
        )

    def test_old_scan_events_are_withdrawn_not_deleted(self):
        events = {event.source_id: event for event in self.archive.events_for("SHP-1")}
        self.assertIn("scan-old", events)
        old = [event for event in self.archive.events_for("SHP-1")
               if event.source_id == "scan-old"][0]
        self.assertIn(old.status, {EventStatus.ATTRIBUTION_WITHDRAWN, EventStatus.SUPERSEDED})
        self.assertTrue(old.attribution_note)

    def test_clause_about_other_parcel_is_withdrawn(self):
        correction_events = [
            event for event in self.archive.events_for("SHP-1")
            if event.source_id == "scan-correction"
        ]
        reassigned = [event for event in correction_events if "ZX-4021" in event.text]
        self.assertTrue(reassigned)
        self.assertEqual(reassigned[0].status, EventStatus.ATTRIBUTION_WITHDRAWN)

    def test_corrected_handover_replaces_old_start(self):
        view = self.archive.case_view("SHP-1", today=date(2026, 11, 10))
        self.assertIsNotNone(view.deadline.chosen)
        self.assertEqual(view.deadline.chosen.source_id, "scan-correction")
        self.assertEqual(
            parse_timestamp(view.deadline.chosen.occurred_at).isoformat(),
            "2026-11-04T11:05:00+00:00",
        )
        excluded = [candidate for candidate in view.deadline.candidates if not candidate.usable]
        self.assertTrue(any(candidate.source_id == "scan-old" for candidate in excluded) or
                        all(c.source_id != "scan-old" for c in view.deadline.candidates))

    def test_non_speculative_events_cannot_confirm(self):
        self.archive.add_source(
            build_source(
                {
                    "id": "recipient-1",
                    "shipment_id": "SHP-1",
                    "kind": "recipient_email",
                    "text": "货物已送达，我认为司机完成了交付。",
                }
            )
        )
        recipient_delivery = [
            event for event in self.archive.events_for("SHP-1")
            if event.source_id == "recipient-1" and event.role is EventRole.DELIVERY
        ]
        self.assertTrue(recipient_delivery)
        self.assertFalse(any(event.can_confirm_delivery for event in recipient_delivery))


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.archive = make_archive()
        self.archive.add_source(
            build_source(
                {
                    "id": "mapping-1",
                    "shipment_id": "SHP-1-R",
                    "kind": "carrier_mapping",
                    "text": "Renumber notice: SHP-1-R replaced by SHP-1; history retained.",
                }
            )
        )

    def test_lookalike_ids_do_not_merge_by_default(self):
        self.assertEqual(self.archive.resolve_id("SHP-1-R"), "SHP-1-R")
        self.assertTrue(self.archive.similarity_warnings())

    def test_link_without_evidence_is_refused(self):
        with self.assertRaises(IdMergeRefused):
            self.archive.proposed_link(
                "SHP-1-R", "SHP-1", evidence_ids=[], rationale="looks similar", created_by="t"
            )

    def test_unverified_link_does_not_merge(self):
        link = self.archive.proposed_link(
            "SHP-1-R", "SHP-1", evidence_ids=["mapping-1"], rationale="notice", created_by="t"
        )
        self.assertEqual(link.status, Verification.UNVERIFIED)
        self.assertEqual(self.archive.resolve_id("SHP-1-R"), "SHP-1-R")

    def test_verified_link_merges_and_keeps_provenance(self):
        link = self.archive.proposed_link(
            "SHP-1-R", "SHP-1", evidence_ids=["mapping-1"], rationale="notice", created_by="t"
        )
        self.archive.verify_link(link.link_id, verified_by="reviewer")
        self.assertEqual(self.archive.resolve_id("SHP-1-R"), "SHP-1")
        view = self.archive.case_view("SHP-1-R")
        self.assertEqual(view.canonical_id, "SHP-1")
        self.assertEqual(view.shipment_id, "SHP-1-R")
        self.assertIn("mapping-1", [source.source_id for source in view.sources])

    def test_verification_requires_both_ids_in_one_source(self):
        link = self.archive.proposed_link(
            "SHP-1-R", "SHP-1", evidence_ids=["driver-1"], rationale="claimed", created_by="t"
        )
        with self.assertRaises(IdMergeRefused):
            self.archive.verify_link(link.link_id, verified_by="reviewer")

    def test_conflicting_verified_links_are_refused(self):
        self.archive.add_source(
            build_source(
                {
                    "id": "mapping-2",
                    "shipment_id": "SHP-1-R",
                    "kind": "carrier_mapping",
                    "text": "Renumber notice: SHP-1-R replaced by SHP-9.",
                }
            )
        )
        first = self.archive.proposed_link(
            "SHP-1-R", "SHP-1", evidence_ids=["mapping-1"], rationale="a", created_by="t"
        )
        self.archive.verify_link(first.link_id, verified_by="r")
        second = self.archive.proposed_link(
            "SHP-1-R", "SHP-9", evidence_ids=["mapping-2"], rationale="b", created_by="t"
        )
        self.archive.verify_link(second.link_id, verified_by="r")
        with self.assertRaises(IdMergeRefused):
            self.archive.resolve_id("SHP-1-R")


class DeadlineTests(unittest.TestCase):
    def test_due_soon_and_expired_are_distinct(self):
        archive = make_archive()
        archive.record_review(
            shipment_id="SHP-1", kind="deadline", note="核对过", actor="reviewer"
        )
        soon = archive.case_view("SHP-1", today=date(2026, 11, 12))
        self.assertEqual(soon.deadline.status.value, "due_soon")
        expired = archive.case_view("SHP-1", today=date(2026, 11, 20))
        self.assertEqual(expired.deadline.status.value, "expired")

    def test_unknown_when_no_usable_start(self):
        archive = ClaimArchive()
        archive.add_source(
            build_source(
                {
                    "id": "x",
                    "shipment_id": "SHP-X",
                    "kind": "recipient_email",
                    "text": "邮件称货物未到。",
                }
            )
        )
        view = archive.case_view("SHP-X", today=date(2026, 11, 22))
        self.assertEqual(view.deadline.status.value, "unknown")
        self.assertIsNone(view.deadline.chosen)

    def test_inference_never_starts_clock(self):
        archive = ClaimArchive()
        archive.add_source(
            build_source(
                {
                    "id": "guess",
                    "shipment_id": "SHP-Y",
                    "kind": "recipient_email",
                    "received_at": "2026-11-05T10:00:00+09:00",
                    "observed_at": "2026-11-04T18:00:00+09:00",
                    "text": "猜测昨天 18:00 已交接。",
                }
            )
        )
        view = archive.case_view("SHP-Y", today=date(2026, 11, 6))
        self.assertIsNone(view.deadline.chosen)
        self.assertTrue(any("推断" in reason for reason in view.deadline.reasons)
                        or any(not c.usable for c in view.deadline.candidates))


class ReviewStalenessTests(unittest.TestCase):
    def test_new_scan_makes_deadline_review_stale(self):
        archive = make_archive()
        archive.record_review(
            shipment_id="SHP-1", kind="deadline", note="首阅", actor="reviewer"
        )
        state, _ = archive.review_state("SHP-1", "deadline")
        self.assertEqual(state, ReviewState.CURRENT)
        archive.add_source(
            build_source(
                {
                    "id": "scan-2",
                    "shipment_id": "SHP-1",
                    "kind": "scan",
                    "received_at": "2026-11-05T10:00:00+08:00",
                    "observed_at": "2026-11-05T09:00:00+08:00",
                    "text": "Scan: delivery at receiver gate.",
                }
            )
        )
        state, note = archive.review_state("SHP-1", "deadline")
        self.assertEqual(state, ReviewState.STALE)
        self.assertIn("需重新核对", note)

    def test_liability_draft_review_goes_stale_but_note_is_kept(self):
        archive = make_archive()
        checkpoint = archive.record_review(
            shipment_id="SHP-1", kind="liability_draft", note="初步认为仓库环节", actor="reviewer"
        )
        archive.add_source(
            build_source(
                {
                    "id": "scan-3",
                    "shipment_id": "SHP-1",
                    "kind": "scan_correction",
                    "received_at": "2026-11-06T10:00:00+08:00",
                    "corrects": ["scan-old"],
                    "text": "Corrected scan: transfer at 19:30; prior event void.",
                }
            )
        )
        state, _ = archive.review_state("SHP-1", "liability_draft")
        self.assertEqual(state, ReviewState.STALE)
        view = archive.case_view("SHP-1")
        self.assertEqual(view.liability_review, ReviewState.STALE)
        self.assertTrue(any("更正" in trigger for trigger in view.review_triggers))
        latest = archive.latest_checkpoint("SHP-1", "liability_draft")
        self.assertEqual(latest.review_id, checkpoint.review_id)
        self.assertEqual(latest.note, "初步认为仓库环节")


if __name__ == "__main__":
    unittest.main()
