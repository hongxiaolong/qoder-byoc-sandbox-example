"""Audience-facing projections of real Worker events, never the raw log."""
import json
from pathlib import Path, PurePosixPath

from qoder_byoc_sandbox_example.environments.self_hosted.tools import credential_values, redact_text


LABELS = {
    "download_report": ("browser", "筛选并下载报表"),
    "run_python": ("analysis", "运行 Python 分析"),
    "append_record": ("analysis", "追加现场记录"),
    "publish_dashboard": ("analysis", "核算并发布看板"),
    "export_artifacts": ("analysis", "生成交付文件"),
    "runtime_evidence": ("analysis", "检查运行环境"),
}


def public_text(value, limit=8000):
    text = redact_text(value, credential_values())
    return text[:limit] + ("\n…内容已截断" if len(text) > limit else "")


def public_read_path(root, args):
    """Only approved demo files and bound Skill documents may expose Read text."""
    raw = args.get("file_path")
    if not isinstance(raw, str) or ".." in PurePosixPath(raw).parts:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute():
        try:
            path = path.relative_to("/workspace")
        except ValueError:
            return None
    try:
        path = PurePosixPath((Path(root) / str(path)).resolve().relative_to(Path(root).resolve()).as_posix())
    except (ValueError, OSError, RuntimeError):
        return None
    if (len(path.parts) >= 3 and path.parts[0] == "skills" and path.name == "SKILL.md"
            or str(path) in {"input.csv", "source.csv", "analysis.py", "analysis.json",
                            "artifacts/analysis.json", "artifacts/dashboard.json"}):
        return "/workspace/" + str(path)
    return None


def execution_snapshot(root, log_path, *, identity=None, allow_read_content=True):
    root = Path(root)
    identity = identity if identity is not None else json.loads((root / "identity.json").read_text())
    calls = {}
    runtime = []
    path = Path(log_path)
    truncated = False
    if path.exists():
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - 2 * 1024 * 1024))
            if size > 2 * 1024 * 1024:
                truncated = True
                stream.readline()
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue  # A concurrent write may not have completed yet.
                if not isinstance(event, dict):
                    continue
                tool = event.get("tool")
                key = event.get("tool_use_id")
                if event.get("session_id") != str(identity.get("qoder_session_url", "")).rsplit("/", 1)[-1]:
                    continue
                kind = event.get("kind")
                if kind in {"worker_ready", "worker_polling", "work_poll", "work_ack", "work_claimed", "heartbeat_confirmed",
                            "heartbeat_failed", "heartbeat_stopped", "worker_stopped", "worker_error", "tool_stage", "tool_cancelled"}:
                    runtime.append({k: public_text(event[k], 400) if isinstance(event[k], str) else event[k]
                                    for k in ["kind", "time", "work_id", "tool_use_id", "stage", "detail", "role", "error",
                                              "environment_id", "worker_id", "state", "result", "ttl_seconds", "last_heartbeat"]
                                    if k in event})
                if not isinstance(tool, str) or not key:
                    continue
                if kind in {"tool_finished", "tool_posted", "tool_cancelled"} and key not in calls:
                    role, title = LABELS.get(tool, ("analysis", "执行工具"))
                    calls[key] = dict(id=key, tool=public_text(tool, 100), role=role, title=title,
                                      message_id=event.get("message_id"), state="observed", input="", code="",
                                      work_id=public_text(event["work_id"], 256) if isinstance(event.get("work_id"), str) else None,
                                      output="较早的执行记录不在当前日志窗口内", history_incomplete=True)
                if kind == "tool_observed":
                    role, title = LABELS.get(tool, ("analysis", "执行工具"))
                    calls[key] = dict(id=key, tool=public_text(tool, 100), role=role, title=title,
                                      message_id=event.get("message_id"), observed_at=event.get("time"),
                                      work_id=public_text(event["work_id"], 256) if isinstance(event.get("work_id"), str) else None,
                                      state="observed", input="", code="", output="等待 Worker 执行")
                elif kind == "permission_wait" and key in calls:
                    calls[key]["state"] = "permission_wait"
                if event.get("kind") == "tool_start":
                    role, title = LABELS.get(tool, ("analysis", "执行工具"))
                    args = event.get("input") or {}
                    if not isinstance(args, dict):
                        continue
                    inputs = {
                        "download_report": {k: args.get(k) for k in ["month", "region"]},
                        "run_python": {"file": "/workspace/input.csv", "script": "/workspace/analysis.py"},
                        "append_record": {k: args.get(k) for k in ["date", "category", "region", "amount"]},
                        "publish_dashboard": {"file": args.get("analysis_file", "analysis.json")},
                        "export_artifacts": {"file": "artifacts/dashboard.json"},
                    }.get(tool, {})
                    # A copied raw log cannot prove the remote file's physical
                    # path or symlink target. Only the live workspace may do so.
                    read_path = public_read_path(root, args) if allow_read_content and tool.lower() == "read" else None
                    if read_path:
                        inputs = {"file_path": read_path, **{k: args[k] for k in ("offset", "limit") if k in args}}
                        title = "读取 Skill" if read_path.endswith("/SKILL.md") else "读取文件"
                    observed = calls.get(key, {}).get("observed_at")
                    work_id = (public_text(event["work_id"], 256) if isinstance(event.get("work_id"), str)
                               else calls.get(key, {}).get("work_id"))
                    calls[key] = dict(id=key, tool=public_text(tool, 100), role=role, title=title,
                                      message_id=event.get("message_id"), observed_at=observed, work_id=work_id,
                                      started_at=event.get("time"), state="running",
                                      input=public_text(json.dumps(inputs, ensure_ascii=False), 1500),
                                      code=public_text(args.get("code", "")) if tool == "run_python" else "",
                                      output="等待工具返回真实结果")
                    if read_path:
                        calls[key]["read_path"] = read_path
                    calls[key]["input_summary"] = (
                        read_path if read_path else
                        f"{args.get('month', '2026-10')} · {args.get('region', '华东')}" if tool == "download_report"
                        else "analysis.py（工作目录含 input.csv）" if tool == "run_python"
                        else "input.csv + 1 条用户指定记录" if tool == "append_record"
                        else "analysis.json + input.csv" if tool == "publish_dashboard"
                        else "已核算的看板结果" if tool == "export_artifacts"
                        else "运行环境" if tool == "runtime_evidence"
                        else "工具参数请在 Qoder 会话查看"
                    )
                elif event.get("kind") == "tool_finished" and key in calls:
                    output = event.get("output", "")
                    if (tool not in LABELS and not calls[key].get("read_path")) or tool == "runtime_evidence":
                        output = "工具执行失败，请在 Qoder 会话查看" if event.get("is_error") else "工具执行完成，请在 Qoder 会话查看完整结果"
                    # Only project the business fields of structured tool results.
                    if tool in LABELS and tool not in {"run_python", "runtime_evidence"} and not event.get("is_error"):
                        try:
                            data = json.loads(output)
                            if tool == "download_report":
                                output = json.dumps({k: data.get(k) for k in ["rows", "bytes", "browser_path", "analysis_path", "source_sha256", "target_sha256"]}, ensure_ascii=False)
                            elif tool == "export_artifacts":
                                output = json.dumps([{k: f.get(k) for k in ["name", "bytes"]} for f in data], ensure_ascii=False)
                            else:
                                output = json.dumps({k: data[k] for k in ["revision", "row_count", "total_amount", "before_rows", "after_rows", "record"] if k in data}, ensure_ascii=False)
                        except (ValueError, TypeError, KeyError, AttributeError):
                            output = "工具已完成，结果格式无法摘要，请查看终端验收。"
                    calls[key].update(state="error" if event.get("is_error") else "completed",
                                      finished_at=event.get("time"), elapsed_ms=event.get("elapsed_ms"),
                                      output=public_text(output, 4000))
                    calls[key]["output_summary"] = "执行失败，展开查看原因" if event.get("is_error") else "已返回真实结果，展开查看输出"
                    if tool == "run_python" and str(output).startswith("exit_code=0"):
                        calls[key]["output_summary"] = "退出码 0 · 代码执行成功，展开查看标准输出"
                    if not event.get("is_error") and tool != "run_python":
                        try:
                            result = json.loads(output)
                            summary = {
                                "download_report": lambda: f"{result['rows']} 条数据已下载并传入分析沙箱",
                                "append_record": lambda: f"{result['before_rows']} → {result['after_rows']} 条数据（不含表头）",
                                "publish_dashboard": lambda: f"v{result['revision']} · {result['row_count']} 条 · 合计 {result['total_amount']:,.2f}",
                                "export_artifacts": lambda: " / ".join(f['name'] for f in result),
                            }[tool]()
                            calls[key]["output_summary"] = public_text(summary, 300)
                        except (ValueError, TypeError, KeyError):
                            pass
                elif kind == "tool_posted" and key in calls:
                    calls[key]["posted_at"] = event.get("time")
                    if calls[key]["state"] == "observed":
                        calls[key]["state"] = "posted"
                elif kind == "tool_cancelled" and key in calls:
                    calls[key].update(state="cancelled", cancelled_at=event.get("time"),
                                      output="Worker 已停止，工具结果未回传 QCA", output_summary="Worker 已停止 · 结果未回传")
                if key in calls and not calls[key].get("work_id") and isinstance(event.get("work_id"), str):
                    calls[key]["work_id"] = public_text(event["work_id"], 256)
    events = list(calls.values())
    public_identity = {k: public_text(v, 1000) if isinstance(v, str) else v for k, v in identity.items()
                       if k in {"run_id", "qoder_session_url", "analysis_sandbox_id", "browser_sandbox_id",
                                "analysis_template", "browser_template", "analysis_mode", "browser_template_name",
                                "browser_image", "analysis_template_name", "analysis_image", "agent_id", "environment_id",
                                "skill_id", "worker_id"}}
    # The journal has its own run binding; a copied log or stale file cannot
    # attach another run's API records, even when the Session happens to match.
    api_trace = {"requests": [], "history_incomplete": False}
    if identity.get("run_id"):
        from qoder_byoc_sandbox_example.qoder.telemetry import APITraceRecorder
        session_id = str(identity.get("qoder_session_url", "")).rsplit("/", 1)[-1]
        api_trace = APITraceRecorder(path.parent / "api-trace.jsonl", identity["run_id"],
                                     session_id=session_id).snapshot()
    return {"identity": public_identity, "api_trace": api_trace, "events": events[-300:], "runtime": runtime[-200:], "recorded": bool(events),
            "truncated": truncated or len(events) > 300 or len(runtime) > 200,
            "history_incomplete": any(event.get("history_incomplete") for event in events)}
