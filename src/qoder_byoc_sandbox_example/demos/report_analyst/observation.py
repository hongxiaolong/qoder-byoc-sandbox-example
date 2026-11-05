"""Read-only QCA Work observation and a bounded public execution timeline.

WorkObserver.snapshot returns environment_id, state (ready/unavailable), stats, work,
observed_at (last successful read), stale and a generic error. Missing counters
remain absent. workers_polling counts distinct Worker IDs polling in the latest
30 seconds, never online Workers.

build_observation accepts already-public status/session/execution/queue mappings.
session.trace contains public QCA events; execution.events contains local tool
spans; execution.runtime contains Worker lifecycle events. The output preserves
these sources and adds events (at most 600), worker health, and history_incomplete.
Normalized events retain the QCA type/Worker kind; local spans use tool_execution.
work_id is copied only from the recorded source, never from the current queue.
Their time is a real source timestamp or None, observed_at is observation time.
elapsed_ms is present only for a matched model span or a complete local span.
execution.stale and execution.observed_at describe transport freshness; neither
queue counters nor a historical Worker PID establish Worker health.
"""

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
import threading
import time
from urllib.parse import quote

from .execution import public_text
from .lifecycle import RESOURCES, public_history


def _id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", value) else None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _time(value):
    if _number(value):
        return value / 1000 if value > 100000000000 else value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # An unzoned string cannot establish a cloud timestamp reliably.
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except (ValueError, OverflowError):
            pass
    return None


class WorkObserver:
    """A small, cached reader scoped to one QCA Environment and Session."""

    def __init__(self, client, environment_id, session_id, *, clock=time.time, cache_seconds=5):
        self.client = client
        self.environment_id = _id(environment_id)
        self.session_id = _id(session_id)
        self.clock = clock
        self.cache_seconds = max(0, min(float(cache_seconds), 60))
        self._cache = {}
        self._last_success = None
        self._lock = threading.Lock()

    def snapshot(self, work_id=None):
        now = self.clock()
        valid_work = _id(work_id) if work_id is not None else None
        invalid = not self.environment_id or not self.session_id or (work_id is not None and not valid_work)
        unknown = dict(environment_id=self.environment_id, state="unavailable", stats=None, work=None,
                       observed_at=None, stale=True, error="QCA Work 状态暂不可用。",
                       workers_polling_meaning="最近 30 秒发起过轮询的不同 Worker ID 数，不表示在线 Worker 总数。")
        if invalid:
            return unknown
        with self._lock:
            previous = self._cache.get(valid_work)
            if previous and 0 <= now - previous[0] < self.cache_seconds:
                return deepcopy(previous[1])
            try:
                base = "/environments/" + quote(self.environment_id, safe="") + "/work"
                stats = self.client.request("GET", base + "/stats")
                allowed = {"depth", "pending", "workers_polling"}
                if not isinstance(stats, dict) or stats.get("type") != "work_queue_stats" or not allowed <= stats.keys():
                    raise ValueError("Invalid Work statistics")
                filtered = {"type":"work_queue_stats"}
                for key in allowed:
                    value = stats[key]
                    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2**53 - 1:
                        raise ValueError("Invalid Work count")
                    filtered[key] = value
                if "oldest_queued_at" in stats:
                    if stats["oldest_queued_at"] is not None and (not isinstance(stats["oldest_queued_at"], str) or _time(stats["oldest_queued_at"]) is None):
                        raise ValueError("Invalid queue timestamp")
                    filtered["oldest_queued_at"] = stats["oldest_queued_at"]
                work = None
                if valid_work:
                    response = self.client.request("GET", base + "/" + quote(valid_work, safe=""))
                    if not isinstance(response, dict):
                        raise ValueError("Invalid Work response")
                    data = response.get("data")
                    if (response.get("id") != valid_work or response.get("type") != "work" or response.get("environment_id") != self.environment_id
                            or not isinstance(data, dict) or data.get("type") != "session" or data.get("id") != self.session_id):
                        raise ValueError("Work ownership mismatch")
                    if response.get("state") not in {"queued", "starting", "active", "stopping", "stopped"}:
                        raise ValueError("Invalid Work state")
                    work = {key: response[key] for key in ("id", "type", "environment_id", "state")}
                    work["data"] = {"id": self.session_id, "type": "session"}
                    for key in ("created_at", "acknowledged_at", "started_at", "latest_heartbeat_at", "stop_requested_at", "stopped_at"):
                        if key in response:
                            if response[key] is not None and (not isinstance(response[key], str) or _time(response[key]) is None):
                                raise ValueError("Invalid Work timestamp")
                            work[key] = response[key]
                result = dict(unknown, state="ready", stats=filtered, work=work, observed_at=now, stale=False, error=None)
                self._last_success = result
            except Exception:
                retained = previous[1] if previous else self._last_success
                result = dict(retained, state="unavailable", stale=True, error=unknown["error"]) if retained else unknown
                if result.get("work") and result["work"]["id"] != valid_work:
                    result["work"] = None
            self._cache[valid_work] = (now, result)
            if len(self._cache) > 8:
                oldest = min(self._cache, key=lambda key:self._cache[key][0])
                if oldest != valid_work:
                    del self._cache[oldest]
            return deepcopy(result)


def build_observation(status, session, execution, queue, *, clock=time.time, max_events=600, api_trace=None):
    now = clock()
    status, session, execution, queue = (deepcopy(value) if isinstance(value, dict) else {}
                                         for value in (status, session, execution, queue))
    events = {}
    local = {call["id"]: call for call in execution.get("events", []) if isinstance(call, dict) and _id(call.get("id"))}
    tools = {call["id"]: call for call in session.get("tools", []) if isinstance(call, dict) and _id(call.get("id"))}
    trace = [event for event in session.get("trace", []) if isinstance(event, dict)]
    model_starts = {}
    requests, results = set(), set()

    def add(identifier, kind, category, label, *, source=None, tool_id=None, role="qoder", state=None,
            input="", code="", output="", elapsed_ms=None, source_time=None):
        source = source or {}
        record = dict(id=identifier, kind=kind, category=category, label=public_text(label, 200),
                      time=_time(source.get("time") if source_time is None else source_time),
                      observed_at=_time(source.get("observed_at")) or now,
                      message_id=_id(source.get("message_id")), tool_id=_id(tool_id),
                      work_id=_id(source.get("work_id")), role=role,
                      state=state, input=public_text(input, 16000), code=public_text(code, 16000), output=public_text(output, 16000))
        if _number(elapsed_ms):
            record["elapsed_ms"] = elapsed_ms
        events[identifier] = record

    for event in public_history(status.get("lifecycle")):
        name = RESOURCES[event["resource"]]
        action = {"resource_created":"创建 ", "resource_checked":"核对 ",
                  "resource_reused":"接续已有 ", "resource_check_failed":"核对暂不可用 · "}[event["kind"]]
        detail = ("创建成功后的本机记录。" if event["source"] == "local" else
                  "服务端对象的 created_at，不是观测时刻补造的创建动作。" if event["source"] == "qca_record" else
                  "观察页接入时核对现有资源与 Session 关联；没有重新创建资源。")
        add("resource:" + event["kind"] + ":" + event["resource_id"], event["kind"], "resource", action + name,
            source=event, role=event["resource"] if event["resource"] in {"analysis", "browser"} else "qoder",
            state="unknown" if event["kind"] == "resource_check_failed" else "completed",
            input=event["resource_id"], output=detail)

    for event in trace:
        identifier, kind = _id(event.get("id")), event.get("type")
        if not identifier or not isinstance(kind, str):
            continue
        if kind in {"user.message", "agent.message"}:
            add("qca:" + identifier, kind, "message", "用户消息" if kind == "user.message" else "Agent 回复",
                source=event, state="observed", output=event.get("text", ""))
        elif kind in {"agent.tool_use", "agent.custom_tool_use"}:
            call = local.get(identifier, {})
            requests.add(identifier)
            add("qca:" + identifier, kind, "tool", "QCA 下发工具：" + str(event.get("tool", "tool")),
                source=event, tool_id=identifier, state="denied" if event.get("permission") == "deny" else "observed",
                input=call.get("input", ""), code=call.get("code", ""))
        elif kind in {"user.tool_result", "user.custom_tool_result", "agent.tool_result"}:
            tool_id = _id(event.get("tool_use_id"))
            results.add(tool_id)
            add("qca:" + identifier, kind, "tool", "QCA 收到工具结果", source=event, tool_id=tool_id,
                state="error" if event.get("is_error") else "posted", output=local.get(tool_id, {}).get("output", ""))
        elif kind in {"span.model_request_start", "session.model_request_start", "agent.model_request_start", "model_request_start"}:
            model_starts[identifier] = event
            add("qca:" + identifier, kind, "model", "模型请求开始", source=event, state="started")
        elif kind in {"span.model_request_end", "session.model_request_end", "agent.model_request_end", "model_request_end"}:
            start = model_starts.get(event.get("model_request_start_id"))
            start_time = _time(start.get("time")) if start else None
            end_time = _time(event.get("time"))
            elapsed = (end_time - start_time) * 1000 if start_time is not None and end_time is not None and end_time >= start_time else None
            add("qca:" + identifier, kind, "model", "模型请求结束", source=event, state="completed", elapsed_ms=elapsed)
        elif kind in {"session.status_busy", "session.status_running", "session.status_rescheduling", "session.status_idle", "session.status_terminated", "session.deleted", "user.interrupt"}:
            label = {"session.status_busy":"Session 开始处理", "session.status_running":"Session 正在执行",
                     "session.status_rescheduling":"Session 等待重新调度", "session.status_idle":"Session 进入空闲",
                     "session.status_terminated":"Session 已结束", "session.deleted":"Session 已删除", "user.interrupt":"用户中断"}[kind]
            add("qca:" + identifier, kind, "session", label, source=event,
                state=event.get("reason") or kind.rsplit(".", 1)[-1])

    for tool_id, tool in tools.items():
        call = local.get(tool_id, {})
        if tool_id not in requests:
            add("qca-request:" + tool_id, "agent.tool_use", "tool", "QCA 下发工具：" + str(tool.get("tool", "tool")),
                source=tool, source_time=tool.get("requested_at"), tool_id=tool_id, state="observed",
                input=call.get("input", ""), code=call.get("code", ""))
        if tool_id not in results and tool.get("posted_at") is not None:
            add("qca-result:" + tool_id, "user.tool_result", "tool", "QCA 收到工具结果",
                source=tool, source_time=tool["posted_at"], tool_id=tool_id, state=tool.get("state"), output=call.get("output", ""))

    for tool_id, call in local.items():
        started, finished = _time(call.get("started_at")), _time(call.get("finished_at"))
        elapsed = call.get("elapsed_ms") if started is not None and finished is not None and finished >= started else None
        stamp = next((call[key] for key in ("finished_at", "cancelled_at", "posted_at", "started_at", "observed_at")
                      if _time(call.get(key)) is not None), None)
        role = call.get("role") if call.get("role") in {"analysis", "browser", "transfer"} else "analysis"
        source = dict(call, message_id=_id(call.get("message_id")) or _id(tools.get(tool_id, {}).get("message_id")))
        if _time(call.get("observed_at")) is not None:
            add("execution-received:" + tool_id, "tool_received", "tool", "Worker 收到工具调用",
                source=source, source_time=call["observed_at"], tool_id=tool_id, role="worker",
                state="received", input=call.get("input", ""))
        if started is not None:
            add("execution-start:" + tool_id, "tool_started", "tool", "开始执行 · " + call.get("title", "工具"),
                source=source, source_time=started, tool_id=tool_id, role=role, state="running",
                input=call.get("input", ""), code=call.get("code", ""))
        if call.get("state") in {"completed", "failed", "error", "cancelled", "canceled", "posted", "denied"}:
            # A cancellation after tool_finished stops result posting; it must
            # not change the identity of the execution that already finished.
            state = call.get("state")
            if finished is not None and state in {"cancelled", "canceled"}:
                state = "completed"
            prefix = "execution-cancel:" if state in {"cancelled", "canceled"} else "execution:"
            add(prefix + tool_id, "tool_execution", "tool", call.get("title", "本地工具执行"),
                source=source, source_time=stamp, tool_id=tool_id, role=role, state=state,
                input=call.get("input", ""), code=call.get("code", ""), output=call.get("output", ""), elapsed_ms=elapsed)

    runtime = [event for event in execution.get("runtime", []) if isinstance(event, dict)]
    runtime_labels = {"worker_ready":"Worker 已启动", "worker_polling":"Worker 开始轮询", "work_poll":"Worker 轮询 Work",
                      "work_ack":"Worker 确认 Work", "work_claimed":"Worker 已领取 Work", "heartbeat_confirmed":"Work 心跳已确认",
                      "heartbeat_failed":"Work 心跳失败", "heartbeat_stopped":"QCA 已停止 Work", "worker_stopped":"Worker 已停止",
                      "worker_error":"Worker 出错", "tool_stage":"工具执行阶段", "tool_cancelled":"工具结果未回传"}
    for event in runtime:
        kind = event.get("kind")
        if kind not in runtime_labels:
            continue
        identity = json.dumps({key:event.get(key) for key in ("kind", "time", "work_id", "tool_use_id", "stage")}, sort_keys=True)
        identifier = "worker:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        category = "heartbeat" if kind.startswith("heartbeat_") else "tool" if kind.startswith("tool_") else "work"
        role = event.get("role") if category == "tool" and event.get("role") in {"analysis", "browser", "transfer"} else "worker"
        add(identifier, kind, category, runtime_labels[kind], source=event, tool_id=event.get("tool_use_id"), role=role,
            state=event.get("state") or event.get("result") or kind, output=event.get("detail") or event.get("error") or "")
        if kind == "tool_stage":
            events[identifier]["stage"] = public_text(event.get("stage", ""), 100)

    heartbeats = [event for event in runtime if event.get("kind") == "heartbeat_confirmed" and _time(event.get("time")) is not None]
    heartbeat = max(heartbeats, key=lambda event: _time(event["time"])) if heartbeats else None
    stops = [event for event in runtime if event.get("kind") in {"worker_stopped", "worker_error", "heartbeat_failed", "heartbeat_stopped"}
             and _time(event.get("time")) is not None]
    stop = max(stops, key=lambda event: _time(event["time"])) if stops else None
    heartbeat_at = (_time(heartbeat.get("last_heartbeat")) or _time(heartbeat.get("time"))) if heartbeat else None
    ttl = heartbeat.get("ttl_seconds") if heartbeat and _number(heartbeat.get("ttl_seconds")) else None
    expires = heartbeat_at + ttl if heartbeat_at is not None and ttl is not None else None
    transport_at = _time(execution.get("observed_at"))
    stale = bool(execution.get("stale") or execution.get("error") or (transport_at is not None and not 0 <= now - transport_at <= 15))
    worker_state = "unknown"
    if stop and (not heartbeat or _time(stop["time"]) >= _time(heartbeat["time"])):
        worker_state = "stopped"
    elif not stale and expires is not None and heartbeat_at <= now:
        worker_state = "healthy" if now < expires else "expired"
    worker = dict(state=worker_state, heartbeat_at=heartbeat_at, ttl_seconds=ttl, expires_at=expires,
                  observed_at=transport_at, stale=stale, reason=stop.get("kind") if worker_state == "stopped" else None)
    ordered = sorted(events.values(), key=lambda event: event["time"] if event["time"] is not None else event["observed_at"])
    result = dict(version=1, status=status, session=session, execution=execution, queue=queue,
                events=ordered[-max_events:], worker=worker, observed_at=now,
                history_incomplete=bool(len(ordered)>max_events or session.get("history_incomplete") or session.get("truncated")
                                        or execution.get("history_incomplete") or execution.get("truncated")))
    # The ordinal journal must see all available facts before the display window
    # drops older rows. Pass identities only, never another copy of task content.
    from .interactions import FLOW_KINDS
    result["_flow_events"] = [{key:event.get(key) for key in
        ("id", "kind", "category", "role", "message_id", "tool_id", "work_id", "state", "time", "observed_at", "label", "stage")}
        for event in ordered if event["kind"] in FLOW_KINDS]

    from .tracing import enrich_observation
    return enrich_observation(result, api_trace)
