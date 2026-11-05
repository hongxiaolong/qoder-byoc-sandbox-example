# SPDX-License-Identifier: Apache-2.0
"""阿里云 Sandbox 接入：创建、连接、命令与释放。Qoder 模块不依赖本模块。"""

from e2b import Sandbox, CommandExitException, NotFoundException

__all__ = [
    "Sandbox",
    "CommandExitException",
    "NotFoundException",
    "opts",
    "create_sandbox",
    "connect_sandbox",
    "command",
]


def opts(values):
    return dict(
        api_key=values["E2B_API_KEY"],
        api_url=values["E2B_API_URL"],
        domain=values["E2B_DOMAIN"],
        request_timeout=45,
    )


def create_sandbox(template, timeout, metadata, values, private=False):
    kwargs = {"network": {"allow_public_traffic": False}} if private else {}
    return Sandbox.create(
        template=template, timeout=timeout, metadata=metadata, **kwargs, **opts(values)
    )


def connect_sandbox(sandbox_id, values):
    return Sandbox.connect(sandbox_id, **opts(values))


def command(sandbox, cmd, timeout=90, cwd=None, user=None):
    try:
        return sandbox.commands.run(
            cmd, timeout=timeout, cwd=cwd, user=user, request_timeout=45
        )
    except CommandExitException as result:
        return result
