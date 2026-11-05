#!/usr/bin/env python3
"""Export an allowlisted, credential-free controller comparison evidence tree."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import shutil


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WINDOWS_FIELDS = ("utc", "cpu_percent", "available_mb", "pages_input_per_sec", "processes")
LINUX_FIELDS = (
    "utc", "cpu_active_percent", "cpu_iowait_percent", "cpu_steal_percent",
    "loadavg", "available_kb", "swap_used_kb", "pswpin", "pswpout", "pressure",
)
CLAIM_FIELDS = (
    "name", "namespace", "phase", "phaseNumber", "boundSandbox", "createCalled",
    "createReturned", "sandboxReady", "deleteCalled", "sandboxDeleted",
    "serverSandboxCreated", "serverSandboxReady", "createAckMs", "sandboxReadyMs",
)


def load(path):
    return json.loads(path.read_text())


def portable(value, root=ROOT):
    if isinstance(value, str):
        prefix = str(root) + "/"
        return value[len(prefix):] if value.startswith(prefix) else value
    if isinstance(value, list):
        return [portable(item, root) for item in value]
    if isinstance(value, dict):
        return {portable(key, root): portable(item, root) for key, item in value.items()}
    return value


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(portable(value), indent=2) + "\n")


def compress(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as out:
            shutil.copyfileobj(src, out)


def filter_jsonl(source, destination, fields):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as out:
            for line in source.read_text().splitlines():
                record = json.loads(line)
                selected = {key: record[key] for key in fields if key in record}
                if "processes" in selected:
                    selected["processes"] = [
                        {"cpu_one_core_percent": process["cpu_one_core_percent"]}
                        for process in selected["processes"] if "cpu_one_core_percent" in process
                    ]
                if record.get("error"):
                    selected["error_present"] = True
                out.write((json.dumps(selected) + "\n").encode())


def workload_summary(value):
    result = dict(value)
    result["config"] = dict(value["config"])
    result["config"]["outputDir"] = "workload"
    if "apfVerification" in result:
        result["apfVerification"] = {
            key: item for key, item in result["apfVerification"].items()
            if key in ("flowSchema", "priorityLevel", "exempt")
        }
    return result


def export_run(entry, output):
    sample = entry["sample"]
    if not sample or Path(sample).name != sample or sample in (".", ".."):
        raise ValueError("Invalid sample directory")
    source = ROOT / "runs" / sample
    destination = output / "runs" / sample
    config = load(source / "config.json")
    write_json(destination / "config.json", config)
    hashes = {}
    for line in (source / "binaries.sha256").read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        hashes[Path(name).name] = digest
    write_json(destination / "binaries.json", hashes)
    (destination / "binaries.sha256").write_text("".join(
        f"{digest}  {name}\n" for name, digest in hashes.items()))
    write_json(destination / "workload" / "summary.json",
               workload_summary(load(source / "workload" / "summary.json")))
    filter_jsonl(source / "workload" / "sandboxes.jsonl",
                 destination / "workload" / "sandboxes.jsonl.gz", CLAIM_FIELDS)
    compress(source / "metrics-final.prom", destination / "metrics-final.prom.gz")
    observation = "telemetry" if entry.get("unprofiled") else "profile"
    folder = source / observation
    write_json(destination / observation / "run.json", load(folder / "run.json"))
    for path in sorted(folder.glob("*.prom")) + sorted(folder.glob("metrics-*.txt")):
        compress(path, destination / observation / (path.name + ".gz"))
    for name in ("cpu.pprof", "mutex.pprof", "allocs.pprof"):
        path = folder / name
        if path.is_file():
            shutil.copyfile(path, destination / observation / name)


def export_results(source, output):
    manifest = load(source / "manifest.json")
    if manifest["status"] != "completed" or manifest["windows_monitor_exit"] != 0:
        raise ValueError("Export requires a completed, monitored suite")
    destination = output / "retest-quiet" / source.name
    write_json(destination / "manifest.json", manifest)
    names = (
        "summary.json", "summary-unprofiled.json", "arrival-audit.json",
        "profiles.json", "metrics.json", "inventory-diagnostic.json",
        "rest-client-requests.json",
    )
    for name in names:
        path = source / name
        if path.is_file():
            write_json(destination / name, load(path))
    for name in ("summary.csv", "summary-unprofiled.csv"):
        path = source / name
        if path.is_file():
            shutil.copyfile(path, destination / name)
    for path in source.glob("schedule-*.json"):
        write_json(destination / path.name, load(path))
    filter_jsonl(source / "windows-load.jsonl",
                 destination / "windows-load.jsonl.gz", WINDOWS_FIELDS)
    filter_jsonl(source / "linux-load.jsonl",
                 destination / "linux-load.jsonl.gz", LINUX_FIELDS)
    for entry in manifest["runs"]:
        if entry["status"] != "completed":
            raise ValueError(f"Incomplete run: {entry['sample']}")
        export_run(entry, output)


def checksum_tree(output):
    records = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            records.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(output)}\n")
    (output / "SHA256SUMS").write_text("".join(records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir()
    for name in ("results", "results-unprofiled"):
        export_results(HERE / name, output)
    root_files = (
        ".gitattributes", "kind.yaml", "run_sample.sh", "profile_workload.py",
        "test_profile_workload.py", "analyze_profiles.go", "sustained_seed0.go", "README.md",
    )
    for name in root_files:
        shutil.copyfile(ROOT / name, output / name)
    harness_files = (
        "repeat_suite.py", "run_unprofiled.py", "summarize.py", "summarize_unprofiled.py",
        "analyze_metrics.go", "analyze_metrics_test.go", "windows_load.ps1",
        "test_repeat_suite.py", "test_run_unprofiled.py", "test_summarize.py",
        "test_summarize_unprofiled.py", "validate_pacing.py", "test_validate_pacing.py",
        "emit_schedule.go", "PROTOCOL.md", "UNPROFILED_PROTOCOL.md", "export_evidence.py",
        "test_export_evidence.py", "diagnose_inventory.py", "test_diagnose_inventory.py",
        "INVENTORY_DIAGNOSTIC.md", "analyze_rest.py", "test_analyze_rest.py", "README.md",
        "reproduce_evidence.py", "test_reproduce_evidence.py",
        "ci_round.py", "test_ci_round.py", "CI_PROTOCOL.md",
    )
    for name in harness_files:
        shutil.copyfile(HERE / name, output / "retest-quiet" / name)
    write_json(output / "EXPORT.json", {
        "suites": ["results", "results-unprofiled"],
        "raw_data_encoding": "gzip for Prometheus text, claim records, and host telemetry",
        "host_fields": {"windows": WINDOWS_FIELDS, "linux": LINUX_FIELDS},
        "claim_fields": CLAIM_FIELDS,
        "included": [
            "registered protocols and harness source", "source and binary fingerprints",
            "complete run summaries and lifecycle timing records",
            "raw controller profiles and Prometheus samples", "numeric host telemetry",
            "allowlisted inventory observations and REST code/method aggregates",
        ],
        "local_only": [
            "kubeconfig and authentication material", "host process identities",
            "full Kubernetes resource inventories", "controller and watch logs",
        ],
    })
    checksum_tree(output)
    print(output)


if __name__ == "__main__":
    main()
