#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run a real command with bounded terminal feedback and a private full log.

The bar is indeterminate: pip does not expose an accurate total percentage.
Stage labels only advance when corresponding pip output is observed.
"""

import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
import unicodedata


def display_width(text):
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)


def fit_line(text, columns):
    """Leave a spare column so an updating line never wraps the terminal."""
    limit = max(0, columns - 1)
    if display_width(text) <= limit:
        return text
    result, used = [], 0
    for char in text:
        width = display_width(char)
        if used + width > max(0, limit - 1):
            break
        result.append(char)
        used += width
    return "".join(result) + ("…" if limit else "")


def clean_output(line):
    line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line).rstrip("\r\n")
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[REDACTED]@", line)


def pip_stage(line, previous="准备安装环境"):
    if "Successfully installed" in line or "Successfully built" in line:
        return "完成当前安装步骤"
    if "Installing collected packages" in line or "Attempting uninstall" in line:
        return "安装项目与依赖"
    if any(part in line for part in ["Building wheel", "Building editable", "Preparing editable", "Preparing metadata"]):
        return "构建项目安装包"
    if "Downloading" in line:
        return "下载所需依赖"
    if any(part in line for part in ["Installing build dependencies", "Getting requirements", "Collecting", "Obtaining", "Processing"]):
        return "解析与检查依赖"
    if "Requirement already satisfied" in line:
        return "复用已安装依赖"
    return previous


def operation_stage(line, previous):
    if "推送分析镜像" in line or "The push refers" in line:
        return "推送分析镜像到 ACR"
    if "重试同一镜像" in line:
        return "连接中断，正在重试上传"
    if "template" in line.lower() and "ready" in line.lower():
        return "云端模板已就绪"
    if "Analysis template" in line:
        return "等待云端模板构建"
    if line.startswith("#") and any(word in line for word in ["building", "[internal]", "FROM", "exporting"]):
        return "构建分析镜像（复用可用缓存）"
    try:
        event = json.loads(line)
        return {"sandboxes_created":"两个 Sandbox 已创建，正在连接 Qoder", "setup_ready":"Qoder 会话与执行环境已就绪", "doctor_pass":"Qoder 连接已验证", "cleanup_pass":"本轮运行资源已回收"}.get(event.get("kind"), previous)
    except (ValueError, AttributeError, TypeError):
        return previous


def signal_process_group(process, signum):
    try:
        if os.name == "posix":
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
    except ProcessLookupError:
        pass


def stop_command(process, signum=signal.SIGINT):
    """Stop the full child process group, with bounded escalation if necessary."""
    signal_process_group(process, signum)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        signal_process_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            signal_process_group(process, signal.SIGKILL)
            process.wait(timeout=2)
    # Descendants may survive a launcher that exits first. Signal the same group
    # again; this never targets the wizard's own foreground process group.
    if os.name == "posix":
        signal_process_group(process, signal.SIGTERM)


def run_command(command, log_path, label="安装项目依赖", stream=None, tty=None, mode="pip"):
    stream = stream or sys.stdout
    tty = stream.isatty() if tty is None else tty
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(log_path, 0o600)
    tail = deque(maxlen=10)
    interrupted = []
    old_handlers = {}
    started = time.monotonic()
    current = "准备安装环境" if mode == "pip" else label
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    events = queue.Queue()
    process = None

    def received_signal(signum, _frame):
        if not interrupted:
            interrupted.append(signum)
        if process is not None:
            signal_process_group(process, signum)

    def read_output():
        try:
            for line in process.stdout:
                events.put(line)
        finally:
            process.stdout.close()
            events.put(None)

    def render(frame):
        position = frame % 16
        if position > 8:
            position = 16 - position
        bar = "─" * position + "━━━" + "─" * (8 - position)
        elapsed = int(time.monotonic() - started)
        columns = shutil.get_terminal_size((80, 24)).columns
        prefix = "  " + frames[frame % len(frames)] + " "
        if columns >= 50:
            prefix += "[" + bar + "] "
        suffix = " · " + str(elapsed) + "s"
        available = max(0, columns - 1 - display_width(prefix + suffix))
        text = prefix + fit_line(current, available + 1) + suffix
        stream.write("\r\033[2K" + fit_line(text, columns))
        stream.flush()

    print("  " + label, file=stream, flush=True)
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, received_signal)
        with os.fdopen(descriptor, "w", encoding="utf-8") as log:
            try:
                process = subprocess.Popen(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", start_new_session=(os.name == "posix"),
                    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"},
                )
            except OSError as exc:
                message = clean_output(str(exc))
                log.write(message + "\n")
                print("  ✗ 无法启动：" + message, file=stream, flush=True)
                return 127
            reader = threading.Thread(target=read_output, daemon=True)
            reader.start()
            frame = 0
            closed = False
            last_plain_stage = ""
            while not closed or process.poll() is None:
                if interrupted:
                    stop_command(process, interrupted[0])
                try:
                    line = events.get(timeout=0.08)
                except queue.Empty:
                    line = ""
                if line is None:
                    closed = True
                elif line:
                    message = clean_output(line)
                    log.write(message + "\n")
                    log.flush()
                    if message:
                        tail.append(message)
                    current = pip_stage(message, current) if mode == "pip" else operation_stage(message, current)
                if tty:
                    render(frame)
                    frame += 1
                elif current != last_plain_stage:
                    print("  · " + current, file=stream, flush=True)
                    last_plain_stage = current
            reader.join(timeout=2)
            code = process.wait()
            if interrupted:
                code = 128 + interrupted[0]
            elif code < 0:
                code = 128 - code
            if tty:
                stream.write("\r\033[2K")
            elapsed = int(time.monotonic() - started)
            if code == 0:
                done = "依赖安装完成" if mode == "pip" else current + " · 完成"
                print("  ✓ " + done + " · " + str(elapsed) + "s", file=stream, flush=True)
            elif interrupted:
                print("  已停止安装，子进程已退出；下次运行可复用已安装的依赖。" if mode == "pip" else "  已停止当前操作，已完成的进度保留；重新运行向导可继续核对。", file=stream, flush=True)
            else:
                print("  ✗ " + ("安装未完成" if mode == "pip" else "当前操作未完成") + "（退出码 " + str(code) + "）：", file=stream, flush=True)
                reasons = [message for message in tail if re.search(r'error|failed|timeout|失败|错误|权限|拒绝', message, re.I)]
                for message in (reasons[-3:] or list(tail)[-3:]):
                    print("    " + message, file=stream)
            try:
                shown_path = log_path.relative_to(Path.cwd())
            except ValueError:
                shown_path = log_path
            print("  " + ("完整安装日志：" if mode == "pip" else "详细日志：") + str(shown_path), file=stream, flush=True)
            return code
    finally:
        if process is not None and process.poll() is None:
            stop_command(process)
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--label", default="安装项目依赖")
    parser.add_argument("--mode", choices=["pip", "operation"], default="pip")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("请提供要执行的命令")
    raise SystemExit(run_command(command, args.log, args.label, mode=args.mode))


if __name__ == "__main__":
    main()
