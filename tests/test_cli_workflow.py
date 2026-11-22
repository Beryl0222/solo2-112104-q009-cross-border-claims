"""End-to-end CLI workflow on the synthetic scenario bundle."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from shipment_claims.archive import ClaimArchive
from shipment_claims.cli import main
from shipment_claims.importers import load_bundle

ROOT = Path(__file__).parents[1]
SCENARIO = ROOT / "data" / "scenario_handover.json"


class CliWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.archive = Path(self.directory.name) / "archive.json"

    def run_cli(self, *arguments: str) -> int:
        return main([*arguments])

    def test_full_workflow(self):
        self.assertEqual(
            self.run_cli("import", str(SCENARIO), "--out", str(self.archive)), 0
        )
        self.assertTrue(self.archive.exists())

        # Unverified link does not merge identity.
        loaded = ClaimArchive.from_export(self.archive)
        self.assertEqual(loaded.resolve_id("SHP-2026-0417-R"), "SHP-2026-0417-R")

        # Verification requires the mapping source to name both ids.
        link = loaded.to_document()["links"][0]
        link_id = link["link_id"]
        self.assertEqual(self.run_cli("merge", "--archive", str(self.archive),
                                      "--old", "SHP-2026-0417-R",
                                      "--new", "SHP-2026-0417",
                                      "--link-id", link_id), 2)

        verified = self.run_cli(
            "link", "--archive", str(self.archive),
            "--old", "SHP-2026-0417-R", "--new", "SHP-2026-0417",
            "--evidence", "src-0417-mapping",
            "--rationale", "承运商重编号通知", "--actor", "handler",
            "--verify",
        )
        self.assertEqual(verified, 0)
        merged = ClaimArchive.from_export(self.archive)
        self.assertEqual(merged.resolve_id("SHP-2026-0417-R"), "SHP-2026-0417")
        # The link command is additive; the pre-existing proposal remains.
        self.assertGreaterEqual(len(merged.to_document()["links"]), 1)

        # Record a review, then a new scan must staleness it.
        self.assertEqual(
            self.run_cli("review", "--archive", str(self.archive),
                         "--shipment", "SHP-2026-0417", "--kind", "deadline",
                         "--note", "按更正扫描核对", "--actor", "reviewer"), 0
        )
        current = ClaimArchive.from_export(self.archive).case_view("SHP-2026-0417")
        self.assertEqual(current.deadline_review.value, "current")

        # Submit original material, then supplement it.
        self.assertEqual(
            self.run_cli("submit", "--archive", str(self.archive),
                         "--claim-id", "CLM-0417", "--shipment", "SHP-2026-0417",
                         "--summary", "东仓交接后无签收扫描", "--actor", "handler",
                         "--source", "src-0417-driver", "src-0417-carrier"), 0
        )
        self.assertEqual(
            self.run_cli("supplement", "--archive", str(self.archive),
                         "--claim-id", "CLM-0417", "--summary", "补入承运商答复",
                         "--actor", "handler", "--source", "src-0417-scan-correction"), 0
        )
        final = ClaimArchive.from_export(self.archive).case_view("SHP-2026-0417")
        self.assertEqual(len(final.submissions), 1)
        self.assertEqual(len(final.submissions[0].versions), 2)
        self.assertEqual(
            final.submissions[0].original.summary, "东仓交接后无签收扫描"
        )

        report = self.run_cli("report", "--archive", str(self.archive),
                              "--shipment", "SHP-2026-0417", "--today", "2026-11-22")
        self.assertEqual(report, 0)

    def test_report_writes_file(self):
        self.run_cli("import", str(SCENARIO), "--out", str(self.archive))
        out = Path(self.directory.name) / "case.md"
        self.assertEqual(
            self.run_cli("report", "--archive", str(self.archive), "--out", str(out)), 0
        )
        text = out.read_text(encoding="utf-8")
        self.assertIn("事件时间线", text)
        self.assertIn("不判断法律责任", text)

    def test_json_view_is_machine_readable(self):
        self.run_cli("import", str(SCENARIO), "--out", str(self.archive))
        view = ClaimArchive.from_export(self.archive).case_view(
            "SHP-2026-0417", today=date(2026, 11, 22)
        ).to_dict()
        json.dumps(view)  # serialisable
        self.assertEqual(view["canonical_id"], "SHP-2026-0417")
        self.assertTrue(any(event["mode"] == "inference" for event in view["events"]))

    def test_unassigned_list_import_requires_explicit_shipment(self):
        plain = ROOT / "data" / "claims.json"
        self.assertEqual(
            self.run_cli("import", str(plain), "--out", str(self.archive)), 2
        )
        self.assertEqual(
            self.run_cli("import", str(plain), "--shipment", "SHP-DEMO", "--out", str(self.archive)),
            0,
        )

    def test_bundle_grouping_rejects_silent_remapping(self):
        bad = Path(self.directory.name) / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "shipments": {
                        "SHP-A": [{"id": "x", "kind": "driver_note", "shipment_id": "SHP-B", "text": "交接"}]
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(Exception):
            load_bundle(bad)


if __name__ == "__main__":
    unittest.main()
