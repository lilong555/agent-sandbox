"""Validate Claim UID protection on the fork."""

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "_artifacts"
PACKAGE = Path("clients/python/agentic-sandbox-client")
REGRESSION = Path("k8s_agent_sandbox/test/unit/test_adoption_lifecycle.py")
BASELINE = "1b7d5336539b9acd44ec431a61f4e83700a0a392"
REVIEW_BASELINE = "26ebc2a46449a8c07f2e27e50b2c8b9813e90ecc"
BUG_TESTS = (
    "test_terminate_preserves_recreated_claim",
    "test_handle_reuse_distinguishes_claim_uid",
    "test_async_lookup_checks_claim_uid_of_concurrently_tracked_handle",
    "test_exit_cleanup_preserves_recreated_claim_and_deletes_other_claims",
)
CONTROL = "test_terminate_missing_claim_clears_handle"


def run(*args, **kwargs):
    print("+ " + " ".join(str(arg) for arg in args), flush=True)
    return subprocess.run(list(args), cwd=ROOT, check=True, **kwargs)


def sdk():
    sys.path.insert(0, str(ROOT / "dev/tools"))
    runner = runpy.run_path(str(ROOT / "dev/tools/test-unit"), run_name="uid_test_unit")
    tests = runner["run_python_tests"]
    tests.__globals__["PYTHON_TEST_SUITES"] = [
        suite for suite in runner["PYTHON_TEST_SUITES"]
        if suite["name"] == "k8s-agent-sandbox"
    ]
    if tests(str(ROOT), str(ARTIFACTS)):
        raise SystemExit("SDK test-unit entry point failed")
    python = ROOT / "bin/python-venv-k8s-agent-sandbox/bin/python"
    run(str(python), "-m", "pytest", "-q", str(ROOT / PACKAGE / REGRESSION),
        f"--junitxml={ARTIFACTS / 'lifecycle-fixed.xml'}")
    with tempfile.TemporaryDirectory(prefix="uid-baseline-") as directory:
        baseline = Path(directory) / "repo"
        run("git", "worktree", "add", "--detach", str(baseline), BASELINE)
        try:
            shutil.copyfile(ROOT / PACKAGE / REGRESSION, baseline / PACKAGE / REGRESSION)
            env = dict(os.environ, PYTHONPATH=str(baseline / PACKAGE))
            result = subprocess.run([
                str(python), "-m", "pytest", "-q", str(baseline / PACKAGE / REGRESSION),
                "-k", " or ".join((*BUG_TESTS, CONTROL)),
                f"--junitxml={ARTIFACTS / 'lifecycle-baseline.xml'}",
            ], cwd=baseline / PACKAGE, env=env)
            cases = ET.parse(ARTIFACTS / "lifecycle-baseline.xml").findall(".//testcase")
            bugs = [case for case in cases if case.get("name", "").startswith(BUG_TESTS)]
            controls = [case for case in cases if case.get("name", "").startswith(CONTROL)]
            assert result.returncode == 1, result.returncode
            assert len(bugs) == 13 and all(case.find("failure") is not None for case in bugs)
            assert len(controls) == 2 and all(len(case) == 0 for case in controls)
            print("Unfixed baseline: 13 regression failures and 2 passing controls", flush=True)
        finally:
            run("git", "worktree", "remove", "--force", str(baseline))

    with tempfile.TemporaryDirectory(prefix="uid-review-baseline-") as directory:
        baseline = Path(directory) / "repo"
        run("git", "worktree", "add", "--detach", str(baseline), REVIEW_BASELINE)
        try:
            shutil.copyfile(ROOT / PACKAGE / REGRESSION, baseline / PACKAGE / REGRESSION)
            env = dict(os.environ, PYTHONPATH=str(baseline / PACKAGE))
            result = subprocess.run([
                str(python), "-m", "pytest", "-q", str(baseline / PACKAGE / REGRESSION),
                "-k", "old_lookup_preserves or generated_creation",
                f"--junitxml={ARTIFACTS / 'review-baseline.xml'}",
            ], cwd=baseline / PACKAGE, env=env)
            cases = ET.parse(ARTIFACTS / "review-baseline.xml").findall(".//testcase")
            assert result.returncode == 1, result.returncode
            assert len(cases) == 8
            assert sum(case.find("failure") is not None for case in cases) == 5
            assert sum(len(case) == 0 for case in cases) == 3
            print("Pre-review baseline: 5 regression failures and 3 passing controls", flush=True)
        finally:
            run("git", "worktree", "remove", "--force", str(baseline))


def api():
    assets = subprocess.check_output([
        "go", "-C", "dev/tools", "tool", "setup-envtest", "use", "1.37.0", "-p", "path",
    ], cwd=ROOT, text=True).strip()
    python = ROOT / "bin/python-venv-k8s-agent-sandbox/bin/python"
    if not python.exists():
        run(sys.executable, "-m", "venv", str(python.parent.parent))
        run(str(python), "-m", "pip", "install", "--quiet", "-e", str(ROOT / PACKAGE) + "[test]")
    env = dict(os.environ, KUBEBUILDER_ASSETS=assets)
    run(str(python), str(ROOT / ".github/claim-uid-api.py"), env=env)


def all_checks():
    with (ARTIFACTS / "make-all.log").open("w") as output:
        result = subprocess.run(["make", "all"], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT)
    print((ARTIFACTS / "make-all.log").read_text()[-20000:], flush=True)
    (ARTIFACTS / "generated.patch").write_bytes(
        subprocess.check_output(["git", "diff", "--", "docs", "k8s", "helm", "olm", "clients/k8s"], cwd=ROOT)
    )
    run("git", "diff", "--check")
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    ARTIFACTS.mkdir(exist_ok=True)
    source = subprocess.check_output([
        "git", "log", "-1", "--format=%H", "--", str(PACKAGE),
    ], cwd=ROOT, text=True).strip()
    tree = subprocess.check_output([
        "git", "rev-parse", f"HEAD:{PACKAGE}",
    ], cwd=ROOT, text=True).strip()
    (ARTIFACTS / "source.json").write_text(json.dumps({
        "source": source, "sdk_tree": tree, "baseline": BASELINE, "review_baseline": REVIEW_BASELINE,
    }) + "\n")
    {"sdk": sdk, "api": api, "all": all_checks}[sys.argv[1]]()
