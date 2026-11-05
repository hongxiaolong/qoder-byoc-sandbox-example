# SPDX-License-Identifier: Apache-2.0
"""Loopback-only observation UI; all QCA/cloud requests stay on the server."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit

from aiohttp import web, ClientSession, ClientTimeout

from .observation import WorkObserver, build_observation
from .live_session import ChatError
from .execution import execution_snapshot
from .tracing import enrich_observation, select_turn_api_trace
from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder, MAX_ENTRIES, project_requests
from qoder_byoc_sandbox_example.environments.self_hosted.tools import credential_values, redact_value

ASSETS = Path(__file__).with_name("web")
MAX_HISTORY_BYTES = 8 * 1024 * 1024
HISTORY_VERSION = 2  # v1 could contain Read text imported from an unverified raw log.


async def create_app(config, status_provider=None, live_session=None, *, work_observer=None):
    """Expose GET-only public views. This process never submits Qoder messages."""
    app = web.Application(client_max_size=16 * 1024)
    client = None
    last_execution = None
    cached_execution = None
    execution_at = 0.0
    execution_lock = asyncio.Lock()
    sync_lock = asyncio.Lock()
    snapshots = {}
    initial = await asyncio.to_thread(status_provider.snapshot) if status_provider else {}
    session_id = getattr(live_session, "session_id", None)
    run_id = initial.get("run_id")
    journal = getattr(live_session, "journal", None)
    history_root = Path(journal).parent if journal and session_id and run_id else None
    history_file = history_root / "execution-history.json" if history_root else None
    binding = {"session_id": session_id, "run_id": run_id}
    identity = {key: initial.get(key) for key in ("run_id", "analysis_sandbox_id", "browser_sandbox_id")}
    identity["qoder_session_url"] = "https://qoder.com/cloud/sessions/" + session_id if session_id else ""
    private_values = (*credential_values(), config.get("analysis_token", ""),
                      getattr(getattr(live_session, "client", None), "token", ""))
    if work_observer is None and status_provider and hasattr(status_provider, "client"):
        work_observer = WorkObserver(status_provider.client, initial.get("environment_id"),
                                     initial.get("session_id"))

    def public_api_trace(value, *, origin=None):
        value = value if isinstance(value, dict) else {}
        result = project_requests(value.get("requests", []), run_id=run_id,
                                  session_id=session_id, secrets=private_values) if run_id else dict(requests=[], history_incomplete=False)
        result["history_incomplete"] |= bool(value.get("history_incomplete"))
        sources = value.get("sources", {}) if isinstance(value.get("sources"), dict) else {}
        result["sources"] = {item["id"]: origin or sources[item["id"]] for item in result["requests"]
                             if origin in {"control", "worker"} or sources.get(item["id"]) in {"control", "worker"}}
        return result

    def merged_api_trace(executed):
        local = (APITraceRecorder(history_root / "api-trace.jsonl", run_id,
                    session_id=session_id, secrets=private_values).snapshot()
                 if history_root and run_id else dict(requests=[], history_incomplete=False))
        remote = public_api_trace(executed.get("api_trace"), origin="worker")
        # Frequent Worker polls must not push recorded setup/mutation evidence
        # out of the combined window while it still exists in the control log.
        local_ids = {item["id"] for item in local["requests"]}
        remote_records = [item for item in remote["requests"] if item["id"] not in local_ids]
        available = max(0, MAX_ENTRIES - len(local["requests"]))
        retained = remote_records[-available:] if available else []
        records = [*retained, *local["requests"]]
        sources = dict(remote["sources"], **{item["id"]: "control" for item in local["requests"]})
        merged = public_api_trace(dict(requests=sorted(records, key=lambda r: r.get("started_at") or 0), sources=sources,
                                      history_incomplete=local["history_incomplete"] or remote["history_incomplete"]
                                      or len(retained) < len(remote_records)))
        return merged

    def reply(data, code=200):
        return web.json_response(data, status=code, headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def status():
        if status_provider:
            return await asyncio.to_thread(status_provider.snapshot)
        return dict(state="unavailable", message="请通过项目 preview 命令启动观察页。", phases={})

    def bound(data):
        source = data.get("identity", {})
        return (isinstance(source, dict) and source.get("run_id") == run_id
                and str(source.get("qoder_session_url", "")).rsplit("/", 1)[-1] == session_id)

    def archive(data):
        """Persist only the public projection, with a bounded, private atomic file."""
        if history_file is None or not bound(data):
            return
        value = {key: data[key] for key in ("identity", "events", "runtime", "recorded", "truncated", "history_incomplete", "observed_at", "api_trace") if key in data}
        value = redact_value(value, private_values)
        value["events"] = value.get("events", [])[-300:]
        value["runtime"] = value.get("runtime", [])[-200:]
        value["api_trace"] = public_api_trace(value.get("api_trace"))
        payload = dict(version=HISTORY_VERSION, **binding, execution=value)
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        while len(encoded) > MAX_HISTORY_BYTES and (value["events"] or value["runtime"] or value["api_trace"]["requests"]):
            value["truncated"] = True
            if value["events"]:
                value["events"].pop(0)
            elif value["runtime"]:
                value["runtime"].pop(0)
            else:
                value["api_trace"]["requests"].pop(0)
                value["api_trace"]["history_incomplete"] = True
            encoded = json.dumps(payload, ensure_ascii=False).encode()
        if len(encoded) > MAX_HISTORY_BYTES:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=history_root, prefix=".execution-history-", delete=False) as stream:
                temporary = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(history_file)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def stored_execution():
        if history_file is not None:
            try:
                if history_file.stat().st_size > MAX_HISTORY_BYTES:
                    raise ValueError("History exceeds limit")
                payload = json.loads(history_file.read_bytes())
                data = payload.get("execution")
                if (payload.get("version") != HISTORY_VERSION or any(payload.get(key) != value for key, value in binding.items())
                        or not isinstance(data, dict) or not bound(data) or not isinstance(data.get("events"), list)):
                    raise ValueError("History identity mismatch")
                data["api_trace"] = public_api_trace(data.get("api_trace"))
                return dict(redact_value(data, private_values), history_source="archive")
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if history_root is not None:
            log = history_root / "worker.jsonl"
            if log.is_file():
                data = execution_snapshot(history_root, log, identity=identity, allow_read_content=False)
                if data["events"] or data["runtime"] or data.get("api_trace", {}).get("requests"):
                    data.update(observed_at=None, history_source="worker_log")
                    try:
                        archive(data)
                    except OSError:
                        pass
                    return data
        return None

    async def load_execution():
        nonlocal last_execution
        try:
            async with client.get(config["analysis_url"] + "/api/execution",
                                  headers={"e2b-traffic-access-token": config["analysis_token"]},
                                  allow_redirects=False, timeout=ClientTimeout(total=4)) as response:
                if response.status != 200:
                    raise ValueError("Execution observation unavailable")
                data = await response.json()
                if (not isinstance(data, dict) or not isinstance(data.get("events"), list)
                        or not isinstance(data.get("runtime", []), list)):
                    raise ValueError("Invalid execution response")
            if history_root is not None and not bound(data):
                raise ValueError("Execution identity mismatch")
            data["api_trace"] = public_api_trace(data.get("api_trace"), origin="worker")
            last_execution = dict(redact_value(data, private_values), observed_at=time.time(), stale=False, error=None)
            try:
                await asyncio.to_thread(archive, last_execution)
            except OSError:
                pass  # A local archive failure must not hide a live observation.
        except Exception:
            if last_execution is None:
                last_execution = await asyncio.to_thread(stored_execution)
            return dict(last_execution or {"events": [], "runtime": [], "identity": {}},
                        stale=True, error="Sandbox 执行记录暂不可用，保留最后已观察内容。")
        return last_execution

    async def execution():
        nonlocal execution_at, cached_execution
        async with execution_lock:
            if cached_execution is not None and time.monotonic() - execution_at < 1:
                return cached_execution
            cached_execution = await load_execution()
            execution_at = time.monotonic()
            return cached_execution

    async def snapshot(message_id=None):
        async with sync_lock:
            previous = snapshots.get(message_id)
            if previous and time.monotonic() - previous[0] < 1:
                return previous[1]
            if message_id is not None and live_session is None:
                raise ChatError("历史观察尚未连接 Qoder 会话。", 503)
            current, session, executed = await asyncio.gather(
                status(),
                asyncio.to_thread(live_session.snapshot, message_id=message_id) if live_session else asyncio.sleep(
                    0, result=dict(turns=[], tools=[], trace=[], stale=True, error="未连接 Qoder 会话。")),
                execution(),
            )
            current["sandbox_region"] = config.get("sandbox_region")
            guided = {p.get("message_id") for p in current.get("phases", {}).values() if p.get("message_id")}
            for turn in session.get("turns", []):
                if turn["id"] in guided:
                    turn["source"] = "start.sh"
            runtime = executed.get("runtime", [])
            work_id = next((e["work_id"] for e in reversed(runtime) if e.get("work_id")), None)
            queue = (await asyncio.to_thread(work_observer.snapshot, work_id)
                     if work_observer and current.get("state") == "ready" else
                     dict(state="unavailable", stats=None, work=None, stale=True,
                          observed_at=None, error="队列尚未连接或本轮已结束。"))
            api_trace = merged_api_trace(executed)
            if message_id is not None:
                # Match recorded tool IDs; never infer Work or execution timestamps.
                tool_ids = {tool["id"] for tool in session.get("tools", [])}
                tool_ids.update(event["id"] for event in session.get("trace", [])
                                if event.get("type") in {"agent.tool_use", "agent.custom_tool_use"})
                api_trace = select_turn_api_trace(api_trace, message_id, tool_ids)
                selected_execution = dict(executed, api_trace=api_trace,
                    events=[event for event in executed.get("events", [])
                            if event.get("message_id") == message_id or event.get("id") in tool_ids],
                    runtime=[event for event in runtime
                             if event.get("message_id") == message_id or event.get("tool_use_id") in tool_ids])
                denied = {tool["id"] for tool in session.get("tools", [])
                          if tool.get("state") == "denied" or tool.get("permission") == "deny"}
                denied.update(event["id"] for event in session.get("trace", [])
                              if event.get("type") in {"agent.tool_use", "agent.custom_tool_use"}
                              and event.get("permission") == "deny")
                recorded = {event.get("id") for event in selected_execution["events"]}
                if tool_ids - denied - recorded:
                    selected_execution["history_incomplete"] = True
                # Replay's lifecycle rows are scoped, while the resource health is
                # still the real current observation, independent of older input.
                health = build_observation(current, {}, dict(executed, events=[]), queue, max_events=1)["worker"]
                cached = build_observation(current, session, selected_execution, queue, max_events=5000)
                cached["events"] = [event for event in cached["events"] if event.get("message_id") == message_id
                                    or event.get("tool_id") in tool_ids]
                cached["worker"] = health
            else:
                cached = build_observation(current, session, executed, queue)
            cached = enrich_observation(cached, api_trace)
            if live_session:
                cached["interactions"] = await asyncio.to_thread(live_session.project_interactions, cached)
            cached.pop("_flow_events", None)
            cached["message_id"] = message_id
            if current.get("state") != "ready":
                cached["worker"]["state"] = "unknown"
            snapshots[message_id] = (time.monotonic(), cached)
            if len(snapshots) > 16:
                oldest = min(snapshots, key=lambda key:snapshots[key][0])
                del snapshots[oldest]
            return cached

    async def remote(request):
        if urlsplit("http://" + request.host).hostname not in {"127.0.0.1", "localhost", "::1"}:
            return reply({"error": "This preview only accepts loopback hosts"}, 403)
        if request.path == "/api/observer":
            identifiers = request.query.getall("message_id", [])
            if len(identifiers) > 1 or (identifiers and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", identifiers[0])):
                return reply({"error": "消息编号无效。"}, 400)
            try:
                return reply(await snapshot(identifiers[0] if identifiers else None))
            except ChatError as exc:
                return reply({"error": str(exc)}, exc.status)
        if request.path == "/api/demo-status":
            return reply(await status())
        if request.path == "/api/identity":
            current = await status()
            if current.get("state") != "ready":
                return reply({"error": "此预览未连接当前运行，请重新启动。"}, 409)
            return reply({key: current.get(key) for key in
                          ("run_id", "analysis_sandbox_id", "browser_sandbox_id")})
        if request.path == "/api/session":
            return reply(await asyncio.to_thread(live_session.snapshot) if live_session else
                         dict(turns=[], tools=[], trace=[], stale=True, error="未连接会话。"))
        if request.path == "/api/execution":
            return reply(await execution())
        local = ("portal.html" if request.path == "/" else
                 request.path.removeprefix("/assets/") if request.path.startswith("/assets/") else None)
        allowed = {"portal.html", "live.css", "live.js", "qoder.svg", "agent-sandbox.png"}
        if local and (local in allowed or (local.startswith("icons/") and local.endswith(".svg"))) and ".." not in Path(local).parts:
            asset = ASSETS / local
            mime = {".html": "text/html", ".css": "text/css", ".js": "text/javascript",
                    ".svg": "image/svg+xml", ".png": "image/png"}
            if asset.resolve().is_relative_to(ASSETS.resolve()) and asset.is_file() and asset.suffix in mime:
                return web.Response(body=asset.read_bytes(), content_type=mime[asset.suffix],
                                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return reply({"error": "观察页只提供事件与运行状态；请在终端或 Qoder 控制台操作。"}, 404)

    async def resources(_app):
        nonlocal client
        client = ClientSession(timeout=ClientTimeout(total=5))
        try:
            yield
        finally:
            await client.close()

    app.cleanup_ctx.append(resources)
    app.router.add_get("/{tail:.*}", remote)
    return app


async def serve(config, port, status_provider=None, live_session=None):
    app = await create_app(config, status_provider, live_session)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    print(f"Observer ready: http://127.0.0.1:{port}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    asyncio.run(serve(json.loads(Path(args.config).read_text()), args.port))
