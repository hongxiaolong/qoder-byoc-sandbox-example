# SPDX-License-Identifier: Apache-2.0
"""演示专用的发消息、观察结束和触发验收；Agent 决策由 Qoder 托管。"""

import json
import time
from qoder_byoc_sandbox_example.settings import save
from .shared import api, bind_trace, connect, results, worker_logs, emit
from .scenarios import prompt
from .verification import verify


def wait_turn(config, values, state, phase):
    if state.get(phase + "_done"):
        return
    client = bind_trace(api(config, values), state, values, mutations_only=True)
    box = connect(state, values)
    cursor = state.get(phase + "_cursor") or state[phase + "_message_id"]
    deadline = time.monotonic() + config["demo"]["turn_timeout_seconds"]
    with (results(state) / (phase + "-events.jsonl")).open("a") as output:
        while time.monotonic() < deadline:
            for event in client.events(state["session_id"], cursor):
                cursor = event["id"]
                output.write(json.dumps(event, ensure_ascii=False) + "\n")
                output.flush()
                if event["type"] in {
                    "agent.custom_tool_use",
                    "agent.tool_use",
                    "agent.message",
                }:
                    emit("qoder_event", **event)
                if event["type"] == "session.status_idle":
                    reason = (event.get("stop_reason") or {}).get("type")
                    if reason == "end_turn":
                        state[phase + "_done"] = True
                        state[phase + "_cursor"] = cursor
                        save(state)
                        return
                    if reason != "requires_action":
                        raise RuntimeError("Turn ended: " + str(reason))
                if event["type"] in {"session.status_terminated", "session.deleted"}:
                    raise RuntimeError("Session terminated")
                state[phase + "_cursor"] = cursor
                save(state)
            log = worker_logs(box)
            claimed = [x for x in log if x["kind"] == "work_claimed"]
            if claimed:
                state["work_id"] = claimed[-1]["work_id"]
                save(state)
            error = [x for x in log if x["kind"] == "worker_error"]
            if error:
                raise RuntimeError(error[-1]["error"])
            time.sleep(0.6)
    raise TimeoutError("任务超时；使用 resume 继续读取，或 logs 排查")


def task(config, values, state, phase, observe=False):
    if state.get(phase + "_message_id"):
        raise ValueError("任务已记录，使用 resume 或 verify")
    previous = {"revise": "collect", "append": "revise"}.get(phase)
    if previous and not state.get(previous + "_verified"):
        raise ValueError("先完成上一轮任务及验收")
    client = bind_trace(api(config, values), state, values, mutations_only=True)
    if observe:
        events = list(
            client.events(
                state["session_id"],
                state.get(previous + "_cursor") if previous else None,
            )
        )
        messages = [e for e in events if e["type"] == "user.message"]
        if len(messages) != 1:
            raise ValueError("应有且仅有一条控制台新任务")
        message = messages[0]
    else:
        response = client.send(
            state["session_id"],
            [
                {
                    "type": "user.message",
                    "content": [{"type": "text", "text": prompt(phase)}],
                }
            ],
            state["run_id"] + "-" + phase,
        )
        message = response["data"][0]
    state[phase + "_message_id"] = message["id"]
    state["active_phase"] = phase
    save(state)
    emit("task_submitted", phase=phase, source="console" if observe else "API")
    wait_turn(config, values, state, phase)
    verify(config, values, state, phase)
