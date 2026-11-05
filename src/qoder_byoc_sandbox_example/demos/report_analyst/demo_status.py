# SPDX-License-Identifier: Apache-2.0
"""给本机预览提供不含凭证的会话状态与三轮任务指引。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from urllib.parse import quote

from qoder_byoc_sandbox_example.qoder.client import APIError
from .lifecycle import public_history, resource_event


class DemoStatus:
    def __init__(self, state_path, analysis_url, client, scenarios, timeout_seconds,
                 *, clock=time.time, cache_seconds=15):
        self.state_path = Path(state_path)
        self.analysis_url = analysis_url
        self.client = client
        self.scenarios = scenarios
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.cache_seconds = cache_seconds
        self._cached = None
        self._lock = threading.Lock()
        self._session_record = None
        self._resource_observations = {}
        self._resource_attempts = {}

    def _session_state(self, identity):
        with self._lock:
            now = self.clock()
            if self._cached and self._cached[0] == identity and now - self._cached[1] < self.cache_seconds:
                return self._cached[2:]
            try:
                session = self.client.request("GET", "/sessions/" + quote(identity, safe=""))
                if not isinstance(session, dict) or session.get("id") != identity:
                    raise ValueError("Unexpected session response")
                self._session_record = {key: session.get(key) for key in
                                        ("id", "agent", "environment_id", "created_at", "metadata")}
                status = session.get("status")
                if isinstance(status, dict):
                    status = status.get("type") or status.get("state")
                archived = any(session.get(key) for key in ["archived_at", "archived", "is_archived", "deleted_at"])
                if archived or str(status).lower() in {"archived", "deleted", "terminated"}:
                    result = ("expired", "Qoder 会话已归档或结束，请重新创建演示资源。")
                else:
                    result = ("ready", "请使用创建此 Session 的 Qoder 国际站个人账号打开会话。")
            except APIError as exc:
                result = (
                    ("expired", "Qoder 会话已不存在，请重新创建演示资源。")
                    if exc.status in {404, 410}
                    else ("unavailable", "暂时无法验证 Qoder 会话，请检查账号凭证或网络后重试。")
                )
            except Exception:
                result = ("unavailable", "暂时无法验证 Qoder 会话，请检查网络后重试。")
            self._cached = (identity, now, *result)
            return result

    def _observe_resources(self, state, *, history_only=False):
        """One successful check per bound resource; retries are bounded by cache time."""
        events = public_history(state.get("lifecycle"))
        with self._lock:
            for resource, route in (("agent", "agents"), ("environment", "environments"), ("session", "sessions")):
                identifier = state.get(resource + "_id")
                if not identifier:
                    continue
                key = (state.get("run_id"), resource, identifier)
                if key in self._resource_observations:
                    events.extend(self._resource_observations[key])
                    continue
                if self.clock() - self._resource_attempts.get(key, float("-inf")) < self.cache_seconds:
                    continue
                self._resource_attempts[key] = self.clock()
                try:
                    item = (self._session_record if resource == "session" and self._session_record else
                            self.client.request("GET", "/" + route + "/" + quote(identifier, safe="")))
                    if not isinstance(item, dict) or item.get("id") != identifier:
                        raise ValueError("Resource identity mismatch")
                    session = self._session_record or {}
                    if not history_only and resource == "agent" and (session.get("agent") or {}).get("id") != identifier:
                        raise ValueError("Session Agent mismatch")
                    if not history_only and resource == "environment" and session.get("environment_id") != identifier:
                        raise ValueError("Session Environment mismatch")
                    observed = []
                    if (not any(e["kind"] == "resource_created" and e["resource_id"] == identifier for e in events)
                            and item.get("created_at") and (item.get("metadata") or {}).get("demo_run") == state.get("run_id")):
                        created = resource_event("created", resource, identifier,
                                                 created_at=item["created_at"], clock=self.clock, source="qca_record")
                        if created["source"] == "qca_record":
                            observed.append(created)
                    if not history_only:
                        observed.append(resource_event("checked", resource, identifier, clock=self.clock, source="observation"))
                        observed.append(resource_event("reused", resource, identifier, clock=self.clock, source="observation"))
                    self._resource_observations[key] = observed
                    events.extend(observed)
                except Exception:
                    events.append(resource_event("check_failed", resource, identifier, clock=self.clock, source="observation"))
        return public_history(events)

    def snapshot(self):
        result = {
            "state": "loading", "message": "等待创建演示资源。", "session_url": None,
            "run_id": None, "analysis_sandbox_id": None, "browser_sandbox_id": None,
            "expires_at": None, "phases": {},
        }
        try:
            state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        except (OSError, ValueError):
            result.update(state="unavailable", message="本地运行记录无法读取，请检查后重新启动预览。")
            return result
        if not isinstance(state, dict):
            result.update(state="unavailable", message="本地运行记录格式不正确，请检查后重新启动预览。")
            return result
        for key in ["run_id", "analysis_sandbox_id", "browser_sandbox_id", "session_id", "agent_id", "agent_version", "environment_id", "skill_id", "skill_version"]:
            result[key] = state.get(key)
        result["lifecycle"] = public_history(state.get("lifecycle"))
        for phase in ["collect", "revise", "append"]:
            phase_state = (
                "verified" if state.get(phase + "_verified") else
                "done" if state.get(phase + "_done") else
                "submitted" if state.get(phase + "_message_id") else "new"
            )
            result["phases"][phase] = {"state": phase_state, "prompt": self.scenarios.get(phase, ""), "message_id": state.get(phase + "_message_id")}
        if not state:
            return result
        if state.get("cleaned"):
            result.update(state="expired", message="本轮运行资源已回收；仍可查看已记录轨迹，重新执行请运行向导。")
            result["lifecycle"] = self._observe_resources(state, history_only=True)
            return result
        if state.get("analysis_url") != self.analysis_url:
            result.update(state="unavailable", message="此预览对应上一轮运行，请关闭旧预览并重新启动。")
            return result
        session_id = state.get("session_id")
        if not session_id or not state.get("worker_pid"):
            result.update(message="环境还在准备，请等待 Worker 启动完成。")
            return result
        created = state.get("created_at")
        if isinstance(created, (int, float)):
            expires = created + self.timeout_seconds
            result["expires_at"] = datetime.fromtimestamp(expires, timezone.utc).isoformat()
            if self.clock() >= expires:
                result.update(state="expired", message="本轮 Sandbox 已达到配置的有效期，请回收后重新创建。")
                return result
        result["state"], result["message"] = self._session_state(session_id)
        if result["state"] == "ready":
            result["session_url"] = "https://qoder.com/cloud/sessions/" + quote(session_id, safe="")
            result["lifecycle"] = self._observe_resources(state)
        return result
