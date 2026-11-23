# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Check coverage artifacts and compare runs in the same prepared environments."""

import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from importlib.machinery import SourceFileLoader


root = Path.cwd()
artifacts = Path(os.environ["ARTIFACTS"])
sys.path.insert(0, str(root / "dev/tools"))
loader = SourceFileLoader("test_unit", str(root / "dev/tools/test-unit"))
spec = importlib.util.spec_from_loader(loader.name, loader)
runner = importlib.util.module_from_spec(spec)
loader.exec_module(runner)

summaries = []
for suite in runner.PYTHON_TEST_SUITES:
    if sys.version_info < suite.get("min_python", (3, 0)):
        continue
    path = artifacts / f"coverage_unit-python-{suite['name']}.xml"
    report = ET.parse(path).getroot()
    classes = report.findall(".//class")
    assert classes, path
    assert all("_proto/" not in cls.attrib["filename"] for cls in classes), path
    summaries.append({"suite": suite["name"], "files": len(classes),
                      "lines_valid": int(report.attrib["lines-valid"]),
                      "lines_covered": int(report.attrib["lines-covered"])})
(artifacts / "coverage-summary.json").write_text(json.dumps(summaries, indent=2) + "\n")

timings = []
for name in ["k8s-agent-sandbox", "dev-tools-shared"]:
    suite = next(s for s in runner.PYTHON_TEST_SUITES if s["name"] == name)
    test_dir = root / suite["dir"]
    python = root / "bin" / f"python-venv-{name}" / "bin/python"
    for index, measured in enumerate([False, True, True, False, False, True]):
        mode = "coverage" if measured else "baseline"
        command = [str(python), "-m", "pytest", str(test_dir), "-q"]
        if measured:
            command += [f"--cov={test_dir}",
                        f"--cov-config={root / 'dev/tools/python-coverage.ini'}",
                        f"--cov-report=xml:{artifacts / f'overhead-{name}-{index}.xml'}"]
        else:
            command += ["-p", "no:cov"]
        env = os.environ.copy()
        env["COVERAGE_FILE"] = str(artifacts / f"overhead-{name}-{index}.data")
        start = time.monotonic()
        with (artifacts / f"overhead-{name}-{index}.log").open("w") as out:
            result = subprocess.run(command, cwd=test_dir, env=env, stdout=out, stderr=subprocess.STDOUT)
        duration = time.monotonic() - start
        assert result.returncode == 0, (name, mode, result.returncode)
        timings.append({"suite": name, "mode": mode, "seconds": duration})
summary = {name: {mode: statistics.median(t["seconds"] for t in timings
                                         if t["suite"] == name and t["mode"] == mode)
                  for mode in ["baseline", "coverage"]}
           for name in ["k8s-agent-sandbox", "dev-tools-shared"]}
(artifacts / "overhead.json").write_text(json.dumps({"runs": timings, "medians": summary}, indent=2) + "\n")
print(json.dumps({"reports": len(summaries), "median_seconds": summary}, indent=2))
