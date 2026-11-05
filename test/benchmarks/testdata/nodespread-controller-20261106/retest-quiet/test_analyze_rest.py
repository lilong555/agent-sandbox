"""Mock-only REST analysis tests; no parser process, cluster or Go tool is run."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import analyze_rest as rest
import repeat_suite


def metric(code="200", method="GET", value=1, host="https://secret-host/?token=secret"):
    return {"name": rest.FAMILY, "labels": {"code": code, "method": method, "host": host},
            "value": value}


def point(second):
    return {"utc": f"2026-11-06T00:00:{second:02d}.123456789Z",
            "monotonic_seconds": second}


class CounterTests(unittest.TestCase):
    def test_aggregation_keeps_raw_codes_methods_and_removes_hosts(self):
        rows = [metric("409", "PATCH", 2), metric("409", "PATCH", 3, host="other"),
                metric("429", value=4), metric("503", value=5),
                metric("<error>", value=6), metric("418", value=7)]
        report = rest.aggregate(rest.counters(rows))
        encoded = json.dumps(report)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("host", encoded)
        codes = {r["code"]: r for r in report["by_code_method"]}
        self.assertEqual(codes["409"]["requests"], 5)
        self.assertEqual(codes["409"]["method"], "PATCH")
        self.assertEqual(codes["429"]["category"], "throttled_429")
        self.assertEqual(codes["503"]["category"], "server_error_5xx")
        self.assertEqual(codes["<error>"]["category"], "transport_error")
        self.assertEqual(codes["418"]["requests"], 7)

    def test_new_series_allowed_but_per_host_resets_rejected(self):
        before = rest.counters([metric(value=10)])
        after = rest.counters([metric(value=12), metric("409", value=3)])
        self.assertEqual(rest.aggregate(rest.delta(before, after))["total"], 5)
        masked_reset = rest.counters([metric(value=9), metric(value=50, host="other")])
        with self.assertRaisesRegex(rest.AnalysisError, "decreased"):
            rest.delta(before, masked_reset)
        with self.assertRaises(rest.AnalysisError):
            rest.delta(before, rest.counters([metric("409")]))

    def test_missing_family_and_invalid_values(self):
        for values in ([], [dict(metric(), name="process_cpu_seconds_total")],
                       [metric(value=-1)], [metric(value=.5)], [metric(value=float("nan"))]):
            with self.subTest(values=values), self.assertRaises(rest.AnalysisError):
                rest.counters(values)

    def test_window_and_clock_order(self):
        first = {"started_at": point(1), "ended_at": point(2)}
        last = {"started_at": point(11), "ended_at": point(12)}
        self.assertEqual(rest.interval(first, last)["duration_seconds"], 10)
        with self.assertRaises(rest.AnalysisError):
            rest.interval(last, first)


class SuiteTests(unittest.TestCase):
    def setUp(self):
        registered = repeat_suite.schedule(extended=True)
        self.runs = [dict(item, status="completed") for item in registered]
        self.manifest = {"status": "completed", "windows_monitor_exit": 0,
                         "runs": self.runs, "schedule": registered}
        self.telemetry = {
            "status": "complete", "exit_code": 0, "errors": [],
            "trigger": {"at": point(0)}, "boundary": {"at": point(30)},
            "samples": [
                {"tag": tag, "artifact": f"metrics-{i:04d}.prom", "status": "complete",
                 "started_at": point(i * 10), "ended_at": point(i * 10)}
                for i, tag in enumerate(("start", "periodic", "periodic", "end"))]}

    def load(self, path):
        if path.name == "manifest.json":
            return self.manifest
        if path.name == "run.json":
            return self.telemetry
        return {"phases": [{"created": 452, "ready": 452, "failed": 0}]}

    def parse(self, paths, binary):
        reports = {}
        for path in paths:
            value = 9 if path.name == "metrics-final.prom" else 1 + int(path.stem.split("-")[1])
            reports[str(path.resolve())] = [metric("200", value=value * 10),
                                            metric("409", "PATCH", value)]
        return reports

    def test_all_24_runs_detail_intervals_and_safe_json(self):
        with patch.object(rest, "read_json", side_effect=self.load), \
                patch.object(rest, "read_metrics", side_effect=self.parse) as parser:
            report = rest.analyze(Path("/results"), Path("/new-parser"), Path("/experiment"))
        parser.assert_called_once()
        self.assertEqual(parser.call_args.kwargs["binary"], Path("/new-parser"))
        self.assertEqual(len(report["runs"]), 24)
        detail = next(row for row in report["runs"] if row["sample"] == rest.DETAIL_SAMPLE)
        self.assertEqual(detail["actual_created"], 452)
        self.assertEqual(detail["actual_failed"], 0)
        self.assertEqual(detail["window_requests"]["total"], 33)
        self.assertEqual(detail["process_total"]["total"], 99)
        self.assertEqual(len(detail["periodic_intervals"]), 1)
        self.assertEqual(detail["periodic_intervals"][0]["requests"]["total"], 11)
        self.assertNotIn("secret-host", json.dumps(report))
        self.assertNotIn("token", json.dumps(report))
        self.assertNotIn("periodic_intervals", report["runs"][0])

    def test_running_manifest_prevents_parser_execution(self):
        self.manifest["status"] = "running"
        with patch.object(rest, "read_json", side_effect=self.load), \
                patch.object(rest, "read_metrics") as parser:
            with self.assertRaises(rest.AnalysisError):
                rest.analyze(Path("/results"))
        parser.assert_not_called()

    def test_incomplete_24_and_failed_telemetry_rejected(self):
        for mutation in ("count", "telemetry"):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == "count":
                    self.runs.pop()
                    self.manifest["schedule"].pop()
                else:
                    self.telemetry["errors"] = ["fixture"]
                with patch.object(rest, "read_json", side_effect=self.load), \
                        patch.object(rest, "read_metrics") as parser:
                    with self.assertRaises(rest.AnalysisError):
                        rest.analyze(Path("/results"))
                parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
