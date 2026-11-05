#!/usr/bin/env python3
"""Summarize complete controller runs, workload-aligned telemetry, and profiles."""

import argparse
import csv
import datetime as dt
import json
import math
from pathlib import Path
import re
import statistics
import subprocess


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def timestamp(value):
    # Python 3.10 accepts microseconds; Go and PowerShell emit finer precision.
    normalized = re.sub(
        r"\.(\d+)(?=(?:Z|[+-]\d{2}:\d{2})$)",
        lambda match: "." + match[1][:6].ljust(6, "0"), value,
    )
    return dt.datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()


def load(path):
    return json.loads(path.read_text())


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def quantile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def verified_latency(records, phase, sample):
    """Recompute full-phase exported durations and check the Go phase summary."""
    fields = ("createCalled", "createReturned", "sandboxReady", "deleteCalled", "sandboxDeleted")
    if (not records
            or any(record.get("error") or record.get("error_present")
                   or not all(record.get(key) for key in fields) for record in records)):
        raise ValueError(f"Failed or incomplete claim lifecycle: {sample}")
    for key, expected in (("created", len(records)), ("ready", len(records)), ("failed", 0)):
        if type(phase.get(key)) is not int or phase[key] != expected:
            raise ValueError(f"Phase count mismatch: {sample}: {key}")
    result = {}
    for kind, field in (("endToEndReady", "sandboxReadyMs"), ("createAck", "createAckMs")):
        values = [record.get(field) for record in records]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in values):
            raise ValueError(f"Invalid exported latency: {sample}: {field}")
        stats = {
            "count": len(values), "minMs": min(values), "maxMs": max(values),
            "meanMs": statistics.mean(values),
            **{f"p{percent}Ms": quantile(values, percent / 100) for percent in (50, 90, 95, 99)},
        }
        reported = phase.get("latency", {}).get(kind, {})
        if not isinstance(reported, dict):
            raise ValueError(f"Missing phase latency: {sample}: {kind}")
        for key, expected in stats.items():
            value = reported.get(key)
            if key == "count":
                valid = type(value) is int and value == expected
            else:
                # Go's phase mean truncates the average duration to whole nanoseconds.
                tolerance = 1e-6 if key == "meanMs" else 1e-9
                valid = (type(value) in (int, float) and math.isfinite(value) and value >= 0
                         and math.isclose(value, expected, rel_tol=1e-12, abs_tol=tolerance))
            if not valid:
                raise ValueError(f"Summary/lifecycle latency mismatch: {sample}: {kind}.{key}")
        result[kind] = stats
    return result


def metric(samples, name, required=False, **labels):
    values = [
        sample["value"] for sample in samples
        if sample["name"] == name and
        all(sample["labels"].get(key) == value for key, value in labels.items())
    ]
    if required and not values:
        raise ValueError(f"Required metric missing: {name}, {labels}")
    return sum(values)


def profile_type(profile, kind):
    return next(sample for sample in profile["sample_types"] if sample["type"] == kind)


def aligned(records, start, end):
    return [record for record in records if start <= timestamp(record["utc"]) <= end]


def load_stats(records, windows):
    if not records or any("error" in record or record.get("error_present") for record in records):
        raise ValueError("Missing or failed host telemetry within workload")
    if windows:
        return {
            "samples": len(records),
            "cpu_median_percent": statistics.median(record["cpu_percent"] for record in records),
            "cpu_max_percent": max(record["cpu_percent"] for record in records),
            "available_mb_min": min(record["available_mb"] for record in records),
            "page_input_max_per_sec": max(record["pages_input_per_sec"] for record in records),
            "process_one_core_cpu_max": max(
                (process["cpu_one_core_percent"]
                 for record in records for process in record["processes"]), default=0,
            ),
        }
    return {
        "samples": len(records),
        "cpu_median_percent": statistics.median(record["cpu_active_percent"] for record in records),
        "cpu_max_percent": max(record["cpu_active_percent"] for record in records),
        "available_mb_min": min(record["available_kb"] for record in records) / 1024,
        "swap_in_pages": records[-1]["pswpin"] - records[0]["pswpin"],
        "swap_out_pages": records[-1]["pswpout"] - records[0]["pswpout"],
        "cpu_pressure_avg10_max": max(record["pressure"]["cpu"]["some"]["avg10"] for record in records),
        "memory_pressure_avg10_max": max(record["pressure"]["memory"]["some"]["avg10"] for record in records),
        "io_pressure_avg10_max": max(record["pressure"]["io"]["some"]["avg10"] for record in records),
    }


def summarize_run(entry, profiles, metrics, windows, linux):
    sample = entry["sample"]
    path = ROOT / "runs" / sample
    config = load(path / "config.json")
    summary = load(path / "workload" / "summary.json")
    phase = summary["phases"][0]
    capture = load(path / "profile" / "run.json")
    records = lines(path / "workload" / "sandboxes.jsonl")
    records = [record for record in records if record.get("phase") == "claims-warm-sustained"]
    if capture["status"] != "complete" or capture["errors"]:
        raise ValueError(f"Incomplete profile: {sample}")
    if not capture["profile_window"]["within_observed_workload"]:
        raise ValueError(f"Profile extends outside workload: {sample}")
    latency = verified_latency(records, phase, sample)
    pstart = timestamp(capture["requests"]["cpu"]["started_at"]["utc"])
    pend = timestamp(capture["requests"]["cpu"]["ended_at"]["utc"])
    cohort = [record for record in records if pstart <= timestamp(record["createCalled"]) <= pend]
    workload_start = min(timestamp(record["createCalled"]) for record in records)
    workload_end = workload_start + config["seconds"]
    warm_samples = metrics[str(path / "metrics-final.prom")]
    start_metrics = metrics[str(path / "profile" / "metrics-start.txt")]
    end_metrics = metrics[str(path / "profile" / "metrics-end.txt")]
    cpu = profile_type(profiles[(sample, "cpu.pprof")], "cpu")
    allocations = profile_type(profiles[(sample, "allocs.pprof")], "alloc_space")
    mutex = profile_type(profiles[(sample, "mutex.pprof")], "delay")
    warm = metric(warm_samples, "agent_sandbox_claim_creation_total", launch_type="warm")
    cold = metric(warm_samples, "agent_sandbox_claim_creation_total", launch_type="cold")
    if warm + cold != phase["created"]:
        raise ValueError(f"Claim counters differ from workload: {sample}")
    samples_by_condition = {}
    for item in warm_samples:
        if item["name"] == "agent_sandbox_claim_creation_total":
            key = item["labels"]["launch_type"] + "/" + item["labels"]["pod_condition"]
            samples_by_condition[key] = samples_by_condition.get(key, 0) + item["value"]
    cohorts = [record["sandboxReadyMs"] for record in cohort]
    return {
        "sample": sample, "rate": entry["rate"], "pair": entry["pair"], "revision": entry["revision"],
        "created": phase["created"], "ready": phase["ready"], "failed": phase["failed"],
        "records": len(records), "complete_lifecycles": len(records),
        "launch_conditions": samples_by_condition,
        "warm": warm, "cold": cold,
        "p50_ms": latency["endToEndReady"]["p50Ms"],
        "p90_ms": latency["endToEndReady"]["p90Ms"],
        "p99_ms": latency["endToEndReady"]["p99Ms"],
        "mean_ms": latency["endToEndReady"]["meanMs"],
        "create_ack_p50_ms": latency["createAck"]["p50Ms"],
        "profile_cohort_count": len(cohort),
        "profile_cohort_p50_ms": quantile(cohorts, .5),
        "profile_cohort_p99_ms": quantile(cohorts, .99),
        "profile_cohort_ready_after_window": sum(timestamp(record["sandboxReady"]) > pend for record in cohort),
        "profile_cpu_seconds": cpu["total"] / 1e9,
        "queue_cpu_seconds": cpu["queue_stack_total"] / 1e9,
        "queue_cpu_percent": cpu["queue_stack_percentage"],
        "profile_alloc_bytes": allocations["total"],
        "queue_alloc_bytes": allocations["queue_stack_total"],
        "profile_mutex_seconds": mutex["total"] / 1e9,
        "queue_mutex_seconds": mutex["queue_stack_total"] / 1e9,
        "controller_cpu_seconds": metric(end_metrics, "process_cpu_seconds_total", required=True)
            - metric(start_metrics, "process_cpu_seconds_total", required=True),
        "controller_alloc_bytes": metric(end_metrics, "go_memstats_alloc_bytes_total", required=True)
            - metric(start_metrics, "go_memstats_alloc_bytes_total", required=True),
        "ready_pool_start": metric(start_metrics, "agent_sandbox_warmpool_size", required=True, sandbox_status="ready"),
        "ready_pool_end": metric(end_metrics, "agent_sandbox_warmpool_size", required=True, sandbox_status="ready"),
        "window_start_utc": dt.datetime.fromtimestamp(workload_start, dt.timezone.utc).isoformat(),
        "window_end_utc": dt.datetime.fromtimestamp(workload_end, dt.timezone.utc).isoformat(),
        "windows": load_stats(aligned(windows, workload_start, workload_end), True),
        "linux": load_stats(aligned(linux, workload_start, workload_end), False),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    output = args.results.resolve()
    manifest = load(output / "manifest.json")
    if manifest["status"] != "completed" or manifest["windows_monitor_exit"] != 0:
        raise ValueError("Suite or host monitoring did not complete")
    runs = [entry for entry in manifest["runs"] if not entry["warmup"]]
    if len(runs) != 12 or any(entry["status"] != "completed" for entry in runs):
        raise ValueError("The registered twelve runs must all complete")
    profile_paths = [
        str(ROOT / "runs" / entry["sample"] / "profile" / f"{kind}.pprof")
        for entry in runs for kind in ("cpu", "mutex", "allocs")
    ]
    profile_data = json.loads(subprocess.check_output(
        [str(ROOT / "tools" / "analyze_profiles"), *profile_paths], text=True))
    if profile_data["errors"]:
        raise ValueError(profile_data["errors"])
    (output / "profiles.json").write_text(json.dumps(profile_data, indent=2) + "\n")
    profiles = {
        (Path(profile["file"]).parent.parent.name, Path(profile["file"]).name): profile
        for profile in profile_data["profiles"]
    }
    metric_paths = [
        str(ROOT / "runs" / entry["sample"] / suffix)
        for entry in runs for suffix in
        ("metrics-final.prom", "profile/metrics-start.txt", "profile/metrics-end.txt")
    ]
    metric_data = json.loads(subprocess.check_output(
        [str(HERE / "analyze_metrics"), *metric_paths], text=True))
    (output / "metrics.json").write_text(json.dumps(metric_data, indent=2) + "\n")
    metrics = {report["file"]: report["samples"] for report in metric_data}
    windows = lines(output / "windows-load.jsonl")
    linux = lines(output / "linux-load.jsonl")
    rows = [summarize_run(entry, profiles, metrics, windows, linux) for entry in runs]
    grouped = {}
    keys = ("p50_ms", "p90_ms", "p99_ms", "controller_cpu_seconds", "controller_alloc_bytes")
    for rate in (2, 8):
        group = {}
        for revision in ("baseline", "candidate"):
            subset = [row for row in rows if row["rate"] == rate and row["revision"] == revision]
            group[revision] = {
                key: {"median": statistics.median(row[key] for row in subset),
                      "min": min(row[key] for row in subset), "max": max(row[key] for row in subset)}
                for key in keys
            }
        group["paired_percent_change"] = {
            key: [
                100 * (
                    next(row[key] for row in rows if row["rate"] == rate and row["pair"] == pair and row["revision"] == "candidate")
                    / next(row[key] for row in rows if row["rate"] == rate and row["pair"] == pair and row["revision"] == "baseline") - 1)
                for pair in (1, 2, 3)
            ] for key in keys
        }
        grouped[str(rate)] = group
    report = {"runs": rows, "by_rate": grouped}
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    scalar_keys = [key for key in rows[0] if key not in ("windows", "linux", "launch_conditions")]
    with (output / "summary.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=scalar_keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(grouped, indent=2))


if __name__ == "__main__":
    main()
