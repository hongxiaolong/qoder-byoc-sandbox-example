# SPDX-License-Identifier: Apache-2.0
"""Opt-in, run-bound QCA HTTP evidence. Never writes unprojected payloads.

The JSONL header binds the file to one run. An exclusive file lock protects
bounded rewrites across threads/processes; snapshots take the same lock and
project again, because a journal is untrusted input to the observer UI.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid
from urllib.parse import urlsplit

from ..environments.self_hosted.tools import credential_values, redact_value

MAX_ENTRIES = 400
MAX_BYTES = 8 * 1024 * 1024
MAX_RECORD_BYTES = 48 * 1024
MAX_TEXT = 8000
HEADER_KIND = "api.trace.journal"
RESOURCE_KINDS = {"sessions", "agents", "environments", "skills"}
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
PUBLIC_EVENTS = {
    "user.message", "agent.message", "agent.tool_use", "agent.custom_tool_use",
    "user.tool_result", "user.custom_tool_result", "agent.tool_result",
    "user.interrupt", "user.tool_confirmation", "session.status_idle",
    "session.status_busy", "session.status_running", "session.status_rescheduling",
    "session.status_terminated", "session.deleted",
    *{prefix + "model_request_" + end for prefix in ("", "span.", "session.", "agent.")
      for end in ("start", "end")},
}
TOOL_ARGS = {
    "download_report": {"month", "region"}, "run_python": {"code"},
    "publish_dashboard": {"analysis_file"},
    "append_record": {"category", "amount", "date", "region"},
    "export_artifacts": {"title"}, "runtime_evidence": set(),
}
SAFE_HEADERS = {"accept", "content-type", "content-length", "content-encoding",
                "idempotency-key", "worker-id", "x-request-id", "request-id", "retry-after"}
EVENT_FIELDS = {"id", "type", "message_id", "model_request_start_id", "tool_use_id",
                "custom_tool_use_id", "created_at", "timestamp", "name", "is_error",
                "evaluated_permission"}
RESOURCE_FIELDS = {"id", "name", "title", "type", "version", "latest_version", "model",
                   "environment_id", "session_id", "created_at", "updated_at", "archived_at", "state"}
WORK_FIELDS = {"id", "state", "environment_id", "session_id", "lease_extended",
               "last_heartbeat", "ttl_seconds", "created_at", "updated_at"}


def _scalar(value):
    if isinstance(value, str):
        # Length limits are applied only after complete known-secret replacement.
        # Cutting here can leave almost an entire credential at the boundary.
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _fields(value, names):
    if not isinstance(value, dict):
        return {}
    return {key: _scalar(value[key]) for key in names if key in value
            and (value[key] is None or isinstance(value[key], (str, bool, int, float)))}


def _identifier(value):
    return value[:200] if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,200}", value) else None


def _recorded_envelope(value, run_id):
    """Validate transport evidence before reprojection can supply any defaults.

    This is a schema/provenance check, not authentication of a remote host.
    Mapped endpoint descriptions and partial records cannot become captures.
    """
    if not isinstance(value, dict) or value.get("kind") != "http.request" or value.get("provider") != "qca" or value.get("run_id") != run_id:
        return False
    if not _identifier(value.get("id")) or value.get("method") not in HTTP_METHODS:
        return False
    path = value.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 2000 or any(c.isspace() for c in path):
        return False
    if urlsplit(path).path != path:
        return False
    proof = value.get("capture")
    if (not isinstance(proof, dict) or proof.get("recorded") is not True
            or proof.get("projection") != "endpoint_allowlist_v1" or proof.get("raw_body_saved") is not False
            or proof.get("mapped") is True or proof.get("synthetic") is True
            or value.get("mapped") is True or value.get("synthetic") is True):
        return False
    for key in ("started_at", "finished_at", "elapsed_ms"):
        number = value.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            return False
    status, attempt, state = value.get("status_code"), value.get("attempt"), value.get("state")
    if "status_code" not in value or (status is not None and (type(status) is not int or not 100 <= status < 600)):
        return False
    if type(attempt) is not int or not 1 <= attempt <= 10000 or state not in ("success", "error", "network_error"):
        return False
    if status is None and state == "success":
        return False
    for direction in ("request", "response"):
        payload = value.get(direction)
        if (not isinstance(payload, dict) or not isinstance(payload.get("headers"), dict)
                or "body" not in payload or payload.get("body_projected") is not True
                or type(payload.get("body_omitted")) is not bool):
            return False
        if direction == "request" and not isinstance(payload.get("query"), dict):
            return False
    return True


def _hidden(value):
    return (value.get("hidden") is True or value.get("is_hidden") is True
            or value.get("visibility") not in (None, "public", "visible")
            or value.get("channel") in ("analysis", "reasoning", "internal")
            or value.get("role") in ("system", "developer"))


def _event(value):
    if not isinstance(value, dict) or value.get("type") not in PUBLIC_EVENTS or _hidden(value):
        return None
    result = _fields(value, EVENT_FIELDS)
    kind = value["type"]
    if kind in {"agent.tool_use", "agent.custom_tool_use"}:
        allowed = TOOL_ARGS.get(value.get("name"), set())
        result["input"] = _fields(value.get("input"), allowed)
        result["input_projected"] = True
        if value.get("name") not in TOOL_ARGS:
            # Native/unknown tools may carry arbitrary files and commands. Their
            # allowed business content is projected separately by execution.py.
            result["input_omitted"] = True
    if kind in {"user.tool_result", "user.custom_tool_result", "agent.tool_result"}:
        # Transport evidence has no trusted tool/path context. Keeping arbitrary
        # result text here would bypass the execution snapshot's content policy.
        result["content_omitted"] = True
    if kind in {"user.message", "agent.message"}:
        content = value.get("content")
        if isinstance(content, list):
            result["content"] = [{"type": "text", "text": item["text"]}
                                 for item in content[:30] if isinstance(item, dict)
                                 and item.get("type") == "text" and isinstance(item.get("text"), str)
                                 and not _hidden(item)]
    if kind == "session.status_idle" and isinstance(value.get("stop_reason"), dict):
        stop = value["stop_reason"]
        result["stop_reason"] = _fields(stop, {"type"})
        if isinstance(stop.get("event_ids"), list):
            result["stop_reason"]["event_ids"] = [v for item in stop["event_ids"][:100]
                                                       if (v := _identifier(item))]
    return result


def _resource(value):
    result = _fields(value, RESOURCE_FIELDS)
    if not isinstance(value, dict):
        return result
    if isinstance(value.get("agent"), dict):
        result["agent"] = _fields(value["agent"], {"id", "type", "version"})
    if isinstance(value.get("metadata"), dict):
        result["metadata"] = _fields(value["metadata"], {"demo_run"})
    if isinstance(value.get("config"), dict):
        result["config"] = _fields(value["config"], {"type"})
    # Agent system/tools/input schemas and environment setup scripts are omitted.
    return result


def _body(value, route, response=False):
    if value is None:
        return None
    if not isinstance(value, dict) or route == "unknown" or route == "binary":
        return None
    if response and "error" in value:
        error = value["error"]
        return {"error": _fields(error, {"code", "type", "message"}) if isinstance(error, dict) else _scalar(error)}
    if route == "events":
        key = "data" if response else "events"
        result = _fields(value, {"has_more", "next_page"}) if response else {}
        if isinstance(value.get(key), list):
            result[key] = [event for item in value[key][:100] if (event := _event(item)) is not None]
        return result
    if route == "work":
        result = _fields(value, WORK_FIELDS | {"force"})
        if isinstance(value.get("data"), dict):
            result["data"] = _fields(value["data"], {"id", "type"})
        return result
    result = _resource(value)
    if isinstance(value.get("data"), list):
        result["data"] = [_resource(item) for item in value["data"][:100] if isinstance(item, dict)]
        result.update(_fields(value, {"has_more", "next_page"}))
    return result


def _route(path):
    parts = path.strip("/").split("/")
    if parts[0] not in RESOURCE_KINDS:
        return "unknown", parts
    if parts[0] == "skills" and len(parts) > 2:
        return "binary", parts
    if parts[0] == "sessions" and len(parts) == 3 and parts[2] == "events":
        return "events", parts
    if parts[0] == "environments" and len(parts) >= 3 and parts[2] == "work":
        if len(parts) in (3, 4) or (len(parts) == 5 and parts[4] in {"ack", "heartbeat", "stop"}):
            return "work", parts
    if len(parts) <= 2 or (len(parts) == 3 and parts[2] in {"archive", "cancel"}):
        return "resource", parts
    return "unknown", parts


def _headers(value):
    if not isinstance(value, dict):
        return {}
    return {str(k).lower(): _scalar(v) for k, v in value.items()
            if isinstance(k, str) and k.lower() in SAFE_HEADERS and isinstance(v, (str, int))}


def _private_values(value, depth=0):
    """Recognize credentials in raw input before dropping their private fields."""
    if depth > 12:
        return []
    result = []
    if isinstance(value, dict):
        for key, item in list(value.items())[:500]:
            if re.search(r"token|password|secret|api[_-]?key|access[_-]?key|authorization|cookie", str(key), re.I):
                if isinstance(item, str) and item:
                    result.append(item)
                    if item.lower().startswith("bearer "):
                        result.append(item[7:])
            elif isinstance(item, (dict, list)):
                result.extend(_private_values(item, depth + 1))
    elif isinstance(value, list):
        for item in value[:500]:
            result.extend(_private_values(item, depth + 1))
    return result


class APITraceRecorder:
    def __init__(self, path, run_id, session_id=None, secrets=()):
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("API trace requires a run_id")
        self.path, self.run_id = Path(path), run_id
        self.session_id = _identifier(session_id)
        self.secrets = tuple(s for s in (*credential_values(), *secrets) if isinstance(s, str) and s)

    def _project(self, event):
        if not isinstance(event, dict):
            raise ValueError("Invalid HTTP capture")
        # Include transient API credentials even if the process environment was cleared.
        private = tuple(sorted(set((*self.secrets, *(s for s in event.get("_secrets", ()) if isinstance(s, str) and s),
                                    *_private_values(event.get("request")), *_private_values(event.get("response")))),
                               key=len, reverse=True))
        path = urlsplit(str(event.get("path", ""))).path
        route, parts = _route(path)
        result = {"id": _identifier(event.get("id")) or "http-" + uuid.uuid4().hex,
                  "kind": "http.request", "provider": "qca", "method": str(event.get("method", "")),
                  "path": path, "run_id": self.run_id,
                  "state": event.get("state") if event.get("state") in {"success", "error", "network_error"} else "error",
                  "capture": {"recorded": True, "projection": "endpoint_allowlist_v1", "raw_body_saved": False}}
        for key in ("started_at", "finished_at", "elapsed_ms"):
            value = event.get(key)
            result[key] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        status, attempt = event.get("status_code"), event.get("attempt")
        result["status_code"] = status if type(status) is int and 100 <= status < 600 else None
        result["attempt"] = attempt if type(attempt) is int and 1 <= attempt <= 10000 else None
        for direction in ("request", "response"):
            source = event.get(direction) if isinstance(event.get(direction), dict) else {}
            projected = _body(source.get("body"), route, direction == "response")
            result[direction] = {"headers": _headers(source.get("headers")), "body": projected,
                                 "body_projected": True,
                                 "body_omitted": source.get("body_omitted") is True or (source.get("body") is not None and projected is None)}
            if direction == "request":
                query_names = ({"order", "limit", "after_id", "page"} if route == "events" else
                               {"block_ms", "expected_last_heartbeat", "desired_ttl_seconds"} if route == "work" else
                               {"limit", "page", "order"} if route == "resource" else set())
                result[direction]["query"] = _fields(source.get("query"), query_names)
        # Parent IDs require route/body evidence, not temporal proximity.
        if len(parts) > 1 and parts[0] in RESOURCE_KINDS and _identifier(parts[1]):
            if route != "work":
                result["resource_id"] = parts[1]
            if parts[0] == "sessions":
                result["session_id"] = parts[1]
            if parts[0] == "environments":
                result["environment_id"] = parts[1]
            if route == "work" and len(parts) > 3 and parts[3] != "poll" and _identifier(parts[3]):
                result["resource_id"] = parts[3]
        for direction in ("request", "response"):
            body = result[direction]["body"]
            if not isinstance(body, dict):
                continue
            if _identifier(body.get("id")):
                result.setdefault("resource_id", body["id"])
                if parts[0] == "sessions" and route == "resource":
                    result.setdefault("session_id", body["id"])
                elif parts[0] == "environments" and route == "resource":
                    result.setdefault("environment_id", body["id"])
            for key in ("session_id", "environment_id"):
                if _identifier(body.get(key)):
                    result.setdefault(key, body[key])
            data = body.get("data")
            if route == "work" and isinstance(data, dict) and data.get("type") == "session" and _identifier(data.get("id")):
                result.setdefault("session_id", data["id"])
            events = body.get("events", body.get("data", []))
            if isinstance(events, list):
                tools = {item.get("tool_use_id") or item.get("custom_tool_use_id") for item in events if isinstance(item, dict)} - {None}
                if len(tools) == 1 and _identifier(next(iter(tools))):
                    result["tool_use_id"] = next(iter(tools))
        # Configured context is retained separately; it never fabricates a parent.
        if self.session_id:
            result["capture"]["recorder_session_id"] = self.session_id
        if isinstance(event.get("capture"), dict) and event["capture"].get("size_limited") is True:
            result["capture"]["size_limited"] = True
        if len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode()) > MAX_RECORD_BYTES:
            for direction in ("request", "response"):
                result[direction]["body"] = None
                result[direction]["body_omitted"] = True
            result["capture"]["size_limited"] = True
        # Very short explicit tokens are also secrets (the shared helper skips <4).
        def redact(value, limit=MAX_TEXT):
            if isinstance(value, str):
                for secret in private:
                    value = value.replace(secret, "[redacted]")
                # Complete known secrets have been removed before any slicing.
                # Also scrub a boundary fragment from journals produced before
                # this ordering fix; only an actual capped boundary qualifies.
                if len(value) == limit:
                    for secret in private:
                        for length in range(min(len(secret) - 1, len(value)), 3, -1):
                            if value.endswith(secret[:length]):
                                value = value[:-length] + "[redacted]"
                                break
                value = value[:limit]
                # Avoid the shared assignment regex's quadratic scan on long
                # innocuous code/output strings lacking any credential marker.
                return redact_value(value, ()) if re.search(
                    r"https?://|bearer\s|token|password|secret|api[_-]?key|access[_-]?key|authorization", value, re.I
                ) else value
            if isinstance(value, dict):
                return {key: redact(item, 2000 if key == "path" else 10 if key == "method" else MAX_TEXT)
                        for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value
        result = redact(result)
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode()) > MAX_RECORD_BYTES:
            for direction in ("request", "response"):
                result[direction]["body"] = None
                result[direction]["body_omitted"] = True
            result["capture"]["size_limited"] = True
        return result

    def _open(self, create=False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
        fd = os.open(self.path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("API trace is not a regular file")
            if create:
                os.fchmod(fd, 0o600)
            return os.fdopen(fd, "r+" if create else "r", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def _read(self, handle):
        handle.seek(0)
        first = handle.readline(64 * 1024)
        if not first:
            return [], False
        try:
            header = json.loads(first)
            if header.get("kind") != HEADER_KIND or header.get("run_id") != self.run_id:
                raise ValueError("API trace run binding mismatch")
        except (TypeError, AttributeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid API trace run binding") from exc
        if os.fstat(handle.fileno()).st_size > MAX_BYTES:
            return [], True
        lines = handle.read(MAX_BYTES).splitlines()
        incomplete, records = bool(header.get("history_incomplete")), []
        for line in lines:
            try:
                item = json.loads(line)
                if not _recorded_envelope(item, self.run_id):
                    incomplete = True
                    continue
                records.append(self._project(item))
            except (ValueError, TypeError, KeyError, OverflowError):
                incomplete = True
        if len(records) > MAX_ENTRIES:
            records, incomplete = records[-MAX_ENTRIES:], True
        return records, incomplete

    def record(self, event):
        projected = self._project(event)
        with self._open(create=True) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            records, incomplete = self._read(handle)
            records.append(projected)
            if len(records) > MAX_ENTRIES:
                records, incomplete = records[-MAX_ENTRIES:], True
            while True:
                header = {"kind": HEADER_KIND, "run_id": self.run_id, "history_incomplete": incomplete}
                data = "\n".join(json.dumps(item, ensure_ascii=False, allow_nan=False) for item in [header, *records]) + "\n"
                if len(data.encode()) <= MAX_BYTES:
                    break
                records.pop(0)
                incomplete = True
            handle.seek(0)
            handle.write(data)
            handle.truncate()
            handle.flush()

    def snapshot(self):
        try:
            with self._open() as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                records, incomplete = self._read(handle)
        except FileNotFoundError:
            records, incomplete = [], False
        except (ValueError, OSError, UnicodeError):
            records, incomplete = [], True
        return {"requests": records, "history_incomplete": incomplete}


def project_requests(records, *, run_id, session_id=None, secrets=()):
    """Reproject remote journal records before including them in public output.

    Requires the expected run binding; supplied IDs/headers/capture flags are
    untrusted. Returns the snapshot contract without opening any file.
    """
    recorder = APITraceRecorder(".", run_id, session_id=session_id, secrets=secrets)
    result, incomplete = [], False
    if not isinstance(records, list):
        return {"requests": [], "history_incomplete": True}
    if len(records) > MAX_ENTRIES:
        records, incomplete = records[-MAX_ENTRIES:], True
    total = 0
    for item in records:
        try:
            if not _recorded_envelope(item, run_id):
                incomplete = True
                continue
            projected = recorder._project(item)
            total += len(json.dumps(projected, ensure_ascii=False).encode())
            if total > MAX_BYTES:
                incomplete = True
                break
            result.append(projected)
        except (ValueError, TypeError, KeyError, OverflowError):
            incomplete = True
    return {"requests": result, "history_incomplete": incomplete}
