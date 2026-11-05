# SPDX-License-Identifier: Apache-2.0
"""验证启动入口的帮助、交互保护与路径处理，不运行安装或真实云阶段。"""

from pathlib import Path
import os
import signal
import shlex
import shutil
import re
import select
import subprocess
import tempfile
import time
import sys
import unittest


class StartScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="qoder wizard ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / "start.sh"
        self.script = self.root / "start.sh"
        shutil.copy2(source, self.script)

    def start_interactive_fixture(self, bootstrap="raise RuntimeError('fixture operation failed')", existing=False,
                                  lock_pid=None):
        """Run the real wizard in a PTY with inert local dependencies; no cloud calls."""
        scripts = self.root / "scripts"
        scripts.mkdir()
        (self.root / "pyproject.toml").touch()
        for name in ("onboarding.py", "terminal_progress.py"):
            (scripts / name).touch()
        (scripts / "bootstrap.py").write_text(bootstrap + "\n")
        binary = self.root / ".venv/bin"
        binary.mkdir(parents=True)
        python = binary / "python"
        python.write_text('''#!/bin/bash
case "$1" in
  -c|*/bootstrap.py) exec ''' + shlex.quote(sys.executable) + ''' "$@" ;;
  */terminal_progress.py)
    shift
    [[ "$1" == --mode ]] || exit 0
    while [[ "$1" != -- ]]; do shift; done
    shift
    exec "$@" ;;
  */onboarding.py)
    printf 'helper:%s:%s\\n' "$2" "${3:-}" >> var/FIXTURE_CALLS
    case "$2" in
      run-status)
        [[ ! -f var/FAIL_RUN_STATUS ]] || exit 9
        if [[ -f var/FIXTURE_RUN_STATUS ]]; then cat var/FIXTURE_RUN_STATUS;
        else printf 'ready\\n'; fi ;;
      preview-status)
        if [[ -f var/FIXTURE_PREVIEW_RUNNING ]]; then printf 'ready\\n';
        elif [[ -f var/FIXTURE_PREVIEW_STATUS ]]; then cat var/FIXTURE_PREVIEW_STATUS;
        else printf 'ready\\n'; fi ;;
      phase-state)
        if [[ -f "var/FIXTURE_PHASE_$3" ]]; then cat "var/FIXTURE_PHASE_$3";
        else printf 'verified\\n'; fi ;;
      template-check)
        fixture="var/FIXTURE_TEMPLATE_$3"
        if [[ -f "$fixture" ]]; then
          case "$(cat "$fixture")" in
            missing) [[ -f "var/FIXTURE_TEMPLATE_CREATED_$3" ]] || { echo "模板不存在"; exit 1; } ;;
            blocked) echo "无法核对模板状态"; exit 17 ;;
          esac
        fi ;;
      show-links) printf '%s\\n' '{"session_url":"https://example.invalid/session","preview_url":"http://127.0.0.1:8765"}' ;;
    esac ;;
  *) exit 29 ;;
esac
''')
        cli = binary / "qoder-byoc-sandbox-example"
        cli.write_text('''#!/bin/bash
printf 'cli:%s\\n' "$*" >> var/FIXTURE_CALLS
if [[ "$1" == templates ]]; then touch "var/FIXTURE_TEMPLATE_CREATED_$2"; fi
if [[ "$1" == preview ]]; then
  printf '%s\\n' "$$" > var/FIXTURE_PREVIEW_RUNNING
  trap 'rm -f var/FIXTURE_PREVIEW_RUNNING; touch var/FIXTURE_PREVIEW_STOPPED; exit 0' TERM INT
  while true; do sleep 0.05; done
fi
[[ "$1" != cleanup ]] || printf "CLEANED_FIXTURE\\n"
exit 0
''')
        for name in ("docker", "wslview"):
            (binary / name).write_text("#!/bin/bash\nexit 0\n")
        for file in binary.iterdir():
            file.chmod(0o755)
        if existing:
            state = self.root / "var/state"
            state.mkdir(parents=True)
            (state / "run.json").write_text('{"cleaned":false}')
        if lock_pid is not None:
            lock = self.root / "var/onboarding.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text(f"{lock_pid}\n")
        master, slave = os.openpty()
        self.process = subprocess.Popen(
            ["/bin/bash", str(self.script)], cwd=self.root,
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
            env={**os.environ, "PATH":str(binary)+os.pathsep+os.environ["PATH"], "TERM":"dumb"},
        )
        os.close(slave)
        self.terminal = master
        self.pending_output = ""
        self.transcript = ""
        def close():
            if self.process.poll() is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=3)
            os.close(master)
        self.addCleanup(close)

    def await_prompt(self, text, timeout=6):
        deadline = time.monotonic() + timeout
        while text not in self.pending_output:
            self.assertLess(time.monotonic(), deadline, self.pending_output)
            if select.select([self.terminal], [], [], 0.1)[0]:
                try:
                    chunk = os.read(self.terminal, 65536)
                except OSError:
                    chunk = b""
                self.pending_output += chunk.decode("utf-8", errors="replace")
                self.transcript += chunk.decode("utf-8", errors="replace")
                if not chunk:
                    self.fail("wizard exited before prompt: " + self.pending_output)
        _, self.pending_output = self.pending_output.split(text, 1)

    def reply(self, value=""):
        os.write(self.terminal, (value + "\n").encode())

    def interrupt(self):
        os.killpg(self.process.pid, signal.SIGINT)
        self.assertEqual(self.process.wait(timeout=5), 130)
        self.assertFalse((self.root / "var/onboarding.lock").exists())

    def test_operation_error_waits_for_ctrl_c_without_repeating_or_advancing(self):
        self.start_interactive_fixture('print("ONE_ATTEMPT", flush=True)\nraise RuntimeError("fixture operation failed")')
        self.await_prompt("fixture operation failed")
        self.await_prompt("Ctrl+C")
        self.assertIsNone(self.process.poll())
        self.reply()
        self.await_prompt("当前阶段已暂停")
        self.assertIsNone(self.process.poll())
        self.interrupt()

    def test_existing_run_menu_reprompts_instead_of_quitting(self):
        self.start_interactive_fixture(existing=True)
        for value in ("quit", "typo"):
            self.await_prompt("选择编号 [1]")
            self.reply(value)
        self.await_prompt("选择编号 [1]")
        self.interrupt()

    def test_dead_wizard_lock_is_recovered_before_starting(self):
        with subprocess.Popen([sys.executable, "-c", "pass"]) as old_wizard:
            old_wizard.wait(timeout=5)
        self.start_interactive_fixture(bootstrap="", lock_pid=old_wizard.pid)
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        lock_pid = self.root / "var/onboarding.lock/pid"
        self.assertEqual(lock_pid.read_text().strip(), str(self.process.pid))
        self.interrupt()

    def test_live_wizard_lock_is_retained_and_reports_nested_shell_exit(self):
        self.start_interactive_fixture(bootstrap="", lock_pid=os.getpid())
        self.await_prompt("再输入 exit 返回原向导")
        self.assertFalse((self.root / "var/FIXTURE_CALLS").exists())
        os.killpg(self.process.pid, signal.SIGINT)
        self.assertIn(self.process.wait(timeout=5), (-signal.SIGINT, 130))
        self.assertEqual((self.root / "var/onboarding.lock/pid").read_text().strip(), str(os.getpid()))

    def continue_existing(self):
        self.await_prompt("选择编号 [1]")
        self.reply()

    def assert_completed_restore_skips_guided_stages(self):
        self.await_prompt("选择编号 [1]")
        for prompt in ("按回车进入模板准备", "按回车进入运行资源创建",
                       "确认页面已打开后，按回车开始三轮演示", "由向导发送提示词",
                       "Stage 3/6", "Stage 4/6", "Stage 5/6"):
            self.assertNotIn(prompt, self.transcript)
        self.assertIn("Stage 6/6", self.transcript)
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        for operation in ("cli:task", "cli:observe", "cli:verify", "cli:resume",
                          "helper:prepare-registry", "helper:docker-login", "helper:template-check"):
            self.assertNotIn(operation, calls)

    def test_completed_continue_reuses_preview_and_goes_directly_to_menu(self):
        self.start_interactive_fixture(existing=True)
        self.continue_existing()
        self.assert_completed_restore_skips_guided_stages()
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        self.assertNotIn("cli:preview", calls)
        self.assertEqual(calls.count("helper:run-status:"), 2)
        self.reply("4")
        self.await_prompt("选择编号 [1]")
        self.interrupt()

    def test_completed_continue_restarts_closed_preview_and_stops_only_its_child(self):
        self.start_interactive_fixture(existing=True)
        (self.root / "var/FIXTURE_PREVIEW_STATUS").write_text("free\n")
        self.continue_existing()
        self.assert_completed_restore_skips_guided_stages()
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        self.assertEqual(calls.count("cli:preview\n"), 1)
        self.assertTrue((self.root / "var/FIXTURE_PREVIEW_RUNNING").exists())
        self.interrupt()
        self.assertTrue((self.root / "var/FIXTURE_PREVIEW_STOPPED").exists())

    def test_completed_continue_does_not_stop_unknown_process_on_occupied_port(self):
        self.start_interactive_fixture(existing=True)
        occupied = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        def close_unknown():
            if occupied.poll() is None:
                occupied.terminate()
            occupied.wait(timeout=3)
        self.addCleanup(close_unknown)
        status = self.root / "var/FIXTURE_PREVIEW_STATUS"
        status.write_text("occupied\n")
        self.continue_existing()
        self.await_prompt("处理后按回车重新检查")
        self.assertIsNone(occupied.poll())
        status.write_text("ready\n")
        self.reply()
        self.assert_completed_restore_skips_guided_stages()
        self.interrupt()
        self.assertIsNone(occupied.poll())
        occupied.terminate()
        occupied.wait(timeout=3)

    def test_completed_continue_rechecks_health_and_pauses_without_recovery_writes(self):
        self.start_interactive_fixture(existing=True)
        self.await_prompt("选择编号 [1]")
        (self.root / "var/FAIL_RUN_STATUS").touch()
        self.reply("1")
        self.await_prompt("第 2 阶段出现错误")
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        self.assertNotIn("cli:preview", calls)
        self.assertNotIn("cli:cleanup", calls)
        self.reply()
        self.await_prompt("当前阶段已暂停")
        self.interrupt()

    def test_completed_continue_stops_if_run_expires_while_choice_is_pending(self):
        self.start_interactive_fixture(existing=True)
        self.await_prompt("选择编号 [1]")
        (self.root / "var/FIXTURE_RUN_STATUS").write_text("expired\n")
        self.reply("1")
        self.await_prompt("当前运行已不再可用（expired）")
        self.await_prompt("第 2 阶段出现错误")
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        for operation in ("cli:preview", "cli:cleanup", "cli:setup"):
            self.assertNotIn(operation, calls)
        self.interrupt()

    def test_unfinished_continue_still_resumes_ledger_without_resending_verified_round(self):
        self.start_interactive_fixture(existing=True)
        (self.root / "var/FIXTURE_PHASE_revise").write_text("done\n")
        (self.root / "var/FIXTURE_PHASE_append").write_text("new\n")
        self.continue_existing()
        self.await_prompt("选择编号 [1]")
        self.reply("1")
        self.await_prompt("按回车发送对话框中的提示词")
        self.reply()
        self.await_prompt("选择编号 [1]")
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        self.assertIn("cli:verify --phase revise", calls)
        self.assertIn("cli:task --phase append", calls)
        self.assertNotIn("cli:task --phase collect", calls)
        self.interrupt()

    def test_ctrl_d_does_not_advance_or_exit_a_pause(self):
        self.start_interactive_fixture('print("CONFIG_READY", flush=True)')
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        os.write(self.terminal, b"\x04")
        self.await_prompt("输入未完成")
        self.assertIsNone(self.process.poll())
        self.reply()
        self.await_prompt("选择编号 [1]")
        self.interrupt()

    def test_explicit_guard_failure_also_waits_for_ctrl_c(self):
        self.start_interactive_fixture(existing=True)
        (self.root / "var/FAIL_RUN_STATUS").touch()
        self.await_prompt("第 2 阶段出现错误")
        self.await_prompt("Ctrl+C")
        self.assertIsNone(self.process.poll())
        self.interrupt()

    def test_keep_and_invalid_task_input_stay_in_wizard_until_ctrl_c(self):
        self.start_interactive_fixture('print("CONFIG_READY", flush=True)')
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        self.reply()
        self.await_prompt("选择编号 [1]")
        self.reply("typo")
        self.await_prompt("选择编号 [1]")
        self.reply("1")
        self.await_prompt("选择编号 [1]")
        self.reply("4")
        self.await_prompt("选择编号 [1]")
        self.reply("2")
        self.await_prompt("✓ 本轮运行资源已回收")
        self.await_prompt("Ctrl+C")
        self.assertIsNone(self.process.poll())
        self.interrupt()

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(self.script)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stage_transitions_never_clear_screen_or_scrollback(self):
        source = self.script.read_text()
        self.assertNotIn("tput clear", source)
        self.assertNotIn("\\033[2J", source)
        self.assertNotIn("\\033[3J", source)
        stage = re.search(r"^stage\(\) \{.*?^\}", source, re.M | re.S).group()
        result = subprocess.run(["/bin/bash", "-euc", 'BOLD= BLUE= RESET=; _STAGE_INDEX=0; TOTAL_STAGES=6;\n' + stage + '\nprintf "上阶段结果保留\\n"; stage 新阶段'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("上阶段结果保留\n\n▸ Stage 1/6 · 新阶段", result.stdout)

    def test_failed_chat_returns_to_cleanup_menu_under_errexit(self):
        source = self.script.read_text()
        action = re.search(r'^    1\)\n(.*?)\n      ;;', source, re.M | re.S).group(1)
        script = 'run_cli() { return 7; }; warn() { printf "%s\\n" "$1"; };\n' + action + '\nprintf "CLEANUP_MENU_AVAILABLE\\n"'
        result = subprocess.run(["/bin/bash", "-euc", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CLEANUP_MENU_AVAILABLE", result.stdout)
        self.assertIn("回收运行资源", result.stdout)

    def test_runtime_creation_requires_explicit_yes(self):
        self.start_interactive_fixture('print("CONFIG_READY", flush=True)')
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        (self.root / "var/FIXTURE_RUN_STATUS").write_text("empty\n")
        self.reply()
        for invalid in ("", "typo"):
            self.await_prompt("创建本轮云端运行资源 · 输入 yes")
            self.reply(invalid)
            self.await_prompt("尚未执行")
            self.assertNotIn("Stage 5/6", self.transcript)
        self.await_prompt("创建本轮云端运行资源 · 输入 yes")
        self.reply("yes")
        self.await_prompt("Stage 5/6")
        self.interrupt()

    def test_fresh_run_builds_checks_and_uninstalls_in_order(self):
        self.start_interactive_fixture(
            'from pathlib import Path\nimport sys\n'
            'if sys.argv[1:] == ["--uninstall"]:\n'
            '    with Path("var/FIXTURE_CALLS").open("a") as log:\n'
            '        print("bootstrap:uninstall", file=log)\n'
        )
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        var = self.root / "var"
        for name, value in {
            "RUN_STATUS": "empty", "TEMPLATE_browser": "missing", "TEMPLATE_analysis": "missing",
            "PHASE_collect": "new", "PHASE_revise": "submitted", "PHASE_append": "done",
        }.items():
            (var / f"FIXTURE_{name}").write_text(value + "\n")
        (var / "state").mkdir()
        (var / "state/bootstrap.json").write_text("{}")
        self.reply()
        self.await_prompt("创建本轮云端运行资源 · 输入 yes")
        self.reply("yes")
        self.await_prompt("选择编号 [1]")
        self.reply("2")
        self.await_prompt("确认只发送一条新消息后")
        self.reply()
        self.await_prompt("选择编号 [1]")
        self.reply("3")
        self.await_prompt("本轮运行已回收")
        calls = (var / "FIXTURE_CALLS").read_text().splitlines()
        expected = [
            "helper:template-team-check:", "helper:prepare-registry:--verify-only",
            "helper:template-check:browser", "cli:templates browser",
            "helper:template-check:browser", "helper:template-summary:browser",
            "helper:template-check:analysis", "helper:docker-login:",
            "cli:templates analysis --use-docker-login", "helper:template-check:analysis",
            "helper:template-summary:analysis", "cli:setup",
            "cli:observe --phase collect", "cli:resume", "cli:verify --phase append",
            "cli:cleanup", "bootstrap:uninstall",
        ]
        tracked = set(expected)
        self.assertEqual([call for call in calls if call in tracked], expected)
        self.assertFalse(any(call.startswith("cli:task ") for call in calls))
        self.interrupt()

    def test_template_status_error_does_not_start_builds_or_runtime(self):
        self.start_interactive_fixture(bootstrap="")
        self.await_prompt("现在准备仓库和两个 Sandbox 模板")
        (self.root / "var/FIXTURE_TEMPLATE_browser").write_text("blocked\n")
        self.reply()
        self.await_prompt("无法核对模板状态")
        self.await_prompt("第 3 阶段出现错误")
        calls = (self.root / "var/FIXTURE_CALLS").read_text()
        self.assertIn("helper:template-check:browser", calls)
        for operation in ("helper:template-check:analysis", "helper:docker-login:", "cli:templates", "cli:setup"):
            self.assertNotIn(operation, calls)
        self.interrupt()

    def test_running_wizard_uses_loaded_body_if_script_is_updated(self):
        gate = 'printf "READY\\n"\nIFS= read -r _\n' + '# read-ahead padding\n' * 1000
        source = self.script.read_text().replace('if [[ "$#" -gt 0 ]]; then', gate + 'if [[ "$#" -gt 0 ]]; then', 1)
        self.script.write_text(source)
        process = subprocess.Popen(["/bin/bash", str(self.script), "--help"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def close_process():
            if process.poll() is None:
                process.kill()
            process.communicate()
        self.addCleanup(close_process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "wizard did not reach pause")
        self.assertEqual(process.stdout.readline().strip(), "READY")
        self.script.write_text("# updated while running\n" * 500 + source)
        stdout, stderr = process.communicate("\n", timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("六阶段", stdout)

    def test_chinese_punctuation_does_not_extend_bash_variable_name(self):
        warning = next(line for line in self.script.read_text().splitlines() if '旧运行状态：' in line)
        result = subprocess.run(
            ['/bin/bash', '-uc', 'warn() { printf "%s\\n" "$1"; }; existing_status=partial;\n' + warning],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('partial，无法直接续接', result.stdout)

    def test_help_works_from_another_directory_without_mutation(self):
        result = subprocess.run(
            ["bash", str(self.script), "--help"], cwd=self.root.parent,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("六阶段", result.stdout)
        self.assertIn("./start.sh", result.stdout)
        self.assertEqual(list(self.root.iterdir()), [self.script])

    def test_noninteractive_execution_cannot_install_or_create_resources(self):
        result = subprocess.run(
            ["bash", str(self.script)], cwd=self.root.parent,
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("交互式终端", result.stderr)
        self.assertEqual(list(self.root.iterdir()), [self.script])

    def test_preview_explains_all_stages_without_files_or_credentials(self):
        result = subprocess.run(["/bin/bash", str(self.script), "--preview"], cwd=self.root.parent,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count(" / 6 · "), 6)
        for word in ["AK/SK", "Qoder", "Team", "ACR", "Python", "101", "不含表头", "不创建资源"]:
            self.assertIn(word, result.stdout)
        self.assertEqual(list(self.root.iterdir()), [self.script])

    def test_unknown_option_fails_before_any_setup(self):
        result = subprocess.run(
            ["bash", str(self.script), "--unknown"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(list(self.root.iterdir()), [self.script])
