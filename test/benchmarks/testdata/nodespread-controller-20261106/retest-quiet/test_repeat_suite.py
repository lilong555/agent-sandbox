import collections
import unittest

from repeat_suite import cpu_times, linux_sample, schedule


class ScheduleTests(unittest.TestCase):
    def test_balanced_replicates_and_unique_names(self):
        runs = schedule()
        self.assertEqual(len(runs), 12)
        self.assertEqual(len({run["sample"] for run in runs}), 12)
        counts = collections.Counter((run["rate"], run["revision"]) for run in runs)
        self.assertEqual(set(counts.values()), {3})
        self.assertEqual(set(counts), {(2, "baseline"), (2, "candidate"),
                                       (8, "baseline"), (8, "candidate")})

    def test_each_pair_contains_both_revisions_and_order_is_balanced(self):
        runs = schedule()
        starts = []
        for index in range(0, len(runs), 2):
            first, second = runs[index:index + 2]
            self.assertEqual((first["pair"], first["rate"]), (second["pair"], second["rate"]))
            self.assertNotEqual(first["revision"], second["revision"])
            starts.append(first["revision"])
        self.assertEqual(collections.Counter(starts), {"baseline": 3, "candidate": 3})

    def test_proc_telemetry_schema(self):
        record, current = linux_sample(cpu_times())
        self.assertEqual(len(current), 4)
        self.assertGreater(record["available_kb"], 0)
        self.assertGreaterEqual(record["pswpin"], 0)
        self.assertEqual(set(record["pressure"]), {"cpu", "memory", "io"})

    def test_unprofiled_control_is_abba(self):
        runs = schedule(unprofiled=True)
        self.assertEqual([run["revision"] for run in runs],
                         ["baseline", "candidate", "candidate", "baseline"])
        self.assertEqual(len({run["sample"] for run in runs}), 4)
        self.assertTrue(all(run["unprofiled"] and run["rate"] == 8 for run in runs))

    def test_extended_primary_and_controls_are_balanced(self):
        runs = schedule(extended=True)
        self.assertEqual(len(runs), 24)
        self.assertEqual(len({run["sample"] for run in runs}), 24)
        self.assertTrue(all(len(run["sample"]) <= 40 and run["unprofiled"] for run in runs))
        groups = collections.Counter(run["group"] for run in runs)
        self.assertEqual(groups, {"plain60": 4, "long8": 12, "mid4": 4,
                                  "noise-baseline": 2, "noise-candidate": 2})
        primary = [run for run in runs if run["group"] == "long8"]
        self.assertEqual(collections.Counter(run["revision"] for run in primary),
                         {"baseline": 6, "candidate": 6})
        starts = [run["revision"] for run in primary if run["slot"] == 1]
        self.assertEqual(collections.Counter(starts), {"baseline": 3, "candidate": 3})
        for group in ("noise-baseline", "noise-candidate"):
            noise = [run for run in runs if run["group"] == group]
            self.assertEqual(noise[0]["revision"], noise[1]["revision"])
            self.assertEqual([run["slot"] for run in noise], [1, 2])


if __name__ == "__main__":
    unittest.main()
