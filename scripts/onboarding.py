#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""交互向导的配置、云端预检和恢复判断；不代替 案例 CLI 执行业务流程。"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
from urllib.parse import urlsplit
import urllib.error
import urllib.request
import webbrowser

from dotenv import dotenv_values, set_key


ROOT = Path(os.environ.get("QODER_SANDBOX_HOME", Path(__file__).resolve().parents[1])).resolve()
NETWORK_KEYS = ("vpc_id", "vswitch_id", "security_group_id")
SANDBOX_TEAM_LABEL = "阿里云 Agent Sandbox 极速版 Team"
TEAM_API_KEY_LABEL = "该 Team 下的 API Key"
PHASES = ("collect", "revise", "append")

# Links are service entry points, not credentials. Do not guess account-specific
# IDs or place tokens in a URL when opening the user's browser.
CONFIG_HELP = {
    "QODER_ACCESS_TOKEN": {
        "example": "<从个人账号复制的完整 PAT，不带 Bearer 前缀>",
        "source": "Qoder 国际站个人账号 → Personal Access Tokens；创建后复制完整令牌。",
        "url": "https://qoder.com/account/integrations",
    },
    "E2B_API_KEY": {
        "example": "<阿里云 Agent Sandbox 极速版 Team 下的完整 API Key>",
        "source": "在阿里云 Agent Sandbox 极速版控制台选择 Team，进入该 Team 的 API Keys。",
        "url": "https://console.aliyun.com/sandbox/cn-hangzhou/overview",
    },
    "ALIBABA_CLOUD_ACCESS_KEY_ID": {
        "example": "<实际 AccessKey ID>",
        "source": "RAM 控制台中的 AccessKey；该账号需具有本次 ACR 与模板操作权限。",
        "url": "https://ram.console.aliyun.com/",
    },
    "ALIBABA_CLOUD_ACCESS_KEY_SECRET": {
        "example": "<与上一个 ID 配对的完整 AccessKey Secret>",
        "source": "与上一个 AccessKey ID 配对的 Secret；不会显示或输出到终端日志。",
        "url": "https://ram.console.aliyun.com/",
    },
    "ALIBABA_CLOUD_SECURITY_TOKEN": {
        "example": "长期 AK：留空；临时 STS：<本次临时凭证的 Security Token>",
        "source": "仅使用 STS 临时凭证时填写与 AK 同批签发的 Token。",
        "url": "https://ram.console.aliyun.com/",
    },
    "repository": {
        "example": "<实例名>-registry.cn-hangzhou.cr.aliyuncs.com/sandboxhub/qoder-byoc-sandbox-example",
        "source": "ACR 企业版实例的 Registry 域名 + 命名空间 + 私有仓库；不带 https:// 或 tag。",
        "url": "https://cr.console.aliyun.com/",
    },
    "instance_id": {
        "example": "cri-<实际实例 ID>",
        "source": "ACR 企业版实例详情中的实例 ID；不是镜像仓库 ID。",
        "url": "https://cr.console.aliyun.com/",
    },
    "team_id": {
        "example": "<与本机 API Key 对应的阿里云 Agent Sandbox 极速版 Team ID>",
        "source": "阿里云 Agent Sandbox 极速版控制台的 Team ID。切换 Team 时使用该 Team 下的 API Key。",
        "url": "https://console.aliyun.com/sandbox/cn-hangzhou/overview",
    },
    "region": {
        "example": "cn-hangzhou",
        "source": "使用阿里云地域 ID；控制台可切换地域，Sandbox 与 ACR 配置需对应。",
        "url": "https://console.aliyun.com/sandbox/cn-hangzhou/overview",
    },
    "vpc_id": {
        "example": "vpc-<实际 ID>；未指定网络时留空",
        "source": "ACR 实例 → 访问控制 → 专有网络，使用已绑定且 RUNNING 的 VPC。",
        "url": "https://cr.console.aliyun.com/",
    },
    "vswitch_id": {
        "example": "vsw-<实际 ID>；网络三项一起填或全部留空",
        "source": "在 VPC 控制台选择上面的 VPC，查看其中交换机的 ID。",
        "url": "https://vpc.console.aliyun.com/",
    },
    "security_group_id": {
        "example": "sg-<实际 ID>；需属于上面的 VPC",
        "source": "ECS 控制台 → 安全组，使用同一 VPC 中的安全组 ID。",
        "url": "https://ecs.console.aliyun.com/",
    },
    "E2B_DOMAIN": {
        "example": "cn-hangzhou.sandbox.aliyuncs.com",
        "source": "杭州默认值已提供；只填域名，不带协议或路径。",
        "url": "https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk",
    },
    "E2B_API_URL": {
        "example": "https://api.cn-hangzhou.sandbox.aliyuncs.com",
        "source": "杭州默认 Endpoint 已提供；须与 Domain 和地域对应。",
        "url": "https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk",
    },
    "ACR_USERNAME": {
        "example": "<ACR 访问凭证中给出的登录用户名>",
        "source": "ACR 企业版实例 → 访问凭证，复制镜像仓库登录用户名。",
        "url": "https://cr.console.aliyun.com/",
    },
    "ACR_PASSWORD": {
        "example": "<镜像仓库登录密码或临时 Token>",
        "source": "使用 ACR 登录凭证；不是阿里云控制台密码，也不是 AccessKey Secret。",
        "url": "https://cr.console.aliyun.com/",
    },
}


def show_field_help(field, detailed=True):
    help = CONFIG_HELP.get(field)
    if not help:
        print("填写当前字段的实际值；已有值可回车保留。")
        return
    print("  示例：" + help["example"])
    if detailed:
        print("  获取：" + help["source"])
        print("  地址：" + help["url"])


def open_config_url(url):
    print("  打开配置地址：" + url)
    try:
        opened = webbrowser.open(url, new=2)
    except OSError:
        opened = False
    if not opened:
        print("  无法自动打开浏览器，可复制上面的地址手动访问。")


def show_config_entries(open_pages=False):
    for title, url in [
        ("Qoder 个人访问令牌", CONFIG_HELP["QODER_ACCESS_TOKEN"]["url"]),
        (f"{SANDBOX_TEAM_LABEL} / {TEAM_API_KEY_LABEL}", CONFIG_HELP["E2B_API_KEY"]["url"]),
        ("ACR 实例与私有仓库", CONFIG_HELP["repository"]["url"]),
        ("RAM AccessKey", CONFIG_HELP["ALIBABA_CLOUD_ACCESS_KEY_ID"]["url"]),
    ]:
        print("  " + title + "：" + url)
        if open_pages:
            open_config_url(url)


def config_section(title, description):
    print("\n  ── " + title + " ──")
    print("  " + description)


class NotReusable(ValueError):
    """已确认资源缺失、过期或配置不匹配。"""


def read_json(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def load_local(root):
    config = Path(root) / "config"
    registry = config / "registry.toml"
    return (
        dict(dotenv_values(config / ".env", interpolate=False)),
        dict(dotenv_values(config / ".env.cloud", interpolate=False)),
        tomllib.loads(registry.read_text()) if registry.exists() else {},
    )


def usable(value):
    return isinstance(value, str) and bool(value.strip()) and "<" not in value and ">" not in value and value.lower() not in {"changeme", "your-api-key", "your_api_key"}


def validate_config(connection, cloud, registry):
    required = [
        (connection, ("QODER_ACCESS_TOKEN", "E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN")),
        (cloud, ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")),
        (registry, ("repository", "instance_id", "team_id", "region")),
    ]
    missing = [key for values, keys in required for key in keys if not usable(values.get(key))]
    if missing:
        raise ValueError("尚未填写完整或仍有占位符：" + "、".join(missing))
    region = registry["region"]
    if not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+", region):
        raise ValueError("region 应为 cn-hangzhou 这样的阿里云地域 ID，不含路径、空格或域名")
    endpoint = urlsplit(connection["E2B_API_URL"])
    domain = connection["E2B_DOMAIN"]
    if endpoint.scheme != "https" or endpoint.netloc != "api." + domain or endpoint.path not in {"", "/"} or endpoint.query or endpoint.fragment:
        raise ValueError("E2B_API_URL 应为 https://api.<E2B_DOMAIN>，检查杭州 Endpoint 与 Domain 是否对应")
    if "/" in domain or ":" in domain or not domain.strip():
        raise ValueError("E2B_DOMAIN 只填写域名，不带协议、端口或路径")
    if domain.endswith(".sandbox.aliyuncs.com") and domain != region + ".sandbox.aliyuncs.com":
        raise ValueError("Sandbox Endpoint/Domain 与 registry.toml 的 region 不一致")
    repository = registry["repository"]
    parts = repository.split("/")
    if len(parts) != 3 or not re.fullmatch(r"[A-Za-z0-9.-]+", parts[0]) or "." not in parts[0] or not all(re.fullmatch(r"[a-z0-9][a-z0-9._-]*", part) for part in parts[1:]):
        raise ValueError("repository 应为 Registry域名/命名空间/仓库名，不带 https:// 或镜像 tag")
    provided = [usable(registry.get(key)) for key in NETWORK_KEYS]
    if any(provided) and not all(provided):
        raise ValueError("VPC、vSwitch 和安全组三项必须一起填写，或三项都留空")
    for key in NETWORK_KEYS:
        if registry.get(key) and not usable(registry[key]):
            raise ValueError(key + " 仍为占位符")


def write_env_values(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Invalid environment key")
        set_key(str(path), key, str(value), quote_mode="always")
    path.chmod(0o600)


def write_toml_values(path, values):
    """只更新顶层字符串字段，保留未知字段、表格和注释。"""
    path = Path(path)
    original = path.read_text() if path.exists() else ""
    lines = original.splitlines()
    pending = dict(values)
    output = []
    top_level = True
    for line in lines:
        if line.lstrip().startswith("[") and top_level:
            output.extend(key + "=" + json.dumps(str(value), ensure_ascii=False) for key, value in pending.items())
            pending.clear()
            top_level = False
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=", line) if top_level else None
        if match and match.group(1) in pending:
            key = match.group(1)
            output.append(key + "=" + json.dumps(str(pending.pop(key)), ensure_ascii=False))
        else:
            output.append(line)
    output.extend(key + "=" + json.dumps(str(value), ensure_ascii=False) for key, value in pending.items())
    content = "\n".join(output) + "\n"
    tomllib.loads(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".registry-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def local_run_state(root):
    state = read_json(Path(root) / "var/state/run.json")
    if not state:
        return "empty"
    if state.get("cleaned"):
        return "cleaned"
    return "recorded" if state.get("session_id") else "partial"


def phase_state(root, phase):
    state = read_json(Path(root) / "var/state/run.json", {})
    if state.get(phase + "_verified"):
        return "verified"
    if state.get(phase + "_done"):
        return "done"
    return "submitted" if state.get(phase + "_message_id") else "new"


def prompt_value(label, current="", secret=False, optional=False, field=None, *, show_hint=True):
    current = current if usable(current) else ""
    if secret:
        suffix = " [已配置，回车保留]" if current else ""
    else:
        suffix = " [" + current + "]" if current else ""
    if optional:
        suffix += " [可留空；输入 - 清除]"
    if field and show_hint:
        show_field_help(field, detailed=False)
        print("  配置地址：" + CONFIG_HELP[field]["url"])
        print("  输入 ? 查看获取方式，输入 open 打开对应页面；示例仅供参考。")
    while True:
        # Sensitive saved defaults stay private; new local input is visible.
        value = input(label + suffix + "：").strip()
        if field and value.casefold() in {"?", "help", "帮助"}:
            show_field_help(field)
            continue
        if field and value.casefold() in {"open", "打开"}:
            open_config_url(CONFIG_HELP[field]["url"])
            continue
        if optional and value == "-":
            return ""
        if not value:
            value = current
        if value and not usable(value):
            print("请填写实际值，不要保留尖括号占位符。")
            continue
        if value or optional:
            return value
        print("此项必填；可按 Ctrl+C 停止，取得凭证后重新运行。")


def configure(root):
    if local_run_state(root) in {"recorded", "partial"}:
        raise ValueError("存在未回收的运行资源；先沿用现有配置执行 cleanup，再修改连接信息")
    config = root / "config"
    config.mkdir(exist_ok=True)
    for name in [".env", ".env.cloud", "registry.toml"]:
        path = config / name
        if not path.exists():
            example = config / (name + ".example" if name.startswith(".env") else "registry.example.toml")
            if example.exists():
                shutil.copyfile(example, path)
            else:
                path.touch()
        path.chmod(0o600)
    connection, cloud, registry = load_local(root)
    try:
        validate_config(connection, cloud, registry)
    except ValueError:
        complete = False
    else:
        complete = True
    if complete:
        config_section("已有配置", "凭证只显示已配置状态，不显示密钥。")
        print("  Qoder PAT      已配置")
        print("  Sandbox Key    已配置")
        print("  阿里云 AK       已配置")
        print("  ACR：" + registry["repository"])
        print("  地域：" + registry["region"])
        while True:
            choice = input("回车保留；e 修改；? 查看配置入口；open 打开配置入口：").strip().lower()
            if choice in {"?", "help", "帮助", "open", "打开"}:
                show_config_entries(open_pages=choice in {"open", "打开"})
                continue
            if choice == "e":
                break
            if choice:
                print("请输入 e、?、open，或回车保留。")
                continue
            print("✓ 已保留现有配置；接下来验证实际连接。")
            return
    config_section("1 / 3 · Qoder 与 Sandbox", "复制真实凭证，输入内容可见；填写后立即保存，支持中断后继续。")
    connection["QODER_ACCESS_TOKEN"] = prompt_value("Qoder 国际站 PAT", connection.get("QODER_ACCESS_TOKEN"), secret=True, field="QODER_ACCESS_TOKEN")
    write_env_values(config / ".env", {"QODER_ACCESS_TOKEN": connection["QODER_ACCESS_TOKEN"]})
    connection["E2B_API_KEY"] = prompt_value(f"{SANDBOX_TEAM_LABEL} 下的 API Key", connection.get("E2B_API_KEY"), secret=True, field="E2B_API_KEY")
    write_env_values(config / ".env", {"E2B_API_KEY": connection["E2B_API_KEY"]})
    config_section("2 / 3 · 阿里云模板权限", f"AK 用于模板和 ACR 操作。数据面调用使用{TEAM_API_KEY_LABEL}。")
    cloud["ALIBABA_CLOUD_ACCESS_KEY_ID"] = prompt_value("阿里云 AK ID", cloud.get("ALIBABA_CLOUD_ACCESS_KEY_ID"), secret=True, field="ALIBABA_CLOUD_ACCESS_KEY_ID")
    write_env_values(config / ".env.cloud", {"ALIBABA_CLOUD_ACCESS_KEY_ID": cloud["ALIBABA_CLOUD_ACCESS_KEY_ID"]})
    cloud["ALIBABA_CLOUD_ACCESS_KEY_SECRET"] = prompt_value("阿里云 AK Secret", cloud.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET"), secret=True, field="ALIBABA_CLOUD_ACCESS_KEY_SECRET")
    write_env_values(config / ".env.cloud", {"ALIBABA_CLOUD_ACCESS_KEY_SECRET": cloud["ALIBABA_CLOUD_ACCESS_KEY_SECRET"]})
    cloud["ALIBABA_CLOUD_SECURITY_TOKEN"] = prompt_value("STS Security Token（长期 AK 留空）", cloud.get("ALIBABA_CLOUD_SECURITY_TOKEN"), secret=True, optional=True, field="ALIBABA_CLOUD_SECURITY_TOKEN")
    write_env_values(config / ".env.cloud", {"ALIBABA_CLOUD_SECURITY_TOKEN": cloud["ALIBABA_CLOUD_SECURITY_TOKEN"]})
    config_section("3 / 3 · ACR 仓库与镜像拉取网络", "使用企业版实例；缺失的命名空间与私有仓库将在下一阶段创建。")
    registry["repository"] = prompt_value("ACR 完整仓库地址（不带 tag）", registry.get("repository"), field="repository")
    write_toml_values(config / "registry.toml", {"repository": registry["repository"]})
    registry["instance_id"] = prompt_value("ACR 实例 ID", registry.get("instance_id"), field="instance_id")
    write_toml_values(config / "registry.toml", {"instance_id": registry["instance_id"]})
    registry["team_id"] = prompt_value("与 Sandbox Key 对应的 Team ID", registry.get("team_id"), field="team_id")
    write_toml_values(config / "registry.toml", {"team_id": registry["team_id"]})
    previous_region = registry.get("region") or "cn-hangzhou"
    registry["region"] = prompt_value("阿里云地域", previous_region, field="region")
    write_toml_values(config / "registry.toml", {"region": registry["region"]})
    print("网络参数只用于拉取 ACR 镜像；VPC/vSwitch/安全组三项一起填写，或全部留空由平台推导。")
    for key, label in [("vpc_id", "VPC ID"), ("vswitch_id", "vSwitch ID"), ("security_group_id", "安全组 ID")]:
        registry[key] = prompt_value(label, registry.get(key), optional=True, field=key)
        write_toml_values(config / "registry.toml", {key: registry[key]})
    domain = registry["region"] + ".sandbox.aliyuncs.com"
    old_domain = connection.get("E2B_DOMAIN") or ""
    changed_region = registry["region"] != previous_region
    mismatched_standard_domain = old_domain.endswith(".sandbox.aliyuncs.com") and old_domain != domain
    default_domain = domain if changed_region or mismatched_standard_domain else old_domain or domain
    connection["E2B_DOMAIN"] = prompt_value("Sandbox Domain", default_domain, field="E2B_DOMAIN")
    write_env_values(config / ".env", {"E2B_DOMAIN": connection["E2B_DOMAIN"]})
    default_url = connection.get("E2B_API_URL") or ""
    if changed_region or urlsplit(default_url).netloc != "api." + connection["E2B_DOMAIN"]:
        default_url = "https://api." + connection["E2B_DOMAIN"]
    connection["E2B_API_URL"] = prompt_value("Sandbox API URL", default_url, field="E2B_API_URL")
    write_env_values(config / ".env", {"E2B_API_URL": connection["E2B_API_URL"]})
    try:
        validate_config(connection, cloud, registry)
    except ValueError as exc:
        raise ValueError(str(exc) + "。已完成的输入已安全保存；重新运行可回车复用，修正后才会进入云端阶段。") from None
    print("配置已保存：config/.env、config/.env.cloud、config/registry.toml（权限 600）。")


def check_config(root):
    validate_config(*load_local(root))
    print("配置字段完整：Qoder / Sandbox / AK / ACR / Team / 镜像拉取网络。密钥未显示。")
    print("这里只检查格式；Qoder、Sandbox、ACR 的实际访问由接下来各阶段验证。")


def cloud_config(root, endpoint):
    from alibabacloud_tea_openapi.models import Config
    _, cloud, registry = load_local(root)
    return Config(access_key_id=cloud["ALIBABA_CLOUD_ACCESS_KEY_ID"], access_key_secret=cloud["ALIBABA_CLOUD_ACCESS_KEY_SECRET"], security_token=cloud.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None, endpoint=endpoint, region_id=registry["region"], read_timeout=20000, connect_timeout=10000)


class AcrError(RuntimeError):
    def __init__(self, action, code):
        self.code = str(code or "unknown")
        message = f"ACR {action} 失败（{self.code}）。"
        if self.code.upper() == "INSTANCE_NOT_EXIST":
            message += "当前账号和地域下找不到配置中的 ACR 实例。请核对实例与订单，再回到配置阶段准备可用实例。"
        super().__init__(message)


def check_acr_response(action, response):
    code = response.get("Code", response.get("code"))
    if response.get("IsSuccess") is False or response.get("Success") is False or response.get("success") is False or (
        code is not None and str(code).lower() not in {"success", "200", "ok"}
    ):
        raise AcrError(action, code)


def registry_rpc_request(client, action, query):
    from alibabacloud_tea_openapi.models import Params, OpenApiRequest
    from alibabacloud_tea_openapi.utils import Utils
    from alibabacloud_tea_util.models import RuntimeOptions

    # The generic client expects map[string]string here, just like the
    # generated product SDK's Utils.query(...) request conversion. Passing
    # integer pagination values directly fails during request signing.
    response = client.call_api(
        Params(
            action=action,
            version="2018-12-01",
            protocol="HTTPS",
            pathname="/",
            method="POST",
            auth_type="AK",
            style="RPC",
            req_body_type="formData",
            body_type="json",
        ),
        OpenApiRequest(query=Utils.query(query)),
        RuntimeOptions(read_timeout=20000, connect_timeout=10000),
    )["body"]
    check_acr_response(action, response)
    return response


def prepare_registry(root, client=None, *, allow_create=True):
    from alibabacloud_tea_openapi.client import Client

    connection, cloud, registry = load_local(root)
    validate_config(connection, cloud, registry)
    _, namespace, name = registry["repository"].split("/")
    if client is None:
        client = Client(cloud_config(root, "cr." + registry["region"] + ".aliyuncs.com"))

    def call(action, query):
        return registry_rpc_request(client, action, {"InstanceId": registry["instance_id"], **query})

    if registry.get("vpc_id"):
        endpoint = call("GetInstanceVpcEndpoint", {"ModuleName": "Registry"})
        matches = [vpc for vpc in endpoint.get("LinkedVpcs", []) if vpc.get("VpcId") == registry["vpc_id"] and vpc.get("Status") == "RUNNING"]
        if not endpoint.get("Enable") or not matches:
            raise ValueError("ACR 尚未绑定配置中的 VPC，或绑定未到 RUNNING；请到 ACR 的访问控制 → 专有网络完成绑定后重试。向导不会修改 VPC。")
        print("ACR 镜像拉取 VPC 已绑定且 RUNNING。")
    existing = call("ListNamespace", {"NamespaceName": namespace, "PageNo": 1, "PageSize": 100})
    namespaces = [item for item in existing.get("Namespaces", []) if item.get("NamespaceName") == namespace]
    if not namespaces:
        if not allow_create:
            raise ValueError("保存的 ACR 命名空间已不可用。请在配置阶段选择「新建 / 更换资源」。")
        call("CreateNamespace", {"NamespaceName": namespace, "AutoCreateRepo": "false", "DefaultRepoType": "PRIVATE"})
        print("已创建 ACR 命名空间：" + namespace)
    elif namespaces[0].get("NamespaceStatus") != "NORMAL":
        raise ValueError("指定 ACR 命名空间尚未就绪")
    existing = call("ListRepository", {"RepoNamespaceName": namespace, "RepoName": name, "PageNo": 1, "PageSize": 100})
    repositories = [item for item in existing.get("Repositories", []) if item.get("RepoNamespaceName") == namespace and item.get("RepoName") == name]
    if not repositories:
        if not allow_create:
            raise ValueError("保存的 ACR 私有仓库已不可用。请在配置阶段选择「新建 / 更换资源」。")
        call("CreateRepository", {"RepoNamespaceName": namespace, "RepoName": name, "RepoType": "PRIVATE", "Summary": "Qoder BYOC Sandbox Example", "TagImmutability": "false"})
        print("已创建 ACR 私有仓库：" + namespace + "/" + name)
    elif repositories[0].get("RepoType") != "PRIVATE":
        raise ValueError("配置中的仓库不是 PRIVATE，向导不会更改已有仓库权限")
    else:
        print("已确认 ACR 私有仓库可用：" + namespace + "/" + name)


def source_digest(root):
    from qoder_byoc_sandbox_example.sandbox.templates import source_digest as runtime_digest

    return runtime_digest(Path(root))


def not_found(exc):
    return any(getattr(exc, key, None) == 404 for key in ["status", "status_code", "statusCode"]) or getattr(exc, "code", None) in {"TemplateNotFound", "NotFound", "ResourceNotFound", "InvalidTemplateId.NotFound"}


def require_template_ready(status, kind):
    if status == "ready":
        return
    if status in {"error", "failed", "disabled", "deleted"}:
        raise NotReusable(kind + " 模板处于 " + status + " 终态，需要重新构建")
    if status in {"building", "pending", "submitted", "creating", "initializing"}:
        raise RuntimeError(kind + " 模板仍在 " + status + "，已有构建任务，暂不重复创建；稍后重新运行向导检查已有模板")
    raise RuntimeError(kind + " 模板状态未知，不能确认是否需要重建：" + str(status))


def template_team_check(root, *, announce=True):
    """Resolve stale Team metadata only with matching AK and API Key evidence."""
    from bootstrap import Cloud
    from alibabacloud_sandbox20260820.client import Client
    from alibabacloud_sandbox20260820.models import GetTemplateRequest
    from e2b import Template
    from e2b.template.types import BuildInfo
    from qoder_byoc_sandbox_example.sandbox.client import opts

    root = Path(root)
    connection, credentials, registry = load_local(root)
    teams = Cloud(credentials, registry["region"]).pages("sandbox", "ListTeams", "teams")
    configured = registry["team_id"]
    if any(team["teamID"] == configured for team in teams):
        if announce:
            print(f"✓ {SANDBOX_TEAM_LABEL} 已确认，当前 AK 可访问。")
        return configured

    guidance = (
        f"配置中的{SANDBOX_TEAM_LABEL}不在当前 AK 可见范围，不能将模板 404 当作模板已删除。"
        "请重新运行 ./start.sh，在第 2 阶段选择「新建 / 更换资源」，核对 AK、地域、Team 与该 Team 下的 API Key；"
        "已有配置和模板记录保留，不自动重建。"
    )
    saved = read_json(root / "var/state/templates.json", {})
    identity, build_id = saved.get("browser_template"), saved.get("browser_build_id")
    if not identity or not build_id:
        raise RuntimeError(guidance)
    client = Client(cloud_config(root, "sandbox." + registry["region"] + ".aliyuncs.com"))
    matches = []
    for team in teams:
        try:
            item = client.get_template(identity, GetTemplateRequest(team_id=team["teamID"])).body
        except Exception as exc:
            if not_found(exc):
                continue
            raise
        if item and item.template_id == identity and item.team_id == team["teamID"]:
            matches.append(team["teamID"])
    if len(matches) != 1:
        raise RuntimeError(guidance)
    name = saved.get("browser_template_name", identity)
    status = Template.get_build_status(
        BuildInfo(template_id=identity, build_id=build_id, name=name, alias=name),
        **opts(connection),
    )
    if status.template_id != identity or status.build_id != build_id or status.status != "ready":
        raise RuntimeError("当前 API Key 尚不能确认同一 Browser 模板已就绪；" + guidance)
    if load_local(root)[2] != registry or read_json(root / "var/state/templates.json", {}) != saved:
        raise RuntimeError("核对期间本地配置或模板记录已改变，停止修改 Team，请重跑。")
    write_toml_values(root / "config/registry.toml", {"team_id": matches[0]})
    if announce:
        print("✓ 已按现有 Browser 模板校正过期 Team 配置；保留原 Key 和模板，不重复创建。")
    return matches[0]


def refresh_analysis_metadata(root, identity, image):
    path = Path(root) / "var/state/templates.json"
    metadata = read_json(path, {})
    if metadata.get("analysis_template") != identity:
        raise RuntimeError("本地分析模板 ID 已改变，停止刷新构建状态，避免覆盖其他运行")
    metadata["analysis_build_status"] = "ready"
    metadata["analysis_image"] = image
    metadata.pop("analysis_build_error", None)
    fd, name = tempfile.mkstemp(prefix=".templates-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(metadata, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def template_profile_matches(runtime, profile, saved_profile, expected_image, *, kind="browser", registry=None):
    if runtime is None or runtime.sandbox_config is None:
        return False
    sandbox = runtime.sandbox_config
    matches = [
        runtime.cpu == profile["cpu"],
        runtime.memory_size == profile["memory_mb"],
        sandbox.image == expected_image,
    ]
    for key in ["start_command", "ready_command"]:
        remote_value = getattr(sandbox, key, None)
        if remote_value is None:
            # Some native responses omit these two fields. The snapshot was
            # saved together with this template ID by our build command.
            matches.append(saved_profile == profile)
        else:
            matches.append(remote_value == profile[key])
    if kind == "analysis":
        matches.extend([
            runtime.disk_size == profile["disk_mb"],
            sandbox.generation == profile["generation"],
        ])
        registry = registry or {}
        if registry.get("instance_id"):
            matches.append(sandbox.acr_instance_id == registry["instance_id"])
        if registry.get("vpc_id"):
            network = getattr(getattr(sandbox, "registry_config", None), "network_config", None)
            matches.extend(
                getattr(network, field, None) == registry[key]
                for field, key in [("vpc_id", "vpc_id"), ("v_switch_id", "vswitch_id"), ("security_group_id", "security_group_id")]
            )
    return all(matches)


def template_check(root, kind):
    from alibabacloud_sandbox20260820.client import Client
    from alibabacloud_sandbox20260820.models import GetTemplateRequest
    saved = read_json(root / "var/state/templates.json", {})
    identity = saved.get(kind + "_template")
    if not identity:
        raise NotReusable(kind + " 模板尚未创建")
    template_team_check(root, announce=False)
    connection, cloud, registry = load_local(root)
    validate_config(connection, cloud, registry)
    profile = tomllib.loads((root / "config/templates.toml").read_text())[kind]
    if kind == "browser":
        profile["image"] = profile["image"].replace("fc-e2b-registry.cn-hangzhou.", "fc-e2b-registry." + registry["region"] + ".")
    client = Client(cloud_config(root, "sandbox." + registry["region"] + ".aliyuncs.com"))
    try:
        remote = client.get_template(identity, GetTemplateRequest(team_id=registry["team_id"])).body
    except Exception as exc:
        if not_found(exc):
            raise NotReusable(kind + " 模板已不存在，需要重新构建") from None
        raise
    if remote.status is None:
        raise RuntimeError("模板接口未返回状态，不能确认模板可复用")
    require_template_ready(remote.status.state, kind)
    runtime = remote.runtime_config
    expected_image = profile.get("image")
    if kind == "analysis":
        expected_image = registry["repository"].replace("-registry.", "-registry-vpc.") + ":analysis-" + source_digest(root)
    if not template_profile_matches(runtime, profile, saved.get(kind + "_profile"), expected_image, kind=kind, registry=registry):
        raise NotReusable(kind + " 模板与当前源码、镜像或规格配置不一致，需要重新构建")
    if kind == "analysis":
        refresh_analysis_metadata(root, identity, expected_image)
    print(kind + " 模板已核对：云端 ready，镜像及配置一致（" + identity + "）。")


def run_status(root):
    local = local_run_state(root)
    if local != "recorded":
        return local
    from e2b import Sandbox, NotFoundException, CommandExitException
    connection, _, _ = load_local(root)
    state = read_json(root / "var/state/run.json", {})
    app = tomllib.loads((root / "config/app.toml").read_text())
    if state.get("api_url") != connection.get("E2B_API_URL") or state.get("qoder_url") != app["qoder"]["base_url"]:
        raise ValueError("当前配置与未回收的运行记录不一致，请恢复原配置后清理")
    if not state.get("source_digest") or state["source_digest"] != source_digest(root):
        print("源码已更新或旧运行缺少版本记录；请先 cleanup 再重新创建，避免复用旧镜像和工具。", file=sys.stderr)
        return "partial"
    keys = ["analysis_sandbox_id", "browser_sandbox_id"]
    if not all(state.get(key) for key in keys):
        return "partial"
    options = dict(api_key=connection["E2B_API_KEY"], api_url=connection["E2B_API_URL"], domain=connection["E2B_DOMAIN"], request_timeout=20)
    boxes = []
    try:
        for key in keys:
            box = Sandbox.connect(state[key], **options)
            info = box.get_info()
            if (info.metadata or {}).get("demo_run") != state["run_id"]:
                raise ValueError("实例归属与本次运行不一致，停止自动恢复")
            boxes.append(box)
    except NotFoundException:
        return "expired"
    gateway = read_json(root / "var/state/gateway.json", {})
    pid = state.get("worker_pid")
    if gateway.get("analysis_url") != state.get("analysis_url") or not pid or not str(pid).isdigit():
        return "partial"
    if not gateway.get("analysis_token") or not gateway.get("browser_token"):
        return "partial"
    try:
        check = boxes[0].commands.run("test -f /tmp/demo-worker/ready.json && kill -0 " + str(pid), timeout=10, request_timeout=20)
    except CommandExitException:
        return "partial"
    if check.exit_code:
        return "partial"
    return "ready"


def show_links(root):
    state = read_json(root / "var/state/run.json", {})
    config = tomllib.loads((root / "config/demo.toml").read_text())
    print(json.dumps({"session_url": state.get("session_url", ""), "preview_url": "http://127.0.0.1:" + str(config["demo"]["preview_port"])}, ensure_ascii=False))


def preview_status(root):
    config = tomllib.loads((root / "config/demo.toml").read_text())
    port = config["demo"]["preview_port"]
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except ConnectionRefusedError:
        return "free"

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    state = read_json(root / "var/state/run.json", {})
    if not state.get("run_id") or state.get("cleaned"):
        return "occupied"
    try:
        with urllib.request.build_opener(NoRedirect).open(
            "http://127.0.0.1:" + str(port) + "/api/identity", timeout=3
        ) as response:
            data = response.read(65537)
            if len(data) > 65536:
                return "occupied"
            identity = json.loads(data)
    except (urllib.error.URLError, OSError, ValueError):
        return "occupied"
    if not isinstance(identity, dict):
        return "occupied"
    keys = ["run_id", "analysis_sandbox_id", "browser_sandbox_id"]
    return "ready" if all(state.get(key) and identity.get(key) == state[key] for key in keys) else "occupied"


def completion_baseline(state):
    phases = ("collect", "revise", "append")
    if not all(state.get(phase + "_verified") is True and isinstance(state.get(phase + "_snapshot"), dict) and state[phase + "_snapshot"] for phase in phases):
        raise ValueError("默认三轮尚未全部验收通过，请先完成相应轮次；不会跳过失败的验收。")
    return {phase: state[phase + "_snapshot"] for phase in phases}


def finish_summary(root):
    """Show already-verified milestones without revalidating mutable live data."""
    from terminal_cards import Cards
    cards = Cards()
    state = read_json(Path(root) / "var/state/run.json", {})
    snapshots = completion_baseline(state)
    cards.line("默认三轮 · 已完成独立验收", "green")
    for phase, title in [("collect", "首次分析"), ("revise", "连续追问"), ("append", "现场追加")]:
        data = snapshots[phase]
        cards.line(f"✓ {title} · v{data['revision']} · {data['row_count']} 条 · {data['total_amount']:,.2f}")
    try:
        from qoder_byoc_sandbox_example.settings import load
        from qoder_byoc_sandbox_example.demos.report_analyst.shared import connect
        _, values = load()
        current = json.loads(connect(state, values).files.read("/workspace/artifacts/dashboard.json"))
        baseline = snapshots["append"]
        changed = current != baseline
        if changed:
            cards.line()
            cards.line("后续自由问答 · 当前看板已变化", "yellow")
            cards.line(f"v{current['revision']} · {current['row_count']} 条 · {current['total_amount']:,.2f}", "yellow")
            cards.line(f"相对默认第三轮：{current['row_count'] - baseline['row_count']:+d} 条，金额 {current['total_amount'] - baseline['total_amount']:+,.2f}")
            cards.paragraph("保留你的当前结果。以上新版本未按默认三轮规则重新验收，不影响此前已通过的三轮结论。")
        else:
            cards.line("当前看板仍与默认第三轮的验收快照一致。", "green")
    except Exception:
        cards.line("当前看板暂不可读取；以上为此前已通过的验收记录，不代表当前实例仍可用。", "yellow")


def template_summary(root, kind):
    saved = read_json(Path(root) / "var/state/templates.json", {})
    label = "网页采集" if kind == "browser" else "报表分析"
    print("    " + label + " Template ID  " + str(saved.get(kind + "_template", "未记录")))
    name = saved.get(kind + "_template_name")
    if name:
        print("    模板名称  " + name)


def summary(root):
    state = read_json(root / "var/state/run.json", {})
    if not state:
        print("尚无本次运行资源。")
        return
    print("\n  本轮现场")
    for label, key in [("运行编号", "run_id"), ("Qoder 会话", "session_url"), ("报表分析 Sandbox", "analysis_sandbox_id"), ("网页采集 Sandbox", "browser_sandbox_id")]:
        if state.get(key):
            print("    " + label + "  " + str(state[key]))
    names = {"collect":"首次分析", "revise":"连续追问", "append":"现场追加"}
    labels = {"new":"待开始", "submitted":"已提交", "done":"待验收", "verified":"已验收"}
    for phase in PHASES:
        snapshot = state.get(phase + "_snapshot", {})
        status = phase_state(root, phase)
        details = " · " + str(snapshot.get("row_count", "—")) + " 条 / " + str(snapshot.get("total_amount", "—")) if snapshot else ""
        print("    " + names[phase] + "  " + labels.get(status, status) + details)
    print("    验收记录  var/results/" + state.get("run_id", ""))
    if state.get("cleaned"):
        print("本次 Qoder 与 Sandbox 运行资源已回收，模板和镜像保留。")


def docker_login(root):
    from bootstrap import Cloud, ensure_registry_login

    _, credentials, registry = load_local(root)
    ensure_registry_login(Cloud(credentials, registry["region"]), registry["instance_id"],
                          registry["repository"], interactive=True)


def round_intro(root, phase, status="new"):
    from terminal_cards import Cards
    scenarios = tomllib.loads((Path(root) / "config/demo.toml").read_text())["scenarios"]
    Cards().intro(phase, scenarios[phase], status)


def display_events(root):
    from terminal_cards import Cards
    cards = Cards()
    tools = {
        "Read": "[分析沙箱] 读取 Skill 或文件",
        "download_report": "[网页采集] 筛选、翻页、下载 CSV → 传入分析沙箱",
        "run_python": "[报表分析] 执行本轮 Python · 输入 input.csv",
        "publish_dashboard": "[报表分析] 独立核算 → 更新看板",
        "append_record": "[报表分析] 向工作副本追加 1 条记录",
        "export_artifacts": "[报表分析] 导出 CSV / Excel / JSON / HTML",
        "runtime_evidence": "核对实际执行环境",
    }
    action_count = 0
    for line in sys.stdin:
        try:
            event = json.loads(line)
        except ValueError:
            print(line.rstrip(), flush=True)
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "task_submitted":
            cards.line("→ 消息已提交，等待 Qoder 调度工具。", "cyan")
        elif kind == "qoder_event":
            event_type = event.get("type")
            if event_type in {"agent.tool_use", "agent.custom_tool_use"}:
                tool = event.get("name", "工具")
                if not action_count:
                    cards.line()
                    cards.line("执行过程 · Qoder 工具调度", "bold")
                action_count += 1
                cards.paragraph(f"{action_count:02d}  " + tools.get(tool, "[分析沙箱] " + tool))
        elif kind == "verification":
            cards.result(event)
        elif kind == "LIVE_PASS":
            print("三轮任务全部验收通过（LIVE_PASS）。", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="start.sh 的配置与恢复支撑工具")
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ["configure", "bootstrap", "check-config", "template-team-check", "run-status", "preview-status", "show-links", "summary", "finish-summary", "docker-login", "display-events"]:
        commands.add_parser(name)
    commands.add_parser("prepare-registry").add_argument("--verify-only", action="store_true")
    for name, choices in [("template-check", ["browser", "analysis"]), ("template-summary", ["browser", "analysis"]), ("phase-state", PHASES), ("round-intro", PHASES)]:
        command = commands.add_parser(name)
        command.add_argument("kind", choices=choices)
        if name == "round-intro":
            command.add_argument("--status", choices=["new", "submitted", "done", "verified"], default="new")
    args = parser.parse_args(argv)
    if args.action == "bootstrap":
        from bootstrap import run
        run(ROOT)
    elif args.action == "prepare-registry":
        prepare_registry(ROOT, allow_create=not args.verify_only)
    elif args.action == "template-check":
        template_check(ROOT, args.kind)
    elif args.action == "template-summary":
        template_summary(ROOT, args.kind)
    elif args.action == "phase-state":
        print(phase_state(ROOT, args.kind))
    elif args.action == "round-intro":
        round_intro(ROOT, args.kind, args.status)
    elif args.action == "run-status":
        print(run_status(ROOT))
    elif args.action == "preview-status":
        print(preview_status(ROOT))
    else:
        globals()[args.action.replace("-", "_")](ROOT)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n向导已停止；已保存的配置保留，可重新运行。", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        from qoder_byoc_sandbox_example.settings import safe_error
        connection = dict(dotenv_values(ROOT / "config/.env", interpolate=False))
        cloud = dict(dotenv_values(ROOT / "config/.env.cloud", interpolate=False))
        prefix = "" if isinstance(exc, NotReusable) else "ERROR: "
        print(prefix + safe_error(exc, {**connection, **cloud}), file=sys.stderr)
        raise SystemExit(1 if isinstance(exc, NotReusable) else 2) from None
