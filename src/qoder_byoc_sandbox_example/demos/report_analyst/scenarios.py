# SPDX-License-Identifier: Apache-2.0
"""演示消息保存在 config/demo.toml；核心 Worker 不读取演示输入。"""

import tomllib
from qoder_byoc_sandbox_example.settings import CONFIG_DIR


def prompt(phase):
    return tomllib.loads((CONFIG_DIR / "demo.toml").read_text())["scenarios"][phase]
