"""Cleanup must prove remote ownership before any destructive operation."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qoder_byoc_sandbox_example.qoder import sessions
from qoder_byoc_sandbox_example.qoder.client import APIError
from qoder_byoc_sandbox_example.demos.report_analyst import shared


class RecordedAPI:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            record = self.records.get(path, APIError(404, "not found"))
            if isinstance(record, Exception):
                raise record
            return deepcopy(record)
        return {}

    @property
    def mutations(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]


class CleanupOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "run_id": "run-a", "session_id": "session-a",
            "environment_id": "env-a", "work_id": "work-a",
            "agent_id": "agent-a", "skill_id": "skill-a",
        }
        self.records = {
            "/sessions/session-a": {
                "id": "session-a", "environment_id": "env-a",
                "metadata": {"demo_run": "run-a"},
            },
            "/environments/env-a": {
                "id": "env-a", "metadata": {"demo_run": "run-a"},
            },
            "/environments/env-a/work/work-a": {
                "id": "work-a", "environment_id": "env-a",
                "data": {"type": "session", "id": "session-a"},
            },
            "/agents/agent-a": {"metadata": {"demo_run": "run-a"}},
            "/skills/skill-a": {"metadata": {"demo_run": "run-a"}},
        }

    def cleanup(self):
        client = RecordedAPI(self.records)
        snapshots = []
        errors = sessions.cleanup_owned(
            client, self.state, lambda state: snapshots.append(deepcopy(state))
        )
        return client, errors, snapshots

    def test_owned_cleanup_checks_association_before_stopping(self):
        client, errors, snapshots = self.cleanup()
        self.assertEqual(errors, [])
        stop = ("POST", "/environments/env-a/work/work-a/stop", {"force": True})
        for path in ("/sessions/session-a", "/environments/env-a",
                     "/environments/env-a/work/work-a"):
            self.assertLess(client.calls.index(("GET", path, None)), client.calls.index(stop))
        self.assertTrue(self.state["work_stopped"])
        for key in ("session_id", "environment_id", "agent_id", "skill_id"):
            self.assertTrue(self.state[key + "_cleaned"])
        self.assertEqual(len(snapshots), 5)

    def test_other_team_or_wrong_work_never_stops_or_archives_parents(self):
        for path, field, value in (
            ("/sessions/session-a", "metadata", {"demo_run": "run-b"}),
            ("/environments/env-a", "metadata", {"demo_run": "run-b"}),
            ("/sessions/session-a", "environment_id", "env-b"),
            ("/environments/env-a/work/work-a", "data", {"type": "session", "id": "session-b"}),
            ("/environments/env-a/work/work-a", "environment_id", "env-b"),
        ):
            with self.subTest(path=path, field=field):
                self.setUp()
                self.records[path][field] = value
                client, errors, _ = self.cleanup()
                self.assertTrue(errors)
                self.assertFalse(self.state.get("work_stopped", False))
                self.assertFalse(self.state.get("session_id_cleaned", False))
                self.assertFalse(self.state.get("environment_id_cleaned", False))
                self.assertEqual(client.mutations, [
                    ("POST", "/agents/agent-a/archive"), ("DELETE", "/skills/skill-a")
                ])

    def test_permissions_and_network_failures_retain_unresolved_state(self):
        for failure in (APIError(403, "forbidden"), RuntimeError("network unavailable")):
            with self.subTest(failure=str(failure)):
                self.setUp()
                self.records["/environments/env-a/work/work-a"] = failure
                client, errors, _ = self.cleanup()
                self.assertTrue(any(str(failure) in error for error in errors))
                self.assertFalse(self.state.get("work_stopped", False))
                self.assertTrue(self.state["agent_id_cleaned"])
                self.assertTrue(self.state["skill_id_cleaned"])
                self.assertEqual(len(client.mutations), 2)

    def test_explicit_work_not_found_is_already_stopped(self):
        del self.records["/environments/env-a/work/work-a"]
        client, errors, _ = self.cleanup()
        self.assertEqual(errors, [])
        self.assertTrue(self.state["work_stopped"])
        self.assertFalse(any(path.endswith("/stop") for _, path in client.mutations))

    def test_session_only_archive_never_infers_another_work(self):
        self.state = {"run_id": "run-a", "session_id": "session-a"}
        client, errors, _ = self.cleanup()
        self.assertEqual(errors, [])
        self.assertEqual(client.mutations, [("POST", "/sessions/session-a/archive")])
        self.assertFalse(any("/work/" in path for _, path, _ in client.calls))

    def test_residual_turn_conflict_cancels_owned_session_and_retries_once(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.setUp()
                class ConflictAPI(RecordedAPI):
                    attempts = 0
                    def request(api, method, path, body=None):
                        result = super().request(method, path, body)
                        if method == "POST" and path == "/sessions/session-a/archive":
                            api.attempts += 1
                            if persistent or api.attempts == 1:
                                raise APIError(409, "residual turn")
                        return result
                client = ConflictAPI(self.records)
                errors = sessions.cleanup_owned(client, self.state, lambda _: None)
                self.assertEqual(client.attempts, 2)
                self.assertEqual(bool(errors), persistent)
                self.assertEqual(bool(self.state.get("session_id_cleaned")), not persistent)
                self.assertEqual(client.mutations.count(("POST", "/sessions/session-a/cancel")), 1)

    def test_already_archived_resource_is_verified_without_repeating_mutation(self):
        for path in ("/sessions/session-a", "/agents/agent-a", "/environments/env-a"):
            self.records[path]["archived_at"] = "2026-10-31T00:00:00Z"
        client, errors, _ = self.cleanup()
        self.assertEqual(errors, [])
        self.assertFalse(any(path.endswith("/archive") for _, path in client.mutations))
        self.assertTrue(self.state["session_id_cleaned"])

    def test_conflict_recovery_rechecks_session_ownership(self):
        class ChangedOwnerAPI(RecordedAPI):
            def request(api, method, path, body=None):
                result = super().request(method, path, body)
                if method == "POST" and path == "/sessions/session-a/archive":
                    raise APIError(409, "conflict")
                if path.endswith("/cancel"):
                    api.records["/sessions/session-a"]["metadata"]["demo_run"] = "other"
                return result
        client = ChangedOwnerAPI(self.records)
        errors = sessions.cleanup_owned(client, self.state, lambda _: None)
        self.assertTrue(errors)
        self.assertFalse(self.state.get("session_id_cleaned", False))
        self.assertEqual(client.mutations.count(("POST", "/sessions/session-a/archive")), 1)

    def test_sandbox_requires_matching_run_and_role_before_kill(self):
        for metadata in ({"demo_run": "run-b", "role": "analysis"},
                         {"demo_run": "run-a", "role": "browser"}, {}):
            with self.subTest(metadata=metadata):
                state = {"run_id": "run-a", "analysis_sandbox_id": "box-a"}
                box = Mock()
                box.get_info.return_value = SimpleNamespace(metadata=metadata)
                with patch.object(shared, "connect", return_value=box):
                    with self.assertRaisesRegex(ValueError, "归属或角色"):
                        shared.cleanup_sandbox(state, {}, "analysis_sandbox_id")
                box.kill.assert_not_called()
                self.assertNotIn("analysis_sandbox_id_killed", state)

    def test_owned_sandbox_and_explicit_not_found_complete(self):
        state = {"run_id": "run-a", "analysis_sandbox_id": "box-a"}
        box = Mock()
        box.get_info.return_value = SimpleNamespace(
            metadata={"demo_run": "run-a", "role": "analysis"}
        )
        with patch.object(shared, "connect", return_value=box):
            self.assertTrue(shared.cleanup_sandbox(state, {}, "analysis_sandbox_id"))
        box.kill.assert_called_once_with()
        with patch.object(shared, "connect", side_effect=shared.NotFoundException("gone")):
            self.assertTrue(shared.cleanup_sandbox(state, {}, "analysis_sandbox_id"))

    def test_sandbox_inspection_failure_preserves_retry(self):
        state = {"run_id": "run-a", "analysis_sandbox_id": "box-a"}
        box = Mock()
        box.get_info.side_effect = RuntimeError("permission denied")
        with patch.object(shared, "connect", return_value=box):
            with self.assertRaisesRegex(RuntimeError, "permission denied"):
                shared.cleanup_sandbox(state, {}, "analysis_sandbox_id")
        box.kill.assert_not_called()
        self.assertEqual(state, {"run_id": "run-a", "analysis_sandbox_id": "box-a"})
