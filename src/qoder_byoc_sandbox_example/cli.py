# SPDX-License-Identifier: Apache-2.0
"""统一命令入口；按子命令加载模块，Worker 可独立于演示依赖使用。"""

import argparse


COMMAND = "qoder-byoc-sandbox-example"
DEMO_ACTIONS = {
    "doctor": "检查连接字段与 Qoder 模型接口",
    "setup": "创建演示资源",
    "resume-setup": "接续未完成的资源准备",
    "preview": "启动本机演示预览",
    "chat": "在同一会话继续自由对话",
    "task": "提交并验收一轮任务",
    "observe": "观察并验收已提交的任务",
    "resume": "接续观察当前任务",
    "verify": "独立验收任务结果",
    "status": "查看本次运行的本地记录",
    "logs": "查看 Sandbox Worker 日志",
    "cleanup": "回收本次演示资源",
    "all": "执行完整三轮演示",
}


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=COMMAND, description="Qoder BYOC Sandbox Example"
    )
    commands = parser.add_subparsers(dest="action", required=True)
    for action, description in {
        **DEMO_ACTIONS,
        "templates": "构建 Browser 或自定义分析 Sandbox 模板",
        "worker": "启动独立的 Self-hosted Sandbox Worker",
    }.items():
        commands.add_parser(action, help=description, add_help=False)
    args, remaining = parser.parse_known_args(argv)
    if args.action == "templates":
        from .sandbox.templates import main as run

        return run(remaining, prog=COMMAND + " templates")
    if args.action == "worker":
        from .environments.self_hosted.worker import main as run

        return run(remaining, prog=COMMAND + " worker")
    from .demos.report_analyst.cli import main as run

    return run([args.action, *remaining], prog=COMMAND)


if __name__ == "__main__":
    main()
