"""Real temporary HTTP/files plus failure injection; no cloud resources."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from qoder_byoc_sandbox_example.demos.report_analyst import cli, session, shared, source_proxy


class ReviewRegressionTests(unittest.TestCase):
    def test_qoder_network_failure_does_not_skip_sandbox_cleanup(self):
        state = {"run_id": "test", "work_id": "w", "environment_id": "e", "session_id": "s",
                 "analysis_sandbox_id": "a", "browser_sandbox_id": "b"}
        records = {
            "/sessions/s": {"id": "s", "environment_id": "e", "metadata": {"demo_run": "test"}},
            "/environments/e": {"id": "e", "metadata": {"demo_run": "test"}},
            "/environments/e/work/w": {
                "id": "w", "type": "work", "environment_id": "e", "state": "active",
                "data": {"type": "session", "id": "s"},
            },
        }
        def request(method, path, body=None):
            if path.endswith("/stop"):
                raise RuntimeError("Qoder network unavailable")
            self.assertEqual(method, "GET")
            return records[path]
        client = SimpleNamespace(request=request)
        boxes = {}
        for key, role in (("analysis_sandbox_id", "analysis"), ("browser_sandbox_id", "browser")):
            box = Mock()
            box.get_info.return_value = SimpleNamespace(metadata={"demo_run": "test", "role": role})
            boxes[key] = box
        with patch.object(cli, "api", return_value=client), patch.object(
            shared, "connect", side_effect=lambda _state, _values, key: boxes[key]
        ) as connect, patch.object(cli, "save"):
            with self.assertRaisesRegex(RuntimeError, "Qoder network unavailable"):
                cli.cleanup({}, {}, state)
        self.assertEqual(connect.call_count, 2)
        for box in boxes.values():
            box.get_info.assert_called_once_with()
            box.kill.assert_called_once_with()
        self.assertTrue(state["analysis_sandbox_id_killed"])
        self.assertTrue(state["browser_sandbox_id_killed"])
        self.assertFalse(state.get("cleaned", False))

    def test_finished_event_and_cursor_are_persisted_together_before_crash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / "checkpoint.json"
            state = {"collect_message_id": "message", "run_id": "test", "session_id": "session"}
            client = SimpleNamespace(events=lambda *_: iter([{
                "id": "last-event", "type": "session.status_idle", "stop_reason": {"type": "end_turn"},
            }]))
            def save_then_crash(value):
                path.write_text(json.dumps(value))
                raise InterruptedError("process stopped after checkpoint")
            with patch.object(session, "api", return_value=client), patch.object(session, "connect"), patch.object(session, "results", return_value=root), patch.object(session, "save", side_effect=save_then_crash):
                with self.assertRaises(InterruptedError):
                    session.wait_turn({"demo": {"turn_timeout_seconds": 1}}, {}, state, "collect")
            restored = json.loads(path.read_text())
            self.assertEqual(restored["collect_cursor"], "last-event")
            self.assertTrue(restored["collect_done"])
            with patch.object(session, "api", side_effect=AssertionError("must not wait again")):
                session.wait_turn({}, {}, restored, "collect")

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_source_proxy_never_forwards_token_to_redirect_target(self):
        target_requests = []
        class Target(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                target_requests.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
        target = self.start_server(ThreadingHTTPServer(("127.0.0.1", 0), Target))
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", target + "/collect")
                self.end_headers()
        upstream = self.start_server(ThreadingHTTPServer(("127.0.0.1", 0), Upstream))
        proxy = self.start_server(source_proxy.create_server(upstream, "FAKE_ONLY_TOKEN", port=0))
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(proxy + "/report", timeout=3)
        self.assertEqual(exc.exception.code, 502)
        exc.exception.close()
        self.assertEqual(target_requests, [])
