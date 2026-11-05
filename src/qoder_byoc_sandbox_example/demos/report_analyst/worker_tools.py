# SPDX-License-Identifier: Apache-2.0
"""可选演示工具提供器；通用 Worker 通过配置加载，不反向依赖此模块。"""

import os
from .browser_tools import WebTools


def create_tools(config):
    names = ("E2B_API_KEY", "BROWSER_ACCESS_TOKEN", "ANALYSIS_TRAFFIC_TOKEN")
    return WebTools(
        config["workdir"],
        config["browser"],
        {name: os.environ.pop(name) for name in names},
    )
