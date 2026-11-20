import importlib.util
import io
import json
import os
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import call, patch


TEST_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "codex_worker", TEST_DIR.parent / "scripts" / "codex-worker.py"
)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class FindCodexTests(unittest.TestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.home = TEST_DIR / "home"
        self.path_cli = str(TEST_DIR / "bin" / "codex")
        self.env = patches.enter_context(patch.object(os, "environ", {}))
        self.which = patches.enter_context(patch.object(worker.shutil, "which", return_value=None))
        self.appx = patches.enter_context(patch.object(
            worker.subprocess, "run", return_value=CompletedProcess([], 0, stdout="")
        ))
        self.is_file = patches.enter_context(patch.object(Path, "is_file", autospec=True, return_value=False))
        self.access = patches.enter_context(patch.object(os, "access", return_value=True))
        patches.enter_context(patch.object(Path, "home", return_value=self.home))

    def test_override_wins_on_both_platforms(self):
        exe = TEST_DIR / "custom" / "codex"
        self.env["CODEX_EXE"] = str(exe)
        self.is_file.side_effect = lambda path: path == exe
        self.which.return_value = self.path_cli
        for windows in (True, False):
            with self.subTest(windows=windows), patch.object(worker, "IS_WINDOWS", windows):
                self.assertEqual(worker.find_codex(), str(exe))
        self.appx.assert_not_called()
        self.which.assert_not_called()

    def test_invalid_override_reports_the_path(self):
        self.env["CODEX_EXE"] = str(TEST_DIR / "missing")
        with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
            worker.find_codex()
        self.assertEqual(error.exception.code, 2)
        self.assertIn(self.env["CODEX_EXE"], stderr.getvalue())
        self.appx.assert_not_called()
        self.which.assert_not_called()

    def test_windows_prefers_appx_to_path(self):
        location = TEST_DIR / "Codex Appx"
        exe = location / "app" / "resources" / "codex.exe"
        self.appx.return_value.stdout = f"{location}\n"
        self.is_file.side_effect = lambda path: path == exe
        self.which.return_value = self.path_cli
        with patch.object(worker, "IS_WINDOWS", True):
            self.assertEqual(worker.find_codex(), str(exe))
        self.appx.assert_called_once()
        self.assertIn("Get-AppxPackage OpenAI.Codex", self.appx.call_args[0][0][-1])
        self.which.assert_not_called()

    def test_windows_uses_path_when_appx_binary_is_missing(self):
        self.appx.return_value.stdout = str(TEST_DIR / "Codex Appx")
        self.which.return_value = self.path_cli
        with patch.object(worker, "IS_WINDOWS", True):
            self.assertEqual(worker.find_codex(), self.path_cli)
        self.appx.assert_called_once()
        self.which.assert_called_once_with("codex")

    def test_windows_uses_path_when_powershell_is_unavailable(self):
        self.appx.side_effect = FileNotFoundError()
        self.which.return_value = self.path_cli
        with patch.object(worker, "IS_WINDOWS", True):
            self.assertEqual(worker.find_codex(), self.path_cli)

    def test_posix_prefers_path_to_app_bundle(self):
        self.which.return_value = self.path_cli
        self.is_file.return_value = True
        with patch.object(worker, "IS_WINDOWS", False):
            self.assertEqual(worker.find_codex(), self.path_cli)
        self.which.assert_called_once_with("codex")
        self.appx.assert_not_called()
        self.is_file.assert_not_called()

    def test_posix_uses_first_executable_app_bundle_candidate(self):
        app_cli = "Codex.app/Contents/Resources/codex"
        system_cli = Path("/Applications") / app_cli
        user_cli = self.home / "Applications" / app_cli
        self.is_file.return_value = True
        with patch.object(worker, "IS_WINDOWS", False):
            self.assertEqual(worker.find_codex(), str(system_cli))
            self.access.side_effect = lambda path, mode: path == user_cli
            self.access.reset_mock()
            self.assertEqual(worker.find_codex(), str(user_cli))
        self.assertEqual(self.access.call_args_list, [call(system_cli, os.X_OK), call(user_cli, os.X_OK)])
        self.appx.assert_not_called()

    def test_missing_cli_error_explains_installation_and_override(self):
        for windows in (True, False):
            with self.subTest(windows=windows), patch.object(worker, "IS_WINDOWS", windows):
                with redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as error:
                    worker.find_codex()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("安装 Codex CLI 并登录", stderr.getvalue())
                self.assertIn("CODEX_EXE", stderr.getvalue())


class ProxyEnvTests(unittest.TestCase):
    def test_proxy_variables_are_removed_only_on_windows(self):
        original = {
            "http_proxy": "test-proxy",
            "HTTP_PROXY": "test-proxy",
            "Https_Proxy": "test-proxy",
            "ALL_PROXY": "test-proxy",
            "no_proxy": "localhost",
            "PATH": "test-path",
        }
        with patch.object(os, "environ", original.copy()):
            with patch.object(worker, "IS_WINDOWS", True):
                self.assertEqual(worker.codex_env(), {"no_proxy": "localhost", "PATH": "test-path"})
            with patch.object(worker, "IS_WINDOWS", False):
                env = worker.codex_env()
                self.assertEqual(env, original)
                env.clear()
            self.assertEqual(os.environ, original)


class MavenMirrorTests(unittest.TestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.home = TEST_DIR / "home"
        self.source = self.home / ".m2" / "repository"
        self.env = patches.enter_context(patch.object(os, "environ", {"SystemDrive": "C:"}))
        self.run = patches.enter_context(patch.object(
            worker.subprocess, "run", return_value=CompletedProcess([], 1)
        ))
        self.is_dir = patches.enter_context(patch.object(
            Path, "is_dir", autospec=True, side_effect=lambda path: path == self.source
        ))
        patches.enter_context(patch.object(Path, "home", return_value=self.home))

    def test_mirror_override_skips_drive_lookup(self):
        mirror = TEST_DIR / "mirror" / "repository"
        self.env["CODEX_WORKER_M2_MIRROR"] = str(mirror)
        self.assertEqual(worker.sync_m2_mirror(), str(mirror))
        self.is_dir.assert_called_once_with(self.source)
        self.assertEqual(self.run.call_args[0][0][:3], ["robocopy", str(self.source), str(mirror)])

    def test_mirror_defaults_to_d_drive_when_present(self):
        self.is_dir.side_effect = lambda path: path in (self.source, Path("D:/"))
        self.assertEqual(worker.sync_m2_mirror(), str(Path("D:/codex-m2/repository")))

    def test_mirror_uses_system_drive_when_d_drive_is_absent(self):
        self.env["SystemDrive"] = "E:"
        self.assertEqual(worker.sync_m2_mirror(), str(Path("E:/codex-m2/repository")))

    def test_mirror_warns_when_inside_home(self):
        mirror = self.home / "mirror" / "repository"
        self.env["CODEX_WORKER_M2_MIRROR"] = str(mirror)
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(worker.sync_m2_mirror(), str(mirror))
        self.assertIn(str(mirror), stderr.getvalue())
        self.assertIn("AccessDeniedException", stderr.getvalue())

    def test_mirror_falls_back_to_user_repo_on_robocopy_failure(self):
        self.run.return_value.returncode = 8
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(worker.sync_m2_mirror(), str(self.source))
        self.assertIn("robocopy 退出码 8", stderr.getvalue())


class SetupMavenTests(unittest.TestCase):
    def setUp(self):
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.repo = Path("maven-repo")
        self.wt = Path("worktree")
        self.tmp = self.wt / "target" / "codex-tmp"
        self.env = {"MAVEN_OPTS": "-Xmx1g", "JAVA_TOOL_OPTIONS": "-Dfile.encoding=UTF-8"}
        self.environ = patches.enter_context(patch.object(os, "environ", {}))
        self.mirror = patches.enter_context(patch.object(worker, "sync_m2_mirror", return_value=str(self.repo)))
        self.run = patches.enter_context(patch.object(worker.subprocess, "run", return_value=CompletedProcess([], 0)))
        self.mkdir = patches.enter_context(patch.object(Path, "mkdir", autospec=True))
        patches.enter_context(patch.object(
            Path, "is_file", autospec=True, side_effect=lambda path: path == self.wt / "pom.xml"
        ))
        patches.enter_context(patch.object(
            Path, "is_dir", autospec=True, side_effect=lambda path: path == self.repo
        ))

    def test_windows_configures_mirror_and_ignored_tmpdir(self):
        with patch.object(worker, "IS_WINDOWS", True):
            hint = worker.setup_maven(self.wt, self.env)
        self.mirror.assert_called_once_with()
        self.run.assert_called_once_with(
            ["git", "-C", str(self.wt), "check-ignore", "-q", "target/codex-tmp"], capture_output=True
        )
        self.mkdir.assert_called_once_with(self.tmp, parents=True, exist_ok=True)
        self.assertEqual(self.env["MAVEN_OPTS"], f"-Xmx1g -Dmaven.repo.local={self.repo}")
        self.assertEqual(self.env["JAVA_TOOL_OPTIONS"], f"-Dfile.encoding=UTF-8 -Djava.io.tmpdir={self.tmp}")
        self.assertIn("target/codex-tmp", hint)
        self.assertIn("mvn -o", hint)

    def test_windows_uses_explicit_repo_and_skips_unignored_tmpdir(self):
        self.environ["CODEX_WORKER_M2_REPO"] = str(self.repo)
        self.run.return_value.returncode = 1
        with patch.object(worker, "IS_WINDOWS", True), redirect_stderr(io.StringIO()) as stderr:
            worker.setup_maven(self.wt, self.env)
        self.mirror.assert_not_called()
        self.mkdir.assert_not_called()
        self.assertEqual(self.env["MAVEN_OPTS"], f"-Xmx1g -Dmaven.repo.local={self.repo}")
        self.assertEqual(self.env["JAVA_TOOL_OPTIONS"], "-Dfile.encoding=UTF-8")
        self.assertIn("target/ 未被 git 忽略", stderr.getvalue())

    def test_posix_only_adds_the_offline_test_hint(self):
        self.environ["CODEX_WORKER_M2_REPO"] = str(self.repo)
        self.environ["CODEX_WORKER_M2_MIRROR"] = str(self.repo)
        original = self.env.copy()
        with patch.object(worker, "IS_WINDOWS", False):
            hint = worker.setup_maven(self.wt, self.env)
        self.assertEqual(self.env, original)
        self.assertIn("mvn -o", hint)
        self.assertIn("与改动直接相关", hint)
        self.assertIn("缺依赖导致跑不起来时写进交付说明", hint)
        for windows_detail in ("MAVEN_OPTS", "target/codex-tmp", "mvn clean", "AccessDeniedException"):
            self.assertNotIn(windows_detail, hint)
        self.mirror.assert_not_called()
        self.run.assert_not_called()
        self.mkdir.assert_not_called()

    def test_non_maven_project_has_no_hint(self):
        for windows in (True, False):
            with self.subTest(windows=windows), patch.object(worker, "IS_WINDOWS", windows):
                self.assertEqual(worker.setup_maven(Path("other-worktree"), self.env), "")
        self.mirror.assert_not_called()
        self.run.assert_not_called()
        self.mkdir.assert_not_called()


class ArgumentAndEventTests(unittest.TestCase):
    def test_parse_args_preserves_all_modes(self):
        cases = (
            (["wt", "/api/skills"], ("new", None, "wt", "/api/skills", None)),
            (["--resume", "session", "wt", "-"], ("resume", "session", "wt", "-", None)),
            (["--review", "wt", "main"], ("review", None, "wt", "main", None)),
            (["--review", "wt", "main", "@角度.md"], ("review", None, "wt", "main", "@角度.md")),
        )
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(worker.parse_args(args), expected)

    def test_parse_args_prints_help(self):
        with redirect_stdout(io.StringIO()) as stdout, self.assertRaises(SystemExit) as error:
            worker.parse_args(["--help"])
        self.assertEqual(error.exception.code, 0)
        self.assertEqual(stdout.getvalue(), worker.USAGE + "\n")

    def test_parse_args_rejects_invalid_arity(self):
        for args in ([], ["wt"], ["--resume", "session", "wt"], ["--review", "wt"]):
            with self.subTest(args=args):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    worker.parse_args(args)
                self.assertEqual(error.exception.code, 2)

    def test_usage_limit_extracts_retry_time_from_both_event_types(self):
        message = (
            "You’ve hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
            "visit https://chatgpt.com/settings/usage to purchase more credits or try again at 6:57 PM."
        )
        events = (
            {"type": "error", "message": message},
            {"type": "turn.failed", "error": {"message": message}},
        )
        for event in events:
            with self.subTest(event_type=event["type"]), redirect_stdout(io.StringIO()):
                run = worker.Run()
                run.handle(json.dumps(event, ensure_ascii=False).encode("utf-8"))
                self.assertEqual(run.usage_limit, "6:57 PM")
                self.assertEqual(run.errors, [message])

    def test_usage_limit_without_retry_time_is_still_detected(self):
        run = worker.Run()
        with redirect_stdout(io.StringIO()):
            run.handle(b'{"type":"error","message":"You have hit your usage limit."}')
        self.assertEqual(run.usage_limit, "")
