import contextlib
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import reproduce_evidence as tool


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "evidence"
        self.root.mkdir()
        self.repo = self.base / "candidate"
        self.repo.mkdir()
        self.git_toplevel = self.repo
        (self.repo / "go.mod").write_text("module sigs.k8s.io/agent-sandbox\n")
        self.output = self.base / "recomputed"

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())

    def checksums(self):
        names = sorted(path.relative_to(self.root).as_posix()
                       for path in self.root.rglob("*")
                       if path.is_file() and path.name != "SHA256SUMS")
        self.write("SHA256SUMS", "".join(
            f"{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names))

    def invoke(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = tool.main(["--evidence-root", str(self.root), "--source-repo", str(self.repo),
                              "--output", str(self.output), *extra])
        return code, json.loads(stdout.getvalue())

    def test_checksum_failure_precedes_copy_or_execution(self):
        self.write("data", "before")
        self.checksums()
        self.write("data", "after")
        with mock.patch.object(tool.subprocess, "Popen") as execute:
            code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("SHA256 mismatch", report["errors"][0])
        self.assertFalse(self.output.exists())
        execute.assert_not_called()

    def test_checksum_coverage_duplicates_and_unsafe_paths(self):
        self.write("data", "abc")
        self.checksums()
        valid = (self.root / "SHA256SUMS").read_text()
        for name in ("../escape", "/absolute", "a/../b", "a//b", "./data",
                     "C:/file", "a\\b", "bad\tname"):
            with self.subTest(name=name):
                self.write("SHA256SUMS", "0" * 64 + "  " + name + "\n")
                with self.assertRaises(ValueError):
                    tool.verify_tree(self.root)
        self.write("SHA256SUMS", valid + valid)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            tool.verify_tree(self.root)
        self.write("SHA256SUMS", valid)
        self.write("unlisted", "x")
        with self.assertRaisesRegex(ValueError, "coverage"):
            tool.verify_tree(self.root)
        (self.root / "unlisted").unlink()
        (self.root / "data").unlink()
        with self.assertRaisesRegex(ValueError, "coverage"):
            tool.verify_tree(self.root)

    def test_symlinks_in_tree_and_root_are_rejected(self):
        self.write("data", "abc")
        self.checksums()
        (self.root / "link").symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic"):
            tool.verify_tree(self.root)
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symbolic"):
            tool.verify_tree(alias)

    def test_output_is_fresh_and_outside_source(self):
        self.write("data", "abc")
        self.checksums()
        self.output.mkdir()
        (self.output / "keep").write_text("original")
        code, _ = self.invoke()
        self.assertEqual(code, 1)
        self.assertEqual((self.output / "keep").read_text(), "original")
        self.output = self.root / "new"
        self.assertEqual(self.invoke()[0], 1)
        self.output = self.repo / "new"
        self.assertEqual(self.invoke()[0], 1)
        self.output = self.base / "linked"
        self.output.symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.invoke()[0], 1)

    def test_single_layer_gzip_and_opaque_pprof(self):
        nested = gzip.compress(b"inner")
        profile = gzip.compress(b"opaque profile fixture")
        self.write("raw.prom.gz", gzip.compress(b"metric 1\n"))
        self.write("nested.gz.gz", gzip.compress(nested))
        self.write("nested.gz", gzip.compress(b"neighbor"))
        self.write("cpu.pprof", profile)
        self.checksums()
        entries = tool.verify_tree(self.root)
        self.output.mkdir()
        tool.rebuild(self.root, self.output, entries)
        self.assertEqual((self.output / "raw.prom").read_bytes(), b"metric 1\n")
        self.assertEqual((self.output / "nested.gz").read_bytes(), nested)
        self.assertEqual((self.output / "nested").read_bytes(), b"neighbor")
        self.assertEqual((self.output / "cpu.pprof").read_bytes(), profile)
        self.assertFalse((self.output / "raw.prom.gz").exists())
        self.assertEqual(tool.verify_tree(self.root), entries)

    def test_collisions_and_reserved_outputs(self):
        for names in (("a.gz", "a"), ("a.gz", "a/b"), ("verify.json",),
                      ("tools/analyze_profiles",), ("reproduce-logs/0",), ("SHA256SUMS.gz",),
                      ("verify.json/child",), ("tools",), ("tools/analyze_profiles/child",)):
            with self.subTest(names=names), self.assertRaises(ValueError):
                tool.expanded_names(dict.fromkeys(names, "digest"))

    def test_changed_source_during_copy_is_rejected(self):
        self.write("data", "first")
        self.checksums()
        entries = tool.verify_tree(self.root)
        self.write("data", "changed")
        self.output.mkdir()
        with self.assertRaisesRegex(ValueError, "changed during copy"):
            tool.rebuild(self.root, self.output, entries)

    def test_corrupt_gzip_fails_with_diagnostic(self):
        self.write("raw.gz", gzip.compress(b"sample")[:-4])
        self.checksums()
        with mock.patch.object(tool, "run_command") as execute:
            code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("EOFError", report["errors"][0])
        self.assertTrue((self.output / "verify.json").is_file())
        execute.assert_not_called()

    def test_json_rejects_duplicates_and_nonfinite_numbers(self):
        for value in ('{"n": 1, "n": 2}', '{"n": NaN}', '{"n": Infinity}'):
            self.write("data.json", value)
            with self.assertRaises(ValueError):
                tool.read_json(self.root / "data.json")

    def test_path_normalization_preserves_statistics_and_units(self):
        expected = {"runs/a": {"file": "runs/a", "n": 452, "p50": 0.1, "unit": "ns"}}
        actual = {str(self.output / "runs/a"): {
            "file": str(self.output / "runs/a"), "n": 452, "p50": 0.1 + 1e-15, "unit": "ns"}}
        normalized = tool.normalize(actual, [self.output])
        self.assertEqual(tool.differences(expected, normalized), [])
        normalized["runs/a"]["n"] = 453
        normalized["runs/a"]["unit"] = "ms"
        self.assertEqual(tool.differences(expected, normalized), ["$/runs~1a/n", "$/runs~1a/unit"])
        self.assertEqual(tool.differences(1, True), ["$"])
        self.assertEqual(tool.differences(10**18, 10**18 + 1), ["$"])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            tool.normalize({"runs/a": 1, str(self.output / "runs/a"): 2}, [self.output])

    def fixture(self, pacing=True):
        self.reference = {"runs": [{"created": 452, "p50": 5.25}], "file": "runs/a"}
        for name in tool.REQUIRED_REPORTS:
            self.write(name, json.dumps(self.reference))
        self.write(tool.INVENTORY, json.dumps({"verification": {
            "cold_snapshots_recounted": 47, "pod_timing_rows_recomputed": 2}}))
        for suite in ("results", "results-unprofiled"):
            self.write(f"{tool.Q}/{suite}/manifest.json", json.dumps({
                "status": "completed", "windows_monitor_exit": 0,
                "runs": [{"sample": "a", "status": "completed"}]}))
            if pacing:
                self.write(f"{tool.Q}/{suite}/arrival-audit.json", json.dumps(self.reference))
                self.write(f"{tool.Q}/{suite}/schedule-r8-s60.json", '{"offsets_ns": [1, 2]}')
        self.commit = "a" * 40
        self.write("runs/a/config.json", json.dumps({"revision": "candidate", "commit": self.commit}))
        self.write("runs/a/raw.prom.gz", gzip.compress(b"metric 1\n"))
        for source, _ in tool.HELPERS:
            self.write(source, "package main\n")
        self.checksums()

    def fake_command(self, argv, cwd, env, output, report, timeout):
        self.calls.append((argv, cwd, env))
        log = output / "mock-command.log"
        report["commands"].append({"argv": argv, "status": "complete",
                                   "exit_code": 0, "log": log.name})
        if argv[:3] == ["git", "rev-parse", "--show-toplevel"]:
            log.write_text(str(self.git_toplevel) + "\n")
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            log.write_text(self.commit)
        if argv[0] == "/custom/go":
            Path(argv[argv.index("-o") + 1]).write_text("mock binary")
        if len(argv) > 2 and argv[2].endswith(".py"):
            script = argv[2]
            names = {
                "summarize.py": tool.REQUIRED_REPORTS[:3],
                "summarize_unprofiled.py": tool.REQUIRED_REPORTS[3:4],
                "analyze_rest.py": tool.REQUIRED_REPORTS[4:],
                "validate_pacing.py": (f"{tool.Q}/{argv[3]}/arrival-audit.json",),
            }.get(script, ())
            for name in names:
                self.assertFalse((output / name).exists())
                result = dict(self.reference, file=str(output / "runs/a"))
                (output / name).write_text(json.dumps(result))
            if script == "diagnose_inventory.py":
                log.write_text(json.dumps({"cold_snapshots_recounted": 47,
                                          "pod_timing_rows_recomputed": 2}))
            if script == "validate_pacing.py":
                (output / tool.Q / argv[3] / "schedule-r8-s60.json").write_text(
                    '{"offsets_ns": [1, 2]}')

    def test_mock_full_reproduction_and_build_cwd(self):
        self.fixture()
        self.calls = []
        original = tool.verify_tree(self.root)
        with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
            code, report = self.invoke(["--go", "/custom/go"])
        self.assertEqual(code, 0, report["errors"])
        self.assertEqual(report["status"], "verified")
        self.assertTrue(all(item["status"] == "match" for item in report["comparisons"]))
        self.assertEqual(report["inventory"]["status"], "verified_public")
        builds = [item for item in self.calls if item[0][0] == "/custom/go"]
        self.assertEqual(len(builds), 3)
        for argv, cwd, env in builds:
            self.assertEqual(cwd, self.repo)
            self.assertIn("-mod=readonly", argv)
            self.assertEqual(env["GOPROXY"], "off")
            self.assertEqual(env["GOTOOLCHAIN"], "local")
            self.assertTrue(Path(argv[-1]).is_relative_to(self.output))
        scripts = [argv[2] for argv, _, _ in self.calls if len(argv) > 2 and argv[1] == "-B"]
        self.assertEqual(scripts, ["summarize.py", "summarize_unprofiled.py", "analyze_rest.py",
                                  "diagnose_inventory.py", "validate_pacing.py", "validate_pacing.py"])
        self.assertEqual(tool.verify_tree(self.root), original)
        self.assertEqual(json.loads((self.output / "verify.json").read_text()), report)

    def test_source_submodule_with_same_commit_is_rejected(self):
        self.fixture()
        self.repo = self.repo / "examples" / "sandboxed-tools"
        self.repo.mkdir(parents=True)
        (self.repo / "go.mod").write_text("module example.test/sandboxed-tools\n")
        self.calls = []
        with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
            code, report = self.invoke(["--go", "/custom/go"])
        self.assertEqual(code, 1)
        self.assertIn("top-level directory", report["errors"][0])
        self.assertEqual([argv for argv, _, _ in self.calls],
                         [["git", "rev-parse", "--show-toplevel"]])

    def test_wrong_root_module_is_rejected_before_build(self):
        self.fixture()
        (self.repo / "go.mod").write_text("module example.test\n")
        self.calls = []
        with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
            code, report = self.invoke(["--go", "/custom/go"])
        self.assertEqual(code, 1)
        self.assertIn("sigs.k8s.io/agent-sandbox root module", report["errors"][0])
        self.assertEqual([argv for argv, _, _ in self.calls],
                         [["git", "rev-parse", "--show-toplevel"]])

    def test_polluted_git_and_go_environment_is_sanitized(self):
        self.fixture()
        self.calls = []
        pollution = dict.fromkeys((
            "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_NAMESPACE",
            "GIT_CEILING_DIRECTORIES", "GIT_REPLACE_REF_BASE",
        ), "/redirected")
        pollution.update(GOPROXY="direct", GONOPROXY="*", GOPRIVATE="*",
                         GONOSUMDB="*", GOTOOLCHAIN="auto", GOWORK="/other/go.work",
                         GOFLAGS="-mod=mod", GOENV="/other/go.env")
        with mock.patch.dict(tool.os.environ, pollution):
            with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
                code, report = self.invoke(["--go", "/custom/go"])
            self.assertEqual(tool.os.environ["GONOPROXY"], "*")
            self.assertEqual(tool.os.environ["GIT_DIR"], "/redirected")
        self.assertEqual(code, 0, report["errors"])
        for _, _, env in self.calls:
            self.assertFalse(any(key.startswith("GIT_") for key in env))
            for key, value in {
                "GOPROXY": "off", "GONOPROXY": "none", "GOPRIVATE": "",
                "GONOSUMDB": "none", "GOSUMDB": "off", "GOTOOLCHAIN": "local",
                "GOWORK": "off", "GOFLAGS": "", "GOENV": "off",
            }.items():
                self.assertEqual(env[key], value, key)

    def test_optional_pacing_is_marked_uncompared(self):
        self.fixture(pacing=False)
        self.calls = []
        with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
            code, report = self.invoke(["--go", "/custom/go"])
        self.assertEqual(code, 0, report["errors"])
        self.assertEqual(sum(row["status"] == "generated_without_reference"
                             for row in report["comparisons"]), 2)

    def test_statistical_mismatch_fails(self):
        self.fixture()
        self.calls = []
        def changed(*args):
            self.fake_command(*args)
            if args[0][2:3] == ["summarize_unprofiled.py"]:
                (args[3] / tool.REQUIRED_REPORTS[3]).write_text('{"runs": []}')
        with mock.patch.object(tool, "run_command", side_effect=changed):
            code, report = self.invoke(["--go", "/custom/go"])
        self.assertEqual(code, 1)
        self.assertTrue(any(row["status"] == "different" for row in report["comparisons"]))
        self.assertEqual(report["status"], "failed")

    def test_missing_report_and_failed_command_are_recorded(self):
        self.fixture()
        (self.root / tool.REQUIRED_REPORTS[0]).unlink()
        self.checksums()
        code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("FileNotFoundError", report["errors"][0])
        self.output = self.base / "another"
        self.write(tool.REQUIRED_REPORTS[0], "{}")
        self.checksums()
        with mock.patch.object(tool, "run_command", side_effect=ValueError("command failed")):
            code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("command failed", report["errors"][0])
        self.assertTrue((self.output / "verify.json").is_file())

    def test_incomplete_manifest_and_wrong_candidate_fail_before_build(self):
        self.fixture()
        manifest = self.root / tool.Q / "results/manifest.json"
        document = json.loads(manifest.read_text())
        document["status"] = "running"
        manifest.write_text(json.dumps(document))
        self.checksums()
        with mock.patch.object(tool, "run_command") as execute:
            code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("Completed monitored suite required", report["errors"][0])
        execute.assert_not_called()
        self.output = self.base / "another"
        document["status"] = "completed"
        manifest.write_text(json.dumps(document))
        self.checksums()
        self.commit = "b" * 40
        self.calls = []
        with mock.patch.object(tool, "run_command", side_effect=self.fake_command):
            code, report = self.invoke()
        self.assertEqual(code, 1)
        self.assertIn("HEAD differs", report["errors"][0])
        self.assertEqual(len(self.calls), 2)

    def test_command_nonzero_and_start_failure_are_recorded(self):
        self.output.mkdir()
        report = {"commands": []}
        with mock.patch.object(tool.subprocess, "Popen", return_value=mock.Mock(
                wait=mock.Mock(return_value=7))):
            with self.assertRaisesRegex(ValueError, "exit code 7"):
                tool.run_command(["fake"], self.repo, {}, self.output, report, 1)
        self.assertEqual(report["commands"][0]["exit_code"], 7)
        with mock.patch.object(tool.subprocess, "Popen", side_effect=FileNotFoundError("fake")):
            with self.assertRaises(FileNotFoundError):
                tool.run_command(["missing"], self.repo, {}, self.output, report, 1)
        self.assertEqual(report["commands"][1]["status"], "failed_to_start")

    def test_timeout_kills_owned_process_group(self):
        self.output.mkdir()
        process = mock.Mock(pid=23456)
        process.wait.side_effect = [subprocess.TimeoutExpired(["fake"], 1), 0]
        report = {"commands": []}
        with mock.patch.object(tool.subprocess, "Popen", return_value=process) as popen:
            with mock.patch.object(tool.os, "killpg") as kill:
                with self.assertRaises(subprocess.TimeoutExpired):
                    tool.run_command(["fake"], self.repo, {}, self.output, report, 1)
        kill.assert_called_once_with(23456, tool.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(report["commands"][0]["status"], "interrupted_or_timed_out")


if __name__ == "__main__":
    unittest.main()
