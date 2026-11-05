#!/usr/bin/env python3
"""账号资源准备。只读发现、明确选择、写前确认、按账号记录恢复。

不读取原仓库凭证；不重置业务 Key；SDK 错误仅输出错误码和 request ID。
"""
import base64
import contextlib
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.error
from urllib.parse import quote, quote_plus
import urllib.request

import onboarding as ui

RESOURCE_PREFIX = "qoder-byoc-sandbox-"
ACR_REFUND_PATH = "费用与成本 → 订单管理 → 退订管理"
ACR_REFUND_DOC = "https://help.aliyun.com/zh/acr/product-overview/refund-policy"

# Official FC network documentation, checked 2026-10-29. Unknown regions fail
# closed until this compatibility list is refreshed, rather than guessing a zone.
FC_ZONES = {
    "cn-hangzhou": "h i j k f g", "cn-shanghai": "m l n b e g f",
    "cn-beijing": "i h k j l c e g f", "cn-shenzhen": "e d f",
    "cn-qingdao": "c", "cn-zhangjiakou": "b c a", "cn-huhehaote": "a b",
    "cn-chengdu": "a b", "cn-hongkong": "d c b",
    "ap-southeast-1": "a c b", "ap-southeast-3": "a",
    "ap-southeast-5": "a b", "ap-northeast-1": "c b a",
    "eu-west-1": "a", "eu-central-1": "a b", "us-west-1": "a b", "us-east-1": "b a",
}


def confirm(message, *, enter_skips=False):
    print("\n准备执行：" + message)
    prompt = "输入 yes 确认；回车逐项选择；Ctrl+C 退出：" if enter_skips else "输入 yes 确认；Ctrl+C 退出："
    while True:
        answer = input(prompt).strip()
        if answer == "yes":
            return True
        if enter_skips and not answer:
            return False
        print("尚未批准，未执行此操作。请按提示输入；Ctrl+C 可退出。")


def choose_number(title, labels):
    print("\n" + title)
    options = {}
    for index, label in enumerate(labels, 1):
        print(f"  {index}. {label}")
        options[str(index)] = index
    while True:
        answer = input("选择编号 [1]：").strip() or "1"
        if answer in options:
            return options[answer]
        print("请选择列出的项目；Ctrl+C 可退出。")


def choose(title, rows, label, allow_new=True):
    if not rows and allow_new:
        print(f"\n{title}\n  未找到可复用项，接下来可确认新建。")
        return None
    labels = [("复用 " if allow_new else "") + label(row) for row in rows]
    if allow_new:
        labels.append("新建专用资源")
    selected = choose_number(title, labels)
    return rows[selected - 1] if selected <= len(rows) else None


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class CloudError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class Cloud:
    def __init__(self, credentials, region):
        self.credentials, self.region = credentials, region

    def config(self, endpoint):
        from alibabacloud_tea_openapi.models import Config
        return Config(access_key_id=self.credentials["ALIBABA_CLOUD_ACCESS_KEY_ID"],
                      access_key_secret=self.credentials["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
                      security_token=self.credentials.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
                      endpoint=endpoint, region_id=self.region,
                      connect_timeout=10000, read_timeout=30000)

    @staticmethod
    def error(action, exc):
        code = str(getattr(exc, "code", None) or type(exc).__name__)
        data = getattr(exc, "data", None) or {}
        request_id = data.get("RequestId", data.get("requestId", "unknown")) if isinstance(data, dict) else "unknown"
        return CloudError(f"{action} 失败：{code}（RequestId={request_id}）。未将失败当作资源不存在。"
                          "请检查权限、开通、余额、配额和地域；完成后重跑。服务入口：https://console.aliyun.com/sandbox/", code)

    def rpc(self, service, action, **query):
        from alibabacloud_tea_openapi.client import Client
        from alibabacloud_tea_openapi.models import Params, OpenApiRequest
        from alibabacloud_tea_openapi.utils import Utils
        from alibabacloud_tea_util.models import RuntimeOptions
        version, endpoint = {
            "cr": ("2018-12-01", f"cr.{self.region}.aliyuncs.com"),
            "vpc": ("2016-04-28", f"vpc.{self.region}.aliyuncs.com"),
            "ecs": ("2014-05-26", f"ecs.{self.region}.aliyuncs.com"),
            "sts": ("2015-04-01", "sts.aliyuncs.com"),
            "resourcemanager": ("2020-03-31", "resourcemanager.aliyuncs.com"),
            "bssopenapi": ("2017-12-14", "business.aliyuncs.com"),
        }[service]
        try:
            result = Client(self.config(endpoint)).call_api(
                Params(action=action, version=version, protocol="HTTPS", pathname="/", method="POST",
                       auth_type="AK", style="RPC", req_body_type="formData", body_type="json"),
                OpenApiRequest(query=Utils.query(query)), RuntimeOptions(autoretry=False))["body"]
            if service == "cr":
                ui.check_acr_response(action, result)
            elif result.get("IsSuccess") is False or result.get("Success") is False or result.get("success") is False:
                code = result.get('Code', result.get('code'))
                raise CloudError(f"{action}: {code}; RequestId={result.get('RequestId')}", code)
            return result
        except ui.AcrError as exc:
            raise CloudError(str(exc), exc.code) from None
        except CloudError:
            raise
        except Exception as exc:
            raise self.error(action, exc) from None

    def sandbox(self, action, body=None, path_id=None, **fields):
        from alibabacloud_sandbox20260820.client import Client
        from alibabacloud_sandbox20260820 import models
        request_type = getattr(models, action + "Request")
        request = request_type.from_map(request_type(), {"body": body, **fields})
        method = re.sub(r"(?<!^)(?=[A-Z])", "_", action).lower()
        try:
            args = (path_id, request) if path_id else (request,)
            response = getattr(Client(self.config(f"sandbox.{self.region}.aliyuncs.com")), method)(*args)
            result = response.body.to_map() if response.body is not None else {}
            if result.get("code") not in {None, "Success", "success", "200", "OK"}:
                raise CloudError(f"{action}: {result.get('code')}; RequestId={result.get('requestId')}", result.get("code"))
            return result
        except CloudError:
            raise
        except Exception as exc:
            raise self.error(action, exc) from None

    def pages(self, service, action, collection, **query):
        rows = []
        seen = set()
        # VPC discovery needs a smaller page size; use it for both requests
        # and the end-of-list check when TotalCount is omitted.
        page_size = 50 if service == "vpc" else 100
        for page in range(1, 10001):
            if service == "sandbox":
                result = self.sandbox(action, pageNumber=page, pageSize=page_size, **query)
            else:
                page_field = "PageNumber" if service in {"vpc", "ecs", "resourcemanager"} else "PageNo"
                result = self.rpc(service, action, **{page_field: page, "PageSize": page_size}, **query)
            values = result
            for key in collection.split("."):
                values = values.get(key, {})
            if not isinstance(values, list):
                raise CloudError(action + " 返回结构不符合预期，停止选择。")
            fingerprint = json.dumps(values, sort_keys=True)
            if values and fingerprint in seen:
                raise CloudError(action + " 重复返回同一页，未使用不完整列表。")
            seen.add(fingerprint)
            rows.extend(values)
            total = result.get("TotalCount", result.get("total"))
            if total is not None:
                if len(rows) >= int(total):
                    return rows
                if not values:
                    raise CloudError(action + " 列表提前结束，未使用不完整列表。")
            elif len(values) < page_size:
                return rows
        raise CloudError("分页超过限制，未使用不完整列表。")


class Bootstrap:
    RESOURCE_LABELS = {
        "team": ui.SANDBOX_TEAM_LABEL, "key": ui.TEAM_API_KEY_LABEL, "acr": "ACR 实例", "vpc": "VPC",
        "switch": "交换机", "sg": "安全组", "repository": "ACR 仓库", "namespace": "ACR 命名空间",
        "binding": "ACR VPC 绑定", "acl": "ACR 公网白名单", "public-endpoint": "ACR 公网入口",
    }
    IDENTITY_FIELDS = {
        "team": ("teamID",), "key": ("apiKeyID",), "acr": ("InstanceId",),
        "vpc": ("VpcId",), "switch": ("VSwitchId",), "sg": ("SecurityGroupId",),
        "repository": ("InstanceId", "RepoId"),
        "namespace": ("InstanceId", "NamespaceName"),
        "binding": ("InstanceId", "VpcId"), "acl": ("InstanceId", "Entry"),
        "public-endpoint": ("InstanceId",),
    }

    def __init__(self, root, cloud):
        self.root, self.cloud = Path(root), cloud
        who = cloud.rpc("sts", "GetCallerIdentity")
        self.account = str(who["AccountId"])
        self.path = self.root / "var/state/bootstrap.json"
        self.state = ui.read_json(self.path, {})
        identity = {"account": self.account, "region": cloud.region}
        if self.state and any(self.state.get(k) != v for k, v in identity.items()):
            raise CloudError("准备记录属于其他账号或地域；请在独立目录运行，或先回收原账号资源。")
        self.state = self.state or {**identity, "resources": {}, "operations": {}, "id": uuid.uuid4().hex[:10]}
        if self.state.get("uninstalled"):
            atomic_json(self.path.with_name("bootstrap-" + self.state["id"] + "-uninstalled.json"), self.state)
            self.state = {**identity, "resources": {}, "operations": {}, "id": uuid.uuid4().hex[:10]}
        self.name = RESOURCE_PREFIX + self.state["id"]
        self.legacy_name = "qsd-" + self.state["id"]
        self.dedicated = False
        self.reselect = False
        self.preferred = {}
        self.save()
        print(f"  ✓ 阿里云身份已验证 · 账号 {self.account} · {cloud.region}")

    def save(self):
        atomic_json(self.path, self.state)

    def record(self, kind, value, owned=False, *, reused=False, announce=True):
        old = self.state["resources"].get(kind, {})
        keys = self.IDENTITY_FIELDS.get(kind, ())
        same = bool(keys) and all(value.get(k) and value.get(k) == old.get("value", {}).get(k) for k in keys)
        if old and not same:
            self.state.setdefault("history", []).append({"kind": kind, **old})
        recovered = False
        for name, operation in self.state["operations"].items():
            operation_name = operation.get("name", name)
            if operation_name == "acr-purchase":
                operation_name = "acr"
            if operation.get("status") != "done" or operation_name != kind:
                continue
            result = operation.get("result", {})
            result = result.get("team", result) if isinstance(result, dict) else {}
            if kind == "acr":
                result = result.get("Data", result.get("data", result))
                if "instanceId" in result and "InstanceId" not in result:
                    result = {**result, "InstanceId": result["instanceId"]}
            created = {**operation.get("scope", {}), **result}
            if keys and all(value.get(k) and value.get(k) == created.get(k) for k in keys):
                recovered = True
                break
        self.state["resources"][kind] = {"value": value, "owned": owned or recovered or (same and old.get("owned", False))}
        self.save()
        identity = " / ".join(str(value[k]) for k in keys if value.get(k))
        if kind == "repository" and value.get("RepoNamespaceName") and value.get("RepoName"):
            identity += f" · {value['RepoNamespaceName']}/{value['RepoName']}"
        action = "新建" if owned and not (reused or same) else "复用"
        if announce:
            print(f"  ✓ {action} {self.RESOURCE_LABELS[kind]} · {identity}")
        return value

    @staticmethod
    def operation_key(name, scope=None):
        if not scope:
            return name
        suffix = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:20]
        return name + ":" + suffix

    def mutate(self, name, description, fn, idempotent=False, *, scope=None, confirmed=False, resource_name=None):
        key = self.operation_key(name, scope)
        operation = self.state["operations"].get(key)
        legacy = self.state["operations"].get(name, {}) if scope else {}
        if not operation and legacy.get("status") == "pending":
            raise CloudError(f"旧版 {name} 操作结果未知且未记录目标资源，请先核对后再续跑。")
        if operation and operation["status"] == "done":
            return operation["result"]
        if operation and operation["status"] == "pending" and not idempotent:
            raise CloudError(f"{name} 上次结果未知。请先核对云端资源；不能盲目重复创建。记录：{self.path}")
        if name in {"namespace", "repository", "switch"} and any(
            old_key != key and old.get("name") == name and old["status"] == "pending"
            for old_key, old in self.state["operations"].items()
        ):
            raise CloudError(f"{name} 有结果未确认的创建请求。请先核对云端资源，再选择已有项或继续原请求。")
        covered = name in {"team", "vpc", "switch", "sg", "namespace", "repository", "public-endpoint", "acl"} or name.startswith(("key-", "binding-"))
        if self.dedicated and covered:
            print("  · " + description)
        elif not confirmed:
            confirm(description)
        token = operation["token"] if operation else str(uuid.uuid4())
        if operation and operation.get("status") == "pending":
            resource_name = operation.get("resource_name", self.legacy_name)
        self.state["operations"][key] = {"token": token, "status": "pending", "description": description, "name": name, "scope": scope or {}, "resource_name": resource_name or self.name}
        self.save()
        try:
            result = fn(token)
        except CloudError as exc:
            # An explicit permission/input rejection is retryable after fixing
            # it; a network timeout or server failure has an unknown outcome.
            if any(x in str(exc.code).lower() for x in ("forbidden", "noprivilege", "accessdenied", "invalidparameter", "unauthorized")):
                self.state["operations"][key]["status"] = "rejected"
                self.save()
            raise
        # Operation receipts contain only resource IDs and credential fingerprints.
        self.state["operations"][key].update(status="done", result=result)
        self.save()
        return result

    def missing(self, name, scope=None):
        """Only after an authoritative list proves an old completed resource absent."""
        self.archive_operation(name, scope)

    def archive_operation(self, name, scope=None):
        name = self.operation_key(name, scope)
        old = self.state["operations"].get(name)
        if old and old["status"] == "done":
            self.state.setdefault("completed_operations", []).append({name: old})
            del self.state["operations"][name]
            self.save()

    def confirm_replacement(self, name, description, scope=None):
        operation = self.state["operations"].get(self.operation_key(name, scope), {})
        if self.reselect and operation.get("status") == "done":
            confirm(description)
            self.archive_operation(name, scope)
            return True
        return False

    def wait(self, description, probe, seconds=600):
        end = time.monotonic() + seconds
        started = time.monotonic()
        last_notice = -30
        while time.monotonic() < end:
            value = probe()
            if value:
                return value
            elapsed = int(time.monotonic() - started)
            if elapsed - last_notice >= 30:
                print("  · " + description + f" · 已等待 {elapsed}s，自动核对中", flush=True)
                last_notice = elapsed
            time.sleep(5)
        raise CloudError(description + "超时；保留记录，重跑继续核对。")

    def network_resource_name(self, operation, scope=None):
        saved = self.state["operations"].get(self.operation_key(operation, scope), {})
        if saved.get("status") == "pending":
            return saved.get("resource_name", self.legacy_name)
        return self.name

    def team(self):
        rows = self.cloud.pages("sandbox", "ListTeams", "teams")
        previous = self.preferred_id("team", "teamID", "team_id")
        operation = self.state["operations"].get("team", {})
        recovery_name = operation.get("resource_name", self.legacy_name)
        match = next((r for r in rows if r["teamID"] == previous or
                      (operation and r.get("teamName") == recovery_name)), None)
        if self.reselect or (not match and not self.dedicated):
            rows.sort(key=lambda row: row["teamID"] != previous)
            match = choose(f"选择{ui.SANDBOX_TEAM_LABEL}（生产业务请使用独立 Team）", rows,
                           lambda r: f"{r.get('teamName')} / {r['teamID']} / {r.get('plan')}")
        if match:
            return self.record("team", match, match.get("teamName") == recovery_name and operation.get("status") in {"pending", "done"}, reused=True)
        groups = self.cloud.pages("resourcemanager", "ListResourceGroups", "ResourceGroups.ResourceGroup", Status="OK")
        groups = [group for group in groups if isinstance(group, dict)
                  and str(group.get("AccountId")) == self.account and group.get("Status") == "OK"
                  and isinstance(group.get("Id"), str) and group["Id"].startswith("rg-") and len(group["Id"]) > 3]
        if not groups:
            raise CloudError("未找到当前账号下的可用资源组；请在资源管理控制台核对资源组状态及查看权限：https://resourcemanager.console.aliyun.com/resource-groups")
        def group_label(group):
            name = group.get("DisplayName") or group.get("Name")
            return f"{name} / {group['Id']}" if name else group["Id"]
        group = groups[0] if len(groups) == 1 else choose("选择新 Team 所属的资源组", groups, group_label, allow_new=False)
        name = self.name
        if any(row.get("teamName") == name for row in rows):
            name += "-" + uuid.uuid4().hex[:6]
        description = f"在资源组 {group_label(group)} 新建{ui.SANDBOX_TEAM_LABEL} {name}（QoS Default，Sandbox 按量计费）。"
        confirmed = self.confirm_replacement("team", description)
        if not self.reselect:
            self.missing("team")
        result = self.mutate("team", description,
                             lambda _: self.cloud.sandbox("CreateTeam", body={"teamName": name, "resourceGroupID": group["Id"], "plan": "std", "description": "Qoder BYOC sandbox example"}),
                             confirmed=confirmed, resource_name=name)
        return self.record("team", result["team"], True)

    def preferred_id(self, kind, field, config_field):
        return self.state["resources"].get(kind, {}).get("value", {}).get(field) or self.preferred.get(config_field)

    def key(self, team, *, allow_reuse=True):
        connection, _, _ = ui.load_local(self.root)
        saved = local_team_key_receipt(connection, self.state)
        old = saved.get("value", {})
        keys = self.cloud.pages("sandbox", "ListApiKeys", "apiKeys", teamID=team["teamID"])
        operation = "key-" + team["teamID"]
        description = f"为所选{ui.SANDBOX_TEAM_LABEL}创建专用 API Key，明文仅保存至本机受限配置文件。"
        if allow_reuse and old.get("teamID") == team["teamID"] and team_key_is_active(keys, old["apiKeyID"]):
            if not self.reselect or choose_number(f"选择{ui.TEAM_API_KEY_LABEL}", [
                "复用本机已保存的 API Key · " + old["apiKeyID"], "新建专用 API Key",
            ]) == 1:
                self.record("key", old, saved.get("owned", False), reused=True)
                return
        elif (allow_reuse and not old and ui.usable(connection.get("E2B_API_KEY"))
              and self.preferred.get("team_id") == team["teamID"]
              and not team_key_issues(connection, self.cloud, team["teamID"], {})):
            if choose_number(f"选择{ui.TEAM_API_KEY_LABEL}", [
                "沿用本机已验证的 API Key", "新建专用 API Key",
            ]) == 1:
                print("  ✓ 沿用本机已验证的 API Key")
                return
        elif self.reselect:
            print("  当前 Team 下没有可直接复用的本机 API Key。云端无法重新返回已创建 API Key 的明文。")
        key_name = self.name + "-key"
        old_operation = self.state["operations"].get(operation, {})
        orphan_name = old_operation.get("resource_name", self.legacy_name + "-key") if old_operation else key_name
        orphan = next((k for k in keys if k.get("apiKeyName") == orphan_name), None)
        recorded_id = old_operation.get("result", {}).get("apiKeyID")
        if orphan and (self.reselect or (orphan["apiKeyID"] != recorded_id and old_operation.get("status") != "pending")):
            key_name += "-" + uuid.uuid4().hex[:6]
        elif orphan:
            self.mutate("orphan-key-" + orphan["apiKeyID"], "本项目专用 Key 明文丢失或失效；先禁用并删除，再重新创建。",
                        lambda _: self.delete_team_key(team["teamID"], orphan["apiKeyID"]), idempotent=True)
            self.state["operations"].pop("key-" + team["teamID"], None)
            self.save()
        elif not self.reselect:
            self.missing(operation)
        new_secret = None

        def create(_):
            nonlocal new_secret
            key = self.cloud.sandbox("CreateApiKey", body={"teamID": team["teamID"], "apiKeyName": key_name})["apiKey"]
            new_secret = key["apiKeyValue"]
            return {"apiKeyID": key["apiKeyID"], "teamID": team["teamID"], "fingerprint": hashlib.sha256(new_secret.encode()).hexdigest()}
        result = self.mutate(operation, description, create,
                             confirmed=self.confirm_replacement(operation, description), resource_name=key_name)
        self.record("key", result, True)
        if new_secret is not None:
            ui.write_env_values(self.root / "config/.env", {"E2B_API_KEY": new_secret})

    def acr_order(self):
        query = {"ProductCode": "acr", "ProductType": "acr_ee_public_cn", "SubscriptionType": "Subscription", "Period": 1, "RenewalStatus": "ManualRenewal"}
        for i, (code, value) in enumerate([("InstanceType", "Basic"), ("InstanceName", self.name), ("Region", self.cloud.region), ("DefaultOssBucket", "true")], 1):
            query[f"Parameter.{i}.Code"], query[f"Parameter.{i}.Value"] = code, value
        return query

    def acr_list_price(self, order):
        region = order["Parameter.3.Value"]
        response = self.cloud.rpc(
            "bssopenapi", "GetSubscriptionPrice",
            ProductCode=order["ProductCode"], ProductType=order["ProductType"],
            SubscriptionType=order["SubscriptionType"], OrderType="NewOrder",
            ServicePeriodQuantity=order["Period"], ServicePeriodUnit="Month", Region=region,
            **{"ModuleList.1.ModuleCode": "InstanceType",
               "ModuleList.1.Config": f"Region:{region},InstanceType:{order['Parameter.1.Value']}",
               "ModuleList.1.PriceType": "Day"},
        )
        if response.get("Code") != "Success" or not isinstance(response.get("Data"), dict):
            raise CloudError(f"无法获取 ACR 目录价（{response.get('Code') or '无报价'}）。请检查费用查询权限，或选择复用已有 ACR。")
        data = response["Data"]
        try:
            price = Decimal(str(data.get("OriginalPrice")))
        except InvalidOperation:
            price = Decimal("NaN")
        if data.get("Currency") != "CNY" or not price.is_finite() or price < 0:
            raise CloudError("ACR 询价未返回有效的人民币目录价。请检查费用查询权限，或选择复用已有 ACR。")
        return f"¥{price:,.2f}"

    def show_acr_order(self, order):
        print(f"    ACR 基础版 · {order['Parameter.3.Value']} · 1 个月 · 手动续费")
        print(f"    目录价     {self.acr_list_price(order)}，确认后自动下单并支付，实际扣款以订单为准")
        print("    按量费用   默认 OSS 的存储与公网拉取流量另计")

    def approve_dedicated(self):
        if self.cloud.region not in FC_ZONES:
            print("  当前地域缺少已核验的 FC 可用区，继续逐项选择。")
            return
        order = self.acr_order()
        print("\n  专用资源方案 · " + self.cloud.region)
        print(f"    执行环境   {ui.SANDBOX_TEAM_LABEL}（QoS Default）与{ui.TEAM_API_KEY_LABEL}")
        print("    私有网络   VPC、交换机、安全组、ACR 拉取绑定")
        print("    镜像仓库   私有命名空间和仓库、推送入口、本机出口白名单")
        print("    后续演示   两个 Sandbox 按使用量计费，Qoder 用量按账号方案计")
        try:
            self.show_acr_order(order)
        except CloudError as exc:
            print(f"  {exc}")
            print("  无法核对费用，继续逐项选择。")
            return
        print("    自动回收   收尾选「回收运行和准备资源」")
        print(f"    人工处理   预付费 ACR 实例通过「{ACR_REFUND_PATH}」退订，OSS 另行核对")
        if not confirm("按上述方案创建专用资源，并自动支付 ACR 订单。", enter_skips=True):
            return
        self.dedicated = True
        self.state.update(setup_mode="new", approved_acr_order=order)
        self.save()

    def acr(self):
        instances = self.cloud.pages("cr", "ListInstance", "Instances")
        rows = [r for r in instances if r.get("InstanceStatus") == "RUNNING" and r.get("InstanceSpecification") in {"Enterprise_Basic", "Enterprise_Standard", "Enterprise_Advanced"}]
        purchase = self.state["operations"].get("acr-purchase", {})
        purchase_status = purchase.get("status")
        order_data = purchase.get("result", {}).get("Data", purchase.get("result", {}).get("data", {}))
        purchased_id = order_data.get("InstanceId") or order_data.get("instanceId")
        if purchase_status == "done" and not purchased_id:
            raise CloudError("历史 ACR 订单没有实例 ID。请先核对订单，再继续准备。")
        recorded = self.state["resources"].get("acr", {}).get("value", {}).get("InstanceId")
        historical_ids = {item.get("value", {}).get("InstanceId") for item in self.state.get("history", [])
                          if item.get("kind") == "acr"}
        if purchased_id and (purchased_id == recorded or purchased_id not in historical_ids):
            purchased = next((row for row in instances if row["InstanceId"] == purchased_id), None)
            if (purchased is not None and purchased["InstanceStatus"] != "RUNNING") or recorded != purchased_id:
                print(f"  · 核对已支付的 ACR 订单 · {purchased_id}")
                if recorded != purchased_id:
                    self.record("acr", {"InstanceId": purchased_id, "order": order_data}, True)
                ready = self.wait("等待已购 ACR 就绪", lambda: next((
                    row for row in self.cloud.pages("cr", "ListInstance", "Instances")
                    if row["InstanceId"] == purchased_id and row.get("InstanceStatus") == "RUNNING"
                ), None))
                return self.record("acr", ready, reused=True)
        previous = recorded or purchased_id or self.preferred.get("instance_id")
        replacing = purchase_status == "done" and previous and not any(r["InstanceId"] == previous for r in instances)
        if replacing:
            try:
                self.cloud.rpc("cr", "GetInstanceEndpoint", InstanceId=previous, EndpointType="internet", ModuleName="Registry")
            except CloudError as exc:
                if str(exc.code).upper() != "INSTANCE_NOT_EXIST":
                    raise
                print("  原 ACR 已不存在。可复用其他实例，或核对旧订单后明确批准新购。")
            else:
                raise CloudError("原 ACR 未出现在实例列表中，但仍可查询。请核对原订单和实例状态后重试。")
        match = next((r for r in rows if r["InstanceId"] == previous or r.get("InstanceName") == self.name), None)
        if purchase_status == "pending":
            print("  上次 ACR 订单待确认，将沿用原订单请求。")
            match = None
        elif replacing or self.reselect or (not match and not self.dedicated):
            rows.sort(key=lambda row: row["InstanceId"] != previous)
            match = choose("选择同地域 ACR 企业版实例（经济版不适用）", rows,
                           lambda r: f"{r['InstanceName']} / {r['InstanceId']}")
        if match:
            if replacing:
                self.dedicated = False
                self.state["setup_mode"] = "discover"
                self.state.pop("approved_acr_order", None)
                self.missing("acr-purchase")
            return self.record("acr", match)
        query = self.acr_order()
        if purchase_status == "pending" or (purchase_status == "done" and not replacing and not self.reselect):
            approved = self.state.get("approved_acr_order")
            if approved:
                query = approved
            else:
                saved = self.state["operations"]["acr-purchase"]
                query["Parameter.2.Value"] = saved.get("resource_name", self.legacy_name)
        elif replacing or self.reselect or (purchase_status != "done" and self.state.get("approved_acr_order") != query):
            if any(row.get("InstanceName") == self.name for row in instances):
                query["Parameter.2.Value"] = RESOURCE_PREFIX + uuid.uuid4().hex[:10]
            print("\n  新购 ACR")
            self.show_acr_order(query)
            print(f"    回收       预付费 ACR 实例通过「{ACR_REFUND_PATH}」退订，OSS 另行核对")
            confirm("自动下单并支付上述 ACR 订单。")
            self.state["approved_acr_order"] = query
            if purchase_status == "done":
                self.archive_operation("acr-purchase")
            self.save()
        result = self.mutate("acr-purchase", "自动下单并支付上述 ACR 实例；OSS及后续使用可能另有费用。",
                             lambda token: self.cloud.rpc("bssopenapi", "CreateInstance", ClientToken=token, **query), True, confirmed=True,
                             resource_name=query["Parameter.2.Value"])
        data = result.get("Data", result.get("data", {}))
        instance_id = data.get("InstanceId", data.get("instanceId"))
        if not instance_id:
            raise CloudError("订单已记录但未返回实例 ID；请核对订单，禁止重复购买。")
        self.record("acr", {"InstanceId": instance_id, "InstanceName": query["Parameter.2.Value"], "order": data}, True)
        return self.wait("等待 ACR 实例就绪", lambda: next((r for r in self.cloud.pages("cr", "ListInstance", "Instances") if r["InstanceId"] == instance_id and r["InstanceStatus"] == "RUNNING"), None))

    def network(self, instance):
        region = self.cloud.region
        suffixes = FC_ZONES.get(region)
        if not suffixes:
            raise CloudError("尚未核验该地域 FC 可用区。请更新 scripts/bootstrap.py 的官方可用区清单后重跑。")
        zones = {region + ("-" if region.startswith("cn-") else "") + z for z in suffixes.split()}
        switches = self.cloud.pages("vpc", "DescribeVSwitches", "VSwitches.VSwitch", RegionId=region)
        private_ranges = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
        vpcs_in_region = self.cloud.pages("vpc", "DescribeVpcs", "Vpcs.Vpc", RegionId=region)
        vpcs_in_region = [v for v in vpcs_in_region if any(ipaddress.ip_network(v["CidrBlock"]).subnet_of(n) for n in private_ranges)]
        valid_vpcs = {v["VpcId"] for v in vpcs_in_region}
        switches = [s for s in switches if s["VpcId"] in valid_vpcs and s["ZoneId"] in zones and int(s.get("AvailableIpAddressCount", 0)) >= 8 and s.get("Status") == "Available"]
        endpoint = self.cloud.rpc("cr", "GetInstanceVpcEndpoint", InstanceId=instance, ModuleName="Registry")
        linked = [v for v in endpoint.get("LinkedVpcs", []) if v.get("Status") == "RUNNING"]
        # Recommend networks already attached to the selected registry first.
        switches.sort(key=lambda s: s["VpcId"] not in {v["VpcId"] for v in linked})
        prev = self.preferred_id("switch", "VSwitchId", "vswitch_id")
        own_vpc = self.state["operations"].get("vpc", {}).get("result", {}).get("VpcId")
        own_switches = {op.get("result", {}).get("VSwitchId") for op in self.state["operations"].values()
                        if op.get("name") == "switch" and op.get("scope", {}).get("VpcId") == own_vpc
                        and op["status"] == "done"}
        switch = next((s for s in switches if s["VSwitchId"] == prev or
                       (self.dedicated and s["VpcId"] == own_vpc and s["VSwitchId"] in own_switches)), None)
        vpcs = [v for v in vpcs_in_region if ipaddress.ip_network(v["CidrBlock"]).prefixlen <= 24]
        vpc = None
        if self.reselect:
            old_vpc = self.preferred_id("vpc", "VpcId", "vpc_id")
            vpcs.sort(key=lambda row: row["VpcId"] != old_vpc)
            vpc = choose("选择 VPC", vpcs, lambda v: f"{v.get('VpcName')} / {v['VpcId']} / {v['CidrBlock']}")
            switch = None
            if vpc:
                candidates = [s for s in switches if s["VpcId"] == vpc["VpcId"]]
                candidates.sort(key=lambda row: row["VSwitchId"] != prev)
                switch = choose("选择该 VPC 的交换机", candidates,
                                lambda s: f"{s.get('VSwitchName')} / {s['VSwitchId']} / {s['ZoneId']}")
        elif not switch and not self.dedicated:
            switch = choose("选择镜像拉取网络；优先显示已绑定 ACR 的网络", switches,
                            lambda s: f"{s.get('VSwitchName')} / {s['VpcId']} / {s['VSwitchId']} / {s['ZoneId']}")
        if switch:
            self.record("switch", switch)
            self.record("vpc", {"VpcId": switch["VpcId"]})
        else:
            if not self.dedicated and not self.reselect:
                vpc = choose("在已有 VPC 新建交换机，或新建独立 VPC", vpcs, lambda v: f"{v.get('VpcName')} / {v['VpcId']} / {v['CidrBlock']}")
            if not vpc:
                old_vpc = self.state["operations"].get("vpc", {}).get("result", {}).get("VpcId")
                if old_vpc and not any(v["VpcId"] == old_vpc for v in vpcs_in_region):
                    self.missing("vpc")
                description = "新建独立 VPC，网段 192.168.0.0/16，不配置互联。"
                vpc_name = self.network_resource_name("vpc")
                vpc = self.mutate("vpc", description,
                                  lambda t: self.cloud.rpc("vpc", "CreateVpc", RegionId=region, VpcName=vpc_name, CidrBlock="192.168.0.0/16", ClientToken=t), True,
                                  confirmed=self.confirm_replacement("vpc", description))
                self.record("vpc", {"VpcId": vpc["VpcId"]}, True)
                vpc = {**vpc, "CidrBlock": "192.168.0.0/16"}
            else:
                self.record("vpc", {"VpcId": vpc["VpcId"]})
            existing = self.cloud.pages("vpc", "DescribeVSwitches", "VSwitches.VSwitch", RegionId=region, VpcId=vpc["VpcId"])
            used = [ipaddress.ip_network(s["CidrBlock"]) for s in existing]
            cidr = next((str(n) for n in ipaddress.ip_network(vpc["CidrBlock"]).subnets(new_prefix=24) if not any(n.overlaps(u) for u in used)), None)
            if not cidr:
                raise CloudError("该 VPC 无可用 /24 网段；请重跑选择其他 VPC。")
            zone_rows = [{"id": z["ZoneId"]} for z in self.cloud.rpc("ecs", "DescribeZones", RegionId=region)["Zones"]["Zone"] if z["ZoneId"] in zones]
            if not zone_rows:
                raise CloudError("当前地域没有已核验的可用区交集。")
            zone = zone_rows[0]["id"]
            self.wait("等待 VPC 就绪", lambda: self.cloud.rpc("vpc", "DescribeVpcAttribute", RegionId=region, VpcId=vpc["VpcId"]).get("Status") == "Available")
            scope = {"VpcId": vpc["VpcId"], "CidrBlock": cidr, "ZoneId": zone}
            switch_name = self.network_resource_name("switch", scope)
            created = self.mutate("switch", f"在 VPC {vpc['VpcId']} 新建交换机 {cidr} / {zone}。",
                                  lambda t: self.cloud.rpc("vpc", "CreateVSwitch", RegionId=region, VpcId=vpc["VpcId"], CidrBlock=cidr, ZoneId=zone, VSwitchName=switch_name, ClientToken=t), True,
                                  scope=scope)
            switch = {"VSwitchId": created["VSwitchId"], "VpcId": vpc["VpcId"]}
            self.record("switch", switch, True)
        self.wait("等待交换机就绪", lambda: self.cloud.rpc("vpc", "DescribeVSwitchAttributes", RegionId=region, VSwitchId=switch["VSwitchId"]).get("Status") == "Available")
        groups = self.cloud.pages("ecs", "DescribeSecurityGroups", "SecurityGroups.SecurityGroup", RegionId=region, VpcId=switch["VpcId"])
        compatible = []
        for group in groups:
            if group.get("ServiceManaged", False) or group.get("SecurityGroupType", "normal") != "normal":
                continue
            rules = self.cloud.rpc("ecs", "DescribeSecurityGroupAttribute", RegionId=region, SecurityGroupId=group["SecurityGroupId"], Direction="egress").get("Permissions", {}).get("Permission", [])
            if egress_allows_https(rules):
                compatible.append(group)
        groups = compatible
        old = self.preferred_id("sg", "SecurityGroupId", "security_group_id")
        created_sg = self.state["operations"].get(self.operation_key("sg", {"VpcId": switch["VpcId"]}), {}).get("result", {}).get("SecurityGroupId")
        sg = next((g for g in groups if g["SecurityGroupId"] == old or
                   (self.dedicated and g["SecurityGroupId"] == created_sg)), None)
        if self.reselect or (not sg and not self.dedicated):
            groups.sort(key=lambda row: row["SecurityGroupId"] != old)
            sg = choose("选择安全组；已有组若规则不兼容会停止，不自动放宽规则", groups, lambda g: f"{g.get('SecurityGroupName')} / {g['SecurityGroupId']}")
        if not sg:
            scope = {"VpcId": switch["VpcId"]}
            description = "新建普通安全组（默认出方向允许，未新增入方向放行）。"
            confirmed = self.confirm_replacement("sg", description, scope)
            if not self.reselect:
                self.missing("sg", scope)
            sg_name = self.network_resource_name("sg", scope)
            sg = self.mutate("sg", description,
                             lambda t: self.cloud.rpc("ecs", "CreateSecurityGroup", RegionId=region, VpcId=switch["VpcId"], SecurityGroupName=sg_name, SecurityGroupType="normal", ClientToken=t), True,
                             scope=scope, confirmed=confirmed)
            self.record("sg", sg, True)
        else:
            self.record("sg", sg)
        # Fail closed on restricted existing groups: no modification of production rules.
        rules = self.cloud.rpc("ecs", "DescribeSecurityGroupAttribute", RegionId=region, SecurityGroupId=sg["SecurityGroupId"], Direction="egress").get("Permissions", {}).get("Permission", [])
        if not egress_allows_https(rules):
            raise CloudError("所选安全组有自定义出方向规则，无法静态证明拉取可达。请重跑选择新建专用安全组。")
        binding_name = "binding-" + instance + "-" + switch["VpcId"]
        if not any(v["VpcId"] == switch["VpcId"] for v in endpoint.get("LinkedVpcs", [])):
            self.missing(binding_name)
            self.mutate(binding_name, f"为 ACR {instance} 添加 VPC {switch['VpcId']} 绑定，允许创建 PrivateZone 服务关联角色和 DNS 记录（需对应权限/配额）。",
                        lambda _: self.cloud.rpc("cr", "CreateInstanceVpcEndpointLinkedVpc", InstanceId=instance, VpcId=switch["VpcId"], VswitchId=switch["VSwitchId"], ModuleName="Registry", EnableCreateDNSRecordInPvzt=True))
            self.record("binding", {"InstanceId": instance, "VpcId": switch["VpcId"]}, True)
        else:
            operation = self.state["operations"].get(binding_name, {})
            self.record("binding", {"InstanceId": instance, "VpcId": switch["VpcId"]},
                        operation.get("status") in {"done", "pending"}, reused=True)
        def vpc_ready():
            current = self.cloud.rpc("cr", "GetInstanceVpcEndpoint", InstanceId=instance, ModuleName="Registry")
            return current.get("Enable") and any(
                v["VpcId"] == switch["VpcId"] and v.get("Status") == "RUNNING" for v in current.get("LinkedVpcs", [])
            )

        self.wait("等待 ACR VPC 绑定生效", vpc_ready)
        print(f"  ✓ ACR 内网访问已核验 · VPC {switch['VpcId']} · RUNNING")
        return dict(vpc_id=switch["VpcId"], vswitch_id=switch["VSwitchId"], security_group_id=sg["SecurityGroupId"])

    def repository(self, instance, host=None):
        # Registry returns real domains; never invent an account-specific hostname.
        if host is None:
            host = registry_public_host(registry_public_endpoint(self.cloud, instance))
        namespace = self.name
        repo_name = "qoder-byoc-sandbox-example"
        repos = self.cloud.pages("cr", "ListRepository", "Repositories", InstanceId=instance)
        candidates = [r for r in repos if r.get("RepoType") == "PRIVATE"]
        old = self.state["resources"].get("repository", {}).get("value", {})
        if not self.reselect:
            for operation in self.state["operations"].values():
                scope = operation.get("scope", {})
                if operation.get("name") in {"namespace", "repository"} and operation.get("status") == "pending" and scope.get("InstanceId") == instance:
                    namespace = scope.get("NamespaceName") or namespace
                    repo_name = scope.get("RepoName") or repo_name
                    break
        repo = next((r for r in candidates if r.get("RepoId") == old.get("RepoId") or (r.get("RepoNamespaceName") == namespace and r.get("RepoName") == repo_name)), None)
        if self.reselect or (not repo and not self.dedicated):
            configured_path = self.preferred.get("repository", "").split("/", 1)[-1]
            candidates.sort(key=lambda row: (
                not old.get("RepoId") or row.get("RepoId") != old["RepoId"],
                configured_path != f"{row['RepoNamespaceName']}/{row['RepoName']}",
            ))
            repo = choose("选择私有仓库（构建将推送独立源码版本 tag）", candidates, lambda r: f"{r['RepoNamespaceName']}/{r['RepoName']}")
        if not repo:
            namespaces = self.cloud.pages("cr", "ListNamespace", "Namespaces", InstanceId=instance)
            existing_names = {n["NamespaceName"] for n in namespaces}
            if self.reselect:
                picked = choose("为新仓库选择命名空间", [n for n in namespaces if n.get("NamespaceStatus", "NORMAL") == "NORMAL"],
                                lambda n: n["NamespaceName"])
                if picked:
                    namespace = picked["NamespaceName"]
                elif namespace in existing_names:
                    namespace = RESOURCE_PREFIX + uuid.uuid4().hex[:10]
                if any(row.get("RepoNamespaceName") == namespace and row.get("RepoName") == repo_name for row in repos):
                    repo_name += "-" + uuid.uuid4().hex[:6]
            namespace_scope = {"InstanceId": instance, "NamespaceName": namespace}
            if namespace not in existing_names:
                self.missing("namespace", namespace_scope)
                self.mutate("namespace", f"在 ACR {instance} 新建专用命名空间 {namespace}（默认私有）。",
                            lambda _: self.cloud.rpc("cr", "CreateNamespace", InstanceId=instance, NamespaceName=namespace, AutoCreateRepo=False, DefaultRepoType="PRIVATE"), scope=namespace_scope)
                self.record("namespace", {"InstanceId": instance, "NamespaceName": namespace}, True)
            else:
                operation = self.state["operations"].get(self.operation_key("namespace", namespace_scope), {})
                self.record("namespace", namespace_scope, operation.get("status") in {"done", "pending"}, reused=True)
            repo_scope = {"InstanceId": instance, "NamespaceName": namespace, "RepoName": repo_name}
            self.missing("repository", repo_scope)
            result = self.mutate("repository", f"新建私有仓库 {namespace}/{repo_name}。", lambda _: self.cloud.rpc("cr", "CreateRepository", InstanceId=instance, RepoNamespaceName=namespace, RepoName=repo_name, RepoType="PRIVATE", Summary="Qoder BYOC sandbox example"), scope=repo_scope)
            repo = {"RepoId": result["RepoId"], "RepoNamespaceName": namespace, "RepoName": repo_name, "InstanceId": instance}
            self.record("repository", repo, True)
        else:
            self.record("namespace", {"InstanceId": instance, "NamespaceName": repo["RepoNamespaceName"]})
            self.record("repository", {**repo, "InstanceId": instance})
        return host + "/" + repo["RepoNamespaceName"] + "/" + repo["RepoName"]

    def delete_team_key(self, team_id, key_id):
        if not team_id:
            raise CloudError(f"API Key {key_id} 缺少所属 Team 记录，已保留。")
        try:
            key = self.cloud.sandbox("DescribeApiKey", path_id=key_id).get("apiKey") or {}
            if key.get("apiKeyID") != key_id or key.get("teamID") != team_id:
                raise CloudError(f"API Key {key_id} 的云端标识或所属 Team 与账本不符，已保留。")
            status = key.get("status")
            if status == "active":
                # The generated SDK serializes omitted IP lists as empty lists.
                self.cloud.sandbox("UpdateApiKey", path_id=key_id, body={
                    "status": "inactive",
                    "ipWhitelist": key.get("ipWhitelist") or [],
                    "ipBlacklist": key.get("ipBlacklist") or [],
                })
            elif status != "inactive":
                raise CloudError(f"API Key {key_id} 状态未知，无法安全禁用或删除。")
            self.cloud.sandbox("DeleteApiKey", path_id=key_id)
        except CloudError as exc:
            if not self.cleanup_absent(exc, "key"):
                raise
        return {}

    @staticmethod
    def cleanup_absent(exc, kind):
        code = str(exc.code or "").lower()
        if kind == "key" and code == "apikeynotfound":
            return True
        if kind == "repository" and code == "repo_not_exist":
            return True
        if kind in {"public-endpoint", "repository", "namespace", "acl", "binding"} and code == "instance_not_exist":
            return True
        return code in {"notfound", "notexist", "resourcenotfound", "resourcenotexist"} or code.endswith((".notfound", ".notexist"))

    def cleanup_identity(self, record):
        kind = record["kind"]
        identity = {field: record["value"].get(field) for field in self.IDENTITY_FIELDS[kind]}
        return kind + ":" + json.dumps(identity, sort_keys=True)

    def uninstall(self):
        if ui.local_run_state(self.root) in {"recorded", "partial"}:
            raise CloudError("先运行 qoder-byoc-sandbox-example cleanup 回收 Qoder 和 Sandbox，再卸载准备资源。")
        pending = [op.get("name", key) for key, op in self.state["operations"].items() if op["status"] == "pending"]
        if pending:
            raise CloudError("以下写入结果待核对，请先恢复准备流程再回收：" + "、".join(pending))
        current = [{"kind": kind, **record} for kind, record in self.state["resources"].items()]
        resources = current + self.state.get("history", [])
        order = ["key", "repository", "namespace", "public-endpoint", "acl", "binding", "sg", "switch", "vpc", "team"]
        labels = self.RESOURCE_LABELS
        templates = ui.read_json(self.root / "var/state/templates.json", {})
        team = self.state["resources"].get("team", {}).get("value", {})
        history = templates.get("created_templates", [])
        # Only explicit create receipts carry ownership. A bare configured ID
        # must never become owned merely because it appears in templates.json.
        known_teams = {team.get("teamID")} | {
            record.get("value", {}).get("teamID")
            for record in self.state.get("history", []) if record.get("kind") == "team"
        }
        deletable_templates = []
        unknown = set()
        for record in history:
            if record.get("deleted"):
                continue
            tid = record.get("template_id")
            if (not record.get("created_by_demo") or not tid
                    or record.get("region") != self.cloud.region
                    or not record.get("team_id") or record["team_id"] not in known_teams):
                unknown.add(str(tid or "未记录 ID"))
                continue
            deletable_templates.append(record)
        owned_template_ids = {record["template_id"] for record in deletable_templates}
        unknown.update(tid for kind in ("analysis", "browser")
                       if (tid := templates.get(kind + "_template")) and tid not in owned_template_ids)

        deleted = set(self.state.get("deleted", []))
        unique = {}
        for record in resources:
            identity = self.cleanup_identity(record)
            legacy = record["kind"] + ":" + json.dumps(record["value"], sort_keys=True)
            if record.get("deleted") or legacy in deleted:
                deleted.add(identity)
            if identity not in unique or (record.get("owned") and not unique[identity].get("owned")):
                unique[identity] = record
        remaining = [(record, identity) for identity, record in unique.items() if identity not in deleted]
        current_ids = {self.cleanup_identity(record) for record in current}
        current_templates = {templates.get(kind + "_template") for kind in ("analysis", "browser")}
        groups = {"当前配置": [], "历史配置": []}
        for tid in sorted(owned_template_ids):
            groups["当前配置" if tid in current_templates else "历史配置"].append(("删除", "Sandbox 模板", tid))
        for record, signature in remaining:
            kind, value = record["kind"], record["value"]
            fields = self.IDENTITY_FIELDS[kind]
            if record.get("owned") and any(not value.get(field) for field in fields):
                raise CloudError(f"{labels[kind]} 缺少资源 ID，无法核对回收范围。")
            identity = " / ".join(str(value[key]) for key in fields if value.get(key))
            action = "保留"
            if record.get("owned"):
                action = {"key": "禁用后删除", "acr": "人工退订", "public-endpoint": "关闭", "binding": "解绑"}.get(kind, "删除")
            groups["当前配置" if signature in current_ids else "历史配置"].append((action, labels[kind], identity))
        print("\n  回收清单 · 本项目准备账本")
        for group, entries in groups.items():
            if entries:
                print(f"  {group} · {len(entries)} 项")
                for action, label, identity in entries:
                    print(f"    [{action}] {label} · {identity}")
        purchase = self.state["operations"].get("acr-purchase", {})
        has_paid_acr = purchase.get("status") == "done" or any(r["kind"] == "acr" and r.get("owned") for r, _ in remaining)
        if has_paid_acr:
            print(f"  预付费 ACR 实例需在「{ACR_REFUND_PATH}」核对订单并手动退订。")
            print("  费用与成本 https://usercenter2.aliyun.com/home")
            print("  确认退订后实例立即删除并停止计费。")
            print("  OSS 存储会保留，请在 OSS 控制台另行核对数据和用量。https://oss.console.aliyun.com/")
            print(f"  官方退订说明 {ACR_REFUND_DOC}")
        print("  共享服务角色保留。")
        if unknown:
            raise CloudError("模板归属记录不完整，已保留：" + ", ".join(sorted(unknown))
                             + "；请人工核对后重试，尚未删除模板、仓库、网络或 Team。")
        confirm("回收清单中的本项目资源（含历史）。单项失败会提示手动处理，并继续回收其他准备资源。")
        from alibabacloud_sandbox20260820 import models
        from alibabacloud_sandbox20260820.client import Client
        for record in deletable_templates:
            tid = record["template_id"]
            client = Client(self.cloud.config(f"sandbox.{self.cloud.region}.aliyuncs.com"))
            try:
                client.delete_template(tid, models.DeleteTemplateRequest(team_id=record["team_id"]))
            except Exception as exc:
                if not ui.not_found(exc):
                    raise self.cloud.error("DeleteTemplate", exc) from None
            record["deleted"] = True
            for kind in ("analysis", "browser"):
                if templates.get(kind + "_template") == tid:
                    templates.pop(kind + "_template")
            atomic_json(self.root / "var/state/templates.json", templates)
        needs_manual = 0
        open_endpoints = set()

        def report_manual(label, identity, reason):
            nonlocal needs_manual
            needs_manual += 1
            heading = "⚠ 需手动处理"
            if sys.stdout.isatty() and "NO_COLOR" not in os.environ:
                heading = "\033[1;33m" + heading + "\033[0m"
            print(f"  {heading} · {label} · {identity}\n    {reason}", flush=True)

        for kind in order:
            for record, signature in remaining:
                if record["kind"] != kind or not record.get("owned"):
                    continue
                v = record["value"]
                if signature in self.state.get("deleted", []):
                    continue
                identity = " / ".join(str(v[field]) for field in self.IDENTITY_FIELDS[kind])
                if kind == "acl" and v["InstanceId"] in open_endpoints:
                    report_manual(labels[kind], identity, "公网入口未确认关闭，暂保留白名单。")
                    continue
                params = {
                    "public-endpoint": ("cr", "UpdateInstanceEndpointStatus", {"InstanceId": v.get("InstanceId"), "EndpointType": "internet", "ModuleName": "Registry", "Enable": False}),
                    "repository": ("cr", "DeleteRepository", {"InstanceId": v.get("InstanceId"), "RepoId": v.get("RepoId")}),
                    "namespace": ("cr", "DeleteNamespace", {"InstanceId": v.get("InstanceId"), "NamespaceName": v.get("NamespaceName")}),
                    "acl": ("cr", "DeleteInstanceEndpointAclPolicy", {"InstanceId": v.get("InstanceId"), "EndpointType": "internet", "ModuleName": "Registry", "Entries": json.dumps([{"Entry": v.get("Entry")}])}),
                    "binding": ("cr", "DeleteInstanceVpcEndpointLinkedVpc", {"InstanceId": v.get("InstanceId"), "VpcId": v.get("VpcId"), "ModuleName": "Registry"}),
                    "sg": ("ecs", "DeleteSecurityGroup", {"RegionId": self.cloud.region, "SecurityGroupId": v.get("SecurityGroupId")}),
                    "switch": ("vpc", "DeleteVSwitch", {"RegionId": self.cloud.region, "VSwitchId": v.get("VSwitchId")}),
                    "vpc": ("vpc", "DeleteVpc", {"RegionId": self.cloud.region, "VpcId": v.get("VpcId")}),
                }
                outcome = "已回收本项目资源"
                try:
                    if kind == "key":
                        self.delete_team_key(v.get("teamID"), v["apiKeyID"])
                    elif kind == "team":
                        self.cloud.sandbox("DeleteTeam", path_id=v["teamID"])
                    else:
                        service, action, query = params[kind]
                        self.cloud.rpc(service, action, **query)
                except CloudError as exc:
                    if not self.cleanup_absent(exc, kind):
                        report_manual(labels[kind], identity, exc)
                        if kind == "public-endpoint":
                            open_endpoints.add(v["InstanceId"])
                        continue
                    outcome = "资源已不存在，跳过"
                self.state.setdefault("deleted", []).append(signature)
                self.save()
                print(f"✓ {outcome} · {labels[kind]} · {identity}")
        if needs_manual:
            print(f"  其余回收步骤已执行，{needs_manual} 项待手动处理。处理后可重跑 --uninstall 核对。")
        else:
            self.state["uninstalled"] = True
            self.save()
        if has_paid_acr:
            print(f"  请按清单到「{ACR_REFUND_PATH}」处理预付费 ACR 实例。")

    def public_acl(self, instance, endpoint, address, *, confirmed=False):
        entries = endpoint.get("AclEntries", [])
        if any(address in ipaddress.ip_network(e["Entry"]) for e in entries):
            return
        # Adding a first ACL to a currently unrestricted shared endpoint could
        # interrupt existing clients. Never do that implicitly.
        if not endpoint.get("AclEnable"):
            return
        cidr = str(address) + "/32"
        scope = {"InstanceId": instance, "Entry": cidr}
        self.missing("acl", scope)
        self.mutate("acl", f"为 ACR 新增已观测公网出口 {cidr} 白名单，保留原有条目。",
                    lambda _: {**self.cloud.rpc("cr", "CreateInstanceEndpointAclPolicy", InstanceId=instance,
                                                EndpointType="internet", ModuleName="Registry",
                                                Entries=json.dumps([{"Entry": cidr, "Comment": self.name}])),
                               "entries_api": True, "endpoint_ready": registry_endpoint_ready(endpoint)}, scope=scope, confirmed=confirmed)
        self.record("acl", {"InstanceId": instance, "Entry": cidr}, True, announce=False)
        return cidr

    def prepare_registry_login(self, instance, repository=None, *, prefer_existing=False):
        scope = {"InstanceId": instance}
        endpoint = registry_public_endpoint(self.cloud, instance)
        if not endpoint.get("Enable"):
            self.missing("public-endpoint", scope)
            self.mutate("public-endpoint", "开启所选 ACR 的公网 Registry 入口，保留现有访问策略。",
                        lambda _: self.cloud.rpc("cr", "UpdateInstanceEndpointStatus", InstanceId=instance, EndpointType="internet", ModuleName="Registry", Enable=True), scope=scope)
            self.record("public-endpoint", scope, True)
        else:
            self.record("public-endpoint", scope)
        endpoint = self.wait("等待 ACR 公网入口生效", lambda: (
            current if registry_endpoint_ready(current := registry_public_endpoint(self.cloud, instance)) else None
        ))
        print("  ✓ ACR 公网入口已生效。")
        acr = self.state["resources"].get("acr", {})
        prepare_access = None
        if acr.get("owned") and acr["value"].get("InstanceId") == instance:
            prepare_access = lambda current: self.prepare_public_acl(instance, current)
        return ensure_registry_login(self.cloud, instance, repository, interactive=True,
                                     prepare_access=prepare_access, prefer_existing=prefer_existing)

    def prepare_public_acl(self, instance, endpoint):
        addresses = public_egress_ipv4s()
        if not endpoint.get("AclEnable"):
            return
        if not addresses:
            raise CloudError("未获取到公网出口 IP，无法核验白名单。请确认网络后重试。", "AcrEndpoint")
        entries = [ipaddress.ip_network(item["Entry"]) for item in endpoint.get("AclEntries", [])]
        missing = []
        for address in addresses:
            if any(address in entry for entry in entries):
                continue
            scope = {"InstanceId": instance, "Entry": f"{address}/32"}
            operation = self.state["operations"].get(self.operation_key("acl", scope), {})
            awaiting_readback = operation.get("result", {}).get("endpoint_ready") and not operation.get("verified")
            if operation.get("status") != "pending" and not awaiting_readback:
                missing.append(address)
        if all(any(address in entry for entry in entries) for address in addresses):
            print("  ✓ ACR 公网白名单已核验。")
            return
        if missing:
            print("  · 公网出口 IPv4 · " + "、".join(map(str, addresses)))
            confirm("将 " + "、".join(f"{address}/32" for address in missing) + " 加入所选 ACR 的公网白名单，保留原有条目。")
            for address in missing:
                self.public_acl(instance, endpoint, address, confirmed=True)

        def acl_ready():
            current = registry_public_endpoint(self.cloud, instance)
            networks = [ipaddress.ip_network(item["Entry"]) for item in current.get("AclEntries", [])]
            return current if registry_endpoint_ready(current) and (
                not current.get("AclEnable") or all(any(address in entry for entry in networks) for address in addresses)
            ) else None

        endpoint.update(self.wait("等待 ACR 公网白名单生效", acl_ready))
        for address in addresses:
            scope = {"InstanceId": instance, "Entry": f"{address}/32"}
            operation = self.state["operations"].get(self.operation_key("acl", scope))
            if operation and operation.get("status") == "done":
                operation["verified"] = True
        self.save()
        print("  ✓ ACR 公网白名单已核验。")


def registry_public_endpoint(cloud, instance):
    return cloud.rpc("cr", "GetInstanceEndpoint", InstanceId=instance, EndpointType="internet", ModuleName="Registry")


def registry_endpoint_ready(endpoint):
    return endpoint.get("Enable") is True and endpoint.get("Status") == "RUNNING" and bool(endpoint.get("Domains"))


def registry_public_host(endpoint, repository=None):
    domains = [item if isinstance(item, str) else item.get("Domain") for item in endpoint.get("Domains", [])]
    if repository:
        host = registry_host(repository)
        if host not in domains:
            raise CloudError("配置中的 ACR 公网域名不属于所选实例。请重新选择 ACR。")
        return host
    host = next((domain for domain in domains if domain), None)
    if not host:
        raise CloudError("ACR 未返回公网域名，请核对实例公网访问配置。")
    return host


def docker_login_is_valid(host):
    try:
        return subprocess.run(["docker", "login", host], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def docker_engine_is_ready():
    try:
        return subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                              capture_output=True, timeout=8).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def registry_login_guidance(cloud, instance, endpoint, error):
    message = str(error)
    # Docker failures prefix the summary with the redacted daemon response.
    summary = message.rsplit("\n  ", 1)[-1] if message.startswith("Docker 原始报错（已脱敏）") else message
    lines = [summary]
    if error.code == "DockerNetwork":
        lines.append("使用 VPN 或阿里郎时，出口 IP 可能变化；请核对实际出口并更新白名单。")
    elif error.code == "DockerDNS":
        lines.append("请核对本机及 Docker 代理的 DNS 设置。")
    if error.code in {"DockerNetwork", "DockerAuth"} and endpoint.get("AclEnable"):
        entries = [ipaddress.ip_network(item["Entry"]) for item in endpoint.get("AclEntries", [])]
        addresses = public_egress_ipv4s()
        if addresses:
            statuses = (f"{ip} · {'已在白名单内' if any(ip in entry for entry in entries) else '未在白名单内'}"
                        for ip in addresses)
            lines.append("IP.SB 查询出口 " + "；".join(statuses))
    if error.code in {"DockerNetwork", "DockerAuth", "AcrEndpoint"}:
        acl_url = f"https://cr.console.aliyun.com/{quote(cloud.region, safe='')}/instance/{quote(instance, safe='')}/acl"
        lines.append("ACR 公网访问控制 " + acl_url)
    return lines


def ensure_registry_login(cloud, instance, repository=None, *, interactive=False, prepare_access=None, prefer_existing=False):
    check_existing = prefer_existing or prepare_access is None
    while True:
        endpoint = registry_public_endpoint(cloud, instance)
        enabled = endpoint.get("Enable")
        host = registry_host(repository) if repository else ""
        try:
            if not registry_endpoint_ready(endpoint):
                raise CloudError("ACR 公网入口尚未生效，请等待控制台显示运行中后重试。", "AcrEndpoint")
            host = registry_public_host(endpoint, repository)
            if not docker_engine_is_ready():
                raise CloudError("当前 Docker 引擎不可用。请启动 Docker Desktop 或检查 Docker context，等 docker version 能显示 Server 后重试。", "DockerEngine")
            if prepare_access:
                prepare_access(endpoint)
            if check_existing and docker_login_is_valid(host):
                print(f"  ✓ 复用已验证的 Docker 登录 · {host}")
            else:
                registry_login(cloud, instance, host)
            return host
        except CloudError as exc:
            if not interactive or not sys.stdin.isatty():
                raise
            from terminal_cards import Cards

            cards = Cards()
            cards.line()
            important = 2 if exc.code == "DockerNetwork" else 1
            for index, line in enumerate(registry_login_guidance(cloud, instance, endpoint, exc)):
                cards.line(line, "red" if index < important else "")
            error = exc
            check_existing = True
        while True:
            choice = input("  回车重试 · detail 查看详情 · shell 查看登录命令 · Ctrl+C 退出：").strip()
            if choice == "detail":
                print("  " + str(error), flush=True)
                if host and error.code in {"DockerDNS", "DockerNetwork", "DockerAuth", "DockerLogin"}:
                    print("  " + registry_probe(host), flush=True)
            elif choice == "shell":
                if host:
                    print(f"  在另一个终端执行：docker login {host} --username '<TempUsername>'")
                    print("  密码输入 AuthorizationToken。获取方式 https://www.alibabacloud.com/help/zh/acr/developer-reference/api-cr-2018-12-01-getauthorizationtoken")
                print("  登录后回车重试；如需回收或重跑，先按 Ctrl+C 结束当前向导。")
            elif not choice:
                break


def registry_probe(host):
    # An anonymous request checks the route without sending login credentials.
    try:
        with urllib.request.urlopen(f"https://{host}/v2/", timeout=5) as response:
            return f"终端探测返回 HTTP {response.status}。"
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and exc.headers.get("WWW-Authenticate"):
            return "终端可达 Registry，已收到正常的认证挑战。"
        return f"终端探测返回 HTTP {exc.code}。"
    except OSError:
        return "终端也未收到 Registry 的 HTTP 响应。"


def public_egress_ipv4s():
    try:
        result = subprocess.run(["curl", "-4", "-fsS", "--max-time", "8", "ip.sb"],
                                capture_output=True, text=True, timeout=10, check=True)
        address = ipaddress.IPv4Address(result.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return [address] if address.is_global else []


def local_private_network():
    pod_ip = os.environ.get("POD_IP", "")
    in_pod = bool(pod_ip or os.environ.get("KUBERNETES_SERVICE_HOST"))
    if in_pod and pod_ip:
        try:
            address = ipaddress.IPv4Address(pod_ip.strip())
        except ValueError:
            pass
        else:
            return f"Pod IP · {address}。内网访问按所属 VPC 绑定，请在 2.5 核对。"
    if sys.platform.startswith("linux") and not in_pod:
        # IMDSv2 uses the ECS link-local endpoint and must bypass proxies.
        base = "http://100.100.100.200/latest"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        request = urllib.request.Request(f"{base}/api/token", method="PUT",
                                         headers={"X-aliyun-ecs-metadata-token-ttl-seconds": "60"})
        try:
            with opener.open(request, timeout=1) as response:
                token = response.read(1024).decode().strip()
            values = []
            for field in ("private-ipv4", "vpc-id", "vswitch-id"):
                request = urllib.request.Request(f"{base}/meta-data/{field}",
                                                 headers={"X-aliyun-ecs-metadata-token": token})
                with opener.open(request, timeout=1) as response:
                    values.append(response.read(128).decode().strip())
            address = ipaddress.IPv4Address(values[0])
            if re.fullmatch(r"vpc-[a-z0-9-]+", values[1]) and re.fullmatch(r"vsw-[a-z0-9-]+", values[2]):
                return f"ECS 内网 IPv4 · {address} · VPC {values[1]} · 交换机 {values[2]}。内网访问在 2.5 核对绑定。"
        except (OSError, ValueError):
            pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("192.0.2.1", 9))
            address = ipaddress.IPv4Address(route.getsockname()[0])
    except OSError:
        return "未检测到内网 IPv4。ACR 内网访问在 2.5 按 VPC 配置。"
    return f"本机路由源 IPv4 · {address}。ACR 内网访问在 2.5 按 VPC 配置。"


def registry_host(repository):
    return repository.split("/")[0].replace("-registry-vpc.", "-registry.")


def docker_login_failure(message, code, stdout, stderr, username, password):
    streams = [value.decode(errors="replace") if isinstance(value, bytes) else value
               for value in (stderr, stdout) if value]
    output = "\n".join(streams).strip()
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    for secret in sorted({password, quote(password, safe=""), quote_plus(password), encoded}, key=len, reverse=True):
        output = output.replace(secret, "[REDACTED]")
    output = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", output)
    output = re.sub(r"""(?i)(https?://[^\s<>"'?]+)\?[^\s<>"']+""", r"\1?[REDACTED]", output)
    output = re.sub(r"(?i)\b((?:authorization|proxy-authorization)\s*:\s*)[^\n]+", r"\1[REDACTED]", output)
    output = "".join(char for char in output if char in "\n\t" or char.isprintable())
    if output:
        output = "\n    ".join(output[:4000].splitlines())
        message = f"Docker 原始报错（已脱敏）\n    {output}\n  {message}"
    return CloudError(message, code)


def docker_password_login(host, username, password):
    if not ui.usable(username) or not ui.usable(password):
        raise CloudError("ACR 登录用户名或凭证为空。")
    try:
        result = subprocess.run(
            ["docker", "login", host, "--username", username, "--password-stdin"],
            input=password + "\n", capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise docker_login_failure("Docker 连接 ACR 超时。请核对公网白名单和 Docker 的代理出口。",
                                   "DockerNetwork", exc.stdout, exc.stderr, username, password) from None
    if result.returncode == 0:
        return
    error = (result.stderr + result.stdout).lower()
    status = re.search(r"(?:status(?: code)?[: ]+|\b)(400|407|429|500|502|503|504)\s+(?:bad request|proxy authentication required|too many requests|internal server error|bad gateway|service unavailable|gateway timeout)\b", error)
    if any(word in error for word in ("error storing credentials", "error saving credentials", "credential helper", "keychain")):
        message, code = "Docker 无法保存 ACR 登录。请检查 Docker 凭证助手或系统钥匙串。", "DockerCredentialStore"
    elif any(word in error for word in ("no such domain", "no such host", "nxdomain")):
        message, code = "Docker 或其代理未能解析 ACR 公网域名。", "DockerDNS"
    elif any(word in error for word in ("timeout", "deadline", "connection refused", "connection reset", "broken pipe", "proxyconnect", "tls", "certificate", "eof")):
        message, code = "Docker 无法连接 ACR 公网入口。请核对白名单、DNS 和 Docker 的代理出口。", "DockerNetwork"
    elif status:
        message, code = f"Docker 登录时 Registry 或代理返回 HTTP {status.group(1)}。请核对 Docker 代理和公网入口。", "DockerNetwork"
    elif any(word in error for word in ("unauthorized", "authentication required", "forbidden", "denied", "401", "403")):
        message, code = "ACR 拒绝 Docker 登录。请核对 Registry 凭证、访问权限和公网白名单。", "DockerAuth"
    else:
        message, code = f"Docker 登录 ACR 失败（退出码 {result.returncode}，Registry {host}）。请结合上述报错核对 Docker Desktop 和 ACR 公网入口。", "DockerLogin"
    raise docker_login_failure(message, code, result.stdout, result.stderr, username, password)


def registry_login(cloud, instance, repository):
    try:
        token = cloud.rpc("cr", "GetAuthorizationToken", InstanceId=instance)
    except CloudError as exc:
        raise CloudError(f"ACR 临时凭证获取失败。{exc}", "AcrToken") from None
    username, password = token.get("TempUsername"), token.get("AuthorizationToken")
    if not ui.usable(username) or not ui.usable(password):
        raise CloudError("ACR 未返回有效的临时用户名和密码。请核对 cr:GetAuthorizationToken 权限及实例状态。", "AcrToken")
    host = registry_host(repository)
    print("  ✓ 已获取 ACR 临时凭证。", flush=True)
    print(f"  · 登录当前 Docker 环境 · {host}", flush=True)
    docker_password_login(host, username, password)
    print("  ✓ Docker 已登录，后续推送沿用本次登录。")


def local_team_key_receipt(connection, state):
    secret = connection.get("E2B_API_KEY")
    if not ui.usable(secret):
        return {}
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
    current = state.get("resources", {}).get("key", {})
    history = [item for item in state.get("history", []) if item.get("kind") == "key"]
    matches = {}
    for receipt in [*history, current]:
        value = receipt.get("value", {})
        if value.get("fingerprint") == fingerprint and value.get("teamID") and value.get("apiKeyID"):
            matches[(value["teamID"], value["apiKeyID"])] = receipt
    if len(matches) > 1:
        raise CloudError("同一个本机 API Key 在账本中对应多个资源，请先核对其所属 Team。")
    return next(iter(matches.values()), {})


def team_key_is_active(keys, key_id):
    return any(row["apiKeyID"] == key_id and str(row.get("status", "active")).lower() in {"active", "enabled", "normal"} for row in keys)


def team_key_issues(connection, cloud, team, key):
    from e2b import Sandbox
    from e2b.exceptions import AuthenticationException

    teams = cloud.pages("sandbox", "ListTeams", "teams")
    if not any(row["teamID"] == team for row in teams):
        return {"team": f"{ui.SANDBOX_TEAM_LABEL} {team} 不存在或当前账号不可见"}
    if key:
        if key["teamID"] != team:
            return {"key": f"本机已保存 API Key，其所属 Team 为 {key['teamID']}，与配置中的 Team {team} 不一致"}
        keys = cloud.pages("sandbox", "ListApiKeys", "apiKeys", teamID=team)
        if not team_key_is_active(keys, key["apiKeyID"]):
            return {"key": f"{ui.TEAM_API_KEY_LABEL} {key['apiKeyID']} 不存在或已停用"}
    try:
        Sandbox.list(limit=1, api_key=connection["E2B_API_KEY"], api_url=connection["E2B_API_URL"], domain=connection["E2B_DOMAIN"], request_timeout=15).next_items()
    except AuthenticationException:
        return {"key": f"{ui.TEAM_API_KEY_LABEL}未通过认证"}
    return {}


def restore_saved_team(root, connection, cloud, registry, state):
    key = local_team_key_receipt(connection, state).get("value", {})
    if key and key["teamID"] != registry["team_id"]:
        team = key["teamID"]
        if not team_key_issues(connection, cloud, team, key):
            ui.write_toml_values(root / "config/registry.toml", {"team_id": team})
            registry["team_id"] = team
            print(f"  ✓ 已恢复{ui.SANDBOX_TEAM_LABEL} {team} 与{ui.TEAM_API_KEY_LABEL} {key['apiKeyID']} 的对应配置。")


def resource_issues(connection, cloud, registry, state):
    """Check selected IDs without interpreting a failed query as absence."""
    key = local_team_key_receipt(connection, state).get("value", {})
    issues = team_key_issues(connection, cloud, registry["team_id"], key)

    instance = registry["instance_id"]
    instances = cloud.pages("cr", "ListInstance", "Instances")
    if not any(row["InstanceId"] == instance and row.get("InstanceStatus") == "RUNNING"
               and row.get("InstanceSpecification") in {"Enterprise_Basic", "Enterprise_Standard", "Enterprise_Advanced"} for row in instances):
        issues["acr"] = f"ACR {instance} 不存在或尚不可用"
    else:
        host, namespace, name = registry["repository"].split("/")
        endpoint = cloud.rpc("cr", "GetInstanceEndpoint", InstanceId=instance, EndpointType="internet", ModuleName="Registry")
        domains = {item if isinstance(item, str) else item.get("Domain") for item in endpoint.get("Domains", [])}
        namespaces = cloud.pages("cr", "ListNamespace", "Namespaces", InstanceId=instance)
        repos = cloud.pages("cr", "ListRepository", "Repositories", InstanceId=instance)
        if not registry_endpoint_ready(endpoint) or registry_host(host) not in domains:
            issues["repository"] = f"ACR 公网入口 {host} 尚未生效或与实例不匹配"
        elif not any(row["NamespaceName"] == namespace and row.get("NamespaceStatus", "NORMAL") == "NORMAL" for row in namespaces):
            issues["repository"] = f"ACR 命名空间 {namespace} 不存在或不可用"
        elif not any(row.get("RepoNamespaceName") == namespace and row.get("RepoName") == name and row.get("RepoType") == "PRIVATE" for row in repos):
            issues["repository"] = f"ACR 私有仓库 {namespace}/{name} 不存在或不可用"

    vpc, switch, sg = (registry.get(key) for key in ui.NETWORK_KEYS)
    if vpc:
        region = registry["region"]
        vpcs = cloud.pages("vpc", "DescribeVpcs", "Vpcs.Vpc", RegionId=region)
        if not any(row["VpcId"] == vpc and row.get("Status", "Available") == "Available" for row in vpcs):
            issues["network"] = f"VPC {vpc} 不存在或不可用"
            return issues
        switches = cloud.pages("vpc", "DescribeVSwitches", "VSwitches.VSwitch", RegionId=region, VpcId=vpc)
        groups = cloud.pages("ecs", "DescribeSecurityGroups", "SecurityGroups.SecurityGroup", RegionId=region, VpcId=vpc)
        network = []
        if not any(row["VSwitchId"] == switch and row["VpcId"] == vpc and row.get("Status") == "Available" for row in switches):
            network.append(f"交换机 {switch} 不可用或不属于所选 VPC")
        if not any(row["SecurityGroupId"] == sg and row.get("VpcId") == vpc for row in groups):
            network.append(f"安全组 {sg} 不存在或不属于所选 VPC")
        if "acr" not in issues and not network:
            linked = cloud.rpc("cr", "GetInstanceVpcEndpoint", InstanceId=instance, ModuleName="Registry")
            if not linked.get("Enable") or not any(row["VpcId"] == vpc and row.get("Status") == "RUNNING" for row in linked.get("LinkedVpcs", [])):
                network.append(f"ACR 与 VPC {vpc} 的拉取绑定未就绪")
        if network:
            issues["network"] = "；".join(network)
    return issues


@contextlib.contextmanager
def locked(root):
    import fcntl
    path = Path(root) / "var/state/bootstrap.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.seek(0)
            owner = lock.read(32).strip()
            process = f"（PID {owner}）" if owner.isdecimal() else ""
            raise CloudError(f"另一个准备向导仍在运行{process}。请返回原向导按 Ctrl+C；若处于旧版子终端，先输入 exit 返回。") from None
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        yield


def run(root):
    root = Path(root)
    if ui.local_run_state(root) in {"recorded", "partial"}:
        raise CloudError("存在未回收运行，请先继续或回收旧运行后再准备账号。")
    connection, credentials, registry = ui.load_local(root)
    ui.config_section("2.1 · 验证两个账号入口", "输入内容可见。回车保留已配置值，输入 ? 查看示例，输入 open 打开配置页。")
    for key, label in [("ALIBABA_CLOUD_ACCESS_KEY_ID", "阿里云 AK ID"), ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "阿里云 AK Secret")]:
        credentials[key] = ui.prompt_value(label, credentials.get(key) or os.environ.get(key), secret=True, field=key, show_hint=False)
    key = "ALIBABA_CLOUD_SECURITY_TOKEN"
    credentials[key] = ui.prompt_value("STS Token（长期 AK 留空）", credentials.get(key) or os.environ.get(key), secret=True, optional=True, field=key, show_hint=False)
    region = ui.prompt_value("阿里云地域", registry.get("region") or "cn-hangzhou", field="region", show_hint=False)
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region):
        raise CloudError("地域格式不正确。")
    connection["QODER_ACCESS_TOKEN"] = ui.prompt_value("Qoder PAT", connection.get("QODER_ACCESS_TOKEN") or os.environ.get("QODER_ACCESS_TOKEN"), secret=True, field="QODER_ACCESS_TOKEN", show_hint=False)
    # Read-only Qoder check happens before any Alibaba Cloud mutation.
    from qoder_byoc_sandbox_example.qoder.client import API
    import tomllib
    app = tomllib.loads((root / "config/app.toml").read_text())
    API(app["qoder"]["base_url"], connection["QODER_ACCESS_TOKEN"]).request("GET", "/models")
    print("  ✓ Qoder 授权可用；接下来查询阿里云资源。")
    with locked(root):
        cloud = Cloud(credentials, region)
        b = Bootstrap(root, cloud)
        b.reselect = True
        if registry.get("region") == region:
            if registry.get("team_id"):
                restore_saved_team(root, connection, cloud, registry, b.state)
            b.preferred = registry.copy()
        print("  逐项选择资源，已有可用配置优先显示。新建和购买需确认。")
        ui.write_env_values(root / "config/.env.cloud", credentials)
        ui.write_env_values(root / "config/.env", {"QODER_ACCESS_TOKEN": connection["QODER_ACCESS_TOKEN"]})
        ui.write_toml_values(root / "config/registry.toml", {"region": region})
        registry["region"] = region
        prepare_resources(b, registry, None)
        ui.write_toml_values(root / "config/registry.toml", registry)
        print("\n  ✓ 基础连接已保存")
        print("    地域   " + region)
        print(f"    {ui.SANDBOX_TEAM_LABEL}   {registry['team_id']}")
        print("    仓库   " + registry["repository"])
        print("    配置   config/（汇总不显示密钥，文件权限 0600）")
        print("  下一步：准备网页采集与 Python 分析两套模板。")


def prepare_resources(bootstrap, registry, issues):
    root, cloud = bootstrap.root, bootstrap.cloud
    verified_acr = None
    public_host = None
    while True:
        if issues:
            print("  以下资源需要补选，核验通过后才能继续：")
            for message in issues.values():
                print("    · " + message)
            bootstrap.reselect = True
            bootstrap.dedicated = False
            bootstrap.state["setup_mode"] = "discover"
            bootstrap.save()
        steps = set(issues) if issues is not None else {"team", "key", "acr", "network", "repository"}
        if "team" in steps or "key" in steps:
            if "team" in steps:
                ui.config_section(f"2.2 · 选择{ui.SANDBOX_TEAM_LABEL}", "复用已有 Team，或为演示新建专用 Team。")
                registry["team_id"] = bootstrap.team()["teamID"]
            ui.config_section("2.3 · 选择 Team API Key", "复用本机可用的 Key，或新建专用 Key。")
            bootstrap.key({"teamID": registry["team_id"]}, allow_reuse=not (issues and "key" in issues))
            ui.write_toml_values(root / "config/registry.toml", {"team_id": registry["team_id"]})
        if "acr" in steps:
            ui.config_section("2.4 · 准备镜像仓库实例", "选择 ACR 后先核验本机 Docker 公网登录。")
            registry["instance_id"] = bootstrap.acr()["InstanceId"]
            steps.update(("network", "repository"))
        if verified_acr != registry["instance_id"]:
            if "acr" not in steps:
                ui.config_section("2.4 · 核验 ACR 公网登录", "检查当前 Docker 环境的登录，未登录时使用临时凭证。")
            repository = None if "repository" in steps else registry.get("repository")
            public_host = bootstrap.prepare_registry_login(registry["instance_id"], repository, prefer_existing=True)
            verified_acr = registry["instance_id"]
        if "network" in steps:
            ui.config_section("2.5 · 接通镜像拉取网络", "选择 VPC、交换机和安全组。")
            registry.update(bootstrap.network(registry["instance_id"]))
        if "repository" in steps:
            ui.config_section("2.6 · 确定镜像存放位置", "选择私有仓库并核对本机推送入口。")
            registry["repository"] = bootstrap.repository(registry["instance_id"], public_host)
        ui.write_env_values(root / "config/.env", {
            "E2B_API_URL": f"https://api.{cloud.region}.sandbox.aliyuncs.com",
            "E2B_DOMAIN": f"{cloud.region}.sandbox.aliyuncs.com",
        })
        connection, _, _ = ui.load_local(root)
        print("  · 核对所选资源及连接 …", flush=True)
        issues = resource_issues(connection, cloud, registry, bootstrap.state)
        if not issues:
            print("  ✓ 所选云端资源已核验。")
            return


def uninstall(root):
    with locked(root):
        state = ui.read_json(Path(root) / "var/state/bootstrap.json", {})
        if not state:
            raise CloudError("没有本项目自动准备账本，不能认领旧配置的资源；请用 qoder-byoc-sandbox-example cleanup 回收本轮实例。")
        if state.get("uninstalled"):
            print("✓ 本项目准备资源已回收。请按此前清单核对保留资源和费用。")
            return
        _, credentials, registry = ui.load_local(root)
        Bootstrap(root, Cloud(credentials, registry["region"])).uninstall()


def egress_allows_https(rules):
    if not rules:
        return True  # Only called for normal (not enterprise) security groups.
    if any(r.get("Policy", "Accept").lower() != "accept" for r in rules):
        return False
    for rule in rules:
        ports = rule.get("PortRange", "-1/-1").split("/")
        if rule.get("DestCidrIp") == "0.0.0.0/0" and rule.get("IpProtocol", "").lower() in {"all", "tcp"}:
            if ports == ["-1", "-1"] or (len(ports) == 2 and int(ports[0]) <= 443 <= int(ports[1])):
                return True
    return False


if __name__ == "__main__":
    try:
        if "--help" in sys.argv:
            print("python scripts/bootstrap.py：自动准备；--uninstall：卸载本项目创建的准备资源（先运行 qoder-byoc-sandbox-example cleanup）。")
            sys.exit(0)
        if not sys.stdin.isatty():
            raise CloudError("请在交互式终端运行；批量输入不能绕过客户选择与授权。")
        if sys.argv[1:] == ["--uninstall"]:
            uninstall(ui.ROOT)
        elif len(sys.argv) == 1:
            run(ui.ROOT)
        else:
            raise CloudError("未知参数；使用 --help 查看说明。")
    except KeyboardInterrupt:
        print("\n已停止；保留准备记录，重跑不会盲目重复创建。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # SDK errors are already reduced to code/request ID. Other exceptions
        # may contain request bodies, so do not print arbitrary exception text.
        if type(exc).__name__ == "APIError" and hasattr(exc, "status"):
            message = f"Qoder HTTP {exc.status}：请核对独立 PAT、账号权限与额度。入口：https://qoder.com/account/integrations"
        else:
            message = str(exc) if isinstance(exc, CloudError) else "准备未完成（" + type(exc).__name__ + "）；凭证未输出，配置和记录保留。"
        print(message, file=sys.stderr)
        sys.exit(1)
