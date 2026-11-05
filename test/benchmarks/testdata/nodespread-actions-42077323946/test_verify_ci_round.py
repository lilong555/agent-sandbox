from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import ci_round as ci
import verify_ci_round as verifier


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "download"
        self.source.mkdir()
        self.output = self.root / "copy"
        self.parser = self.root / "metrics-parser"
        self.emitter = self.root / "schedule-emitter"
        for binary in (self.parser, self.emitter):
            binary.write_text("This fixture must never be executed.\n")
            binary.chmod(0o700)
        self.config = {
            "protocol": copy.deepcopy(ci.PROTOCOL), "controller_args": list(ci.FLAGS),
            "available_cpus": [1, 3], "controller_cpus": [1, 3],
            "source_commits": dict(verifier.COMMITS),
            "binary_sha256": {role: "a" * 64 for role in verifier.ROLES},
            "schedule_emitter_supplied": True,
            "source_commit_basis": "Caller-declared build provenance",
            "runner_binary_path": "/do-not-execute/manager",
        }
        self.actual = {}
        self.rows = []
        entries = [verifier.calibration_entry(rate) for rate in (2, 4, 8)] + ci.formal_plan(8)
        for entry in entries:
            row = {**entry, "status": "complete", "records": 2, "created": 2, "ready": 2,
                   "failed": 0, "complete_lifecycles": 2, "cold": 0, "warm": 2,
                   "ready_pool_start": 128, "ready_pool_sampled_min": 100,
                   "p50_ms": 4.0 if entry["revision"] == "baseline" else 3.0, "p99_ms": 8.0,
                   "controller_cpu_seconds": 1.0, "controller_alloc_bytes": 100,
                   "linux": {"cpu_active_median_percent": 50}, "cleanup": "complete"}
            self.actual[entry["sample"]] = copy.deepcopy(row)
            if entry["kind"] == "calibration":
                row["calibration"] = ci.calibration_check(row)
            self.rows.append(row)
            run = self.source / "runs" / entry["sample"]
            (run / "workload").mkdir(parents=True)
            fixed = {**entry, "pool": 128, "seed": [0, 0], "profile_enabled": False,
                     "gomaxprocs": 4, "stress_gomaxprocs": None, "controller_args": ci.FLAGS,
                     "controller_cpus": [1, 3], "commit": verifier.COMMITS[entry["revision"]],
                     "binary_sha256": self.config["binary_sha256"], "metrics_interval_seconds": 5}
            ci.save(run / "config.json", fixed)
            command = ci.workload.workload_args("stress", Path("/RUN"), entry["sample"],
                                              entry["rate"], entry["seconds"], 128)
            ci.save(run / "command.json", [value.replace("/RUN", ".") for value in command])
            workload_config = {
                "namespace": "np2179-" + entry["sample"], "image": "node:22-alpine", "cleanup": True,
                "phases": ["claims-warm-sustained"], "sustainedRate": entry["rate"],
                "sustainedSeconds": entry["seconds"], "sustainedNamespaces": 1,
                "sustainedPoolHeadroomNanos": (128 // entry["rate"]) * 1_000_000_000,
                "claimDwellNanos": 1_000_000_000, "clientConnections": 4,
                "profileController": False, "profileAPIServer": False, "collectMetrics": False,
                "timeoutNanos": 480_000_000_000, "perSandboxTimeoutNanos": 120_000_000_000,
                "outputDir": "/original/runner/absolute/workload",
            }
            ci.save(run / "workload/summary.json", {"config": workload_config})
            (run / "workload/sandboxes.jsonl").write_text('{"raw":"preserved"}\n')
            ci.save(run / "summary.json", row)
            ci.save(run / "schedule.json", {"offsets_ns": [1, 2]})
            ci.save(run / "arrival-audit.json", {"actual_arrivals": 2})
        self.manifest = {
            "status": "complete", "errors": [], "registration": self.config,
            "protocol": copy.deepcopy(ci.PROTOCOL), "selected_rate": 8, "runs": self.rows,
            "formal_schedule": ci.formal_plan(8), "nodes": [
                {"name": "cp", "role": "control-plane", "ready": True},
                {"name": "w1", "role": "worker", "ready": True},
                {"name": "w2", "role": "worker", "ready": True},
            ],
        }
        (self.source / "linux-load.jsonl").write_text('{"raw":"host"}\n')
        self.persist()

    def persist(self):
        ci.save(self.source / "config.json", self.config)
        ci.write_outputs(self.source, self.manifest)

    def fake_summary(self, run, entry, metrics_parser, host_path, emitter):
        self.assertEqual(run.parent, self.output / "runs")
        self.assertEqual(metrics_parser, self.parser)
        self.assertEqual(emitter, self.emitter)
        self.assertEqual(host_path, self.output / "linux-load.jsonl")
        self.assertFalse((run / "summary.json").exists())
        self.assertFalse((run / "schedule.json").exists())
        self.assertFalse((run / "arrival-audit.json").exists())
        ci.save(run / "schedule.json", {"offsets_ns": [1, 2]})
        ci.save(run / "arrival-audit.json", {"actual_arrivals": 2})
        return {**self.actual[entry["sample"]], **entry}

    def verify(self, side_effect=None):
        original = verifier.tree_hashes(self.source)
        with mock.patch.object(ci, "summarize_run", side_effect=side_effect or self.fake_summary) as summary, \
                mock.patch.object(ci, "kube", side_effect=AssertionError("Cluster access forbidden")), \
                mock.patch.object(ci.subprocess, "Popen", side_effect=AssertionError("Execution forbidden")):
            report = verifier.verify(self.source, self.output, self.parser, self.emitter)
        self.assertEqual(verifier.tree_hashes(self.source), original)
        return report, summary

    def test_complete_round_recomputed_only_in_copy(self):
        report, summary = self.verify()
        self.assertEqual(report["status"], "verified", report["errors"])
        self.assertEqual(summary.call_count, 7)
        self.assertEqual(report["difference_count"], 0)
        self.assertTrue(report["copy_verified"])
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(report["source_tree_sha256"], verifier.tree_digest(report["source_files_sha256"]))
        self.assertEqual(len(report["local_helper_sha256"]), 2)
        saved = ci.read(self.output / "verification.json")
        self.assertEqual(saved, report)
        rebuilt = ci.read(self.output / "summary-recomputed.json")
        self.assertAlmostEqual(rebuilt["primary_comparison"]["ratio"], .75)
        self.assertEqual(len(rebuilt["pairs"]), 2)
        self.assertNotIn("verification.json", verifier.tree_hashes(self.source))

    def test_standalone_copy_uses_imported_harness_hashes(self):
        standalone = self.root / "published"
        standalone.mkdir()
        script = standalone / "verify_ci_round.py"
        shutil.copy2(Path(verifier.__file__), script)
        original = verifier.tree_hashes(self.source)
        argv = [str(script), "--source", str(self.source), "--output", str(self.output),
                "--metrics-parser", str(self.parser), "--schedule-emitter", str(self.emitter)]
        stdout = io.StringIO()
        with mock.patch.object(sys, "argv", argv), redirect_stdout(stdout), \
                mock.patch.object(ci, "summarize_run", side_effect=self.fake_summary) as summary, \
                mock.patch.object(ci, "kube", side_effect=AssertionError("Cluster access forbidden")), \
                mock.patch.object(ci.subprocess, "Popen", side_effect=AssertionError("Execution forbidden")):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(str(script), run_name="__main__")
        self.assertEqual(exited.exception.code, 0, stdout.getvalue())
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["status"], "verified")
        self.assertEqual(summary.call_count, 7)
        self.assertEqual(report["local_code_sha256"]["verify_ci_round.py"], ci.sha256(script))
        dependency_root = Path(ci.__file__).resolve().parent
        for name, digest in report["local_code_sha256"].items():
            if name != "verify_ci_round.py":
                self.assertEqual(digest, ci.sha256(dependency_root / name))
        self.assertEqual(list(standalone.iterdir()), [script])
        self.assertEqual(verifier.tree_hashes(self.source), original)

    def test_all_copied_summaries_can_agree_and_still_fail_raw_recomputation(self):
        self.rows[3]["p50_ms"] = 999.0
        ci.save(self.source / "runs" / self.rows[3]["sample"] / "summary.json", self.rows[3])
        self.persist()
        report, _ = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertGreater(report["difference_count"], 0)
        self.assertTrue(any(path.endswith("/p50_ms") for c in report["comparisons"]
                            for path in c["difference_paths"]))

    def test_primary_comparison_tampering_is_detected(self):
        summary = ci.read(self.source / "summary.json")
        summary["primary_comparison"]["ratio"] = .1
        ci.save(self.source / "summary.json", summary)
        report, _ = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(c["scope"] == "summary/primary_comparison" and c["status"] == "different"
                            for c in report["comparisons"]))

    def test_wrong_source_commit_and_controller_flags_fail(self):
        self.config["source_commits"]["candidate"] = "b" * 40
        self.config["controller_args"] = ["--enable-pprof-debug"]
        self.persist()
        report, _ = self.verify()
        self.assertEqual(report["status"], "failed")
        changed = {c["scope"] for c in report["comparisons"] if c["status"] == "different"}
        self.assertIn("registration/source_commits", changed)
        self.assertIn("registration/controller_args", changed)

    def test_wrong_abba_order_fails(self):
        self.rows[3], self.rows[4] = self.rows[4], self.rows[3]
        self.persist()
        report, summary = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(summary.call_count, 3)
        self.assertTrue(any("Run order" in e for e in report["errors"]))

    def test_calibration_recomputed_instead_of_accepting_published_pass(self):
        self.actual["ci-cal-r8"]["ready_pool_sampled_min"] = 63
        report, _ = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any(c["scope"] == "selection/highest-passing-rate" and c["status"] == "different"
                            for c in report["comparisons"]))

    def test_failed_run_is_preserved_and_completed_subset_is_checked(self):
        self.manifest["status"] = "failed"
        self.manifest["errors"] = ["Formal execution failed"]
        self.rows[-1].update(status="failed", errors=["TimeoutError"])
        self.persist()
        report, summary = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["artifact_status"], "failed")
        self.assertEqual(summary.call_count, 6)
        self.assertEqual(len(report["recomputed_runs"]), 6)
        self.assertEqual(len(report["skipped_runs"]), 1)
        self.assertTrue((self.output / "verification.json").exists())

    def test_analysis_failure_retains_raw_copy_and_failure_status(self):
        def broken(*args):
            if args[1]["sample"] == "ci-formal-4-baseline":
                raise ValueError("A private parser payload")
            return self.fake_summary(*args)
        report, _ = self.verify(side_effect=broken)
        self.assertEqual(report["status"], "failed")
        failed = self.output / "runs/ci-formal-4-baseline"
        self.assertEqual(ci.read(failed / "summary.json")["status"], "verification_failed")
        self.assertEqual((failed / "workload/sandboxes.jsonl").read_text(), '{"raw":"preserved"}\n')
        self.assertNotIn("private parser", json.dumps(report))

    def test_empty_destination_is_supported(self):
        self.output.mkdir()
        report, _ = self.verify()
        self.assertEqual(report["status"], "verified", report["errors"])

    def test_nonempty_destination_is_preserved(self):
        self.output.mkdir()
        (self.output / "existing").write_text("keep")
        report, summary = self.verify()
        self.assertEqual(report["status"], "failed")
        summary.assert_not_called()
        self.assertEqual(list(self.output.iterdir()), [self.output / "existing"])

    def test_symlink_source_is_rejected(self):
        (self.source / "outside").symlink_to(self.parser)
        with mock.patch.object(ci, "summarize_run") as summary:
            report = verifier.verify(self.source, self.output, self.parser, self.emitter)
        self.assertEqual(report["status"], "failed")
        summary.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_nested_destination_and_path_traversal_are_rejected(self):
        self.output = self.source / "nested"
        report, summary = self.verify()
        self.assertEqual(report["status"], "failed")
        summary.assert_not_called()

    def test_unsafe_sample_is_rejected_before_run_reads(self):
        self.rows[0]["sample"] = "../outside"
        self.persist()
        report, summary = self.verify()
        self.assertEqual(report["status"], "failed")
        summary.assert_not_called()

    def test_artifact_helper_is_never_executed(self):
        helper = self.source / "untrusted-parser"
        helper.write_bytes(self.parser.read_bytes())
        helper.chmod(0o700)
        with mock.patch.object(ci, "summarize_run") as summary:
            report = verifier.verify(self.source, self.output, helper, self.emitter)
        self.assertEqual(report["status"], "failed")
        summary.assert_not_called()

    def test_failed_lowest_calibration_cannot_be_verified_as_complete(self):
        self.actual["ci-cal-r2"]["cold"] = 1
        self.actual["ci-cal-r2"]["warm"] = 1
        report, _ = self.verify()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(any("lowest-rate" in e for e in report["errors"]))

    def test_interruption_cannot_report_verified(self):
        report, _ = self.verify(side_effect=KeyboardInterrupt)
        self.assertEqual(report["status"], "failed")
        self.assertIn("KeyboardInterrupt", report["errors"])
        self.assertTrue((self.output / "verification.json").exists())


if __name__ == "__main__":
    unittest.main()
