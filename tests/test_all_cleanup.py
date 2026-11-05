"""Exercise CLI all against real temporary run records; no cloud resources."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qoder_byoc_sandbox_example.demos.report_analyst import cli


class AllCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run.json"
        self.config = {"qoder": {"base_url": "https://qoder.invalid"}}
        self.values = {"E2B_API_URL": "https://sandbox.invalid", "E2B_API_KEY": "FAKE_ONLY_SECRET"}
        self.state = {"run_id": "new-run", "api_url": self.values["E2B_API_URL"],
                      "qoder_url": self.config["qoder"]["base_url"],
                      "browser_sandbox_id": "new-browser", "cleaned": False}
        for target in (patch.object(cli, "state_file", return_value=self.path),
                       patch("qoder_byoc_sandbox_example.settings.state_file", return_value=self.path),
                       patch.object(cli, "load", return_value=(self.config, self.values))):
            target.start()
            self.addCleanup(target.stop)

    def write(self, state):
        self.path.write_text(json.dumps(state))

    def fail_after_recording(self, *_):
        self.write(self.state)
        raise RuntimeError("setup failed after Browser creation")

    def invoke_failure(self, args):
        self.output, self.errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors):
            with self.assertRaises(SystemExit) as result:
                cli.main(args)
        self.assertEqual(result.exception.code, 1)

    def test_setup_failure_cleans_newly_saved_partial_run(self):
        with patch.object(cli, "setup", side_effect=self.fail_after_recording), patch.object(cli, "cleanup") as cleanup, patch.object(cli, "task") as task:
            self.invoke_failure(["all"])
        cleanup.assert_called_once_with(self.config, self.values, self.state)
        task.assert_not_called()
        self.assertIn("setup failed after Browser creation", self.errors.getvalue())

    def test_setup_rejection_never_cleans_existing_uncleaned_run(self):
        old = {**self.state, "run_id": "old-run"}
        self.write(old)
        with patch.object(cli, "cleanup") as cleanup, patch.object(cli, "api", side_effect=AssertionError("must not call cloud")):
            self.invoke_failure(["all"])
        cleanup.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), old)
        self.assertIn("存在未回收资源", self.errors.getvalue())

    def test_keep_preserves_new_partial_run_after_setup_failure(self):
        with patch.object(cli, "setup", side_effect=self.fail_after_recording), patch.object(cli, "cleanup") as cleanup:
            self.invoke_failure(["all", "--keep"])
        cleanup.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_cleanup_failure_keeps_original_setup_error_and_reports_redacted_reason(self):
        with patch.object(cli, "setup", side_effect=self.fail_after_recording), patch.object(cli, "cleanup", side_effect=RuntimeError("cleanup denied FAKE_ONLY_SECRET")):
            self.invoke_failure(["all"])
        self.assertIn("ERROR: setup failed after Browser creation", self.errors.getvalue())
        report = json.loads(self.output.getvalue())
        self.assertEqual(report["kind"], "cleanup_failed")
        self.assertIn("[REDACTED]", report["error"])
        self.assertNotIn("FAKE_ONLY_SECRET", self.output.getvalue())
        self.assertEqual(json.loads(self.path.read_text()), self.state)

    def test_normal_completion_runs_three_phases_then_cleanup(self):
        self.write({**self.state, "run_id": "old-cleaned-run", "cleaned": True})
        actions = []
        def ready(*_):
            self.write(self.state)
            return self.state
        with patch.object(cli, "setup", side_effect=ready), patch.object(cli, "task", side_effect=lambda _c, _v, _s, phase: actions.append(phase)), patch.object(cli, "cleanup", side_effect=lambda *_: actions.append("cleanup")):
            cli.main(["all"])
        self.assertEqual(actions, ["collect", "revise", "append", "cleanup"])

    def test_failure_before_new_record_does_not_clean_previous_cleaned_run(self):
        old = {**self.state, "run_id": "old-cleaned-run", "cleaned": True}
        self.write(old)
        with patch.object(cli, "setup", side_effect=RuntimeError("model access denied")), patch.object(cli, "cleanup") as cleanup:
            self.invoke_failure(["all"])
        cleanup.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()), old)

    def test_task_failure_still_cleans_this_new_run(self):
        with patch.object(cli, "setup", return_value=self.state), patch.object(cli, "task", side_effect=RuntimeError("task failed")), patch.object(cli, "cleanup") as cleanup:
            self.invoke_failure(["all"])
        cleanup.assert_called_once_with(self.config, self.values, self.state)
        self.assertIn("ERROR: task failed", self.errors.getvalue())
