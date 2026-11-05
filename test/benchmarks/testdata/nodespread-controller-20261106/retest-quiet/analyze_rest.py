#!/usr/bin/env python3
"""Offline controller REST-counter analysis; run after all 24 formal runs finish."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import subprocess
from summarize_unprofiled import HERE, ROOT, manifest_errors, read_json, read_metrics, timestamp_ns

FAMILY = "rest_client_requests_total"
DETAIL_SAMPLE = "quiet-plain-p5-baseline"


class AnalysisError(ValueError):
    """Safe diagnostic text containing no metric labels or parser payloads."""


def require(condition, message):
    if not condition:
        raise AnalysisError(message)


def counters(samples):
    result = {}
    for sample in samples:
        if sample["name"] != FAMILY:
            continue
        labels, value = sample["labels"], sample["value"]
        require(all(labels.get(k) for k in ("code", "method")), "Missing REST code/method label")
        require(math.isfinite(value) and value >= 0 and value == int(value), "Invalid REST counter")
        key = tuple(sorted(labels.items()))
        require(key not in result, "Duplicate REST series")
        result[key] = int(value)
    require(result, "Missing rest_client_requests_total; use an updated metrics parser")
    return result


def delta(before, after):
    require(all(key in after and after[key] >= value for key, value in before.items()),
            "REST counter decreased or disappeared")
    return {key: value - before.get(key, 0) for key, value in after.items()}


def aggregate(values):
    grouped = defaultdict(int)
    for labels, value in values.items():
        labels = dict(labels)
        grouped[(labels["code"], labels["method"])] += value
    rows = []
    for (code, method), value in sorted(grouped.items()):
        category = {"409": "http_conflict_409", "429": "throttled_429",
                    "<error>": "transport_error"}.get(code, "other")
        if code.isdigit() and 500 <= int(code) <= 599:
            category = "server_error_5xx"
        rows.append({"code": code, "method": method, "category": category, "requests": value})
    return {"total": sum(values.values()), "by_code_method": rows}


def interval(first, last):
    a, b = first["started_at"], first["ended_at"]
    c, d = last["started_at"], last["ended_at"]
    require(timestamp_ns(a["utc"]) <= timestamp_ns(b["utc"]) <= timestamp_ns(c["utc"])
            <= timestamp_ns(d["utc"]), "Invalid REST scrape UTC order")
    values = [p["monotonic_seconds"] for p in (a, b, c, d)]
    require(all(math.isfinite(v) for v in values) and values == sorted(values),
            "Invalid REST scrape monotonic order")
    return {"start_scrape_utc": [a["utc"], b["utc"]], "end_scrape_utc": [c["utc"], d["utc"]],
            "duration_seconds": ((values[2] - values[0]) + (values[3] - values[1])) / 2,
            "duration_method": "monotonic scrape midpoints"}


def analyze(results, metrics_binary=HERE / "analyze_metrics", root=ROOT):
    manifest = read_json(results / "manifest.json")
    require(not manifest_errors(manifest), "Manifest/schedule or host-monitor validation failed")
    runs = [r for r in manifest["runs"] if not r.get("warmup", False)]
    require(len(runs) == 24 and all(r["status"] == "completed" and r.get("unprofiled") for r in runs),
            "All 24 unprofiled formal runs must complete")
    plans, paths = [], []
    for entry in runs:
        require(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", entry["sample"]), "Unsafe sample name")
        path = root / "runs" / entry["sample"]
        telemetry = read_json(path / "telemetry/run.json")
        require(telemetry["status"] == "complete" and telemetry["exit_code"] == 0
                and not telemetry.get("errors"), "Incomplete telemetry run")
        samples = telemetry["samples"]
        require(len(samples) >= 2 and samples[0]["tag"] == "start" and samples[-1]["tag"] == "end"
                and all(s["tag"] == "periodic" for s in samples[1:-1]), "Invalid telemetry sample tags")
        require(all(s["status"] == "complete" for s in samples), "Failed telemetry sample")
        selected = samples if entry["sample"] == DETAIL_SAMPLE else [samples[0], samples[-1]]
        files = [path / "telemetry" / s["artifact"] for s in selected]
        require(all(Path(s["artifact"]).name == s["artifact"] and s["artifact"] not in (".", "..")
                    for s in selected) and len(set(files)) == len(files), "Unsafe or duplicate artifact")
        for first, last in zip(selected, selected[1:]):
            interval(first, last)
        files.append(path / "metrics-final.prom")
        phases = read_json(path / "workload/summary.json")["phases"]
        require(len(phases) == 1, "Expected one workload phase")
        plans.append((entry, telemetry, selected, files, phases[0]))
        paths.extend(files)
    parsed = read_metrics(paths, binary=metrics_binary)
    output = {"status": "complete", "scope": "Controller REST client only",
              "coverage": "Stress, kubelet and API-server-owned requests were not collected here",
              "interpretation": "HTTP 409 includes optimistic conflicts; internal HTTP results are distinct from final claim outcomes",
              "process_total_scope": "Since controller process start through final scrape, including setup and drain/cleanup preceding that scrape",
              "periodic_interpretation": "Intervals localize REST activity; cold-start timing requires separate workload evidence",
              "runs": []}
    for entry, telemetry, samples, files, phase in plans:
        series = [counters(parsed[str(path.resolve())]) for path in files]
        for before, after in zip(series, series[1:]):
            delta(before, after)
        row = {key: entry[key] for key in ("sample", "revision", "group", "pair", "slot")}
        row.update(actual_created=phase["created"], actual_ready=phase["ready"], actual_failed=phase["failed"],
                   window=interval(samples[0], samples[-1]),
                   traffic_utc=[telemetry["trigger"]["at"]["utc"], telemetry["boundary"]["at"]["utc"]],
                   window_requests=aggregate(delta(series[0], series[-2])),
                   process_total=aggregate(series[-1]))
        if entry["sample"] == DETAIL_SAMPLE:
            periodic = [i for i, s in enumerate(samples) if s["tag"] == "periodic"]
            require(len(periodic) >= 2, "Need at least two periodic samples for detailed analysis")
            row["periodic_intervals"] = [
                {"window": interval(samples[i], samples[j]), "requests": aggregate(delta(series[i], series[j]))}
                for i, j in zip(periodic, periodic[1:])]
        output["runs"].append(row)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="?", type=Path, default=HERE / "results-unprofiled")
    parser.add_argument("--metrics-binary", type=Path, default=HERE / "analyze_metrics")
    args = parser.parse_args(argv)
    try:
        output = analyze(args.results.resolve(), args.metrics_binary.resolve())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, AnalysisError) else type(exc).__name__
        print(json.dumps({"status": "invalid", "error": reason}))
        return 1
    (args.results / "rest-client-requests.json").write_text(json.dumps(output, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"status": "complete", "runs": len(output["runs"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
