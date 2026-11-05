"""Shared, resumable view of public Qoder events for Web and terminal chat.

The observer never makes intent decisions. Messages go unchanged to the existing
Qoder Session. Its journal is separate from the onboarding acceptance state.
"""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import re
from pathlib import Path
from .interactions import interaction_catalog, project_interactions
import sqlite3
import time
import uuid

from .execution import public_text


RUNNING_STATUSES = {"session.status_busy", "session.status_running", "session.status_rescheduling"}


class ChatError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def event_time(event):
    value = next((event[key] for key in ("processed_at", "created_at", "timestamp", "time")
                  if event.get(key) is not None), None)
    if isinstance(value, (float, int)):
        return value / 1000 if value > 100000000000 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def message_text(event):
    content = event.get("content", [])
    if not isinstance(content, list):
        return ""
    return public_text("\n".join(
        item.get("text", "") for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ), 16000)


class LiveSession:
    def __init__(self, client, session_id, journal, *, clock=time.time):
        self.client = client
        self.session_id = session_id
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS submissions (
                    request_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, source TEXT NOT NULL,
                    text_hash TEXT NOT NULL
                );
            """)
            recorded = db.execute("SELECT value FROM meta WHERE key='session_id'").fetchone()
            if recorded and recorded[0] != session_id:
                raise ValueError("会话观察记录属于另一 Session，不能复用。")
            db.execute("INSERT OR IGNORE INTO meta VALUES ('session_id',?)", (session_id,))
        self.journal.chmod(0o600)

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.journal, timeout=3)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def project_interactions(self, observation):
        """Persist task-flow identities once, shared by live and history views."""
        with self.db() as db:
            return project_interactions(observation, db)

    @contextmanager
    def locked(self, wait_seconds=0):
        # Both the preview process and CLI use this lock. The send request is not
        # automatically retried with a fresh key after an ambiguous network error.
        lock_path = self.journal.with_suffix(".lock")
        with lock_path.open("a") as lock:
            lock_path.chmod(0o600)
            deadline = time.monotonic() + wait_seconds
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ChatError("正在同步或提交消息，请稍后重试。") from None
                    time.sleep(.05)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _refresh(self):
        with self.db() as db:
            meta = dict(db.execute("SELECT key, value FROM meta"))
            active = self._observed_active(db, meta.get("active_message_id", ""))
        cursor = meta.get("cursor")
        parent = meta.get("message_id")
        terminal = meta.get("terminal", "")
        for event in self.client.events(self.session_id, cursor):
            identifier, kind = event.get("id"), event.get("type", "")
            if not identifier:
                continue
            event_parent = event.get("message_id") or parent
            if kind in RUNNING_STATUSES and not event.get("message_id") and not active:
                event_parent = None
            if kind == "user.message":
                event_parent = parent = identifier
                active = identifier
            elif (kind == "session.status_idle" and event_parent == active
                  and (event.get("stop_reason") or {}).get("type") != "requires_action"):
                active = ""
            elif kind == "user.interrupt" and event_parent == active:
                active = ""
            elif (kind in RUNNING_STATUSES
                  and event_parent == parent):
                active = event_parent or ""
            elif kind in {"session.status_terminated", "session.deleted"}:
                active, terminal = "", "true"
            record = {
                "id": identifier, "type": kind, "message_id": event_parent,
                "time": event_time(event), "observed_at": self.clock(),
            }
            if kind in RUNNING_STATUSES:
                # Keep provenance for future explicit resumptions. Legacy rows
                # lack this field and cannot prove an explicit association.
                record["message_id_explicit"] = bool(event.get("message_id"))
            if kind in {"user.message", "agent.message"}:
                record["text"] = message_text(event)
            elif kind in {"agent.tool_use", "agent.custom_tool_use"}:
                permission = event.get("evaluated_permission")
                record.update(tool=public_text(event.get("name", "tool"), 100),
                              permission=permission if permission in ("allow", "deny", "ask") else None)
            elif kind in {"user.tool_result", "agent.tool_result", "user.custom_tool_result"}:
                record.update(tool_use_id=event.get("tool_use_id") or event.get("custom_tool_use_id"),
                              is_error=bool(event.get("is_error")))
            elif kind == "session.status_idle":
                stop = event.get("stop_reason") or {}
                record.update(reason=public_text(str(stop.get("type", "")), 100),
                              tool_ids=[public_text(item, 200) for item in stop.get("event_ids", [])
                                        if isinstance(item, str)][:300])
            elif kind in {"span.model_request_start", "span.model_request_end",
                          "session.model_request_start", "session.model_request_end",
                          "agent.model_request_start", "agent.model_request_end",
                          "model_request_start", "model_request_end"}:
                start = event.get("model_request_start_id")
                if isinstance(start, str):
                    record["model_request_start_id"] = public_text(start, 200)
            elif kind not in {"user.interrupt", "session.status_terminated", "session.deleted",
                              "session.status_busy", "session.status_running", "session.status_rescheduling"}:
                # No model reasoning, hidden messages, headers, or raw API payloads.
                record = None
            with self.db() as db:
                if record:
                    db.execute("INSERT OR IGNORE INTO events(id,payload) VALUES (?,?)",
                               (identifier, json.dumps(record, ensure_ascii=False)))
                db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [
                    ("cursor", identifier), ("message_id", parent or ""),
                    ("observed_at", str(self.clock())),
                    ("active_message_id", active), ("terminal", terminal),
                ])

    @staticmethod
    def _observed_active(db, active):
        """Correct legacy false-active metadata without rewriting its journal."""
        if not active:
            return ""
        rows = db.execute("""SELECT payload FROM events
            WHERE json_extract(payload,'$.message_id')=?
              AND json_extract(payload,'$.type') IN (
                'session.status_idle','user.interrupt',
                'session.status_busy','session.status_running','session.status_rescheduling')
            ORDER BY seq DESC""", (active,))
        for (payload,) in rows:
            event = json.loads(payload)
            if event["type"] in RUNNING_STATUSES:
                if event.get("message_id_explicit") is True:
                    return active
            elif event["type"] == "user.interrupt" or event.get("reason") != "requires_action":
                return ""
        return active

    def snapshot(self, *, refresh=True, message_id=None):
        if message_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", message_id):
            raise ChatError("消息编号格式不正确。", 400)
        error = None
        if refresh:
            try:
                with self.locked():
                    self._refresh()
            except Exception:
                error = "会话事件暂未同步，显示上次已观察的记录。"
        with self.db() as db:
            sources = dict(db.execute("SELECT message_id,source FROM submissions"))
            meta = dict(db.execute("SELECT key,value FROM meta"))
            if message_id:
                rows = list(db.execute("""SELECT seq,payload FROM events
                    WHERE id=? OR json_extract(payload,'$.message_id')=?
                    ORDER BY seq LIMIT 5001""", (message_id, message_id)))
                if not any(json.loads(payload).get("type") == "user.message" and json.loads(payload)["id"] == message_id for _, payload in rows):
                    raise ChatError("该消息不在当前 Session 的已观察记录中。", 404)
                history_incomplete = len(rows) > 5000
                rows = rows[:5000]
            else:
                rows = list(db.execute("SELECT seq,payload FROM events ORDER BY seq DESC LIMIT 1200"))
                history_incomplete = db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > len(rows)
            records = {seq: json.loads(payload) for seq, payload in rows}
            active = self._observed_active(db, meta.get("active_message_id"))
            latest = active or meta.get("message_id")
            if latest and history_incomplete and not message_id:
                # Keep an old active turn's replies and tool lifecycle visible
                # even when recent model/status spans fill the rolling window.
                extra = db.execute("""
                    SELECT seq,payload FROM events
                    WHERE json_extract(payload,'$.message_id')=?
                      AND json_extract(payload,'$.type') IN (
                        'agent.message','agent.tool_use','agent.custom_tool_use',
                        'user.tool_result','agent.tool_result','user.custom_tool_result')
                    ORDER BY seq DESC LIMIT 1200
                """, (latest,))
                records.update((seq, json.loads(payload)) for seq, payload in extra)
            needed = {active} if active and not message_id else set()
            for event in records.values():
                needed.update(item for item in (
                    event.get("message_id"), event.get("tool_use_id"),
                    event.get("model_request_start_id"), *event.get("tool_ids", [])
                ) if isinstance(item, str) and item)
            known = {event["id"] for event in records.values()}
            # Fetch referenced roots and requests from the journal, not the
            # remote API. Chunk IDs to stay below SQLite parameter limits.
            missing = list(needed - known)
            for offset in range(0, len(missing), 500):
                identifiers = missing[offset:offset + 500]
                placeholders = ",".join("?" for _ in identifiers)
                extra = db.execute(f"SELECT seq,payload FROM events WHERE id IN ({placeholders})", identifiers)
                records.update((seq, json.loads(payload)) for seq, payload in extra)
            events = [records[seq] for seq in sorted(records)]
            history_incomplete = history_incomplete or bool(
                needed - {event["id"] for event in events}
            )
            catalog = interaction_catalog(db, events)
        turns, tools = [], {}
        by_id = {}
        terminal = False
        for event in events:
            kind = event["type"]
            if kind == "user.message":
                turn = dict(id=event["id"], text=event["text"], source=sources.get(event["id"], "qoder"),
                            time=event["time"], observed_at=event["observed_at"], state="running", replies=[])
                turns.append(turn)
                by_id[turn["id"]] = turn
            parent = by_id.get(event.get("message_id"))
            if kind == "agent.message" and parent and event["text"]:
                parent["replies"].append({k: event[k] for k in ("id", "text", "time", "observed_at")})
            elif kind in {"agent.tool_use", "agent.custom_tool_use"}:
                tools[event["id"]] = {
                    "id": event["id"], "tool": event["tool"], "message_id": event["message_id"],
                    "requested_at": event["time"], "observed_at": event["observed_at"],
                    "permission": event.get("permission"),
                    "state": "denied" if event.get("permission") == "deny" else "observed",
                }
            elif kind in {"user.tool_result", "agent.tool_result", "user.custom_tool_result"}:
                call = tools.get(event.get("tool_use_id"))
                if call:
                    call.update(state="error" if event["is_error"] else "posted", posted_at=event["time"])
            elif kind == "session.status_idle" and parent:
                reason = event.get("reason")
                if reason == "end_turn":
                    parent.update(state="completed", ended_at=event["time"])
                elif reason == "requires_action":
                    parent["state"] = "waiting_tools"
                else:
                    parent.update(state="stopped", stop_reason=reason)
            elif kind == "user.interrupt" and parent:
                parent["state"] = "interrupted"
            elif kind in RUNNING_STATUSES and parent:
                if parent["state"] in {"completed", "stopped", "interrupted"} and event.get("message_id_explicit") is not True:
                    # Older observers attached a Session-level pre-input status
                    # to the completed parent. Correct only this public view.
                    event["message_id"] = None
                else:
                    parent["state"] = "running"
            elif kind in {"session.status_terminated", "session.deleted"}:
                terminal = True
                if parent and parent["state"] in {"running", "waiting_tools"}:
                    parent["state"] = "stopped"
        conversation_numbers = {item["message_id"]: item["number"] for item in catalog["conversations"]}
        for turn in turns:
            turn["number"] = conversation_numbers.get(turn["id"])
        # These records were projected before persistence: no raw parameters,
        # model responses, hidden reasoning, authentication or API envelopes.
        trace_fields = {"id", "type", "message_id", "time", "observed_at", "text", "tool",
                        "tool_use_id", "reason", "tool_ids", "model_request_start_id", "permission", "is_error"}
        trace = [{key: value for key, value in event.items() if key in trace_fields} for event in events]
        return dict(turns=turns[-100:], tools=list(tools.values())[-300:], trace=trace, interaction_catalog=catalog,
                    history_incomplete=history_incomplete or len(turns) > 100 or len(tools) > 300,
                    active_message_id=active or None,
                    terminal=terminal or meta.get("terminal") == "true", observed_at=float(meta.get("observed_at", 0)), stale=bool(error), error=error)

    def send(self, text, request_id=None, *, source="terminal"):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ChatError("请输入 1–16000 字的消息。", 400)
        request_id = request_id or str(uuid.uuid4())
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ChatError("消息编号无效，请重新提交。", 400) from None
        if source not in {"web", "terminal"}:
            raise ChatError("消息来源无效。", 400)
        with self.locked(wait_seconds=5):
            text_hash = hashlib.sha256(text.strip().encode()).hexdigest()
            with self.db() as db:
                old = db.execute("SELECT message_id,text_hash FROM submissions WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old[1] != text_hash:
                    raise ChatError("同一消息编号不能用于不同内容。", 400)
                if old[0]:
                    return {"id": old[0], "request_id": request_id}
            self._refresh()
            current = self.snapshot(refresh=False)
            if current["terminal"]:
                raise ChatError("会话已结束，请重新创建演示资源。", 410)
            if current["active_message_id"] and not old:
                raise ChatError("Qoder 正在处理上一条消息，请等待本轮结束后继续。")
            if not old:
                with self.db() as db:
                    db.execute("INSERT INTO submissions VALUES (?,?,?,?)", (request_id, "", source, text_hash))
            response = self.client.send(self.session_id, [
                {"type": "user.message", "content": [{"type": "text", "text": text.strip()}]}
            ], "live-" + request_id)
            messages = [e for e in response.get("data", []) if e.get("type", "user.message") == "user.message"]
            if not messages or not messages[0].get("id"):
                raise ChatError("未取得消息回执；请先到 Qoder 确认，重试将沿用同一消息编号。", 502)
            message_id = messages[0]["id"]
            with self.db() as db:
                db.execute("UPDATE submissions SET message_id=? WHERE request_id=?", (message_id, request_id))
            return {"id": message_id, "request_id": request_id}
