#!/usr/bin/env python3
"""Verify and recompute an exported evidence tree in a fresh directory, offline.

Example:
  python3 -B reproduce_evidence.py --source-repo /path/to/candidate \
      --output /path/to/new-reproduction --go /path/to/go

The evidence root defaults to this script's parent directory's parent.
Go and the dependencies in the candidate checkout's go.mod must be cached.
SHA256SUMS checks integrity; use an evidence package from a trusted publisher,
as its Python and Go analysis sources will be executed.
"""

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


Q = "retest-quiet"
REQUIRED_REPORTS = (
    f"{Q}/results/summary.json",
    f"{Q}/results/profiles.json",
    f"{Q}/results/metrics.json",
    f"{Q}/results-unprofiled/summary-unprofiled.json",
    f"{Q}/results-unprofiled/rest-client-requests.json",
)
INVENTORY = f"{Q}/results-unprofiled/inventory-diagnostic.json"
HELPERS = (
    (f"{Q}/analyze_metrics.go", f"{Q}/analyze_metrics"),
    ("analyze_profiles.go", "tools/analyze_profiles"),
    (f"{Q}/emit_schedule.go", "tools/emit_schedule"),
)


def safe_relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or ":" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError("Unsafe evidence path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("Unsafe evidence path")
    return path


def checked_path(path):
    path = Path(path).expanduser().absolute()
    if ".." in path.parts:
        raise ValueError("Use paths without '..'")
    for component in (*reversed(path.parents), path):
        if component.is_symlink():
            raise ValueError(f"Symbolic link rejected: {component}")
    return path


def digest_file(path):
    checked_path(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_tree(root):
    """Require an exact manifest of all regular files, before copying any data."""
    root = checked_path(root)
    checksum = root / "SHA256SUMS"
    digest_file(checksum)
    entries = {}
    for line in checksum.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](.+)", line)
        if not match:
            raise ValueError("Malformed SHA256SUMS record")
        digest, name = match.groups()
        safe_relative(name)
        if name in entries or name == "SHA256SUMS":
            raise ValueError("Duplicate or self-referential checksum record")
        entries[name] = digest.lower()
    if not entries:
        raise ValueError("Empty SHA256SUMS")
    observed = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Symbolic link or special file rejected: {path}")
            relative = path.relative_to(root).as_posix()
            safe_relative(relative)
            if stat.S_ISREG(mode) and relative != "SHA256SUMS":
                observed.add(relative)
    if observed != entries.keys():
        raise ValueError("SHA256SUMS file coverage differs from the evidence tree")
    for name, digest in entries.items():
        if digest_file(root / name) != digest:
            raise ValueError(f"SHA256 mismatch: {name}")
    return entries


def expanded_names(entries):
    targets = {}
    reserved = {"SHA256SUMS", "verify.json", "reproduce-logs", *(dst for _, dst in HELPERS)}
    for name in entries:
        target = name[:-3] if name.endswith(".gz") else name
        safe_relative(target)
        if target in targets:
            raise ValueError(f"Decompression destination collision: {target}")
        if any(target == path or target.startswith(path + "/") or path.startswith(target + "/")
               for path in reserved):
            raise ValueError(f"Reserved reproduction path: {target}")
        targets[target] = name
    for target in targets:
        if any(parent.as_posix() in targets for parent in PurePosixPath(target).parents):
            raise ValueError(f"File/directory destination collision: {target}")
    return targets


def rebuild(root, output, entries):
    for target, name in expanded_names(entries).items():
        source = checked_path(root / name)
        destination = output / target
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Verify the bytes actually copied, then decompress exactly one layer.
        digest = hashlib.sha256()
        with source.open("rb") as src, tempfile.TemporaryFile(dir=output) as staged:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(block)
                staged.write(block)
            if digest.hexdigest() != entries[name]:
                raise ValueError(f"Evidence changed during copy: {name}")
            staged.seek(0)
            with destination.open("xb") as dst:
                if name.endswith(".gz"):
                    with gzip.GzipFile(fileobj=staged, mode="rb") as decoded:
                        shutil.copyfileobj(decoded, dst)
                else:
                    shutil.copyfileobj(staged, dst)


def read_json(path):
    def invalid(value):
        raise ValueError(f"Non-finite JSON number: {value}")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid,
                      object_pairs_hook=unique)


def normalize(value, roots):
    if isinstance(value, str):
        for root in roots:
            if value == str(root):
                return "."
            prefix = str(root) + "/"
            if value.startswith(prefix):
                return value[len(prefix):]
        return value
    if isinstance(value, list):
        return [normalize(item, roots) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            normalized = normalize(key, roots)
            if normalized in result:
                raise ValueError("Path normalization creates duplicate JSON keys")
            result[normalized] = normalize(item, roots)
        return result
    return value


def differences(expected, actual, path="$"):
    """Return differing JSON locations without copying sensitive field values."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        result = []
        for key in sorted(expected.keys() | actual.keys()):
            location = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in expected or key not in actual:
                result.append(location)
            else:
                result.extend(differences(expected[key], actual[key], location))
        return result
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [path + "/length"]
        return [diff for i, (left, right) in enumerate(zip(expected, actual))
                for diff in differences(left, right, f"{path}/{i}")]
    if type(expected) is float and type(actual) is float:
        equal = math.isclose(expected, actual, rel_tol=1e-12, abs_tol=1e-9)
    else:
        equal = type(expected) is type(actual) and expected == actual
    return [] if equal else [path]


def run_command(argv, cwd, env, output, report, timeout):
    index = len(report["commands"])
    log = output / "reproduce-logs" / f"{index:02d}.log"
    log.parent.mkdir(exist_ok=True)
    record = {"argv": normalize(argv, [output]), "cwd": str(cwd),
              "log": log.relative_to(output).as_posix(), "status": "running"}
    report["commands"].append(record)
    with log.open("xb") as stream:
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=stream,
                                       stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            record["status"] = "failed_to_start"
            raise
        try:
            code = process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            # Analysis scripts can spawn helpers: terminate the entire owned group.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            record["status"] = "interrupted_or_timed_out"
            raise
    record.update(exit_code=code, status="complete" if code == 0 else "failed")
    if code:
        raise ValueError(f"Command {index} failed with exit code {code}; see {record['log']}")


def recompute(output, repo, go, report, timeout):
    references = {name: read_json(output / name) for name in REQUIRED_REPORTS}
    inventory = read_json(output / INVENTORY)
    optional = []
    for suite in ("results", "results-unprofiled"):
        folder = output / Q / suite
        manifest = read_json(folder / "manifest.json")
        if (manifest.get("status") != "completed"
                or manifest.get("windows_monitor_exit") != 0
                or not manifest.get("runs")
                or any(entry.get("status") != "completed" for entry in manifest["runs"])):
            raise ValueError(f"Completed monitored suite required: {suite}")
        for entry in manifest["runs"]:
            sample = safe_relative(entry["sample"])
            if len(sample.parts) != 1:
                raise ValueError("Sample must be a directory name")
        optional.append(f"{Q}/{suite}/arrival-audit.json")
        optional.extend(path.relative_to(output).as_posix()
                        for path in sorted(folder.glob("schedule-*.json")))
    for name in optional:
        if (output / name).is_file():
            references[name] = read_json(output / name)

    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", GOENV="off", GOFLAGS="",
               GOWORK="off", GOPROXY="off", GOSUMDB="off", GOTOOLCHAIN="local",
               GONOPROXY="none", GOPRIVATE="", GONOSUMDB="none")
    for key in ("GOOS", "GOARCH"):
        env.pop(key, None)
    expected_commits = set()
    for config_path in (output / "runs").glob("*/config.json"):
        config = read_json(config_path)
        if config.get("revision") == "candidate":
            expected_commits.add(config.get("commit"))
    if (len(expected_commits) != 1
            or not re.fullmatch(r"[0-9a-f]{40,64}", next(iter(expected_commits)) or "")):
        raise ValueError("Evidence must identify one candidate commit")
    run_command(["git", "rev-parse", "--show-toplevel"], repo, env, output, report, timeout)
    toplevel = (output / report["commands"][-1]["log"]).read_text().rstrip("\r\n")
    if toplevel != str(repo):
        raise ValueError("--source-repo must be the candidate Git checkout top-level directory")
    with (repo / "go.mod").open(encoding="utf-8") as module:
        if module.readline().strip() != "module sigs.k8s.io/agent-sandbox":
            raise ValueError("Source go.mod must declare the sigs.k8s.io/agent-sandbox root module")
    run_command(["git", "rev-parse", "HEAD"], repo, env, output, report, timeout)
    commit = (output / report["commands"][-1]["log"]).read_text().strip()
    if commit not in expected_commits:
        raise ValueError("Source checkout HEAD differs from the evidence candidate commit")
    run_command(["git", "diff", "--exit-code", "--no-ext-diff", "--no-textconv",
                 "HEAD", "--", "go.mod", "go.sum"], repo, env, output, report, timeout)
    report["source"] = {"candidate_commit": commit, "go_mod_sha256": digest_file(repo / "go.mod")}
    for source, target in HELPERS:
        binary = output / target
        binary.parent.mkdir(parents=True, exist_ok=True)
        run_command([go, "build", "-mod=readonly", "-o", str(binary), str(output / source)],
                    repo, env, output, report, timeout)
    # Remove only copied derived outputs so successful commands must regenerate them.
    for name in references:
        (output / name).unlink()
    commands = (
        ("summarize.py", "results"),
        ("summarize_unprofiled.py", "results-unprofiled"),
        ("analyze_rest.py", "results-unprofiled"),
        ("diagnose_inventory.py", "--verify-public",
         "results-unprofiled/inventory-diagnostic.json"),
        ("validate_pacing.py", "results-unprofiled"),
        ("validate_pacing.py", "results"),
    )
    for command in commands:
        run_command([sys.executable, "-B", *command], output / Q, env, output, report, timeout)
        if command[0] == "diagnose_inventory.py":
            verification = read_json(output / report["commands"][-1]["log"])
            if verification != inventory["verification"]:
                raise ValueError("Public inventory verification differs from the published recount")
            report["inventory"] = {"status": "verified_public", "recount": verification}
    for name in sorted(references):
        expected = normalize(references[name], [output, Path(report["evidence_root"])])
        actual = normalize(read_json(output / name), [output, Path(report["evidence_root"])])
        mismatches = differences(expected, actual)
        report["comparisons"].append({
            "file": name, "status": "different" if mismatches else "match",
            "difference_count": len(mismatches), "difference_paths": mismatches[:50],
        })
    for name in optional:
        if name not in references:
            read_json(output / name)
            report["comparisons"].append({"file": name, "status": "generated_without_reference"})
    if any(row["status"] == "different" for row in report["comparisons"]):
        raise ValueError("Recomputed statistics differ from the published reports")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-root", type=Path, default=Path(__file__).absolute().parent.parent)
    parser.add_argument("--output", type=Path, required=True, help="New, nonexistent directory")
    parser.add_argument("--source-repo", type=Path, required=True,
                        help="Matching candidate Git checkout root (not a submodule directory)")
    parser.add_argument("--go", default="go", help="Installed Go executable (no toolchain downloads)")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Per-command timeout")
    args = parser.parse_args(argv)
    report = {
        "status": "failed", "commands": [], "comparisons": [], "errors": [],
        "comparison_policy": {
            "paths": "Strip evidence-root and reproduction-root prefixes recursively, including keys",
            "ignored_fields": [],
            "integers": "exact", "float_relative_tolerance": 1e-12, "float_absolute_tolerance": 1e-9,
            "csv": "Regenerated from JSON statistics; JSON reports are compared",
            "inventory": "Public selected-event recount only",
            "missing_pacing_reference": "Reported as generated_without_reference",
        },
    }
    destination = None
    try:
        root, repo, output = map(checked_path, (args.evidence_root, args.source_repo, args.output))
        if args.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")
        if output.exists() or root == output or root in output.parents or repo in output.parents:
            raise ValueError("Output must be new and outside evidence and source checkout trees")
        if not root.is_dir() or not repo.is_dir():
            raise ValueError("Evidence root and source checkout must be directories")
        digest_file(repo / "go.mod")
        if not output.parent.is_dir():
            raise ValueError("Output parent directory must already exist")
        report["evidence_root"] = str(root)
        entries = verify_tree(root)
        targets = expanded_names(entries)
        report["checksums"] = {"status": "verified", "files": len(entries),
                               "scope": "Original compressed evidence tree",
                               "manifest_sha256": digest_file(root / "SHA256SUMS")}
        output.mkdir()
        destination = output
        rebuild(root, output, entries)
        report["reconstructed_files"] = len(targets)
        go = str(Path(args.go).expanduser().absolute()) if "/" in args.go else args.go
        recompute(output, repo, go, report, args.timeout_seconds)
        report["status"] = "verified"
    except (Exception, KeyboardInterrupt) as error:
        report["errors"].append(f"{type(error).__name__}: {error}")
    text = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if destination is not None:
        with (destination / "verify.json").open("x", encoding="utf-8") as stream:
            stream.write(text)
    sys.stdout.write(text)
    return 0 if report["status"] == "verified" else 1


if __name__ == "__main__":
    sys.exit(main())
