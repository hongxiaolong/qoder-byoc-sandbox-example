# SPDX-License-Identifier: Apache-2.0
"""Offline HTTP evidence: transport truth, isolation, projection and bounds."""

import concurrent.futures
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch

from qoder_byoc_sandbox_example.qoder.client import API, APIError
from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder, MAX_BYTES, MAX_TEXT, project_requests


class Response(io.BytesIO):
    def __init__(self, value, status=200, headers=None):
        super().__init__(json.dumps(value).encode())
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}


def capture(**updates):
    event = {"method": "GET", "path": "/sessions/sess-a/events", "started_at": 10.0,
             "finished_at": 10.1, "elapsed_ms": 95.0, "status_code": 200,
             "state": "success", "attempt": 1,
             "request": {"headers": {}, "query": {"limit": 100}, "body": None},
             "response": {"headers": {}, "body": {"data": []}}}
    event.update(updates)
    return event


class ClientCaptureTests(unittest.TestCase):
    def api(self, observer, *results):
        api = API("https://example.invalid/api/v1/cloud", "private-api-token", observer=observer)
        api.opener = Mock()
        api.opener.open.side_effect = results
        return api

    def http_error(self, status=503):
        return urllib.error.HTTPError("https://example.invalid", status, "failure",
                                     {"Content-Type": "application/json", "Set-Cookie": "private-cookie"},
                                     io.BytesIO(b'{"error":"private-api-token"}'))

    def test_success_records_real_status_timing_and_relative_route(self):
        events = []
        api = self.api(events.append, Response({"id": "sess-a"}, status=201))
        with patch("qoder_byoc_sandbox_example.qoder.client.time.time", side_effect=[100, 99]), \
             patch("qoder_byoc_sandbox_example.qoder.client.time.monotonic", side_effect=[10, 10.125]):
            self.assertEqual(api.request("POST", "/sessions", {"title": "actual"}), {"id": "sess-a"})
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual((event["status_code"], event["attempt"], event["state"]), (201, 1, "success"))
        self.assertEqual((event["started_at"], event["finished_at"], event["elapsed_ms"]), (100, 99, 125))
        self.assertEqual(event["path"], "/sessions")
        self.assertEqual(event["request"]["body"], {"title": "actual"})
        self.assertEqual(event["response"]["body"], {"id": "sess-a"})

    def test_http_retry_and_network_failure_emit_one_record_per_attempt(self):
        events = []
        api = self.api(events.append, self.http_error(), urllib.error.URLError("offline"), Response({"data": []}))
        with patch("qoder_byoc_sandbox_example.qoder.client.time.sleep") as sleep:
            self.assertEqual(api.request("GET", "/sessions/sess-a/events"), {"data": []})
        self.assertEqual([e["status_code"] for e in events], [503, None, 200])
        self.assertEqual([e["state"] for e in events], ["error", "network_error", "success"])
        self.assertEqual([e["attempt"] for e in events], [1, 2, 3])
        self.assertEqual(sleep.call_count, 2)

    def test_mutation_error_is_not_retried_and_callback_failure_cannot_change_it(self):
        def fail(_):
            raise RuntimeError("capture failed")
        api = self.api(fail, self.http_error())
        with self.assertRaises(APIError) as error:
            api.request("POST", "/sessions", {"title": "actual"})
        self.assertEqual(error.exception.status, 503)
        self.assertNotIn("private-api-token", str(error.exception))
        self.assertEqual(api.opener.open.call_count, 1)

    def test_callback_failure_cannot_mutate_success_or_request_input(self):
        def mutate_then_fail(event):
            event["request"]["body"]["title"] = "changed"
            event["response"]["body"]["id"] = "changed"
            raise ValueError("disk unavailable")
        body = {"title": "actual"}
        api = self.api(mutate_then_fail, Response({"id": "sess-a"}))
        self.assertEqual(api.request("POST", "/sessions", body), {"id": "sess-a"})
        self.assertEqual(body, {"title": "actual"})

    def test_network_retry_exhaustion_and_parse_failure_are_truthful(self):
        events = []
        api = self.api(events.append, TimeoutError(), TimeoutError(), TimeoutError())
        with patch("qoder_byoc_sandbox_example.qoder.client.time.sleep"), self.assertRaises(RuntimeError):
            api.request("GET", "/sessions")
        self.assertEqual(len(events), 3)
        self.assertTrue(all(e["status_code"] is None for e in events))
        response = Response(None, status=200)
        response.seek(0)
        response.truncate()
        response.write(b"invalid-json")
        response.seek(0)
        api = self.api(events.append, response)
        with self.assertRaises(ValueError):
            api.request("GET", "/sessions")
        self.assertEqual((events[-1]["status_code"], events[-1]["state"]), (200, "error"))


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "api-trace.jsonl"
        self.recorder = APITraceRecorder(self.path, "run-a")

    def test_actual_api_token_headers_and_work_secrets_never_reach_disk(self):
        api = API("https://example.invalid", "private-api-token", observer=self.recorder.record)
        api.opener = Mock()
        api.opener.open.return_value = Response({
            "id": "work-a", "state": "queued", "secret": "private-work-secret",
            "data": {"type": "session", "id": "sess-a", "secret": "nested-secret"},
            "environment_id": "env-a", "unknown": "private-api-token",
        }, headers={"Content-Type": "application/json", "Set-Cookie": "cookie-value", "Location": "signed-url"})
        api.request("GET", "/environments/env-a/work/poll", query={"block_ms": 999, "secret": "query-private"},
                    headers={"Worker-ID": "worker-a", "X-Api-Key": "header-private"})
        text = self.path.read_text()
        for secret in ("private-api-token", "private-work-secret", "nested-secret", "query-private", "header-private", "cookie-value", "signed-url"):
            self.assertNotIn(secret, text)
        record = self.recorder.snapshot()["requests"][0]
        self.assertEqual((record["session_id"], record["environment_id"], record["resource_id"]), ("sess-a", "env-a", "work-a"))
        self.assertEqual(record["request"]["query"], {"block_ms": 999})
        self.assertEqual(record["request"]["headers"]["worker-id"], "worker-a")
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_public_messages_and_known_tool_inputs_only_with_redaction(self):
        body = {"data": [
            {"id": "evt-a", "type": "agent.message", "unknown": "unknown-private", "content": [
                {"type": "text", "text": "visible Bearer credential-value https://host/file?sig=signature-private"},
                {"type": "thinking", "text": "hidden-reasoning-private"},
                {"type": "text", "text": "hidden-flag-private", "hidden": True}]},
            {"id": "evt-hidden", "type": "agent.message", "hidden": True,
             "content": [{"type": "text", "text": "hidden-event-private"}]},
            {"id": "evt-tool", "type": "agent.tool_use", "name": "Bash",
             "input": {"command": "echo api_key=command-private", "timeout": 3000, "unknown": "unknown-arg-private"}},
            {"id": "evt-custom", "type": "agent.custom_tool_use", "name": "untrusted_custom",
             "input": {"anything": "unknown-custom-private"}},
            {"id": "model-a", "type": "span.model_request_end", "result": "hidden-result-private", "reasoning": "hidden-private"},
            {"id": "unknown", "type": "agent.thinking", "content": "hidden-unknown-private"},
        ], "has_more": False}
        self.recorder.record(capture(response={"body": body}))
        text = self.path.read_text()
        for secret in ("credential-value", "signature-private", "hidden-reasoning-private", "hidden-flag-private", "hidden-event-private", "unknown-private",
                       "command-private", "unknown-arg-private", "unknown-custom-private", "hidden-result-private", "hidden-private", "hidden-unknown-private"):
            self.assertNotIn(secret, text)
        events = self.recorder.snapshot()["requests"][0]["response"]["body"]["data"]
        self.assertEqual(len(events), 4)
        self.assertIn("visible", events[0]["content"][0]["text"])
        self.assertEqual(events[1]["input"], {})
        self.assertTrue(events[1]["input_omitted"])
        self.assertEqual(events[2]["input"], {})

    def test_transport_projection_cannot_bypass_tool_content_policy(self):
        marker = "confidential document without a credential pattern"
        native = [{"id": "call-" + name, "type": "agent.tool_use", "name": name,
                   "input": {key: marker for key in ("file_path", "command", "content", "old_string", "new_string")}}
                  for name in ("Read", "Write", "Edit", "Bash")]
        results = [{"type": kind, "tool_use_id": "call-Read", "is_error": False,
                    "content": [{"type": "text", "text": marker}]}
                   for kind in ("user.tool_result", "user.custom_tool_result", "agent.tool_result")]
        self.recorder.record(capture(method="POST", request={"body": {"events": results}},
                                     response={"body": {"data": native + results}}))
        self.assertNotIn(marker, self.path.read_text())
        record = self.recorder.snapshot()["requests"][0]
        for event in record["response"]["body"]["data"][:4]:
            self.assertEqual(event["input"], {})
            self.assertTrue(event["input_omitted"])
        for event in record["request"]["body"]["events"]:
            self.assertEqual(event["tool_use_id"], "call-Read")
            self.assertTrue(event["content_omitted"])
            self.assertNotIn("content", event)
        # Previously stored or remote envelopes are reprojected by the same rule.
        legacy = copy.deepcopy(record)
        legacy["response"]["body"]["data"] = native + results
        legacy["request"]["body"]["events"] = results
        projected = project_requests([legacy], run_id="run-a", session_id="sess-a")
        self.assertEqual(len(projected["requests"]), 1)
        self.assertNotIn(marker, json.dumps(projected))
        lines = self.path.read_text().splitlines()
        self.path.write_text(lines[0] + "\n" + json.dumps(legacy) + "\n")
        self.assertNotIn(marker, json.dumps(self.recorder.snapshot()))

    def test_events_request_and_error_body_use_actual_allowlisted_projection(self):
        self.recorder.record(capture(method="POST", state="error", status_code=409,
            request={"body": {"events": [{"type": "user.tool_result", "tool_use_id": "tool-a", "is_error": True,
                                            "content": [{"type": "text", "text": "actual failure"}]}]}},
            response={"body": {"error": {"message": "actual conflict", "code": "conflict", "private": "unknown-private"}}}))
        record = self.recorder.snapshot()["requests"][0]
        self.assertEqual(record["tool_use_id"], "tool-a")
        self.assertEqual(record["status_code"], 409)
        self.assertEqual(record["response"]["body"], {"error": {"message": "actual conflict", "code": "conflict"}})
        self.assertNotIn("unknown-private", self.path.read_text())

    def test_unknown_endpoints_and_binary_uploads_are_metadata_only(self):
        for path, body in (("/unknown", {"message": "unknown-private"}), ("/skills", b"binary-private"),
                           ("/skills/skill-a/versions/1/content", b"zip-private")):
            self.recorder.record(capture(path=path, request={"body": body}, response={"body": body}))
        text = self.path.read_text()
        for secret in ("unknown-private", "binary-private", "zip-private"):
            self.assertNotIn(secret, text)
        for record in self.recorder.snapshot()["requests"]:
            self.assertIsNone(record["request"]["body"])
            self.assertTrue(record["request"]["body_omitted"])

    def test_resource_projection_does_not_store_system_scripts_schemas_or_download_urls(self):
        body = {"id": "agent-a", "name": "name", "model": "model-a", "system": "system-private",
                "tools": [{"input_schema": {"private": "schema-private"}}], "download_url": "signed-private",
                "metadata": {"demo_run": "run-a", "private": "metadata-private"},
                "config": {"type": "self_hosted", "setup_script": "script-private"}}
        self.recorder.record(capture(path="/agents", request={"body": body}, response={"body": body}))
        text = self.path.read_text()
        for secret in ("system-private", "schema-private", "signed-private", "metadata-private", "script-private"):
            self.assertNotIn(secret, text)
        record = self.recorder.snapshot()["requests"][0]
        self.assertEqual(record["resource_id"], "agent-a")
        self.assertNotIn("session_id", record)

    def test_snapshot_reprojects_tampered_file_and_refuses_other_run(self):
        self.recorder.record(capture())
        lines = self.path.read_text().splitlines()
        record = json.loads(lines[1])
        record["response"]["body"]["unknown"] = "tampered-private"
        record["response"]["headers"]["Authorization"] = "tampered-token"
        self.path.write_text(lines[0] + "\n" + json.dumps(record) + "\n{bad json}\n")
        snapshot = self.recorder.snapshot()
        self.assertTrue(snapshot["history_incomplete"])
        self.assertNotIn("tampered-private", json.dumps(snapshot))
        self.assertNotIn("tampered-token", json.dumps(snapshot))
        other = APITraceRecorder(self.path, "other-run")
        before = self.path.read_bytes()
        self.assertEqual(other.snapshot(), {"requests": [], "history_incomplete": True})
        with self.assertRaises(ValueError):
            other.record(capture())
        self.assertEqual(self.path.read_bytes(), before)

    def test_bounds_and_concurrent_writers_keep_valid_journal(self):
        def write(index):
            APITraceRecorder(self.path, "run-a").record(capture(attempt=index + 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write, range(25)))
        self.assertEqual(len(self.recorder.snapshot()["requests"]), 25)
        with patch("qoder_byoc_sandbox_example.qoder.telemetry.MAX_ENTRIES", 3):
            self.recorder.record(capture())
            snapshot = self.recorder.snapshot()
        self.assertEqual(len(snapshot["requests"]), 3)
        self.assertTrue(snapshot["history_incomplete"])
        self.assertLess(self.path.stat().st_size, MAX_BYTES)

    def test_oversized_body_and_journal_are_bounded_and_marked(self):
        events = [{"type": "agent.message", "content": [{"type": "text", "text": "x" * 10000}]} for _ in range(50)]
        self.recorder.record(capture(response={"body": {"data": events}}))
        record = self.recorder.snapshot()["requests"][0]
        self.assertTrue(record["capture"]["size_limited"])
        self.assertTrue(record["response"]["body_omitted"])
        header = self.path.read_text().splitlines()[0]
        self.path.write_text(header + "\n" + "x" * MAX_BYTES)
        self.assertEqual(self.recorder.snapshot(), {"requests": [], "history_incomplete": True})
        self.recorder.record(capture())
        self.assertLess(self.path.stat().st_size, MAX_BYTES)
        self.assertTrue(self.recorder.snapshot()["history_incomplete"])

    def test_missing_legacy_journal_is_readable_and_symlink_is_refused(self):
        self.assertEqual(self.recorder.snapshot(), {"requests": [], "history_incomplete": False})
        other = Path(self.temp.name) / "private-file"
        other.write_text("do not touch")
        self.path.symlink_to(other)
        with self.assertRaises(OSError):
            self.recorder.record(capture())
        self.assertEqual(other.read_text(), "do not touch")

    def test_short_tokens_and_work_secret_echoes_are_redacted_before_disk(self):
        recorder = APITraceRecorder(self.path, "run-a", secrets=("xyz",))
        recorder.record(capture(path="/environments/env-a/work/poll", response={"body": {
            "id": "work-a", "state": "xyz private-work-secret", "secret": "private-work-secret"}}))
        text = self.path.read_text()
        self.assertNotIn("xyz", text)
        self.assertNotIn("private-work-secret", text)

    def test_remote_records_require_binding_and_are_reprojected_without_trusting_parent_ids(self):
        self.recorder.record(capture())
        record = self.recorder.snapshot()["requests"][0]
        record.update(session_id="fake-parent", tool_use_id="fake-tool", environment_id="fake-environment")
        record["response"]["body"]["unknown"] = "unknown-private"
        record["request"]["headers"] = {"Authorization": "private-authorization", "X-Security": "private-security"}
        record["request"]["body"] = {"events": [{"type": "user.message", "content": [{"type": "text", "text": "known-private"}]}]}
        snapshot = project_requests([record, {**record, "run_id": "other-run"}], run_id="run-a", secrets=("known-private",))
        self.assertTrue(snapshot["history_incomplete"])
        self.assertEqual(len(snapshot["requests"]), 1)
        public = snapshot["requests"][0]
        self.assertEqual(public["session_id"], "sess-a")
        self.assertNotIn("tool_use_id", public)
        self.assertNotIn("environment_id", public)
        for secret in ("unknown-private", "private-authorization", "private-security", "known-private", "fake-parent"):
            self.assertNotIn(secret, json.dumps(public))
        self.assertEqual(project_requests(None, run_id="run-a"), {"requests": [], "history_incomplete": True})

    def test_known_credentials_are_redacted_before_all_string_length_boundaries(self):
        secret = "AbC123dEf456GhI789jKl012MnO345Pq"
        recorder = APITraceRecorder(self.path, "run-a", secrets=(secret,))
        def boundary(limit):
            return "x" * (limit - len(secret) + 1) + secret
        cases = [
            capture(response={"body": {"data": [{"type": "agent.message", "content": [
                {"type": "text", "text": boundary(MAX_TEXT)}]}]}}),
            capture(path="/sessions", request={"body": {"title": boundary(MAX_TEXT)}}),
            capture(request={"headers": {"Idempotency-Key": boundary(MAX_TEXT)}}),
            capture(path="/" + boundary(1999)),
            capture(method=secret),
            # Unknown dynamic keys are dropped rather than shortened into public keys.
            capture(response={"body": {"data": [], boundary(400): secret}}),
        ]
        for event in cases:
            recorder.record(event)
        text = self.path.read_text()
        public = json.dumps(recorder.snapshot())
        for output in (text, public):
            self.assertNotIn(secret, output)
            self.assertNotIn(secret[:-1], output)
            self.assertNotIn(secret[:10], output)
        # The intentionally invalid HTTP method is sanitized on disk, then
        # rejected by the strict reader rather than promoted as HTTP evidence.
        self.assertEqual(len(recorder.snapshot()["requests"]), len(cases) - 1)

    def test_snapshot_scrubs_a_known_credential_fragment_at_an_old_capped_boundary(self):
        secret = "AbC123dEf456GhI789jKl012MnO345Pq"
        recorder = APITraceRecorder(self.path, "run-a", secrets=(secret,))
        recorder.record(capture())
        header = self.path.read_text().splitlines()[0]
        record = recorder.snapshot()["requests"][0]
        record["response"]["body"] = {"data": [{"type": "agent.message", "content": [
            {"type": "text", "text": "x" * (MAX_TEXT - len(secret) + 1) + secret[:-1]}]}]}
        self.path.write_text(header + "\n" + json.dumps(record) + "\n")
        self.assertNotIn(secret[:-1], json.dumps(recorder.snapshot()))

    def test_public_projection_rejects_missing_transport_proof_and_malformed_records(self):
        self.recorder.record(capture())
        valid = self.recorder.snapshot()["requests"][0]
        malformed = []
        for key in ("id", "kind", "provider", "run_id", "method", "path", "started_at", "finished_at",
                    "elapsed_ms", "status_code", "state", "attempt", "request", "response", "capture"):
            record = copy.deepcopy(valid)
            del record[key]
            malformed.append(record)
        for key, value in (("id", ""), ("method", "mapped"), ("path", "https://example.invalid/sessions"),
                           ("path", "/sessions?access_token=private"), ("path", "//other-host/sessions"),
                           ("path", "/sessions\n"), ("started_at", "10"), ("elapsed_ms", -1),
                           ("elapsed_ms", float("nan")), ("status_code", 0), ("status_code", "200"),
                           ("status_code", None), ("attempt", False), ("state", "mapped"),
                           ("mapped", True), ("synthetic", True)):
            malformed.append({**valid, key: value})
        for key, value in (("recorded", None), ("recorded", False), ("recorded", "true"),
                           ("projection", "mapped"), ("raw_body_saved", True), ("mapped", True), ("synthetic", True)):
            malformed.append({**valid, "capture": {**valid["capture"], key: value}})
        # A mapped endpoint envelope has neither an actual measured attempt nor
        # journal proof; reprojection must not manufacture recorded=True for it.
        malformed.append({"kind": "http.request", "provider": "qca", "run_id": "run-a",
                          "method": "GET", "path": "/sessions/sess-a/events", "state": "mapped"})
        for record in malformed:
            with self.subTest(record=record):
                self.assertEqual(project_requests([record], run_id="run-a"),
                                 {"requests": [], "history_incomplete": True})
        self.assertEqual(project_requests([valid], run_id="run-a")["requests"], [valid])

    def test_nullable_network_status_and_backward_wall_clock_remain_recorded(self):
        self.recorder.record(capture(state="network_error", status_code=None, started_at=10, finished_at=9, elapsed_ms=25))
        records = self.recorder.snapshot()["requests"]
        snapshot = project_requests(records, run_id="run-a")
        self.assertFalse(snapshot["history_incomplete"])
        self.assertEqual(snapshot["requests"][0]["status_code"], None)
        self.assertEqual(snapshot["requests"][0]["elapsed_ms"], 25)

    def test_snapshot_does_not_promote_a_mapped_journal_line_to_recorded_capture(self):
        self.recorder.record(capture())
        header = self.path.read_text().splitlines()[0]
        mapped = {"kind": "http.request", "run_id": "run-a", "method": "GET", "path": "/sessions"}
        self.path.write_text(header + "\n" + json.dumps(mapped) + "\n")
        self.assertEqual(self.recorder.snapshot(), {"requests": [], "history_incomplete": True})


if __name__ == "__main__":
    unittest.main()
