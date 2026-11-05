# SPDX-License-Identifier: Apache-2.0
"""Environments：注册 Session 使用的自托管执行环境；Worker 实现在 environments/self_hosted/。"""


def create(client, name, run_id, settings):
    if settings.get("type", "self_hosted") != "self_hosted":
        raise ValueError("本项目实现 self_hosted Environment")
    config = {"type": "self_hosted"}
    if settings.get("setup_script"):
        config["setup_script"] = settings["setup_script"]
    return client.request(
        "POST",
        "/environments",
        {"name": name, "config": config, "metadata": {"demo_run": run_id}},
    )
