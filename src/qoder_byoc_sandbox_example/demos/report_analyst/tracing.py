"""Truthful, read-only operation projection over already-public observation facts.

Rows reference original observation event IDs; durations never come from sorting
unrelated clocks or from observing a later event. No model content is inferred.
"""
from copy import deepcopy

from .observation import _id, _number, _time
from .lifecycle import public_history
from .interactions import project_interactions
from qoder_byoc_sandbox_example.qoder.telemetry import project_requests


MODEL_START = {prefix + "model_request_start" for prefix in ("", "span.", "session.", "agent.")}
MODEL_END = {prefix + "model_request_end" for prefix in ("", "span.", "session.", "agent.")}
ROLES = {"qoder", "analysis", "browser", "worker", "transfer"}
CREATE_ROUTES = {"agent": "/agents", "environment": "/environments", "session": "/sessions", "skill": "/skills"}


def _http_tool(record):
    """Only a sent result's explicit tool-use ID establishes an HTTP parent."""
    if record.get("method") != "POST":
        return None
    body = record.get("request", {}).get("body")
    items = body.get("events", []) if isinstance(body, dict) else []
    tools = {_id(item.get("tool_use_id") or item.get("custom_tool_use_id"))
             for item in items if isinstance(item, dict)} - {None}
    return next(iter(tools)) if len(tools) == 1 else None


def _http_message(record):
    if record.get("method") != "POST" or not record.get("path", "").endswith("/events"):
        return None
    body = record.get("response", {}).get("body")
    items = body.get("data", []) if isinstance(body, dict) else []
    messages = {_id(item.get("id")) for item in items if isinstance(item, dict)
                and item.get("type") == "user.message"} - {None}
    sent = record.get("request", {}).get("body")
    sent = sent.get("events", []) if isinstance(sent, dict) else []
    messages.update(_id(item.get("message_id")) for item in sent if isinstance(item, dict))
    messages.discard(None)
    return next(iter(messages)) if len(messages) == 1 else None



def select_turn_api_trace(api_trace, message_id, tool_ids):
    """Keep only explicit POST evidence for one input, never Session proximity.

    Run-level setup, polls and heartbeats remain available in the live view.
    Mixed-input batches are omitted so selecting one parent cannot expose a
    different input's request/response content.
    """
    selected = []
    allowed_tools = {_id(identifier) for identifier in tool_ids} - {None}
    for record in api_trace.get("requests", []):
        if record.get("method") != "POST" or not record.get("path", "").endswith("/events"):
            continue
        request = record.get("request", {}).get("body")
        response = record.get("response", {}).get("body")
        sent = request.get("events", []) if isinstance(request, dict) else []
        received = response.get("data", []) if isinstance(response, dict) else []
        sent = sent if isinstance(sent, list) else []
        received = received if isinstance(received, list) else []
        facts = [item for item in [*sent, *received] if isinstance(item, dict)]
        explicit_tools = {_id(item.get("tool_use_id") or item.get("custom_tool_use_id")) for item in facts} - {None}
        explicit_messages = {_id(item.get("message_id")) for item in facts} - {None}
        receipts = {_id(item.get("id")) for item in received
                    if isinstance(item, dict) and item.get("type") == "user.message"} - {None}
        explicit_messages.update(receipts)
        sent_tools = {_id(item.get("tool_use_id") or item.get("custom_tool_use_id"))
                      for item in sent if isinstance(item, dict)} - {None}
        sent_messages = {_id(item.get("message_id")) for item in sent if isinstance(item, dict)} - {None}
        if explicit_tools - allowed_tools or explicit_messages - {message_id}:
            continue
        if any(isinstance(item, dict) and item.get("type") == "user.message" for item in sent) and receipts != {message_id}:
            continue
        if sent_tools or message_id in sent_messages or message_id in receipts:
            selected.append(deepcopy(record))
    identifiers = {record["id"] for record in selected}
    return dict(requests=selected, history_incomplete=bool(api_trace.get("history_incomplete")),
                sources={key: value for key, value in api_trace.get("sources", {}).items() if key in identifiers})


def build_tracing(data, api_trace=None):
    """Project operation rows without changing raw events or assigning hidden parents."""
    session, execution = data.get("session", {}), data.get("execution", {})
    raw = [e for e in data.get("events", []) if isinstance(e, dict)]
    by_id = {e["id"]: e for e in raw if isinstance(e.get("id"), str)}
    trace = [e for e in session.get("trace", []) if isinstance(e, dict)]
    local = {e["id"]: e for e in execution.get("events", []) if isinstance(e, dict) and _id(e.get("id"))}
    requested = {e["id"]: e for e in session.get("tools", []) if isinstance(e, dict) and _id(e.get("id"))}
    tool_ids = set(local) | set(requested) | {e["tool_id"] for e in raw if _id(e.get("tool_id"))}
    groups, consumed = {}, set()
    turns = {t.get("id"): t for t in session.get("turns", []) if isinstance(t, dict) and _id(t.get("id"))}

    def put(row, scope="background"):
        message = _id(row.get("message_id"))
        if message and scope != "setup" and row["operation"] not in {"session", "work"}:
            scope = "turn"
        key = "turn:" + message if scope == "turn" else scope
        if scope == "background" and row["operation"] in {"tool", "model", "stage"}:
            key = "unassigned"
        if key not in groups:
            groups[key] = dict(id=key, label="未关联输入的操作" if key == "unassigned" else "准备资源" if scope == "setup" else "会话与后台活动" if scope == "background" else "对话",
                               scope=scope, rows=[])
            if scope == "turn":
                groups[key]["message_id"] = message
        groups[key]["rows"].append(row)
        consumed.update(row["event_ids"])
        return row

    def row(identifier, operation, label, ids, *, role="qoder", state="observed", message=None,
            kind="event", timing="instant", source="qca", measurement="event_timestamp", tool=None):
        result = dict(id=identifier, type=kind, operation=operation, label=label,
                      role=role if role in ROLES else "qoder", state=state,
                      event_ids=[key for key in ids if key in by_id], timing_state=timing,
                      source=source, measurement=measurement)
        if _id(message):
            result["message_id"] = message
        if _id(tool):
            result["tool_id"] = tool
        return result

    tool_rows = {}
    for tool_id in sorted(tool_ids):
        facts = [e for e in raw if e.get("tool_id") == tool_id and e.get("kind") != "tool_stage" and e.get("category") != "http"]
        if not facts:
            continue
        call, request = local.get(tool_id, {}), requested.get(tool_id, {})
        message = _id(call.get("message_id")) or _id(request.get("message_id")) or next((_id(e.get("message_id")) for e in facts if _id(e.get("message_id"))), None)
        state = call.get("state") or request.get("state") or facts[-1].get("state") or "observed"
        start, end = _time(call.get("started_at")), _time(call.get("finished_at"))
        measured = start is not None and end is not None and end >= start and _number(call.get("elapsed_ms"))
        timing = "measured" if measured else "running" if start is not None and state == "running" else "incomplete" if start is not None or call.get("history_incomplete") else "uncollected"
        title = call.get("title") or request.get("tool") or facts[0].get("label") or "工具执行"
        item = row("tool:" + tool_id, "tool", title, [e["id"] for e in facts], role=call.get("role", facts[-1].get("role")),
                   state=state, message=message, tool=tool_id, kind="span", timing=timing,
                   source="worker" if call else "qca", measurement="monotonic" if measured else "uncollected")
        if start is not None:
            item["started_at"] = start
        if end is not None:
            item["finished_at"] = end
        if measured:
            item["elapsed_ms"] = call["elapsed_ms"]
        tool_rows[tool_id] = put(item)

    starts = {e["id"]: e for e in trace if e.get("type") in MODEL_START and _id(e.get("id"))}
    ends = {e.get("model_request_start_id"): e for e in trace if e.get("type") in MODEL_END and _id(e.get("model_request_start_id"))}
    for identifier, start in starts.items():
        end = ends.get(identifier)
        a, b = _time(start.get("time")), _time(end.get("time")) if end else None
        measured = a is not None and b is not None and b >= a
        message = _id(start.get("message_id")) or (_id(end.get("message_id")) if end else None)
        active = message and session.get("active_message_id") == message and turns.get(message, {}).get("state") in {"running", "waiting_tools"}
        ids = ["qca:" + identifier] + (["qca:" + end["id"]] if end else [])
        if not any(key in by_id for key in ids):
            continue
        item = row("model:" + identifier, "model", "模型请求", ids, message=message, kind="span",
                   state="completed" if end else "running" if active else "unknown",
                   timing="measured" if measured else "running" if active and a is not None else "incomplete",
                   measurement="qca_paired_timestamps" if measured else "uncollected")
        if a is not None:
            item["started_at"] = a
        if b is not None:
            item["finished_at"] = b
        if measured:
            item["elapsed_ms"] = round((b - a) * 1000, 3)
        put(item)
    for event in trace:
        if event.get("type") in MODEL_END and "qca:" + str(event.get("id")) not in consumed and "qca:" + str(event.get("id")) in by_id:
            item = row("model-end:" + event["id"], "model", "模型请求结束 · 缺少开始记录", ["qca:" + event["id"]],
                       message=event.get("message_id"), kind="span", timing="incomplete", state="completed", measurement="uncollected")
            if _time(event.get("time")) is not None:
                item["finished_at"] = _time(event["time"])
            put(item)

    resource_api = {}
    for record in (api_trace or {}).get("requests", []):
        identifier = "api:" + record["id"]
        if identifier not in by_id:
            continue
        tool_id = _http_tool(record)
        parent = tool_rows.get(tool_id)
        message = parent.get("message_id") if parent else _http_message(record)
        measured = _number(record.get("elapsed_ms"))
        origin = (api_trace or {}).get("sources", {}).get(record["id"])
        item = row("http:" + record["id"], "http", record["method"] + " " + record["path"], [identifier],
                   message=message, role="worker" if origin == "worker" or "/work" in record["path"] else "qoder",
                   state=record.get("state", "unknown"), kind="span", timing="measured" if measured else "incomplete",
                   source=origin + "_qca_http" if origin in {"control", "worker"} else "qca_http", measurement="monotonic" if measured else "uncollected", tool=tool_id)
        for key in ("started_at", "finished_at", "elapsed_ms"):
            if _number(record.get(key)):
                item[key] = record[key]
        if parent:
            item["parent_id"] = parent["id"]
        setup = record["method"] == "POST" and record["path"] in CREATE_ROUTES.values()
        put(item, "setup" if setup else "background")
        body = record.get("response", {}).get("body")
        if setup and record.get("state") == "success" and isinstance(body, dict) and _id(body.get("id")):
            resource_api[(record["path"], body["id"])] = item

    lifecycle = {"resource:" + e["kind"] + ":" + e["resource_id"]: e for e in public_history(data.get("status", {}).get("lifecycle"))}
    for event in raw:
        if event["id"] in consumed:
            continue
        category, kind = event.get("category"), event.get("kind", "")
        operation = "resource" if category == "resource" else "stage" if kind == "tool_stage" else "message" if category == "message" else "model" if category == "model" else "session" if category == "session" else "work"
        message = event.get("message_id") if operation in {"message", "stage", "model", "session"} else None
        if kind == "user.message":
            message = event.get("message_id") or event["id"].removeprefix("qca:")
        item = row("event:" + event["id"], operation, event.get("label", kind), [event["id"]],
                   role=event.get("role"), state=event.get("state"), message=message,
                   source="worker" if event["id"].startswith("worker:") else "lifecycle" if category == "resource" else "qca",
                   tool=event.get("tool_id"))
        if _time(event.get("time")) is not None:
            item["started_at"] = _time(event["time"])
        else:
            item["measurement"] = "uncollected"
        if operation == "stage" and event.get("tool_id") in tool_rows:
            parent = tool_rows[event["tool_id"]]
            item["parent_id"] = parent["id"]
            if parent.get("message_id"):
                item["message_id"] = parent["message_id"]
        resource = lifecycle.get(event["id"])
        if resource:
            path = CREATE_ROUTES.get(resource["resource"])
            captured = resource_api.get((path, resource["resource_id"])) if kind == "resource_created" else None
            sdk = resource.get("sdk_create") if kind == "resource_created" else None
            if captured:
                item["parent_id"] = captured["id"]
                item["source"] = "lifecycle+qca_http"
            elif sdk:
                parent = row("sdk:" + resource["resource_id"], "resource", "创建 " + ("Browser Sandbox" if resource["resource"] == "browser" else "Analysis Sandbox"),
                             [event["id"]], role=event.get("role"), state="completed", kind="span", timing="measured",
                             source="sandbox_sdk", measurement="monotonic")
                parent.update({key: sdk[key] for key in ("started_at", "finished_at", "elapsed_ms")})
                put(parent, "setup")
                item["parent_id"] = parent["id"]
            elif kind == "resource_created":
                item["measurement"] = "uncollected"
                if path:
                    item["label"] += " · POST " + path + "（未采集）"
        put(item, "setup" if operation == "resource" else "background")

    ordered = sorted(groups.values(), key=lambda g: (0 if g["scope"] == "setup" else 2 if g["scope"] == "background" else 1,
                                                      min((r.get("started_at", float("inf")) for r in g["rows"])), g["id"]))
    for group in ordered:
        group["rows"].sort(key=lambda r: (r.get("started_at", float("inf")), r["id"]))
    return dict(version=1, groups=ordered,
                history_incomplete=bool(data.get("history_incomplete") or (api_trace or {}).get("history_incomplete")))


def enrich_observation(data, api_trace=None):
    """Append only captured HTTP events and retain their safe original records."""
    data = deepcopy(data)
    run_id = data.get("status", {}).get("run_id") or data.get("execution", {}).get("identity", {}).get("run_id")
    session_id = data.get("status", {}).get("session_id")
    captured = project_requests((api_trace or {}).get("requests", []), run_id=run_id, session_id=session_id) if run_id else {"requests": [], "history_incomplete": bool((api_trace or {}).get("requests"))}
    captured["history_incomplete"] |= bool((api_trace or {}).get("history_incomplete"))
    sources = (api_trace or {}).get("sources", {})
    sources = sources if isinstance(sources, dict) else {}
    captured["sources"] = {item["id"]: sources[item["id"]] for item in captured["requests"]
                           if sources.get(item["id"]) in {"control", "worker"}}
    existing = {event["id"] for event in data.get("events", [])}
    tool_messages = {item["id"]: _id(item.get("message_id"))
                     for source in (data.get("session", {}).get("tools", []), data.get("execution", {}).get("events", []))
                     for item in source if isinstance(item, dict) and _id(item.get("id")) and _id(item.get("message_id"))}
    for record in captured["requests"]:
        identifier = "api:" + record["id"]
        if identifier in existing:
            continue
        tool_id = _http_tool(record)
        data.setdefault("events", []).append(dict(id=identifier, kind="http.request", category="http",
            label=record["method"] + " " + record["path"], time=record.get("finished_at"),
            observed_at=record.get("finished_at"), message_id=tool_messages.get(tool_id) or _http_message(record), tool_id=tool_id,
            work_id=None, role="worker" if captured["sources"].get(record["id"]) == "worker" or "/work" in record["path"] else "qoder", state=record.get("state"),
            input="", code="", output="", api_request_id=record["id"], **({"elapsed_ms": record["elapsed_ms"]} if _number(record.get("elapsed_ms")) else {})))
        existing.add(identifier)
    # A captured request/response becomes visible only when the attempt ended.
    # Python's stable sort preserves existing order for equal real timestamps.
    def event_time(event):
        stamp = _time(event.get("time"))
        if stamp is None:
            stamp = _time(event.get("observed_at"))
        return stamp if stamp is not None else float("inf")
    data["events"] = sorted(data.get("events", []), key=event_time)
    data["api_trace"] = captured
    data["tracing"] = build_tracing(data, captured)
    data["interactions"] = project_interactions(data)
    data["history_incomplete"] = bool(data.get("history_incomplete") or captured["history_incomplete"])
    return data
