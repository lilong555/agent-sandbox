"""Collect deterministic WarmPool recreation evidence against main."""

from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess

root = Path.cwd()
artifacts = root / "_artifacts"
artifacts.mkdir(exist_ok=True)
test = root / "extensions/controllers/warmpool_recreation_probe_test.go"
shutil.copyfile(root / ".github/warmpool-recreation/recreation_test.go", test)
subprocess.run(["gofmt", "-w", str(test)], check=True)
with (artifacts / "reproduction.jsonl").open("w") as output:
    result = subprocess.run([
        "go", "test", "-race", "-count=3", "-shuffle=on", "-json",
        "-run", "^TestWarmPoolRecreation", "./extensions/controllers",
    ], stdout=output)
events = [json.loads(line) for line in (artifacts / "reproduction.jsonl").read_text().splitlines()]
cases = Counter((event.get("Test"), event["Action"]) for event in events if event["Action"] in {"fail", "pass"})
expected = {
    "same_pool_still_counts_terminating_member": "pass",
    "recreated_pool_before_expiry": "fail",
    "recreated_pool_after_expiry": "pass",
}
assert result.returncode == 1, f"unexpected go test exit: {result.returncode}"
for name, action in expected.items():
    assert cases[(f"TestWarmPoolRecreation/{name}", action)] == 3, cases
assert cases[("TestWarmPoolRecreationLateEvents/previous_owner_event", "fail")] == 3, cases
assert cases[("TestWarmPoolRecreationLateEvents/current_owner_event", "pass")] == 3, cases
for event in events:
    if "pool_uid=" in event.get("Output", "") or "previous_owner_event=" in event.get("Output", ""):
        print(event["Output"], end="")
summary = {
    "baseline": "1b7d5336539b9acd44ec431a61f4e83700a0a392",
    "expected_regression_failures": 6,
    "passing_controls": 9,
    "race_detector": True,
    "repetitions": 3,
    "scope": "Public Reconcile with fake-client lifecycle operations, finalizer retention and delayed watch observations.",
}
(artifacts / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
