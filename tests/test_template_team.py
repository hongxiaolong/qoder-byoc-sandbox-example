"""Exercise stale Team recovery through real SDK HTTP requests; no cloud access."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap
import onboarding
from alibabacloud_tea_openapi.models import Config


class TemplateTeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        (self.root / "var/state").mkdir(parents=True)
        self.requests = []
        self.teams = [{"teamID": "actual-team"}]
        self.matches = {"actual-team"}
        self.denied = False
        self.build_status = "ready"
        self.build_identity = "browser-test"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                owner.requests.append((parsed.path, query))
                status = 200
                if parsed.path == "/pop/2026-08-20/teams":
                    data = {"teams": owner.teams, "total": len(owner.teams)}
                elif parsed.path.startswith("/pop/2026-08-20/templates/"):
                    team = query.get("teamID", [""])[0]
                    if owner.denied:
                        status, data = 403, {"code": "Forbidden", "message": "denied"}
                    elif team in owner.matches:
                        data = {"templateID": "browser-test", "teamID": team, "status": {"state": "ready"}}
                    else:
                        status, data = 404, {"code": "TemplateNotFound", "message": "missing"}
                elif parsed.path == "/templates/browser-test/builds/build-test/status":
                    data = {"templateID": owner.build_identity, "buildID": "build-test", "status": owner.build_status, "logs": [], "logEntries": []}
                else:
                    status, data = 400, {"code": "UnexpectedRequest", "message": "unexpected"}
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_POST(self):
                raise AssertionError("Recovery must not create cloud resources")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        endpoint = f"127.0.0.1:{server.server_port}"
        local = Config(access_key_id="test-id", access_key_secret="test-secret", protocol="HTTP", endpoint=endpoint)
        for target in [patch.object(bootstrap.Cloud, "config", return_value=local), patch.object(onboarding, "cloud_config", return_value=local)]:
            target.start()
            self.addCleanup(target.stop)
        onboarding.write_env_values(self.root / "config/.env", {"E2B_API_KEY": "e2b_" + "0" * 40, "E2B_API_URL": "http://" + endpoint, "E2B_DOMAIN": "example.invalid"})
        onboarding.write_env_values(self.root / "config/.env.cloud", {"ALIBABA_CLOUD_ACCESS_KEY_ID": "test-id", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test-secret"})
        onboarding.write_toml_values(self.root / "config/registry.toml", {"team_id": "stale-team", "region": "cn-hangzhou", "repository": "registry.example.invalid/ns/repo", "unknown_field": "preserve"})
        (self.root / "var/state/templates.json").write_text(json.dumps({"browser_template": "browser-test", "browser_build_id": "build-test", "browser_template_name": "browser-name", "analysis_template": "preserve-analysis"}))
        self.before_state = (self.root / "var/state/templates.json").read_bytes()
        self.before_credentials = (self.root / "config/.env").read_bytes()
        self.before_registry = (self.root / "config/registry.toml").read_bytes()

    def check_team(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return onboarding.template_team_check(self.root)

    def test_stale_team_is_recovered_only_from_same_template_and_key(self):
        self.assertEqual(self.check_team(), "actual-team")
        config = tomllib.loads((self.root / "config/registry.toml").read_text())
        self.assertEqual(config["team_id"], "actual-team")
        self.assertEqual(config["unknown_field"], "preserve")
        self.assertEqual((self.root / "var/state/templates.json").read_bytes(), self.before_state)
        self.assertEqual((self.root / "config/.env").read_bytes(), self.before_credentials)
        self.assertTrue(any(path.endswith("/status") for path, _ in self.requests))
        self.assertEqual(self.check_team(), "actual-team")

    def test_unproven_or_ambiguous_team_never_changes_configuration(self):
        for matches, teams in [(set(), [{"teamID": "actual-team"}]), ({"actual-team", "other-team"}, [{"teamID": "actual-team"}, {"teamID": "other-team"}])]:
            with self.subTest(matches=matches):
                self.matches, self.teams = matches, teams
                with self.assertRaises(RuntimeError) as exc:
                    self.check_team()
                self.assertNotIsInstance(exc.exception, onboarding.NotReusable)
                self.assertEqual((self.root / "config/registry.toml").read_bytes(), self.before_registry)

    def test_permissions_and_wrong_key_build_identity_do_not_trigger_rebuild(self):
        for denied, status, identity in [(True, "ready", "browser-test"), (False, "building", "browser-test"), (False, "ready", "different-template")]:
            with self.subTest(denied=denied, status=status, identity=identity):
                self.denied, self.build_status, self.build_identity = denied, status, identity
                with self.assertRaises(Exception) as exc:
                    self.check_team()
                self.assertNotIsInstance(exc.exception, onboarding.NotReusable)
                if not denied:
                    self.assertIn("不能确认同一 Browser 模板", str(exc.exception))
                self.assertEqual((self.root / "config/registry.toml").read_bytes(), self.before_registry)

    def test_valid_team_does_not_need_template_or_key_replacement(self):
        self.teams = [{"teamID": "stale-team"}]
        (self.root / "var/state/templates.json").unlink()
        self.assertEqual(self.check_team(), "stale-team")
        self.assertEqual((self.root / "config/registry.toml").read_bytes(), self.before_registry)
