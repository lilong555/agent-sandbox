#!/usr/bin/env python3
"""Run a warm-claim comparison with profiling disabled and bounded telemetry."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from profile_workload import match_boundary, match_trigger, timestamp


EXPECTED_BINARIES = {
    "baseline": "391936e7449042fa16effd512af0e9b811a95a83f3fd6a668bf7c13b7f4640ef",
    "candidate": "1abdc62ce6e1dc18afe34692d21e0a4ea0a7a23994918deb0b3b124bf5da21b7",
    "stress": "07bfc65345219b80cedcbb073123db88bf5b275eb683665913c585638b69bbb1",
}


def controller_args(original):
    result = []
    for argument in original:
        if argument == "--enable-pprof-debug":
            continue
        if argument.startswith("--pprof-mutex-profile-fraction="):
            result.append("--pprof-mutex-profile-fraction=0")
        else:
            result.append(argument)
    return result


def environments(parent, kubeconfig):
    stress = dict(parent, KUBECONFIG=str(kubeconfig))
    return dict(stress, GOMAXPROCS="4"), stress


def workload_args(binary, run, sample, rate, seconds, pool):
    if rate < 1 or seconds < 1 or pool < 1 or pool % rate:
        raise ValueError("Positive pool size must be divisible by positive rate")
    return [
        str(binary), f"-namespace=np2179-{sample}",
        f"-output-dir={run / 'workload'}", "-image=node:22-alpine",
        "-phases=claims-warm-sustained", f"-sustained-rate={rate}",
        f"-sustained-seconds={seconds}", f"-sustained-pool-headroom={pool // rate}s",
        "-claim-dwell=1s", "-client-connections=4",
        "-profile-controller=false", "-profile-apiserver=false",
        "-collect-metrics=false", "-timeout=8m", "-per-sandbox-timeout=2m",
    ]


class MetricsSampler:
    def __init__(self, path, result, interval=5):
        self.path = path
        self.result = result
        self.interval = interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def capture(self, tag):
        record = {"tag": tag, "started_at": timestamp()}
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:18080/metrics", timeout=5) as response:
                content = response.read()
            artifact = f"metrics-{len(self.result['samples']):04d}.prom"
            with (self.path / artifact).open("xb") as output:
                output.write(content)
            record.update(status="complete", artifact=artifact)
        except Exception as error:
            record.update(status="error", error=type(error).__name__)
            self.result["errors"].append(record["error"])
        record["ended_at"] = timestamp()
        self.result["samples"].append(record)

    def run(self):
        self.capture("start")
        while not self.stop.wait(self.interval):
            self.capture("periodic")
        self.capture("end")

    def start(self):
        self.thread.start()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise TimeoutError("Metrics sampler did not stop")


def observe_workload(command, env, run, timeout=500):
    path = run / "telemetry"
    path.mkdir()
    result = {"status": "running", "samples": [], "errors": []}
    sampler = None
    stream = queue.Queue()
    with (run / "stress.log").open("x") as log:
        process = subprocess.Popen(
            command, env=env, cwd=ROOT, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )

        def read_output():
            try:
                for line in process.stdout:
                    at = timestamp()
                    log.write(line)
                    log.flush()
                    stream.put((at, line))
            finally:
                stream.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Workload deadline exceeded")
                try:
                    event = stream.get(timeout=0.1)
                except queue.Empty:
                    continue
                if event is None:
                    break
                at, line = event
                if "trigger" not in result:
                    trigger = match_trigger(line)
                    if trigger:
                        result["trigger"] = dict(trigger, at=at)
                        sampler = MetricsSampler(path, result)
                        sampler.start()
                elif "boundary" not in result:
                    boundary = match_boundary(line, result["trigger"])
                    if boundary:
                        result["boundary"] = {"kind": boundary, "at": at}
                        sampler.close()
                        sampler = None
            result["exit_code"] = process.wait(timeout=10)
            if result["exit_code"]:
                result["errors"].append(f"WorkloadExit:{result['exit_code']}")
            if "trigger" not in result or result.get("boundary", {}).get("kind") != "arrivals_finished":
                result["errors"].append("MissingArrivalWindow")
            result["status"] = "complete" if not result["errors"] else "failed"
        except BaseException as error:
            result["status"] = "failed"
            result["errors"].append(type(error).__name__)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if sampler is not None:
                sampler.close()
            reader.join(timeout=5)
            process.stdout.close()
            (path / "run.json").write_text(json.dumps(result, indent=2) + "\n")
    if result["status"] != "complete":
        raise RuntimeError(f"Incomplete workload observation: {result['errors']}")
    return result


def wait_namespace_deleted(namespace, env, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        completed = subprocess.run(
            ["kubectl", "get", "namespace", namespace, "--ignore-not-found", "-o", "name"],
            env=env, check=True, capture_output=True, text=True, timeout=15,
        )
        if not completed.stdout.strip():
            return
        time.sleep(2)
    raise TimeoutError(f"Namespace cleanup incomplete: {namespace}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("revision", choices=("baseline", "candidate"))
    parser.add_argument("sample")
    parser.add_argument("--rate", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--pool", type=int, default=128)
    parser.add_argument("--group", default="plain60")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.sample):
        parser.error("Use a lowercase sample name of at most 40 characters")
    repo = ROOT.parent / ("baseline" if args.revision == "baseline" else "repo")
    source = ROOT / "runs" / f"quiet-r8-p1-{args.revision}" / "config.json"
    config = json.loads(source.read_text())
    config.update(profile_enabled=False, profile_seconds=0, profile_delay=0,
                  controller_args=controller_args(config["controller_args"]),
                  seconds=args.seconds, claims_per_second=args.rate, pool=args.pool,
                  group=args.group, metrics_interval_seconds=5)
    commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if commit != config["commit"]:
        raise RuntimeError("Controller source revision changed")
    manager_env, env = environments(os.environ, ROOT / "kubeconfig")
    config["stress_gomaxprocs"] = env.get("GOMAXPROCS")
    context = subprocess.check_output(["kubectl", "config", "current-context"], env=env, text=True).strip()
    if context != "kind-as-nodespread-2179":
        raise RuntimeError(f"Unexpected Kubernetes context: {context}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open("http://127.0.0.1:18081/healthz", timeout=1).close()
    except OSError:
        pass
    else:
        raise RuntimeError("Controller port is occupied")
    run = ROOT / "runs" / args.sample
    run.mkdir()
    binaries = [repo / "bin" / "manager", ROOT / "tools" / "stress"]
    command = workload_args(binaries[1], run, args.sample, args.rate, args.seconds, args.pool)
    (run / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in binaries]
    if hashes != [EXPECTED_BINARIES[args.revision], EXPECTED_BINARIES["stress"]]:
        raise RuntimeError("A registered executable has changed")
    (run / "binaries.sha256").write_text("".join(
        f"{digest}  {path}\n" for digest, path in zip(hashes, binaries)))
    (run / "command.json").write_text(json.dumps(command, indent=2) + "\n")

    def interrupted(signum, frame):
        raise InterruptedError(f"Received signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    with (run / "controller.log").open("x") as log:
        manager = subprocess.Popen(
            ["taskset", "-c", "6,8,10,12", str(binaries[0]), *config["controller_args"]],
            env=manager_env, cwd=repo, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(60):
                if manager.poll() is not None:
                    raise RuntimeError("Controller exited before readiness")
                try:
                    opener.open("http://127.0.0.1:18081/readyz", timeout=1).close()
                    break
                except OSError:
                    time.sleep(1)
            else:
                raise RuntimeError("Controller readiness timed out")
            observe_workload(command, env, run)
            with opener.open("http://127.0.0.1:18080/metrics", timeout=10) as response:
                (run / "metrics-final.prom").write_bytes(response.read())
            wait_namespace_deleted(f"np2179-{args.sample}", env)
        finally:
            manager.terminate()
            try:
                manager.wait(timeout=20)
            except subprocess.TimeoutExpired:
                manager.kill()
                manager.wait(timeout=10)


if __name__ == "__main__":
    main()
