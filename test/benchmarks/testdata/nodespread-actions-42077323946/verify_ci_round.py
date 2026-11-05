#!/usr/bin/env python3
"""Copy and verify one downloaded CI round using trusted local analysis tools."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat

import ci_round as ci
from reproduce_evidence import checked_path, differences, read_json


ENTRY_FIELDS = ("sample", "kind", "revision", "rate", "seconds", "pair", "slot")
COMMITS = {"baseline": ci.BASELINE_COMMIT, "candidate": ci.CANDIDATE_COMMIT,
           "stress": ci.CANDIDATE_COMMIT}
ROLES = {"baseline", "candidate", "stress", "metrics_parser", "schedule_emitter"}
RESERVED = {"verification.json", "summary-recomputed.json"}


class VerificationError(ValueError):
    """Controlled diagnostic text; artifact contents are never executed."""


def require(condition, message):
    if not condition:
        raise VerificationError(message)


def tree_hashes(root):
    hashes = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            require(stat.S_ISDIR(mode) or stat.S_ISREG(mode), "Source contains a symlink or special file")
            if stat.S_ISREG(mode):
                hashes[path.relative_to(root).as_posix()] = ci.sha256(path)
    return dict(sorted(hashes.items()))


def tree_digest(hashes):
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def compare(report, label, expected, actual):
    locations = differences(expected, actual)
    report["comparisons"].append({"scope": label, "status": "different" if locations else "match",
                                  "difference_count": len(locations), "difference_paths": locations[:100]})
    return not locations


def projection(value, keys):
    require(isinstance(value, dict) and all(key in value for key in keys), "Missing required report fields")
    return {key: value[key] for key in keys}


def calibration_entry(rate):
    return {"sample": f"ci-cal-r{rate}", "kind": "calibration", "revision": "baseline",
            "rate": rate, "seconds": 30, "pair": None, "slot": rate}


def validate_registration(config, manifest, published, report):
    compare(report, "registration/config-vs-manifest", config, manifest["registration"])
    compare(report, "protocol/config", ci.PROTOCOL, config["protocol"])
    compare(report, "protocol/manifest", ci.PROTOCOL, manifest["protocol"])
    compare(report, "protocol/summary", ci.PROTOCOL, published["protocol"])
    compare(report, "registration/source_commits", COMMITS, config["source_commits"])
    compare(report, "registration/controller_args", ci.FLAGS, config["controller_args"])
    require(config.get("schedule_emitter_supplied") is True, "Round must register a schedule emitter")
    hashes = config["binary_sha256"]
    require(set(hashes) == ROLES and all(isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v)
                                       for v in hashes.values()), "Invalid registered binary hashes")
    cpus = config["available_cpus"]
    require(isinstance(cpus, list) and bool(cpus)
            and all(type(cpu) is int and cpu >= 0 for cpu in cpus)
            and cpus == sorted(set(cpus)), "Invalid registered CPU affinity")
    compare(report, "registration/controller_cpus", cpus[:4], config["controller_cpus"])
    require(manifest["status"] in ("complete", "failed", "running"), "Unknown round status")
    compare(report, "round/status", manifest["status"], published["status"])
    compare(report, "round/errors", manifest.get("errors", []), published.get("errors", []))
    compare(report, "round/selected_rate", manifest.get("selected_rate"), published.get("selected_rate"))
    if manifest["status"] == "complete":
        require(not manifest.get("errors"), "Completed manifest contains errors")
        nodes = manifest["nodes"]
        require(len(nodes) == 3 and all(node.get("ready") is True for node in nodes)
                and sorted(node.get("role", "") for node in nodes) == ["control-plane", "worker", "worker"]
                and len({node["name"] for node in nodes}) == 3, "Invalid registered kind topology")
    report["registered_binary_sha256"] = hashes
    report["registered_source_commits"] = config["source_commits"]


def validate_run_config(run, entry, config, report, completed):
    declared = read_json(run / "config.json")
    fixed = {**entry, "pool": 128, "seed": [0, 0], "profile_enabled": False,
             "gomaxprocs": 4, "stress_gomaxprocs": None, "controller_args": ci.FLAGS,
             "controller_cpus": config["controller_cpus"], "commit": COMMITS[entry["revision"]],
             "binary_sha256": config["binary_sha256"], "metrics_interval_seconds": 5}
    compare(report, entry["sample"] + "/config", fixed, projection(declared, fixed))
    if not completed:
        return
    expected_command = ci.workload.workload_args(
        "stress", Path("/RUN"), entry["sample"], entry["rate"], entry["seconds"], 128)
    expected_command = [part.replace("/RUN", ".") for part in expected_command]
    compare(report, entry["sample"] + "/command", expected_command, read_json(run / "command.json"))
    # The original outputDir and any runner paths are provenance, not local paths.
    workload_config = read_json(run / "workload/summary.json")["config"]
    expected_config = {
        "namespace": "np2179-" + entry["sample"], "image": "node:22-alpine", "cleanup": True,
        "phases": ["claims-warm-sustained"], "sustainedRate": entry["rate"],
        "sustainedSeconds": entry["seconds"], "sustainedNamespaces": 1,
        "sustainedPoolHeadroomNanos": (128 // entry["rate"]) * 1_000_000_000,
        "claimDwellNanos": 1_000_000_000, "clientConnections": 4,
        "profileController": False, "profileAPIServer": False, "collectMetrics": False,
        "timeoutNanos": 480_000_000_000, "perSandboxTimeoutNanos": 120_000_000_000,
    }
    compare(report, entry["sample"] + "/workload-config", expected_config,
            projection(workload_config, expected_config))


def recompute(output, metrics_parser, emitter, report):
    manifest = read_json(output / "manifest.json")
    published = read_json(output / "summary.json")
    config = read_json(output / "config.json")
    report["artifact_status"] = manifest["status"]
    validate_registration(config, manifest, published, report)
    rows = manifest["runs"]
    require(isinstance(rows, list) and 1 <= len(rows) <= 7, "Expected one to seven attempted runs")
    require(len(published["runs"]) == len(rows), "Summary run count differs from manifest")
    names = [row["sample"] for row in rows]
    require(len(set(names)) == len(names) and all(
        isinstance(name, str) and re.fullmatch(
            r"ci-(?:cal-r[248]|formal-[1-4]-(?:baseline|candidate))", name) for name in names),
        "Invalid or duplicate sample names")
    require(all(row["status"] in ("complete", "failed", "running") for row in rows), "Unknown run status")
    require(all(row["status"] == "complete" for row in rows[:-1]), "Run continued after operational failure")
    if manifest["status"] == "complete":
        require(len(rows) == 7 and all(row["status"] == "complete" for row in rows),
                "Completed round must contain three calibrations and four formal runs")
    actual_folders = {p.name for p in (output / "runs").iterdir()}
    require(actual_folders == set(names), "Run directories differ from manifest")
    rebuilt, passed_rates = [], []
    low_failed = False
    for index, original in enumerate(rows):
        if index < 3:
            entry = calibration_entry((2, 4, 8)[index])
        else:
            require(bool(passed_rates), "Formal runs require a passing calibration")
            entry = ci.formal_plan(max(passed_rates))[index - 3]
        compare(report, f"schedule/{index}", entry, projection(original, ENTRY_FIELDS))
        # Even a malformed schedule cannot redirect reads to another run.
        require(original["sample"] == entry["sample"], "Run order or sample identity differs from registration")
        if low_failed:
            report["errors"].append("Round continued after lowest-rate calibration failed")
        run = output / "runs" / entry["sample"]
        completed = original["status"] == "complete"
        validate_run_config(run, entry, config, report, completed)
        compare(report, entry["sample"] + "/manifest-vs-summary", original, published["runs"][index])
        if not completed:
            report["skipped_runs"].append({"sample": entry["sample"], "status": original["status"],
                                           "reason": "No completed measurement to recompute"})
            rebuilt.append({**entry, "status": original["status"]})
            continue
        per_run = read_json(run / "summary.json")
        compare(report, entry["sample"] + "/manifest-vs-per-run", original, per_run)
        require(original.get("cleanup") == "complete" and not original.get("errors")
                and not original.get("error_present"), "Completed run contains failure or missing cleanup status")
        references = {name: read_json(run / name) for name in ("schedule.json", "arrival-audit.json")}
        # No copied derived statistic is an input to the trusted recomputation.
        for name in ("summary.json", "schedule.json", "arrival-audit.json"):
            (run / name).unlink()
        try:
            actual = ci.summarize_run(run, entry, metrics_parser, output / "linux-load.jsonl", emitter)
            actual["cleanup"] = original["cleanup"]
            if index < 3:
                actual["calibration"] = ci.calibration_check(actual)
                if actual["calibration"]["passed"]:
                    passed_rates.append(entry["rate"])
                elif index == 0:
                    low_failed = True
            for source, reference in (("manifest", original), ("per-run", per_run),
                                      ("summary", published["runs"][index])):
                compare(report, entry["sample"] + "/" + source + "-recomputed",
                        projection(reference, actual), actual)
            for name, reference in references.items():
                compare(report, entry["sample"] + "/" + name, reference, read_json(run / name))
            ci.save(run / "summary.json", actual)
            rebuilt.append(actual)
            report["recomputed_runs"].append(entry["sample"])
        except Exception as error:
            report["errors"].append(entry["sample"] + ": " + safe_error(error))
            ci.save(run / "summary.json", {**entry, "status": "verification_failed",
                                           "error": safe_error(error)})
            rebuilt.append({**entry, "status": "verification_failed"})
    selected = max(passed_rates) if passed_rates else None
    # A failed calibration run is persisted before its selected_rate assignment.
    if manifest["status"] == "complete" or len(rows) > 3:
        compare(report, "selection/highest-passing-rate", selected, manifest.get("selected_rate"))
    if len(rows) > 3:
        compare(report, "schedule/formal", ci.formal_plan(selected), manifest.get("formal_schedule"))
    complete = manifest["status"] == "complete" and all(row["status"] == "complete" for row in rebuilt)
    pairs = ci.paired_summary(rebuilt) if complete else []
    primary = ci.primary_comparison(pairs)
    compare(report, "summary/pairs", published["pairs"], pairs)
    compare(report, "summary/primary_comparison", published["primary_comparison"], primary)
    ci.save(output / "summary-recomputed.json", {
        "status": "complete" if complete else "incomplete",
        "selected_rate": selected, "runs": rebuilt, "pairs": pairs, "primary_comparison": primary,
    })
    if manifest["status"] != "complete":
        report["errors"].append("Source round is " + manifest["status"] + "; completed subset was checked")


def safe_error(error):
    if isinstance(error, (VerificationError, ci.RoundError)):
        return str(error)
    return type(error).__name__


def verify(source, output, metrics_parser, emitter):
    report = {
        "status": "failed", "artifact_status": None, "comparisons": [], "errors": [],
        "recomputed_runs": [], "skipped_runs": [],
        "policy": {
            "code": "Trusted modules imported from the verifier directory; no artifact code or provenance paths executed",
            "statistics": "Raw exported durations and Prometheus samples recomputed, including calibration and two-pair primary comparison",
            "floats": {"relative_tolerance": 1e-12, "absolute_tolerance": 1e-9},
            "cleanup": "Recorded completion status checked; cluster cleanup is not replayed",
            "binary_provenance": "Registered hashes checked for consistency; local helper hashes recorded separately",
        },
    }
    destination = None
    source_hashes = None
    try:
        source, output = checked_path(source), checked_path(output)
        require(source.is_dir(), "Source must be a downloaded round directory")
        require(source != output and source not in output.parents and output not in source.parents,
                "Source and output must be separate trees")
        require(not output.exists() or (output.is_dir() and not any(output.iterdir())),
                "Output must be new or empty")
        require(output.parent.is_dir(), "Output parent must exist")
        binaries = {}
        for role, path in (("metrics_parser", metrics_parser), ("schedule_emitter", emitter)):
            path = checked_path(path)
            require(path.is_file() and os.access(path, os.X_OK), "Local executable missing: " + role)
            require(source not in path.parents and output not in path.parents,
                    "Local helpers must be outside artifact trees")
            binaries[role] = path
        report["local_helper_sha256"] = {role: ci.sha256(path) for role, path in binaries.items()}
        root = Path(ci.__file__).resolve().parent
        report["local_code_sha256"] = {"verify_ci_round.py": ci.sha256(Path(__file__).resolve())}
        report["local_code_sha256"].update({name: ci.sha256(root / name) for name in
                                      ("ci_round.py", "summarize.py",
                                       "summarize_unprofiled.py", "validate_pacing.py",
                                       "run_unprofiled.py", "repeat_suite.py", "reproduce_evidence.py")})
        source_hashes = tree_hashes(source)
        require(not RESERVED.intersection(source_hashes), "Source contains reserved verification outputs")
        report["source_files_sha256"] = source_hashes
        report["source_tree_sha256"] = tree_digest(source_hashes)
        destination = output
        shutil.copytree(source, output, dirs_exist_ok=output.exists())
        require(tree_hashes(output) == source_hashes, "Copied bytes differ from source")
        report["copy_verified"] = True
        recompute(output, binaries["metrics_parser"], binaries["schedule_emitter"], report)
        report["recomputed_summary_sha256"] = ci.sha256(output / "summary-recomputed.json")
    except (Exception, KeyboardInterrupt) as error:
        report["errors"].append(safe_error(error))
    finally:
        if source_hashes is not None:
            try:
                report["source_unchanged"] = tree_hashes(source) == source_hashes
                if not report["source_unchanged"]:
                    report["errors"].append("Source tree changed during verification")
            except Exception as error:
                report["source_unchanged"] = False
                report["errors"].append(safe_error(error))
        report["difference_count"] = sum(c["difference_count"] for c in report["comparisons"])
        if (not report["errors"] and not report["difference_count"]
                and report["artifact_status"] == "complete" and len(report["recomputed_runs"]) == 7):
            report["status"] = "verified"
        if destination is not None and destination.is_dir():
            ci.save(destination / "verification.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "output", "metrics-parser", "schedule-emitter"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args(argv)
    report = verify(args.source, args.output, args.metrics_parser, args.schedule_emitter)
    print(json.dumps(report, indent=2, allow_nan=False))
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
