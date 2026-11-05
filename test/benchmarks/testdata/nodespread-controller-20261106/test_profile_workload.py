"""CPU-only harness tests; all HTTP responses are disposable test fixtures."""

import contextlib
import gzip
import http.server
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import profile_workload as harness


BURST = "[claims-warm#1] firing 128 claims simultaneously"
SUSTAINED = (
    "[claims-warm-sustained#2] arrivals: 30.0/s Poisson for 60s "
    "(~1800 claims) across 1 namespace(s), dwell 5s after Ready"
)


class TriggerTests(unittest.TestCase):
    def test_real_phase_lines(self):
        self.assertEqual(harness.match_trigger("2026/11/05 22:00:00 " + BURST),
                         {"kind": "warm_burst", "phase": "claims-warm",
                          "phase_number": 1})
        self.assertEqual(harness.match_trigger(SUSTAINED)["kind"], "sustained")

    def test_setup_progress_and_other_phase_do_not_trigger(self):
        for line in (
            "[claims-warm#1] provisioning warm pool x with 128 replicas",
            "[claims-warm-sustained#2] window [0s-10s): arrivals=30 ready=30",
            "[probe#1] firing 128 claims simultaneously",
            "[claims-warm#1] firing x claims simultaneously",
        ):
            with self.subTest(line=line):
                self.assertIsNone(harness.match_trigger(line))

    def test_phase_boundaries(self):
        trigger = harness.match_trigger(BURST)
        self.assertEqual(harness.match_boundary(
            "[claims-warm#1] all 128 created claims are Ready (0 failed to create)",
            trigger), "claims_ready")
        self.assertEqual(harness.match_boundary(
            "[claims-warm#1] cleaning up 128 claims, pool p, template t",
            trigger), "cleanup_started")
        self.assertIsNone(harness.match_boundary(
            "[claims-warm#2] cleaning up 128 claims", trigger))
        self.assertEqual(harness.match_boundary(
            "[claims-warm-sustained#2] arrival window done: 30 claims launched",
            harness.match_trigger(SUSTAINED)), "arrivals_finished")


class ArgumentTests(unittest.TestCase):
    def parse(self, *arguments):
        return harness.parse_args(["--output-dir", "/tmp/example", *arguments])

    def test_defaults_and_passthrough(self):
        args = self.parse("--", "./tools/stress", "--timeout", "2m",
                          "--output-dir", "stress-data", "--image=x")
        self.assertEqual(args.profile_seconds, 30)
        self.assertEqual(args.profile_delay, 10)
        self.assertEqual(args.metrics_url, "http://127.0.0.1:18080")
        self.assertEqual(args.command, [
            "./tools/stress", "--timeout", "2m", "--output-dir", "stress-data",
            "--image=x"])

    def test_short_burst_and_ipv6(self):
        args = self.parse("--profile-delay", "0", "--profile-seconds", "15",
                          "--metrics-url", "http://[::1]:18080/", "--", "stress")
        self.assertEqual(args.profile_delay, 0)
        self.assertEqual(args.profile_seconds, 15)
        self.assertEqual(args.metrics_url, "http://[::1]:18080")

    def test_invalid_commands_and_options(self):
        for args in (
            (), ("--",), ("stress",),
            ("--", "--phases", "claims-warm"), ("--", "stress", "bad\0arg"),
            ("--profile-seconds", "0", "--", "stress"),
            ("--profile-delay", "-1", "--", "stress"),
            ("--profile-delay", "nan", "--", "stress"),
            ("--timeout-seconds", "inf", "--", "stress"),
            ("--timeout-seconds", "0", "--", "stress"),
            ("--metrics-url", "http://user:secret@localhost:18080", "--", "stress"),
            ("--metrics-url", "http://example.org:18080", "--", "stress"),
            ("--metrics-url", "http://127.0.0.1:18080/?token=secret", "--", "stress"),
        ):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.parse(*args)
                self.assertEqual(error.exception.code, 2)


class LocalServer(http.server.ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "output"
        self.paths = []
        self.delay = 0.15
        self.http_error = False
        self.fixture = gzip.compress(b"disposable HTTP test fixture, not a profile")
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append((self.path, time.monotonic()))
                if owner.http_error:
                    self.send_error(503)
                    return
                if self.path.startswith("/debug/pprof/"):
                    time.sleep(owner.delay)
                    body = owner.fixture
                else:
                    body = b"go_goroutines 10\n"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = LocalServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.assertFalse(self.server_thread.is_alive())

    def arguments(self, source, *extra):
        return [
            "--output-dir", str(self.output),
            "--metrics-url", f"http://127.0.0.1:{self.server.server_port}",
            "--profile-delay", "0", "--profile-seconds", "1",
            "--timeout-seconds", "5", "--stop-grace-seconds", "0.1",
            *extra, "--", sys.executable, "-u", "-c", source,
        ]

    def run_harness(self, source, *extra):
        with contextlib.redirect_stdout(io.StringIO()):
            code = harness.main(self.arguments(source, *extra))
        result = json.loads((self.output / "run.json").read_text())
        self.assertFalse(list(self.output.glob("*.part")))
        self.assertEqual(harness.multiprocessing.active_children(), [])
        return code, result

    def test_concurrent_capture_and_unmodified_logging(self):
        source = (
            "import os,sys,time; "
            "assert os.environ['KUBECONFIG']=='/private/test-kubeconfig'; "
            f"print({SUSTAINED!r}); "
            "print('stderr-marker', file=sys.stderr); time.sleep(2)"
        )
        with mock.patch.dict(os.environ, {"KUBECONFIG": "/private/test-kubeconfig"}):
            code, result = self.run_harness(source)
        self.assertEqual(code, 0)
        self.assertEqual((self.output / "stress.log").read_text(),
                         SUSTAINED + "\nstderr-marker\n")
        self.assertNotIn("test-kubeconfig", (self.output / "run.json").read_text())
        for name in harness.PROFILE_NAMES:
            self.assertEqual((self.output / f"{name}.pprof").read_bytes(),
                             self.fixture)
        self.assertTrue(result["profile_window"]["within_observed_workload"])
        self.assertLess(result["profile_window"]["start_skew_seconds"], 0.2)
        paths = [path for path, _ in self.paths]
        self.assertEqual(paths.count("/metrics"), 2)
        for endpoint in ("profile", "mutex", "allocs"):
            self.assertIn(f"/debug/pprof/{endpoint}?seconds=1", paths)

    def test_nonzero_without_trigger(self):
        code, result = self.run_harness("import sys; print('setup failed'); sys.exit(7)")
        self.assertEqual(code, 7)
        self.assertEqual(result["stress"]["returncode"], 7)
        self.assertIn({"kind": "trigger_not_observed"}, result["errors"])
        self.assertEqual(self.paths, [])

    def test_workload_ends_during_delay(self):
        code, result = self.run_harness(
            f"print({BURST!r})", "--profile-delay", "2")
        self.assertEqual(code, 1)
        self.assertEqual(self.paths, [])
        self.assertEqual(result["requests"]["cpu"]["status"], "skipped")

    def test_early_cleanup_cancels_requests(self):
        self.delay = 1.2
        source = (
            f"import time; print({BURST!r}); time.sleep(0.6); "
            "print('[claims-warm#1] cleaning up 128 claims'); time.sleep(0.3)"
        )
        code, result = self.run_harness(source)
        self.assertEqual(code, 1)
        self.assertEqual(result["workload"]["end"]["kind"], "cleanup_started")
        self.assertFalse(result["profile_window"]["complete"])
        self.assertFalse(list(self.output.glob("*.pprof")))
        self.assertIn(result["requests"]["cpu"]["status"], ("cancelled", "skipped"))

    def test_profiler_error_is_reported(self):
        self.http_error = True
        code, result = self.run_harness(
            f"import time; print({BURST!r}); time.sleep(1)")
        self.assertEqual(code, 1)
        self.assertEqual(result["requests"]["cpu"]["error"]["http_status"], 503)
        self.assertFalse(list(self.output.glob("*.pprof")))

    def test_timeout_kills_owned_process(self):
        code, result = self.run_harness(
            "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "time.sleep(60)", "--timeout-seconds", "0.2")
        self.assertEqual(code, 124)
        self.assertEqual(result["stress"]["returncode"], -signal.SIGKILL)

    def test_request_deadline_reaps_slow_http_workers(self):
        self.delay = 2
        with mock.patch.object(harness, "HTTP_GRACE_SECONDS", 0.5):
            code, result = self.run_harness(
                f"import time; print({BURST!r}); time.sleep(2.5)")
        self.assertEqual(code, 1)
        self.assertEqual(result["requests"]["cpu"]["status"], "timeout")
        self.assertFalse(list(self.output.glob("*.pprof")))

    def test_interrupt_during_profiles_cancels_workers(self):
        self.delay = 1.2
        command = [
            sys.executable, "-B", str(Path(harness.__file__)),
            *self.arguments(f"import time; print({SUSTAINED!r}); time.sleep(60)"),
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if len([path for path, _ in self.paths
                        if path.startswith("/debug/pprof/")]) == 3:
                    break
                time.sleep(0.02)
            else:
                self.fail("HTTP fixtures did not start")
            process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 143, output)
            result = json.loads((self.output / "run.json").read_text())
            for name in harness.PROFILE_NAMES:
                self.assertEqual(result["requests"][name]["status"], "cancelled")
            self.assertFalse(list(self.output.glob("*.pprof")))
            self.assertFalse(list(self.output.glob("*.part")))
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_interrupt_is_reported_and_reaped(self):
        command = [
            sys.executable, "-B", str(Path(harness.__file__)),
            *self.arguments("import time; print('started',flush=True); time.sleep(60)"),
        ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                log = self.output / "stress.log"
                if log.exists() and b"started" in log.read_bytes():
                    break
                time.sleep(0.02)
            else:
                self.fail("stress fixture did not start")
            process.send_signal(signal.SIGINT)
            output, _ = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, output)
            result = json.loads((self.output / "run.json").read_text())
            self.assertEqual(result["exit_code"], 130)
            self.assertIsNotNone(result["stress"]["returncode"])
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

    def test_existing_artifacts_are_preserved(self):
        self.output.mkdir()
        original = self.output / "stress.log"
        original.write_text("prior run\n")
        with contextlib.redirect_stdout(io.StringIO()):
            code = harness.main(self.arguments("raise AssertionError()"))
        self.assertEqual(code, 2)
        self.assertEqual(original.read_text(), "prior run\n")
        self.assertFalse((self.output / "run.json").exists())


if __name__ == "__main__":
    unittest.main()
