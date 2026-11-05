#!/usr/bin/env python3
r"""Profile a controller while an unchanged stress command runs.

Example:
  KUBECONFIG=/path/to/config python3 profile_workload.py --output-dir run-1 \
      --profile-delay 10 --profile-seconds 30 -- ./tools/stress \
      --phases claims-warm-sustained --profile-controller=false \
      --profile-apiserver=false --output-dir run-1/stress

The first matching claims phase is sampled. run.json uses client-observed UTC
and monotonic timestamps; request intervals bound the server sampling windows.
Only complete HTTP payloads are published as artifacts. A phase ending early
cancels outstanding profiles and makes the run unsuccessful. Existing output
artifacts are protected. Environment and command arguments are never serialized;
stress.log contains the command's unmodified stdout and stderr.
"""

import argparse
import datetime
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


POLL_SECONDS = 0.02
HTTP_GRACE_SECONDS = 10
PROFILE_NAMES = ("cpu", "mutex", "allocs")
ARTIFACTS = {
    "cpu": "cpu.pprof",
    "mutex": "mutex.pprof",
    "allocs": "allocs.pprof",
    "metrics_start": "metrics-start.txt",
    "metrics_end": "metrics-end.txt",
}
PHASE_LINE = re.compile(r"\[(claims-warm(?:-sustained)?)#(\d+)\]\s+(.*)")


def timestamp():
    return {
        "utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "monotonic_seconds": time.monotonic(),
    }


def live(message):
    print(f"{datetime.datetime.now(datetime.timezone.utc).isoformat()} {message}",
          flush=True)


def match_trigger(line):
    match = PHASE_LINE.search(line)
    if not match:
        return None
    phase, number, body = match.groups()
    if phase == "claims-warm" and re.match(
            r"firing \d+ claims simultaneously\b", body):
        kind = "warm_burst"
    elif phase == "claims-warm-sustained" and re.match(
            r"arrivals:\s+.+\bPoisson\b", body):
        kind = "sustained"
    else:
        return None
    return {"kind": kind, "phase": phase, "phase_number": int(number)}


def match_boundary(line, trigger):
    match = PHASE_LINE.search(line)
    if not match:
        return None
    phase, number, body = match.groups()
    if (phase, int(number)) != (trigger["phase"], trigger["phase_number"]):
        return None
    if phase == "claims-warm" and re.match(
            r"all \d+ created claims are Ready\b", body):
        return "claims_ready"
    if body.startswith("arrival window done:"):
        return "arrivals_finished"
    if "cleaning up" in body:
        return "cleanup_started"
    if body.startswith("done:"):
        return "phase_finished"
    return None


def finite_number(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be finite and >= 0")
    return number


def local_url(value):
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
        valid = (
            url.scheme in ("http", "https")
            and url.hostname in ("localhost", "127.0.0.1", "::1")
            and url.username is None and url.password is None
            and url.path in ("", "/") and not url.query and not url.fragment
            and (port is None or 0 < port < 65536)
        )
    except ValueError:
        valid = False
    if not valid:
        raise argparse.ArgumentTypeError(
            "use a loopback HTTP(S) origin without credentials, query or path")
    return value.rstrip("/")


def parse_args(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--metrics-url", type=local_url,
                        default="http://127.0.0.1:18080")
    parser.add_argument("--profile-seconds", type=int, default=30)
    parser.add_argument("--profile-delay", type=finite_number, default=10)
    parser.add_argument("--timeout-seconds", type=finite_number, default=2100,
                        help="total stress process deadline (default: 2100)")
    parser.add_argument("--stop-grace-seconds", type=finite_number, default=10,
                        help="SIGTERM grace before SIGKILL (default: 10)")
    split = argv.index("--") if "--" in argv else len(argv)
    args = parser.parse_args(argv[:split])
    args.command = argv[split + 1:]
    if not args.command or not args.command[0] or args.command[0].startswith("-"):
        parser.error("supply an executable and its arguments after --")
    if any("\0" in argument for argument in args.command):
        parser.error("command arguments must be free of NUL characters")
    if args.profile_seconds <= 0:
        parser.error("--profile-seconds must be > 0")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be > 0")
    return args


def error_details(exc):
    # Exception messages, response bodies and argv can contain credentials.
    result = {"type": type(exc).__name__}
    if isinstance(exc, urllib.error.HTTPError):
        result["http_status"] = exc.code
    elif isinstance(exc, OSError) and isinstance(exc.errno, int):
        result["errno"] = exc.errno
    return result


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_worker(connection, url, partial_path, seconds, is_profile):
    """Run one cancellable urllib request in an owned, bounded child process."""
    result = {"status": "error"}
    try:
        connection.send({"event": "ready"})
        if not connection.poll(HTTP_GRACE_SECONDS):
            raise TimeoutError()
        connection.recv()
        result["started_at"] = timestamp()
        connection.send({"event": "started", "at": result["started_at"]})
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        size = 0
        prefix = b""
        limit = 128 * 1024 * 1024 if is_profile else 32 * 1024 * 1024
        with opener.open(url, timeout=seconds + HTTP_GRACE_SECONDS) as response:
            if response.status != 200:
                raise ValueError("unexpected HTTP status")
            expected_length = response.headers.get("Content-Length")
            with open(partial_path, "xb") as output:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    if not prefix:
                        prefix = chunk[:2]
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("response size limit")
                    output.write(chunk)
            if expected_length is not None and size != int(expected_length):
                raise ValueError("truncated response")
        if not size or (is_profile and prefix != b"\x1f\x8b"):
            raise ValueError("empty response or invalid pprof gzip signature")
        result.update(status="complete", bytes=size)
    except Exception as exc:
        result["error"] = error_details(exc)
    finally:
        result["ended_at"] = timestamp()
        try:
            connection.send({"event": "result", "result": result})
        finally:
            connection.close()


class Capture:
    def __init__(self, args, result):
        self.args = args
        self.result = result
        self.context = multiprocessing.get_context("spawn")
        self.jobs = {}
        self.stage = "waiting"

    def launch(self, name):
        endpoint = {
            "cpu": f"/debug/pprof/profile?seconds={self.args.profile_seconds}",
            "mutex": f"/debug/pprof/mutex?seconds={self.args.profile_seconds}",
            "allocs": f"/debug/pprof/allocs?seconds={self.args.profile_seconds}",
        }.get(name, "/metrics")
        parent, child = self.context.Pipe()
        partial = self.args.output_dir / (ARTIFACTS[name] + ".part")
        process = self.context.Process(
            target=fetch_worker,
            args=(child, self.args.metrics_url + endpoint, str(partial),
                  self.args.profile_seconds if name in PROFILE_NAMES else 0,
                  name in PROFILE_NAMES),
        )
        record = self.result["requests"][name]
        record.update(status="starting", endpoint=endpoint)
        try:
            process.start()
        except Exception as exc:
            record.update(status="error", error=error_details(exc),
                          ended_at=timestamp())
            parent.close()
            child.close()
            return
        child.close()
        self.jobs[name] = {
            "process": process, "connection": parent, "partial": partial,
            "ready": False, "released": False, "record": record,
            "deadline": time.monotonic() + HTTP_GRACE_SECONDS,
        }

    def release(self, names):
        now = timestamp()
        if all(name in self.jobs for name in PROFILE_NAMES) and (
                set(names) == set(PROFILE_NAMES)):
            self.result["profile_window"]["released_at"] = now
            live("Capturing CPU, delta mutex and delta allocs.")
        for name in names:
            job = self.jobs[name]
            try:
                job["connection"].send("start")
                job["released"] = True
                seconds = self.args.profile_seconds if name in PROFILE_NAMES else 0
                job["deadline"] = time.monotonic() + seconds + HTTP_GRACE_SECONDS
                job["record"]["status"] = "running"
            except (OSError, EOFError) as exc:
                self.finish(name, {"status": "error", "error": error_details(exc),
                                   "ended_at": timestamp()})

    def finish(self, name, update):
        job = self.jobs.pop(name)
        process = job["process"]
        # A worker only sends its result after closing the response and file.
        if process.is_alive():
            process.terminate()
        process.join(timeout=0.2)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        job["connection"].close()
        process.close()
        record = job["record"]
        record.update(update)
        try:
            if record["status"] == "complete":
                target = self.args.output_dir / ARTIFACTS[name]
                os.link(job["partial"], target)  # Publish without overwriting.
                record["artifact"] = target.name
        except OSError as exc:
            record.update(status="error", error=error_details(exc))
        finally:
            job["partial"].unlink(missing_ok=True)
        if record["status"] != "complete":
            live(f"{name}: {record['status']}")

    def poll(self):
        for name in list(self.jobs):
            job = self.jobs[name]
            connection = job["connection"]
            try:
                while name in self.jobs and connection.poll():
                    message = connection.recv()
                    if message["event"] == "ready":
                        job["ready"] = True
                    elif message["event"] == "started":
                        job["record"]["started_at"] = message["at"]
                    else:
                        self.finish(name, message["result"])
                if name not in self.jobs:
                    continue
                if not job["process"].is_alive():
                    self.finish(name, {"status": "error",
                                       "error": {"type": "WorkerExited"},
                                       "ended_at": timestamp()})
                elif time.monotonic() >= job["deadline"]:
                    self.finish(name, {"status": "timeout",
                                       "error": {"type": "RequestDeadline"},
                                       "ended_at": timestamp()})
            except (EOFError, OSError) as exc:
                if name in self.jobs:
                    self.finish(name, {"status": "error",
                                       "error": error_details(exc),
                                       "ended_at": timestamp()})
        for name in ("metrics_start", "metrics_end"):
            if name in self.jobs:
                job = self.jobs[name]
                if job["ready"] and not job["released"]:
                    self.release([name])
        profiles = [name for name in PROFILE_NAMES if name in self.jobs]
        if profiles and all(self.jobs[name]["ready"] for name in profiles):
            unreleased = [name for name in profiles if not self.jobs[name]["released"]]
            if unreleased:
                self.release(unreleased)

    def stop_profiles(self, reason):
        for name in list(self.jobs):
            if name != "metrics_end":
                self.finish(name, {"status": "cancelled", "reason": reason,
                                   "ended_at": timestamp()})
        for name, record in self.result["requests"].items():
            if name != "metrics_end" and record["status"] == "pending":
                record.update(status="skipped", reason=reason)

    def tick(self, active, due):
        self.poll()
        if self.stage == "waiting" and active and due:
            self.stage = "start_metrics"
            self.result["profile_window"]["preparation_started_at"] = timestamp()
            self.launch("metrics_start")
        elif self.stage == "start_metrics" and "metrics_start" not in self.jobs:
            if active:
                self.stage = "profiles"
                for name in PROFILE_NAMES:
                    self.launch(name)
        elif self.stage == "profiles" and not any(
                name in self.jobs for name in PROFILE_NAMES):
            self.stage = "end_metrics"
            self.launch("metrics_end")
        elif self.stage == "end_metrics" and not self.jobs:
            self.stage = "done"

    def workload_ended(self, reason):
        if self.stage in ("end_metrics", "done"):
            return
        started = self.stage != "waiting"
        self.stop_profiles(reason)
        self.stage = "end_metrics" if started else "done"
        if started:
            self.launch("metrics_end")

    def close(self, reason):
        for name in list(self.jobs):
            self.finish(name, {"status": "cancelled", "reason": reason,
                               "ended_at": timestamp()})
        for record in self.result["requests"].values():
            if record["status"] == "pending":
                record.update(status="skipped", reason=reason)


def summarize_window(result):
    trigger = result["workload"].get("trigger")
    end = result["workload"].get("end")
    records = [result["requests"][name] for name in PROFILE_NAMES]
    complete = all(record["status"] == "complete" for record in records)
    window = result["profile_window"]
    window["complete"] = complete
    window["within_observed_workload"] = False
    if not (trigger and end):
        return
    workload_start = trigger["observed_at"]["monotonic_seconds"]
    workload_end = end["observed_at"]["monotonic_seconds"]
    timed = []
    for record in result["requests"].values():
        if "started_at" not in record or "ended_at" not in record:
            continue
        start = record["started_at"]["monotonic_seconds"]
        stop = record["ended_at"]["monotonic_seconds"]
        record.update(
            request_elapsed_seconds=stop - start,
            overlap_with_workload_seconds=max(
                0, min(stop, workload_end) - max(start, workload_start)),
            within_observed_workload=(
                start >= workload_start and stop <= workload_end),
        )
    for record in records:
        if "started_at" in record and "ended_at" in record:
            timed.append(record)
    if not timed:
        return
    starts = [record["started_at"]["monotonic_seconds"] for record in timed]
    ends = [record["ended_at"]["monotonic_seconds"] for record in timed]
    window.update(
        start_skew_seconds=max(starts) - min(starts),
        request_envelope_seconds=max(ends) - min(starts),
        common_request_overlap_seconds=max(0, min(ends) - max(starts)),
        overlap_with_workload_seconds=max(
            0, min(max(ends), workload_end) - max(min(starts), workload_start)),
        within_observed_workload=(
            complete and min(starts) >= workload_start and max(ends) <= workload_end),
    )


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def run(args):
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reserved = ["run.json", "stress.log", *ARTIFACTS.values(),
                *(name + ".part" for name in ARTIFACTS.values())]
    if any((args.output_dir / name).exists() for name in reserved):
        live("Output artifacts already exist; choose a fresh output directory.")
        return 2
    result = {
        "schema_version": 1, "started_at": timestamp(), "status": "running",
        "config": {
            "metrics_url": args.metrics_url,
            "profile_seconds": args.profile_seconds,
            "profile_delay_seconds": args.profile_delay,
            "timeout_seconds": args.timeout_seconds,
            "stop_grace_seconds": args.stop_grace_seconds,
            "log_poll_seconds": POLL_SECONDS,
        },
        "stress": {}, "workload": {}, "profile_window": {},
        "requests": {name: {"status": "pending"} for name in ARTIFACTS},
        "errors": [],
    }
    # Exclusive creation protects both prior results and concurrent invocations.
    with (args.output_dir / "run.json").open("x", encoding="utf-8") as report:
        json.dump(result, report, indent=2)
        report.flush()
        capture = Capture(args, result)
        process = None
        interrupted = []
        previous_handlers = {}
        forced_exit = None
        stop_deadline = None
        pending = b""
        trigger_due = None

        def on_signal(signum, frame):
            if not interrupted:
                interrupted.append(signum)

        def observe(line):
            nonlocal trigger_due
            trigger = result["workload"].get("trigger")
            if trigger is None:
                trigger = match_trigger(line)
                if trigger:
                    trigger["observed_at"] = timestamp()
                    result["workload"]["trigger"] = trigger
                    trigger_due = time.monotonic() + args.profile_delay
                    live(f"Workload triggered: {trigger['kind']}.")
            if trigger and "end" not in result["workload"]:
                boundary = match_boundary(line, trigger)
                if boundary:
                    end_workload(boundary)

        def end_workload(reason):
            if "end" not in result["workload"]:
                result["workload"]["end"] = {
                    "kind": reason, "observed_at": timestamp()}
                capture.workload_ended(reason)
                live(f"Workload boundary: {reason}.")

        try:
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, on_signal)
            with (args.output_dir / "stress.log").open("xb", buffering=0) as log:
                process = subprocess.Popen(
                    args.command, stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
                result["stress"]["started_at"] = timestamp()
                live("Stress process started.")
                deadline = time.monotonic() + args.timeout_seconds
                with (args.output_dir / "stress.log").open("rb") as tail:
                    while True:
                        chunk = tail.read(65536)
                        if chunk:
                            lines = (pending + chunk).split(b"\n")
                            pending = lines.pop()[-65536:]
                            for line in lines:
                                observe(line.decode("utf-8", errors="replace"))
                        returncode = process.poll()
                        if returncode is not None:
                            if not chunk and pending:
                                observe(pending.decode("utf-8", errors="replace"))
                                pending = b""
                            if "ended_at" not in result["stress"]:
                                result["stress"].update(
                                    ended_at=timestamp(), returncode=returncode)
                                end_workload("process_exited")
                                # Descendants belong to this invocation's session.
                                signal_group(process, signal.SIGKILL)
                                live(f"Stress process exited: {returncode}.")
                        now = time.monotonic()
                        if forced_exit is None and (
                                interrupted or (returncode is None and now >= deadline)):
                            forced_exit = 128 + interrupted[0] if interrupted else 124
                            reason = "interrupted" if interrupted else "run_timeout"
                            result["errors"].append({"kind": reason, "at": timestamp()})
                            capture.close(reason)
                            capture.stage = "done"
                            end_workload(reason)
                            signal_group(process, signal.SIGTERM)
                            stop_deadline = now + args.stop_grace_seconds
                        if stop_deadline is not None and now >= stop_deadline:
                            signal_group(process, signal.SIGKILL)
                            stop_deadline = None
                        active = (
                            "trigger" in result["workload"]
                            and "end" not in result["workload"]
                            and returncode is None and forced_exit is None)
                        capture.tick(
                            active, trigger_due is not None and now >= trigger_due)
                        if ("ended_at" in result["stress"] and not capture.jobs
                                and not chunk):
                            break
                        if not chunk:
                            time.sleep(POLL_SECONDS)
        except Exception as exc:
            result["errors"].append({
                "kind": "harness_error", "error": error_details(exc),
                "at": timestamp(),
            })
            forced_exit = forced_exit or 1
        finally:
            capture.close("harness_finished")
            if process is not None and process.poll() is None:
                signal_group(process, signal.SIGTERM)
                try:
                    process.wait(timeout=args.stop_grace_seconds)
                except subprocess.TimeoutExpired:
                    signal_group(process, signal.SIGKILL)
                    process.wait(timeout=5)
            if process is not None:
                signal_group(process, signal.SIGKILL)
                result["stress"].setdefault("ended_at", timestamp())
                result["stress"]["returncode"] = process.returncode
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
            if "end" not in result["workload"]:
                result["workload"]["end"] = {
                    "kind": "harness_finished", "observed_at": timestamp()}
            if "trigger" not in result["workload"]:
                result["errors"].append({"kind": "trigger_not_observed"})
            summarize_window(result)
            returncode = result["stress"].get("returncode", 0)
            if forced_exit:
                exit_code = forced_exit
            elif returncode:
                exit_code = returncode if returncode > 0 else 128 - returncode
            else:
                good = all(record["status"] == "complete"
                           for record in result["requests"].values())
                exit_code = 0 if (
                    good and result["profile_window"]["within_observed_workload"]
                    and not result["errors"]) else 1
            result.update(
                ended_at=timestamp(), exit_code=exit_code,
                status="complete" if exit_code == 0 else "failed")
            report.seek(0)
            json.dump(result, report, indent=2)
            report.write("\n")
            report.truncate()
            report.flush()
            live(f"Run {result['status']}; exit={exit_code}; report=run.json")
    return exit_code


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except OSError as exc:
        live(f"Output setup failed: {type(exc).__name__}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
