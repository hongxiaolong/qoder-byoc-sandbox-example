# SPDX-License-Identifier: Apache-2.0
"""向导离线回归：真实临时配置、隐藏值的存储和重复运行状态。

不读取项目的真实配置，不连接任何云服务。
"""

from copy import deepcopy
from contextlib import redirect_stdout
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import io
import os
from pathlib import Path
import signal
import stat
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from urllib.parse import parse_qs, urlsplit

from dotenv import dotenv_values


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/onboarding.py"
SPEC = importlib.util.spec_from_file_location("onboarding_test_module", MODULE_PATH)
onboarding = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = onboarding
SPEC.loader.exec_module(onboarding)


class OnboardingTests(unittest.TestCase):
    def test_registry_preparation_signs_real_sdk_requests_and_reuses_existing_repo(self):
        from alibabacloud_tea_openapi.client import Client
        from alibabacloud_tea_openapi.models import Config

        registry = {
            **self.registry,
            "vpc_id": "vpc-unit-test",
            "vswitch_id": "vsw-unit-test",
            "security_group_id": "sg-unit-test",
        }
        onboarding.write_env_values(self.root / "config/.env", self.connection)
        onboarding.write_env_values(self.root / "config/.env.cloud", self.cloud)
        onboarding.write_toml_values(self.root / "config/registry.toml", registry)
        received = []
        created = set()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                action = self.headers["x-acs-action"]
                query = parse_qs(urlsplit(self.path).query)
                received.append((action, query, self.headers.get("Authorization")))
                response = {"IsSuccess": True}
                if action == "GetInstanceVpcEndpoint":
                    response.update(Enable=True, LinkedVpcs=[{
                        "VpcId": "vpc-unit-test", "Status": "RUNNING",
                    }])
                elif action == "ListNamespace":
                    response["Namespaces"] = ([{
                        "NamespaceName": "sandboxhub", "NamespaceStatus": "NORMAL",
                    }] if "namespace" in created else [])
                elif action == "CreateNamespace":
                    created.add("namespace")
                elif action == "ListRepository":
                    response["Repositories"] = ([{
                        "RepoNamespaceName": "sandboxhub",
                        "RepoName": "qoder-byoc-sandbox-example",
                        "RepoType": "PRIVATE",
                    }] if "repository" in created else [])
                elif action == "CreateRepository":
                    created.add("repository")
                else:
                    self.send_error(400)
                    return
                data = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Client(Config(
            access_key_id="unit-test-id",
            access_key_secret="unit-test-secret",
            endpoint="127.0.0.1:" + str(server.server_port),
            protocol="http",
            region_id="cn-hangzhou",
        ))
        output = io.StringIO()
        with redirect_stdout(output):
            onboarding.prepare_registry(self.root, client=client)
            onboarding.prepare_registry(self.root, client=client)

        self.assertEqual([action for action, _, _ in received], [
            "GetInstanceVpcEndpoint", "ListNamespace", "CreateNamespace",
            "ListRepository", "CreateRepository", "GetInstanceVpcEndpoint",
            "ListNamespace", "ListRepository",
        ])
        for action, query, authorization in received:
            self.assertTrue(authorization.startswith("ACS3-HMAC-SHA256 "))
            self.assertEqual(query["InstanceId"], [registry["instance_id"]])
            if action in {"ListNamespace", "ListRepository"}:
                self.assertEqual(query["PageNo"], ["1"])
                self.assertEqual(query["PageSize"], ["100"])
        self.assertNotIn("unit-test-secret", output.getvalue())
        self.assertFalse((self.root / "var").exists())

    def test_native_missing_start_ready_requires_matching_build_snapshot(self):
        from alibabacloud_sandbox20260820 import models

        profile = {
            "cpu": 4,
            "memory_mb": 8192,
            "image": "official-browser:v1",
            "start_command": "start-browser",
            "ready_command": "check-browser",
        }
        runtime = models.PublicTemplateRuntimeConfig(
            cpu=4,
            memory_size=8192,
            sandbox_config=models.PublicTemplateSandboxConfig(image=profile["image"]),
        )
        self.assertTrue(onboarding.template_profile_matches(
            runtime, profile, dict(profile), profile["image"]
        ))
        for snapshot in [None, {**profile, "start_command": "old-start"}]:
            with self.subTest(snapshot=snapshot):
                self.assertFalse(onboarding.template_profile_matches(
                    runtime, profile, snapshot, profile["image"]
                ))
        runtime.sandbox_config.start_command = "different-cloud-command"
        self.assertFalse(onboarding.template_profile_matches(
            runtime, profile, dict(profile), profile["image"]
        ))
        runtime.sandbox_config.start_command = None
        runtime.cpu = 2
        self.assertFalse(onboarding.template_profile_matches(
            runtime, profile, dict(profile), profile["image"]
        ))
        runtime.cpu = 4
        runtime.sandbox_config.image = "different-cloud-image:v1"
        self.assertFalse(onboarding.template_profile_matches(
            runtime, profile, dict(profile), profile["image"]
        ))

    def test_pending_or_unknown_template_status_does_not_request_rebuild(self):
        for status in ["building", "pending", "submitted", "creating", "initializing", "unknown", None]:
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                onboarding.require_template_ready(status, "analysis")
        for status in ["error", "failed", "disabled", "deleted"]:
            with self.subTest(status=status), self.assertRaises(onboarding.NotReusable):
                onboarding.require_template_ready(status, "analysis")
        onboarding.require_template_ready("ready", "analysis")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.connection = {
            "QODER_ACCESS_TOKEN": "qoder-unit-test-token",
            "E2B_API_KEY": "sandbox-unit-test-key",
            "E2B_API_URL": "https://api.cn-hangzhou.sandbox.aliyuncs.com",
            "E2B_DOMAIN": "cn-hangzhou.sandbox.aliyuncs.com",
        }
        self.cloud = {
            "ALIBABA_CLOUD_ACCESS_KEY_ID": "unit-test-id",
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "unit-test-secret",
            "ALIBABA_CLOUD_SECURITY_TOKEN": "",
        }
        self.registry = {
            "repository": (
                "test-registry.cn-hangzhou.cr.aliyuncs.com/"
                "sandboxhub/qoder-byoc-sandbox-example"
            ),
            "instance_id": "cri-unit-test",
            "team_id": "00000000-0000-4000-8000-000000000001",
            "region": "cn-hangzhou",
            "vpc_id": "",
            "vswitch_id": "",
            "security_group_id": "",
        }

    def write_state(self, state):
        directory = self.root / "var/state"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "run.json").write_text(json.dumps(state))

    def test_complete_configuration_and_optional_network_are_accepted(self):
        onboarding.validate_config(self.connection, self.cloud, self.registry)
        network = {
            **self.registry,
            "vpc_id": "vpc-unit-test",
            "vswitch_id": "vsw-unit-test",
            "security_group_id": "sg-unit-test",
        }
        onboarding.validate_config(self.connection, self.cloud, network)

    def test_missing_and_placeholder_required_values_are_rejected(self):
        groups = [
            (0, ["QODER_ACCESS_TOKEN", "E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"]),
            (1, ["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"]),
            (2, ["repository", "instance_id", "team_id", "region"]),
        ]
        for group, keys in groups:
            for key in keys:
                for value in ["", "<请填写实际值>"]:
                    with self.subTest(key=key, value=value):
                        values = deepcopy(
                            [self.connection, self.cloud, self.registry]
                        )
                        values[group][key] = value
                        with self.assertRaises(ValueError):
                            onboarding.validate_config(*values)

    def test_incomplete_network_configuration_is_rejected(self):
        for fields in [
            {"vpc_id": "vpc-test"},
            {"vswitch_id": "vsw-test", "security_group_id": "sg-test"},
        ]:
            with self.subTest(fields=list(fields)):
                with self.assertRaises(ValueError):
                    onboarding.validate_config(
                        self.connection, self.cloud, {**self.registry, **fields}
                    )

    def test_repository_requires_registry_namespace_and_untagged_name(self):
        invalid = [
            "registry.example.test/repository",
            "registry.example.test/namespace/repository/extra",
            "https://registry.example.test/namespace/repository",
            "registry.example.test/namespace/repository:latest",
            "registry.example.test/namespace/repository@sha256:abcd",
            "registry.example.test//repository",
        ]
        for repository in invalid:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                onboarding.validate_config(
                    self.connection,
                    self.cloud,
                    {**self.registry, "repository": repository},
                )

    def test_endpoint_and_domain_must_refer_to_the_same_sandbox_service(self):
        with self.assertRaises(ValueError):
            onboarding.validate_config(
                {
                    **self.connection,
                    "E2B_API_URL": "https://api.cn-shanghai.sandbox.aliyuncs.com",
                },
                self.cloud,
                self.registry,
            )

    def test_region_is_safe_and_matches_the_standard_service_domain(self):
        for region in ["cn/hangzhou", "cn.hangzhou", "cn hangzhou"]:
            with self.subTest(region=region), self.assertRaises(ValueError):
                onboarding.validate_config(
                    self.connection, self.cloud, {**self.registry, "region": region}
                )
        with self.subTest(region="standard endpoint mismatch"):
            with self.assertRaises(ValueError):
                onboarding.validate_config(
                    {
                        **self.connection,
                        "E2B_DOMAIN": "cn-shanghai.sandbox.aliyuncs.com",
                        "E2B_API_URL": "https://api.cn-shanghai.sandbox.aliyuncs.com",
                    },
                    self.cloud,
                    self.registry,
                )
        region = "ap-southeast-1"
        onboarding.validate_config(
            {
                **self.connection,
                "E2B_DOMAIN": region + ".sandbox.aliyuncs.com",
                "E2B_API_URL": "https://api." + region + ".sandbox.aliyuncs.com",
            },
            self.cloud,
            {
                **self.registry,
                "region": region,
                "repository": self.registry["repository"].replace(
                    "cn-hangzhou", region
                ),
            },
        )

    def test_dotenv_roundtrip_keeps_literal_secrets_and_unknown_values(self):
        target = self.root / "config/.env"
        target.write_text("UNRELATED='keep me'\nQODER_ACCESS_TOKEN=old\n")
        values = {
            "QODER_ACCESS_TOKEN": "a'b \"quoted\" # literal ${NOT_EXPANDED}",
            "E2B_API_KEY": "leading and trailing spaces ",
        }
        onboarding.write_env_values(target, values)
        saved = dotenv_values(target, interpolate=False)
        self.assertEqual(saved["QODER_ACCESS_TOKEN"], values["QODER_ACCESS_TOKEN"])
        self.assertEqual(saved["E2B_API_KEY"], values["E2B_API_KEY"])
        self.assertEqual(saved["UNRELATED"], "keep me")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        onboarding.write_env_values(target, {"QODER_ACCESS_TOKEN": "replacement"})
        saved = dotenv_values(target, interpolate=False)
        self.assertEqual(saved["QODER_ACCESS_TOKEN"], "replacement")
        self.assertEqual(saved["E2B_API_KEY"], values["E2B_API_KEY"])

    def test_registry_roundtrip_keeps_unknown_fields_and_string_characters(self):
        target = self.root / "config/registry.toml"
        target.write_text('repository="old"\nnote="保留说明"\nretry_count=3\n')
        values = {
            "repository": self.registry["repository"],
            "description": '带空格、引号 " 和反斜杠 \\',
        }
        onboarding.write_toml_values(target, values)
        saved = tomllib.loads(target.read_text())
        self.assertEqual(saved["repository"], values["repository"])
        self.assertEqual(saved["description"], values["description"])
        self.assertEqual(saved["note"], "保留说明")
        self.assertEqual(saved["retry_count"], 3)

    def configure_environment(self):
        return {
            **os.environ,
            "QODER_SANDBOX_HOME": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }

    def test_changed_or_unversioned_run_stops_before_connecting_to_cloud(self):
        connection = {
            **self.connection,
            "E2B_API_URL": "http://127.0.0.1:9",
            "E2B_DOMAIN": "127.0.0.1",
        }
        onboarding.write_env_values(self.root / "config/.env", connection)
        (self.root / "config/app.toml").write_text(
            '[qoder]\nbase_url="http://127.0.0.1:9/qoder"\n'
        )
        source = self.root / "src/report_analyst"
        source.mkdir(parents=True)
        script = source / "portal.js"
        script.write_text("console.log('old version')")
        old_digest = onboarding.source_digest(self.root)
        script.write_text("console.log('new version')")
        state = {
            "run_id": "source-version-test",
            "session_id": "session-test",
            "analysis_sandbox_id": "analysis-test",
            "browser_sandbox_id": "browser-test",
            "api_url": connection["E2B_API_URL"],
            "qoder_url": "http://127.0.0.1:9/qoder",
            "worker_pid": 123,
        }
        for digest in [None, old_digest]:
            with self.subTest(recorded_digest=digest):
                current = dict(state)
                if digest is not None:
                    current["source_digest"] = digest
                self.write_state(current)
                path = self.root / "var/state/run.json"
                before = path.read_bytes()
                result = subprocess.run(
                    [sys.executable, str(MODULE_PATH), "run-status"],
                    env=self.configure_environment(), text=True,
                    capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "partial\n")
                self.assertIn("cleanup", result.stderr)
                self.assertEqual(path.read_bytes(), before)

    def test_onboarding_and_image_build_share_runtime_source_digest(self):
        from qoder_byoc_sandbox_example.sandbox.templates import source_digest

        directory = self.root / "src/report_analyst/web"
        directory.mkdir(parents=True)
        (directory / "portal.js").write_text("console.log('same runtime')")
        (directory / "qoder.svg").write_text("<svg/>")
        self.assertEqual(onboarding.source_digest(self.root), source_digest(self.root))

    def test_later_validation_failure_preserves_entered_credentials(self):
        secrets = [
            "persisted-pat-test-only",
            "persisted-sandbox-key-test-only",
            "persisted-ak-id-test-only",
            "persisted-ak-secret-test-only",
        ]
        answers = [
            *secrets,
            "",  # optional STS token
            "registry.example.test/repository",  # missing namespace
            "cri-unit-test",
            self.registry["team_id"],
            "",  # default region
            "", "", "",  # optional network fields
            "", "",  # default Domain and API URL
        ]
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "configure"],
            input="\n".join(answers) + "\n",
            text=True,
            capture_output=True,
            env=self.configure_environment(),
            timeout=10,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("repository", result.stderr)
        connection = dotenv_values(self.root / "config/.env", interpolate=False)
        cloud = dotenv_values(self.root / "config/.env.cloud", interpolate=False)
        self.assertEqual(connection["QODER_ACCESS_TOKEN"], secrets[0])
        self.assertEqual(connection["E2B_API_KEY"], secrets[1])
        self.assertEqual(cloud["ALIBABA_CLOUD_ACCESS_KEY_ID"], secrets[2])
        self.assertEqual(cloud["ALIBABA_CLOUD_ACCESS_KEY_SECRET"], secrets[3])
        for value in secrets:
            self.assertNotIn(value, result.stdout)
            self.assertNotIn(value, result.stderr)

    def test_interrupt_preserves_credentials_already_entered(self):
        secrets = ["interrupt-pat-test-only", "interrupt-key-test-only"]
        process = subprocess.Popen(
            [sys.executable, str(MODULE_PATH), "configure"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.configure_environment(),
        )

        def stop_process():
            if process.poll() is None:
                process.kill()
            process.communicate()

        self.addCleanup(stop_process)
        process.stdin.write("\n".join(secrets) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        saved = {}
        while time.monotonic() < deadline and process.poll() is None:
            path = self.root / "config/.env"
            if path.exists():
                saved = dotenv_values(path, interpolate=False)
                if saved.get("E2B_API_KEY") == secrets[1]:
                    break
            time.sleep(0.02)
        self.assertEqual(saved.get("E2B_API_KEY"), secrets[1])
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130, stderr)
        saved = dotenv_values(self.root / "config/.env", interpolate=False)
        self.assertEqual(saved["QODER_ACCESS_TOKEN"], secrets[0])
        self.assertEqual(saved["E2B_API_KEY"], secrets[1])
        for value in secrets:
            self.assertNotIn(value, stdout)
            self.assertNotIn(value, stderr)

    def test_template_check_distinguishes_missing_template_from_broken_state(self):
        path = self.root / "var/state/templates.json"
        path.parent.mkdir(parents=True)
        for content, expected in [(None, 1), ("{broken json", 2)]:
            with self.subTest(content=content):
                if content is not None:
                    path.write_text(content)
                result = subprocess.run(
                    [sys.executable, str(MODULE_PATH), "template-check", "browser"],
                    text=True,
                    capture_output=True,
                    env=self.configure_environment(),
                    timeout=10,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_confirmed_analysis_metadata_refresh_preserves_other_resources(self):
        path = self.root / "var/state/templates.json"
        path.parent.mkdir(parents=True)
        saved = {
            "analysis_template": "analysis-test",
            "analysis_image": "old-image:tag",
            "analysis_build_status": "submitted",
            "analysis_build_error": "old error",
            "browser_template": "browser-test",
            "unknown_field": {"kept": True},
        }
        path.write_text(json.dumps(saved))
        image = self.registry["repository"] + ":analysis-current"
        onboarding.refresh_analysis_metadata(self.root, "analysis-test", image)
        actual = json.loads(path.read_text())
        self.assertEqual(actual["analysis_template"], "analysis-test")
        self.assertEqual(actual["analysis_build_status"], "ready")
        self.assertEqual(actual["analysis_image"], image)
        self.assertNotIn("analysis_build_error", actual)
        self.assertEqual(actual["browser_template"], "browser-test")
        self.assertEqual(actual["unknown_field"], {"kept": True})

    def test_analysis_metadata_refresh_rejects_changed_template_identity(self):
        path = self.root / "var/state/templates.json"
        path.parent.mkdir(parents=True)
        before = b'{"analysis_template":"another-template","browser_template":"b"}\n'
        path.write_bytes(before)
        with self.assertRaises(RuntimeError):
            onboarding.refresh_analysis_metadata(
                self.root, "analysis-test", "registry.example.test/ns/repo:tag"
            )
        self.assertEqual(path.read_bytes(), before)

    def test_load_local_reads_only_the_supplied_temporary_directory(self):
        directory = self.root / "config"
        (directory / ".env").write_text(
            "\n".join(f"{key}={value}" for key, value in self.connection.items())
        )
        (directory / ".env.cloud").write_text(
            "\n".join(f"{key}={value}" for key, value in self.cloud.items())
        )
        (directory / "registry.toml").write_text(
            "\n".join(
                f"{key}={json.dumps(value)}" for key, value in self.registry.items()
            )
        )
        connection, cloud, registry = onboarding.load_local(self.root)
        self.assertEqual(connection, self.connection)
        self.assertEqual(cloud, self.cloud)
        self.assertEqual(registry, self.registry)

    def test_local_state_distinguishes_empty_cleaned_partial_and_recorded(self):
        self.assertEqual(onboarding.local_run_state(self.root), "empty")
        self.write_state({"cleaned": True})
        self.assertEqual(onboarding.local_run_state(self.root), "cleaned")
        self.write_state({"cleaned": False, "agent_id": "agent-test"})
        self.assertEqual(onboarding.local_run_state(self.root), "partial")
        self.write_state(
            {
                "cleaned": False,
                "session_id": "session-test",
                "analysis_sandbox_id": "analysis-test",
                "browser_sandbox_id": "browser-test",
            }
        )
        self.assertEqual(onboarding.local_run_state(self.root), "recorded")

    def test_phase_state_does_not_repeat_already_submitted_messages(self):
        self.assertEqual(onboarding.phase_state(self.root, "collect"), "new")
        self.write_state({"collect_message_id": "message-test"})
        self.assertEqual(onboarding.phase_state(self.root, "collect"), "submitted")
        self.assertEqual(onboarding.phase_state(self.root, "revise"), "new")
        self.write_state(
            {"collect_message_id": "message-test", "collect_done": True}
        )
        self.assertEqual(onboarding.phase_state(self.root, "collect"), "done")
        self.write_state(
            {
                "collect_message_id": "message-test",
                "collect_done": True,
                "collect_verified": True,
            }
        )
        self.assertEqual(onboarding.phase_state(self.root, "collect"), "verified")

    def preview_server(self, identity):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path != "/api/identity":
                    self.send_error(404)
                    return
                data = json.dumps(identity).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        (self.root / "config/demo.toml").write_text(
            "[demo]\npreview_port=" + str(server.server_port) + "\n"
        )
        return server

    def test_matching_preview_identity_is_ready(self):
        identity = {
            "run_id": "preview-run",
            "analysis_sandbox_id": "analysis-test",
            "browser_sandbox_id": "browser-test",
        }
        self.write_state({**identity, "session_id": "session-test"})
        self.preview_server(identity)
        self.assertEqual(onboarding.preview_status(self.root), "ready")

    def test_preview_with_another_run_or_instance_is_occupied(self):
        identity = {
            "run_id": "preview-run",
            "analysis_sandbox_id": "analysis-test",
            "browser_sandbox_id": "browser-test",
        }
        remote_identity = identity.copy()
        self.write_state({**identity, "session_id": "session-test"})
        self.preview_server(remote_identity)
        for key in identity:
            with self.subTest(changed=key):
                remote_identity.clear()
                remote_identity.update({**identity, key: "another-resource"})
                self.assertEqual(onboarding.preview_status(self.root), "occupied")

    def test_unbound_preview_port_is_free(self):
        with socket.socket() as socket_guard:
            socket_guard.bind(("127.0.0.1", 0))
            port = socket_guard.getsockname()[1]
        (self.root / "config/demo.toml").write_text(
            "[demo]\npreview_port=" + str(port) + "\n"
        )
        self.write_state({"run_id": "preview-run", "session_id": "session-test"})
        self.assertEqual(onboarding.preview_status(self.root), "free")

    def test_source_identity_changes_with_runtime_code_not_generated_cache(self):
        source = self.root / "src/package"
        source.mkdir(parents=True)
        (source / "main.py").write_text("print('version 1')\n")
        image = self.root / "images/report_analyst"
        image.mkdir(parents=True)
        (image / "Dockerfile").write_text("FROM example:1\n")
        initial = onboarding.source_digest(self.root)
        self.assertRegex(initial, r"^[0-9a-f]{12}$")
        cache = source / "__pycache__"
        cache.mkdir()
        (cache / "generated.py").write_text("print('ignored')\n")
        (source / "notes.md").write_text("non-runtime notes")
        self.assertEqual(onboarding.source_digest(self.root), initial)
        (source / "portal.js").write_text("console.log('first version')\n")
        script_version = onboarding.source_digest(self.root)
        self.assertNotEqual(script_version, initial)
        (source / "portal.js").write_text("console.log('second version')\n")
        self.assertNotEqual(onboarding.source_digest(self.root), script_version)
        before_icon = onboarding.source_digest(self.root)
        (source / "qoder.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        self.assertNotEqual(onboarding.source_digest(self.root), before_icon)
        before_bitmap = onboarding.source_digest(self.root)
        (source / "agent-sandbox.png").write_bytes(b'product-brand-bitmap')
        self.assertNotEqual(onboarding.source_digest(self.root), before_bitmap)
        (source / "main.py").write_text("print('version 2')\n")
        changed = onboarding.source_digest(self.root)
        self.assertNotEqual(changed, initial)
        (image / "Dockerfile").write_text("FROM example:2\n")
        self.assertNotEqual(onboarding.source_digest(self.root), changed)


if __name__ == "__main__":
    unittest.main()
