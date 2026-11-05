#!/usr/bin/env python3
"""Run one serial, unprofiled Linux calibration and ABBA comparison on existing kind."""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import threading
import time
import urllib.request

import repeat_suite
import run_unprofiled as workload
from summarize import verified_latency as rawstats
from summarize_unprofiled import read_metrics, timestamp_ns
from validate_pacing import audit as pacing_audit


BASELINE_COMMIT = "4d39311ba9d89001d79ff1217ed330c7ea3761ec"
CANDIDATE_COMMIT = "a87fa000ec96c8e3d9be493da5c6e6e2048b37ad"
FLAGS = [
    "--leader-elect=false", "--extensions",
    "--metrics-bind-address=127.0.0.1:18080",
    "--health-probe-bind-address=127.0.0.1:18081",
    "--metrics-secure-serving=false", "--pprof-mutex-profile-fraction=0",
    "--pprof-block-profile-rate=0", "--sandbox-concurrent-workers=100",
    "--sandbox-claim-concurrent-workers=50", "--sandbox-warm-pool-concurrent-workers=1",
    "--sandbox-template-concurrent-workers=1", "--sandbox-warm-pool-max-batch-size=32",
    "--sandbox-warm-pool-max-refill-rate=16",
]
PROTOCOL = {
    "platform": "native Linux; existing kind with one control plane and two workers",
    "execution": "one job, serial measurements, fresh controller and namespace per run",
    "pool": 128, "initial_ready_required": 128, "seed": [0, 0],
    "manager_gomaxprocs": 4, "profile_enabled": False,
    "metrics_interval_seconds": 5, "linux_interval_seconds": 5,
    "calibration": {"revision": "baseline", "rates": [2, 4, 8], "seconds": 30,
                    "ready_sampled_min_at_least": 64, "cold": 0,
                    "lifecycle_errors": 0, "linux_cpu_active_median_at_most": 85},
    "selection": "Stop if 2/s fails; otherwise highest passing rate after all three calibrations",
    "operational_failure": "Abort and retain evidence, including failed calibration",
    "formal": {"order": ["baseline", "candidate", "candidate", "baseline"],
               "pairs": [1, 1, 2, 2], "seconds": 60, "inclusion": "All four runs, including cold launches and low inventory"},
    "comparison": "Two AB/BA run pairs; descriptive p50, p99, CPU and allocation only",
    "primary_comparison": "Geometric mean of the two paired candidate/baseline p50 ratios",
    "uncertainty": "No bootstrap interval or significance test",
    "counter_window": "Arrival-trigger start scrape to arrivals-finished end scrape; excludes subsequent drain",
    "latency_window": "All actual claims through final Ready, including drain; exported Go durations",
    "inventory": "Sampled Ready is controller-visible state, not continuous queue availability",
}


class RoundError(ValueError):
    """Diagnostic containing only controlled, publishable text."""


def require(condition, message):
    if not condition:
        raise RoundError(message)


def save(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def read(path):
    return json.loads(path.read_text())


def jsonlines(path):
    with path.open() as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_error(error):
    return str(error) if isinstance(error, RoundError) else type(error).__name__


def kube(env, *args):
    # Credentials travel only through the child environment, never artifact argv.
    return subprocess.run(["kubectl", *args], env=env, capture_output=True,
                          text=True, check=True, timeout=20).stdout


def cluster_check(env):
    context = kube(env, "config", "current-context").strip()
    require(context.startswith("kind-"), "Expected an existing kind context")
    nodes = json.loads(kube(env, "get", "nodes", "-o", "json"))["items"]
    observed = []
    for node in nodes:
        labels = node["metadata"].get("labels", {})
        control = any(key in labels for key in
                      ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
        ready = any(c["type"] == "Ready" and c["status"] == "True"
                    for c in node.get("status", {}).get("conditions", []))
        observed.append({"name": node["metadata"]["name"],
                         "role": "control-plane" if control else "worker", "ready": ready})
    require(len(observed) == 3 and sum(n["role"] == "control-plane" for n in observed) == 1
            and all(n["ready"] for n in observed), "Expected one Ready control plane and two Ready workers")
    return observed


def ports_available():
    for port in (18080, 18081):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", port))


def http_bytes(url, timeout=5):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return response.read()


def wait_manager(manager, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        require(manager.poll() is None, "Controller exited before readiness")
        try:
            http_bytes("http://127.0.0.1:18081/readyz", timeout=1)
            return
        except OSError:
            time.sleep(0.5)
    raise RoundError("Controller readiness timed out")


def stop_manager(manager):
    if manager.poll() is None:
        os.killpg(manager.pid, signal.SIGTERM)
        try:
            manager.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(manager.pid, signal.SIGKILL)
            manager.wait(timeout=10)


def monitor_runner(stop, path, errors):
    try:
        repeat_suite.linux_monitor(stop, path)
    except BaseException as error:
        errors.append(safe_error(error))


def host_window(path, start, end):
    all_records = jsonlines(path)
    records = [r for r in all_records if start <= timestamp_ns(r["utc"]) <= end]
    require(len(records) >= 3, "Insufficient Linux samples in arrival window")
    times = [timestamp_ns(r["utc"]) for r in records]
    require(times == sorted(set(times)), "Unordered Linux sample times")
    gaps = [(b - a) / 1e9 for a, b in zip([start, *times], [*times, end])]
    require(max(gaps) <= 15, "Linux telemetry gap exceeds 15 seconds")
    values = [r.get("cpu_active_percent") for r in records]
    require(not any(r.get("error") or r.get("error_present") for r in records)
            and all(type(v) in (float, int) and math.isfinite(v) and 0 <= v <= 100 for v in values),
            "Invalid Linux CPU telemetry")
    return {"samples": len(records), "cpu_active_median_percent": statistics.median(values),
            "cpu_active_max_percent": max(values), "max_gap_seconds": max(gaps),
            "first_sample_utc": records[0]["utc"], "last_sample_utc": records[-1]["utc"]}


def metric(samples, name, **labels):
    matched = [s["value"] for s in samples if s["name"] == name
               and all(s["labels"].get(k) == v for k, v in labels.items())]
    require(bool(matched), "Required metric series missing: " + name)
    require(all(math.isfinite(v) and v >= 0 for v in matched), "Invalid metric value: " + name)
    return sum(matched)


def summarize_run(run, entry, metrics_parser, host_path, emitter=None):
    phases = read(run / "workload/summary.json")["phases"]
    require(len(phases) == 1 and phases[0]["name"] == "claims-warm-sustained", "Unexpected workload phases")
    records = jsonlines(run / "workload/sandboxes.jsonl")
    require(all(r.get("phase") == "claims-warm-sustained" for r in records), "Unexpected lifecycle phase")
    latency = rawstats(records, phases[0], entry["sample"])
    namespace = "np2179-" + entry["sample"]
    ids = []
    for record in records:
        match = re.fullmatch(r"p1-sclaim-(\d+)", record["name"])
        require(match is not None and record.get("namespace") == namespace, "Invalid claim identity")
        ids.append(int(match[1]))
        called, returned, ready, deleted, gone = [
            timestamp_ns(record[k]) for k in ("createCalled", "createReturned", "sandboxReady",
                                               "deleteCalled", "sandboxDeleted")]
        require(called <= returned and called <= ready <= deleted <= gone
                and bool(record.get("boundSandbox")), "Invalid lifecycle ordering or binding")
    require(sorted(ids) == list(range(len(records))), "Missing or duplicate arrival IDs")
    pacing = {"status": "contiguous_ids_verified", "actual_arrivals": len(records),
              "schedule_verification": "Emitter not supplied; exact seeded count and offsets unverified"}
    if emitter:
        schedule = json.loads(subprocess.check_output(
            [str(emitter), "-rate", str(entry["rate"]), "-seconds", str(entry["seconds"])],
            text=True, timeout=20))
        save(run / "schedule.json", schedule)
        pacing = {"status": "seeded_schedule_verified", **pacing_audit(records, schedule["offsets_ns"])}
    save(run / "arrival-audit.json", pacing)
    telemetry = read(run / "telemetry/run.json")
    require(telemetry["status"] == "complete" and telemetry["exit_code"] == 0
            and not telemetry["errors"], "Incomplete telemetry")
    require(telemetry["trigger"]["phase"] == "claims-warm-sustained"
            and telemetry["boundary"]["kind"] == "arrivals_finished", "Invalid arrival boundaries")
    start, end = (timestamp_ns(telemetry[key]["at"]["utc"]) for key in ("trigger", "boundary"))
    require(end > start, "Reversed arrival window")
    # The final lifecycle goroutine may call Create after the scheduler logs its boundary.
    arrivals = [timestamp_ns(r["createCalled"]) for r in records]
    samples = telemetry["samples"]
    require(len(samples) >= 3 and samples[0]["tag"] == "start" and samples[-1]["tag"] == "end"
            and all(s["tag"] == "periodic" for s in samples[1:-1]), "Invalid telemetry sample sequence")
    paths, midpoints = [], []
    previous_end = -math.inf
    for i, sample in enumerate(samples):
        require(sample["status"] == "complete" and sample["artifact"] == f"metrics-{i:04d}.prom",
                "Failed or unordered telemetry artifact")
        a, b = (sample[k]["monotonic_seconds"] for k in ("started_at", "ended_at"))
        require(all(type(v) in (int, float) and math.isfinite(v) for v in (a, b))
                and previous_end <= a <= b and b - a <= 5, "Invalid scrape interval")
        previous_end = b
        midpoints.append((a + b) / 2)
        paths.append(run / "telemetry" / sample["artifact"])
    require(max(b - a for a, b in zip(midpoints, midpoints[1:])) <= 10, "Telemetry gap exceeds 10 seconds")
    offsets = [midpoints[0] - telemetry["trigger"]["at"]["monotonic_seconds"],
               midpoints[-1] - telemetry["boundary"]["at"]["monotonic_seconds"]]
    require(all(0 <= v <= 5 for v in offsets), "Boundary scrape delay exceeds 5 seconds")
    final_path = run / "metrics-final.prom"
    parsed = read_metrics([*paths, final_path], binary=metrics_parser)
    snapshots = [parsed[str(p.resolve())] for p in paths]
    labels = {"namespace": namespace, "warmpool_name": "p1-spool"}
    ready_inventory = [metric(x, "agent_sandbox_warmpool_size", sandbox_status="ready", **labels)
                       for x in snapshots]
    require(all(v == int(v) for v in ready_inventory), "Invalid Ready inventory")
    require(ready_inventory[0] == 128, "Start Ready inventory must equal 128")
    launches = {"warm/ready": 0, "warm/not_ready": 0, "cold/not_ready": 0}
    for sample in parsed[str(final_path.resolve())]:
        if sample["name"] != "agent_sandbox_claim_creation_total":
            continue
        tags = sample["labels"]
        if not all(tags.get(k) == v for k, v in labels.items()):
            continue
        key = tags.get("launch_type", "") + "/" + tags.get("pod_condition", "")
        value = sample["value"]
        require(key in launches and value >= 0 and value == int(value), "Invalid launch counter")
        launches[key] += value
    require(sum(launches.values()) == len(records), "Launch counters differ from actual arrivals")
    deltas = {}
    for field, name in (("controller_cpu_seconds", "process_cpu_seconds_total"),
                        ("controller_alloc_bytes", "go_memstats_alloc_bytes_total")):
        values = [metric(x, name) for x in snapshots]
        require(all(b >= a for a, b in zip(values, values[1:])), "Controller counter reset")
        deltas[field] = values[-1] - values[0]
    host = host_window(host_path, start, end)
    return {
        **entry, "status": "complete", "records": len(records), "created": len(records),
        "ready": len(records), "failed": 0, "complete_lifecycles": len(records),
        "p50_ms": latency["endToEndReady"]["p50Ms"], "p99_ms": latency["endToEndReady"]["p99Ms"],
        "latency": latency, **deltas, "counter_window_seconds": midpoints[-1] - midpoints[0],
        "counter_boundary_offsets_seconds": offsets,
        "traffic_utc": [telemetry["trigger"]["at"]["utc"], telemetry["boundary"]["at"]["utc"]],
        "creates_before_observed_trigger": sum(at < start for at in arrivals),
        "creates_after_observed_boundary": sum(at > end for at in arrivals),
        "first_create_offset_from_trigger_ms": (min(arrivals) - start) / 1e6,
        "last_create_offset_from_boundary_ms": (max(arrivals) - end) / 1e6,
        "ready_after_traffic_count": sum(timestamp_ns(r["sandboxReady"]) > end for r in records),
        "ready_pool_start": ready_inventory[0], "ready_pool_sampled_min": min(ready_inventory),
        "ready_pool_zero_samples": ready_inventory.count(0),
        "ready_pool_samples": [{"artifact": s["artifact"], "started_at": s["started_at"],
                                "ended_at": s["ended_at"], "ready": v}
                               for s, v in zip(samples, ready_inventory)],
        "launch_conditions": launches, "cold": launches["cold/not_ready"],
        "warm": launches["warm/ready"] + launches["warm/not_ready"], "linux": host, "pacing": pacing,
    }


def calibration_check(row):
    checks = {"ready_min_at_least_64": row["ready_pool_sampled_min"] >= 64,
              "cold_zero": row["cold"] == 0,
              "complete_lifecycles": row["records"] == row["complete_lifecycles"] and row["failed"] == 0,
              "linux_cpu_median_at_most_85": row["linux"]["cpu_active_median_percent"] <= 85}
    return {"passed": all(checks.values()), "checks": checks}


def formal_plan(rate):
    return [{"sample": f"ci-formal-{i}-{revision}", "kind": "formal", "revision": revision,
             "rate": rate, "seconds": 60, "pair": 1 if i <= 2 else 2, "slot": i}
            for i, revision in enumerate(PROTOCOL["formal"]["order"], 1)]


def paired_summary(rows):
    formal = [r for r in rows if r["kind"] == "formal"]
    if len(formal) != 4 or any(r["status"] != "complete" for r in formal):
        return []
    result = []
    for pair in (1, 2):
        subset = [r for r in formal if r["pair"] == pair]
        baseline = next(r for r in subset if r["revision"] == "baseline")
        candidate = next(r for r in subset if r["revision"] == "candidate")
        require(baseline["records"] == candidate["records"], "Paired actual arrival counts differ")
        values = {}
        for key in ("p50_ms", "p99_ms", "controller_cpu_seconds", "controller_alloc_bytes"):
            a, b = baseline[key], candidate[key]
            values[key] = {"baseline": a, "candidate": b, "difference": b - a,
                           "percent_change": 100 * (b / a - 1) if a > 0 else None}
        result.append({"pair": pair, "order": "AB" if pair == 1 else "BA", "measurements": values})
    return result


def primary_comparison(pairs):
    if len(pairs) != 2:
        return None
    values = [pair["measurements"]["p50_ms"] for pair in pairs]
    result = {"n_pairs": 2, "endpoint": "run-level p50_ms",
              "effect": "geometric mean candidate/baseline ratio",
              "inference": "Descriptive only; no confidence interval or significance test"}
    if any(value[role] <= 0 for value in values for role in ("baseline", "candidate")):
        return {**result, "status": "undefined_nonpositive_p50", "ratio": None, "percent_change": None}
    log_ratio = statistics.mean(math.log(value["candidate"]) - math.log(value["baseline"])
                                for value in values)
    return {**result, "status": "complete", "ratio": math.exp(log_ratio),
            "percent_change": 100 * math.expm1(log_ratio)}


def progress(event, row):
    print(json.dumps({
        "event": event, "utc": repeat_suite.utc(), "sample": row["sample"],
        "status": row.get("status", "running"), "rate": row["rate"],
        "count": row.get("records"), "p50_ms": row.get("p50_ms"),
        "calibration_passed": row.get("calibration", {}).get("passed"),
        "errors": row.get("errors", []),
    }, allow_nan=False), flush=True)


def run_one(entry, args, registration, envs):
    run = args.output / "runs" / entry["sample"]
    run.mkdir()
    manager_env, stress_env = envs
    namespace = "np2179-" + entry["sample"]
    manager = None
    owned = False
    row = {**entry, "status": "failed", "errors": []}
    config = {**entry, "pool": 128, "seed": [0, 0], "profile_enabled": False,
              "gomaxprocs": 4, "stress_gomaxprocs": stress_env.get("GOMAXPROCS"),
              "controller_args": FLAGS, "controller_cpus": registration["controller_cpus"],
              "commit": registration["source_commits"][entry["revision"]],
              "binary_sha256": registration["binary_sha256"], "metrics_interval_seconds": 5}
    save(run / "config.json", config)
    binary = getattr(args, entry["revision"])
    try:
        for role, expected in registration["binary_sha256"].items():
            require(sha256(getattr(args, role)) == expected, "Registered executable changed: " + role)
        require(not kube(stress_env, "get", "namespace", namespace, "--ignore-not-found", "-o", "name").strip(),
                "Run namespace already exists; existing resources preserved")
        ports_available()
        owned = True
        command = workload.workload_args(args.stress, run, entry["sample"], entry["rate"], entry["seconds"], 128)
        save(run / "command.json", ["stress", *[x.replace(str(run), ".") for x in command[1:]]])
        with (run / "controller.log").open("x") as log:
            try:
                manager = subprocess.Popen(
                    ["taskset", "-c", ",".join(map(str, registration["controller_cpus"])), str(binary), *FLAGS],
                    cwd=args.output, env=manager_env, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                wait_manager(manager)
                workload.observe_workload(command, stress_env, run, timeout=500)
                require(manager.poll() is None, "Controller exited during workload")
                (run / "metrics-final.prom").write_bytes(http_bytes("http://127.0.0.1:18080/metrics"))
                row = summarize_run(run, entry, args.metrics_parser, args.output / "linux-load.jsonl",
                                    args.schedule_emitter)
            finally:
                if owned:
                    try:
                        kube(stress_env, "delete", "namespace", namespace, "--ignore-not-found=true", "--wait=false")
                        workload.wait_namespace_deleted(namespace, stress_env, timeout=120)
                        row["cleanup"] = "complete"
                    except BaseException as error:
                        row["cleanup"] = "failed"
                        row.setdefault("errors", []).append("Cleanup:" + safe_error(error))
                    finally:
                        if manager is not None:
                            stop_manager(manager)
    except BaseException as error:
        row["status"] = "failed"
        row.setdefault("errors", []).append(safe_error(error))
    if row.get("cleanup") != "complete":
        row["status"] = "failed"
    save(run / "summary.json", row)
    return row


def write_outputs(output, manifest):
    report = {"status": manifest["status"], "selected_rate": manifest.get("selected_rate"),
              "protocol": PROTOCOL, "runs": manifest["runs"], "errors": manifest.get("errors", []),
              "pairs": paired_summary(manifest["runs"]) if manifest["status"] == "complete" else []}
    report["primary_comparison"] = primary_comparison(report["pairs"])
    save(output / "manifest.json", manifest)
    save(output / "summary.json", report)
    text = ["# Linux CI Round", "", f"Status: {report['status']}",
            f"Selected rate: {report['selected_rate']}", "",
            "Single-job descriptive AB/BA comparison. All calibration and formal runs are retained.",
            "Pool replicas: 128. Profiling disabled. Linux telemetry interval: 5 seconds.",
            "Calibration: baseline 2/4/8 claims/s for 30 seconds; Ready minimum >=64,",
            "zero cold launches, complete error-free lifecycles, Linux CPU median <=85%.",
            "Lowest-rate failure stops the round. Highest passing rate selects four 60-second ABBA runs.",
            "Latency includes drain; CPU/allocation counters cover the arrival window.",
            "Sampled Ready does not resolve continuous queue availability.",
            "No confidence intervals or significance tests are calculated.", "",
            "| Run | Status | Rate | Claims | Cold | Ready Min | CPU Median % | p50 ms | p99 ms |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in report["runs"]:
        text.append("| " + " | ".join(str(x) for x in (
            row["sample"], row["status"], row["rate"], row.get("records", ""),
            row.get("cold", ""), row.get("ready_pool_sampled_min", ""),
            row.get("linux", {}).get("cpu_active_median_percent", ""),
            row.get("p50_ms", ""), row.get("p99_ms", ""))) + " |")
    for pair in report["pairs"]:
        text.extend(["", f"## Pair {pair['pair']} ({pair['order']})", "",
                     "| Measure | Baseline | Candidate | Difference | Change % |",
                     "|---|---:|---:|---:|---:|"])
        for name, values in pair["measurements"].items():
            text.append("| " + name + " | " + " | ".join(str(values[k]) for k in
                        ("baseline", "candidate", "difference", "percent_change")) + " |")
    if report["primary_comparison"]:
        primary = report["primary_comparison"]
        text.extend(["", "## Primary Comparison", "",
                     f"Two-pair p50 geometric mean ratio: {primary['ratio']}.",
                     f"Percent change: {primary['percent_change']}%.",
                     primary["inference"] + "."])
    if report["errors"]:
        text.extend(["", "Errors: " + "; ".join(report["errors"])])
    (output / "REPORT.md").write_text("\n".join(text) + "\n")


def execute(args, registration, runner=run_one):
    parent = dict(os.environ)
    parent.pop("GOMAXPROCS", None)
    manager_env, stress_env = workload.environments(parent, args.kubeconfig)
    manifest = {"status": "running", "started_at": repeat_suite.utc(), "protocol": PROTOCOL,
                "registration": registration, "runs": [], "errors": [], "selected_rate": None}
    stop = threading.Event()
    monitor_errors = []
    monitor = threading.Thread(target=monitor_runner,
                               args=(stop, args.output / "linux-load.jsonl", monitor_errors), daemon=True)
    old_root = workload.ROOT
    workload.ROOT = args.output
    try:
        write_outputs(args.output, manifest)
        manifest["nodes"] = cluster_check(stress_env)
        monitor.start()
        for rate in (2, 4, 8):
            entry = {"sample": f"ci-cal-r{rate}", "kind": "calibration", "revision": "baseline",
                     "rate": rate, "seconds": 30, "pair": None, "slot": rate}
            manifest["runs"].append({**entry, "status": "running"})
            write_outputs(args.output, manifest)
            progress("START", manifest["runs"][-1])
            row = runner(entry, args, registration, (manager_env, stress_env))
            manifest["runs"][-1] = row
            if row["status"] == "complete":
                row["calibration"] = calibration_check(row)
            progress("DONE", row)
            require(row["status"] == "complete", "Calibration execution failed: " + entry["sample"])
            save(args.output / "runs" / entry["sample"] / "summary.json", row)
            write_outputs(args.output, manifest)
            if row["calibration"]["passed"]:
                manifest["selected_rate"] = rate
            elif rate == 2:
                raise RoundError("Calibration at 2 claims/s failed acceptance checks")
            require(not monitor_errors and monitor.is_alive(), "Linux monitor stopped unexpectedly")
        manifest["formal_schedule"] = formal_plan(manifest["selected_rate"])
        write_outputs(args.output, manifest)
        for entry in manifest["formal_schedule"]:
            manifest["runs"].append({**entry, "status": "running"})
            write_outputs(args.output, manifest)
            progress("START", manifest["runs"][-1])
            row = runner(entry, args, registration, (manager_env, stress_env))
            manifest["runs"][-1] = row
            progress("DONE", row)
            require(row["status"] == "complete", "Formal execution failed: " + entry["sample"])
            require(not monitor_errors and monitor.is_alive(), "Linux monitor stopped unexpectedly")
            write_outputs(args.output, manifest)
        paired_summary(manifest["runs"])
        manifest["status"] = "complete"
    except BaseException as error:
        manifest["status"] = "failed"
        manifest["errors"].append(safe_error(error))
        if manifest["runs"] and manifest["runs"][-1]["status"] == "running":
            manifest["runs"][-1].update(status="failed", errors=[safe_error(error)])
            progress("DONE", manifest["runs"][-1])
    finally:
        stop.set()
        if monitor.ident is not None:
            monitor.join(timeout=10)
            if monitor.is_alive():
                monitor_errors.append("Linux monitor did not stop")
        if monitor_errors:
            manifest["status"] = "failed"
            manifest["errors"].extend(monitor_errors)
        workload.ROOT = old_root
        manifest["ended_at"] = repeat_suite.utc()
        write_outputs(args.output, manifest)
    return 0 if manifest["status"] == "complete" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "candidate", "stress", "metrics-parser", "kubeconfig", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--schedule-emitter", type=Path)
    for role, default in (("baseline", BASELINE_COMMIT), ("candidate", CANDIDATE_COMMIT),
                          ("stress", CANDIDATE_COMMIT)):
        parser.add_argument(f"--{role}-commit", default=default)
        parser.add_argument(f"--{role}-sha256")
    args = parser.parse_args(argv)
    created = False
    old_handlers = {}
    try:
        require(os.name == "posix" and Path("/proc/stat").is_file(), "Native Linux is required")
        for role in ("baseline", "candidate", "stress", "metrics_parser", "schedule_emitter"):
            path = getattr(args, role)
            if path is not None:
                path = path.expanduser().resolve(strict=True)
                require(path.is_file() and os.access(path, os.X_OK), "Executable unavailable: " + role)
                setattr(args, role, path)
        args.kubeconfig = args.kubeconfig.expanduser().resolve(strict=True)
        require(args.kubeconfig.is_file(), "Kubeconfig must be a file")
        cpus = sorted(os.sched_getaffinity(0))
        require(bool(cpus), "No available CPUs")
        commits, hashes = {}, {}
        for role in ("baseline", "candidate", "stress"):
            commit = getattr(args, role + "_commit")
            require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None, "Invalid source commit: " + role)
            commits[role] = commit
        for role in ("baseline", "candidate", "stress", "metrics_parser", "schedule_emitter"):
            if getattr(args, role) is None:
                continue
            hashes[role] = sha256(getattr(args, role))
            expected = getattr(args, role + "_sha256", None)
            require(expected is None or expected == hashes[role], "Executable SHA256 mismatch: " + role)
        args.output = args.output.expanduser().absolute()
        require(not args.output.exists() and not args.output.is_symlink(), "Output must be a new directory")
        args.output.mkdir(parents=True)
        created = True
        (args.output / "runs").mkdir()
        registration = {"protocol": PROTOCOL, "controller_args": FLAGS,
                        "available_cpus": cpus, "controller_cpus": cpus[:4],
                        "source_commits": commits, "source_commit_basis": "Caller-declared build provenance",
                        "binary_sha256": hashes, "schedule_emitter_supplied": bool(args.schedule_emitter)}
        save(args.output / "config.json", registration)

        def interrupted(signum, frame):
            raise RoundError("Round interrupted")

        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, interrupted)
        return execute(args, registration)
    except BaseException as error:
        message = safe_error(error)
        if created:
            save(args.output / "failure.json", {"status": "failed", "error": message})
        print(json.dumps({"status": "failed", "error": message}), flush=True)
        return 1
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
