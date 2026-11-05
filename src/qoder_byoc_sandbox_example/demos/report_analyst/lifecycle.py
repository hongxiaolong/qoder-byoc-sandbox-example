"""Small public resource history; raw SDK responses and credentials never enter it."""
from datetime import datetime
import math
import re
import time

RESOURCE_KEYS = {"agent": "agent_id", "environment": "environment_id", "session": "session_id",
                 "skill": "skill_id", "analysis": "analysis_sandbox_id", "browser": "browser_sandbox_id"}

RESOURCES = {"agent": "Agent", "environment": "Environment", "session": "Session",
             "skill": "Skill", "analysis": "Analysis Sandbox", "browser": "Browser Sandbox"}


def pending_creation_error(state):
    """An unacknowledged create is not evidence that no resource exists."""
    pending = state.get("pending_creates")
    if not pending:
        return None
    names = ", ".join(RESOURCES.get(key, str(key)) for key in pending) if isinstance(pending, dict) else "创建记录格式异常"
    return ("创建结果待核对：" + names + "；不能重试创建或标记清理完成。"
            "请在相同账号和 Endpoint 下按 metadata.demo_run=" + str(state.get("run_id", "未记录"))
            + " 核对云端；Sandbox 另核对 role=browser/analysis。"
            "保留 var/state/run.json；找到资源后补入对应 ID 并核对 pending_creates，"
            "只有确认未创建或已回收后才能清除待确认记录，不要直接删除运行账本。")


def require_confirmed_creations(state):
    error = pending_creation_error(state)
    if error:
        raise ValueError(error)


def begin_create(state, resource, persist):
    """Persist intent before any non-idempotent create crosses the network."""
    require_confirmed_creations(state)
    if resource not in RESOURCE_KEYS or state.get(RESOURCE_KEYS[resource]):
        raise ValueError("资源已经记录或类型不正确，禁止重复创建：" + resource)
    state["pending_creates"] = {resource: {"started_at": time.time()}}
    state["cleaned"] = False
    persist(state)


def resource_event(kind, resource, identifier, *, created_at=None, clock=time.time, source="local"):
    observed = clock()
    stamp = None
    if isinstance(created_at, str):
        try:
            date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            stamp = date.timestamp() if date.tzinfo else None
        except ValueError:
            pass
    return dict(kind="resource_" + kind, resource=resource, resource_id=identifier,
                time=stamp if stamp is not None else observed, observed_at=observed,
                source=source if stamp is not None or source != "qca_record" else "observation")


def record_created(state, resource, identifier, response=None, *, sdk_create=None):
    # Clear the intent only alongside the exact known ID in the same save.
    pending = state.get("pending_creates")
    if isinstance(pending, dict) and resource in pending:
        if not identifier or state.get(RESOURCE_KEYS[resource]) != identifier:
            raise ValueError("创建回执与运行记录不匹配：" + resource)
        del pending[resource]
        if not pending:
            del state["pending_creates"]
    records = state.setdefault("lifecycle", [])
    if not any(e.get("kind") == "resource_created" and e.get("resource_id") == identifier for e in records):
        records.append(resource_event("created", resource, identifier,
                                     created_at=(response or {}).get("created_at")))
        if isinstance(sdk_create, dict):
            measured = {key: sdk_create.get(key) for key in ("started_at", "finished_at", "elapsed_ms")}
            if all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in measured.values()):
                records[-1]["sdk_create"] = dict(measured, measurement="monotonic", source="sandbox_sdk")
        del records[:-100]


def public_history(records):
    result = []
    for event in records[-100:] if isinstance(records, list) else []:
        if (not isinstance(event, dict) or event.get("resource") not in RESOURCES
                or event.get("kind") not in {"resource_created", "resource_checked", "resource_reused", "resource_check_failed"}
                or not isinstance(event.get("resource_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", event["resource_id"])):
            continue
        item = {key: event[key] for key in ("kind", "resource", "resource_id")}
        for key in ("time", "observed_at"):
            value = event.get(key)
            item[key] = value if isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
        item["source"] = event.get("source") if event.get("source") in {"local", "qca_record", "observation"} else "local"
        measured = event.get("sdk_create")
        if isinstance(measured, dict) and measured.get("measurement") == "monotonic" and measured.get("source") == "sandbox_sdk":
            fields = {key: measured.get(key) for key in ("started_at", "finished_at", "elapsed_ms")}
            if all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in fields.values()):
                item["sdk_create"] = dict(fields, measurement="monotonic", source="sandbox_sdk")
        result.append(item)
    return result
