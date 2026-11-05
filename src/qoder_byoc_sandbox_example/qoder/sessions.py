# SPDX-License-Identifier: Apache-2.0
"""Sessions：绑定 Agent 与 Environment；停止工作并按归属回收 Session 关联资源。"""

from .client import APIError


def create(client, agent, environment, run_id, title):
    return client.request(
        "POST",
        "/sessions",
        {
            "agent": {"id": agent["id"], "type": "agent", "version": agent["version"]},
            "environment_id": environment["id"],
            "title": title,
            "metadata": {"demo_run": run_id},
        },
    )


def cleanup_owned(client, state, persist):
    errors = []
    records = {}

    def owned(key, plural):
        if not state.get("run_id") or not state.get(key):
            raise ValueError(f"{key}: 缺少资源归属信息")
        if key not in records:
            records[key] = client.request("GET", f"/{plural}/{state[key]}")
        item = records[key]
        if (
            not isinstance(item, dict)
            or item.get("metadata", {}).get("demo_run") != state["run_id"]
            or item.get("id", state[key]) != state[key]
        ):
            raise ValueError(f"{key}: 资源归属不匹配")
        if (
            key == "session_id"
            and state.get("environment_id")
            and item.get("environment_id") != state["environment_id"]
        ):
            raise ValueError("session_id: Session 与 Environment 关联不匹配")
        return item

    def completed(key):
        state[key] = True
        persist(state)

    if state.get("work_id") and not state.get("work_stopped"):
        try:
            path = f"/environments/{state['environment_id']}/work/{state['work_id']}"
            try:
                work = client.request("GET", path)
            except APIError as exc:
                if exc.status != 404:
                    raise
                completed("work_stopped")
            else:
                owned("session_id", "sessions")
                owned("environment_id", "environments")
                if (
                    not isinstance(work, dict)
                    or work.get("id") != state["work_id"]
                    or not isinstance(work.get("data"), dict)
                    or work["data"].get("type") != "session"
                    or work["data"].get("id") != state["session_id"]
                    or work.get("environment_id", state["environment_id"])
                    != state["environment_id"]
                ):
                    raise ValueError("work_id: Work 与 Session/Environment 关联不匹配")
                try:
                    client.request("POST", path + "/stop", {"force": True})
                except APIError as exc:
                    if exc.status != 404:
                        raise
                completed("work_stopped")
        except Exception as exc:
            errors.append(f"work_id: {exc}")
    for key, plural, method, suffix in [
        ("session_id", "sessions", "POST", "/archive"),
        ("agent_id", "agents", "POST", "/archive"),
        ("environment_id", "environments", "POST", "/archive"),
        ("skill_id", "skills", "DELETE", ""),
    ]:
        if state.get(key) and not state.get(key + "_cleaned"):
            # Archiving either parent could stop unverified Work indirectly.
            if (
                key in ("session_id", "environment_id")
                and state.get("work_id")
                and not state.get("work_stopped")
            ):
                continue
            try:
                item = owned(key, plural)
                path = f"/{plural}/{state[key]}"
                if suffix != "/archive" or not item.get("archived_at"):
                    try:
                        client.request(method, path + suffix)
                    except APIError as exc:
                        if key != "session_id" or exc.status != 409:
                            raise
                        # An expired worker can leave a residual Session turn.
                        # Cancel only the proven-owned Session, then retry once.
                        client.request("POST", path + "/cancel")
                        records.pop(key, None)
                        if not owned(key, plural).get("archived_at"):
                            client.request("POST", path + suffix)
                completed(key + "_cleaned")
            except APIError as exc:
                if exc.status == 404:
                    completed(key + "_cleaned")
                else:
                    errors.append(f"{key}: {exc}")
            except Exception as exc:
                errors.append(f"{key}: {exc}")
    return errors
