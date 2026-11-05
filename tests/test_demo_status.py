# SPDX-License-Identifier: Apache-2.0
"""通过真实本机HTTP验证会话入口的有效性、到期状态和凭证边界。"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest

from qoder_byoc_sandbox_example.qoder.client import API
from qoder_byoc_sandbox_example.demos.report_analyst.demo_status import DemoStatus


class DemoStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "run.json"
        self.responses = {"status": 200, "body": {"id": "sess_test", "status": "idle"}, "requests": 0}
        holder = self.responses

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                holder["requests"] += 1
                if self.path != "/sessions/sess_test" or self.headers.get("Authorization") != "Bearer test-private-token":
                    self.send_error(400)
                    return
                data = json.dumps(holder["body"]).encode()
                self.send_response(holder["status"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.now = [1000]
        self.state = {
            "run_id": "test-run", "session_id": "sess_test", "worker_pid": 123,
            "analysis_url": "https://analysis.example", "created_at": 900,
            "analysis_sandbox_id": "analysis-test", "browser_sandbox_id": "browser-test",
            "session_url": "https://untrusted.invalid/ignored", "collect_verified": True,
        }
        self.write()
        self.provider = DemoStatus(
            self.path, self.state["analysis_url"],
            API(f"http://127.0.0.1:{self.server.server_port}", "test-private-token"),
            {"collect": "download and analyze"}, 3600, clock=lambda: self.now[0],
        )

    def write(self):
        self.path.write_text(json.dumps(self.state))

    def test_ready_link_is_canonical_cached_and_contains_no_credentials(self):
        first = self.provider.snapshot()
        self.assertEqual(first["state"], "ready")
        self.assertEqual(first["session_url"], "https://qoder.com/cloud/sessions/sess_test")
        self.assertEqual(first["phases"]["collect"]["state"], "verified")
        self.assertEqual(first["phases"]["collect"]["prompt"], "download and analyze")
        self.assertEqual(self.provider.snapshot()["state"], "ready")
        self.assertEqual(self.responses["requests"], 1)
        self.assertNotIn("test-private-token", json.dumps(first))
        self.assertNotIn("untrusted.invalid", json.dumps(first))

    def test_cleanup_and_expiration_disable_a_previously_ready_link_immediately(self):
        self.assertEqual(self.provider.snapshot()["state"], "ready")
        self.state["cleaned"] = True
        self.write()
        self.assertEqual(self.provider.snapshot()["state"], "expired")
        self.assertIsNone(self.provider.snapshot()["session_url"])
        self.state["cleaned"] = False
        self.write()
        self.now[0] = 5000
        self.assertEqual(self.provider.snapshot()["state"], "expired")
        self.assertEqual(self.responses["requests"], 1)

    def test_archived_missing_and_unauthorized_sessions_are_not_actionable(self):
        self.provider.cache_seconds = 0
        for status, body, expected in [
            (200, {"id": "sess_test", "archived": True}, "expired"),
            (404, {"message": "missing"}, "expired"),
            (401, {"message": "unauthorized"}, "unavailable"),
            (200, {"id": "different_session"}, "unavailable"),
        ]:
            with self.subTest(status=status, expected=expected):
                self.responses.update(status=status, body=body)
                snapshot = self.provider.snapshot()
                self.assertEqual(snapshot["state"], expected)
                self.assertIsNone(snapshot["session_url"])

    def test_stale_preview_and_incomplete_setup_do_not_query_or_open_qoder(self):
        self.state["analysis_url"] = "https://another.example"
        self.write()
        self.assertEqual(self.provider.snapshot()["state"], "unavailable")
        self.state["analysis_url"] = self.provider.analysis_url
        self.state.pop("worker_pid")
        self.write()
        self.assertEqual(self.provider.snapshot()["state"], "loading")
        self.path.unlink()
        self.assertEqual(self.provider.snapshot()["state"], "loading")
        self.assertEqual(self.responses["requests"], 0)
