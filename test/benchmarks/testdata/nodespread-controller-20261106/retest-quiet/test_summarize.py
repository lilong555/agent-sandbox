import copy
import unittest
from unittest import mock

import summarize
from summarize import aligned, load_stats, metric, quantile, timestamp, verified_latency


class SummaryTests(unittest.TestCase):
    def test_quantile_matches_nearest_rank(self):
        self.assertEqual(quantile([4, 1, 2, 3], .5), 2)
        self.assertEqual(quantile([4, 1, 2, 3], .99), 4)
        self.assertIsNone(quantile([], .5))

    def test_metrics_preserve_labels(self):
        samples = [
            {"name": "claim", "labels": {"type": "warm"}, "value": 8},
            {"name": "claim", "labels": {"type": "cold"}, "value": 2},
        ]
        self.assertEqual(metric(samples, "claim"), 10)
        self.assertEqual(metric(samples, "claim", type="warm"), 8)
        self.assertEqual(metric(samples, "claim", type="pending"), 0)
        with self.assertRaises(ValueError):
            metric(samples, "missing", required=True)

    def test_window_alignment_uses_timezone_and_fraction(self):
        first = timestamp("2026-11-05T23:00:00.123456789+08:00")
        second = timestamp("2026-11-05T15:00:01.123456Z")
        records = [
            {"utc": "2026-11-05T15:00:00Z"},
            {"utc": "2026-11-05T15:00:00.5Z"},
            {"utc": "2026-11-05T15:00:02Z"},
        ]
        self.assertEqual(aligned(records, first, second), [records[1]])

    def test_short_fraction_is_padded(self):
        self.assertEqual(timestamp("2026-11-05T15:00:00.1Z"),
                         timestamp("2026-11-05T15:00:00.100000+00:00"))
        self.assertEqual(timestamp("2026-11-05T15:00:00.12345Z"),
                         timestamp("2026-11-05T15:00:00.123450+00:00"))

    def test_anonymized_monitor_errors_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "failed host telemetry"):
            load_stats([{"error_present": True}], windows=False)


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.records = [
            {
                "phase": "claims-warm-sustained",
                "createCalled": "2026-11-05T15:00:00Z",
                "createReturned": "2026-11-05T15:00:00.001Z",
                "sandboxReady": "2026-11-05T15:00:00.004Z",
                "deleteCalled": "2026-11-05T15:00:01Z",
                "sandboxDeleted": "2026-11-05T15:00:02Z",
                "sandboxReadyMs": 4.0, "createAckMs": 1.0,
            }
            for _ in range(2)
        ]
        self.phase = {
            "created": 2, "ready": 2, "failed": 0,
            "latency": {
                kind: {"count": 2, **{key: value for key in
                       ("minMs", "maxMs", "meanMs", "p50Ms", "p90Ms", "p95Ms", "p99Ms")}}
                for kind, value in (("endToEndReady", 4.0), ("createAck", 1.0))
            },
        }

    def test_raw_four_ms_rejects_copied_999_ms(self):
        self.phase["latency"]["endToEndReady"]["p50Ms"] = 999.0
        with self.assertRaisesRegex(ValueError, "endToEndReady.p50Ms"):
            verified_latency(self.records, self.phase, "fixture")

    def test_every_latency_summary_field_is_checked(self):
        for kind, stats in self.phase["latency"].items():
            for key in stats:
                with self.subTest(kind=kind, key=key):
                    phase = copy.deepcopy(self.phase)
                    phase["latency"][kind][key] += 1
                    with self.assertRaisesRegex(ValueError, f"{kind}.{key}"):
                        verified_latency(self.records, phase, "fixture")

    def test_phase_counts_and_record_count_must_agree(self):
        for key, value in (("created", 1), ("ready", 1), ("failed", 1),
                           ("created", 2.0), ("failed", False), ("ready", None)):
            with self.subTest(key=key, value=value):
                phase = copy.deepcopy(self.phase)
                phase[key] = value
                with self.assertRaisesRegex(ValueError, "Phase count mismatch"):
                    verified_latency(self.records, phase, "fixture")
        with self.assertRaisesRegex(ValueError, "Phase count mismatch"):
            verified_latency(self.records + [self.records[0]], self.phase, "fixture")
        with self.assertRaisesRegex(ValueError, "incomplete claim lifecycle"):
            verified_latency([], {"created": 0, "ready": 0, "failed": 0}, "fixture")

    def test_invalid_or_missing_exported_durations_are_rejected(self):
        for field in ("sandboxReadyMs", "createAckMs"):
            for value in (float("nan"), float("inf"), float("-inf"), -1, True, "4", None):
                with self.subTest(field=field, value=value):
                    records = copy.deepcopy(self.records)
                    records[0][field] = value
                    with self.assertRaisesRegex(ValueError, "Invalid exported latency"):
                        verified_latency(records, self.phase, "fixture")
            records = copy.deepcopy(self.records)
            del records[0][field]
            with self.assertRaisesRegex(ValueError, "Invalid exported latency"):
                verified_latency(records, self.phase, "fixture")

    def test_invalid_or_missing_summary_values_are_rejected(self):
        for kind, stats in self.phase["latency"].items():
            for key in stats:
                for value in (float("nan"), float("inf"), -1, True, "4", None):
                    with self.subTest(kind=kind, key=key, value=value):
                        phase = copy.deepcopy(self.phase)
                        phase["latency"][kind][key] = value
                        with self.assertRaisesRegex(ValueError, "latency mismatch"):
                            verified_latency(self.records, phase, "fixture")
                phase = copy.deepcopy(self.phase)
                del phase["latency"][kind][key]
                with self.assertRaisesRegex(ValueError, "latency mismatch"):
                    verified_latency(self.records, phase, "fixture")

    def test_incomplete_and_anonymized_error_lifecycles_are_rejected(self):
        for field in ("createCalled", "createReturned", "sandboxReady",
                      "deleteCalled", "sandboxDeleted"):
            records = copy.deepcopy(self.records)
            del records[0][field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "incomplete claim"):
                verified_latency(records, self.phase, "fixture")
        for field, value in (("error", "failure"), ("error_present", True)):
            records = copy.deepcopy(self.records)
            records[0][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "incomplete claim"):
                verified_latency(records, self.phase, "fixture")

    def test_zero_durations_are_valid(self):
        for record in self.records:
            record.update(sandboxReadyMs=0, createAckMs=0)
        for stats in self.phase["latency"].values():
            stats.update({key: 0 for key in stats if key != "count"})
        result = verified_latency(self.records, self.phase, "fixture")
        self.assertEqual(result["endToEndReady"]["meanMs"], 0)

    def test_summary_mean_nanosecond_truncation_preserves_raw_mean(self):
        self.records[1]["sandboxReadyMs"] = 4.000001
        stats = self.phase["latency"]["endToEndReady"]
        stats.update(maxMs=4.000001, p90Ms=4.000001, p95Ms=4.000001, p99Ms=4.000001)
        result = verified_latency(self.records, self.phase, "fixture")
        self.assertAlmostEqual(result["endToEndReady"]["meanMs"], 4.0000005, places=12)
        self.assertNotEqual(result["endToEndReady"]["meanMs"], stats["meanMs"])
        stats["meanMs"] = 4.000003
        with self.assertRaisesRegex(ValueError, "endToEndReady.meanMs"):
            verified_latency(self.records, self.phase, "fixture")

    def test_summarize_run_uses_exported_full_phase_durations(self):
        sample = "fixture"
        path = summarize.ROOT / "runs" / sample
        self.records[1]["createCalled"] = "2026-11-05T15:00:02Z"
        self.records[1]["createReturned"] = "2026-11-05T15:00:02.001Z"
        self.records[1]["sandboxReady"] = "2026-11-05T15:00:02.008000001Z"
        self.records[1]["deleteCalled"] = "2026-11-05T15:00:03Z"
        self.records[1]["sandboxDeleted"] = "2026-11-05T15:00:04Z"
        self.records[1]["sandboxReadyMs"] = 8.000001
        self.phase["latency"]["endToEndReady"].update(
            meanMs=6.0, maxMs=8.000001, p90Ms=8.000001, p95Ms=8.000001, p99Ms=8.000001)
        capture = {
            "status": "complete", "errors": [],
            "profile_window": {"within_observed_workload": True},
            "requests": {"cpu": {
                "started_at": {"utc": "2026-11-05T15:00:00Z"},
                "ended_at": {"utc": "2026-11-05T15:00:01Z"},
            }},
        }
        loaded = {
            path / "config.json": {"seconds": 2},
            path / "workload" / "summary.json": {"phases": [self.phase]},
            path / "profile" / "run.json": capture,
        }
        profiles = {
            (sample, file): {"sample_types": [
                {"type": kind, "total": 0, "queue_stack_total": 0, "queue_stack_percentage": 0}]}
            for file, kind in (("cpu.pprof", "cpu"), ("allocs.pprof", "alloc_space"),
                               ("mutex.pprof", "delay"))
        }
        metrics = {
            str(path / "metrics-final.prom"): [{
                "name": "agent_sandbox_claim_creation_total",
                "labels": {"launch_type": "warm", "pod_condition": "ready"}, "value": 2,
            }],
        }
        for suffix in ("metrics-start.txt", "metrics-end.txt"):
            metrics[str(path / "profile" / suffix)] = [
                {"name": name, "labels": labels, "value": value}
                for name, labels, value in (
                    ("process_cpu_seconds_total", {}, 0),
                    ("go_memstats_alloc_bytes_total", {}, 0),
                    ("agent_sandbox_warmpool_size", {"sandbox_status": "ready"}, 128),
                )
            ]
        entry = {"sample": sample, "rate": 2, "pair": 1, "revision": "baseline"}
        with mock.patch.object(summarize, "load", side_effect=loaded.__getitem__), \
                mock.patch.object(summarize, "lines", return_value=self.records), \
                mock.patch.object(summarize, "load_stats", return_value={}):
            row = summarize.summarize_run(entry, profiles, metrics, [], [])
            self.assertEqual(row["p50_ms"], 4.0)
            self.assertEqual(row["p90_ms"], 8.000001)
            self.assertEqual(row["p99_ms"], 8.000001)
            self.assertAlmostEqual(row["mean_ms"], 6.0000005, places=12)
            self.assertEqual(row["create_ack_p50_ms"], 1.0)
            self.assertEqual(row["complete_lifecycles"], 2)
            self.assertEqual(row["profile_cohort_count"], 1)
            self.phase["latency"]["endToEndReady"]["p50Ms"] = 999.0
            with self.assertRaisesRegex(ValueError, "endToEndReady.p50Ms"):
                summarize.summarize_run(entry, profiles, metrics, [], [])


if __name__ == "__main__":
    unittest.main()
