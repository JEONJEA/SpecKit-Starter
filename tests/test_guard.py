"""Offline tests for the same operation guard the skill calls."""

import importlib.util
import base64
import hashlib
import io
import zipfile
import json
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
        self.preview = self.scratch / "미리 보기"
        self.preview.mkdir()
        self.files = (".specify/integration.json", ".agents/skills/speckit-specify/SKILL.md")
        for name in self.files:
            self.write_fixture(self.preview, name, "scratch output")
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
            "--executable", str(self.tool), "--operation", operation, "--agent", agent,
            "--preview", str(self.preview), *extra,
        ])

    def write_fixture(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

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

    def uv_tool_args(self, *, include_launcher=True, list_version=None):
        tools = self.scratch / "uv tools"
        bin_dir = self.scratch / "uv bin"
        tool_root = tools / "specify-cli"
        dist_info = tool_root / (
            "Lib/site-packages/specify_cli-1.4.2.dist-info" if os.name == "nt"
            else "lib/python3.12/site-packages/specify_cli-1.4.2.dist-info"
        )
        launcher = bin_dir / ("specify.exe" if os.name == "nt" else "specify")
        cache = self.scratch / "uv cache"
        cache_record = cache / "wheels-v6/pypi/specify-cli/1.4.2-py3-none-any.http"
        cache_record.parent.mkdir(parents=True, exist_ok=True)
        cache_record.write_bytes(
            b"https://files.pythonhosted.org/packages/fixture/specify_cli-1.4.2-py3-none-any.whl"
        )
        backing = tool_root / ("Scripts/specify.exe" if os.name == "nt" else "bin/specify")
        launcher.parent.mkdir(parents=True, exist_ok=True)
        backing.parent.mkdir(parents=True, exist_ok=True)
        dist_info.mkdir(parents=True, exist_ok=True)
        content = b"unsigned launcher fixture"
        launcher.write_bytes(content)
        backing.write_bytes(content)
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        record_path = os.path.relpath(backing, dist_info.parent).replace(os.sep, "/")
        (dist_info / "RECORD").write_text(
            f"{record_path},sha256={digest},{len(content)}\n", encoding="utf-8"
        )
        (dist_info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: specify-cli\nVersion: 1.4.2\n", encoding="utf-8"
        )
        (dist_info / "entry_points.txt").write_text(
            "[console_scripts]\nspecify = specify_cli:main\n", encoding="utf-8"
        )
        package = dist_info.parent / "specify_cli"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_bytes(b"# official fixture")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as wheel:
            for path in (*dist_info.iterdir(), package / "__init__.py"):
                wheel.write(path, path.relative_to(dist_info.parent).as_posix())
        payload = buffer.getvalue()
        filename = "specify_cli-1.4.2-py3-none-any.whl"
        url = "https://files.pythonhosted.org/packages/fixture/" + filename
        release = {"urls": [{"url": url, "filename": filename,
                             "digests": {"sha256": hashlib.sha256(payload).hexdigest()}}]}
        responses = {"https://pypi.org/pypi/specify-cli/1.4.2/json": json.dumps(release).encode(), url: payload}
        self.enterContext(patch.object(guard, "PINNED_RELEASES", {}))
        self.enterContext(patch.object(guard, "fetch_release_bytes", side_effect=lambda url, limit: responses[url]))
        receipt = tool_root / "uv-receipt.toml"
        receipt.write_text(
            '[tool]\n'
            'requirements = [{ name = "specify-cli" }]\n'
            'entrypoints = [{ name = "specify", install-path = "'
            + launcher.as_posix()
            + '", from = "specify-cli" }]\n',
            encoding="utf-8",
        )
        arguments = [
            "verify-uv-tool", "--tools", str(tools), "--bin", str(bin_dir), "--cache", str(cache),
        ]
        if include_launcher:
            arguments.extend(["--launcher", str(launcher)])
        if list_version:
            arguments.extend(["--list-version", list_version])
        return guard.parser().parse_args(arguments), launcher, backing

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

    def test_incomplete_force_plan_does_not_start_cli(self):
        self.write_fixture(self.project, "README.md", "user file")
        args = self.project_args("--create=.specify/integration.json")
        with self.assertRaisesRegex(guard.SetupError, "do not match the scratch preview"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_missing_or_stale_shared_file_blocks_add(self):
        version = "1.4.2"
        tracked = ".specify/templates/spec-template.md"
        stale = ".specify/scripts/powershell/old.ps1"
        shared = {"version": version, "files": {tracked: "recorded hash"}}
        options = {"speckit_version": version, "ai": "codex", "script": "ps"}
        names = (".claude/skills/speckit-specify/SKILL.md", ".specify/integrations/claude.manifest.json")
        for name in (*names, tracked):
            self.write_fixture(self.preview, name, "scratch output")
        for root in (self.preview, self.project):
            self.write_fixture(root, guard.SHARED_MANIFEST, json.dumps(shared))
            self.write_fixture(root, guard.INIT_OPTIONS, json.dumps(options))
        self.write_fixture(self.project, ".specify/integration.json", json.dumps({
            "default_integration": "codex", "installed_integrations": ["codex"],
        }))
        args = self.project_args(
            *[f"--create={name}" for name in names],
            *[f"--update={name}" for name in sorted(guard.METADATA)], operation="add", agent="claude",
        )
        with self.assertRaisesRegex(guard.SetupError, "Missing shared infrastructure"):
            guard.run(args)
        self.write_fixture(self.project, tracked, "retained template")
        args.approve = guard.run(args)["plan_id"]
        self.write_fixture(self.project, stale, "retained script")
        shared["files"][stale] = "old hash"
        self.write_fixture(self.project, guard.SHARED_MANIFEST, json.dumps(shared))
        with self.assertRaisesRegex(guard.SetupError, "Shared infrastructure differs"):
            guard.run(args)
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
            with self.assertRaisesRegex(guard.SetupError, "Plan changed.*child environment"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_path_added_after_plan_is_reported_as_environment_change(self):
        args = self.init_args()
        with patch.dict(os.environ, {"PATH": str(self.scratch / "base")}):
            args.approve = guard.run(args)["plan_id"]
        added = os.pathsep.join((str(self.scratch / "base"), str(self.scratch / "Git" / "cmd")))
        with patch.dict(os.environ, {"PATH": added}):
            with self.assertRaises(guard.SetupError) as raised:
                guard.run(args)
        message = str(raised.exception)
        self.assertIn("child environment", message)
        self.assertIn("same environment", message)
        self.assertNotIn(str(self.scratch), message)
        self.invoke.assert_not_called()

    def test_project_change_after_plan_is_reported_as_operation_change(self):
        args = self.init_args()
        args.approve = guard.run(args)["plan_id"]
        (self.project / "notes.md").write_bytes(b"added after review")
        with self.assertRaisesRegex(guard.SetupError, "Plan changed.*command, arguments or target file state"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_plan_and_environment_change_together_are_both_reported(self):
        args = self.init_args()
        args.approve = guard.run(args)["plan_id"]
        (self.project / "notes.md").write_bytes(b"added after review")
        with patch.dict(os.environ, {"TMPDIR": str(self.scratch / "different-temp")}):
            with self.assertRaisesRegex(guard.SetupError, "Plan changed.*all differ"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_plan_id_is_stable_and_malformed_approval_is_identified(self):
        args = self.init_args()
        first = guard.run(args)["plan_id"]
        self.assertRegex(first, r"^[0-9a-f]{32}-[0-9a-f]{32}$")
        self.assertEqual(guard.run(args)["plan_id"], first)
        args.approve = first.replace("-", "")
        with self.assertRaisesRegex(guard.SetupError, "not a plan ID from this guard"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_unsigned_uv_launcher_is_verified_from_receipt_record_and_official_cache(self):
        args, _, _ = self.uv_tool_args(list_version="1.4.2")
        result = guard.run(args)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["package"], "specify-cli")
        self.assertEqual(result["version"], "1.4.2")
        self.assertEqual(result["index_source"], "files.pythonhosted.org (uv wheel cache)")
        self.assertFalse(result["signature_required"])
        self.invoke.assert_not_called()

    def test_pinned_release_checks_content_without_network(self):
        args, _, _ = self.uv_tool_args()
        dist = guard.site_packages(Path(args.tools) / "specify-cli")
        (dist / "WHEEL").write_bytes(b"fixture")
        (dist / "licenses").mkdir()
        (dist / "licenses/LICENSE").write_bytes(b"fixture")
        with patch.dict(guard.PINNED_RELEASES, {"1.4.2": guard.installed_release_digest(dist)}):
            guard.run(args)
            (dist.parent / "specify_cli/__init__.py").write_bytes(b"changed")
            with self.assertRaisesRegex(guard.SetupError, "differ from pinned"):
                guard.run(args)
        guard.fetch_release_bytes.assert_not_called()

    def test_same_version_modified_package_is_rejected(self):
        args, _, _ = self.uv_tool_args()
        dist = guard.site_packages(Path(args.tools) / "specify-cli")
        (dist.parent / "specify_cli/__init__.py").write_bytes(b"# different build")
        with self.assertRaisesRegex(guard.SetupError, "differs from official wheel"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_official_wheel_digest_mismatch_blocks_execution(self):
        args, _, _ = self.uv_tool_args()
        fetch = guard.fetch_release_bytes
        def changed_download(url, limit):
            data = fetch(url, limit)
            return data + b"changed" if url.endswith(".whl") else data
        with patch.object(guard, "fetch_release_bytes", side_effect=changed_download):
            with self.assertRaisesRegex(guard.SetupError, "wheel digest mismatch"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_unrecorded_origin_requires_matching_official_cache_evidence(self):
        args, _, _ = self.uv_tool_args()
        cache_record = Path(args.cache) / "wheels-v6/pypi/specify-cli/1.4.2-py3-none-any.http"
        cache_record.unlink()
        with self.assertRaisesRegex(guard.SetupError, "No matching uv wheel cache source record"):
            guard.run(args)

        cache_record.write_bytes(
            b"https://packages.example/specify_cli-1.4.2-py3-none-any.whl"
        )
        with self.assertRaisesRegex(guard.SetupError, "nonofficial or mismatched"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_official_receipt_index_is_sufficient_without_cache(self):
        args, _, _ = self.uv_tool_args()
        cache_record = Path(args.cache) / "wheels-v6/pypi/specify-cli/1.4.2-py3-none-any.http"
        cache_record.unlink()
        receipt = Path(args.tools) / "specify-cli" / "uv-receipt.toml"
        with receipt.open("a", encoding="utf-8") as stream:
            stream.write('\n[tool.options]\nindex = [{ url = "https://pypi.org/simple" }]\n')
        result = guard.run(args)
        self.assertEqual(result["index_source"], "pypi.org")
        self.invoke.assert_not_called()

    def test_inaccessible_cache_source_fails_closed(self):
        args, _, _ = self.uv_tool_args()
        cache_record = Path(args.cache) / "wheels-v6/pypi/specify-cli/1.4.2-py3-none-any.http"
        real_read_bytes = Path.read_bytes

        def deny_cache_read(path):
            if path == cache_record:
                raise PermissionError("synthetic cache access denial")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", deny_cache_read):
            with self.assertRaisesRegex(guard.SetupError, "Could not read uv wheel cache source record"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_recorded_nonofficial_index_is_rejected(self):
        args, _, _ = self.uv_tool_args()
        receipt = Path(args.tools) / "specify-cli" / "uv-receipt.toml"
        with receipt.open("a", encoding="utf-8") as stream:
            stream.write('\n[tool.options]\nindex = [{ url = "https://packages.example/simple" }]\n')
        with self.assertRaisesRegex(guard.SetupError, "nonofficial package index"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_path_missing_and_uv_list_lock_failure_use_receipt(self):
        args, launcher, _ = self.uv_tool_args(include_launcher=False)
        with patch.dict(os.environ, {"PATH": ""}):
            result = guard.run(args)
        self.assertEqual(Path(result["launcher"]), launcher)
        self.assertEqual(result["status"], "verified")
        self.invoke.assert_not_called()

    def test_uv_hash_access_denied_does_not_block_receipt_verification(self):
        args, _, _ = self.uv_tool_args()
        uv_executable = self.scratch / "uv.exe"
        uv_executable.write_bytes(b"uv fixture")
        real_read_bytes = Path.read_bytes

        def deny_uv_read(path):
            if path == uv_executable:
                raise PermissionError("synthetic uv hash denial")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", deny_uv_read):
            self.assertEqual(guard.run(args)["status"], "verified")
        self.invoke.assert_not_called()

    def test_unregistered_launcher_and_inaccessible_candidate_fail_closed(self):
        args, launcher, _ = self.uv_tool_args()
        unknown = launcher.with_name("unknown.exe" if os.name == "nt" else "unknown")
        unknown.write_bytes(b"unknown launcher")
        args.launcher = str(unknown)
        with self.assertRaisesRegex(guard.SetupError, "differs from the uv receipt"):
            guard.run(args)

        args.launcher = str(launcher)
        real_read_bytes = Path.read_bytes

        def deny_launcher_read(path):
            if path == launcher:
                raise PermissionError("synthetic launcher read denial")
            return real_read_bytes(path)

        with patch.object(Path, "read_bytes", deny_launcher_read):
            with self.assertRaisesRegex(guard.SetupError, "Could not read resolved Specify launcher"):
                guard.run(args)
        self.invoke.assert_not_called()

    def test_uv_list_version_receipt_and_record_must_match(self):
        args, _, _ = self.uv_tool_args(list_version="1.4.3")
        with self.assertRaisesRegex(guard.SetupError, "versions do not match"):
            guard.run(args)

        args, _, _ = self.uv_tool_args()
        record = Path(args.tools) / "specify-cli" / (
            "Lib/site-packages/specify_cli-1.4.2.dist-info/RECORD" if os.name == "nt"
            else "lib/python3.12/site-packages/specify_cli-1.4.2.dist-info/RECORD"
        )
        record.write_text("../../Scripts/specify.exe,sha256=wrong,1\n", encoding="utf-8")
        with self.assertRaisesRegex(guard.SetupError, "does not match its package RECORD"):
            guard.run(args)
        self.invoke.assert_not_called()

    def test_missing_uv_receipt_fails_closed(self):
        args, _, _ = self.uv_tool_args()
        receipt = Path(args.tools) / "specify-cli" / "uv-receipt.toml"
        receipt.unlink()
        with self.assertRaisesRegex(guard.SetupError, "Missing or linked uv tool receipt"):
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
