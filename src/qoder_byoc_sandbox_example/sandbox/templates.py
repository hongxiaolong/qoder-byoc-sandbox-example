#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""构建 Browser 与自定义分析模板。

演示目标：Browser 使用官方镜像；分析镜像将运行依赖和应用固化，实例创建后直接服务。
执行流程：构建 linux/amd64 镜像、通过标准输入登录并推送 ACR，再创建二代模板并等就绪。
能力体现：标准镜像复用、模板启动/就绪命令；模板就绪与实例测试分别验证。
"""

import argparse
from collections import deque
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib

from dotenv import dotenv_values
from qoder_byoc_sandbox_example.settings import (
    ROOT,
    CONFIG_DIR,
    STATE_DIR,
    load,
    safe_error,
)
from .client import opts


def profile(kind):
    cfg = tomllib.loads((CONFIG_DIR / "templates.toml").read_text())[kind]
    if kind == "browser" and (CONFIG_DIR / "registry.toml").exists():
        region = tomllib.loads((CONFIG_DIR / "registry.toml").read_text())["region"]
        cfg["image"] = cfg["image"].replace("fc-e2b-registry.cn-hangzhou.", "fc-e2b-registry." + region + ".")
    return cfg


def project_name():
    return tomllib.loads((CONFIG_DIR / "app.toml").read_text())["project"]["name"]


def state():
    p = STATE_DIR / "templates.json"
    return json.loads(p.read_text()) if p.exists() else {}


def store(data):
    p = STATE_DIR / "templates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".templates-", dir=p.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
        os.replace(temporary, p)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record_created(saved, kind, template_id, scope):
    """Preserve every created ID; legacy IDs remain explicitly unclaimed."""
    history = saved.setdefault("created_templates", [])
    previous = saved.get(kind + "_template")
    if previous and not any(item.get("template_id") == previous for item in history):
        history.append({"kind": kind, "template_id": previous})
    record = {"kind": kind, "template_id": template_id,
              "team_id": scope.get("team_id"), "region": scope.get("region"),
              "created_by_demo": True}
    if not any(item == record for item in history):
        history.append(record)


def source_digest(root=ROOT):
    """Identify every source and image input delivered by this demo."""
    digest = hashlib.sha256()
    for directory in ["src", "images"]:
        for path in sorted((root / directory).rglob("*")):
            if (
                path.is_file()
                and (path.suffix in {".py", ".html", ".css", ".js", ".svg", ".png", ".txt"}
                     or path.name in {"Dockerfile", "NOTICE.md"})
                and "__pycache__" not in path.parts
            ):
                digest.update(str(path.relative_to(root)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def push_image(image, *, attempts=3, retry_delays=(2, 5)):
    """Retry transient upload failures without rebuilding or creating templates."""
    permanent = (
        "unauthorized", "authentication required", "denied:", "no basic auth",
        "x509:", "certificate", "invalid reference", "manifest invalid",
        "repository does not exist",
    )
    transient = (
        "broken pipe", "connection reset", "connection refused", "unexpected eof",
        "i/o timeout", "tls handshake timeout", "context deadline exceeded",
        "network is unreachable", "no such host", "proxyconnect", "temporary failure",
        "service unavailable", "bad gateway", "gateway timeout", "too many requests",
        "500 internal server error", "502 bad gateway", "503 service unavailable",
    )
    for attempt in range(1, attempts + 1):
        print(f"推送分析镜像（第 {attempt}/{attempts} 次）", flush=True)
        tail = deque(maxlen=80)
        with subprocess.Popen(
            ["docker", "push", image], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1,
        ) as process:
            try:
                for line in process.stdout:
                    # Upload URLs can contain temporary signed credentials.
                    clean = re.sub(r'(https?://[^\s"?]+)\?[^\s"]+', r'\1?[redacted]', line)
                    print(clean, end="", flush=True)
                    tail.append(clean)
                code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
        if code == 0:
            return
        reason = "".join(tail).lower()
        retryable = not any(word in reason for word in permanent) and any(word in reason for word in transient)
        if retryable and attempt < attempts:
            delay = retry_delays[min(attempt - 1, len(retry_delays) - 1)]
            print(f"上传连接暂时中断，{delay} 秒后重试同一镜像；已上传的镜像层可复用。", flush=True)
            time.sleep(delay)
            continue
        guidance = (
            "请检查 Docker Desktop 的代理/VPN 和网络连接后重新运行 ./start.sh。"
            if retryable else
            "请根据上面的 Docker 错误检查仓库登录、权限、证书或镜像配置，再重新运行 ./start.sh。"
        )
        raise RuntimeError(
            f"镜像推送失败（已尝试 {attempt} 次，退出码 {code}）。" + guidance
            + " 本次尚未提交分析模板创建请求；已有配置与 var/state/ 保留，不要删除。"
        )


def build_name(kind, cfg):
    prefix = cfg.get("name_prefix", "qoder-byoc-sandbox-" + ("web" if kind == "browser" else "python"))
    name = prefix + "-" + str(int(time.time()))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", name):
        raise ValueError("模板名称需为 1–36 个字母、数字、连字符或下划线；请缩短 config/templates.toml 的 name_prefix")
    return name


def browser():
    from e2b import Template

    _, values = load(required=["E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"])
    cfg = profile("browser")
    saved = state()
    registry_path = CONFIG_DIR / "registry.toml"
    scope = tomllib.loads(registry_path.read_text()) if registry_path.exists() else {}
    template_name = build_name("browser", cfg)
    build = Template.build(
        Template().from_image(cfg["image"]),
        name=template_name,
        cpu_count=cfg["cpu"],
        memory_mb=cfg["memory_mb"],
        headers={
            "X-E2B-Template-Build-Mode": "micro",
            "X-E2B-Template-Alpha-Start-Command": cfg["start_command"],
            "X-E2B-Template-Alpha-Ready-Command": cfg["ready_command"],
        },
        **{**opts(values), "request_timeout": 600},
    )
    record_created(saved, "browser", build.template_id, scope)
    saved["browser_template"] = build.template_id
    saved["browser_template_name"] = template_name
    saved["browser_image"] = cfg["image"]
    saved["browser_build_id"] = build.build_id
    saved["browser_start_ready"] = True
    saved["browser_profile"] = cfg
    store(saved)
    print("Browser template ready:", build.template_id)


def analysis(*, use_docker_login=False):
    from alibabacloud_sandbox20260820 import models
    from alibabacloud_sandbox20260820.client import Client
    from alibabacloud_tea_openapi import models as config_models

    cfg = profile("analysis")
    template_name = build_name("analysis", cfg)
    resources = tomllib.loads((CONFIG_DIR / "registry.toml").read_text())
    secrets = {
        **dotenv_values(CONFIG_DIR / ".env.cloud", interpolate=False),
        **dotenv_values(CONFIG_DIR / ".env.registry", interpolate=False),
    }
    required = ["ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET"]
    for key in required:
        if not secrets.get(key):
            raise ValueError("请在本地凭证文件填写 " + key)
    for key in ["repository", "team_id"]:
        if not resources.get(key):
            raise ValueError("请在 config/registry.toml 填写 " + key)
    network_keys = ["vpc_id", "vswitch_id", "security_group_id"]
    if any(resources.get(key) for key in network_keys) and not all(
        resources.get(key) for key in network_keys
    ):
        raise ValueError("VPC、vSwitch 和安全组三项必须一起填写")
    tag = "analysis-" + source_digest()
    repository = resources["repository"].rstrip(":/")
    push_repository = repository.replace("-registry-vpc.", "-registry.")
    image = push_repository + ":" + tag
    server_image = repository.replace("-registry.", "-registry-vpc.") + ":" + tag
    try:
        subprocess.run(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64",
                "--load",
                "-t",
                image,
                "-f",
                cfg["dockerfile"],
                ".",
            ],
            cwd=ROOT,
            check=True,
        )
        if not use_docker_login and secrets.get("ACR_USERNAME") and secrets.get("ACR_PASSWORD"):
            subprocess.run(
                [
                    "docker",
                    "login",
                    push_repository.split("/")[0],
                    "-u",
                    secrets["ACR_USERNAME"],
                    "--password-stdin",
                ],
                input=secrets["ACR_PASSWORD"].encode(),
                check=True,
                capture_output=True,
            )
        else:
            print("Using existing Docker registry login", flush=True)
        push_image(image)
        config = config_models.Config(
            access_key_id=secrets["ALIBABA_CLOUD_ACCESS_KEY_ID"],
            access_key_secret=secrets["ALIBABA_CLOUD_ACCESS_KEY_SECRET"],
            security_token=secrets.get("ALIBABA_CLOUD_SECURITY_TOKEN") or None,
            region_id=resources["region"],
            endpoint="sandbox." + resources["region"] + ".aliyuncs.com",
        )
        client = Client(config)
        fields = dict(
            image=server_image,
            generation=cfg["generation"],
            start_command=cfg["start_command"],
            ready_command=cfg["ready_command"],
        )
        if resources.get("instance_id"):
            fields["acr_instance_id"] = resources["instance_id"]
        if resources.get("vpc_id"):
            fields["registry_config"] = models.CreateTemplateRegistryConfig(
                network_config=models.CreateTemplateRegistryNetworkConfig(
                    vpc_id=resources["vpc_id"],
                    v_switch_id=resources["vswitch_id"],
                    security_group_id=resources["security_group_id"],
                )
            )
        result = client.create_template(
            models.CreateTemplateRequest(
                body=models.CreateTemplateInput(
                    name=template_name,
                    team_id=resources["team_id"],
                    runtime_config=models.CreateTemplateRuntimeConfig(
                        cpu=cfg["cpu"],
                        memory_size=cfg["memory_mb"],
                        disk_size=cfg["disk_mb"],
                        sandbox_config=models.CreateTemplateSandboxConfig(**fields),
                    ),
                )
            )
        )
        tid = result.body.template_id
        saved = state()
        record_created(saved, "analysis", tid, resources)
        saved.pop("analysis_build_error", None)
        saved.update(
            analysis_template=tid,
            analysis_template_name=template_name,
            analysis_image=server_image,
            analysis_build_status="submitted",
            analysis_profile=cfg,
        )
        store(saved)
        deadline = time.monotonic() + 1200
        previous = None
        while time.monotonic() < deadline:
            item = client.get_template(
                tid, models.GetTemplateRequest(team_id=resources["team_id"])
            )
            status = item.body.status.state
            if status != previous:
                print("Analysis template", tid, status, flush=True)
                previous = status
            if status == "error":
                saved["analysis_build_status"] = "error"
                saved["analysis_build_error"] = item.body.status.reason.message
                store(saved)
                raise RuntimeError(saved["analysis_build_error"])
            if status == "ready":
                saved["analysis_build_status"] = "ready"
                store(saved)
                return
            time.sleep(5)
        raise TimeoutError("Template build did not reach ready")
    except Exception as exc:
        raise RuntimeError(
            safe_error(exc, {k: v or "" for k, v in secrets.items()})
        ) from None


def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("kind", choices=["browser", "analysis"])
    parser.add_argument("--use-docker-login", action="store_true", help="Reuse the current Docker login for the analysis image push")
    args = parser.parse_args(argv)
    try:
        if args.kind == "browser":
            browser()
        else:
            analysis(use_docker_login=args.use_docker_login)
    except RuntimeError as exc:
        parser.exit(1, "ERROR: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
