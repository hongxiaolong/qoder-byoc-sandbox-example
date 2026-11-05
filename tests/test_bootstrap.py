"""Provisioning transactions and real official SDK wire contracts, no cloud access."""
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bootstrap as b
from qoder_byoc_sandbox_example.sandbox import templates as template_builder


class Account:
    region = "cn-hangzhou"
    def __init__(self, uid="100"):
        self.uid = uid
        self.teams, self.keys, self.instances = [], [], []
        self.switches, self.vpcs, self.groups, self.bindings = [], [], [], []
        self.namespaces, self.repos, self.writes = [], [], []
        self.resource_groups = [{"Id": "rg-test", "DisplayName": "Default", "AccountId": uid, "Status": "OK"}]

    def pages(self, service, action, collection, **params):
        return {"ListTeams": self.teams, "ListApiKeys": self.keys, "ListInstance": self.instances,
                "DescribeVSwitches": self.switches, "DescribeVpcs": self.vpcs,
                "DescribeSecurityGroups": self.groups, "ListNamespace": self.namespaces,
                "ListRepository": self.repos, "ListResourceGroups": self.resource_groups}[action]

    def sandbox(self, action, body=None, **params):
        self.writes.append(action)
        if action == "CreateTeam":
            row = {"teamID": "team-1", **body}
            self.teams.append(row)
            return {"team": row}
        if action == "CreateApiKey":
            row = {"apiKeyID": "key-1", **body, "status": "active"}
            self.keys.append(row)
            return {"apiKey": {**row, "apiKeyValue": "e2b_TEST_ONLY_SECRET"}}
        if action in {"DescribeApiKey", "UpdateApiKey", "DeleteApiKey"}:
            row = next((key for key in self.keys if key["apiKeyID"] == params["path_id"]), None)
            if row is None:
                raise b.CloudError("key missing", "ApiKeyNotFound")
            if action == "DescribeApiKey":
                return {"apiKey": row.copy()}
            if action == "UpdateApiKey":
                row.update(body)
                return {"apiKey": row.copy()}
            if row["status"] != "inactive":
                raise b.CloudError("disable before deleting", "InvalidApiKeyStatus")
            self.keys.remove(row)
            return {}
        if action.startswith("Delete"):
            return {}
        raise AssertionError(action)

    def rpc(self, service, action, **params):
        if action == "GetCallerIdentity": return {"AccountId": self.uid, "Arn": "test"}
        if action == "GetSubscriptionPrice":
            return {"Code": "Success", "Data": {"OriginalPrice": 564.0, "TradePrice": 520.0, "Currency": "CNY"}}
        if action == "GetDefaultResourceGroup": raise b.CloudError("GetDefaultResourceGroup: InvalidAction.NotFound", "InvalidAction.NotFound")
        if action == "GetInstanceVpcEndpoint": return {"Enable": True, "LinkedVpcs": self.bindings}
        if action == "DescribeZones": return {"Zones": {"Zone": [{"ZoneId": "cn-hangzhou-i"}]}}
        if action in {"DescribeVpcAttribute", "DescribeVSwitchAttributes"}: return {"Status": "Available"}
        if action == "DescribeSecurityGroupAttribute": return {"Permissions": {"Permission": []}}
        if action == "GetInstanceEndpoint": return {"Status": "RUNNING", "Enable": True, "AclEnable": False, "Domains": [{"Domain": "test-registry.cn-hangzhou.cr.aliyuncs.com"}]}
        self.writes.append(action)
        if action == "CreateInstance":
            self.instances.append({"InstanceId": "cri-test", "InstanceName": params["Parameter.2.Value"], "InstanceStatus": "RUNNING", "InstanceSpecification": "Enterprise_Basic"})
            return {"Data": {"InstanceId": "cri-test", "OrderId": "order-test"}}
        if action == "CreateVpc":
            self.vpcs.append({"VpcId": "vpc-test", "CidrBlock": params["CidrBlock"]})
            return {"VpcId": "vpc-test"}
        if action == "CreateVSwitch":
            self.switches.append({"VpcId": params["VpcId"], "VSwitchId": "vsw-test", "ZoneId": params["ZoneId"], "Status": "Available", "AvailableIpAddressCount": 200, "CidrBlock": params["CidrBlock"]})
            return {"VSwitchId": "vsw-test"}
        if action == "CreateSecurityGroup":
            self.groups.append({"SecurityGroupId": "sg-test", "VpcId": params["VpcId"]})
            return {"SecurityGroupId": "sg-test"}
        if action == "CreateInstanceVpcEndpointLinkedVpc":
            self.bindings.append({"VpcId": params["VpcId"], "Status": "RUNNING"})
            return {}
        if action == "CreateNamespace":
            self.namespaces.append({"NamespaceName": params["NamespaceName"]})
            return {}
        if action == "CreateRepository":
            self.repos.append({"RepoId": "repo-test", "RepoNamespaceName": params["RepoNamespaceName"], "RepoName": params["RepoName"], "RepoType": "PRIVATE"})
            return {"RepoId": "repo-test"}
        if action.startswith("Delete"): return {}
        raise AssertionError(action)


def authorize(prompt):
    return "yes"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sink = contextlib.redirect_stdout(io.StringIO())
        self.sink.__enter__()
        self.addCleanup(self.sink.__exit__, None, None, None)

    def prepare(self, cloud):
        instance = b.Bootstrap(self.root, cloud)
        with patch("builtins.input", side_effect=authorize), patch.object(b.ui, "open_config_url"):
            team = instance.team()
            instance.key(team)
            acr = instance.acr()
            network = instance.network(acr["InstanceId"])
            repository = instance.repository(acr["InstanceId"])
        self.assertEqual(network["vpc_id"], "vpc-test")
        self.assertIn("/qoder-byoc-sandbox-example", repository)
        return instance

    def test_blank_account_transaction_restarts_without_duplicate_purchase_or_resources(self):
        cloud = Account()
        instance = self.prepare(cloud)
        writes = list(cloud.writes)
        self.prepare(cloud)
        self.assertEqual(writes, cloud.writes)
        self.assertEqual(writes.count("CreateInstance"), 1)
        text = instance.path.read_text()
        self.assertNotIn("e2b_TEST_ONLY_SECRET", text)
        self.assertEqual((self.root / "config/.env").stat().st_mode & 0o777, 0o600)

    def test_other_account_cannot_inherit_resources(self):
        b.Bootstrap(self.root, Account())
        with self.assertRaises(b.CloudError): b.Bootstrap(self.root, Account("200"))

    def test_new_team_uses_the_explicitly_selected_resource_group(self):
        cloud = Account()
        cloud.teams = [{"teamID": "existing", "teamName": "default", "plan": "std"}]
        cloud.resource_groups.append({"Id": "rg-project", "DisplayName": "Project", "AccountId": cloud.uid, "Status": "OK"})
        instance = b.Bootstrap(self.root, cloud)
        with patch("builtins.input", side_effect=["2", "2", "yes"]):
            team = instance.team()
        self.assertEqual(team["resourceGroupID"], "rg-project")
        self.assertEqual(team["plan"], "std")
        self.assertEqual(cloud.writes, ["CreateTeam"])

    def test_dedicated_mode_quotes_once_and_uses_one_confirmation_for_all_resources(self):
        cloud = Account()
        cloud.teams.append({"teamID": "shared-team", "teamName": "default"})
        cloud.instances.append({"InstanceId": "shared-acr", "InstanceName": "shared", "InstanceStatus": "RUNNING", "InstanceSpecification": "Enterprise_Basic"})
        instance = b.Bootstrap(self.root, cloud)
        output = io.StringIO()
        with patch("builtins.input", side_effect=["yes"]) as prompt, patch.object(cloud, "rpc", wraps=cloud.rpc) as rpc, contextlib.redirect_stdout(output):
            instance.approve_dedicated()
            team = instance.team()
            instance.key(team)
            acr = instance.acr()
            instance.network(acr["InstanceId"])
            instance.repository(acr["InstanceId"])
        self.assertEqual(prompt.call_count, 1)
        self.assertIn("¥564.00", output.getvalue())
        self.assertNotIn("¥520", output.getvalue())
        self.assertIn("默认 OSS", output.getvalue())
        quote = next(call for call in rpc.call_args_list if call.args[1] == "GetSubscriptionPrice")
        self.assertEqual(quote.kwargs["SubscriptionType"], "Subscription")
        self.assertEqual(quote.kwargs["ModuleList.1.Config"], "Region:cn-hangzhou,InstanceType:Basic")
        self.assertNotEqual(team["teamID"], "shared-team")
        self.assertNotEqual(acr["InstanceId"], "shared-acr")
        self.assertTrue(instance.state["resources"]["vpc"]["owned"])
        self.assertTrue(instance.state["resources"]["repository"]["owned"])

    def test_no_usable_resource_group_stops_before_any_team_write(self):
        cloud = Account()
        cloud.resource_groups = [
            {"Id": "rg-foreign", "AccountId": "200", "Status": "OK"},
            {"Id": "rg-deleting", "AccountId": cloud.uid, "Status": "PendingDelete"},
        ]
        instance = b.Bootstrap(self.root, cloud)
        with patch("builtins.input", side_effect=["2"]), self.assertRaisesRegex(b.CloudError, "可用资源组"):
            instance.team()
        self.assertEqual(cloud.writes, [])
        self.assertNotIn("team", instance.state["operations"])

    def test_old_account_reuses_existing_resources_without_creating_namespace(self):
        cloud = Account()
        cloud.teams = [{"teamID": "existing", "teamName": "test"}]
        cloud.instances = [{"InstanceId": "cri-test", "InstanceName": "existing", "InstanceStatus": "RUNNING", "InstanceSpecification": "Enterprise_Basic"}]
        cloud.vpcs = [{"VpcId": "vpc-test", "CidrBlock": "10.0.0.0/16"}]
        cloud.switches = [{"VpcId": "vpc-test", "VSwitchId": "vsw-test", "ZoneId": "cn-hangzhou-i", "Status": "Available", "AvailableIpAddressCount": 100}]
        cloud.bindings = [{"VpcId": "vpc-test", "Status": "RUNNING"}]
        cloud.groups = [{"SecurityGroupId": "sg-test"}]
        cloud.repos = [{"RepoId": "old-repo", "RepoName": "existing", "RepoNamespaceName": "existing", "RepoType": "PRIVATE"}]
        instance = b.Bootstrap(self.root, cloud)
        with patch("builtins.input", side_effect=lambda prompt: "1" if "选择编号" in prompt else "yes"):
            team = instance.team()
            instance.key(team)
            acr = instance.acr()
            instance.network(acr["InstanceId"])
            instance.repository(acr["InstanceId"])
        self.assertEqual(cloud.writes, ["CreateApiKey"])
        self.assertFalse(instance.state["resources"]["repository"]["owned"])

    def test_permission_failure_does_not_turn_into_create(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        with patch.object(cloud, "pages", side_effect=b.CloudError("Forbidden", "Forbidden")), self.assertRaises(b.CloudError):
            instance.team()
        self.assertEqual(cloud.writes, [])

    def test_explicit_rejection_can_retry_after_authorization_is_fixed(self):
        instance = b.Bootstrap(self.root, Account())
        with patch("builtins.input", return_value="yes"):
            with self.assertRaises(b.CloudError):
                instance.mutate("x", "test", lambda _: (_ for _ in ()).throw(b.CloudError("no access", "Forbidden")))
            self.assertEqual(instance.mutate("x", "test", lambda _: {"id": "ok"}), {"id": "ok"})

    def test_uninstall_deletes_owned_resources_but_retains_paid_acr(self):
        cloud = Account()
        instance = self.prepare(cloud)
        instance.record("team", {"teamID": "shared-team"})
        cloud.writes.clear()
        output = io.StringIO()
        with patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(output), patch.object(cloud, "sandbox", wraps=cloud.sandbox) as sandbox:
            instance.uninstall()
        self.assertIn("DeleteRepository", cloud.writes)
        sandbox.assert_any_call("DeleteTeam", path_id="team-1")
        self.assertNotIn("shared-team", [call.kwargs["path_id"] for call in sandbox.call_args_list])
        self.assertNotIn("DeleteInstance", cloud.writes)
        self.assertNotIn("RefundInstance", cloud.writes)
        self.assertIn("[人工退订] ACR 实例", output.getvalue())
        self.assertIn("费用与成本 → 订单管理 → 退订管理", output.getvalue())
        self.assertIn("https://help.aliyun.com/zh/acr/product-overview/refund-policy", output.getvalue())
        self.assertIn("cri-test", output.getvalue())
        self.assertIn("https://oss.console.aliyun.com/", output.getvalue())
        fresh = b.Bootstrap(self.root, cloud)
        self.assertNotEqual(instance.state["id"], fresh.state["id"])

    def test_template_rebuild_preserves_creation_receipts_and_legacy_ids(self):
        saved = {"browser_template": "legacy-browser"}
        scope = {"team_id": "team-1", "region": "cn-hangzhou"}
        template_builder.record_created(saved, "browser", "new-browser", scope)
        saved["browser_template"] = "new-browser"
        template_builder.record_created(saved, "browser", "next-browser", scope)
        records = saved["created_templates"]
        self.assertEqual([r["template_id"] for r in records], ["legacy-browser", "new-browser", "next-browser"])
        self.assertNotIn("created_by_demo", records[0])
        self.assertEqual(records[1]["team_id"], "team-1")
        self.assertEqual(records[2]["region"], "cn-hangzhou")
        with patch.object(template_builder, "STATE_DIR", self.root / "var/state"):
            template_builder.store(saved)
            self.assertEqual(template_builder.state(), saved)

    def test_uninstall_cleans_all_recorded_templates_and_accepts_not_found(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("team", {"teamID": "team-1"})
        saved = {}
        scope = {"team_id": "team-1", "region": cloud.region}
        for identity in ("old-analysis", "new-analysis"):
            template_builder.record_created(saved, "analysis", identity, scope)
            saved["analysis_template"] = identity
        b.atomic_json(self.root / "var/state/templates.json", saved)
        missing = RuntimeError("already removed")
        missing.status_code = 404
        with patch("alibabacloud_sandbox20260820.client.Client") as client, patch.object(cloud, "config", return_value=None, create=True), patch("builtins.input", return_value="yes"):
            client.return_value.delete_template.side_effect = [missing, None]
            instance.uninstall()
        self.assertEqual([call.args[0] for call in client.return_value.delete_template.call_args_list], ["old-analysis", "new-analysis"])
        stored = b.ui.read_json(self.root / "var/state/templates.json")
        self.assertNotIn("analysis_template", stored)
        self.assertTrue(all(record["deleted"] for record in stored["created_templates"]))
        self.assertTrue(instance.state["uninstalled"])

    def test_uninstall_retains_unclaimed_or_wrong_scope_templates_and_dependencies(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("team", {"teamID": "team-1"}, True)
        instance.record("repository", {"InstanceId": "cri-1", "RepoId": "repo-1"}, True)
        for history in ([], [{"template_id": "unknown", "created_by_demo": True, "team_id": "team-1", "region": "other-region"}], [{"template_id": "unknown", "created_by_demo": True, "team_id": "other-team", "region": cloud.region}]):
            with self.subTest(history=history):
                b.atomic_json(self.root / "var/state/templates.json", {"analysis_template": "unknown", "created_templates": history})
                with patch("alibabacloud_sandbox20260820.client.Client") as client, patch("builtins.input", return_value="yes"), self.assertRaisesRegex(b.CloudError, "模板归属记录不完整"):
                    instance.uninstall()
                client.assert_not_called()
                self.assertEqual(cloud.writes, [])
                self.assertNotIn("uninstalled", instance.state)

    def test_unknown_write_is_never_automatically_replayed(self):
        instance = b.Bootstrap(self.root, Account())
        with patch("builtins.input", return_value="yes"):
            with self.assertRaises(TimeoutError):
                instance.mutate("create", "create test", lambda _: (_ for _ in ()).throw(TimeoutError()))
            with self.assertRaises(b.CloudError):
                instance.mutate("create", "create test", lambda _: self.fail("replayed"))

    def test_idempotent_purchase_timeout_reuses_same_token(self):
        instance = b.Bootstrap(self.root, Account())
        tokens = []
        def purchase(token):
            tokens.append(token)
            if len(tokens) == 1: raise TimeoutError()
            return {"id": "same"}
        with patch("builtins.input", return_value="yes"):
            with self.assertRaises(TimeoutError): instance.mutate("buy", "test purchase", purchase, True)
            instance.mutate("buy", "test purchase", purchase, True)
        self.assertEqual(tokens[0], tokens[1])

    def test_decline_creates_neither_operation_nor_resource(self):
        instance = b.Bootstrap(self.root, Account())
        with patch("builtins.input", side_effect=["", "no", "YES", KeyboardInterrupt]), self.assertRaises(KeyboardInterrupt):
            instance.mutate("x", "test", lambda _: self.fail("executed"))
        self.assertEqual(instance.state["operations"], {})

    def test_only_explicit_approval_runs_mutation_after_empty_and_invalid_input(self):
        instance = b.Bootstrap(self.root, Account())
        writes = []
        def answer(_prompt):
            self.assertEqual(writes, [])
            return next(answers)
        answers = iter(["", "no", "y", "yes"])
        with patch("builtins.input", side_effect=answer):
            instance.mutate("x", "test", lambda _: writes.append("executed") or {})
        self.assertEqual(writes, ["executed"])

    def test_numbered_menu_defaults_to_first_and_rejects_out_of_range(self):
        with patch("builtins.input", side_effect=["new", "3", ""]) as read:
            self.assertEqual(b.choose_number("资源方案", ["复用", "新建"]), 1)
        self.assertEqual(read.call_count, 3)

    def test_legacy_dedicated_mode_requires_fresh_purchase_approval(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.dedicated = True
        instance.state["setup_mode"] = "new"
        with patch("builtins.input", side_effect=["", "purchase", KeyboardInterrupt]), self.assertRaises(KeyboardInterrupt):
            instance.acr()
        self.assertNotIn("CreateInstance", cloud.writes)
        self.assertNotIn("approved_acr_order", instance.state)
        with patch("builtins.input", side_effect=["yes"]):
            instance.acr()
        self.assertEqual(cloud.writes.count("CreateInstance"), 1)

    def test_missing_catalog_price_stops_dedicated_mode_before_confirmation_or_writes(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        with patch.object(cloud, "rpc", return_value={"Code": "InvalidParameter"}), patch("builtins.input") as read:
            instance.approve_dedicated()
        read.assert_not_called()
        self.assertFalse(instance.dedicated)
        self.assertFalse(cloud.writes)

    def test_enter_leaves_dedicated_plan_unapproved(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        with patch("builtins.input", return_value=""):
            instance.approve_dedicated()
        self.assertFalse(instance.dedicated)
        self.assertNotIn("approved_acr_order", instance.state)
        self.assertFalse(cloud.writes)

    def test_reused_resource_does_not_inherit_ownership_from_another_id(self):
        instance = b.Bootstrap(self.root, Account())
        instance.record("switch", {"VSwitchId": "one", "VpcId": "same"}, True)
        instance.record("switch", {"VSwitchId": "two", "VpcId": "same"})
        self.assertFalse(instance.state["resources"]["switch"]["owned"])
        self.assertTrue(instance.state["history"][0]["owned"])

    def test_restrictive_groups_are_not_silently_modified(self):
        self.assertFalse(b.egress_allows_https([{"Policy": "Drop"}]))
        self.assertFalse(b.egress_allows_https([{"Policy": "Accept", "IpProtocol": "tcp", "PortRange": "80/80", "DestCidrIp": "0.0.0.0/0"}]))
        self.assertTrue(b.egress_allows_https([{"Policy": "Accept", "IpProtocol": "tcp", "PortRange": "443/443", "DestCidrIp": "0.0.0.0/0"}]))

    def test_same_namespace_in_another_registry_does_not_inherit_ownership(self):
        instance = b.Bootstrap(self.root, Account())
        instance.record("namespace", {"InstanceId": "cri-a", "NamespaceName": "demo"}, True)
        instance.record("namespace", {"InstanceId": "cri-b", "NamespaceName": "demo"})
        self.assertFalse(instance.state["resources"]["namespace"]["owned"])
        self.assertEqual(instance.state["history"][0]["value"]["InstanceId"], "cri-a")

    def test_each_registry_gets_its_own_acl_and_deleted_acl_can_be_recreated(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        calls = []
        def rpc(service, action, **query):
            self.assertEqual(action, "CreateInstanceEndpointAclPolicy")
            calls.append(query)
            return {}
        endpoint = {"AclEnable": True, "AclEntries": []}
        address = b.ipaddress.IPv4Address("198.51.100.10")
        with patch.object(cloud, "rpc", side_effect=rpc), patch("builtins.input", return_value="yes"):
            instance.public_acl("cri-a", endpoint, address)
            instance.public_acl("cri-b", endpoint, address)
            # The authoritative endpoint list says our first ACL was removed.
            instance.public_acl("cri-a", endpoint, address)
        self.assertEqual([q["InstanceId"] for q in calls], ["cri-a", "cri-b", "cri-a"])

    def test_network_writes_are_scoped_and_creation_ownership_survives_interruption(self):
        instance = b.Bootstrap(self.root, Account())
        with patch("builtins.input", return_value="yes"):
            first = instance.mutate("sg", "create in A", lambda _: {"SecurityGroupId": "sg-a"}, True, scope={"VpcId": "vpc-a"})
            second = instance.mutate("sg", "create in B", lambda _: {"SecurityGroupId": "sg-b"}, True, scope={"VpcId": "vpc-b"})
        self.assertNotEqual(first, second)
        # Process stopped after the create result was saved but before record().
        recovered = b.Bootstrap(self.root, Account())
        recovered.record("sg", {"SecurityGroupId": "sg-a"})
        self.assertTrue(recovered.state["resources"]["sg"]["owned"])

    def test_uninstall_disables_owned_keys_and_resumes_after_a_failed_delete(self):
        cloud = Account()
        key = {"apiKeyID": "key-1", "teamID": "team-1", "status": "active",
               "ipWhitelist": [{"ipAddress": "203.0.113.1/32"}]}
        cloud.keys = [key, {"apiKeyID": "shared-key", "teamID": "team-1", "status": "active"}]
        instance = b.Bootstrap(self.root, cloud)
        instance.record("key", {k: key[k] for k in ("apiKeyID", "teamID")}, True)
        instance.state["history"] = [{"kind": "key", "owned": False,
                                      "value": {"apiKeyID": "shared-key", "teamID": "team-1"}}]
        sandbox = cloud.sandbox

        def fail_delete(action, *args, **kwargs):
            if action == "DeleteApiKey":
                raise b.CloudError("temporary failure", "SystemError")
            return sandbox(action, *args, **kwargs)

        with patch("builtins.input", return_value="yes"):
            with patch.object(cloud, "sandbox", side_effect=fail_delete):
                instance.uninstall()
            self.assertEqual(key["status"], "inactive")
            self.assertEqual(key["ipWhitelist"], [{"ipAddress": "203.0.113.1/32"}])
            self.assertNotIn("uninstalled", instance.state)
            instance = b.Bootstrap(self.root, cloud)
            instance.uninstall()
        self.assertEqual(cloud.writes, ["DescribeApiKey", "UpdateApiKey", "DescribeApiKey", "DeleteApiKey"])
        self.assertEqual([key["apiKeyID"] for key in cloud.keys], ["shared-key"])

    def test_uninstall_rejects_key_with_a_different_cloud_team(self):
        cloud = Account()
        cloud.keys = [{"apiKeyID": "key-1", "teamID": "other-team", "status": "active"}]
        instance = b.Bootstrap(self.root, cloud)
        instance.record("key", {"apiKeyID": "key-1", "teamID": "expected-team"}, True)
        with patch("builtins.input", return_value="yes"):
            instance.uninstall()
        self.assertEqual(cloud.writes, ["DescribeApiKey"])
        self.assertNotIn("uninstalled", instance.state)

    def test_uninstall_groups_history_and_deduplicates_by_resource_identity(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("repository", {"InstanceId": "cri-current", "RepoId": "repo-current"}, True)
        duplicate = {"InstanceId": "cri-current", "RepoId": "repo-current", "RepoName": "old-name"}
        legacy_deleted = {"InstanceId": "cri-deleted", "RepoId": "repo-deleted", "RepoName": "old-name"}
        instance.state["history"] = [{"kind": "repository", "value": value, "owned": True} for value in (
            duplicate, legacy_deleted, {"InstanceId": "cri-deleted", "RepoId": "repo-deleted"},
            {"InstanceId": "cri-history", "RepoId": "repo-history"},
        )]
        instance.state["deleted"] = ["repository:" + json.dumps(legacy_deleted, sort_keys=True)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.input", return_value="yes"):
            instance.uninstall()
        current, history = output.getvalue().split("历史配置", 1)
        self.assertIn("当前配置", current)
        self.assertEqual(current.count("repo-current"), 1)
        self.assertIn("repo-history", history)
        self.assertNotIn("repo-deleted", output.getvalue())
        self.assertEqual(cloud.writes.count("DeleteRepository"), 2)

    def test_uninstall_resumes_when_cloud_resource_is_already_deleted(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("key", {"apiKeyID": "removed-key", "teamID": "t"}, True)
        with patch.object(cloud, "sandbox", side_effect=b.CloudError("gone", "ApiKeyNotFound")), patch("builtins.input", return_value="yes"):
            instance.uninstall()
        self.assertTrue(instance.state["uninstalled"])
        self.assertEqual(len(instance.state["deleted"]), 1)

    def test_uninstall_skips_missing_acr_repository_and_continues(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("repository", {"InstanceId": "cri-test", "RepoId": "repo-missing"}, True)
        instance.record("namespace", {"InstanceId": "cri-test", "NamespaceName": "demo"}, True)
        rpc = cloud.rpc

        def delete(service, action, **params):
            if action == "DeleteRepository":
                raise b.CloudError("ACR DeleteRepository failed", "REPO_NOT_EXIST")
            return rpc(service, action, **params)

        output = io.StringIO()
        with patch.object(cloud, "rpc", side_effect=delete), patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(output):
            instance.uninstall()
        self.assertIn("资源已不存在，跳过 · ACR 仓库 · cri-test / repo-missing", output.getvalue())
        self.assertIn("DeleteNamespace", cloud.writes)
        self.assertTrue(instance.state["uninstalled"])

    def test_uninstall_does_not_hide_permission_or_dependency_failures(self):
        for code in ("Forbidden", "ResourceInUse", "SystemError"):
            cloud = Account()
            instance = b.Bootstrap(self.root, cloud)
            instance.record("key", {"apiKeyID": "retained-key", "teamID": "t"}, True)
            with patch.object(cloud, "sandbox", side_effect=b.CloudError("failed", code)), patch("builtins.input", return_value="yes"):
                instance.uninstall()
            self.assertNotIn("uninstalled", instance.state)
            self.assertEqual(instance.state.get("deleted", []), [])

    def test_uninstall_continues_after_namespace_failure_and_keeps_receipts(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("namespace", {"InstanceId": "cri-test", "NamespaceName": "demo"}, True)
        instance.record("vpc", {"VpcId": "vpc-test"}, True)
        rpc = cloud.rpc

        def reject_namespace(service, action, **params):
            if action == "DeleteNamespace":
                raise b.CloudError("DeleteNamespace: AUTHENTICATION_FAILED", "AUTHENTICATION_FAILED")
            return rpc(service, action, **params)

        output = io.StringIO()
        with patch("builtins.input", return_value="yes"), contextlib.redirect_stdout(output):
            with patch.object(cloud, "rpc", side_effect=reject_namespace):
                instance.uninstall()
            self.assertIn("需手动处理 · ACR 命名空间 · cri-test / demo", output.getvalue())
            self.assertEqual(cloud.writes, ["DeleteVpc"])
            self.assertNotIn("uninstalled", instance.state)
            instance.uninstall()
        self.assertEqual(cloud.writes, ["DeleteVpc", "DeleteNamespace"])
        self.assertTrue(instance.state["uninstalled"])

    def test_pagination_uses_total_when_server_returns_smaller_pages(self):
        cloud = b.Cloud({}, "cn-hangzhou")
        pages = [{"teams": [{"id": i} for i in ids], "total": 5} for ids in ([1, 2], [3, 4], [5])]
        with patch.object(cloud, "sandbox", side_effect=pages):
            rows = cloud.pages("sandbox", "ListTeams", "teams")
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4, 5])

    def test_pagination_rejects_repeated_or_incomplete_pages(self):
        cloud = b.Cloud({}, "cn-hangzhou")
        for second in ([{"id": 1}], []):
            with patch.object(cloud, "sandbox", side_effect=[{"teams": [{"id": 1}], "total": 2}, {"teams": second, "total": 2}]), self.assertRaises(b.CloudError):
                cloud.pages("sandbox", "ListTeams", "teams")

    def test_complete_legacy_configuration_can_keep_its_working_key(self):
        connection = {"QODER_ACCESS_TOKEN": "fake-qoder", "E2B_API_KEY": "fake-existing-key", "E2B_API_URL": "https://api.cn-hangzhou.sandbox.aliyuncs.com", "E2B_DOMAIN": "cn-hangzhou.sandbox.aliyuncs.com"}
        credentials = {"ALIBABA_CLOUD_ACCESS_KEY_ID": "fake-ak", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "fake-sk"}
        registry = {"repository": "test-registry.cn-hangzhou.cr.aliyuncs.com/old/repo", "instance_id": "cri-old", "team_id": "team-old", "region": "cn-hangzhou",
                    "vpc_id": "vpc-old", "vswitch_id": "vsw-old", "security_group_id": "sg-old"}
        cloud = Account()
        cloud.credentials = credentials
        cloud.teams = [{"teamID": "team-other"}, {"teamID": "team-old"}]
        cloud.instances = [
            {"InstanceId": identity, "InstanceName": identity, "InstanceStatus": "RUNNING",
             "InstanceSpecification": "Enterprise_Basic"} for identity in ("cri-other", "cri-old")
        ]
        cloud.vpcs = [{"VpcId": "vpc-old", "CidrBlock": "192.168.0.0/16"}]
        cloud.switches = [{"VSwitchId": "vsw-old", "VpcId": "vpc-old", "ZoneId": "cn-hangzhou-i",
                           "Status": "Available", "AvailableIpAddressCount": 32}]
        cloud.groups = [{"SecurityGroupId": "sg-old", "VpcId": "vpc-old"}]
        cloud.bindings = [{"VpcId": "vpc-old", "Status": "RUNNING"}]
        cloud.namespaces = [{"NamespaceName": "old"}]
        cloud.repos = [{"RepoNamespaceName": "old", "RepoName": "repo", "RepoType": "PRIVATE"}]
        b.ui.write_env_values(self.root / "config/.env", connection)
        b.ui.write_env_values(self.root / "config/.env.cloud", credentials)
        b.ui.write_toml_values(self.root / "config/registry.toml", registry)
        (self.root / "config/app.toml").write_text('[qoder]\nbase_url="https://qoder.invalid"\n')
        original = (self.root / "config/.env").read_bytes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("builtins.input", return_value=""), patch("e2b.Sandbox.list") as listing, patch("qoder_byoc_sandbox_example.qoder.client.API.request") as qoder, patch.object(b, "Cloud", return_value=cloud), patch.object(b, "docker_engine_is_ready", return_value=True), patch.object(b, "docker_login_is_valid", return_value=True) as docker:
            b.run(self.root)
        text = output.getvalue()
        self.assertLess(text.index("2.1 · 验证两个账号入口"), text.index("Qoder 授权可用"))
        self.assertLess(text.index("Qoder 授权可用"), text.index("2.2 · 选择"))
        self.assertLess(text.index("2.2 · 选择"), text.index("2.3 · 选择 Team API Key"))
        self.assertLess(text.index("2.3 · 选择 Team API Key"), text.index("2.4 · 准备镜像仓库实例"))
        self.assertIn("1. 复用 cri-old / cri-old", text)
        self.assertNotIn("选择资源准备方式", text)
        self.assertFalse(cloud.writes)
        docker.assert_called_once_with("test-registry.cn-hangzhou.cr.aliyuncs.com")
        self.assertEqual(listing.return_value.next_items.call_count, 2)
        qoder.assert_called_once_with("GET", "/models")
        self.assertEqual((self.root / "config/.env").read_bytes(), original)
        saved = b.ui.read_json(self.root / "var/state/bootstrap.json", {})
        self.assertFalse(any(row["owned"] for row in saved["resources"].values()))

    def test_replace_mode_offers_existing_team_despite_saved_choice(self):
        cloud = Account()
        bootstrap = self.prepare(cloud)
        cloud.teams.append({"teamID": "team-other", "teamName": "Other"})
        bootstrap.reselect = True
        with patch("builtins.input", return_value="2"):
            self.assertEqual(bootstrap.team()["teamID"], "team-other")
        self.assertFalse(bootstrap.state["resources"]["team"]["owned"])
        self.assertEqual(cloud.writes.count("CreateTeam"), 1)

    def test_reuse_recovers_team_from_the_saved_key_fingerprint(self):
        secret = "fake-existing-key"
        connection = {"E2B_API_KEY": secret, "E2B_API_URL": "https://sandbox.invalid", "E2B_DOMAIN": "sandbox.invalid"}
        b.ui.write_env_values(self.root / "config/.env", connection)
        registry = {"team_id": "team-before", "region": "cn-hangzhou"}
        config_path = self.root / "config/registry.toml"
        b.ui.write_toml_values(config_path, registry)
        state = {
            "resources": {"key": {"value": {"teamID": "team-other", "apiKeyID": "key-other", "fingerprint": "other"}}},
            "history": [{"kind": "key", "value": {"teamID": "team-saved", "apiKeyID": "key-saved",
                                                "fingerprint": b.hashlib.sha256(secret.encode()).hexdigest()}}],
        }
        cloud = Account()
        cloud.teams = [{"teamID": "team-before"}, {"teamID": "team-saved"}]
        cloud.keys = [{"apiKeyID": "key-saved", "status": "active"}]
        original = (self.root / "config/.env").read_bytes()
        with patch("e2b.Sandbox.list") as listing:
            b.restore_saved_team(self.root, connection, cloud, registry, state)
        self.assertEqual(registry["team_id"], "team-saved")
        self.assertEqual(b.ui.load_local(self.root)[2]["team_id"], "team-saved")
        self.assertEqual((self.root / "config/.env").read_bytes(), original)
        self.assertFalse(cloud.writes)
        listing.return_value.next_items.assert_called_once()

    def test_reuse_repairs_only_missing_repository_and_rechecks_it(self):
        cloud = Account()
        bootstrap = self.prepare(cloud)
        namespace = bootstrap.name
        cloud.repos.clear()
        cloud.writes.clear()
        registry = {
            "region": cloud.region, "team_id": "team-1", "instance_id": "cri-test",
            "repository": f"test-registry.cn-hangzhou.cr.aliyuncs.com/{namespace}/qoder-byoc-sandbox-example",
            "vpc_id": "vpc-test", "vswitch_id": "vsw-test", "security_group_id": "sg-test",
        }
        b.ui.write_env_values(self.root / "config/.env", {
            "E2B_API_URL": "https://api.cn-hangzhou.sandbox.aliyuncs.com", "E2B_DOMAIN": "cn-hangzhou.sandbox.aliyuncs.com",
        })
        connection, _, _ = b.ui.load_local(self.root)
        def login(_):
            self.assertFalse(cloud.writes)
            return True

        with patch("e2b.Sandbox.list"), patch("builtins.input", side_effect=["1", "yes"]), patch.object(b, "docker_engine_is_ready", return_value=True), patch.object(b, "docker_login_is_valid", side_effect=login):
            issues = b.resource_issues(connection, cloud, registry, bootstrap.state)
            self.assertEqual(set(issues), {"repository"})
            b.prepare_resources(bootstrap, registry, issues)
            self.assertEqual(b.resource_issues(connection, cloud, registry, bootstrap.state), {})
        self.assertEqual(cloud.writes, ["CreateRepository"])

    def test_failed_resource_query_is_not_treated_as_missing(self):
        cloud = Account()
        with patch.object(cloud, "pages", side_effect=b.CloudError("forbidden", "Forbidden")):
            with self.assertRaisesRegex(b.CloudError, "forbidden"):
                b.resource_issues({}, cloud, {"team_id": "team-1"}, {})
        self.assertFalse(cloud.writes)

    def test_registry_temporary_token_only_goes_through_stdin(self):
        cloud = Account()
        with patch.object(cloud, "rpc", return_value={"TempUsername": "test-user", "AuthorizationToken": "FAKE_ONLY_TOKEN"}), patch.object(b.subprocess, "run", return_value=type("Result", (), {"returncode": 0})()) as process:
            b.registry_login(cloud, "cri-test", "registry.invalid/namespace/repo")
        self.assertNotIn("FAKE_ONLY_TOKEN", process.call_args.args[0])
        self.assertEqual(process.call_args.kwargs["input"], "FAKE_ONLY_TOKEN\n")

    def test_login_adds_missing_egress_and_waits_for_acl_readback(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("acr", {"InstanceId": "cri-test"}, True)
        host = "test-registry.cn-hangzhou.cr.aliyuncs.com"
        entries = [{"Entry": "198.51.100.1/32"}]
        request = cloud.rpc

        def rpc(service, action, **params):
            if action == "GetInstanceEndpoint":
                return {"Status": "RUNNING", "Enable": True, "AclEnable": True, "Domains": [{"Domain": host}], "AclEntries": entries[:]}
            if action == "CreateInstanceEndpointAclPolicy":
                entries.extend(json.loads(params["Entries"]))
                return {}
            if action == "GetAuthorizationToken":
                return {"TempUsername": "test-user", "AuthorizationToken": "FAKE_ONLY_TOKEN"}
            return request(service, action, **params)

        def login(*_):
            self.assertEqual([e["Entry"] for e in entries],
                             ["198.51.100.1/32", "203.0.113.2/32", "203.0.113.3/32"])

        addresses = [b.ipaddress.IPv4Address(ip) for ip in ("203.0.113.2", "203.0.113.3")]
        with patch.object(cloud, "rpc", side_effect=rpc) as api, patch.object(b, "local_private_network", return_value="local"), patch.object(b, "public_egress_ipv4s", return_value=addresses), patch.object(b, "docker_engine_is_ready", return_value=True), patch.object(b, "docker_login_is_valid", return_value=False), patch.object(b, "docker_password_login", side_effect=login) as docker, patch("builtins.input", return_value="yes") as approve:
            instance.prepare_registry_login("cri-test", host + "/example/repo")
        self.assertEqual([call.args[1] for call in api.call_args_list],
                         ["GetInstanceEndpoint", "GetInstanceEndpoint", "GetInstanceEndpoint", "CreateInstanceEndpointAclPolicy", "CreateInstanceEndpointAclPolicy", "GetInstanceEndpoint", "GetAuthorizationToken"])
        approve.assert_called_once()
        docker.assert_called_once()

    def test_acl_readback_timeout_blocks_tokens_and_docker_on_every_retry(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("acr", {"InstanceId": "cri-test"}, True)
        endpoint = {"Status": "RUNNING", "Enable": True, "AclEnable": True,
                    "Domains": ["registry.invalid"], "AclEntries": []}
        address = b.ipaddress.IPv4Address("203.0.113.2")

        def wait(description, probe, seconds=600):
            if "入口" in description:
                return endpoint
            raise b.CloudError("白名单尚未生效", "AcrEndpoint")

        with (patch.object(cloud, "rpc", return_value=endpoint) as api,
              patch.object(instance, "wait", side_effect=wait),
              patch.object(b, "public_egress_ipv4s", return_value=[address]),
              patch.object(b, "docker_engine_is_ready", return_value=True),
              patch.object(b, "docker_login_is_valid") as existing,
              patch.object(b, "docker_password_login") as login,
              patch.object(b.sys.stdin, "isatty", return_value=False),
              patch("builtins.input", return_value="yes")):
            for _ in range(2):
                with self.assertRaisesRegex(b.CloudError, "白名单尚未生效"):
                    instance.prepare_registry_login("cri-test", prefer_existing=True)
        self.assertNotIn("GetAuthorizationToken", [call.args[1] for call in api.call_args_list])
        existing.assert_not_called()
        login.assert_not_called()

    def test_registry_preparation_waits_for_each_real_readiness_state(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.record("acr", {"InstanceId": "cri-test"}, True)
        events = []
        reads = 0
        submitted = False
        acl_reads = 0

        def rpc(service, action, **params):
            nonlocal reads, submitted, acl_reads
            events.append(action)
            if action == "GetInstanceEndpoint":
                reads += 1
                if submitted:
                    acl_reads += 1
                return {
                    "Enable": reads > 1, "Status": "RUNNING" if reads >= 3 else "CREATING",
                    "Domains": ["registry.invalid"], "AclEnable": True,
                    "AclEntries": [{"Entry": "203.0.113.2/32"}] if acl_reads >= 2 else [],
                }
            if action == "UpdateInstanceEndpointStatus":
                return {}
            if action == "CreateInstanceEndpointAclPolicy":
                self.assertGreaterEqual(reads, 3)
                self.assertEqual(json.loads(params["Entries"])[0]["Entry"], "203.0.113.2/32")
                submitted = True
                return {}
            if action == "GetAuthorizationToken":
                self.assertGreaterEqual(acl_reads, 2)
                return {"TempUsername": "user", "AuthorizationToken": "fake-token"}
            raise AssertionError(action)

        with (patch.object(cloud, "rpc", side_effect=rpc),
              patch.object(b, "public_egress_ipv4s", return_value=[b.ipaddress.IPv4Address("203.0.113.2")]),
              patch.object(b, "docker_engine_is_ready", return_value=True),
              patch.object(b, "docker_password_login") as login,
              patch.object(b.time, "sleep"), patch("builtins.input", return_value="yes")):
            instance.prepare_registry_login("cri-test")
        self.assertLess(events.index("UpdateInstanceEndpointStatus"), events.index("CreateInstanceEndpointAclPolicy"))
        self.assertLess(events.index("CreateInstanceEndpointAclPolicy"), events.index("GetAuthorizationToken"))
        login.assert_called_once()

    def test_bootstrap_lock_blocks_active_owner_but_releases_after_exit(self):
        with b.locked(self.root):
            with self.assertRaisesRegex(b.CloudError, "PID"):
                with b.locked(self.root):
                    self.fail("active lock was bypassed")
        with b.locked(self.root):
            pass

    def test_existing_egress_allowlist_is_unchanged(self):
        cloud = Account()
        endpoint = {"Status": "RUNNING", "Enable": True, "AclEnable": True, "Domains": ["registry.invalid"],
                    "AclEntries": [{"Entry": "203.0.113.2/32"}]}
        with patch.object(cloud, "rpc", return_value=endpoint) as api, patch.object(b, "docker_engine_is_ready", return_value=True), patch.object(b, "docker_login_is_valid", return_value=True), patch.object(b, "registry_login") as temporary:
            b.ensure_registry_login(cloud, "cri-test", "registry.invalid/example/repo")
        api.assert_called_once()
        temporary.assert_not_called()

    def test_acr_network_failure_shows_acl_and_rechecks_manual_login(self):
        cloud = Account()
        endpoint = {"Status": "RUNNING", "Enable": True, "AclEnable": True, "Domains": ["registry.invalid"],
                    "AclEntries": [{"Entry": "203.0.113.2/32"}]}
        addresses = [b.ipaddress.IPv4Address(ip) for ip in ("203.0.113.2", "203.0.113.3")]
        output = io.StringIO()
        error = b.CloudError("Docker 原始报错（已脱敏）\n    timeout\n  Docker 连接超时", "DockerNetwork")
        with (contextlib.redirect_stdout(output),
              patch.object(cloud, "rpc", return_value=endpoint),
              patch.object(b, "docker_engine_is_ready", return_value=True),
              patch.object(b, "docker_login_is_valid", side_effect=[False, True]),
              patch.object(b, "registry_login", side_effect=error) as temporary,
              patch.object(b, "registry_probe", return_value="终端无响应") as probe,
              patch.object(b, "public_egress_ipv4s", return_value=addresses),
              patch.object(b.sys.stdin, "isatty", return_value=True),
              patch.object(output, "isatty", return_value=True),
              patch.dict(b.os.environ, {"TERM": "xterm"}, clear=True),
              patch("builtins.input", side_effect=["detail", "shell", ""]),
              patch.object(b.subprocess, "run", return_value=None) as subprocess_run):
            self.assertEqual(b.ensure_registry_login(cloud, "cri-test", interactive=True), "registry.invalid")
        temporary.assert_called_once()
        subprocess_run.assert_not_called()
        probe.assert_called_once_with("registry.invalid")
        self.assertIn("\033[91mDocker 连接超时", output.getvalue())
        self.assertIn("\033[91m使用 VPN 或阿里郎时", output.getvalue())
        self.assertLess(output.getvalue().index("IP.SB 查询出口"), output.getvalue().index("Docker 原始报错（已脱敏）"))
        self.assertIn("203.0.113.2 · 已在白名单内", output.getvalue())
        self.assertIn("203.0.113.3 · 未在白名单内", output.getvalue())
        self.assertIn("https://cr.console.aliyun.com/cn-hangzhou/instance/cri-test/acl", output.getvalue())
        self.assertIn("docker login registry.invalid --username", output.getvalue())

    def test_acr_preflight_requires_a_running_docker_engine(self):
        cloud = Account()
        with patch.object(b, "docker_engine_is_ready", return_value=False), patch.object(b, "docker_login_is_valid") as existing, patch.object(b, "registry_login") as temporary:
            with self.assertRaisesRegex(b.CloudError, "Docker 引擎不可用"):
                b.ensure_registry_login(cloud, "cri-test")
        existing.assert_not_called()
        temporary.assert_not_called()

    def test_legacy_registry_order_preserves_existing_name_on_retry(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.state["operations"]["acr-purchase"] = {"status": "pending", "token": "original-token"}
        with patch.object(cloud, "rpc", wraps=cloud.rpc) as api:
            instance.acr()
        order = next(call.kwargs for call in api.call_args_list if call.args[1] == "CreateInstance")
        self.assertEqual(order["ClientToken"], "original-token")
        self.assertEqual(order["Parameter.2.Value"], instance.legacy_name)
        self.assertTrue(instance.name.startswith("qoder-byoc-sandbox-"))
        self.assertLessEqual(len(instance.name), 30)

    def test_acr_error_code_without_success_flag_is_reported(self):
        with self.assertRaisesRegex(b.ui.AcrError, "INSTANCE_NOT_EXIST.*找不到"):
            b.ui.check_acr_response("GetInstanceEndpoint", {"Code": "INSTANCE_NOT_EXIST"})

    def test_interrupted_acr_purchase_waits_for_the_paid_instance(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.reselect = True
        instance.state["operations"]["acr-purchase"] = {
            "status": "done", "result": {"Data": {"InstanceId": "cri-paid"}},
        }
        purchased = {"InstanceId": "cri-paid", "InstanceName": "paid", "InstanceSpecification": "Enterprise_Basic"}
        with (patch.object(cloud, "pages", side_effect=[
                [{**purchased, "InstanceStatus": "CREATING"}],
                [{**purchased, "InstanceStatus": "RUNNING"}],
              ]), patch("builtins.input") as read):
            self.assertEqual(instance.acr()["InstanceId"], "cri-paid")
        read.assert_not_called()
        self.assertFalse(cloud.writes)
        self.assertTrue(instance.state["resources"]["acr"]["owned"])

    def test_missing_paid_acr_requires_new_approval_before_reordering(self):
        cloud = Account()
        instance = b.Bootstrap(self.root, cloud)
        instance.dedicated = True
        instance.state["approved_acr_order"] = instance.acr_order()
        instance.state["operations"]["acr-purchase"] = {"status": "done", "result": {"Data": {"InstanceId": "cri-old"}}}
        instance.record("acr", {"InstanceId": "cri-old"}, True)
        request = cloud.rpc

        def rpc(service, action, **params):
            if action == "GetInstanceEndpoint":
                raise b.CloudError("instance absent", "INSTANCE_NOT_EXIST")
            return request(service, action, **params)

        with patch.object(cloud, "rpc", side_effect=rpc):
            with patch("builtins.input", side_effect=["", KeyboardInterrupt]), self.assertRaises(KeyboardInterrupt):
                instance.acr()
            self.assertFalse(cloud.writes)
            with patch("builtins.input", return_value="yes"):
                self.assertEqual(instance.acr()["InstanceId"], "cri-test")
        self.assertEqual(cloud.writes, ["CreateInstance"])
        self.assertEqual(instance.state["completed_operations"][0]["acr-purchase"]["result"]["Data"]["InstanceId"], "cri-old")

    def test_docker_network_failure_reports_original_error_without_secrets(self):
        failure = b.subprocess.CompletedProcess([], 1, "", "deadline exceeded at https://registry.invalid/v2/?Signature=FAKE_SIGNATURE with FAKE_ONLY_TOKEN")
        with patch.object(b.subprocess, "run", return_value=failure), self.assertRaises(b.CloudError) as error:
            b.docker_password_login("registry.invalid", "user", "FAKE_ONLY_TOKEN")
        self.assertEqual(error.exception.code, "DockerNetwork")
        self.assertIn("deadline exceeded", str(error.exception))
        self.assertNotIn("FAKE_SIGNATURE", str(error.exception))
        self.assertNotIn("FAKE_ONLY_TOKEN", str(error.exception))

    def test_docker_proxy_http_error_is_distinct_from_registry_auth(self):
        failure = b.subprocess.CompletedProcess([], 1, "", "407 Proxy Authentication Required, FAKE_ONLY_TOKEN")
        with patch.object(b.subprocess, "run", return_value=failure), self.assertRaises(b.CloudError) as error:
            b.docker_password_login("registry.invalid", "user", "FAKE_ONLY_TOKEN")
        self.assertEqual(error.exception.code, "DockerNetwork")
        self.assertIn("HTTP 407", str(error.exception))
        self.assertNotIn("FAKE_ONLY_TOKEN", str(error.exception))

    def test_legacy_docker_login_can_be_kept_without_token_management_permission(self):
        b.ui.write_env_values(self.root / "config/.env.cloud", {"ALIBABA_CLOUD_ACCESS_KEY_ID": "fake-ak", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "fake-sk"})
        b.ui.write_toml_values(self.root / "config/registry.toml", {"region": "cn-hangzhou", "repository": "registry.invalid/namespace/repo", "instance_id": "cri-old"})
        with patch.object(b.Cloud, "rpc", return_value={"Status": "RUNNING", "Enable": True, "Domains": ["registry.invalid"]}) as api, patch.object(b, "docker_engine_is_ready", return_value=True), patch.object(b.subprocess, "run", return_value=b.subprocess.CompletedProcess([], 0)) as process:
            b.ui.docker_login(self.root)
        process.assert_called_once_with(["docker", "login", "registry.invalid"], stdin=b.subprocess.DEVNULL, capture_output=True, timeout=15)
        api.assert_called_once()

    def test_prepaid_purchase_ownership_is_recovered_from_nested_result(self):
        for result in ({"Data": {"InstanceId": "cri-paid"}}, {"data": {"instanceId": "cri-paid"}}):
            instance = b.Bootstrap(self.root, Account())
            instance.state["operations"]["acr-purchase"] = {"status": "done", "result": result}
            instance.save()
            restored = b.Bootstrap(self.root, Account())
            restored.record("acr", {"InstanceId": "cri-paid"})
            self.assertTrue(restored.state["resources"]["acr"]["owned"])

    def test_vpc_discovery_uses_supported_page_size_and_reads_all_pages(self):
        received = []
        include_total = [True]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                action = self.headers["x-acs-action"]
                query = parse_qs(urlsplit(self.path).query)
                page, size = int(query["PageNumber"][0]), int(query["PageSize"][0])
                received.append((action, page, size))
                code = 200
                if size > 50:
                    code, body = 400, {"Code": "InvalidParameter", "Message": 'The specified parameter "PageSize" is not valid.'}
                else:
                    rows = [{"id": n} for n in range((page - 1) * size, min(page * size, 75))]
                    outer, inner = {"DescribeVSwitches": ("VSwitches", "VSwitch"), "DescribeVpcs": ("Vpcs", "Vpc")}[action]
                    body = {outer: {inner: rows}}
                    if include_total[0]: body["TotalCount"] = 75
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from alibabacloud_tea_openapi.models import Config
        cloud = b.Cloud({"ALIBABA_CLOUD_ACCESS_KEY_ID": "test", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test"}, "cn-hangzhou")
        with patch.object(cloud, "config", return_value=Config(access_key_id="test", access_key_secret="test", protocol="HTTP", endpoint=f"127.0.0.1:{server.server_port}")):
            for total in [True, False]:
                include_total[0] = total
                for action, collection in [("DescribeVSwitches", "VSwitches.VSwitch"), ("DescribeVpcs", "Vpcs.Vpc")]:
                    with self.subTest(action=action, total=total):
                        received.clear()
                        rows = cloud.pages("vpc", action, collection, RegionId="cn-hangzhou")
                        self.assertEqual(rows, [{"id": n} for n in range(75)])
                        self.assertEqual(received, [(action, 1, 50), (action, 2, 50)])

    def test_sdk_requests_use_actual_serialization_and_signature(self):
        received = []
        key_info = {"apiKeyID": "k", "teamID": "t", "status": "active",
                    "ipWhitelist": [{"ipAddress": "203.0.113.1/32"}]}
        responses = {
            "ListTeams": {"teams": [], "total": 0},
            "CreateTeam": {"team": {"teamID": "t"}},
            "CreateApiKey": {"apiKey": {"apiKeyID": "k", "apiKeyValue": "test"}},
            "DescribeApiKey": {"apiKey": key_info},
            "UpdateApiKey": {"apiKey": {**key_info, "status": "inactive"}},
            "DeleteApiKey": {},
            "ListInstance": {"IsSuccess": True, "Instances": []},
            "ListResourceGroups": {
                "ResourceGroups": {"ResourceGroup": [{"Id": "rg-test", "AccountId": "100", "Status": "OK"}]},
                "TotalCount": 1,
            },
        }
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                received.append((self.headers.get("x-acs-action"), self.path, self.rfile.read(int(self.headers.get("Content-Length", 0))), self.headers.get("Authorization")))
                raw = json.dumps(responses[received[-1][0]]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            do_GET = do_POST
            do_PUT = do_POST
            do_DELETE = do_POST
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        from alibabacloud_tea_openapi.models import Config
        cloud = b.Cloud({"ALIBABA_CLOUD_ACCESS_KEY_ID": "test", "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test"}, "cn-hangzhou")
        with patch.object(cloud, "config", return_value=Config(access_key_id="test", access_key_secret="test", protocol="HTTP", endpoint=f"127.0.0.1:{server.server_port}")):
            self.assertEqual(cloud.pages("sandbox", "ListTeams", "teams"), [])
            cloud.sandbox("CreateTeam", body={"teamName": "demo", "resourceGroupID": "rg-test", "plan": "pro"})
            cloud.sandbox("CreateApiKey", body={"teamID": "t", "apiKeyName": "demo"})
            cloud.rpc("cr", "ListInstance", PageNo=1, PageSize=100)
            groups = cloud.pages("resourcemanager", "ListResourceGroups", "ResourceGroups.ResourceGroup", Status="OK")
            self.assertEqual([g["Id"] for g in groups], ["rg-test"])
            bootstrap = b.Bootstrap(self.root, Account())
            bootstrap.cloud = cloud
            bootstrap.delete_team_key("t", "k")
        self.assertTrue(all(row[3] for row in received))
        self.assertEqual(json.loads(received[1][2])["teamName"], "demo")
        self.assertEqual(json.loads(received[2][2])["teamID"], "t")
        self.assertEqual(parse_qs(urlsplit(received[3][1]).query)["PageSize"], ["100"])
        query = parse_qs(urlsplit(received[4][1]).query)
        self.assertEqual(query, {"PageNumber": ["1"], "PageSize": ["100"], "Status": ["OK"]})
        self.assertEqual([row[0] for row in received[5:]], ["DescribeApiKey", "UpdateApiKey", "DeleteApiKey"])
        self.assertTrue(all(row[1] == "/pop/2026-08-20/api-keys/k" for row in received[5:]))
        self.assertEqual(json.loads(received[6][2]), {
            "status": "inactive", "ipWhitelist": key_info["ipWhitelist"], "ipBlacklist": [],
        })
