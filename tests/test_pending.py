"""Pending carrier confirmations and the 'what still needs confirming' view."""

import unittest

from shipment_claims.archive import ArchiveError, ClaimArchive
from shipment_claims.models import ConfirmationStatus, build_source
from shipment_claims.report import render_case


def archive() -> ClaimArchive:
    instance = ClaimArchive()
    instance.add_source(
        build_source(
            {
                "id": "driver",
                "shipment_id": "SHP-1",
                "kind": "driver_note",
                "received_at": "2026-11-02T09:00:00+08:00",
                "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。",
            }
        )
    )
    instance.add_pending(
        shipment_id="SHP-1",
        question="东仓交接后是否有签收扫描？",
        target_party="carrier",
        created_by="handler",
        evidence_ids=["driver"],
    )
    return instance


class PendingTests(unittest.TestCase):
    def test_open_question_is_reported_as_still_needing_confirmation(self):
        instance = archive()
        view = instance.case_view("SHP-1")
        self.assertEqual(len(view.pending), 1)
        self.assertEqual(view.pending[0].status, ConfirmationStatus.OPEN)
        text = render_case(view, instance)
        self.assertIn("仍需承运商确认的环节", text)

    def test_new_relevant_source_marks_evidence_available(self):
        instance = archive()
        instance.add_source(
            build_source(
                {
                    "id": "scan-late",
                    "shipment_id": "SHP-1",
                    "kind": "scan",
                    "received_at": "2026-11-23T10:00:00+08:00",
                    "text": "Scan: no signature collected at East Depot.",
                }
            )
        )
        pending = instance.pending_for("SHP-1")[0]
        self.assertEqual(pending.status, ConfirmationStatus.EVIDENCE_AVAILABLE)
        self.assertIn("scan-late", pending.evidence_ids)

    def test_answered_question_keeps_answer_and_source(self):
        instance = archive()
        question = instance.pending_for("SHP-1")[0]
        instance.add_source(
            build_source(
                {
                    "id": "carrier-reply",
                    "shipment_id": "SHP-1",
                    "kind": "carrier_message",
                    "received_at": "2026-11-06T10:00:00+08:00",
                    "text": "Carrier confirms: no signature record exists.",
                }
            )
        )
        answered = instance.answer_pending(
            question.question_id,
            answer="承运商确认无签收记录",
            answered_by="handler",
            source_ids=["carrier-reply"],
        )
        self.assertEqual(answered.status, ConfirmationStatus.ANSWERED)
        self.assertEqual(answered.answer_source_ids, ("carrier-reply",))
        view = instance.case_view("SHP-1")
        self.assertNotIn("仍需承运商确认的环节", render_case(view, instance))

    def test_withdrawn_question_cannot_be_answered(self):
        instance = archive()
        question = instance.pending_for("SHP-1")[0]
        instance.withdraw_pending(question.question_id, reason="重复登记", actor="handler")
        with self.assertRaises(ArchiveError):
            instance.answer_pending(
                question.question_id, answer="迟到的答复", answered_by="handler"
            )

    def test_question_requires_archived_evidence(self):
        instance = ClaimArchive()
        with self.assertRaises(ArchiveError):
            instance.add_pending(
                shipment_id="SHP-1",
                question="?",
                target_party="carrier",
                created_by="handler",
                evidence_ids=["missing"],
            )


class DriftClassificationTests(unittest.TestCase):
    def test_earlier_unused_material_is_not_called_post_submission(self):
        instance = archive()
        instance.submit_claim(
            claim_id="CLM-1",
            shipment_id="SHP-1",
            summary="仅采用司机备注",
            created_by="handler",
            source_ids=["driver"],
        )
        instance.add_source(
            build_source(
                {
                    "id": "early-carrier",
                    "shipment_id": "SHP-1",
                    "kind": "carrier_message",
                    "received_at": "2026-10-30T09:00:00+08:00",
                    "text": "Manifest shows handover only.",
                }
            )
        )
        claim = instance.submissions_for("SHP-1")[0]
        drift = instance.submission_drift(claim)
        self.assertTrue(any("提交前已存在但未选用" in note for note in drift))
        self.assertFalse(any("提交后新增" in note for note in drift))


if __name__ == "__main__":
    unittest.main()
