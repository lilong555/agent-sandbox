"""Collect project checks and regression evidence for the WarmPool UID fix."""

import json
import os
from pathlib import Path
import subprocess
import sys


root = Path.cwd()
artifacts = root / "_artifacts"
artifacts.mkdir(exist_ok=True)
source = subprocess.check_output(["git", "rev-parse", os.environ["SOURCE_COMMIT"]], text=True).strip()
subprocess.run(["git", "diff", "--exit-code", source, "HEAD", "--", "extensions/controllers"], check=True)
(artifacts / "source-commit.txt").write_text(source + "\n")


def run(name, command, env=None, expect_success=True):
    print(f"Running {name}", flush=True)
    with (artifacts / f"{name}.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=env)
    print((artifacts / f"{name}.log").read_text()[-8000:], flush=True)
    if expect_success and result.returncode:
        sys.exit(result.returncode)
    return result.returncode


if sys.argv[1] == "all":
    run("make-all", ["make", "all"])
    run("generated-diff", ["git", "diff", "--exit-code"])
else:
    run("focused", ["go", "test", "-race", "-count=3", "-shuffle=on", "-json", "-timeout=300s",
                    "-run", "^(TestWarmPoolExpectations.*|TestReconcilePool_.*|TestWarmPoolSandboxEventHandler.*)$",
                    "./extensions/controllers"])
    archive = artifacts / "envtest.tar.gz"
    assets = artifacts / "envtest"
    assets.mkdir(exist_ok=True)
    run("download-envtest", ["curl", "-fLsS", "--retry", "2", "--max-time", "120",
                             "https://github.com/kubernetes-sigs/controller-tools/releases/download/"
                             "envtest-v1.36.2/envtest-v1.36.2-linux-amd64.tar.gz", "-o", str(archive)])
    run("unpack-envtest", ["tar", "-xzf", str(archive), "-C", str(assets)])
    archive.unlink()
    overlay = artifacts / "overlay.json"
    overlay.write_text(json.dumps({"Replace": {
        str(root / "extensions/controllers/warmpool_uid_real_probe_test.go"):
            str(root / ".github/warmpool-uid/real_probe_test.go"),
    }}, indent=2) + "\n")
    env = os.environ.copy()
    env["KUBEBUILDER_ASSETS"] = str(next(assets.rglob("kube-apiserver")).parent)
    run("real-cache", ["go", "test", "-race", "-count=3", "-shuffle=on", "-json", "-timeout=180s",
                       "-overlay", str(overlay), "-run", "^TestWarmPoolUID.*$", "./extensions/controllers"], env)
