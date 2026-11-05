import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from validate_pacing import audit, main


class PacingTests(unittest.TestCase):
    @staticmethod
    def records():
        return [
            {"name": f"p1-sclaim-{index}", "phase": "claims-warm-sustained",
             "createCalled": f"2026-11-06T00:00:00.{fraction}+08:00"}
            for index, fraction in enumerate(("050010000", "100020000", "150030000"))
        ]

    def test_exact_identity_count_and_relative_offsets(self):
        result = audit(self.records(), [50_000_000, 100_000_000, 150_000_000])
        self.assertEqual(result["actual_arrivals"], 3)
        self.assertAlmostEqual(result["offset_envelope_ms"], .02, places=3)
        self.assertEqual(result["adjacent_out_of_order_arrivals"], 0)

    def test_missing_duplicate_and_extra_arrivals_are_rejected(self):
        records = self.records()
        for invalid in (records[:2], records + records[:1], records[1:]):
            with self.assertRaises(ValueError):
                audit(invalid, [50_000_000, 100_000_000, 150_000_000])

    def test_reordered_creation_is_reported(self):
        records = self.records()
        records[0]["createCalled"], records[1]["createCalled"] = (
            records[1]["createCalled"], records[0]["createCalled"])
        result = audit(records, [50_000_000, 100_000_000, 150_000_000])
        self.assertEqual(result["adjacent_out_of_order_arrivals"], 1)
        self.assertGreater(result["offset_envelope_ms"], 90)

    def test_profiled_manifest_without_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            results = root / "results"
            results.mkdir()
            sample = root / "runs" / "profiled-sample"
            (sample / "workload").mkdir(parents=True)
            (sample / "config.json").write_text(json.dumps({
                "claims_per_second": 8, "seconds": 60,
            }))
            (sample / "workload" / "sandboxes.jsonl").write_text(
                "\n".join(json.dumps(row) for row in self.records()) + "\n")
            (results / "manifest.json").write_text(json.dumps({
                "status": "completed",
                "runs": [{"sample": "profiled-sample", "warmup": False,
                          "status": "completed"}],
            }))
            schedule = json.dumps({"offsets_ns": [50_000_000, 100_000_000, 150_000_000]})
            with patch("validate_pacing.ROOT", root), \
                    patch("validate_pacing.subprocess.check_output", return_value=schedule), \
                    patch("sys.argv", ["validate_pacing.py", str(results)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                main()
            report = json.loads((results / "arrival-audit.json").read_text())
            self.assertEqual(report["runs"][0]["group"], "profiled")
            self.assertEqual(report["runs"][0]["actual_arrivals"], 3)


if __name__ == "__main__":
    unittest.main()
