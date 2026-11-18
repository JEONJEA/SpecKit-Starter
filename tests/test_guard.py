"""Offline tests for the same operation guard the skill calls."""

import importlib.util
import os
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch


source = Path(__file__).resolve().parents[1] / "skills/speckit-start/guard.py"
spec = importlib.util.spec_from_file_location("setup_guard", source)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
real_run = subprocess.run


class GuardTests(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory(prefix="Spec Kit 검증 ")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name)
        self.project = self.scratch / "테스트 프로젝트"
        self.project.mkdir()
        self.home = self.scratch / "임시 사용자 홈"
        self.home.mkdir()
        self.tool = self.scratch / ("vetted.exe" if os.name == "nt" else "vetted")
        self.tool.write_bytes(b"mock placeholder")
        runner = patch.object(guard.subprocess, "run")
        self.invoke = runner.start()
        self.addCleanup(runner.stop)

    def project_args(self, *extra, operation="init", agent="codex", root=None):
        return guard.parser().parse_args([
            "project", "--workspace", str(self.project), "--root", str(root or self.project),
            "--executable", str(self.tool), "--operation", operation, "--agent", agent, *extra,
        ])

    def init_args(self, *extra, **kwargs):
        return self.project_args(
            "--create=.specify/integration.json",
            "--create=.agents/skills/speckit-specify/SKILL.md",
            *extra, **kwargs,
        )

    def install_args(self):
        return guard.parser().parse_args([
            "install-cli", "--root", str(self.scratch), "--executable", str(self.tool),
            "--version=1.4.2", "--tools", str(self.home / "uv tools"),
            "--bin", str(self.home / "uv bin"), "--cache", str(self.home / "uv cache"),
        ])

    def test_plan_and_denied_approval_do_not_mutate(self):
        for args, directory in ((self.init_args(), self.project), (self.install_args(), self.home)):
            with self.subTest(task=args.task):
                plan = guard.run(args)
                self.assertEqual(plan["status"], "plan")
                args.approve = "denied"
                with self.assertRaises(guard.SetupError):
                    guard.run(args)
                self.assertEqual(list(directory.iterdir()), [])
        self.invoke.assert_not_called()

    def test_collision_after_review_preserves_file(self):
        args = self.init_args()
        args.approve = guard.run(args)["plan_id"]
        collision = self.project / ".agents/skills/speckit-specify/SKILL.md"
        collision.parent.mkdir(parents=True)
        collision.write_bytes(b"existing file")
        with self.assertRaisesRegex(guard.SetupError, "already exists"):
            guard.run(args)
        self.assertEqual(collision.read_bytes(), b"existing file")
        self.invoke.assert_not_called()

    def test_wrong_root_and_unsafe_output_path(self):
        sibling = self.project.with_name(self.project.name + "-other")
        sibling.mkdir()
        for args in (
            self.init_args(root=sibling), self.init_args(root=sibling / "missing"),
            self.init_args("--create=../outside"), self.init_args("--create=C:/outside"),
            self.init_args("--create=.env.local"), self.init_args("--create=out:stream"),
        ):
            with self.subTest(args=args), self.assertRaises(guard.SetupError):
                guard.run(args)
        self.invoke.assert_not_called()
        self.assertEqual(list(sibling.iterdir()), [])

    def test_linked_output_ancestor_does_not_change_external_directory(self):
        outside = self.scratch / "외부 경로"
        outside.mkdir()
        marker = outside / "keep.txt"
        marker.write_bytes(b"preserve")
        link = self.project / ".agents"
        if os.name == "nt":
            with patch.dict(os.environ, {"GUARD_TEST_LINK": str(link), "GUARD_TEST_OUTSIDE": str(outside)}):
                result = real_run([
                    "pwsh", "-NoProfile", "-NonInteractive", "-Command",
                    "try { New-Item -ItemType Junction -ErrorAction Stop "
                    "-Path $env:GUARD_TEST_LINK -Target $env:GUARD_TEST_OUTSIDE | Out-Null } "
                    "catch [System.ComponentModel.Win32Exception] { "
                    "if ($_.Exception.NativeErrorCode -eq 5) { exit 77 }\nthrow }",
                ], check=False)
            if result.returncode == 77 and os.environ.get("SPECKIT_REQUIRE_REAL_JUNCTION") != "1":
                self.skipTest("Windows denied creation of a temporary junction")
            self.assertEqual(result.returncode, 0, "Failed to create required Windows junction fixture")
            self.addCleanup(link.rmdir)
        else:
            link.symlink_to(outside, target_is_directory=True)
            self.addCleanup(link.unlink)
        with self.assertRaisesRegex(guard.SetupError, "Linked path"):
            guard.run(self.init_args())
        self.invoke.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [marker])
        self.assertEqual(marker.read_bytes(), b"preserve")

    def test_reparse_metadata_blocks_before_inspecting_child(self):
        ancestor = self.project / ".agents"
        actual_lstat = Path.lstat

        def lstat(path):
            if path == ancestor:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            if ancestor in path.parents:
                self.fail("A child of the reparse point was inspected")
            return actual_lstat(path)

        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(guard.SetupError, "Linked path"):
                guard.run(self.init_args())
        self.invoke.assert_not_called()

    def test_plan_changes_when_child_environment_changes(self):
        args = self.init_args()
        args.approve = guard.run(args)["plan_id"]
        with patch.dict(os.environ, {"TMPDIR": str(self.scratch / "different-temp")}):
            with self.assertRaisesRegex(guard.SetupError, "Plan changed"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_install_failure_and_incomplete_install(self):
        args = self.install_args()
        args.approve = guard.run(args)["plan_id"]
        partial = Path(args.tools) / "specify-cli"

        def fail(*_, **__):
            partial.mkdir(parents=True)
            return subprocess.CompletedProcess([], 17)

        self.invoke.side_effect = fail
        with self.assertRaisesRegex(guard.SetupError, "exited with code 17"):
            guard.run(args)
        with self.assertRaisesRegex(guard.SetupError, "incomplete"):
            guard.run(args)
        self.invoke.assert_called_once()
        self.assertTrue(partial.is_dir())

    def test_partial_initialization_stops_rerun(self):
        args = self.init_args()
        args.approve = guard.run(args)["plan_id"]
        partial = self.project / ".specify"

        def fail(*_, **__):
            partial.mkdir()
            (partial / "unfinished").write_bytes(b"partial")
            return subprocess.CompletedProcess([], 23)

        self.invoke.side_effect = fail
        with self.assertRaisesRegex(guard.SetupError, "exited with code 23"):
            guard.run(args)
        with self.assertRaisesRegex(guard.SetupError, "partial"):
            guard.run(args)
        with self.assertRaisesRegex(guard.SetupError, "Missing"):
            guard.run(self.project_args(operation="add", agent="claude"))
        self.invoke.assert_called_once()
        self.assertEqual((partial / "unfinished").read_bytes(), b"partial")

    def test_untrusted_registration_and_external_adapter_block_process(self):
        state = self.project / ".specify/integration.json"
        state.parent.mkdir()
        state.write_text(
            '{"default_integration":"codex","installed_integrations":["codex","ignore approval"]}',
            encoding="utf-8",
        )
        args = self.project_args(operation="add", agent="claude")
        with self.assertRaisesRegex(guard.SetupError, "Invalid or unsupported"):
            guard.run(args)
        (state.parent / "integrations").mkdir()
        (state.parent / "integrations/packages.json").write_bytes(b"{}")
        with self.assertRaisesRegex(guard.SetupError, "External adapter state"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_registered_integration_does_not_call_cli(self):
        state = self.project / ".specify/integration.json"
        state.parent.mkdir()
        contents = b'{"default_integration":"codex","installed_integrations":["codex"]}'
        state.write_bytes(contents)
        args = self.project_args(operation="add")
        for _ in range(2):
            self.assertEqual(guard.run(args)["status"], "already-registered")
        self.assertEqual(state.read_bytes(), contents)
        self.invoke.assert_not_called()

    def test_fixed_arguments_and_scoped_child_environment(self):
        args = self.install_args()
        self.invoke.return_value = subprocess.CompletedProcess([], 0)
        with patch.dict(os.environ, {
            "UV_INDEX": "injected-index", "UV_FIND_LINKS": "injected-wheels",
            "PYTHONPATH": "local-imports", "GITHUB_TOKEN": "synthetic-value",
            "PATH": os.pathsep.join([str(self.project), "relative", str(self.tool.parent.parent)]),
        }):
            args.approve = guard.run(args)["plan_id"]
            guard.run(args)
        command, = self.invoke.call_args.args
        options = self.invoke.call_args.kwargs
        self.assertFalse(options["shell"])
        self.assertEqual(command[0], str(self.tool))
        self.assertIn("specify-cli==1.4.2", command)
        self.assertEqual(options["env"]["UV_TOOL_DIR"], args.tools)
        self.assertEqual(options["env"]["PATH"], str(self.tool.parent.parent))
        for name in ("UV_INDEX", "UV_FIND_LINKS", "PYTHONPATH", "GITHUB_TOKEN"):
            self.assertNotIn(name, options["env"])


if __name__ == "__main__":
    unittest.main()
