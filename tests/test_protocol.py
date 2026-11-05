# SPDX-License-Identifier: Apache-2.0
"""演示目标：验证 Self-hosted Worker 能按 Qoder Work API 契约处理任务。
执行流程：本地协议服务模拟租约、事件分页、权限和回传失败；工具执行真实文件操作。
能力体现：覆盖确认、心跳、中断、结果重传与鉴权边界；此测试不替代真实云端接入。"""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qoder_byoc_sandbox_example.qoder.client import API, APIError
from qoder_byoc_sandbox_example.environments.self_hosted.tools import Tools
from qoder_byoc_sandbox_example.environments.self_hosted.worker import Worker


class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tools = Tools(self.temp.name)

    def test_write_read_edit_and_ambiguous_edit(self):
        self.assertFalse(
            self.tools.execute("Write", {"file_path": "src/a.py", "content": "x x"})[1]
        )
        self.assertIn("x x", self.tools.execute("Read", {"file_path": "src/a.py"})[0])
        self.assertTrue(
            self.tools.execute(
                "Edit", {"file_path": "src/a.py", "old_string": "x", "new_string": "y"}
            )[1]
        )
        self.assertFalse(
            self.tools.execute(
                "Edit",
                {
                    "file_path": "src/a.py",
                    "old_string": "x",
                    "new_string": "y",
                    "replace_all": True,
                },
            )[1]
        )
        self.assertEqual((Path(self.temp.name) / "src/a.py").read_text(), "y y")

    def test_path_escape_rejected(self):
        for value in ("../outside", "/etc/passwd"):
            with self.subTest(value=value):
                self.assertTrue(self.tools.execute("Read", {"file_path": value})[1])

    def test_bash_cwd_and_credentials(self):
        with patch.dict(
            os.environ, {"QODER_ACCESS_TOKEN": "do-not-leak", "E2B_API_KEY": "secret"}
        ):
            out, error = self.tools.execute("Bash", {"command": "pwd; env"})
        self.assertFalse(error)
        self.assertIn(self.temp.name, out)
        self.assertNotIn("do-not-leak", out)
        self.assertNotIn("secret", out)

    def test_bash_timeout_and_nonzero_exit(self):
        out, error = self.tools.execute("Bash", {"command": "sleep 5", "timeout": 1000})
        self.assertTrue(error)
        self.assertIn("exit_code=124", out)
        self.assertTrue(self.tools.execute("Bash", {"command": "exit 3"})[1])


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.posts = []
        self.keys = []
        self.requests = []
        self.post_failures = 1
        self.full_worker = False
        self.worker_stops_on_result = False
        self.event = {
            "id": "evt_tool",
            "type": "agent.tool_use",
            "name": "Bash",
            "evaluated_permission": "allow",
            "input": {
                "command": "python3 -c \"from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\""
            },
        }
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, value, status=200):
                data = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                test.requests.append(self.path)
                if self.path.startswith("/environments/"):
                    self.reply(
                        {
                            "id": "work_a",
                            "data": {"id": "sess_a", "type": "session"},
                            "state": "queued",
                        }
                    )
                elif self.path.startswith("/failure"):
                    self.reply({"error": "token-value"}, 401)
                elif self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "http://example.invalid/credentials")
                    self.end_headers()
                elif "page=second" in self.path:
                    self.reply(
                        {
                            "data": [
                                {
                                    "id": "evt_idle",
                                    "type": "session.status_idle",
                                    "stop_reason": {
                                        "type": "requires_action",
                                        "event_ids": ["evt_tool"],
                                    },
                                }
                            ],
                            "has_more": False,
                            "next_page": None,
                        }
                    )
                else:
                    self.reply(
                        {"data": [test.event], "has_more": True, "next_page": "second"}
                    )

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                data = json.loads(raw) if raw else None
                test.requests.append(self.path)
                if "/work/" in self.path:
                    if "/heartbeat" in self.path:
                        self.reply(
                            {
                                "state": "active",
                                "lease_extended": True,
                                "last_heartbeat": "2026-10-28T00:00:00Z",
                                "ttl_seconds": 60,
                            }
                        )
                    else:
                        self.reply(
                            {
                                "state": "stopped"
                                if self.path.endswith("/stop")
                                else "starting"
                            }
                        )
                    return
                test.posts.append(data)
                test.keys.append(self.headers.get("Idempotency-Key"))
                if test.post_failures:
                    test.post_failures -= 1
                    self.reply({"error": "transient"}, 503)
                else:
                    self.reply({"data": [{"id": "evt_result"}]})
                    if test.worker_stops_on_result:
                        test.worker.stopping.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = API(f"http://127.0.0.1:{self.server.server_port}", "token-value")
        self.worker = Worker(
            self.api,
            "env_a",
            "sess_a",
            "worker_a",
            self.temp.name,
            Path(self.temp.name) / "worker-state",
        )

    def test_event_pagination_and_retry_does_not_repeat_execution(self):
        self.worker.reconcile()
        with redirect_stdout(io.StringIO()):
            self.worker.dispatch()
            self.worker.dispatch()
        self.assertEqual((Path(self.temp.name) / "counter").read_text(), "1")
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.keys, ["tool-evt_tool", "tool-evt_tool"])
        result = self.posts[-1]["events"][0]
        self.assertEqual(result["type"], "user.tool_result")
        self.assertEqual(result["tool_use_id"], "evt_tool")
        self.assertFalse(result["is_error"])
        self.assertTrue(any("page=second" in r for r in self.requests))
        self.assertFalse(
            any("page=second" in r and "after_id" in r for r in self.requests)
        )

    def test_worker_claims_acknowledges_heartbeats_and_stops(self):
        self.worker_stops_on_result = True
        with redirect_stdout(io.StringIO()):
            self.worker.run(max_seconds=5)
        self.assertTrue(any("/work/poll" in r for r in self.requests))
        self.assertTrue(any(r.endswith("/work/work_a/ack") for r in self.requests))
        self.assertTrue(
            any("expected_last_heartbeat=NO_HEARTBEAT" in r for r in self.requests)
        )
        self.assertTrue(any(r.endswith("/work/work_a/stop") for r in self.requests))
        self.assertEqual((Path(self.temp.name) / "counter").read_text(), "1")
        records = [json.loads(line) for line in (self.worker.state_dir / 'worker.jsonl').read_text().splitlines()]
        self.assertEqual(next(e for e in records if e['kind']=='work_poll')['state'],'queued')
        self.assertEqual(next(e for e in records if e['kind']=='work_ack')['state'],'starting')
        self.assertEqual(next(e for e in records if e['kind']=='heartbeat_confirmed')['ttl_seconds'],60)

    def test_custom_tool_returns_custom_result_contract(self):
        class EchoTools(Tools):
            def tool_echo(self, args):
                return json.dumps({"test": True}), False

        self.worker.tools = EchoTools(self.temp.name)
        self.event.update(type="agent.custom_tool_use", name="echo", input={})
        self.event.pop("evaluated_permission")
        self.worker.reconcile()
        with redirect_stdout(io.StringIO()):
            self.worker.dispatch()
        result = self.posts[-1]["events"][0]
        self.assertEqual(result["type"], "user.custom_tool_result")
        self.assertEqual(result["custom_tool_use_id"], "evt_tool")
        self.assertNotIn("tool_use_id", result)
        self.assertIn("test", result["content"][0]["text"])

    def test_ask_is_not_executed_without_confirmation(self):
        self.event["evaluated_permission"] = "ask"
        self.worker.reconcile()
        with redirect_stdout(io.StringIO()):
            self.worker.dispatch()
        self.assertFalse((Path(self.temp.name) / "counter").exists())
        self.worker.confirmed["evt_tool"] = "allow"
        with redirect_stdout(io.StringIO()):
            self.worker.dispatch()
        self.assertTrue((Path(self.temp.name) / "counter").exists())

    def test_interrupted_call_does_not_execute(self):
        self.worker.reconcile()
        self.worker.interrupted.add("evt_tool")
        self.worker.dispatch()
        self.assertEqual(self.posts, [])

    def test_unknown_permission_does_not_execute(self):
        self.event["evaluated_permission"] = "unrecognized"
        self.worker.reconcile()
        with redirect_stdout(io.StringIO()):
            self.worker.dispatch()
        self.assertEqual(self.posts, [])

    def test_auth_error_redacts_token(self):
        with self.assertRaises(APIError) as caught:
            self.api.request("GET", "/failure")
        self.assertNotIn("token-value", str(caught.exception))
        self.assertEqual(caught.exception.status, 401)

    def test_redirect_does_not_forward_credentials(self):
        with self.assertRaises(APIError) as caught:
            self.api.request("GET", "/redirect")
        self.assertEqual(caught.exception.status, 302)


class WorkerLeaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        class LeaseAPI:
            token = 'private-api-credential'
            posts = []
            heartbeat = None

            def request(self, method, path, **kwargs):
                if self.heartbeat is not None:
                    return self.heartbeat
                raise RuntimeError('lease heartbeat failed')

            def send(self, *args):
                self.posts.append(args)

        self.api = LeaseAPI()
        self.worker = Worker(self.api,'env','session','worker',self.root,self.root/'state')
        self.worker.calls['tool'] = dict(id='tool',type='agent.tool_use',name='Bash',
                                         evaluated_permission='allow',input={'command':'touch started; sleep 30; touch completed'})
        self.worker.pending.add('tool')
        self.worker.work_id = 'work'

    def records(self):
        return [json.loads(line) for line in (self.worker.state_dir/'worker.jsonl').read_text().splitlines()]

    def run_cancellation(self):
        dispatch = threading.Thread(target=self.worker.dispatch)
        with redirect_stdout(io.StringIO()):
            dispatch.start()
            try:
                deadline = time.monotonic()+3
                while not (self.root/'started').exists() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertTrue((self.root/'started').exists(),'Bash did not begin executing')
                self.worker.heartbeats()
                dispatch.join(3)
            finally:
                self.worker.stopping.set()
                dispatch.join(3)
        self.assertFalse(dispatch.is_alive())
        self.assertFalse((self.root/'completed').exists())
        self.assertEqual(self.api.posts,[])
        records = self.records()
        self.assertIn('tool_cancelled',[record['kind'] for record in records])
        self.assertNotIn('tool_posted',[record['kind'] for record in records])

    def test_heartbeat_failure_cancels_running_bash_and_never_posts_result(self):
        self.run_cancellation()
        self.assertIsNotNone(self.worker.lease_error)
        self.assertIn('heartbeat_failed',[record['kind'] for record in self.records()])

    def test_remote_stopped_work_cancels_running_bash_and_never_posts_result(self):
        self.api.heartbeat={'state':'stopped'}
        self.run_cancellation()
        self.assertIn('heartbeat_stopped',[record['kind'] for record in self.records()])

    def test_worker_log_redacts_captured_credentials_even_after_environment_removal(self):
        with patch.dict(os.environ,{'E2B_API_KEY':'opaque-e2b-credential'}):
            worker = Worker(self.api,'env','session','worker',self.root,self.root/'safe-state')
        with redirect_stdout(io.StringIO()):
            worker.log('tool_finished',input={'token':'unrecognized-secret'},
                       output='private-api-credential opaque-e2b-credential password="alpha,beta" https://user:password@example.test/file')
        text = (worker.state_dir/'worker.jsonl').read_text()
        for value in ['private-api-credential','opaque-e2b-credential','unrecognized-secret','alpha','beta','user:password']:
            self.assertNotIn(value,text)

    def test_log_records_claimed_work_and_preserves_explicit_poll_work(self):
        with redirect_stdout(io.StringIO()):
            self.worker.log('tool_start',tool_use_id='tool')
            self.worker.log('work_poll',work_id='work-polled')
        records=self.records()
        self.assertEqual(records[0]['work_id'],'work')
        self.assertEqual(records[1]['work_id'],'work-polled')

    def test_tool_message_parent_prefers_explicit_id_over_latest_user_message(self):
        feed=[dict(id='msg1',type='user.message'),dict(id='msg2',type='user.message'),
              dict(id='call1',type='agent.tool_use',message_id='msg1',name='Read',input={'file_path':'input.csv'}),
              dict(id='call2',type='agent.tool_use',name='Read',input={'file_path':'input.csv'})]
        self.api.events=lambda *args:iter(feed)
        with redirect_stdout(io.StringIO()):
            self.worker.reconcile()
        self.assertEqual(self.worker.call_messages['call1'],'msg1')
        self.assertEqual(self.worker.call_messages['call2'],'msg2')
        records={record['tool_use_id']:record for record in self.records() if record['kind']=='tool_observed'}
        self.assertEqual(records['call1']['message_id'],'msg1')
        self.assertEqual(records['call2']['message_id'],'msg2')


if __name__ == "__main__":
    unittest.main()
