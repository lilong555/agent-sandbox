#!/usr/bin/env python3
"""Audit actual arrival pacing against the unchanged PCG(0,0) schedule."""

import argparse
import json
from pathlib import Path
import re
import subprocess

from summarize import lines, load, quantile
from summarize_unprofiled import timestamp_ns


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def audit(records, offsets):
    arrivals = {}
    for record in records:
        if record.get("phase") != "claims-warm-sustained":
            continue
        match = re.fullmatch(r"p1-sclaim-(\d+)", record["name"])
        if not match or not record.get("createCalled"):
            raise ValueError("Missing arrival identity or creation timestamp")
        index = int(match[1])
        if index in arrivals:
            raise ValueError("Duplicate arrival identity")
        arrivals[index] = timestamp_ns(record["createCalled"])
    if not offsets or set(arrivals) != set(range(len(offsets))):
        raise ValueError("Actual arrival identities differ from the seeded schedule")
    origin = arrivals[0]
    errors = [
        ((arrivals[index] - origin) - (offset - offsets[0])) / 1e6
        for index, offset in enumerate(offsets)
    ]
    return {
        "expected_arrivals": len(offsets),
        "actual_arrivals": len(arrivals),
        "expected_first_to_last_seconds": (offsets[-1] - offsets[0]) / 1e9,
        "actual_first_to_last_seconds": (arrivals[len(offsets) - 1] - origin) / 1e9,
        "first_arrival_relative_offset_min_ms": min(errors),
        "first_arrival_relative_offset_p01_ms": quantile(errors, .01),
        "first_arrival_relative_offset_p50_ms": quantile(errors, .5),
        "first_arrival_relative_offset_p99_ms": quantile(errors, .99),
        "first_arrival_relative_offset_max_ms": max(errors),
        "offset_envelope_ms": max(errors) - min(errors),
        "adjacent_out_of_order_arrivals": sum(
            arrivals[index] < arrivals[index - 1] for index in range(1, len(arrivals))
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--emitter", type=Path, default=ROOT / "tools" / "emit_schedule")
    args = parser.parse_args()
    output = args.results.resolve()
    manifest = load(output / "manifest.json")
    if manifest["status"] != "completed":
        raise ValueError("The full suite must complete before the pacing audit")
    schedules = {}
    rows = []
    for entry in manifest["runs"]:
        if entry["status"] != "completed":
            raise ValueError(f"Incomplete sample: {entry['sample']}")
        path = ROOT / "runs" / entry["sample"]
        config = load(path / "config.json")
        key = config["claims_per_second"], config["seconds"]
        if key not in schedules:
            data = json.loads(subprocess.check_output([
                str(args.emitter), "-rate", str(key[0]), "-seconds", str(key[1]),
            ], text=True))
            (output / f"schedule-r{key[0]}-s{key[1]}.json").write_text(
                json.dumps(data, indent=2) + "\n")
            schedules[key] = data["offsets_ns"]
        rows.append({
            "sample": entry["sample"], "group": entry.get("group", "profiled"),
            "warmup": entry["warmup"],
            **audit(lines(path / "workload" / "sandboxes.jsonl"), schedules[key]),
        })
    report = {
        "status": "complete",
        "method": (
            "Compare creation timestamps by scheduled claim ID with the exact "
            "Go PCG(0,0) inter-arrival offsets, aligning each run's first arrival. "
            "The envelope measures relative pacing jitter; the workload does "
            "not record an exact timestamp for the schedule origin."
        ),
        "runs": rows,
    }
    (output / "arrival-audit.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "runs": len(rows),
        "formal_claims": sum(row["actual_arrivals"] for row in rows if not row["warmup"]),
        "maximum_offset_envelope_ms": max(row["offset_envelope_ms"] for row in rows),
    }, indent=2))


if __name__ == "__main__":
    main()
