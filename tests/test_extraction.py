"""Extraction-level tests: roles, modes, and provenance retention."""

import unittest

from shipment_claims.lexicon import EventRole, StatementMode, extract_clauses
from shipment_claims.models import (
    build_source,
    parse_timestamp,
    events_for_source,
    extract_calendar_date,
    extract_reference_ids,
)


def modes(text, kind):
    return [(clause.role, clause.mode) for clause in extract_clauses(text, kind)]


class RoleAndModeTests(unittest.TestCase):
    def test_storage_and_missing_signature_are_separate(self):
        clauses = extract_clauses("18:40 放在东仓暂存区，值班员未签收。", "driver_note")
        self.assertEqual(clauses[0].role, EventRole.STORAGE)
        self.assertEqual(clauses[0].mode, StatementMode.OBSERVED)
        self.assertEqual(clauses[1].role, EventRole.NON_ACKNOWLEDGEMENT)
        self.assertEqual(clauses[1].mode, StatementMode.OBSERVED)

    def test_handover_is_not_silently_read_as_delivery(self):
        clauses = extract_clauses(
            "Manifest shows handover at East Depot, not proof of delivery to consignee.",
            "carrier_message",
        )
        self.assertEqual(clauses[0].role, EventRole.HANDOVER)
        self.assertEqual(clauses[0].mode, StatementMode.SYSTEM_RECORD)

    def test_recipient_guess_is_inference_not_observation(self):
        clauses = extract_clauses("猜测司机漏送；", "recipient_email")
        self.assertEqual(clauses[0].mode, StatementMode.INFERRED)

    def test_relayed_email_claim_is_reported(self):
        clauses = extract_clauses("邮件称货物未到，", "recipient_email")
        self.assertEqual(clauses[0].role, EventRole.NON_DELIVERY)
        self.assertEqual(clauses[0].mode, StatementMode.REPORTED)

    def test_corrected_scan_is_formal_record(self):
        clauses = extract_clauses(
            "Corrected scan: transfer recorded at 19:05;", "scan_correction"
        )
        self.assertEqual(clauses[0].role, EventRole.HANDOVER)
        self.assertEqual(clauses[0].mode, StatementMode.SYSTEM_RECORD)

    def test_traditional_and_simplified_chinese_both_match(self):
        for text, expected in [
            ("貨物暫存於倉庫", EventRole.STORAGE),
            ("货物暂存于仓库", EventRole.STORAGE),
            ("已簽收", EventRole.ACKNOWLEDGEMENT),
            ("已签收", EventRole.ACKNOWLEDGEMENT),
            ("完成交接", EventRole.HANDOVER),
        ]:
            with self.subTest(text=text):
                self.assertEqual(extract_clauses(text, "warehouse_note")[0].role, expected)

    def test_english_handover_synonyms(self):
        for text in ("custody transferred at gate", "handed over to depot", "release to warehouse"):
            with self.subTest(text=text):
                self.assertEqual(extract_clauses(text, "dock_note")[0].role, EventRole.HANDOVER)

    def test_negated_delivery_becomes_non_delivery(self):
        clauses = extract_clauses("Parcel was not delivered to the recipient.", "driver_note")
        self.assertEqual(clauses[0].role, EventRole.NON_DELIVERY)

    def test_unrelated_clause_is_kept_as_unknown(self):
        clauses = extract_clauses("Refrigerated trailer, internal temperature 4C.", "driver_note")
        self.assertEqual(clauses[0].role, EventRole.UNKNOWN)
        self.assertTrue(clauses[0].text.strip())


class ProvenanceTests(unittest.TestCase):
    def test_every_event_keeps_author_time_place_and_source(self):
        record = build_source(
            {
                "id": "s1",
                "kind": "driver_note",
                "lang": "zh",
                "author": "司机 甲",
                "author_role": "driver",
                "received_at": "2026-11-02T09:30:00+08:00",
                "location": "东仓",
                "text": "11月1日 18:40 放在东仓暂存区，值班员未签收。",
            }
        )
        events = events_for_source(record)
        self.assertEqual(len(events), 2)
        first = events[0]
        self.assertEqual(first.author, "司机 甲")
        self.assertEqual(first.author_role, "driver")
        self.assertEqual(first.location, "东仓暂存区")
        self.assertEqual(first.source_id, "s1")
        self.assertEqual(first.clause_index, 0)
        self.assertIsNotNone(first.occurred_at)
        # 18:40 +08:00 on the date stated in the text -> 10:40 UTC.
        self.assertEqual(
            parse_timestamp(first.occurred_at).isoformat(), "2026-11-01T10:40:00+00:00"
        )
        self.assertIn("+08:00", first.occurred_at)
        self.assertEqual(first.time_precision, "date_from_text")

    def test_clause_without_time_does_not_borrow_neighbour_clock(self):
        record = build_source(
            {
                "id": "s2",
                "kind": "driver_note",
                "text": "18:40 放在东仓暂存区，值班员未签收。",
                "received_at": "2026-11-02T09:30:00+08:00",
            }
        )
        events = events_for_source(record)
        self.assertIsNotNone(events[0].occurred_at)
        self.assertIsNone(events[1].occurred_at)

    def test_missing_metadata_is_reported_not_invented(self):
        record = build_source({"id": "s3", "kind": "driver_note", "text": "放在仓库"})
        self.assertIsNone(record.observed_at)
        self.assertEqual(record.shipment_id, "UNASSIGNED")
        errors = record.validate()
        self.assertTrue(any("shipment_id" in error for error in errors))

    def test_chinese_date_parsing(self):
        self.assertEqual(
            str(extract_calendar_date("11月1日 18:40", "2026-11-02T00:00:00+08:00")),
            "2026-11-01",
        )
        self.assertEqual(
            str(extract_calendar_date("2026年12月3日", None)), "2026-12-03"
        )

    def test_reference_ids_are_not_truncated(self):
        found = extract_reference_ids(
            "Renumber notice: SHP-2026-0417-R replaced by SHP-2026-0417; retained."
        )
        self.assertIn("SHP-2026-0417-R", found)
        self.assertIn("SHP-2026-0417", found)


if __name__ == "__main__":
    unittest.main()
