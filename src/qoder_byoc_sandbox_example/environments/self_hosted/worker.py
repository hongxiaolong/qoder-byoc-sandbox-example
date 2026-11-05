# SPDX-License-Identifier: Apache-2.0
"""Qoder Self-hosted Worker；此程序运行在阿里云 Sandbox 内。

演示目标：将 Qoder 的云端编排接入阿里云执行环境，执行真实文件与命令工具。
执行流程：领取指定 Session 的工作、确认、维持租约；读取事件，执行获准的工具，
    将结果按 tool_use_id 回传。结果先写入本地日志，回传重试不重跑工具。
能力体现：Self-hosted、Agent Use、技能文件读取和同一工作目录连续执行。
    本 Worker 是演示实现，不提供进程崩溃后的任意 Bash 恰好一次执行保障。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time

from qoder_byoc_sandbox_example.qoder.client import API, APIError
from .tools import Tools, credential_values, redact_value


class Worker:
    def __init__(
        self, api, environment_id, session_id, worker_id, workdir, state_dir, tools=None
    ):
        self.api = api
        self.environment_id = environment_id
        self.session_id = session_id
        self.worker_id = worker_id
        self.tools = tools if tools is not None else Tools(workdir)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stopping = threading.Event()
        self.private_values = (*credential_values(), getattr(api, "token", ""))
        self.work_id = None
        self.cursor = None
        self.calls = {}
        self.answered = set()
        self.confirmed = {}
        self.pending = set()
        self.interrupted = set()
        self.lease_error = None
        self.lease_ready = threading.Event()
        self.log_lock = threading.Lock()
        self.message_id = None
        self.call_messages = {}
        self.permission_logged = set()
        self.active_tool_id = None
        self.bind_tools()

    def bind_tools(self):
        if hasattr(self.tools, "cancel_event"):
            self.tools.cancel_event = self.stopping
        if hasattr(self.tools, "observe"):
            self.tools.observe = self.tool_stage

    def tool_stage(self, stage, **values):
        if hasattr(self.tools, "check_cancelled"):
            self.tools.check_cancelled()
        self.log("tool_stage", tool_use_id=self.active_tool_id, stage=stage, **values)

    def log(self, kind, **values):
        record = {
            "time": time.time(),
            "kind": kind,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "work_id": self.work_id,
            "environment_id": self.environment_id,
            "role": "analysis",
            **values,
        }
        # Never serialize work.secret, HTTP headers, or Worker environment.
        line = json.dumps(redact_value(record, self.private_values), ensure_ascii=False)
        with self.log_lock:
            print(line, flush=True)
            with (self.state_dir / "worker.jsonl").open("a") as output:
                output.write(line + "\n")

    def work_path(self, suffix=""):
        return f"/environments/{self.environment_id}/work/{self.work_id}{suffix}"

    def heartbeats(self):
        previous = "NO_HEARTBEAT"
        while not self.stopping.is_set():
            try:
                result = self.api.request(
                    "POST",
                    self.work_path("/heartbeat"),
                    query={
                        "expected_last_heartbeat": previous,
                        "desired_ttl_seconds": 60,
                    },
                )
                if result.get("state") in {"stopping", "stopped"}:
                    self.stopping.set()
                    self.log("heartbeat_stopped", work_id=self.work_id, state=result.get("state"))
                    return
                if not result.get("lease_extended"):
                    raise RuntimeError("Work lease was not extended")
                previous = result["last_heartbeat"]
                self.lease_ready.set()
                self.log("heartbeat_confirmed", work_id=self.work_id,
                         state=result.get("state"), ttl_seconds=result.get("ttl_seconds"), last_heartbeat=previous)
            except Exception as exc:
                self.lease_error = str(exc)
                self.stopping.set()
                self.log("heartbeat_failed", work_id=self.work_id)
                return
            self.stopping.wait(10)

    def reconcile(self):
        for event in self.api.events(self.session_id, self.cursor):
            event_id, kind = event["id"], event["type"]
            if kind == "user.message":
                self.message_id = event_id
            elif kind in {"agent.tool_use", "agent.custom_tool_use"}:
                message_id = event.get("message_id") or self.message_id
                if event_id not in self.calls:
                    self.log("tool_observed", tool_use_id=event_id, tool=event["name"],
                             message_id=message_id)
                self.call_messages[event_id] = message_id
                self.calls[event_id] = event
            elif kind in {
                "user.tool_result",
                "agent.tool_result",
                "user.custom_tool_result",
            }:
                self.answered.add(
                    event.get("tool_use_id") or event.get("custom_tool_use_id")
                )
            elif kind == "user.tool_confirmation":
                self.confirmed[event["tool_use_id"]] = event.get("result")
            elif kind == "user.interrupt":
                self.interrupted.update(self.calls)
                self.pending.clear()
            elif kind == "session.status_idle":
                stop = event.get("stop_reason") or {}
                if stop.get("type") == "requires_action":
                    self.pending = set(stop.get("event_ids", []))
            elif kind in {"session.status_terminated", "session.deleted"}:
                self.stopping.set()
            self.cursor = event_id

    def dispatch(self):
        for tool_id, event in list(self.calls.items()):
            if self.stopping.is_set():
                return
            if tool_id in self.answered or tool_id in self.interrupted:
                continue
            if tool_id not in self.pending:
                continue
            custom = event["type"] == "agent.custom_tool_use"
            permission = "allow" if custom else event.get("evaluated_permission")
            allowed = permission == "allow" or (
                permission == "ask" and self.confirmed.get(tool_id) == "allow"
            )
            if not allowed:
                if permission == "deny" or self.confirmed.get(tool_id) == "deny":
                    self.answered.add(tool_id)
                elif tool_id not in self.permission_logged:
                    self.log("permission_wait", tool_use_id=tool_id, tool=event["name"])
                    self.permission_logged.add(tool_id)
                continue
            cached = self.state_dir / (
                hashlib.sha256(tool_id.encode()).hexdigest() + ".json"
            )
            if cached.exists():
                result = json.loads(cached.read_text())
            else:
                started = time.monotonic()
                self.log(
                    "tool_start",
                    tool_use_id=tool_id,
                    tool=event["name"],
                    input=event["input"],
                    message_id=self.call_messages.get(tool_id),
                )
                self.active_tool_id = tool_id
                try:
                    text, error = self.tools.execute(event["name"], event["input"])
                finally:
                    self.active_tool_id = None
                result = {
                    "type": "user.custom_tool_result" if custom else "user.tool_result",
                    "custom_tool_use_id" if custom else "tool_use_id": tool_id,
                    "is_error": error,
                    "content": [{"type": "text", "text": text}],
                }
                tmp = cached.with_suffix(".tmp")
                tmp.write_text(json.dumps(result))
                tmp.replace(cached)
                self.log(
                    "tool_finished",
                    tool_use_id=tool_id,
                    tool=event["name"],
                    is_error=error,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                    output=text,
                )
            # Retry delivery, never execution. Stable idempotency key for one tool call.
            for attempt in range(3):
                if self.stopping.is_set() or self.lease_error:
                    self.log("tool_cancelled", tool_use_id=tool_id, tool=event["name"],
                             message_id=self.call_messages.get(tool_id),
                             detail="Worker stopped; cached result was not posted")
                    return
                try:
                    self.api.send(self.session_id, [result], f"tool-{tool_id}")
                    break
                except APIError as exc:
                    if exc.status not in {408, 409, 429, 500, 502, 503, 504}:
                        raise
                    if attempt == 2:
                        raise
                    self.stopping.wait(0.5 * (attempt + 1))
                except RuntimeError:
                    if attempt == 2:
                        raise
                    self.stopping.wait(0.5 * (attempt + 1))
            self.answered.add(tool_id)
            self.log("tool_posted", tool_use_id=tool_id, tool=event["name"])

    def run(self, max_seconds=840):
        deadline = time.monotonic() + max_seconds
        (self.state_dir / "ready.json").write_text(
            json.dumps({"worker_id": self.worker_id, "session_id": self.session_id})
        )
        self.log("worker_ready", environment_id=self.environment_id)
        self.log("worker_polling", environment_id=self.environment_id)
        heartbeat = None
        try:
            while not self.stopping.is_set() and time.monotonic() < deadline:
                if self.work_id is None:
                    work = self.api.request(
                        "GET",
                        f"/environments/{self.environment_id}/work/poll",
                        query={"block_ms": 999},
                        headers={"Worker-ID": self.worker_id},
                    )
                    self.log("work_poll", result="empty" if work is None else "returned",
                             work_id=work.get("id") if work else None,
                             state=work.get("state") if work else None)
                    if work is None:
                        continue
                    if work.get("data", {}).get("id") != self.session_id:
                        raise RuntimeError(
                            "Dedicated Environment returned another Session; not acknowledged"
                        )
                    self.work_id = work["id"]
                    acknowledgment = self.api.request(
                        "POST",
                        self.work_path("/ack"),
                        headers={"Worker-ID": self.worker_id},
                    )
                    self.log("work_ack", work_id=self.work_id, state=(acknowledgment or {}).get("state"))
                    self.log("work_claimed", work_id=self.work_id, state=(acknowledgment or {}).get("state"))
                    heartbeat = threading.Thread(target=self.heartbeats, daemon=True)
                    heartbeat.start()
                    # Require the first successful heartbeat before executing any tools.
                    if not self.lease_ready.wait(20):
                        raise RuntimeError(
                            self.lease_error or "Work lease did not become active"
                        )
                self.reconcile()
                self.dispatch()
                self.stopping.wait(0.4)
            if self.lease_error:
                raise RuntimeError(self.lease_error)
        finally:
            self.stopping.set()
            if heartbeat:
                heartbeat.join(timeout=20)
            if self.work_id:
                self.api.request("POST", self.work_path("/stop"), {"force": True})
            self.log("worker_stopped", work_id=self.work_id)


def main(argv=None, *, prog=None):
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    token = os.environ.pop("QODER_ACCESS_TOKEN")
    client = API(config["base_url"], token)
    if config.get("run_id"):
        from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder
        client.observer = APITraceRecorder(Path(config["state_dir"]) / "api-trace.jsonl",
            config["run_id"], session_id=config["session_id"], secrets=(token,)).record
    worker = Worker(
        client,
        config["environment_id"],
        config["session_id"],
        config["worker_id"],
        config["workdir"],
        config["state_dir"],
    )
    if config.get("tool_provider"):
        import importlib

        module, factory = config["tool_provider"].split(":", 1)
        worker.tools = getattr(importlib.import_module(module), factory)(config)
        worker.bind_tools()
    signal.signal(signal.SIGTERM, lambda *_: worker.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: worker.stopping.set())
    try:
        worker.run(config.get("max_seconds", 840))
    except Exception as exc:
        worker.log("worker_error", error=str(exc).replace(token, "[REDACTED]"))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
