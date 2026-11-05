"""A lost create receipt never becomes a clean run or an automatic retry."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from qoder_byoc_sandbox_example.demos.report_analyst import cli
from qoder_byoc_sandbox_example.demos.report_analyst.lifecycle import (
    RESOURCE_KEYS, begin_create, record_created,
)


class CreateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.state_dir = self.root / "var/state"
        self.state_dir.mkdir(parents=True)
        self.path = self.state_dir / "run.json"
        (self.state_dir / "templates.json").write_text(json.dumps({"browser_template": "browser-template"}))
        skill = self.root / "skills/report-analysis/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("# Demo Skill")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr("report-analysis/SKILL.md", skill.read_bytes())
        self.skill_zip = archive.getvalue()
        self.config = {
            "sandbox": {"analysis_template": "analysis-template", "timeout_seconds": 600},
            "qoder": {"base_url": "https://qoder.invalid"},
            "project": {"name": "test-demo"}, "demo": {"preview_port": 9999},
            "environment": {"tool_provider": "fake:tools"},
            "agent": {"skill_dir": "skills/report-analysis", "name_prefix": "demo",
                      "model": "model", "system_prompt": "Read {{skill_path}}"},
        }
        self.values = {"E2B_API_URL": "https://sandbox.invalid", "E2B_DOMAIN": "sandbox.invalid",
                       "E2B_API_KEY": "FAKE_ONLY", "QODER_ACCESS_TOKEN": "FAKE_ONLY"}
        self.remote, self.mutations, self.saves = {}, [], []
        self.failure = None
        self.client = SimpleNamespace(request=self.request)
        self.boxes = {}
        for role in ("browser", "analysis"):
            self.boxes[role] = SimpleNamespace(
                sandbox_id=role + "-id", traffic_access_token="FAKE_TRAFFIC",
                _envd_access_token="FAKE_CDP", get_host=lambda port: f"{port}.invalid",
                files=Mock(), commands=Mock(),
            )
            self.boxes[role].commands.run.return_value = SimpleNamespace(pid=10)
        self.probe = Mock()
        self.probe.__enter__ = Mock(return_value=self.probe)
        self.probe.__exit__ = Mock(return_value=False)
        self.probe.get.side_effect = lambda _url, **kw: SimpleNamespace(status_code=200 if kw else 401)
        patches = [
            patch.object(cli, "ROOT", self.root), patch.object(cli, "STATE_DIR", self.state_dir),
            patch.object(cli, "state_file", return_value=self.path),
            patch.object(cli, "source_digest", return_value="source-version"),
            patch.object(cli, "save", side_effect=self.save),
            patch.object(cli, "api", return_value=self.client),
            patch.object(cli, "bind_trace", side_effect=lambda client, *a, **kw: client),
            patch.object(cli, "create_sandbox", side_effect=self.create_sandbox),
            patch.object(cli, "cleanup_sandbox", side_effect=self.cleanup_sandbox),
            patch.object(cli, "command", return_value=SimpleNamespace(stdout="1000", stderr="", exit_code=0)),
            patch("httpx.Client", return_value=self.probe),
        ]
        for target in patches:
            target.start()
            self.addCleanup(target.stop)

    def save(self, state):
        snapshot = json.loads(json.dumps(state))
        self.saves.append(snapshot)
        self.path.write_text(json.dumps(snapshot))

    def commit_create(self, resource, identifier, item):
        # The persisted intent must precede the actual cloud-side commit.
        self.assertIn(resource, json.loads(self.path.read_text())["pending_creates"])
        self.remote[identifier] = item
        if self.failure == resource:
            raise RuntimeError("response lost after " + resource + " create committed")
        return item

    def create_sandbox(self, _template, _timeout, metadata, _values, **_kwargs):
        role = metadata["role"]
        box = self.boxes[role]
        self.commit_create(role, box.sandbox_id, {"metadata": metadata})
        return box

    def cleanup_sandbox(self, _state, _values, key):
        self.remote.pop(_state[key])
        self.mutations.append(("kill", key))

    def request(self, method, path, body=None, **kwargs):
        if method == "GET" and path == "/models":
            return {"data": []}
        if method == "GET" and path.endswith("/content"):
            if self.failure == "skill_download":
                raise RuntimeError("skill content download lost")
            return self.skill_zip
        if method == "POST" and path in ("/skills", "/agents", "/environments", "/sessions"):
            resource = path[1:-1]
            item = {"id": resource + "-id", "version": 1, "latest_version": 1,
                    "metadata": {"demo_run": json.loads(self.path.read_text())["run_id"]}}
            if resource == "session":
                item["environment_id"] = "environment-id"
            return self.commit_create(resource, item["id"], item)
        identifier = path.split("/")[2]
        if method == "GET":
            return self.remote[identifier]
        self.mutations.append((method, path))
        self.remote.pop(identifier)
        return {}

    def setup(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return cli.setup(self.config, self.values, official_analysis=True)

    def test_lost_receipts_for_all_creates_preserve_intent_and_never_report_cleanup_pass(self):
        for resource, key in RESOURCE_KEYS.items():
            with self.subTest(resource=resource):
                self.failure = resource
                self.remote.clear()
                self.mutations.clear()
                self.saves.clear()
                self.path.unlink(missing_ok=True)
                with self.assertRaisesRegex(RuntimeError, "response lost"):
                    self.setup()
                state = json.loads(self.path.read_text())
                self.assertEqual(set(state["pending_creates"]), {resource})
                self.assertNotIn(key, state)
                self.assertIn(resource + "-id", self.remote)
                before = set(self.remote)
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "创建结果待核对"):
                    cli.cleanup(self.config, self.values, state)
                self.assertNotIn("cleanup_pass", output.getvalue())
                self.assertFalse(json.loads(self.path.read_text())["cleaned"])
                self.assertEqual(set(self.remote), {resource + "-id"})
                if len(before) > 1:
                    self.assertTrue(self.mutations, "known resources still need cleanup")
                with self.assertRaisesRegex(ValueError, "创建结果待核对"):
                    self.setup()
                with self.assertRaisesRegex(ValueError, "创建结果待核对"):
                    cli.finish_setup(None, None, state, None, None)

    def test_success_saves_exact_ids_and_clears_each_pending_intent(self):
        state = self.setup()
        self.assertNotIn("pending_creates", state)
        for resource, key in RESOURCE_KEYS.items():
            self.assertEqual(state[key], resource + "-id")
            pending = next(i for i, value in enumerate(self.saves) if resource in value.get("pending_creates", {}))
            confirmed = next(i for i, value in enumerate(self.saves) if key in value)
            self.assertLess(pending, confirmed)
            self.assertNotIn(resource, self.saves[confirmed].get("pending_creates", {}))

    def test_pending_intent_blocks_even_inconsistently_marked_cleaned_state(self):
        self.save({"run_id": "old-run", "cleaned": True, "pending_creates": {"agent": {}}})
        with self.assertRaisesRegex(ValueError, "创建结果待核对"):
            self.setup()
        self.assertEqual(json.loads(self.path.read_text())["run_id"], "old-run")
        self.assertFalse(self.remote)

    def test_intent_must_be_persisted_before_create_and_exact_id_before_clear(self):
        state = {"run_id": "r"}
        begin_create(state, "agent", self.save)
        with self.assertRaisesRegex(ValueError, "回执.*不匹配"):
            record_created(state, "agent", "agent-a")
        self.assertIn("agent", state["pending_creates"])
        with self.assertRaisesRegex(ValueError, "创建结果待核对"):
            begin_create(state, "agent", self.save)
        state["agent_id"] = "agent-a"
        record_created(state, "agent", "agent-a")
        self.assertNotIn("pending_creates", state)

    def test_skill_receipt_is_confirmed_before_version_download(self):
        self.failure = "skill_download"
        with self.assertRaisesRegex(RuntimeError, "skill content download"):
            self.setup()
        state = json.loads(self.path.read_text())
        self.assertEqual(state["skill_id"], "skill-id")
        self.assertNotIn("pending_creates", state)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cleanup(self.config, self.values, state)
        self.assertTrue(state["cleaned"])
        self.assertFalse(self.remote)

    def test_resume_cli_rejects_unconfirmed_create_before_any_runtime_access(self):
        state = {"run_id": "old-run", "pending_creates": {"browser": {}}}
        with patch.object(cli, "load", return_value=(self.config, self.values)), patch.object(cli, "state_for", return_value=state), patch.object(cli, "connect", side_effect=AssertionError("must not connect")), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as result:
                cli.main(["resume-setup"])
        self.assertEqual(result.exception.code, 1)
        self.assertIn("创建结果待核对", errors.getvalue())
        self.assertFalse(self.remote)

    def test_failed_intent_persistence_never_calls_create(self):
        def fail_pending(state):
            if state.get("pending_creates"):
                raise OSError("disk full before create")
            self.save(state)
        with patch.object(cli, "save", side_effect=fail_pending), self.assertRaisesRegex(OSError, "disk full"):
            self.setup()
        self.assertFalse(self.remote)
