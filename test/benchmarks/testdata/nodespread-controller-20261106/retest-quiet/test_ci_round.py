import errno
import json
import math
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import ci_round as ci


def completed(entry, ready=100, cold=0, cpu=50):
    return {**entry, "status": "complete", "records": 2, "complete_lifecycles": 2,
            "created": 2, "ready": 2, "failed": 0, "cold": cold, "warm": 2 - cold,
            "ready_pool_sampled_min": ready, "linux": {"cpu_active_median_percent": cpu},
            "p50_ms": 4, "p99_ms": 8, "controller_cpu_seconds": 1,
            "controller_alloc_bytes": 100, "cleanup": "complete"}


class PlanningTests(unittest.TestCase):
    def test_boundary_thresholds_and_individual_failures(self):
        row = completed({}, ready=64, cpu=85)
        self.assertTrue(ci.calibration_check(row)["passed"])
        for update in ({"ready_pool_sampled_min": 63}, {"cold": 1}, {"failed": 1},
                       {"complete_lifecycles": 1}, {"linux": {"cpu_active_median_percent": 85.01}}):
            with self.subTest(update=update):
                self.assertFalse(ci.calibration_check({**row, **update})["passed"])

    def test_abba_pairing_preserves_cold_and_zero_denominators(self):
        plan = ci.formal_plan(4)
        self.assertEqual([r["revision"] for r in plan], ["baseline", "candidate", "candidate", "baseline"])
        self.assertEqual([r["pair"] for r in plan], [1, 1, 2, 2])
        rows = [completed(entry, ready=0, cold=1) for entry in plan]
        rows[0]["controller_cpu_seconds"] = 0
        pairs = ci.paired_summary(rows)
        self.assertEqual(len(pairs), 2)
        self.assertIsNone(pairs[0]["measurements"]["controller_cpu_seconds"]["percent_change"])
        self.assertNotIn("bootstrap", pairs[0])
        rows[1]["records"] = 3
        with self.assertRaisesRegex(ci.RoundError, "arrival counts"):
            ci.paired_summary(rows)

    def test_same_flags_and_no_profiler(self):
        self.assertNotIn("--enable-pprof-debug", ci.FLAGS)
        self.assertIn("--pprof-mutex-profile-fraction=0", ci.FLAGS)
        self.assertIn("--pprof-block-profile-rate=0", ci.FLAGS)
        self.assertIn("--sandbox-warm-pool-max-refill-rate=16", ci.FLAGS)

    def test_primary_is_two_pair_geometric_mean_without_ci(self):
        rows = [completed(entry) for entry in ci.formal_plan(4)]
        rows[1]["p50_ms"] = 2
        rows[2]["p50_ms"] = 3
        result = ci.primary_comparison(ci.paired_summary(rows))
        expected = math.sqrt(0.5 * 0.75)
        self.assertAlmostEqual(result["ratio"], expected)
        self.assertAlmostEqual(result["percent_change"], 100 * (expected - 1))
        self.assertEqual(result["n_pairs"], 2)
        self.assertIn("no confidence interval", result["inference"])
        self.assertNotIn("ci95", result)
        self.assertIsNone(ci.primary_comparison([]))
        rows[0]["p50_ms"] = 0
        self.assertIsNone(ci.primary_comparison(ci.paired_summary(rows))["ratio"])

    def test_progress_prints_small_flushed_json(self):
        with mock.patch("builtins.print") as printed:
            ci.progress("DONE", completed({"sample": "ci-cal-r2", "rate": 2}))
        value = json.loads(printed.call_args.args[0])
        self.assertEqual(value["event"], "DONE")
        self.assertEqual(value["count"], 2)
        self.assertEqual(value["p50_ms"], 4)
        self.assertTrue(printed.call_args.kwargs["flush"])

    def test_cluster_topology_is_checked_without_saving_addresses(self):
        nodes = {"items": [
            {"metadata": {"name": name, "labels": labels},
             "status": {"conditions": [{"type": "Ready", "status": "True"}],
                        "addresses": [{"address": "private-address"}]}}
            for name, labels in (("cp", {"node-role.kubernetes.io/control-plane": ""}),
                                 ("w1", {}), ("w2", {}))
        ]}
        with mock.patch.object(ci, "kube", side_effect=["kind-as-nodespread-2179\n", json.dumps(nodes)]):
            result = ci.cluster_check({})
        self.assertEqual(len(result), 3)
        self.assertNotIn("private-address", json.dumps(result))
        with mock.patch.object(ci, "kube", return_value="production"):
            with self.assertRaisesRegex(ci.RoundError, "kind context"):
                ci.cluster_check({})


class PortTests(unittest.TestCase):
    def test_active_listener_is_rejected_with_controlled_diagnostic(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ci.RoundError, f"port {port} unavailable.*errno {errno.EADDRINUSE}"):
                ci.ports_available((port,))

    def test_recently_closed_connection_does_not_block_controller_restart(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            with socket.create_connection(listener.getsockname(), timeout=2) as client:
                connection, _ = listener.accept()
                with connection:
                    connection.shutdown(socket.SHUT_WR)
                    self.assertEqual(client.recv(1), b"")
        with socket.socket() as plain_probe:
            with self.assertRaises(OSError) as failure:
                plain_probe.bind(("127.0.0.1", port))
            self.assertEqual(failure.exception.errno, errno.EADDRINUSE)
        ci.ports_available((port,))


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        (self.output / "runs").mkdir()
        self.args = SimpleNamespace(output=self.output, kubeconfig=Path("/private/kubeconfig"))

    @staticmethod
    def monitor(stop, path, errors):
        path.touch()
        stop.wait()

    def execute(self, responses):
        calls = []

        def runner(entry, args, registration, envs):
            calls.append(entry)
            (args.output / "runs" / entry["sample"]).mkdir()
            self.assertEqual(envs[0]["GOMAXPROCS"], "4")
            self.assertNotIn("GOMAXPROCS", envs[1])
            return responses(entry)

        old_root = ci.workload.ROOT
        with mock.patch.object(ci, "cluster_check", return_value=[]), \
                mock.patch.object(ci, "monitor_runner", side_effect=self.monitor), \
                mock.patch.dict(ci.os.environ, {"GOMAXPROCS": "99", "SECRET_TOKEN": "hidden"}), \
                mock.patch.object(ci, "progress") as progress:
            code = ci.execute(self.args, {}, runner=runner)
        self.assertEqual([c.args[0] for c in progress.call_args_list], ["START", "DONE"] * len(calls))
        self.assertEqual(ci.workload.ROOT, old_root)
        manifest = ci.read(self.output / "manifest.json")
        for name in ("manifest.json", "summary.json", "REPORT.md"):
            self.assertNotIn("hidden", (self.output / name).read_text())
            self.assertNotIn("/private/kubeconfig", (self.output / name).read_text())
        return code, calls, manifest

    def test_highest_passing_rate_and_all_seven_runs(self):
        code, calls, manifest = self.execute(lambda e: completed(e))
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 7)
        self.assertEqual([e["rate"] for e in calls[:3]], [2, 4, 8])
        self.assertEqual([e["seconds"] for e in calls], [30, 30, 30, 60, 60, 60, 60])
        self.assertEqual(manifest["selected_rate"], 8)
        self.assertEqual(len(ci.read(self.output / "summary.json")["pairs"]), 2)
        self.assertEqual(ci.read(self.output / "summary.json")["primary_comparison"]["ratio"], 1)

    def test_high_rate_normal_failures_lower_selected_rate(self):
        code, calls, manifest = self.execute(
            lambda e: completed(e, ready=63 if e["kind"] == "calibration" and e["rate"] > 2 else 100))
        self.assertEqual(code, 0)
        self.assertEqual(manifest["selected_rate"], 2)
        self.assertEqual(len(calls), 7)
        self.assertFalse(manifest["runs"][1]["calibration"]["passed"])

    def test_lowest_rate_failure_aborts_and_retains_evidence(self):
        code, calls, manifest = self.execute(lambda e: completed(e, cold=1))
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(manifest["runs"][0]["cold"], 1)
        self.assertTrue((self.output / "runs/ci-cal-r2/summary.json").is_file())

    def test_operational_failure_aborts_instead_of_lowering_rate(self):
        code, calls, manifest = self.execute(
            lambda e: {**completed(e), "status": "failed" if e["rate"] == 4 else "complete"})
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(manifest["runs"][-1]["status"], "failed")

    def test_formal_cold_and_pressure_remain_in_both_pairs(self):
        code, calls, manifest = self.execute(
            lambda e: completed(e, ready=0, cold=1, cpu=99) if e["kind"] == "formal" else completed(e))
        self.assertEqual(code, 0)
        self.assertEqual(len(ci.read(self.output / "summary.json")["pairs"]), 2)
        self.assertEqual(sum(r["cold"] for r in manifest["runs"]), 4)


class RawSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "workload").mkdir()
        (self.run / "telemetry").mkdir()
        self.entry = {"sample": "ci-cal-r2", "kind": "calibration", "revision": "baseline",
                      "rate": 2, "seconds": 30, "pair": None, "slot": 2}
        self.records = []
        for i, (second, duration) in enumerate(((1, 4), (3, 8))):
            self.records.append({
                "name": f"p1-sclaim-{i}", "namespace": "np2179-ci-cal-r2",
                "phase": "claims-warm-sustained", "boundSandbox": f"pool-{i}",
                "createCalled": self.utc(second),
                "createReturned": f"2026-01-01T00:00:{second:02d}.001Z",
                "sandboxReady": f"2026-01-01T00:00:{second:02d}.{duration:03d}Z",
                "deleteCalled": self.utc(second + 1), "sandboxDeleted": self.utc(second + 2),
                "sandboxReadyMs": duration, "createAckMs": 1,
            })
        self.phase = {"name": "claims-warm-sustained", "created": 2, "ready": 2, "failed": 0,
                      "latency": {
                          "endToEndReady": {"count": 2, "minMs": 4, "maxMs": 8, "meanMs": 6,
                                           "p50Ms": 4, "p90Ms": 8, "p95Ms": 8, "p99Ms": 8},
                          "createAck": {"count": 2, **{k: 1 for k in
                                        ("minMs", "maxMs", "meanMs", "p50Ms", "p90Ms", "p95Ms", "p99Ms")}},
                      }}
        samples = [{"tag": "start" if i == 0 else "end" if i == 6 else "periodic",
                    "status": "complete", "artifact": f"metrics-{i:04d}.prom",
                    "started_at": self.point(i * 5), "ended_at": self.point(i * 5)}
                   for i in range(7)]
        self.telemetry = {"status": "complete", "exit_code": 0, "errors": [], "samples": samples,
                          "trigger": {"phase": "claims-warm-sustained", "at": self.point(0)},
                          "boundary": {"kind": "arrivals_finished", "at": self.point(30)}}
        ci.save(self.run / "telemetry/run.json", self.telemetry)
        self.host = self.run / "linux-load.jsonl"
        self.host.write_text("".join(json.dumps({"utc": self.utc(i * 5), "cpu_active_percent": 50}) + "\n"
                                     for i in range(1, 7)))
        self.parsed = {}
        self.labels = {"namespace": "np2179-ci-cal-r2", "warmpool_name": "p1-spool"}
        for i in range(7):
            self.parsed[str(self.run / "telemetry" / f"metrics-{i:04d}.prom")] = [
                {"name": "agent_sandbox_warmpool_size", "labels": {**self.labels, "sandbox_status": "ready"},
                 "value": 128 if i == 0 else 100},
                {"name": "process_cpu_seconds_total", "labels": {}, "value": i},
                {"name": "go_memstats_alloc_bytes_total", "labels": {}, "value": i * 100},
            ]
        self.final = [{"name": "agent_sandbox_claim_creation_total",
                       "labels": {**self.labels, "launch_type": "warm", "pod_condition": "ready"}, "value": 2}]
        self.parsed[str(self.run / "metrics-final.prom")] = self.final

    @staticmethod
    def utc(second):
        return f"2026-01-01T00:00:{second:02d}Z"

    def point(self, second):
        return {"utc": self.utc(second), "monotonic_seconds": second}

    def summarize(self, emitter=None):
        ci.save(self.run / "workload/summary.json", {"phases": [self.phase]})
        (self.run / "workload/sandboxes.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in self.records))
        with mock.patch.object(ci, "read_metrics", return_value=self.parsed):
            return ci.summarize_run(self.run, self.entry, Path("/parser"), self.host, emitter)

    def test_valid_raw_lifecycle_and_counter_deltas(self):
        row = self.summarize()
        self.assertEqual(row["p50_ms"], 4)
        self.assertEqual(row["p99_ms"], 8)
        self.assertEqual(row["controller_cpu_seconds"], 6)
        self.assertEqual(row["controller_alloc_bytes"], 600)
        self.assertEqual(row["ready_pool_start"], 128)
        self.assertEqual(row["pacing"]["status"], "contiguous_ids_verified")

    def test_seeded_pacing_is_checked_when_supplied(self):
        with mock.patch.object(ci.subprocess, "check_output", return_value=json.dumps(
                {"offsets_ns": [1_000_000_000, 3_000_000_000]})):
            row = self.summarize(Path("/emitter"))
        self.assertEqual(row["pacing"]["status"], "seeded_schedule_verified")
        self.assertEqual(row["pacing"]["offset_envelope_ms"], 0)

    def test_start_ready_127_aborts(self):
        self.parsed[str(self.run / "telemetry/metrics-0000.prom")][0]["value"] = 127
        with self.assertRaisesRegex(ci.RoundError, "Start Ready"):
            self.summarize()

    def test_create_after_scheduler_boundary_preserves_lifecycle_and_counter_window(self):
        self.records[1].update(
            createCalled="2026-01-01T00:00:30.000103647Z",
            createReturned="2026-01-01T00:00:30.001103647Z",
            sandboxReady="2026-01-01T00:00:30.008103647Z",
            deleteCalled=self.utc(31), sandboxDeleted=self.utc(32),
        )
        with mock.patch.object(ci.subprocess, "check_output", return_value=json.dumps(
                {"offsets_ns": [1_000_000_000, 29_999_000_000]})):
            row = self.summarize(Path("/emitter"))
        self.assertEqual(row["complete_lifecycles"], 2)
        self.assertEqual(row["creates_before_observed_trigger"], 0)
        self.assertEqual(row["creates_after_observed_boundary"], 1)
        self.assertAlmostEqual(row["last_create_offset_from_boundary_ms"], 0.103647)
        self.assertEqual(row["ready_after_traffic_count"], 1)
        self.assertEqual(row["counter_window_seconds"], 30)
        self.assertEqual(row["controller_cpu_seconds"], 6)
        self.assertEqual(row["pacing"]["status"], "seeded_schedule_verified")

    def test_cold_and_sampled_zero_are_retained(self):
        self.parsed[str(self.run / "telemetry/metrics-0001.prom")][0]["value"] = 0
        self.final[0]["value"] = 1
        self.final.append({"name": "agent_sandbox_claim_creation_total",
                           "labels": {**self.labels, "launch_type": "cold", "pod_condition": "not_ready"},
                           "value": 1})
        row = self.summarize()
        self.assertEqual(row["cold"], 1)
        self.assertEqual(row["ready_pool_zero_samples"], 1)
        self.assertFalse(ci.calibration_check(row)["passed"])

    def test_raw_summary_contradiction_is_rejected(self):
        self.phase["latency"]["endToEndReady"]["p50Ms"] = 999
        with self.assertRaisesRegex(ValueError, "latency mismatch"):
            self.summarize()

    def test_duplicate_arrivals_are_rejected(self):
        self.records[1]["name"] = self.records[0]["name"]
        with self.assertRaisesRegex(ci.RoundError, "arrival IDs"):
            self.summarize()

    def test_counter_reset_is_rejected(self):
        self.parsed[str(self.run / "telemetry/metrics-0006.prom")][1]["value"] = 0
        with self.assertRaisesRegex(ci.RoundError, "counter reset"):
            self.summarize()

    def test_error_present_and_invalid_linux_values_are_rejected(self):
        self.records[0]["error_present"] = True
        with self.assertRaisesRegex(ValueError, "incomplete claim"):
            self.summarize()
        self.records[0].pop("error_present")
        self.host.write_text("".join(json.dumps({"utc": self.utc(i * 5), "cpu_active_percent": 50,
                                                 "error_present": True}) + "\n" for i in range(1, 7)))
        with self.assertRaisesRegex(ci.RoundError, "Linux CPU"):
            self.summarize()


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "runs").mkdir()
        self.args = SimpleNamespace(output=root, baseline=Path("/manager"), stress=Path("/stress"),
                                    metrics_parser=Path("/parser"), schedule_emitter=None)
        self.registration = {"source_commits": {"baseline": ci.BASELINE_COMMIT},
                             "binary_sha256": {}, "controller_cpus": [2, 5]}
        self.entry = {"sample": "ci-cal-r2", "kind": "calibration", "revision": "baseline",
                      "rate": 2, "seconds": 30, "pair": None, "slot": 2}

    def test_workload_error_cleans_namespace_and_manager_and_redacts_exception(self):
        manager = mock.Mock()
        with mock.patch.object(ci, "kube", return_value="") as kube, \
                mock.patch.object(ci, "ports_available"), \
                mock.patch.object(ci.subprocess, "Popen", return_value=manager) as popen, \
                mock.patch.object(ci, "wait_manager"), \
                mock.patch.object(ci.workload, "observe_workload", side_effect=RuntimeError("SECRET")), \
                mock.patch.object(ci.workload, "wait_namespace_deleted") as deleted, \
                mock.patch.object(ci, "stop_manager") as stopped:
            row = ci.run_one(self.entry, self.args, self.registration, ({}, {}))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["cleanup"], "complete")
        self.assertEqual(kube.call_args_list[-1].args[1:3], ("delete", "namespace"))
        deleted.assert_called_once()
        stopped.assert_called_once_with(manager)
        self.assertEqual(popen.call_args.args[0][2], "2,5")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("SECRET", json.dumps(row))
        self.assertTrue((self.args.output / "runs/ci-cal-r2/summary.json").exists())

    def test_namespace_collision_preserves_existing_resources(self):
        with mock.patch.object(ci, "kube", return_value="namespace/existing") as kube, \
                mock.patch.object(ci.subprocess, "Popen") as popen:
            row = ci.run_one(self.entry, self.args, self.registration, ({}, {}))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(kube.call_count, 1)
        popen.assert_not_called()

    def test_cleanup_failure_invalidates_success(self):
        manager = mock.Mock()
        manager.poll.return_value = None
        with mock.patch.object(ci, "kube", return_value=""), \
                mock.patch.object(ci, "ports_available"), \
                mock.patch.object(ci.subprocess, "Popen", return_value=manager), \
                mock.patch.object(ci, "wait_manager"), \
                mock.patch.object(ci.workload, "observe_workload"), \
                mock.patch.object(ci, "http_bytes", return_value=b"metric 1\n"), \
                mock.patch.object(ci, "summarize_run", return_value=completed(self.entry)), \
                mock.patch.object(ci.workload, "wait_namespace_deleted", side_effect=TimeoutError), \
                mock.patch.object(ci, "stop_manager") as stopped:
            row = ci.run_one(self.entry, self.args, self.registration, ({}, {}))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["cleanup"], "failed")
        stopped.assert_called_once_with(manager)

    def test_manager_readiness_and_termination_are_bounded(self):
        manager = mock.Mock(pid=123)
        manager.poll.return_value = 1
        with self.assertRaisesRegex(ci.RoundError, "exited"):
            ci.wait_manager(manager)
        manager.poll.return_value = None
        manager.wait.side_effect = [subprocess.TimeoutExpired("manager", 20), 0]
        with mock.patch.object(ci.os, "killpg") as kill:
            ci.stop_manager(manager)
        self.assertEqual([c.args[1] for c in kill.call_args_list], [ci.signal.SIGTERM, ci.signal.SIGKILL])


class CliTests(unittest.TestCase):
    def test_new_output_only_legal_affinity_and_no_secret_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            binary = root / "binary"
            binary.write_bytes(b"fixture")
            binary.chmod(0o700)
            kube = root / "credential"
            kube.write_text("SECRET-CREDENTIAL")
            output = root / "results"
            argv = [item for role in ("baseline", "candidate", "stress", "metrics-parser")
                    for item in ("--" + role, str(binary))]
            argv += ["--kubeconfig", str(kube), "--output", str(output)]
            with mock.patch.object(ci.os, "sched_getaffinity", return_value={2, 5}), \
                    mock.patch.object(ci, "execute", return_value=0) as execute:
                self.assertEqual(ci.main(argv), 0)
            config = ci.read(output / "config.json")
            self.assertEqual(config["controller_cpus"], [2, 5])
            self.assertNotIn(str(kube), json.dumps(config))
            self.assertNotIn("SECRET-CREDENTIAL", json.dumps(config))
            execute.assert_called_once()
            with mock.patch.object(ci, "execute") as execute, mock.patch("builtins.print"):
                self.assertEqual(ci.main(argv), 1)
                execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
