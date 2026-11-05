import gzip
import json
from pathlib import Path
import tempfile
import unittest

from export_evidence import WINDOWS_FIELDS, filter_jsonl, portable, workload_summary


class ExportTests(unittest.TestCase):
    def test_host_process_identities_are_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "host.jsonl"
            destination = Path(directory) / "filtered.jsonl.gz"
            source.write_text(json.dumps({
                "utc": "2026-11-06T00:00:00Z", "cpu_percent": 42,
                "available_mb": 1000, "pages_input_per_sec": 0,
                "processes": [{"name": "private-application", "pid": 123,
                               "cpu_one_core_percent": 12.5}],
                "unexpected_private_field": "secret",
            }) + "\n")
            filter_jsonl(source, destination, WINDOWS_FIELDS)
            value = json.loads(gzip.decompress(destination.read_bytes()))
            self.assertEqual(set(value), set(WINDOWS_FIELDS))
            self.assertEqual(value["processes"], [{"cpu_one_core_percent": 12.5}])
            self.assertNotIn(b"private", gzip.decompress(destination.read_bytes()))

    def test_workload_summary_retains_apf_classification(self):
        value = {
            "config": {"outputDir": "/private/path", "sustainedRate": 8},
            "apfVerification": {"flowSchema": "global-default", "flowSchemaUID": "private",
                                "priorityLevel": "global-default", "exempt": False},
        }
        filtered = workload_summary(value)
        self.assertEqual(filtered["config"]["outputDir"], "workload")
        self.assertFalse(filtered["apfVerification"]["exempt"])
        self.assertNotIn("flowSchemaUID", filtered["apfVerification"])
        self.assertEqual(value["config"]["outputDir"], "/private/path")

    def test_artifact_paths_are_relative_recursively(self):
        value = {"/tmp/evidence/runs/one": {"file": "/tmp/evidence/runs/two"}}
        self.assertEqual(portable(value, Path("/tmp/evidence")),
                         {"runs/one": {"file": "runs/two"}})


if __name__ == "__main__":
    unittest.main()
