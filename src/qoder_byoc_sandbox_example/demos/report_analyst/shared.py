# SPDX-License-Identifier: Apache-2.0
"""演示运行记录与角色到 Sandbox 实例的映射。"""

import json
from qoder_byoc_sandbox_example.settings import RESULTS_DIR
from qoder_byoc_sandbox_example.qoder.client import API
from qoder_byoc_sandbox_example.sandbox.client import (
    NotFoundException,
    connect_sandbox,
)


def api(config, values):
    return API(config["qoder"]["base_url"], values["QODER_ACCESS_TOKEN"])


def bind_trace(client, state, values=(), *, mutations_only=False):
    """Opt-in recording for this run; dashboard readers remain unconfigured."""
    from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder
    secrets = tuple(values.values()) if isinstance(values, dict) else tuple(values)
    recorder = APITraceRecorder(results(state) / "api-trace.jsonl", state["run_id"],
                                session_id=state.get("session_id"), secrets=secrets)
    if mutations_only:
        client.observer = lambda event: recorder.record(event) if event.get("method") != "GET" else None
    else:
        client.observer = recorder.record
    return client


def connect(state, values, key="analysis_sandbox_id"):
    return connect_sandbox(state[key], values)


def cleanup_sandbox(state, values, key):
    """Verify remote ownership before killing; the caller persists completion.

    Return True after deletion or an explicit not-found response. Ownership,
    permission and network failures raise and leave the run record unchanged.
    """
    roles = {"analysis_sandbox_id": "analysis", "browser_sandbox_id": "browser"}
    if key not in roles or not state.get(key) or not state.get("run_id"):
        raise ValueError(f"{key}: 缺少 Sandbox 归属信息")
    try:
        box = connect(state, values, key)
        info = box.get_info()
        metadata = info.metadata
        if not isinstance(metadata, dict) or (
            metadata.get("demo_run") != state["run_id"]
            or metadata.get("role") != roles[key]
        ):
            raise ValueError(f"{key}: Sandbox 归属或角色不匹配")
        box.kill()
    except NotFoundException:
        pass
    return True


def emit(kind, **data):
    print(
        json.dumps({"kind": kind, **data}, ensure_ascii=False, default=str), flush=True
    )


def results(state):
    p = RESULTS_DIR / state["run_id"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def worker_logs(box):
    return [
        json.loads(x)
        for x in box.files.read("/tmp/demo-worker/worker.jsonl").splitlines()
        if x.strip()
    ]
