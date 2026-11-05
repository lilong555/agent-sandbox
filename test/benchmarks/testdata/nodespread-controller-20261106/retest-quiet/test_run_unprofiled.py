import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from run_unprofiled import MetricsSampler, controller_args, environments, observe_workload, workload_args


class ControllerArgsTests(unittest.TestCase):
    def test_only_profiler_flags_change(self):
        original = [
            "--extensions", "--enable-pprof-debug",
            "--pprof-mutex-profile-fraction=1", "--pprof-block-profile-rate=0",
            "--sandbox-claim-concurrent-workers=50",
        ]
        self.assertEqual(controller_args(original), [
            "--extensions", "--pprof-mutex-profile-fraction=0",
            "--pprof-block-profile-rate=0", "--sandbox-claim-concurrent-workers=50",
        ])
        self.assertIn("--enable-pprof-debug", original)

    def test_controller_gomaxprocs_does_not_leak_into_stress(self):
        parent = {"PATH": "/bin"}
        manager, stress = environments(parent, "/tmp/kube")
        self.assertEqual(manager["GOMAXPROCS"], "4")
        self.assertNotIn("GOMAXPROCS", stress)
        self.assertEqual(parent, {"PATH": "/bin"})
        self.assertEqual(manager["KUBECONFIG"], stress["KUBECONFIG"])

    def test_existing_stress_environment_is_preserved(self):
        manager, stress = environments({"GOMAXPROCS": "6"}, "/tmp/kube")
        self.assertEqual(manager["GOMAXPROCS"], "4")
        self.assertEqual(stress["GOMAXPROCS"], "6")

    def test_workload_duration_rate_and_inventory(self):
        command = workload_args("/tmp/stress", Path("/tmp/run"), "sample", 4, 120, 128)
        self.assertIn("-sustained-rate=4", command)
        self.assertIn("-sustained-seconds=120", command)
        self.assertIn("-sustained-pool-headroom=32s", command)
        self.assertIn("-profile-controller=false", command)
        with self.assertRaises(ValueError):
            workload_args("/tmp/stress", Path("/tmp/run"), "sample", 3, 120, 128)


class ObservationTests(unittest.TestCase):
    @staticmethod
    def fake_capture(sampler, tag):
        sampler.result["samples"].append({"tag": tag, "status": "complete"})

    def test_arrival_boundaries_and_complete_log(self):
        script = (
            "print('[claims-warm-sustained#1] preparing')\n"
            "print('[claims-warm-sustained#1] arrivals: 8/s Poisson')\n"
            "print('[claims-warm-sustained#1] arrival window done: 1 created')\n"
            "print('[claims-warm-sustained#1] done: 1 Ready')\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            with mock.patch.object(MetricsSampler, "capture", self.fake_capture):
                result = observe_workload([sys.executable, "-u", "-c", script], os.environ, run)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["boundary"]["kind"], "arrivals_finished")
            self.assertEqual([row["tag"] for row in result["samples"]], ["start", "end"])
            self.assertIn("done: 1 Ready", (run / "stress.log").read_text())
            self.assertEqual(json.loads((run / "telemetry" / "run.json").read_text()), result)

    def test_missing_window_is_retained_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "MissingArrivalWindow"):
                observe_workload([sys.executable, "-c", "print('no workload')"], os.environ, run)
            result = json.loads((run / "telemetry" / "run.json").read_text())
            self.assertEqual(result["status"], "failed")

    def test_deadline_stops_child_and_preserves_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            with self.assertRaises(TimeoutError):
                observe_workload(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    os.environ, run, timeout=0.1,
                )
            result = json.loads((run / "telemetry" / "run.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("TimeoutError", result["errors"])


if __name__ == "__main__":
    unittest.main()
