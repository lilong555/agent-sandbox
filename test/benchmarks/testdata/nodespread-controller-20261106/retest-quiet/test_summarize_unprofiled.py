"""Lightweight, isolated fixtures for the unprofiled result analyzer."""

import copy
import json
from pathlib import Path
import tempfile
import unittest

import summarize_unprofiled as analyzer


def utc(second):
    minute, second = divmod(second, 60)
    return f"2026-11-05T00:{minute:02d}:{second:02d}Z"


def point(second):
    return {"utc": utc(second), "monotonic_seconds": 100 + second}


def entry(sample="test", revision="baseline", slot=1, group="long8", pair=1):
    return {
        "sample": sample, "revision": revision, "slot": slot, "pair": pair,
        "group": group, "rate": 8, "seconds": 120, "pool": 128,
        "warmup": False, "unprofiled": True, "status": "completed",
    }


def configuration(revision="baseline"):
    return dict(copy.deepcopy(analyzer.REGISTERED_RUNTIME),
                commit=analyzer.REGISTERED_COMMITS[revision])


def registered_manifest():
    schedule = analyzer.registered_schedule(extended=True)
    return {
        "status": "completed", "windows_monitor_exit": 0, "schedule": schedule,
        "runs": [dict(item, status="completed") for item in schedule],
    }


def row(sample, revision, slot, value, group="long8", pair=1):
    result = entry(sample, revision, slot, group, pair)
    result.update(status="complete", errors=[], created=100,
                  binary_sha256={"stress": analyzer.REGISTERED_BINARIES["stress"],
                                 "manager": analyzer.REGISTERED_BINARIES[revision]},
                  configuration=configuration(revision))
    result.update({key: value for key in analyzer.MEASURES})
    return result


class TimeTests(unittest.TestCase):
    def test_nanoseconds_and_timezone_are_preserved(self):
        a = analyzer.timestamp_ns("2026-11-05T08:00:00.123456789+08:00")
        b = analyzer.timestamp_ns("2026-11-05T00:00:00.123456788Z")
        self.assertEqual(a - b, 1)
        self.assertEqual(analyzer.timestamp_ns("2026-11-05T00:00:00.1Z"),
                         analyzer.timestamp_ns("2026-11-05T00:00:00.100000000Z"))
        with self.assertRaises(ValueError):
            analyzer.timestamp_ns("2026-11-05T00:00:00")


class PairTests(unittest.TestCase):
    def test_exact_sign_flip_and_bootstrap_are_pair_level(self):
        result = analyzer.paired_inference([(100, 50)] * 6)
        self.assertEqual(result["n_pairs"], 6)
        self.assertEqual(result["two_sided_exact_sign_flip_p"], 2 / 64)
        self.assertAlmostEqual(result["percent_change"], -50)
        for value in result["percent_change_ci95"]:
            self.assertAlmostEqual(value, -50)
        self.assertEqual(result["bootstrap_resamples"], 20000)
        with self.assertRaises(ValueError):
            analyzer.paired_inference([(100, 50)] * 5)

    def test_null_effect(self):
        result = analyzer.paired_inference([(12, 12)] * 6, iterations=100)
        self.assertEqual(result["two_sided_exact_sign_flip_p"], 1)
        self.assertEqual(result["ratio_ci95"], [1, 1])

    def test_pair_changes_are_not_ratio_of_pooled_medians(self):
        rows = [
            row("a1", "baseline", 1, 100, pair=1),
            row("b1", "candidate", 2, 90, pair=1),
            row("b2", "candidate", 1, 100, pair=2),
            row("a2", "baseline", 2, 200, pair=2),
        ]
        groups, _ = analyzer.group_results(rows)
        pairs = groups["long8"]["pairs"]
        self.assertAlmostEqual(pairs[0]["percent_change"]["p50_ms"], -10)
        self.assertAlmostEqual(pairs[1]["percent_change"]["p50_ms"], -50)
        description = next(iter(groups["long8"]["descriptive_by_revision"].values()))
        self.assertEqual(description["baseline"]["p50_ms"],
                         {"median": 150, "min": 100, "max": 200})

    def test_negative_control_uses_second_over_first_and_stays_separate(self):
        rows = [row("n1", "candidate", 1, 20, "noise-candidate"),
                row("n2", "candidate", 2, 30, "noise-candidate"),
                row("a", "baseline", 1, 100), row("b", "candidate", 2, 50)]
        groups, _ = analyzer.group_results(rows)
        noise = groups["noise-candidate"]
        self.assertEqual(noise["kind"], "negative_control")
        self.assertEqual(noise["pairs"][0]["percent_change"]["p50_ms"], 50)
        self.assertEqual(noise["pairs"][0]["ratio"]["p50_ms"], 1.5)
        self.assertEqual(noise["descriptive_by_revision"], {})
        self.assertNotIn("primary_inference", noise)
        self.assertEqual(len(groups["long8"]["pairs"]), 1)
        rows[1]["binary_sha256"]["manager"] = "changed"
        _, errors = analyzer.group_results(rows)
        self.assertTrue(any("Negative-control binary mismatch" in e for e in errors))

    def test_missing_or_mismatched_partner_is_reported(self):
        a, b = row("a", "baseline", 1, 100), row("b", "candidate", 1, 90)
        _, errors = analyzer.group_results([a, b])
        self.assertTrue(any("slots 1 and 2" in error for error in errors))
        b["slot"], b["rate"] = 2, 4
        _, errors = analyzer.group_results([a, b])
        self.assertTrue(any("exactly two runs" in error for error in errors))

    def test_manifest_missing_sample_and_schedule_mismatch(self):
        run = entry()
        manifest = {"status": "completed", "windows_monitor_exit": 0,
                    "runs": [run], "schedule": [dict(run, slot=2)]}
        self.assertTrue(any("slot" in e for e in analyzer.manifest_errors(manifest)))
        manifest["runs"] = []
        self.assertIn("Schedule/runs sample mismatch", analyzer.manifest_errors(manifest))

    def test_partial_allows_pending_runs_but_detects_mismatch(self):
        manifest = registered_manifest()
        first = manifest["runs"][0]
        manifest.update(status="running", runs=[first])
        self.assertEqual(analyzer.manifest_errors(manifest, partial=True), [])
        manifest["runs"] = [dict(first, slot=2)]
        self.assertTrue(any("slot" in error for error in
                            analyzer.manifest_errors(manifest, partial=True)))

    def test_partial_pair_waits_and_primary_inference_is_deferred(self):
        groups, errors = analyzer.group_results(
            [row("a", "baseline", 1, 100)], partial=True)
        self.assertEqual(errors, [])
        self.assertEqual(groups["long8"]["pairs"][0]["status"], "incomplete")
        self.assertEqual(groups["long8"]["primary_inference"]["status"], "deferred")

    def test_primary_keeps_all_six_pairs_with_cold_and_exhaustion_flags(self):
        rows = []
        for pair in range(1, 7):
            rows.extend((row(f"a{pair}", "baseline", 1, 100 + pair, pair=pair),
                         row(f"b{pair}", "candidate", 2, 90 + pair, pair=pair)))
        original, _ = analyzer.group_results(rows)
        rows[0].update(has_cold_starts=True, mixed_cold_start_scenario=True,
                       inventory_exhaustion=True, cold=47,
                       protocol_observations=[{"kind": "initial_ready_inventory"}])
        flagged, _ = analyzer.group_results(rows)
        group = flagged["long8"]
        self.assertEqual(group["primary_inference"], original["long8"]["primary_inference"])
        self.assertEqual(group["primary_inference"]["n_pairs"], 6)
        self.assertTrue(all(pair["status"] == "complete" for pair in group["pairs"]))
        self.assertEqual(group["scenario_observations"]["cold_start_samples"], ["a1"])
        self.assertEqual(
            group["scenario_observations"]["initial_inventory_observation_samples"], ["a1"])
        self.assertEqual(group["pairs"][0]["scenario_observations"]["inventory_exhaustion_samples"],
                         ["a1"])

    def test_registered_manifest_accepts_only_fixed_schedule_and_order(self):
        self.assertEqual(analyzer.manifest_errors(registered_manifest()), [])
        for mutation in ("order", "revision", "rate", "missing", "run_order"):
            manifest = registered_manifest()
            if mutation == "order":
                manifest["schedule"][0:2] = reversed(manifest["schedule"][0:2])
                manifest["runs"][0:2] = reversed(manifest["runs"][0:2])
            elif mutation == "missing":
                manifest["schedule"].pop()
                manifest["runs"].pop()
            elif mutation == "run_order":
                manifest["runs"][0:2] = reversed(manifest["runs"][0:2])
            else:
                for section in ("schedule", "runs"):
                    manifest[section][0][mutation] = "candidate" if mutation == "revision" else 4
            with self.subTest(mutation=mutation):
                self.assertTrue(analyzer.manifest_errors(manifest))

    def test_same_wrong_binaries_or_runtime_cannot_pass_by_matching(self):
        for mutation in ("manager", "stress", "seed", "commit", "controller_args"):
            rows = [row("a", "baseline", 1, 100), row("b", "candidate", 2, 82)]
            for item in rows:
                if mutation in ("manager", "stress"):
                    item["binary_sha256"][mutation] = "a" * 64
                else:
                    item["configuration"][mutation] = (
                        [7, 9] if mutation == "seed" else
                        [] if mutation == "controller_args" else "unregistered")
            groups, errors = analyzer.group_results(rows, partial=True)
            with self.subTest(mutation=mutation):
                self.assertTrue(any("Registered" in error for error in errors))
                self.assertEqual(groups["long8"]["pairs"][0]["status"], "invalid")

    def test_signed_latency_has_difference_and_nonpositive_base_has_null_ratio(self):
        for base in (-2, 0, 2):
            rows = [row("a", "baseline", 1, 100), row("b", "candidate", 2, 90)]
            for key in analyzer.SIGNED_LATENCY_MEASURES:
                rows[0][key], rows[1][key] = base, base + 1
            groups, _ = analyzer.group_results(rows, partial=True)
            pair = groups["long8"]["pairs"][0]
            for key in analyzer.SIGNED_LATENCY_MEASURES:
                self.assertEqual(pair["difference_ms"][key], 1)
                if base <= 0:
                    self.assertIsNone(pair["ratio"][key])
                    self.assertIsNone(pair["percent_change"][key])
                else:
                    self.assertEqual(pair["ratio"][key], 1.5)
                    self.assertEqual(pair["percent_change"][key], 50)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = dict(analyzer.registered_schedule(extended=True)[0], status="completed")
        self.path = self.root / "runs" / self.entry["sample"]
        (self.path / "workload").mkdir(parents=True)
        (self.path / "telemetry").mkdir()
        self.namespace = "np2179-test"
        self.config = {
            **configuration(), "revision": "baseline", "seconds": 60,
            "claims_per_second": 8, "pool": 128,
        }
        self.save("config.json", self.config)
        (self.path / "binaries.sha256").write_text(
            analyzer.REGISTERED_BINARIES["baseline"] + "  /some/bin/manager\n"
            + analyzer.REGISTERED_BINARIES["stress"] + "  /some/tools/stress\n")
        self.records = []
        for i in (1, 2):
            base = utc(i).removesuffix("Z")
            self.records.append({
                "name": f"claim-{i}", "namespace": self.namespace,
                "phase": "claims-warm-sustained", "phaseNumber": 1,
                "boundSandbox": f"pool-sandbox-{i}", "createCalled": base + ".000000001Z",
                "createReturned": base + ".001000001Z",
                "sandboxReady": base + ".004000001Z",
                "deleteCalled": base + ".005000001Z",
                "sandboxDeleted": base + ".006000001Z",
                "createAckMs": 1, "sandboxReadyMs": 4,
            })
        self.save_records()
        stats = lambda value: {"p50Ms": value, "p90Ms": value, "p99Ms": value}
        self.summary = {
            "config": {"namespace": self.namespace},
            "phases": [{
                "kind": "claims-warm-sustained", "phaseNumber": 1,
                "created": 2, "ready": 2, "failed": 0, "requested": 480,
                "latency": {"endToEndReady": stats(4), "createAck": stats(1)},
            }],
        }
        self.save("workload/summary.json", self.summary)
        self.capture = {
            "status": "complete", "exit_code": 0, "errors": [],
            "trigger": {"kind": "sustained", "phase": "claims-warm-sustained",
                        "phase_number": 1, "at": point(0)},
            "boundary": {"kind": "arrivals_finished", "at": point(60)},
            "samples": [
                {"tag": "start" if second == 0 else "end" if second == 60 else "periodic",
                 "started_at": point(second), "ended_at": point(second),
                 "artifact": f"metrics-{i:04d}.prom", "status": "complete"}
                for i, second in enumerate(range(0, 61, 5))
            ],
        }
        self.save("telemetry/run.json", self.capture)
        self.metrics = {}
        for i in range(13):
            name = self.path / "telemetry" / f"metrics-{i:04d}.prom"
            self.metrics[str(name.resolve())] = [
                self.metric("process_cpu_seconds_total", 10 + i / 2),
                self.metric("go_memstats_alloc_bytes_total", 1000 + i * 200 / 12),
                self.metric("agent_sandbox_warmpool_size", 128 if i == 0 else 120 if i == 12 else 90,
                            namespace=self.namespace, warmpool_name="p1-spool",
                            sandbox_status="ready"),
            ]
        self.metrics[str((self.path / "metrics-final.prom").resolve())] = [
            self.metric("agent_sandbox_claim_creation_total", 2,
                        namespace=self.namespace, warmpool_name="p1-spool",
                        launch_type="warm", pod_condition="ready"),
            self.metric("agent_sandbox_claim_creation_total", 999,
                        namespace="other", warmpool_name="p1-spool",
                        launch_type="cold", pod_condition="not_ready"),
        ]
        self.windows = [{"utc": utc(i), "cpu_percent": 20, "available_mb": 1000,
                         "pages_input_per_sec": 0, "processes": []} for i in range(0, 61, 5)]
        pressure = {key: {"some": {"avg10": 0}} for key in ("cpu", "memory", "io")}
        self.linux = [{"utc": utc(i), "cpu_active_percent": 10, "available_kb": 1024000,
                       "pswpin": 0, "pswpout": 0, "pressure": pressure} for i in range(0, 61, 5)]

    @staticmethod
    def metric(name, value, **labels):
        return {"name": name, "value": value, "labels": labels}

    def save(self, name, data):
        (self.path / name).write_text(json.dumps(data))

    def save_records(self):
        (self.path / "workload/sandboxes.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in self.records))

    def analyze(self):
        return analyzer.summarize_run(
            self.entry, self.root, self.windows, self.linux,
            metrics_reader=lambda paths: {str(p.resolve()): self.metrics[str(p.resolve())] for p in paths})

    def test_complete_run_counts_precise_latency_and_counter_window(self):
        row = self.analyze()
        self.assertEqual(row["errors"], [])
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["p50_ms"], 4)
        self.assertEqual(row["ready_minus_ack_p50_ms"], 3)
        self.assertEqual(row["ready_minus_ack_median_ms"], 3)
        self.assertEqual(row["created"], 2)
        self.assertEqual(row["requested"], 480)
        self.assertEqual(row["warm"], 2)
        self.assertEqual(row["controller_cpu_seconds"], 6)
        self.assertEqual(row["controller_alloc_bytes"], 200)
        self.assertEqual(row["counter_window_seconds"], 60)
        self.assertEqual(row["ready_pool_sampled_min"], 90)
        self.assertEqual(row["ready_pool_start"], 128)
        self.assertEqual(row["ready_pool_end"], 120)
        self.assertEqual(row["windows"]["samples"], 13)

    def test_missing_periodic_sample_preserves_workload_result(self):
        self.capture["samples"] = [self.capture["samples"][0], self.capture["samples"][-1]]
        self.save("telemetry/run.json", self.capture)
        row = self.analyze()
        self.assertEqual(row["status"], "invalid")
        self.assertEqual(row["p50_ms"], 4)
        self.assertTrue(any("periodic" in error for error in row["errors"]))

    def test_exported_go_durations_survive_utc_clock_difference(self):
        self.records[0]["createReturned"] = utc(1).removesuffix("Z") + ".001000999Z"
        self.save_records()
        result = self.analyze()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["ready_minus_ack_median_ms"], 3)
        self.assertAlmostEqual(result["utc_vs_exported_ack_max_abs_ms"], .000998)

    def test_median_of_request_differences_not_difference_of_medians(self):
        self.records.append(copy.deepcopy(self.records[0]))
        for i, (record, ready, ack) in enumerate(zip(
                self.records, (10, 20, 100), (9, 2, 50)), 1):
            base = utc(i).removesuffix("Z")
            record.update(
                name=f"claim-{i}", createCalled=base + ".000000001Z",
                createReturned=base + f".{ack:03d}000001Z",
                sandboxReady=base + f".{ready:03d}000001Z",
                deleteCalled=base + ".101000001Z", sandboxDeleted=base + ".102000001Z",
                createAckMs=ack, sandboxReadyMs=ready,
            )
        phase = self.summary["phases"][0]
        phase.update(created=3, ready=3)
        phase["latency"] = {
            "endToEndReady": {"p50Ms": 20, "p90Ms": 100, "p99Ms": 100},
            "createAck": {"p50Ms": 9, "p90Ms": 50, "p99Ms": 50},
        }
        self.metrics[str((self.path / "metrics-final.prom").resolve())][0]["value"] = 3
        self.save_records()
        self.save("workload/summary.json", self.summary)
        result = self.analyze()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["ready_minus_ack_median_ms"], 18)
        self.assertNotEqual(result["ready_minus_ack_median_ms"],
                            result["p50_ms"] - result["create_ack_p50_ms"])

    def test_failed_lifecycle_and_counter_mismatch_are_explicit(self):
        self.records[0]["error"] = "fixture failure"
        self.save_records()
        self.metrics[str((self.path / "metrics-final.prom").resolve())][0]["value"] = 1
        row = self.analyze()
        self.assertIn("Lifecycle contains errors", row["errors"])
        self.assertIn("Launch counters differ from actual claims", row["errors"])

    def test_redacted_lifecycle_error_marker_is_preserved(self):
        for marker in (True, False):
            with self.subTest(error_present=marker):
                self.records[0]["error_present"] = marker
                self.assertNotIn("error", self.records[0])
                self.save_records()
                result = self.analyze()
                self.assertEqual(result["status"], "invalid" if marker else "complete")
                self.assertEqual("Lifecycle contains errors" in result["errors"], marker)
                self.assertEqual(result["records"], 2)

    def test_cold_and_inventory_exhaustion_are_observations_not_errors(self):
        final = self.metrics[str((self.path / "metrics-final.prom").resolve())]
        final[0]["value"] = 1
        final.append(self.metric(
            "agent_sandbox_claim_creation_total", 1, namespace=self.namespace,
            warmpool_name="p1-spool", launch_type="cold", pod_condition="not_ready"))
        middle = self.metrics[str((self.path / "telemetry/metrics-0001.prom").resolve())]
        middle[2]["value"] = 0
        result = self.analyze()
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["cold"], 1)
        self.assertEqual(result["cold_percent"], 50)
        self.assertTrue(result["has_cold_starts"])
        self.assertTrue(result["mixed_cold_start_scenario"])
        self.assertTrue(result["inventory_exhaustion"])
        self.assertEqual(result["ready_pool_zero_samples"], 1)
        self.assertEqual(result["inventory_exhaustion_evidence"], "sampled_zero")
        self.assertTrue(result["scenario_notes"])
        middle[2]["value"] = 90
        unsampled = self.analyze()
        self.assertEqual(unsampled["status"], "complete")
        self.assertTrue(unsampled["has_cold_starts"])
        self.assertFalse(unsampled["inventory_exhaustion"])
        self.assertEqual(unsampled["inventory_exhaustion_evidence"], "not_observed")

    def test_missing_numbered_telemetry_artifact(self):
        self.capture["samples"][1]["artifact"] = "metrics-0002.prom"
        self.save("telemetry/run.json", self.capture)
        row = self.analyze()
        self.assertTrue(any("artifact number" in error for error in row["errors"]))

    def test_missing_host_samples(self):
        self.windows = self.windows[:1]
        row = self.analyze()
        self.assertEqual(row["status"], "invalid")
        self.assertTrue(any("Missing host samples" in error for error in row["errors"]))

    def test_counter_reset_is_rejected(self):
        key = str((self.path / "telemetry/metrics-0002.prom").resolve())
        self.metrics[key][0]["value"] = 1
        self.assertIn("CPU counter reset or invalid value", self.analyze()["errors"])

    def test_config_mismatch(self):
        self.config["pool"] = 256
        self.save("config.json", self.config)
        self.assertIn("Manifest/config mismatch: pool", self.analyze()["errors"])

    def test_registered_identity_and_runtime_are_required(self):
        original = copy.deepcopy(self.config)
        for field, value in (("commit", "unregistered"), ("seed", [7, 9]),
                             ("gomaxprocs", 8), ("cpus", [0]),
                             ("controller_args", []), ("metrics_interval_seconds", 30)):
            self.config = dict(copy.deepcopy(original), **{field: value})
            self.save("config.json", self.config)
            result = self.analyze()
            with self.subTest(field=field):
                self.assertEqual(result["status"], "invalid")
                self.assertTrue(any("Registered" in error for error in result["errors"]))
        self.config = copy.deepcopy(original)
        del self.config["stress_gomaxprocs"]
        self.save("config.json", self.config)
        self.assertIn("Registered runtime mismatch: stress_gomaxprocs", self.analyze()["errors"])
        self.save("config.json", original)
        (self.path / "binaries.sha256").write_text(
            analyzer.REGISTERED_BINARIES["candidate"] + "  manager\n"
            + analyzer.REGISTERED_BINARIES["stress"] + "  stress\n")
        self.assertIn("Registered manager binary mismatch", self.analyze()["errors"])

    def test_shifted_counter_window_fails_and_retains_measured_offsets(self):
        for sample in self.capture["samples"]:
            for key in ("started_at", "ended_at"):
                second = int(sample[key]["monotonic_seconds"] - 100) + 600
                sample[key] = point(second)
        self.save("telemetry/run.json", self.capture)
        result = self.analyze()
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["counter_start_offset_from_trigger_seconds"], 600)
        self.assertEqual(result["counter_end_offset_from_boundary_seconds"], 600)
        self.assertTrue(any("boundary delay" in error for error in result["errors"]))

    def test_boundary_delay_and_scrape_limit_are_inclusive(self):
        original = copy.deepcopy(self.capture)
        self.capture["samples"][-1]["started_at"] = point(65)
        self.capture["samples"][-1]["ended_at"] = point(65)
        self.save("telemetry/run.json", self.capture)
        self.assertEqual(self.analyze()["errors"], [])
        self.capture["samples"][-1]["started_at"] = point(66)
        self.capture["samples"][-1]["ended_at"] = point(66)
        self.save("telemetry/run.json", self.capture)
        self.assertTrue(any("boundary delay" in error for error in self.analyze()["errors"]))
        self.capture = copy.deepcopy(original)
        self.capture["samples"][-1]["ended_at"] = point(65)
        self.save("telemetry/run.json", self.capture)
        self.assertEqual(self.analyze()["errors"], [])
        self.capture["samples"][-1]["ended_at"] = point(66)
        self.save("telemetry/run.json", self.capture)
        self.assertTrue(any("scrape exceeds 5s" in error for error in self.analyze()["errors"]))

    def test_large_gap_fails_even_with_sequential_artifact_names(self):
        # Preserve valid artifact numbering while introducing a long sampling gap.
        for index, sample in enumerate(self.capture["samples"][1:-1], 1):
            sample["started_at"] = point(index)
            sample["ended_at"] = point(index)
        self.save("telemetry/run.json", self.capture)
        result = self.analyze()
        self.assertEqual(result["status"], "invalid")
        self.assertEqual(result["telemetry_max_midpoint_gap_seconds"], 49)
        self.assertIn("Telemetry sample gap exceeds 10s", result["errors"])

    def test_utc_only_boundary_shift_is_detected(self):
        for sample in self.capture["samples"]:
            for key in ("started_at", "ended_at"):
                sample[key]["utc"] = utc(int(sample[key]["monotonic_seconds"] - 100) + 600)
        self.save("telemetry/run.json", self.capture)
        result = self.analyze()
        self.assertEqual(result["counter_start_offset_from_trigger_seconds"], 0)
        self.assertEqual(result["counter_start_offset_from_trigger_utc_seconds"], 600)
        self.assertEqual(result["status"], "invalid")

    def test_host_gap_uses_fixed_tolerance_not_observed_cadence(self):
        self.windows = [self.windows[0], self.windows[-1]]
        result = self.analyze()
        self.assertEqual(result["status"], "invalid")
        self.assertTrue(any("Host telemetry gap" in error for error in result["errors"]))
        normal = {"cpu_percent": 20, "available_mb": 1000, "pages_input_per_sec": 0,
                  "processes": []}
        start, end = analyzer.timestamp_ns(utc(0)), analyzer.timestamp_ns(utc(60))
        sparse = [dict(normal, utc=utc(second)) for second in (0, 15, 30, 45, 60)]
        stats = analyzer.host_summary(sparse, start, end, True)
        self.assertEqual(stats["missing_tolerance_seconds"], 15)
        sparse[1]["utc"] = utc(16)
        with self.assertRaisesRegex(ValueError, "Host telemetry gap"):
            analyzer.host_summary(sparse, start, end, True)

    def test_initial_inventory_is_an_observation_and_accounts_for_start_adoptions(self):
        start = self.metrics[str((self.path / "telemetry/metrics-0000.prom").resolve())]
        for value, assessment in ((0, "not_supported"), (127, "not_supported")):
            start[2]["value"] = value
            result = self.analyze()
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["protocol_validity"], "review_required")
            self.assertEqual(result["initial_inventory"]["assessment"], assessment)
        start.append(self.metric(
            "agent_sandbox_claim_creation_total", 1, namespace=self.namespace,
            warmpool_name="p1-spool", launch_type="warm", pod_condition="ready"))
        result = self.analyze()
        self.assertEqual(result["initial_inventory"]["ready_plus_warm_ready_launches"], 128)
        self.assertEqual(result["initial_inventory"]["assessment"], "adoption_adjusted_support")
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["protocol_observations"])
        start[-1]["labels"]["launch_type"] = "cold"
        result = self.analyze()
        self.assertEqual(result["initial_inventory"]["assessment"], "not_supported")

    def test_csv_json_keep_invalid_and_complete_runs(self):
        row = self.analyze()
        invalid = dict(copy.deepcopy(row), sample="bad", status="invalid", errors=["missing fixture"])
        report = {"status": "invalid", "errors": ["missing fixture"], "runs": [row, invalid]}
        destination = self.root / "output"
        analyzer.write_report(report, destination)
        saved = json.loads((destination / "summary-unprofiled.json").read_text())
        self.assertEqual(len(saved["runs"]), 2)
        self.assertIn("missing fixture", (destination / "summary-unprofiled.csv").read_text())
        self.assertTrue((self.path / "workload/sandboxes.jsonl").exists())

    def test_partial_manifest_excludes_warmup_and_running_formal(self):
        results = self.root / "results"
        results.mkdir()
        schedule = analyzer.registered_schedule(extended=True)
        upcoming = schedule[1]
        manifest = {
            "status": "running", "schedule": schedule,
            "runs": [dict(entry("warm"), warmup=True, status="completed"),
                     self.entry, dict(upcoming, status="running")],
        }
        (results / "manifest.json").write_text(json.dumps(manifest))
        for kind, rows in (("windows", self.windows), ("linux", self.linux)):
            (results / f"{kind}-load.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows) + '{"utc":')
        report = analyzer.analyze(
            results, self.root,
            metrics_reader=lambda paths: {str(p.resolve()): self.metrics[str(p.resolve())] for p in paths},
            partial=True,
        )
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual([row["sample"] for row in report["runs"]], [self.entry["sample"]])
        self.assertEqual(report["pending_samples"], [item["sample"] for item in schedule[1:]])
        self.assertEqual(report["excluded_warmups"], ["warm"])
        self.assertIn("not additional statistically preregistered thresholds",
                      report["validation_criteria"]["basis"])
        analyzer.write_report(report, results)
        self.assertIn("incomplete", (results / "summary-unprofiled.csv").read_text())


if __name__ == "__main__":
    unittest.main()
