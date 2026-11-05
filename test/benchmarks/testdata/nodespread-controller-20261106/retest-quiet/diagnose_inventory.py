#!/usr/bin/env python3
"""Reproduce the fixed p5-baseline inventory diagnostic from existing artifacts."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path

from summarize_unprofiled import read_metrics, timestamp_ns


HERE = Path(__file__).resolve().parent
SAMPLE = "quiet-plain-p5-baseline"
NAMESPACE = "np2179-" + SAMPLE
POOL = "p1-spool"
PODS = ("p1-spool-qsm76", "p1-spool-jgtc8")
RUN = HERE.parent / "runs" / SAMPLE
OUTPUT = HERE / "results-unprofiled" / "inventory-diagnostic.json"
STATUSES = ("ready", "pending", "failed", "succeeded")
CONDITIONS = {"Ready", "Finished", "PodScheduled", "PodReadyToStartContainers"}
NODE_ALIASES = {
    "as-nodespread-2179-worker": "worker-1",
    "as-nodespread-2179-worker2": "worker-2",
}


def json_lines(path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line, text in enumerate(stream, 1):
            yield line, json.loads(text)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def condition(record, kind):
    return next((item for item in record["conditions"]
                 if item["type"] == kind), None)


def ready(record):
    value = condition(record, "Ready")
    return value is not None and value["status"] == "True"


def classify(record):
    finished = condition(record, "Finished")
    if finished and finished["status"] == "True":
        if finished.get("reason") == "PodSucceeded":
            return "succeeded"
        if finished.get("reason") == "PodFailed":
            return "failed"
    return "ready" if ready(record) else "pending"


def project_watch(line, event):
    """Allowlist namespace-scoped evidence; never copy a resource object."""
    obj = event.get("object", {})
    metadata = obj.get("metadata", {})
    resource = event.get("resource")
    if metadata.get("namespace") != NAMESPACE or resource not in ("pods", "sandboxes"):
        return None
    if event["type"] not in ("ADDED", "MODIFIED", "DELETED"):
        return None
    status = obj.get("status", {})
    selected = {
        "source_line": line,
        "observed_at": event["timestamp"],
        "event": event["type"],
        "resource": resource,
        "name": metadata["name"],
        "created_at": metadata.get("creationTimestamp"),
        "terminating": bool(metadata.get("deletionTimestamp")),
        "has_pod_ip": bool(status.get("podIPs") or status.get("podIP")),
        "conditions": [],
    }
    for item in status.get("conditions", []):
        if item["type"] in CONDITIONS:
            value = {key: item[key] for key in (
                "type", "status", "lastTransitionTime") if key in item}
            if item["type"] == "Finished" and item.get("reason") in (
                    "PodSucceeded", "PodFailed"):
                value["reason"] = item["reason"]
            selected["conditions"].append(value)
    if resource == "sandboxes":
        selected["pool_member"] = any(
            ref.get("controller") and ref.get("kind") == "SandboxWarmPool"
            and ref.get("name") == POOL
            for ref in metadata.get("ownerReferences", [])
        )
        selected["pod_name"] = status.get("podName") or metadata["name"]
    else:
        node = obj.get("spec", {}).get("nodeName")
        selected["node_alias"] = NODE_ALIASES.get(
            node, "other-node" if node else "unscheduled")
        selected["phase"] = status.get("phase", "Unknown")
        selected["containers"] = []
        for item in status.get("containerStatuses", []):
            state = item.get("state", {})
            selected["containers"].append({
                "name": item["name"],
                "started_at": state.get("running", {}).get("startedAt"),
                "container_creating": (
                    state.get("waiting", {}).get("reason") == "ContainerCreating"),
            })
    return selected


def count_members(members, events):
    inventory = dict.fromkeys(STATUSES, 0)
    inventory.update(total=0, pending_without_sandbox_pod_ip=0,
                     pending_without_pod_ip=0, pending_with_pod_ip=0, pending_pod_missing=0)
    nodes = Counter()
    phases = Counter()
    seen = set()
    for member in members:
        sandbox = events[member["sandbox_line"]]
        if sandbox["name"] in seen:
            raise ValueError("Duplicate sandbox in a watch snapshot")
        seen.add(sandbox["name"])
        if (sandbox["resource"] != "sandboxes" or not sandbox["pool_member"]
                or sandbox["terminating"] or sandbox["event"] == "DELETED"):
            raise ValueError("Snapshot includes an ineligible pool member")
        inventory["total"] += 1
        state = classify(sandbox)
        inventory[state] += 1
        if state != "pending":
            continue
        inventory["pending_without_sandbox_pod_ip"] += not sandbox["has_pod_ip"]
        pod = events.get(member["pod_line"])
        if pod is None:
            inventory["pending_pod_missing"] += 1
            phases["unobserved"] += 1
            nodes["unobserved"] += 1
            continue
        if pod["resource"] != "pods" or pod["name"] != sandbox["pod_name"]:
            raise ValueError("Mismatched Pod in a watch snapshot")
        inventory["pending_without_pod_ip"] += not pod["has_pod_ip"]
        inventory["pending_with_pod_ip"] += pod["has_pod_ip"]
        phases[pod["phase"]] += 1
        nodes[pod["node_alias"]] += 1
    return {
        "inventory": inventory,
        "pending_by_node_alias": dict(sorted(nodes.items())),
        "pending_pod_phases": dict(sorted(phases.items())),
    }


def pod_timing(name, lines, events):
    records = [events[line] for line in lines]
    running = next(item for item in records if item["resource"] == "pods"
                   and item["phase"] == "Running")
    sandbox = next(item for item in records if item["resource"] == "sandboxes"
                   and ready(item))
    started = [item["started_at"] for item in running["containers"]
               if item["started_at"] is not None]
    if len(started) != 1:
        raise ValueError("Expected one running container in the fixed sample")
    network = condition(running, "PodReadyToStartContainers")
    if network is None or network["status"] != "True":
        raise ValueError("Missing network-ready condition on first observed Running Pod")
    return {
        "name": name,
        "event_lines": lines,
        "first_running_pod_line": running["source_line"],
        "first_ready_sandbox_line": sandbox["source_line"],
        "container_started_at": started[0],
        "network_condition_transition_at": network["lastTransitionTime"],
        "pod_running_observed_at": running["observed_at"],
        "sandbox_ready_observed_at": sandbox["observed_at"],
        "started_at_to_running_observed_ns": (
            timestamp_ns(running["observed_at"]) - timestamp_ns(started[0])),
        "network_transition_to_running_observed_ns": (
            timestamp_ns(running["observed_at"])
            - timestamp_ns(network["lastTransitionTime"])),
        "pod_to_sandbox_observation_difference_ns": (
            timestamp_ns(sandbox["observed_at"]) - timestamp_ns(running["observed_at"])),
    }


def watch_summary(cold_events):
    fields = ("ready", "pending", "total", "pending_without_sandbox_pod_ip",
              "pending_without_pod_ip")
    return {
        "cold_events": len(cold_events),
        "ready_zero_events": sum(item["inventory"]["ready"] == 0 for item in cold_events),
        "events_with_pending_pod_missing": sum(
            item["inventory"]["pending_pod_missing"] > 0 for item in cold_events),
        "events_with_pending_pod_ip": sum(
            item["inventory"]["pending_with_pod_ip"] > 0 for item in cold_events),
        "events_with_pending_sandbox_running_pod": sum(
            item["pending_pod_phases"].get("Running", 0) > 0 for item in cold_events),
        "ranges": {field: [
            min(item["inventory"][field] for item in cold_events),
            max(item["inventory"][field] for item in cold_events),
        ] for field in fields},
    }


def analyze_watch(records, cold_names):
    sandboxes, pods, selected = {}, {}, {}
    cold_events = []
    timelines = {name: [] for name in PODS}
    warnings = 0
    owner_uids = set()
    for line, raw in records:
        obj = raw.get("object", {})
        meta = obj.get("metadata", {})
        if meta.get("namespace") != NAMESPACE:
            continue
        if raw.get("resource") == "events" and obj.get("type") == "Warning":
            warnings += 1
        item = project_watch(line, raw)
        if item is None:
            continue
        name = item["name"]
        if item["resource"] == "sandboxes" and item["pool_member"]:
            owner_uids.update(ref["uid"] for ref in meta.get("ownerReferences", [])
                              if ref.get("controller") and ref.get("kind") == "SandboxWarmPool"
                              and ref.get("name") == POOL and ref.get("uid"))
            if len(owner_uids) > 1:
                raise ValueError("Pool was recreated; fixed-sample reconstruction is ambiguous")
        target = sandboxes if item["resource"] == "sandboxes" else pods
        if item["event"] == "DELETED":
            target.pop(name, None)
        else:
            target[name] = item
        if name in timelines:
            selected[line] = item
            timelines[name].append(line)
        if item["resource"] != "sandboxes" or item["event"] != "ADDED" or name not in cold_names:
            continue
        selected[line] = item
        members = []
        for sandbox in sorted(sandboxes.values(), key=lambda record: record["name"]):
            if not sandbox["pool_member"] or sandbox["terminating"]:
                continue
            pod = pods.get(sandbox["pod_name"])
            selected[sandbox["source_line"]] = sandbox
            if pod is not None:
                selected[pod["source_line"]] = pod
            members.append({
                "sandbox_line": sandbox["source_line"],
                "pod_line": pod["source_line"] if pod is not None else None,
            })
        cold_events.append({
            "name": name, "observed_at": item["observed_at"],
            "source_line": line, "members": members,
            **count_members(members, selected),
        })
    observed = [item["name"] for item in cold_events]
    if len(observed) != len(set(observed)) or set(observed) != set(cold_names):
        raise ValueError("Cold workload identities and watch ADDED events disagree")
    return {
        "snapshot_basis": "Latest preceding records in watch JSONL file order; separate from controller cache",
        "source_line_basis": "One-based line in decompressed workload/watch.jsonl.gz",
        "warning_event_records_in_namespace": warnings,
        "summary": watch_summary(cold_events),
        "cold_events": cold_events,
        "pod_timings": [pod_timing(name, lines, selected) for name, lines in timelines.items()],
        "selected_events": [selected[line] for line in sorted(selected)],
    }


def metric_counts(samples):
    inventory, launches, seen = {}, Counter(), set()
    for sample in samples:
        name, labels, value = sample["name"], sample["labels"], sample["value"]
        if name not in ("agent_sandbox_warmpool_size", "agent_sandbox_claim_creation_total"):
            continue
        if labels.get("namespace") != NAMESPACE or labels.get("warmpool_name") != POOL:
            continue
        identity = (name, tuple(sorted(labels.items())))
        if identity in seen:
            raise ValueError("Duplicate Prometheus series")
        seen.add(identity)
        if not isinstance(value, (int, float)) or value < 0 or int(value) != value:
            raise ValueError("Invalid inventory or launch counter value")
        value = int(value)
        if name == "agent_sandbox_warmpool_size":
            state = labels["sandbox_status"]
            if state not in STATUSES:
                raise ValueError("Unknown inventory status")
            inventory[state] = inventory.get(state, 0) + value
        else:
            launch, state = labels["launch_type"], labels["pod_condition"]
            if launch not in ("cold", "warm") or state not in ("ready", "not_ready"):
                raise ValueError("Unknown claim counter label")
            launches[launch + "_" + state] += value
    if inventory and set(inventory) != set(STATUSES):
        raise ValueError("Incomplete inventory gauge")
    return {
        "inventory": {**inventory, "total": sum(inventory.values())} if inventory else None,
        "launch_counts": {key: launches[key] for key in (
            "warm_ready", "warm_not_ready", "cold_ready", "cold_not_ready")},
    }


def analyze_metrics(run, telemetry, binary):
    paths = [run / "telemetry" / sample["artifact"] for sample in telemetry["samples"]]
    paths.append(run / "metrics-final.prom")
    reports = read_metrics(paths, binary=binary)
    timeline = []
    previous = dict.fromkeys(("warm_ready", "warm_not_ready", "cold_ready", "cold_not_ready"), 0)
    for sample, path in zip(telemetry["samples"], paths):
        if sample["status"] != "complete":
            raise ValueError("Incomplete metrics scrape")
        counts = metric_counts(reports[str(path.resolve())])
        if counts["inventory"] is None:
            raise ValueError("Missing inventory in traffic-window scrape")
        if any(counts["launch_counts"][key] < previous[key] for key in previous):
            raise ValueError("Launch counter reset during traffic")
        previous = counts["launch_counts"]
        timeline.append({
            "artifact": str(path.relative_to(run)),
            "published_artifact": "runs/" + SAMPLE + "/" + str(path.relative_to(run)) + ".gz",
            "raw_sha256": sha256(path),
            "tag": sample["tag"],
            "scrape_started_at": sample["started_at"]["utc"],
            "scrape_ended_at": sample["ended_at"]["utc"],
            **counts,
        })
    final = metric_counts(reports[str(paths[-1].resolve())])
    final["raw_sha256"] = sha256(paths[-1])
    final["published_artifact"] = "runs/" + SAMPLE + "/metrics-final.prom.gz"
    final["additional_launches_after_last_scrape"] = {
        key: final["launch_counts"][key] - previous[key] for key in previous}
    if any(value < 0 for value in final["additional_launches_after_last_scrape"].values()):
        raise ValueError("Final launch counter reset")
    return {"timeline": timeline, "final": final}


def analyze_controller(records, start, end):
    counts, refill = Counter(), {}
    gates, cold, zero_ready = [], [], []
    errors = error_levels = error_fields = 0
    inactive = []
    categories = {
        "Successfully adopted sandbox from warm pool": "warm_adoption",
        "Created sandbox for claim": "cold_creation",
        "Created new pool sandbox": "refill_creation",
        "Skipping sandbox creation: waiting for in-flight creates/deletes to be observed": "expectations_gate",
    }
    for line, entry in records:
        if entry.get("namespace") != NAMESPACE:
            continue
        error_levels += entry.get("level") == "error"
        error_fields += entry.get("error") is not None
        errors += entry.get("level") == "error" or entry.get("error") is not None
        category = categories.get(entry.get("msg"))
        ts = entry["ts"]
        point = {"source_line": line, "observed_at": ts}
        if category:
            counts[category] += 1
        if category == "expectations_gate":
            gates.append(point)
        elif category == "cold_creation":
            cold.append({**point, "name": entry["claim"]})
        elif category == "refill_creation":
            row = refill.setdefault(ts, {**point, "count": 0})
            row["count"] += 1
        if start <= timestamp_ns(ts) <= end:
            if entry.get("msg") == "Updated SandboxWarmPool status" and entry["readyReplicas"] == 0:
                zero_ready.append({**point, "ready": 0, "replicas": entry["replicas"]})
            if entry.get("msg") == "Pool status":
                inactive.append(entry["inactive"])
    return {
        "scope": "Only records with the exact test namespace",
        "error_records": errors,
        "error_level_records": error_levels,
        "non_null_error_field_records": error_fields,
        "event_counts": dict(sorted(counts.items())),
        "expectations_gates": gates,
        "cold_creations": cold,
        "refill_by_logged_second": [refill[key] for key in sorted(refill)],
        "zero_ready_status_during_arrivals": zero_ready,
        "inactive_replica_range_during_arrivals": [min(inactive), max(inactive)] if inactive else None,
    }


def verify_public(document):
    """Recount all cold snapshots and both timing rows using only public fields."""
    watch = document["watch"]
    events = {item["source_line"]: item for item in watch["selected_events"]}
    if len(events) != len(watch["selected_events"]):
        raise ValueError("Duplicate public watch source line")
    for snapshot in watch["cold_events"]:
        cold = events[snapshot["source_line"]]
        if (cold["name"] != snapshot["name"] or cold["event"] != "ADDED"
                or cold["resource"] != "sandboxes" or cold["pool_member"]
                or cold["observed_at"] != snapshot["observed_at"]):
            raise ValueError("Invalid cold event reference")
        for member in snapshot["members"]:
            if any(line is not None and line >= snapshot["source_line"]
                   for line in member.values()):
                raise ValueError("Snapshot references a future watch event")
        recounted = count_members(snapshot["members"], events)
        if any(snapshot[key] != value for key, value in recounted.items()):
            raise ValueError("Public watch snapshot recount failed")
    if watch_summary(watch["cold_events"]) != watch["summary"]:
        raise ValueError("Public watch summary recount failed")
    for row in watch["pod_timings"]:
        if pod_timing(row["name"], row["event_lines"], events) != row:
            raise ValueError("Public Pod timing recomputation failed")
    return {
        "cold_snapshots_recounted": len(watch["cold_events"]),
        "pod_timing_rows_recomputed": len(watch["pod_timings"]),
    }


def build_diagnostic(run=RUN, binary=HERE / "analyze_metrics"):
    if run.name != SAMPLE:
        raise ValueError("This diagnostic is restricted to " + SAMPLE)
    telemetry = json.loads((run / "telemetry" / "run.json").read_text())
    if telemetry["status"] != "complete" or telemetry["errors"]:
        raise ValueError("Incomplete telemetry")
    claims = [record for _, record in json_lines(run / "workload" / "sandboxes.jsonl")]
    if any(record["namespace"] != NAMESPACE for record in claims):
        raise ValueError("Unexpected workload namespace")
    cold = [{
        "name": record["name"],
        "create_called_at": record["createCalled"],
        "create_returned_at": record["createReturned"],
        "sandbox_ready_observed_at": record["sandboxReady"],
    } for record in claims if record["boundSandbox"] == record["name"]]
    if len(claims) != len({record["name"] for record in claims}):
        raise ValueError("Duplicate workload claim")
    start, end = telemetry["trigger"]["at"]["utc"], telemetry["boundary"]["at"]["utc"]
    document = {
        "schema_version": 1,
        "sample": SAMPLE,
        "namespace": NAMESPACE,
        "pool_name": POOL,
        "scope": "Post-hoc descriptive diagnosis of one fixed run; no causal branch attribution",
        "measurement_window": {
            "arrivals_trigger_observed_at": start,
            "arrivals_boundary_observed_at": end,
        },
        "provenance": {
            name: {"sha256": sha256(run / name), "local_only": local_only}
            for name, local_only in (
                ("controller.log", True), ("workload/watch.jsonl.gz", True),
                ("workload/sandboxes.jsonl", False), ("telemetry/run.json", False))
        },
        "metrics_parser": {
            "name": "analyze_metrics",
            "binary_sha256": sha256(binary),
            "interface": "Official Prometheus parser stdout decoded as JSON",
        },
        "metrics": analyze_metrics(run, telemetry, binary),
        "workload": {"claims": len(claims), "same_name_cold_claims": cold},
        "controller": analyze_controller(
            json_lines(run / "controller.log"), timestamp_ns(start), timestamp_ns(end)),
        "watch": analyze_watch(
            json_lines(run / "workload" / "watch.jsonl.gz"), {row["name"] for row in cold}),
        "rest_api_errors": {
            "status": "separate_report",
            "report": "rest-client-requests.json",
            "note": "analyze_rest.py reports arrival-window and process-lifetime counters separately. Error-level logs and internally handled HTTP conflicts/retries describe different observables; final claim outcomes are recorded in the workload.",
        },
        "interpretation": [
            "Inventory is visible Sandbox status, not the instantaneous queue or physical container state.",
            "Watch snapshots use preceding file records and are a separate observer from the controller cache.",
            "Timestamp differences compare embedded object timestamps with local watch observation timestamps; they do not isolate a lifecycle stage.",
            "Individual cold fallbacks cannot be assigned to queue-empty versus PodIPs-grace expiry from these records.",
            "Retain the run in the registered full comparison; healthy-inventory and depletion views are descriptive diagnostics.",
        ],
    }
    final = document["metrics"]["final"]["launch_counts"]
    controller = document["controller"]
    if (final["cold_not_ready"] != len(cold) or final["warm_ready"] != len(claims) - len(cold)
            or final["cold_ready"] or final["warm_not_ready"]
            or controller["event_counts"].get("warm_adoption") != final["warm_ready"]
            or controller["event_counts"].get("cold_creation") != len(cold)
            or {row["name"] for row in controller["cold_creations"]} != {row["name"] for row in cold}):
        raise ValueError("Workload, controller log, and launch counters disagree")
    document["verification"] = verify_public(document)
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=RUN)
    parser.add_argument("--metrics-binary", type=Path, default=HERE / "analyze_metrics")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--verify-public", type=Path,
                        help="Recount a public diagnostic JSON without local logs or a metrics binary")
    args = parser.parse_args()
    if args.verify_public:
        result = verify_public(json.loads(args.verify_public.read_text()))
    else:
        document = build_diagnostic(args.run_dir.resolve(), args.metrics_binary.resolve())
        args.output.write_text(json.dumps(document, indent=2, allow_nan=False) + "\n")
        result = {"output": str(args.output), **document["verification"]}
    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
