# SPDX-License-Identifier: Apache-2.0
"""交互式报表分析 Agent 的可选演示入口。

演示目标：用户在 Qoder Session 提交报表分析任务，观察 Agent 下载、计算与交付。
执行流程：装配 Agent、Skill、Self-hosted Environment 与双 Sandbox；提交或观察
    三轮任务，以真实文件核对统计结果；预览与验收不参与 Qoder 的 Agent Loop。
能力体现：Self-hosted 工具执行、多模板协作、同一会话连续追问与可下载分析产物。
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from qoder_byoc_sandbox_example.cli import COMMAND, DEMO_ACTIONS
from qoder_byoc_sandbox_example.settings import (
    ROOT,
    STATE_DIR,
    load,
    save,
    state_for,
    state_file,
    safe_error,
)
from qoder_byoc_sandbox_example.sandbox.client import (
    create_sandbox,
    NotFoundException,
    command,
)
from qoder_byoc_sandbox_example.sandbox.templates import source_digest
from qoder_byoc_sandbox_example.qoder import (
    agents,
    environments,
    sessions,
    skills,
)
from .shared import api, bind_trace, connect, emit, cleanup_sandbox
from .session import task, wait_turn
from .verification import verify
from .tool_specs import TOOLS
from .data import seed_csv, sha
from .lifecycle import record_created, begin_create, pending_creation_error, require_confirmed_creations


def setup(config, values, official_analysis=False):
    if state_file().exists():
        previous = json.loads(state_file().read_text())
        require_confirmed_creations(previous)
        if not previous.get("cleaned"):
            raise ValueError("存在未回收资源，请先 status 查看或 cleanup")
    templates_path = STATE_DIR / "templates.json"
    templates = (
        json.loads(templates_path.read_text()) if templates_path.exists() else {}
    )
    if not templates.get("browser_template"):
        raise ValueError("先运行 " + COMMAND + " templates browser")
    analysis = None if official_analysis else templates.get("analysis_template")
    if not analysis and not official_analysis:
        raise ValueError("先构建自定义分析模板；开发验证可显式加 --official-analysis")
    if analysis and templates.get("analysis_build_status") != "ready":
        raise ValueError("自定义分析模板尚未 ready")
    analysis = analysis or config["sandbox"]["analysis_template"]
    skill_dir = ROOT / config["agent"]["skill_dir"]
    skill_name = skill_dir.name
    client = api(config, values)
    client.request("GET", "/models")
    run_id = uuid.uuid4().hex[:12]
    state = {
        "run_id": run_id,
        "api_url": values["E2B_API_URL"],
        "qoder_url": config["qoder"]["base_url"],
        "analysis_template": analysis,
        "browser_template": templates["browser_template"],
        "analysis_mode": "official-development"
        if official_analysis
        else "custom_image",
        "created_at": time.time(),
        "source_digest": source_digest(),
        "workdir": "/workspace",
        "cleaned": False,
    }
    for key in ["browser_template_name", "browser_image", "analysis_template_name", "analysis_image"]:
        state[key] = templates.get(key) if not official_analysis or key.startswith("browser_") else None
    save(state)
    bind_trace(client, state, values)
    begin_create(state, "browser", save)
    browser_started_at, browser_started = time.time(), time.monotonic()
    browser = create_sandbox(
        templates["browser_template"],
        config["sandbox"]["timeout_seconds"],
        {"demo_run": run_id, "role": "browser"},
        values,
    )
    state["browser_sandbox_id"] = browser.sandbox_id
    record_created(state, "browser", browser.sandbox_id, sdk_create=dict(
        started_at=browser_started_at, finished_at=time.time(),
        elapsed_ms=round((time.monotonic() - browser_started) * 1000, 1)))
    save(state)
    begin_create(state, "analysis", save)
    started_at, started = time.time(), time.monotonic()
    analysis_box = create_sandbox(
        analysis,
        config["sandbox"]["timeout_seconds"],
        {"demo_run": run_id, "role": "analysis"},
        values,
        private=True,
    )
    state["analysis_sandbox_id"] = analysis_box.sandbox_id
    state["analysis_create_ms"] = round((time.monotonic() - started) * 1000, 1)
    record_created(state, "analysis", analysis_box.sandbox_id, sdk_create=dict(
        started_at=started_at, finished_at=time.time(), elapsed_ms=state["analysis_create_ms"]))
    save(state)
    if not analysis_box.traffic_access_token:
        raise RuntimeError(
            "Analysis Sandbox did not provide its required traffic access token"
        )
    emit(
        "sandboxes_created",
        **{
            k: state[k]
            for k in [
                "analysis_sandbox_id",
                "browser_sandbox_id",
                "analysis_mode",
                "analysis_create_ms",
            ]
        },
    )
    uid = command(analysis_box, "id -u").stdout.strip()
    gid = command(analysis_box, "id -g").stdout.strip()
    if not uid.isdigit() or not gid.isdigit():
        raise ValueError("Cannot identify Sandbox execution user")
    create = command(
        analysis_box,
        "mkdir -p /workspace/artifacts /workspace/evidence /workspace/skills/"
        + skill_name
        + " /opt/demo/src /tmp/demo-worker && chown -R "
        + uid
        + ":"
        + gid
        + " /workspace /opt/demo /tmp/demo-worker",
        user="root",
    )
    if create.exit_code:
        raise RuntimeError(create.stderr)
    runtime_files = [
        p
        for p in (ROOT / "src").rglob("*")
        if p.is_file()
        and (p.suffix in {".py", ".html", ".css", ".js", ".svg", ".png"} or p.name == "NOTICE.md")
        and "__pycache__" not in p.parts
    ]
    if state["analysis_mode"] != "custom_image":
        for folder in sorted({p.parent for p in runtime_files}):
            analysis_box.files.make_dir("/opt/demo/" + str(folder.relative_to(ROOT)))
        for p in runtime_files:
            analysis_box.files.write(
                "/opt/demo/" + str(p.relative_to(ROOT)), p.read_bytes()
            )
    else:
        for p in runtime_files:
            name = str(p.relative_to(ROOT))
            if (
                analysis_box.files.read("/opt/demo/" + name, format="bytes")
                != p.read_bytes()
            ):
                raise ValueError(
                    "Custom image source differs: "
                    + name
                    + "; rebuild the image/template"
                )
        state["runtime_source_verified"] = True
        save(state)
    if state["analysis_mode"] != "custom_image":
        install = command(
            analysis_box,
            "python3 -m pip install -q e2b==2.35.0 playwright==1.55.0 pandas==2.3.3 plotly==6.3.1 openpyxl==3.1.5",
            timeout=300,
        )
        if install.exit_code:
            raise RuntimeError(
                "Development dependency install failed: " + install.stderr[-1500:]
            )
    source = seed_csv()
    analysis_box.files.write("/workspace/source.csv", source)
    state["source_sha256"] = sha(source)
    # Custom templates already have the service running. Development starts it explicitly.
    health = command(analysis_box, "curl -fsS http://127.0.0.1:8000/health", timeout=5)
    if health.exit_code:
        if state["analysis_mode"] == "custom_image":
            raise RuntimeError(
                "Custom template service was not ready on creation; inspect template start/ready commands"
            )
        analysis_box.commands.run(
            "exec python3 -u -m qoder_byoc_sandbox_example.demos.report_analyst.service --root /workspace --port 8000 > /tmp/demo-service.log 2>&1",
            cwd="/opt/demo",
            background=True,
            timeout=0,
            envs={"PYTHONPATH": "/opt/demo/src", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    elif state["analysis_mode"] == "custom_image":
        state["service_ready_without_setup"] = True
        save(state)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if (
            command(
                analysis_box, "curl -fsS http://127.0.0.1:8000/health", timeout=5
            ).exit_code
            == 0
        ):
            break
        time.sleep(0.3)
    else:
        raise TimeoutError("Analysis service not ready")
    return finish_setup(config, values, state, analysis_box, browser)


def finish_setup(config, values, state, analysis_box, browser):
    require_confirmed_creations(state)
    if any(
        state.get(key)
        for key in ("skill_id", "agent_id", "environment_id", "session_id")
    ):
        raise ValueError(
            "已记录 Qoder 资源，请先运行 "
            + COMMAND
            + " cleanup，再运行 "
            + COMMAND
            + " setup，避免覆盖已有资源记录"
        )
    run_id = state["run_id"]
    skill_name = Path(config["agent"]["skill_dir"]).name
    client = bind_trace(api(config, values), state, values)
    browser_host = browser.get_host(3000)
    browser_token = browser._envd_access_token
    health = command(browser, "curl -fsS http://127.0.0.1:3000/health", timeout=15)
    if health.exit_code:
        raise RuntimeError("Browser service not ready: " + health.stderr[-1000:])
    state["analysis_url"] = "https://" + analysis_box.get_host(8000)
    import httpx

    with httpx.Client(timeout=20, follow_redirects=False) as probe:
        anonymous = probe.get(state["analysis_url"] + "/health")
        authenticated = probe.get(
            state["analysis_url"] + "/health",
            headers={"e2b-traffic-access-token": analysis_box.traffic_access_token},
        )
    if anonymous.status_code not in {401, 403} or authenticated.status_code != 200:
        raise RuntimeError(
            "Analysis ingress check failed: anonymous="
            + str(anonymous.status_code)
            + ", authenticated="
            + str(authenticated.status_code)
        )
    state["ingress_status"] = {
        "anonymous": anonymous.status_code,
        "authenticated": authenticated.status_code,
    }
    state["cdp_url"] = "wss://" + browser_host + "/ws/automation"
    save(state)
    skill_dir = ROOT / config["agent"]["skill_dir"]
    skill_name = skill_dir.name
    state["skill_path"] = "/workspace/skills/" + skill_name + "/SKILL.md"

    def skill_created(item):
        state.update(skill_id=item["id"], skill_version=item["latest_version"])
        record_created(state, "skill", item["id"], item)
        save(state)

    begin_create(state, "skill", save)
    skill, downloaded = skills.upload(
        client, run_id, skill_name, skill_dir / "SKILL.md", on_created=skill_created
    )
    analysis_box.files.write(state["skill_path"], downloaded)
    begin_create(state, "agent", save)
    agent = agents.create(
        client, config["agent"], TOOLS, skill, state["skill_path"], run_id
    )
    state["agent_id"] = agent["id"]
    state["agent_version"] = agent["version"]
    record_created(state, "agent", agent["id"], agent)
    save(state)
    begin_create(state, "environment", save)
    env = environments.create(
        client,
        config["project"]["name"] + "-" + run_id,
        run_id,
        config["environment"],
    )
    state["environment_id"] = env["id"]
    record_created(state, "environment", env["id"], env)
    save(state)
    begin_create(state, "session", save)
    session = sessions.create(client, agent, env, run_id, "报表分析 Agent " + run_id)
    state["session_id"] = session["id"]
    record_created(state, "session", session["id"], session)
    state["session_url"] = "https://qoder.com/cloud/sessions/" + session["id"]
    save(state)
    identity = {
        k: state[k]
        for k in [
            "run_id",
            "analysis_sandbox_id",
            "browser_sandbox_id",
            "analysis_template",
            "browser_template",
            "analysis_mode",
        ]
    }
    identity["qoder_session_url"] = state["session_url"]
    for key in ["browser_template_name", "browser_image", "analysis_template_name", "analysis_image"]:
        identity[key] = state.get(key)
    analysis_box.files.write(
        "/workspace/identity.json", json.dumps(identity, ensure_ascii=False)
    )
    worker_config = {
        "run_id": run_id,
        "base_url": state["qoder_url"],
        "environment_id": env["id"],
        "session_id": session["id"],
        "worker_id": config["project"]["name"] + "-" + run_id,
        "workdir": "/workspace",
        "state_dir": "/tmp/demo-worker",
        "max_seconds": config["sandbox"]["timeout_seconds"] - 60,
        "tool_provider": config["environment"]["tool_provider"],
        "browser": {
            "sandbox_id": browser.sandbox_id,
            "api_url": values["E2B_API_URL"],
            "domain": values["E2B_DOMAIN"],
            "cdp_url": state["cdp_url"],
            "source_url": state["analysis_url"] + "/source",
        },
    }
    analysis_box.files.write("/tmp/worker-config.json", json.dumps(worker_config))
    handle = analysis_box.commands.run(
        "exec python3 -u -m qoder_byoc_sandbox_example.environments.self_hosted.worker --config /tmp/worker-config.json > /tmp/demo-worker/console.log 2>&1",
        cwd="/opt/demo",
        background=True,
        timeout=0,
        envs={
            "QODER_ACCESS_TOKEN": values["QODER_ACCESS_TOKEN"],
            "E2B_API_KEY": values["E2B_API_KEY"],
            "BROWSER_ACCESS_TOKEN": browser_token,
            "ANALYSIS_TRAFFIC_TOKEN": analysis_box.traffic_access_token or "",
            "PYTHONPATH": "/opt/demo/src",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    state["worker_pid"] = handle.pid
    save(state)
    gateway = {
        "analysis_url": state["analysis_url"],
        "analysis_token": analysis_box.traffic_access_token or "",
        "cdp_url": state["cdp_url"],
        "browser_token": browser_token,
    }
    path = STATE_DIR / "gateway.json"
    path.write_text(json.dumps(gateway))
    path.chmod(0o600)
    time_limit = time.monotonic() + 25
    while time.monotonic() < time_limit:
        if (
            command(
                analysis_box, "test -f /tmp/demo-worker/ready.json", timeout=5
            ).exit_code
            == 0
        ):
            break
        time.sleep(0.3)
    else:
        raise RuntimeError("Worker failed to start")
    emit(
        "setup_ready",
        session_url=state["session_url"],
        preview_url=f"http://127.0.0.1:{config['demo']['preview_port']}",
        analysis_sandbox_id=analysis_box.sandbox_id,
        browser_sandbox_id=browser.sandbox_id,
    )
    return state


def cleanup(config, values, state):
    pending_error = pending_creation_error(state)
    if pending_error:
        state["cleaned"] = False
        save(state)
    client = bind_trace(api(config, values), state, values)
    errors = sessions.cleanup_owned(client, state, save)
    for key in ["analysis_sandbox_id", "browser_sandbox_id"]:
        if state.get(key) and not state.get(key + "_killed"):
            try:
                cleanup_sandbox(state, values, key)
                state[key + "_killed"] = True
                save(state)
            except NotFoundException:
                state[key + "_killed"] = True
                save(state)
            except Exception as exc:
                errors.append(str(exc))
    if pending_error:
        errors.append("已按归属处理已知资源；" + pending_error)
    if errors:
        raise RuntimeError("清理未完成：" + " | ".join(errors))
    state["cleaned"] = True
    save(state)
    emit("cleanup_pass")


def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument(
        "action",
        choices=list(DEMO_ACTIONS),
    )
    parser.add_argument(
        "--phase", choices=["collect", "revise", "append"], default="collect"
    )
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--message", help="chat：发送一条自由对话消息后等待回复")
    parser.add_argument("--retry", action="store_true", help="chat：沿用本机待确认消息编号重试，不重复生成消息")
    parser.add_argument(
        "--official-analysis",
        action="store_true",
        help="仅用于开发验证；不代表自定义镜像已验证",
    )
    args = parser.parse_args(argv)
    if args.retry and (args.message is not None or args.action != "chat"):
        parser.error("--retry 仅用于 chat，且不能与 --message 同时使用")
    values = {}
    try:
        config, values = load()
        if args.action == "doctor":
            models = api(config, values).request("GET", "/models")
            emit("doctor_pass", qoder_models=len(models.get("data", [])))
            return
        if args.action == "setup":
            setup(config, values, args.official_analysis)
            return
        if args.action == "resume-setup":
            current = state_for(config, values)
            require_confirmed_creations(current)
            finish_setup(
                config,
                values,
                current,
                connect(current, values),
                connect(current, values, "browser_sandbox_id"),
            )
            return
        if args.action == "all":
            previous_run_id = (
                json.loads(state_file().read_text()).get("run_id")
                if state_file().exists() else None
            )
            state = None
            original_error = None
            try:
                state = setup(config, values, args.official_analysis)
                for phase in ["collect", "revise", "append"]:
                    task(config, values, state, phase)
            except BaseException as exc:
                original_error = exc
                raise
            finally:
                if not args.keep:
                    try:
                        # setup saves each created resource before continuing;
                        # an exception can occur before it returns that state.
                        current = state
                        if current is None and state_file().exists():
                            current = state_for(config, values)
                        if (current and current.get("run_id")
                                and current["run_id"] != previous_run_id
                                and not current.get("cleaned")):
                            cleanup(config, values, current)
                    except BaseException as cleanup_error:
                        if original_error is None:
                            raise
                        original_error.add_note("本轮自动回收未完成，运行账本保留。")
                        emit("cleanup_failed", error=safe_error(cleanup_error, values))
            return
        state = state_for(config, values)
        if args.action == "preview":
            from .preview import serve
            from .demo_status import DemoStatus
            from .conversation import observer
            from qoder_byoc_sandbox_example.qoder.client import API
            import asyncio

            asyncio.run(
                serve(
                    {**json.loads((STATE_DIR / "gateway.json").read_text()),
                     "sandbox_region": values["E2B_DOMAIN"].split(".")[0]},
                    config["demo"]["preview_port"],
                    DemoStatus(
                        state_file(), state["analysis_url"],
                        API(config["qoder"]["base_url"], values["QODER_ACCESS_TOKEN"], timeout=3),
                        config["scenarios"], config["sandbox"]["timeout_seconds"],
                    ),
                    observer(config, values, state),
                )
            )
        elif args.action == "chat":
            from .conversation import chat
            chat(config, values, state, args.message, retry=args.retry)
        elif args.action in ["task", "observe"]:
            task(config, values, state, args.phase, args.action == "observe")
        elif args.action == "resume":
            wait_turn(config, values, state, state["active_phase"])
            verify(config, values, state, state["active_phase"])
        elif args.action == "verify":
            verify(config, values, state, args.phase)
        elif args.action == "cleanup":
            cleanup(config, values, state)
        elif args.action == "status":
            emit("state", **state)
        elif args.action == "logs":
            print(connect(state, values).files.read("/tmp/demo-worker/console.log"))
    except (Exception, KeyboardInterrupt) as exc:
        print("ERROR: " + safe_error(exc, values), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
