#!/usr/bin/env python3
"""Summarize paired unprofiled controller runs without modifying source results.

Usage: python3 -B summarize_unprofiled.py RESULTS [--output DESTINATION]
       python3 -B summarize_unprofiled.py --output RESULTS
Add --partial for an explicitly incomplete, descriptive view of completed runs.
"""

import argparse
import csv
import datetime as dt
import itertools
import json
import math
from pathlib import Path
import random
import re
import statistics
import subprocess

from summarize import load_stats, metric, quantile
from repeat_suite import schedule as registered_schedule


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REGISTERED_COMMITS = {
    "baseline": "4d39311ba9d89001d79ff1217ed330c7ea3761ec",
    "candidate": "a87fa000ec96c8e3d9be493da5c6e6e2048b37ad",
}
REGISTERED_BINARIES = {
    "baseline": "391936e7449042fa16effd512af0e9b811a95a83f3fd6a668bf7c13b7f4640ef",
    "candidate": "1abdc62ce6e1dc18afe34692d21e0a4ea0a7a23994918deb0b3b124bf5da21b7",
    "stress": "07bfc65345219b80cedcbb073123db88bf5b275eb683665913c585638b69bbb1",
}
REGISTERED_RUNTIME = {
    "image": "node:22-alpine", "gomaxprocs": 4, "stress_gomaxprocs": None,
    "cpus": [6, 8, 10, 12], "seed": [0, 0],
    "profile_enabled": False, "profile_seconds": 0, "profile_delay": 0,
    "metrics_interval_seconds": 5,
    "controller_args": [
        "--leader-elect=false", "--extensions",
        "--metrics-bind-address=127.0.0.1:18080",
        "--health-probe-bind-address=127.0.0.1:18081",
        "--metrics-secure-serving=false",
        "--pprof-mutex-profile-fraction=0", "--pprof-block-profile-rate=0",
        "--sandbox-concurrent-workers=100", "--sandbox-claim-concurrent-workers=50",
        "--sandbox-warm-pool-concurrent-workers=1",
        "--sandbox-template-concurrent-workers=1",
        "--sandbox-warm-pool-max-batch-size=32",
        "--sandbox-warm-pool-max-refill-rate=16",
    ],
}
VALIDATION_CRITERIA = {
    "basis": "Diagnostic completeness checks specified before full collection completed; not additional statistically preregistered thresholds",
    "metrics_interval_seconds": 5,
    "boundary_midpoint_delay_max_seconds": 5,
    "scrape_duration_max_seconds": 5,
    "telemetry_gap_max_seconds": 10,
    "host_interval_seconds": 5,
    "host_missing_tolerance_seconds": 15,
}
EXPECTED_PAIRS = {
    "plain60": 2, "long8": 6, "mid4": 2,
    "noise-baseline": 1, "noise-candidate": 1,
}
MEASURES = (
    "p50_ms", "p90_ms", "p99_ms", "create_ack_p50_ms",
    "ready_minus_ack_p50_ms", "ready_minus_ack_median_ms", "controller_cpu_seconds",
    "controller_alloc_bytes", "controller_cpu_cores",
    "controller_alloc_bytes_per_second",
)
SIGNED_LATENCY_MEASURES = ("ready_minus_ack_p50_ms", "ready_minus_ack_median_ms")


def registration_errors(revision, configuration, hashes):
    errors = []
    if revision not in REGISTERED_COMMITS:
        return ["Unknown registered revision"]
    if configuration.get("commit") != REGISTERED_COMMITS[revision]:
        errors.append("Registered source commit mismatch")
    for key, expected in REGISTERED_RUNTIME.items():
        if key not in configuration or configuration[key] != expected:
            errors.append(f"Registered runtime mismatch: {key}")
    for kind, expected in (("manager", REGISTERED_BINARIES[revision]),
                           ("stress", REGISTERED_BINARIES["stress"])):
        if hashes.get(kind) != expected:
            errors.append(f"Registered {kind} binary mismatch")
    return errors


def read_json(path):
    return json.loads(path.read_text())


def read_lines(path, live=False):
    text = path.read_text()
    result = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError:
            # A live monitor may be midway through its final JSONL write.
            if live and index == len(lines) - 1 and not text.endswith("\n"):
                break
            raise
    return result


def timestamp_ns(value):
    """Parse RFC3339 without rounding Go's nanoseconds through a float."""
    match = re.fullmatch(
        r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)",
        value,
    )
    if not match:
        raise ValueError(f"Invalid RFC3339 timestamp: {value!r}")
    whole, fraction, zone = match.groups()
    instant = dt.datetime.fromisoformat(whole + zone.replace("Z", "+00:00"))
    delta = instant - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return ((delta.days * 86400 + delta.seconds) * 1_000_000_000
            + int((fraction or "").ljust(9, "0")))


def monotonic(point):
    value = point["monotonic_seconds"]
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid monotonic timestamp")
    return value


def distribution(values):
    if not values:
        raise ValueError("Empty measurement distribution")
    return {
        "count": len(values), "p50_ms": quantile(values, .5),
        "p90_ms": quantile(values, .9), "p99_ms": quantile(values, .99),
        "mean_ms": statistics.mean(values),
    }


def percent_change(first, second):
    return None if first <= 0 else 100 * (second / first - 1)


def paired_inference(pairs, iterations=20000):
    """Exact sign flips and percentile bootstrap of run-pair log ratios."""
    if len(pairs) != 6 or any(a <= 0 or b <= 0 for a, b in pairs):
        raise ValueError("Primary endpoint requires six positive paired p50 values")
    logs = [math.log(b / a) for a, b in pairs]
    observed = abs(math.fsum(logs) / 6)
    extreme = sum(
        abs(math.fsum(sign * value for sign, value in zip(signs, logs)) / 6)
        >= math.nextafter(observed, -math.inf)
        for signs in itertools.product((-1, 1), repeat=6)
    )
    rng = random.Random(0)
    boot = sorted(
        sum(logs[rng.randrange(6)] for _ in range(6)) / 6
        for _ in range(iterations)
    )
    bounds = [quantile(boot, .025), quantile(boot, .975)]
    return {
        "endpoint": "long8 run-level p50_ms",
        "n_pairs": 6, "unit": "paired run", "paired_log_ratios": logs,
        "effect": "geometric mean candidate/baseline ratio",
        "ratio": math.exp(statistics.mean(logs)),
        "percent_change": 100 * math.expm1(statistics.mean(logs)),
        "two_sided_exact_sign_flip_p": extreme / 64,
        "sign_flip_assignments": 64,
        "bootstrap_seed": 0, "bootstrap_resamples": iterations,
        "bootstrap_method": "percentile; resample six whole run pairs",
        "ratio_ci95": [math.exp(value) for value in bounds],
        "percent_change_ci95": [100 * math.expm1(value) for value in bounds],
    }


def host_summary(records, start, end, windows):
    selected = [record for record in records
                if start <= timestamp_ns(record["utc"]) <= end]
    if len(selected) < 2:
        raise ValueError("Missing host samples in the traffic window")
    times = [timestamp_ns(record["utc"]) for record in selected]
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("Host timestamps are duplicated or out of order")
    all_times = [timestamp_ns(record["utc"]) for record in records]
    gaps = [(b - a) / 1e9 for a, b in zip(all_times, all_times[1:]) if b > a]
    cadence = statistics.median(gaps) if gaps else 0
    max_gap = max([times[0] - start, end - times[-1],
                   *(b - a for a, b in zip(times, times[1:]))]) / 1e9
    if max_gap > VALIDATION_CRITERIA["host_missing_tolerance_seconds"]:
        raise ValueError(f"Host telemetry gap of {max_gap:.3f}s within traffic")
    result = load_stats(selected, windows)
    if windows:
        result.update(
            available_mb_median=statistics.median(r["available_mb"] for r in selected),
            available_mb_max=max(r["available_mb"] for r in selected),
            page_input_median_per_sec=statistics.median(r["pages_input_per_sec"] for r in selected),
            page_input_nonzero_samples=sum(r["pages_input_per_sec"] > 0 for r in selected),
        )
    result.update(
        alignment="sample UTC in observed traffic trigger..boundary (inclusive)",
        first_sample_utc=selected[0]["utc"], last_sample_utc=selected[-1]["utc"],
        max_gap_seconds=max_gap, median_cadence_seconds=cadence,
        registered_interval_seconds=VALIDATION_CRITERIA["host_interval_seconds"],
        missing_tolerance_seconds=VALIDATION_CRITERIA["host_missing_tolerance_seconds"],
    )
    return result


def read_metrics(paths, binary=HERE / "analyze_metrics"):
    parsed = json.loads(subprocess.check_output(
        [str(binary), *(str(path) for path in paths)], text=True, timeout=30))
    reports = {}
    for report in parsed:
        key = str(Path(report["file"]).resolve())
        if key in reports:
            raise ValueError("Duplicate metrics parser report")
        if any(not math.isfinite(sample["value"]) for sample in report["samples"]):
            raise ValueError("Nonfinite Prometheus sample")
        reports[key] = report["samples"]
    expected = {str(path.resolve()) for path in paths}
    if set(reports) != expected:
        raise ValueError("Metrics parser returned missing or unexpected reports")
    return reports


def binary_hashes(path):
    hashes = {}
    for line in path.read_text().splitlines():
        digest, filename = line.split(maxsplit=1)
        if not re.fullmatch("[0-9a-fA-F]{64}", digest):
            raise ValueError("Invalid binary digest")
        name = Path(filename.lstrip("*")).name
        if name in hashes:
            raise ValueError("Duplicate binary digest")
        hashes[name] = digest.lower()
    if not {"manager", "stress"} <= hashes.keys():
        raise ValueError("Missing manager or stress digest")
    return hashes


def scenario_summary(rows):
    result = {
        "cold_start_samples": [r["sample"] for r in rows if r.get("has_cold_starts")],
        "mixed_cold_start_samples": [
            r["sample"] for r in rows if r.get("mixed_cold_start_scenario")],
        "inventory_exhaustion_samples": [
            r["sample"] for r in rows if r.get("inventory_exhaustion")],
        "unready_warm_adoption_samples": [
            r["sample"] for r in rows if r.get("has_unready_warm_adoptions")],
        "initial_inventory_observation_samples": [
            r["sample"] for r in rows if r.get("protocol_observations")],
        "inclusion_policy": "Retain all structurally valid registered runs and pairs, including cold launches and inventory exhaustion",
        "notes": [],
    }
    if result["cold_start_samples"]:
        result["notes"].append(
            "Cold launches are present; interpret the unchanged comparison as including cold-start workload.")
    if result["inventory_exhaustion_samples"]:
        result["notes"].append(
            "Ready inventory reached zero in telemetry samples; this is a workload-validity observation.")
    if result["unready_warm_adoption_samples"]:
        result["notes"].append(
            "Some pool adoptions were not Ready at adoption completion.")
    if result["initial_inventory_observation_samples"]:
        result["notes"].append(
            "Initial Ready inventory differs from 128; review startup adoption counters and prewarm validity without excluding the run.")
    return result


def summarize_run(entry, root, windows, linux, metrics_reader=read_metrics):
    row = {key: entry.get(key) for key in (
        "sample", "revision", "pair", "group", "rate", "seconds", "pool", "slot")}
    row.update(status="invalid", errors=[], protocol_observations=[])
    errors = row["errors"]

    def check(condition, message):
        if not condition:
            errors.append(message)

    try:
        sample = entry["sample"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", sample):
            raise ValueError("Unsafe sample path")
        path = root / "runs" / sample
        check(entry.get("status") == "completed", "Manifest run did not complete")
        check(entry.get("unprofiled") is True, "Manifest run is not unprofiled")
        config = read_json(path / "config.json")
        check(config.get("profile_enabled") is False, "Controller profiling is enabled")
        for left, right in (
                ("revision", "revision"), ("pool", "pool"),
                ("rate", "claims_per_second"), ("seconds", "seconds")):
            check(entry[left] == config[right], f"Manifest/config mismatch: {left}")
        row["configuration"] = {key: config[key] for key in (
            "commit", *REGISTERED_RUNTIME) if key in config}
        row["binary_sha256"] = binary_hashes(path / "binaries.sha256")
        errors.extend(registration_errors(
            entry["revision"], row["configuration"], row["binary_sha256"]))
        summary = read_json(path / "workload" / "summary.json")
        phases = [p for p in summary["phases"]
                  if p.get("kind", p.get("name")) == "claims-warm-sustained"]
        if len(phases) != 1:
            raise ValueError("Expected exactly one sustained phase")
        phase = phases[0]
        records = read_lines(path / "workload" / "sandboxes.jsonl")
        check(all(record.get("phase") == "claims-warm-sustained"
                  and record.get("phaseNumber") == phase["phaseNumber"]
                  for record in records), "Unexpected phase in lifecycle records")
        if not records:
            raise ValueError("No lifecycle records")
        namespace = summary["config"]["namespace"]
        pool_name = f"p{phase['phaseNumber']}-spool"
        check(all(record.get("namespace") == namespace for record in records),
              "Mixed lifecycle namespaces")
        identities = [(record["namespace"], record["name"]) for record in records]
        check(len(set(identities)) == len(records), "Duplicate lifecycle identity")
        check(not any(record.get("error") or record.get("error_present") for record in records),
              "Lifecycle contains errors")
        check(phase["created"] == phase["ready"] == len(records) and phase["failed"] == 0,
              "created/ready/failed counts do not match actual lifecycle records")
        fields = ("createCalled", "createReturned", "sandboxReady",
                  "deleteCalled", "sandboxDeleted")
        complete = [record for record in records if all(record.get(key) for key in fields)]
        row.update(
            created=phase["created"], ready=phase["ready"], failed=phase["failed"],
            records=len(records), complete_lifecycles=len(complete),
            requested=phase.get("requested"), namespace=namespace, warmpool_name=pool_name,
        )
        check(len(complete) == len(records), "Incomplete lifecycle timestamps")
        if not complete:
            raise ValueError("No complete lifecycle")
        latency, ack, after_ack, arrivals, ready_times = [], [], [], [], []
        wall_ready_deltas, wall_ack_deltas = [], []
        for record in complete:
            create, returned, ready, delete, gone = (
                timestamp_ns(record[key]) for key in fields)
            check(create <= returned and create <= ready <= delete <= gone,
                  f"Invalid lifecycle ordering: {record['name']}")
            check(bool(record.get("boundSandbox")), f"Missing boundSandbox: {record['name']}")
            # Go's exported durations retain its monotonic time component;
            # serialized UTC timestamps are for alignment and ordering.
            ready_ms, ack_ms = record["sandboxReadyMs"], record["createAckMs"]
            if not all(isinstance(value, (int, float)) and math.isfinite(value)
                       and value >= 0 for value in (ready_ms, ack_ms)):
                raise ValueError(f"Invalid exported latency: {record['name']}")
            latency.append(ready_ms)
            ack.append(ack_ms)
            after_ack.append(ready_ms - ack_ms)
            wall_ready_deltas.append(abs((ready - create) / 1e6 - ready_ms))
            wall_ack_deltas.append(abs((returned - create) / 1e6 - ack_ms))
            arrivals.append(create)
            ready_times.append(ready)
        for prefix, values in (("", latency), ("create_ack_", ack),
                               ("ready_minus_ack_", after_ack)):
            for key, value in distribution(values).items():
                row[prefix + key] = value
        row["ready_before_ack_count"] = sum(value < 0 for value in after_ack)
        row["ready_minus_ack_median_ms"] = statistics.median(after_ack)
        row["latency_duration_source"] = "exported sandboxReadyMs and createAckMs"
        row["utc_vs_exported_ready_max_abs_ms"] = max(wall_ready_deltas)
        row["utc_vs_exported_ack_max_abs_ms"] = max(wall_ack_deltas)
        row["actual_first_create_utc"] = min(complete, key=lambda r: timestamp_ns(
            r["createCalled"]))["createCalled"]
        row["actual_last_create_utc"] = max(complete, key=lambda r: timestamp_ns(
            r["createCalled"]))["createCalled"]
        row["actual_arrival_span_seconds"] = (max(arrivals) - min(arrivals)) / 1e9
        row["created_per_configured_second"] = len(records) / config["seconds"]
        for kind, values in (("endToEndReady", latency), ("createAck", ack)):
            for q in (.5, .9, .99):
                key = f"p{int(q * 100)}Ms"
                check(math.isclose(phase["latency"][kind][key], quantile(values, q),
                                   rel_tol=1e-7, abs_tol=1e-5),
                      f"summary/lifecycle latency mismatch: {kind}.{key}")

        capture = read_json(path / "telemetry" / "run.json")
        check(capture.get("status") == "complete" and capture.get("exit_code") == 0
              and not capture.get("errors"), "Telemetry run failed or contains errors")
        trigger, boundary = capture["trigger"], capture["boundary"]
        check(trigger["phase"] == "claims-warm-sustained"
              and trigger["phase_number"] == phase["phaseNumber"],
              "Telemetry trigger phase mismatch")
        check(boundary["kind"] == "arrivals_finished", "Missing arrivals_finished boundary")
        start, end = timestamp_ns(trigger["at"]["utc"]), timestamp_ns(boundary["at"]["utc"])
        if end <= start or monotonic(boundary["at"]) <= monotonic(trigger["at"]):
            raise ValueError("Invalid traffic interval")
        row.update(
            traffic_start_utc=trigger["at"]["utc"], traffic_end_utc=boundary["at"]["utc"],
            traffic_observed_seconds=monotonic(boundary["at"]) - monotonic(trigger["at"]),
            ready_after_traffic_count=sum(value > end for value in ready_times),
            creates_before_observed_trigger=sum(value < start for value in arrivals),
            creates_after_observed_boundary=sum(value > end for value in arrivals),
        )
        samples = capture["samples"]
        if (len(samples) < 3 or samples[0]["tag"] != "start"
                or samples[-1]["tag"] != "end"
                or any(item["tag"] != "periodic" for item in samples[1:-1])):
            raise ValueError("Require start, periodic and end telemetry samples in order")
        artifacts, midpoints, ready_inventory, scrape_intervals = [], [], [], []
        previous = -math.inf
        for index, item in enumerate(samples):
            if item["status"] != "complete":
                raise ValueError(f"Failed telemetry sample: {item.get('artifact')}")
            artifact = Path(item["artifact"])
            if artifact.is_absolute() or len(artifact.parts) != 1:
                raise ValueError("Telemetry artifact must be a local filename")
            numbered = re.fullmatch(r"metrics-(\d+)\.prom", artifact.name)
            if not numbered or int(numbered[1]) != index:
                raise ValueError("Missing or out-of-order telemetry artifact number")
            artifacts.append(path / "telemetry" / artifact)
            begin, finish = monotonic(item["started_at"]), monotonic(item["ended_at"])
            if begin < previous or finish < begin:
                raise ValueError("Overlapping or unordered telemetry intervals")
            if timestamp_ns(item["ended_at"]["utc"]) < timestamp_ns(item["started_at"]["utc"]):
                raise ValueError("Reversed telemetry UTC interval")
            utc_duration = (timestamp_ns(item["ended_at"]["utc"])
                            - timestamp_ns(item["started_at"]["utc"])) / 1e9
            check(max(finish - begin, utc_duration)
                  <= VALIDATION_CRITERIA["scrape_duration_max_seconds"],
                  f"Telemetry scrape exceeds 5s: {item['artifact']}")
            previous = finish
            midpoints.append((begin + finish) / 2)
            scrape_intervals.append({
                "tag": item["tag"], "artifact": item["artifact"],
                "started_at": item["started_at"], "ended_at": item["ended_at"],
            })
        if len(set(artifacts)) != len(artifacts):
            raise ValueError("Duplicate telemetry artifacts")
        duration = midpoints[-1] - midpoints[0]
        if duration <= 0:
            raise ValueError("Invalid telemetry counter interval")
        final_path = path / "metrics-final.prom"
        reports = metrics_reader([*artifacts, final_path])
        parsed = [reports[str(artifact.resolve())] for artifact in artifacts]
        labels = {"namespace": namespace, "warmpool_name": pool_name}
        for metrics in parsed:
            ready_inventory.append(metric(
                metrics, "agent_sandbox_warmpool_size", required=True,
                sandbox_status="ready", **labels))
        check(all(value >= 0 and value == int(value) for value in ready_inventory),
              "Invalid Ready inventory values")
        cpu = [metric(m, "process_cpu_seconds_total", required=True) for m in parsed]
        alloc = [metric(m, "go_memstats_alloc_bytes_total", required=True) for m in parsed]
        for name, values in (("CPU", cpu), ("allocation", alloc)):
            check(all(value >= 0 for value in values)
                  and all(b >= a for a, b in zip(values, values[1:])),
                  f"{name} counter reset or invalid value")
        cpu_delta, alloc_delta = cpu[-1] - cpu[0], alloc[-1] - alloc[0]
        row.update(
            controller_cpu_seconds=cpu_delta, controller_alloc_bytes=alloc_delta,
            controller_cpu_cores=cpu_delta / duration,
            controller_alloc_bytes_per_second=alloc_delta / duration,
            counter_window_seconds=duration,
            counter_window_method="difference between start/end scrape monotonic midpoints",
            counter_window_start_utc=samples[0]["started_at"]["utc"],
            counter_window_end_utc=samples[-1]["ended_at"]["utc"],
            counter_window_min_seconds=max(
                0, monotonic(samples[-1]["started_at"]) - monotonic(samples[0]["ended_at"])),
            counter_window_max_seconds=(
                monotonic(samples[-1]["ended_at"]) - monotonic(samples[0]["started_at"])),
            counter_start_offset_from_trigger_seconds=(
                midpoints[0] - monotonic(trigger["at"])),
            counter_end_offset_from_boundary_seconds=(
                midpoints[-1] - monotonic(boundary["at"])),
            telemetry_max_midpoint_gap_seconds=max(
                b - a for a, b in zip(midpoints, midpoints[1:])),
            telemetry_max_start_gap_seconds=max(
                monotonic(b["started_at"]) - monotonic(a["started_at"])
                for a, b in zip(samples, samples[1:])),
            telemetry_max_scrape_seconds=max(
                monotonic(item["ended_at"]) - monotonic(item["started_at"])
                for item in samples),
            ready_pool_start=ready_inventory[0], ready_pool_end=ready_inventory[-1],
            ready_pool_sampled_min=min(ready_inventory),
            ready_pool_sampled_max=max(ready_inventory),
            ready_pool_samples=len(ready_inventory),
            ready_pool_zero_samples=sum(value == 0 for value in ready_inventory),
            inventory_exhaustion=any(value == 0 for value in ready_inventory),
            inventory_exhaustion_evidence=(
                "sampled_zero" if any(value == 0 for value in ready_inventory) else "not_observed"),
            telemetry_samples=[dict(item, ready_pool=value)
                               for item, value in zip(scrape_intervals, ready_inventory)],
        )
        for label, item, boundary_point in (
                ("start", samples[0], trigger["at"]), ("end", samples[-1], boundary["at"])):
            # Subtract before averaging to retain precision for epoch nanoseconds.
            utc_offset = (
                (timestamp_ns(item["started_at"]["utc"]) - timestamp_ns(boundary_point["utc"]))
                + (timestamp_ns(item["ended_at"]["utc"]) - timestamp_ns(boundary_point["utc"]))
            ) / 2e9
            endpoint = "trigger" if label == "start" else "boundary"
            row[f"counter_{label}_offset_from_{endpoint}_utc_seconds"] = utc_offset
            offset = row[f"counter_{label}_offset_from_{endpoint}_seconds"]
            check(monotonic(item["started_at"]) >= monotonic(boundary_point),
                  f"Telemetry {label} scrape precedes observed {endpoint}")
            check(0 <= offset <= VALIDATION_CRITERIA["boundary_midpoint_delay_max_seconds"]
                  and 0 <= utc_offset <= VALIDATION_CRITERIA["boundary_midpoint_delay_max_seconds"],
                  f"Telemetry {label} boundary delay exceeds 5s or precedes boundary")
        check(max(row["telemetry_max_midpoint_gap_seconds"], row["telemetry_max_start_gap_seconds"])
              <= VALIDATION_CRITERIA["telemetry_gap_max_seconds"],
              "Telemetry sample gap exceeds 10s")
        start_adoptions = metric(
            parsed[0], "agent_sandbox_claim_creation_total",
            launch_type="warm", pod_condition="ready", **labels)
        check(start_adoptions >= 0 and start_adoptions == int(start_adoptions),
              "Invalid startup warm/ready counter")
        adjusted_ready = ready_inventory[0] + start_adoptions
        assessment = (
            "snapshot_matches" if ready_inventory[0] == entry["pool"] else
            "adoption_adjusted_support" if ready_inventory[0] < entry["pool"]
            and adjusted_ready >= entry["pool"] else "not_supported")
        row["initial_inventory"] = {
            "expected_ready": entry["pool"], "observed_ready": ready_inventory[0],
            "warm_ready_launches_at_start": start_adoptions,
            "ready_plus_warm_ready_launches": adjusted_ready, "assessment": assessment,
        }
        if assessment != "snapshot_matches":
            row["protocol_observations"].append({
                "kind": "initial_ready_inventory", "assessment": assessment,
                "note": "Start scrape is expected to show 128 Ready members; startup warm/ready adoptions may explain a deficit. Counts support compatibility, not a direct pre-arrival snapshot.",
            })
        conditions = {"warm/ready": 0, "warm/not_ready": 0, "cold/not_ready": 0}
        for sample_metric in reports[str(final_path.resolve())]:
            if sample_metric["name"] != "agent_sandbox_claim_creation_total":
                continue
            tags = sample_metric["labels"]
            if any(tags.get(key) != value for key, value in labels.items()):
                continue
            key = tags.get("launch_type", "") + "/" + tags.get("pod_condition", "")
            value = sample_metric["value"]
            check(key in conditions, f"Unexpected launch condition: {key}")
            check(value >= 0 and value == int(value), "Invalid claim creation counter")
            conditions[key] = conditions.get(key, 0) + value
        row["launch_conditions"] = conditions
        check(sum(conditions.values()) == len(records), "Launch counters differ from actual claims")
        row["warm"] = conditions["warm/ready"] + conditions["warm/not_ready"]
        row["cold"] = conditions["cold/not_ready"]
        row["has_cold_starts"] = row["cold"] > 0
        row["cold_percent"] = 100 * row["cold"] / len(records)
        row["mixed_cold_start_scenario"] = row["cold"] > 0 and row["warm"] > 0
        row["has_unready_warm_adoptions"] = conditions["warm/not_ready"] > 0
        row["scenario_notes"] = scenario_summary([row])["notes"]
        for key, records_host, is_windows in (("windows", windows, True), ("linux", linux, False)):
            try:
                row[key] = host_summary(records_host, start, end, is_windows)
            except (ValueError, KeyError, TypeError) as exc:
                errors.append(f"{key} host telemetry: {exc}")
    except (OSError, ValueError, KeyError, TypeError, IndexError,
            subprocess.SubprocessError) as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    row["status"] = "complete" if not errors else "invalid"
    row["protocol_validity"] = (
        "invalid" if errors else
        "review_required" if row["protocol_observations"] else "recorded_checks_passed")
    return row


def manifest_errors(manifest, partial=False):
    errors = []
    if partial:
        if (manifest.get("status") not in ("running", "completed")
                or manifest.get("windows_monitor_exit") not in (None, 0)):
            errors.append("Suite or Windows monitoring failed")
    elif manifest.get("status") != "completed" or manifest.get("windows_monitor_exit") != 0:
        errors.append("Suite or Windows monitoring did not complete")
    runs = [entry for entry in manifest.get("runs", []) if not entry.get("warmup", False)]
    schedule = [entry for entry in manifest.get("schedule", []) if not entry.get("warmup", False)]
    registered = registered_schedule(extended=True)
    if [entry.get("sample") for entry in schedule] != [entry["sample"] for entry in registered]:
        errors.append("Manifest schedule differs from registered 24-run order")
    for supplied, fixed in zip(schedule, registered):
        for key, value in fixed.items():
            if key not in supplied or supplied[key] != value:
                errors.append(f"{fixed['sample']}: registered schedule mismatch for {key}")
    if [entry.get("sample") for entry in runs] != [
            entry["sample"] for entry in registered[:len(runs)]]:
        errors.append("Manifest run order differs from registered execution order")
    for entry in manifest.get("runs", []) + manifest.get("schedule", []):
        if "warmup" in entry and not isinstance(entry["warmup"], bool):
            errors.append("Manifest warmup flag must be boolean")
    def indexed(entries):
        result = {}
        for entry in entries:
            sample = entry.get("sample")
            if not sample or sample in result:
                errors.append("Missing or duplicate sample in manifest")
            result[sample] = entry
        return result
    actual, expected = indexed(runs), indexed(schedule)
    if (not expected or not set(actual) <= set(expected)
            or (not partial and set(actual) != set(expected))):
        errors.append("Schedule/runs sample mismatch")
    for sample in actual.keys() & expected.keys():
        for key in ("group", "revision", "pair", "rate", "seconds", "pool", "slot", "unprofiled"):
            if key not in actual[sample] or actual[sample].get(key) != expected[sample].get(key):
                errors.append(f"{sample}: schedule/run mismatch for {key}")
    if partial:
        for entry in runs:
            if entry.get("status") not in ("completed", "running", "pending"):
                errors.append(f"{entry.get('sample')}: run failed or has unknown status")
    return errors


def group_results(rows, partial=False):
    groups, errors = {}, []
    for name in dict.fromkeys([*EXPECTED_PAIRS, *(row.get("group") for row in rows)]):
        subset = [row for row in rows if row.get("group") == name]
        grouped = {}
        for row in subset:
            key = (row.get("rate"), row.get("seconds"), row.get("pool"), row.get("pair"))
            grouped.setdefault(key, []).append(row)
        group = {
            "kind": "negative_control" if str(name).startswith("noise-") else "comparison",
            "pairs": [], "descriptive_by_revision": {},
            "scenario_observations": scenario_summary(subset),
        }
        groups[name] = group
        if name not in EXPECTED_PAIRS:
            errors.append(f"Unexpected group: {name}")
        elif (len(grouped) > EXPECTED_PAIRS[name]
              or (not partial and len(grouped) != EXPECTED_PAIRS[name])):
            errors.append(f"{name}: expected {EXPECTED_PAIRS[name]} pairs, got {len(grouped)}")
        primary_pairs = []
        for (rate, seconds, pool, pair), members in grouped.items():
            item = {"pair": pair, "rate": rate, "seconds": seconds, "pool": pool,
                    "samples": [row["sample"] for row in members], "status": "invalid",
                    "scenario_observations": scenario_summary(members)}
            group["pairs"].append(item)
            reason = None
            if partial and len(members) == 1:
                item.update(status="incomplete", reason="Awaiting paired run")
                continue
            if len(members) != 2 or {r.get("slot") for r in members} != {1, 2}:
                reason = "Require exactly two runs with slots 1 and 2"
            elif any(r["status"] != "complete" for r in members):
                reason = "Pair contains an invalid run"
            elif name == "long8" and (rate, seconds, pool) != (8, 120, 128):
                reason = "long8 requires rate=8, seconds=120, pool=128"
            elif name == "mid4" and (rate, seconds, pool) != (4, 120, 128):
                reason = "mid4 requires rate=4, seconds=120, pool=128"
            elif name == "plain60" and (seconds, pool) != (60, 128):
                reason = "plain60 requires seconds=60, pool=128"
            if reason is None:
                first, second = sorted(members, key=lambda r: r["slot"])
                if group["kind"] == "negative_control":
                    revision = name.removeprefix("noise-")
                    if any(r["revision"] != revision for r in members):
                        reason = "Negative-control revision mismatch"
                    elif first["binary_sha256"] != second["binary_sha256"]:
                        reason = "Negative-control binary mismatch"
                    item["direction"] = "second/first, same binary"
                else:
                    if {r["revision"] for r in members} != {"baseline", "candidate"}:
                        reason = "Require one baseline and one candidate"
                    else:
                        first = next(r for r in members if r["revision"] == "baseline")
                        second = next(r for r in members if r["revision"] == "candidate")
                    item["direction"] = "candidate/baseline"
                if first["binary_sha256"]["stress"] != second["binary_sha256"]["stress"]:
                    reason = "Stress binary differs within pair"
                for key in ("image", "gomaxprocs", "stress_gomaxprocs", "cpus", "seed", "controller_args"):
                    if first["configuration"].get(key) != second["configuration"].get(key):
                        reason = f"Pair configuration differs: {key}"
                if first["created"] != second["created"]:
                    reason = "Actual claim count differs within paired fixed-seed workloads"
                if reason is None:
                    violations = [
                        problem for member in members for problem in registration_errors(
                            member["revision"], member["configuration"], member["binary_sha256"])]
                    if violations:
                        reason = "; ".join(violations)
            if reason:
                item["error"] = reason
                errors.append(f"{name}/{pair}: {reason}")
                continue
            item.update(
                status="complete", numerator=second["sample"], denominator=first["sample"],
                ratio={key: None if first[key] <= 0 else second[key] / first[key]
                       for key in MEASURES},
                percent_change={key: percent_change(first[key], second[key]) for key in MEASURES},
                difference_ms={key: second[key] - first[key] for key in SIGNED_LATENCY_MEASURES},
            )
            if name == "long8":
                primary_pairs.append((first["p50_ms"], second["p50_ms"]))
        # Keep each load stratum separate even when a group contains several rates.
        if group["kind"] == "comparison":
            for row in subset:
                if row["status"] != "complete":
                    continue
                stratum = f"rate={row['rate']},seconds={row['seconds']},pool={row['pool']}"
                group["descriptive_by_revision"].setdefault(stratum, {})
            for stratum, description in group["descriptive_by_revision"].items():
                for revision in ("baseline", "candidate"):
                    selected = [r for r in subset if r["status"] == "complete"
                                and r["revision"] == revision
                                and stratum == f"rate={r['rate']},seconds={r['seconds']},pool={r['pool']}"]
                    description[revision] = {
                        "n_runs": len(selected),
                        **{key: {"median": statistics.median(r[key] for r in selected),
                                 "min": min(r[key] for r in selected),
                                 "max": max(r[key] for r in selected)}
                           for key in MEASURES if selected},
                    }
        if name == "long8":
            if partial:
                group["primary_inference"] = {
                    "status": "deferred", "n_pairs": len(primary_pairs),
                    "reason": "Partial views are descriptive; run the full validated summary for inference",
                }
            elif len(primary_pairs) == 6 and len(grouped) == 6:
                try:
                    group["primary_inference"] = paired_inference(primary_pairs)
                except ValueError as exc:
                    errors.append(str(exc))
            else:
                group["primary_inference"] = {"status": "unavailable", "n_pairs": len(primary_pairs)}
    return groups, errors


def analyze(results, root=ROOT, metrics_reader=read_metrics, partial=False):
    manifest = read_json(results / "manifest.json")
    errors = manifest_errors(manifest, partial)
    formal = [entry for entry in manifest.get("runs", []) if not entry.get("warmup", False)]
    selected = [entry for entry in formal if not partial or entry.get("status") == "completed"]
    host = {}
    for kind in ("windows", "linux"):
        try:
            host[kind] = read_lines(results / f"{kind}-load.jsonl", live=partial)
        except (OSError, ValueError) as exc:
            host[kind] = []
            if selected or not partial:
                errors.append(f"{kind}: {exc}")
    rows = [
        summarize_run(entry, root, host["windows"], host["linux"], metrics_reader)
        for entry in selected
    ]
    groups, grouping_errors = group_results(rows, partial)
    errors.extend(grouping_errors)
    errors.extend(f"{row['sample']}: {error}" for row in rows for error in row["errors"])
    if errors and "primary_inference" in groups["long8"]:
        groups["long8"]["primary_inference"] = {
            "status": "unavailable", "reason": "Resolve validation errors before primary inference"}
    completed = {entry["sample"] for entry in formal if entry.get("status") == "completed"}
    pending = [entry["sample"] for entry in manifest.get("schedule", [])
               if not entry.get("warmup", False) and entry["sample"] not in completed]
    return {
        "schema_version": 1,
        "status": "incomplete" if partial else ("complete" if not errors else "invalid"),
        "partial": partial, "validation_status": "invalid" if errors else "valid",
        "suite_status": manifest.get("status"), "pending_samples": pending,
        "notice": "INCOMPLETE: completed-run snapshot; no full-suite inference" if partial else None,
        "errors": errors, "runs": rows, "groups": groups,
        "validation_criteria": dict(VALIDATION_CRITERIA),
        "scenario_observations": scenario_summary(rows),
        "excluded_warmups": [entry["sample"] for entry in manifest.get("runs", [])
                             if entry.get("warmup", False)],
        "definitions": {
            "independent_unit": "paired run; claims are not independent experimental replicates",
            "latency": "nearest-rank quantiles of exported Go duration fields for all claims through final Ready, including drain",
            "ready_minus_ack": "per-request sandboxReadyMs - createAckMs; median_ms averages the middle two for even n; p50_ms uses nearest rank; signed watch/API timing",
            "paired_difference_ms": "Ready-minus-Ack endpoints use candidate minus baseline (noise: slot 2 minus slot 1); ratios and percent changes are null for denominators <= 0",
            "host_window": "observed traffic trigger..arrivals_finished UTC, not first claim plus nominal duration",
            "host_validity": "memory, paging and CPU readings remain visible independently of lifecycle validation; interpret latency alongside controller/API and host load",
            "controller_window": "start/end telemetry counter difference; monotonic scrape midpoint duration",
            "inventory": "sampled Ready inventory extrema, not continuous-time extrema",
            "initial_inventory": "Expected start Ready=128; start Ready plus startup warm/ready adoptions can support compatibility with prewarm, without proving pre-arrival state. Deviations remain protocol observations and do not exclude runs or pairs.",
            "inventory_exhaustion": "true when any telemetry sample has Ready inventory zero; not_observed leaves between-sample exhaustion unresolved",
            "cold_starts": "cold/not_ready final counter, reported separately from sampled inventory exhaustion",
            "scenario_inclusion": "Cold launches, unready pool adoptions and inventory exhaustion are observations, not exclusion criteria; retain complete runs in the registered primary comparison",
            "launch_conditions": "Sandbox Ready state at adoption completion; namespace/pool-scoped final counters",
            "statistics": "long8 p50 only is inferential; all other endpoints and groups are descriptive",
            "negative_controls": "same-binary slot 2 / slot 1, separate from baseline/candidate comparisons",
            "effect_sign": "negative percent change means lower candidate value (noise: lower second value)",
        },
    }


def write_report(report, destination):
    destination.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    (destination / "summary-unprofiled.json").write_text(text)
    flattened = []
    for row in report["runs"]:
        flattened.append(dict({
            key: json.dumps(value, sort_keys=True, allow_nan=False)
            if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        }, summary_status=report["status"], partial=report.get("partial", False)))
    keys = list(dict.fromkeys(key for row in flattened for key in row))
    with (destination / "summary-unprofiled.csv").open("w", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=keys or ["sample", "status", "errors"])
        writer.writeheader()
        writer.writerows(flattened)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--partial", action="store_true",
                        help="completed-run snapshot, explicitly incomplete; defer inference")
    args = parser.parse_args(argv)
    results = (args.results or args.output or HERE / "results").resolve()
    destination = (args.output or results).resolve()
    try:
        report = analyze(results, partial=args.partial)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {"status": "invalid", "errors": [f"{type(exc).__name__}: {exc}"],
                  "runs": [], "groups": {}}
    write_report(report, destination)
    print(json.dumps({"status": report["status"], "runs": len(report["runs"]),
                      "errors": report["errors"]}, indent=2))
    return 0 if not report["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
