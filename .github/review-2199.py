"""Run the SDK through its standard test entry point on the fork."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import xml.etree.ElementTree as ET

root = Path(__file__).resolve().parent.parent
artifacts = root / "_artifacts"
artifacts.mkdir(exist_ok=True)
subprocess.run(["git", "rev-parse", "HEAD^"], stdout=(artifacts / "tested-source.txt").open("w"), check=True)
sys.path.insert(0, str(root / "dev/tools"))
runner = runpy.run_path(str(root / "dev/tools/test-unit"), run_name="review_test_unit")
run_python_tests = runner["run_python_tests"]
run_python_tests.__globals__["PYTHON_TEST_SUITES"] = [
    suite for suite in runner["PYTHON_TEST_SUITES"]
    if suite["name"] in ("k8s-agent-sandbox", "dev-tools-shared")
]
code = run_python_tests(str(root), str(artifacts))
report = ET.parse(artifacts / "junit_unit-python-k8s-agent-sandbox.xml")
cases = [case for case in report.findall(".//testcase") if "test_command_limits_daemon" in case.get("classname", "")]
assert cases and all(case.find("skipped") is None for case in cases), "daemon tests must execute"
print(f"Executed {len(cases)} daemon cases through test-unit", flush=True)
if code:
    raise SystemExit(code)
python = root / "bin/python-venv-k8s-agent-sandbox/bin/python"
package = root / "clients/python/agentic-sandbox-client"
env = dict(os.environ, SANDBOXD_BINARY=str(root / "bin/sandboxd"))
subprocess.run([
    str(python), "-m", "pytest", "-q",
    "k8s_agent_sandbox/test/unit/test_command_limits.py",
    "k8s_agent_sandbox/test/unit/test_command_limits_daemon.py",
    f"--junitxml={artifacts / 'focused.xml'}",
], cwd=package, env=env, check=True)
