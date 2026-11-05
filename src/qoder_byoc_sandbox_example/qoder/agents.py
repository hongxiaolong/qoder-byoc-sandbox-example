# SPDX-License-Identifier: Apache-2.0
"""Agents：声明模型、system prompt、tools 和 skills；Agent Loop 由 Qoder 托管。"""


def definition(settings, tools, skill, skill_path, run_id):
    enabled = settings.get("enabled_builtin_tools", ["Read"])
    return {
        "name": settings["name_prefix"] + "-" + run_id,
        "model": settings["model"],
        "system": settings["system_prompt"].replace("{{skill_path}}", skill_path),
        "tools": [
            {
                "type": "agent_toolset_20260401",
                "enabled_tools": enabled,
                "disallowed_tools": settings.get("disallowed_builtin_tools", []),
                "configs": [
                    {"name": n, "permission_policy": {"type": "always_allow"}}
                    for n in enabled
                ],
            },
            *tools,
        ],
        "skills": [
            {
                "type": "custom",
                "skill_id": skill["id"],
                "version": skill["latest_version"],
            }
        ],
        "metadata": {"demo_run": run_id},
    }


def create(client, settings, tools, skill, skill_path, run_id):
    return client.request(
        "POST", "/agents", definition(settings, tools, skill, skill_path, run_id)
    )
