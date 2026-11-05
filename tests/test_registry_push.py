"""Exercise push recovery with a real failing/succeeding CLI subprocess."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qoder_byoc_sandbox_example.sandbox.templates import push_image


class RegistryPushTests(unittest.TestCase):
    def run_push(self, failures, error, attempts=3):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            docker = root / "docker"
            docker.write_text(
                "#!/usr/bin/env python3\n"
                "import pathlib,sys\n"
                "p=pathlib.Path(__file__).with_name('count')\n"
                "n=int(p.read_text())+1 if p.exists() else 1\n"
                "p.write_text(str(n))\n"
                "assert sys.argv[1:] == ['push','example.invalid/demo:fixed']\n"
                f"print({error!r} if n <= {failures} else 'Layer already exists; digest: sha256:done',flush=True)\n"
                f"sys.exit(1 if n <= {failures} else 0)\n"
            )
            docker.chmod(0o755)
            output = io.StringIO()
            error_result = None
            with patch.dict(os.environ, {"PATH": folder + os.pathsep + os.environ["PATH"]}), contextlib.redirect_stdout(output):
                try:
                    push_image("example.invalid/demo:fixed", attempts=attempts, retry_delays=(0, 0))
                except RuntimeError as exc:
                    error_result = str(exc)
            return int((root / "count").read_text()), output.getvalue(), error_result

    def test_broken_pipe_retries_the_same_image_and_succeeds(self):
        count, output, error = self.run_push(1, 'Put "https://registry.invalid/upload?_state=private&digest=abc": write: broken pipe')
        self.assertEqual(count, 2)
        self.assertIsNone(error)
        self.assertIn("重试", output)
        self.assertNotIn("private", output)

    def test_retry_budget_is_bounded_and_reports_recovery(self):
        count, _, error = self.run_push(10, "connection reset by peer")
        self.assertEqual(count, 3)
        self.assertIn("./start.sh", error)
        self.assertIn("代理", error)

    def test_auth_certificate_and_unknown_failures_are_not_retried(self):
        for reason in ["unauthorized: authentication required", "x509: certificate signed by unknown authority", "unknown failure", "denied: unexpected EOF"]:
            with self.subTest(reason=reason):
                count, _, error = self.run_push(10, reason)
                self.assertEqual(count, 1)
                self.assertIsNotNone(error)

    def test_success_does_not_repeat_upload(self):
        count, _, error = self.run_push(0, "unused")
        self.assertEqual(count, 1)
        self.assertIsNone(error)
