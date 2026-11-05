# SPDX-License-Identifier: Apache-2.0
"""Use real short-lived subprocesses to check progress, failure and cancellation."""

import importlib.util
import io
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "scripts/terminal_progress.py"
SPEC = importlib.util.spec_from_file_location("terminal_progress_test_module", PROGRESS_PATH)
progress = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(progress)


class TerminalProgressTests(unittest.TestCase):
    def test_operation_mode_shows_real_phases_and_keeps_build_noise_in_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'build.log'
            output = io.StringIO()
            command = [sys.executable, '-c', "print('#0 building with docker'); print('The push refers to repository [example]'); print('deadbeef: Pushed'); print('Analysis template test-id ready')"]
            code = progress.run_command(command, log, '准备分析模板', stream=output, tty=False, mode='operation')
            self.assertEqual(code, 0)
            self.assertIn('推送分析镜像', output.getvalue())
            self.assertIn('云端模板已就绪', output.getvalue())
            self.assertNotIn('deadbeef', output.getvalue())
            self.assertIn('deadbeef', log.read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qoder progress ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / "logs/install.log"

    def test_real_success_keeps_full_log_with_plain_stage_feedback(self):
        output = io.StringIO()
        code = progress.run_command(
            [sys.executable, "-c", "print('Collecting pandas'); print('Downloading pandas.whl'); print('Installing collected packages: pandas'); print('Successfully installed pandas')"],
            self.log, stream=output, tty=False,
        )
        self.assertEqual(code, 0)
        self.assertIn("依赖安装完成", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertIn("Downloading pandas.whl", self.log.read_text())
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)

    def test_failure_preserves_exit_code_and_shows_actionable_log_tail(self):
        output = io.StringIO()
        code = progress.run_command(
            [sys.executable, "-c", "import sys; print('ERROR: unavailable dependency'); sys.exit(7)"],
            self.log, stream=output, tty=False,
        )
        self.assertEqual(code, 7)
        self.assertIn("退出码 7", output.getvalue())
        self.assertIn("ERROR: unavailable dependency", output.getvalue())
        self.assertIn(str(self.log), output.getvalue())

    def test_tty_activity_bar_does_not_invent_a_percentage(self):
        output = io.StringIO()
        code = progress.run_command(
            [sys.executable, "-c", "import time; print('Building wheel for demo',flush=True); time.sleep(0.2)"],
            self.log, stream=output, tty=True,
        )
        self.assertEqual(code, 0)
        self.assertIn("\033[2K", output.getvalue())
        self.assertIn("构建项目安装包", output.getvalue())
        self.assertNotIn("%", output.getvalue())

    def test_narrow_tty_updates_fit_one_line_with_chinese_and_combining_text(self):
        for columns in (20, 40, 60):
            with self.subTest(columns=columns), patch.object(progress.shutil, "get_terminal_size", return_value=os.terminal_size((columns, 24))):
                output = io.StringIO()
                code = progress.run_command(
                    [sys.executable, "-c", "print('step',flush=True)"],
                    self.log, label="准备并连接分析环境与浏览器 e\u0301" * 3,
                    stream=output, tty=True, mode="operation",
                )
                self.assertEqual(code, 0)
                frames = output.getvalue().split("\r\033[2K")[1:-1]
                self.assertTrue(frames)
                self.assertTrue(all(progress.display_width(frame) < columns for frame in frames))
                self.assertTrue(all(frame.endswith("s") for frame in frames))
                self.assertNotIn("\033[2J", output.getvalue())
                self.assertNotIn("\033[3J", output.getvalue())

    def test_registry_password_is_not_written_to_log_or_terminal(self):
        output = io.StringIO()
        code = progress.run_command(
            [sys.executable, "-c", "print('Collecting https://test-user:private-password@example.invalid/a.whl')"],
            self.log, stream=output, tty=False,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("private-password", self.log.read_text())
        self.assertNotIn("private-password", output.getvalue())
        self.assertIn("[REDACTED]", self.log.read_text())

    def test_cli_accepts_command_paths_and_arguments_with_spaces(self):
        result = subprocess.run(
            [sys.executable, str(PROGRESS_PATH), "--log", str(self.log), "--", sys.executable, "-c", "import sys; print(sys.argv[1])", "argument with spaces"],
            text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argument with spaces", self.log.read_text())

    @unittest.skipUnless(os.name == "posix", "Bash launcher uses POSIX process groups")
    def test_sigint_stops_the_real_child_and_returns_130(self):
        marker = self.root / "child-stopped"
        child = (
            "import signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGINT, lambda *a: (Path(" + repr(str(marker)) + ").write_text('stopped'),exit(0))); "
            "print('child-ready',flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, str(PROGRESS_PATH), "--log", str(self.log), "--", sys.executable, "-c", child],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.log.exists() and "child-ready" in self.log.read_text():
                break
            time.sleep(0.02)
        self.assertIn("child-ready", self.log.read_text())
        process.send_signal(signal.SIGINT)
        output, errors = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 130, errors)
        self.assertEqual(marker.read_text(), "stopped")
        self.assertIn("已停止安装", output)

    @unittest.skipUnless(os.name == "posix", "Bash launcher uses POSIX process groups")
    def test_cancellation_also_reaps_the_install_commands_descendant(self):
        ready = self.root / "descendant-ready"
        stopped = self.root / "descendant-stopped"
        leaf = (
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGINT,lambda *a:(Path(" + repr(str(stopped)) + ").write_text(str(os.getpid())),exit(0))); "
            "Path(" + repr(str(ready)) + ").write_text('ready'); time.sleep(60)"
        )
        launcher = (
            "import signal,subprocess,sys; signal.signal(signal.SIGINT,signal.SIG_IGN); "
            "p=subprocess.Popen([sys.executable,'-c'," + repr(leaf) + "]); p.wait()"
        )
        process = subprocess.Popen(
            [sys.executable, str(PROGRESS_PATH), "--log", str(self.log), "--", sys.executable, "-c", launcher],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "Real descendant did not start")
        process.send_signal(signal.SIGINT)
        _, errors = process.communicate(timeout=8)
        self.assertEqual(process.returncode, 130, errors)
        descendant_pid = int(stopped.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(descendant_pid, 0)


class ConfigurationHelpTests(unittest.TestCase):
    def prompt_process(self, field, supplied):
        code = (
            "import importlib.util; from pathlib import Path; "
            "p=Path(" + repr(str(ROOT / "scripts/onboarding.py")) + "); "
            "s=importlib.util.spec_from_file_location('wizard_help_test',p); "
            "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
            "v=m.prompt_value('测试字段', field=" + repr(field) + "); print('accepted='+v)"
        )
        return subprocess.run(
            [sys.executable, "-c", code], input=supplied,
            text=True, capture_output=True,
        )

    def test_help_command_is_not_saved_as_a_configuration_value(self):
        result = self.prompt_process("region", "?\ncn-hangzhou\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("示例", result.stdout)
        self.assertIn("https://", result.stdout)
        self.assertIn("accepted=cn-hangzhou", result.stdout)
        self.assertNotIn("accepted=?", result.stdout)

    def test_qoder_pat_guidance_contains_verified_configuration_entry(self):
        result = self.prompt_process("QODER_ACCESS_TOKEN", "?\nunit-test-token\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://qoder.com/account/integrations", result.stdout)
        self.assertIn("个人", result.stdout)
