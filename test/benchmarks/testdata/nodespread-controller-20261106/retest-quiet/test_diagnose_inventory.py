import copy
import json
import unittest
from unittest.mock import patch

import diagnose_inventory as diagnostic


def event(resource, name, timestamp="2026-11-06T00:00:00Z", event_type="MODIFIED",
          namespace=diagnostic.NAMESPACE, pool=False, status=None):
    return {
        "timestamp": timestamp, "resource": resource, "type": event_type,
        "object": {
            "metadata": {
                "name": name, "namespace": namespace,
                "uid": "PRIVATE-UID", "annotations": {"token": "PRIVATE-TOKEN"},
                "ownerReferences": ([{
                    "kind": "SandboxWarmPool", "name": diagnostic.POOL,
                    "controller": True, "uid": "PRIVATE-POOL-UID",
                }] if pool else []),
            },
            "spec": {"nodeName": "as-nodespread-2179-worker",
                     "containers": [{"env": [{"value": "PRIVATE-PASSWORD"}]}]},
            "status": status or {},
        },
    }


def status(ready=False, ip=False):
    return {
        "conditions": [{"type": "Ready", "status": "True" if ready else "False",
                        "lastTransitionTime": "2026-11-06T00:00:00Z",
                        "message": "PRIVATE-MESSAGE"}],
        "podIPs": ["10.244.1.115"] if ip else [],
    }


class ProjectionTests(unittest.TestCase):
    def test_projection_is_allowlisted_and_does_not_mutate_input(self):
        source = event("pods", "pool-a", status={
            **status(), "podIP": "10.244.1.115", "hostIP": "172.21.0.4",
            "phase": "Pending", "containerStatuses": [{
                "name": "main", "containerID": "PRIVATE-ID",
                "imageID": "PRIVATE-IMAGE", "state": {
                    "waiting": {"reason": "ContainerCreating", "message": "PRIVATE-ERROR"}},
            }],
        })
        original = copy.deepcopy(source)
        value = diagnostic.project_watch(1, source)
        text = json.dumps(value)
        self.assertEqual(source, original)
        for secret in ("PRIVATE", "10.244.1.115", "172.21.0.4", "as-nodespread"):
            self.assertNotIn(secret, text)
        self.assertTrue(value["has_pod_ip"])
        self.assertEqual(value["node_alias"], "worker-1")
        self.assertTrue(value["containers"][0]["container_creating"])
        self.assertEqual(set(value), {
            "source_line", "observed_at", "event", "resource", "name", "created_at",
            "terminating", "has_pod_ip", "conditions", "node_alias", "phase", "containers"})

    def test_other_namespace_and_resource_are_excluded(self):
        self.assertIsNone(diagnostic.project_watch(1, event("pods", "private", namespace="default")))
        self.assertIsNone(diagnostic.project_watch(1, event("secrets", "private")))

    def test_finished_status_precedes_ready_like_the_gauge(self):
        source = event("sandboxes", "pool-a", pool=True, status=status(True, True))
        source["object"]["status"]["conditions"].append({
            "type": "Finished", "status": "True", "reason": "PodFailed"})
        self.assertEqual(diagnostic.classify(diagnostic.project_watch(1, source)), "failed")


class MetricTests(unittest.TestCase):
    def metric(self, name, value, **labels):
        return {"name": name, "value": value, "labels": {
            "namespace": diagnostic.NAMESPACE, "warmpool_name": diagnostic.POOL, **labels}}

    def test_formally_parsed_series_aggregate_and_ignore_rest(self):
        samples = [self.metric("agent_sandbox_warmpool_size", value, sandbox_status=state)
                   for state, value in zip(diagnostic.STATUSES, (2, 107, 0, 0))]
        samples += [
            self.metric("agent_sandbox_claim_creation_total", 6,
                        launch_type="cold", pod_condition="not_ready"),
            self.metric("rest_client_requests_total", 17, code="409"),
            self.metric("agent_sandbox_warmpool_size", 999, namespace="other",
                        sandbox_status="ready"),
        ]
        result = diagnostic.metric_counts(samples)
        self.assertEqual(result["inventory"]["total"], 109)
        self.assertEqual(result["launch_counts"]["cold_not_ready"], 6)
        self.assertEqual(result["launch_counts"]["warm_ready"], 0)

    def test_absent_pool_gauge_is_not_a_zero_inventory(self):
        self.assertIsNone(diagnostic.metric_counts([])["inventory"])

    def test_partial_duplicate_or_invalid_series_fail(self):
        one = self.metric("agent_sandbox_warmpool_size", 2, sandbox_status="ready")
        for samples in ([one], [one, one],
                        [self.metric("agent_sandbox_warmpool_size", -1, sandbox_status="ready")]):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                diagnostic.metric_counts(samples)


class WatchTests(unittest.TestCase):
    def records(self):
        return [
            (1, event("sandboxes", "pool-a", pool=True, status=status())),
            (2, event("pods", "pool-a", status={**status(), "phase": "Pending"})),
            (3, event("sandboxes", "claim-a", event_type="ADDED")),
            (4, event("sandboxes", "pool-a", pool=True, status=status(True, True))),
            (5, event("sandboxes", "claim-b", event_type="ADDED")),
            (6, event("sandboxes", "pool-a", pool=False, status=status(True, True))),
            (7, event("sandboxes", "claim-c", event_type="ADDED")),
        ]

    def analyze(self, records=None, names=None):
        with patch.object(diagnostic, "PODS", ()):
            return diagnostic.analyze_watch(
                records or self.records(), names or {"claim-a", "claim-b", "claim-c"})

    def test_snapshot_uses_only_preceding_status_and_excludes_adopted_member(self):
        watch = self.analyze()
        self.assertEqual([row["inventory"]["ready"] for row in watch["cold_events"]], [0, 1, 0])
        self.assertEqual([row["inventory"]["total"] for row in watch["cold_events"]], [1, 1, 0])
        self.assertEqual(watch["cold_events"][0]["members"], [{"sandbox_line": 1, "pod_line": 2}])
        document = {"watch": watch}
        self.assertEqual(diagnostic.verify_public(document)["cold_snapshots_recounted"], 3)
        self.assertNotIn("PRIVATE", json.dumps(document))

    def test_public_recount_detects_changed_totals_and_future_reference(self):
        document = {"watch": self.analyze()}
        changed = copy.deepcopy(document)
        changed["watch"]["cold_events"][0]["inventory"]["ready"] = 1
        with self.assertRaises(ValueError):
            diagnostic.verify_public(changed)
        changed = copy.deepcopy(document)
        changed["watch"]["cold_events"][0]["members"][0]["sandbox_line"] = 4
        with self.assertRaisesRegex(ValueError, "future"):
            diagnostic.verify_public(changed)

    def test_terminating_and_deleted_members_are_excluded(self):
        records = self.records()[:2]
        terminating = event("sandboxes", "pool-a", pool=True, status=status(True, True))
        terminating["object"]["metadata"]["deletionTimestamp"] = "2026-11-06T00:00:01Z"
        records += [(3, terminating), (4, event("sandboxes", "claim-a", event_type="ADDED")),
                    (5, event("sandboxes", "pool-a", event_type="DELETED", pool=True)),
                    (6, event("sandboxes", "claim-b", event_type="ADDED"))]
        watch = self.analyze(records, {"claim-a", "claim-b"})
        self.assertEqual([row["inventory"]["total"] for row in watch["cold_events"]], [0, 0])

    def test_missing_pod_is_reported_separately_from_observed_missing_ip(self):
        watch = self.analyze(
            [(1, event("sandboxes", "pool-a", pool=True, status=status())),
             (2, event("sandboxes", "claim-a", event_type="ADDED"))], {"claim-a"})
        inventory = watch["cold_events"][0]["inventory"]
        self.assertEqual(inventory["pending_pod_missing"], 1)
        self.assertEqual(inventory["pending_without_pod_ip"], 0)
        self.assertEqual(inventory["pending_without_sandbox_pod_ip"], 1)

    def test_pending_sandbox_can_have_a_running_pod_with_an_ip(self):
        watch = self.analyze([
            (1, event("sandboxes", "pool-a", pool=True, status=status())),
            (2, event("pods", "pool-a", status={**status(True, True), "phase": "Running"})),
            (3, event("sandboxes", "claim-a", event_type="ADDED")),
        ], {"claim-a"})
        snapshot = watch["cold_events"][0]
        self.assertEqual(snapshot["inventory"]["pending"], 1)
        self.assertEqual(snapshot["inventory"]["pending_without_sandbox_pod_ip"], 1)
        self.assertEqual(snapshot["inventory"]["pending_with_pod_ip"], 1)
        self.assertEqual(snapshot["inventory"]["pending_without_pod_ip"], 0)
        self.assertEqual(snapshot["pending_pod_phases"], {"Running": 1})
        self.assertEqual(watch["summary"]["events_with_pending_pod_ip"], 1)

    def test_missing_cold_event_and_pool_recreation_fail(self):
        with self.assertRaises(ValueError):
            self.analyze(names={"missing"})
        records = self.records()
        records[3][1]["object"]["metadata"]["ownerReferences"][0]["uid"] = "new-pool"
        with self.assertRaisesRegex(ValueError, "recreated"):
            self.analyze(records)


class TimingTests(unittest.TestCase):
    def test_nanoseconds_offsets_and_distinct_time_meanings(self):
        pod = event("pods", "pool-a", timestamp="2026-11-06T00:03:53.807849045+08:00",
                    status={"phase": "Running", "conditions": [{
                        "type": "PodReadyToStartContainers", "status": "True",
                        "lastTransitionTime": "2026-11-05T16:03:39Z",
                    }], "containerStatuses": [{
                        "name": "main", "state": {"running": {
                            "startedAt": "2026-11-05T16:03:38Z"}},
                    }]})
        sandbox = event("sandboxes", "pool-a", timestamp="2026-11-06T00:03:53.837937467+08:00",
                        pool=True, status=status(True, True))
        events = {1: diagnostic.project_watch(1, pod), 2: diagnostic.project_watch(2, sandbox)}
        result = diagnostic.pod_timing("pool-a", [1, 2], events)
        self.assertEqual(result["started_at_to_running_observed_ns"], 15807849045)
        self.assertEqual(result["network_transition_to_running_observed_ns"], 14807849045)
        self.assertEqual(result["pod_to_sandbox_observation_difference_ns"], 30088422)


class ControllerTests(unittest.TestCase):
    def test_scoped_error_and_gate_evidence_contains_no_log_payload(self):
        rows = [
            (1, {"namespace": diagnostic.NAMESPACE, "ts": "2026-11-06T00:00:00Z",
                 "msg": "Skipping sandbox creation: waiting for in-flight creates/deletes to be observed",
                 "reconcileID": "PRIVATE"}),
            (2, {"namespace": "other", "ts": "2026-11-06T00:00:00Z", "level": "error"}),
            (3, {"namespace": diagnostic.NAMESPACE, "ts": "2026-11-06T00:00:00Z",
                 "level": "error", "error": "PRIVATE-TOKEN"}),
        ]
        result = diagnostic.analyze_controller(rows, 0, 10**20)
        self.assertEqual(result["error_records"], 1)
        self.assertEqual(result["error_level_records"], 1)
        self.assertEqual(result["non_null_error_field_records"], 1)
        self.assertEqual(len(result["expectations_gates"]), 1)
        self.assertNotIn("PRIVATE", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
