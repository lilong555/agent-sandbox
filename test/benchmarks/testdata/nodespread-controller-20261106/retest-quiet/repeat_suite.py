#!/usr/bin/env python3
"""Run an order-alternated controller comparison with continuous host telemetry."""

import argparse
import base64
import datetime as dt
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def schedule(unprofiled=False, extended=False):
    if extended:
        pairs = [
            ("plain60", 4, 8, 60, ("baseline", "candidate")),
            ("plain60", 5, 8, 60, ("candidate", "baseline")),
            ("noise-baseline", 1, 8, 120, ("baseline", "baseline")),
            ("long8", 1, 8, 120, ("baseline", "candidate")),
            ("mid4", 1, 4, 120, ("candidate", "baseline")),
            ("long8", 2, 8, 120, ("candidate", "baseline")),
            ("long8", 3, 8, 120, ("baseline", "candidate")),
            ("noise-candidate", 1, 8, 120, ("candidate", "candidate")),
            ("long8", 4, 8, 120, ("candidate", "baseline")),
            ("mid4", 2, 4, 120, ("baseline", "candidate")),
            ("long8", 5, 8, 120, ("baseline", "candidate")),
            ("long8", 6, 8, 120, ("candidate", "baseline")),
        ]
        return [
            {
                "pair": pair, "rate": rate, "seconds": seconds, "pool": 128,
                "revision": revision, "group": group, "slot": slot, "unprofiled": True,
                "sample": (
                    f"quiet-plain-p{pair}-{revision}" if group == "plain60" else
                    f"local-{group}-s{slot}" if group.startswith("noise-") else
                    f"local-{group}-p{pair}-{revision}"
                ),
            }
            for group, pair, rate, seconds, revisions in pairs
            for slot, revision in enumerate(revisions, 1)
        ]
    if unprofiled:
        return [
            {
                "pair": pair,
                "rate": 8,
                "revision": revision,
                "sample": f"quiet-plain-p{pair}-{revision}",
                "unprofiled": True,
                "group": "plain60", "seconds": 60, "pool": 128, "slot": slot,
            }
            for pair, revisions in ((4, ("baseline", "candidate")),
                                    (5, ("candidate", "baseline")))
            for slot, revision in enumerate(revisions, 1)
        ]
    pairs = [
        (1, 2, ("baseline", "candidate")),
        (1, 8, ("candidate", "baseline")),
        (2, 8, ("baseline", "candidate")),
        (2, 2, ("candidate", "baseline")),
        (3, 2, ("baseline", "candidate")),
        (3, 8, ("candidate", "baseline")),
    ]
    return [
        {
            "pair": pair,
            "rate": rate,
            "revision": revision,
            "sample": f"quiet-r{rate}-p{pair}-{revision}",
        }
        for pair, rate, revisions in pairs
        for revision in revisions
    ]


def cpu_times():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()
    values = [int(value) for value in fields[1:9]]
    return sum(values), values[3], values[4], values[7]


def linux_sample(previous):
    current = cpu_times()
    delta = current[0] - previous[0]
    memory = {
        key: int(value.split()[0])
        for line in Path("/proc/meminfo").read_text().splitlines()
        for key, value in [line.split(":", 1)]
    }
    vm = dict(line.split() for line in Path("/proc/vmstat").read_text().splitlines())
    pressure = {}
    for resource in ("cpu", "memory", "io"):
        rows = {}
        for line in Path(f"/proc/pressure/{resource}").read_text().splitlines():
            kind, *fields = line.split()
            rows[kind] = {
                key: float(value) for key, value in (field.split("=") for field in fields)
            }
        pressure[resource] = rows
    return {
        "utc": utc(),
        "cpu_active_percent": (
            100 * (delta - (current[1] - previous[1]) - (current[2] - previous[2])) / delta
            if delta else None
        ),
        "cpu_iowait_percent": 100 * (current[2] - previous[2]) / delta if delta else None,
        "cpu_steal_percent": 100 * (current[3] - previous[3]) / delta if delta else None,
        "loadavg": [float(value) for value in Path("/proc/loadavg").read_text().split()[:3]],
        "available_kb": memory["MemAvailable"],
        "swap_used_kb": memory["SwapTotal"] - memory["SwapFree"],
        "pswpin": int(vm["pswpin"]),
        "pswpout": int(vm["pswpout"]),
        "pressure": pressure,
    }, current


def linux_monitor(stop, path):
    previous = cpu_times()
    with path.open("x") as output:
        while not stop.wait(5):
            try:
                record, previous = linux_sample(previous)
            except Exception as error:
                record = {"utc": utc(), "error": str(error)}
            output.write(json.dumps(record) + "\n")
            output.flush()


def kube(*args):
    result = subprocess.run(
        ["kubectl", "--kubeconfig", str(ROOT / "kubeconfig"), *args],
        check=True, capture_output=True, text=True, timeout=25,
    )
    return json.loads(result.stdout)


def snapshot(output, suffix):
    for resource in ("nodes", "namespaces", "pods"):
        extra = ["-A"] if resource == "pods" else []
        save(output / f"{resource}-{suffix}.json", kube("get", resource, *extra, "-o", "json"))


def wait_clean():
    deadline = time.monotonic() + 120
    while True:
        namespaces = kube("get", "namespaces", "-o", "json")
        remaining = [
            item["metadata"]["name"] for item in namespaces["items"]
            if item["metadata"]["name"].startswith("np2179-")
        ]
        if not remaining:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"Previous test namespaces remain: {remaining}")
        time.sleep(3)


def run_one(item, output, manifest, warmup=False):
    wait_clean()
    time.sleep(15)
    run = output / item["sample"]
    run.mkdir()
    snapshot(run, "before")
    nodes = json.loads((run / "nodes-before.json").read_text())["items"]
    for node in nodes:
        conditions = {row["type"]: row["status"] for row in node["status"]["conditions"]}
        if conditions.get("Ready") != "True" or any(
            conditions.get(condition) != "False"
            for condition in ("MemoryPressure", "DiskPressure", "PIDPressure")
        ):
            raise RuntimeError(f"Unhealthy node before {item['sample']}: {conditions}")
    record = dict(item, warmup=warmup, start_utc=utc(), status="running")
    manifest["runs"].append(record)
    save(output / "manifest.json", manifest)
    print(f"{utc()} START {item['sample']}", flush=True)
    env = dict(os.environ)
    seconds = item.get("seconds", 20 if warmup else 60)
    pool = item.get("pool", 128)
    env.update(RUN_SECONDS=str(seconds),
               PROFILE_SECONDS="5" if warmup else "30", PROFILE_DELAY="5" if warmup else "10")
    command = ["bash", str(ROOT / "run_sample.sh"), item["revision"], item["sample"], str(pool), str(item["rate"])]
    if item.get("unprofiled"):
        command = [
            "python3", str(HERE / "run_unprofiled.py"), item["revision"], item["sample"],
            "--rate", str(item["rate"]), "--seconds", str(seconds),
            "--pool", str(pool), "--group", item.get("group", "plain60"),
        ]
    with (run / "console.log").open("x") as log:
        process = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=650)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    record.update(status="completed" if status == 0 else "failed", exit_code=status, end_utc=utc())
    save(output / "manifest.json", manifest)
    snapshot(run, "after")
    if status:
        raise RuntimeError(f"{item['sample']} exited with {status}")
    summary = json.loads((ROOT / "runs" / item["sample"] / "workload" / "summary.json").read_text())
    phase = summary["phases"][0]
    latency = phase["latency"]["endToEndReady"]
    record.update(created=phase["created"], ready=phase["ready"], failed=phase["failed"],
                  p50_ms=latency["p50Ms"], p99_ms=latency["p99Ms"])
    save(output / "manifest.json", manifest)
    print(f"{utc()} DONE {item['sample']} ready={phase['ready']} failed={phase['failed']} "
          f"p50={latency['p50Ms']:.3f}ms p99={latency['p99Ms']:.3f}ms", flush=True)
    if phase["failed"] or phase["ready"] != phase["created"]:
        raise RuntimeError(f"Incomplete workload: {item['sample']}")
    wait_clean()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--monitor-smoke", action="store_true")
    parser.add_argument("--unprofiled", action="store_true")
    parser.add_argument("--extended", action="store_true",
                        help="Run the registered 24-run unprofiled comparison")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir()
    stop = threading.Event()
    stop_file = output / "stop-windows"
    windows_path = lambda path: subprocess.check_output(["wslpath", "-w", str(path)], text=True).strip()
    thread = threading.Thread(target=linux_monitor, args=(stop, output / "linux-load.jsonl"), daemon=True)
    thread.start()
    with (output / "windows-load.jsonl").open("x") as windows_log, (output / "windows-error.log").open("x") as windows_error:
        script = "& {\n" + (HERE / "windows_load.ps1").read_text() + "\n} -StopFile '"
        script += windows_path(stop_file).replace("'", "''") + "'"
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        windows = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            stdout=windows_log, stderr=windows_error,
        )
        unprofiled = args.unprofiled or args.extended
        manifest = {"started_utc": utc(), "schedule": schedule(unprofiled, args.extended),
                    "unprofiled": unprofiled, "extended": args.extended,
                    "runs": [], "status": "running"}
        save(output / "manifest.json", manifest)
        try:
            time.sleep(12)
            if windows.poll() is not None:
                raise RuntimeError("Windows monitor exited early; inspect windows-error.log")
            if not (output / "windows-load.jsonl").stat().st_size:
                raise RuntimeError("Windows monitor produced no samples")
            if args.monitor_smoke:
                print("Host monitors produced samples.", flush=True)
            else:
                if unprofiled:
                    run_one({
                        "pair": 0, "rate": 8, "seconds": 20, "pool": 128,
                        "revision": "baseline", "sample": "quiet-plain-warmup",
                        "group": "warmup", "slot": 1, "unprofiled": True,
                    }, output, manifest, warmup=True)
                else:
                    run_one({"pair": 0, "rate": 8, "revision": "baseline",
                             "sample": "quiet-warmup"}, output, manifest, warmup=True)
                for item in manifest["schedule"]:
                    if windows.poll() is not None:
                        raise RuntimeError("Windows monitor exited during suite")
                    run_one(item, output, manifest)
            manifest["status"] = "completed"
        except BaseException as error:
            manifest.update(status="failed", error=str(error))
            raise
        finally:
            stop.set()
            thread.join(timeout=10)
            stop_file.touch()
            try:
                windows.wait(timeout=20)
            except subprocess.TimeoutExpired:
                windows.terminate()
                windows.wait(timeout=10)
            manifest.update(ended_utc=utc(), windows_monitor_exit=windows.returncode)
            save(output / "manifest.json", manifest)


if __name__ == "__main__":
    main()
