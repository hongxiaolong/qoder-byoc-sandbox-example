# SPDX-License-Identifier: Apache-2.0
"""集中读取 config/ 下的人工配置；运行产物统一写入 var/。"""

import json
import os
from pathlib import Path
import tomllib
from dotenv import dotenv_values

ROOT = Path(
    os.environ.get("QODER_SANDBOX_HOME", Path(__file__).resolve().parents[2])
).resolve()
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "var/state"
RESULTS_DIR = ROOT / "var/results"


def credentials(filename=".env"):
    return {**dotenv_values(CONFIG_DIR / filename, interpolate=False)}


def load(required=None):
    config = tomllib.loads((CONFIG_DIR / "app.toml").read_text())
    for name in ["agent.toml", "demo.toml"]:
        p = CONFIG_DIR / name
        if p.exists():
            config.update(tomllib.loads(p.read_text()))
    file = credentials()
    keys = ["QODER_ACCESS_TOKEN", "E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN"]
    values = {k: file.get(k) or os.environ.get(k) or "" for k in keys}
    required = keys if required is None else required
    missing = [k for k in required if not values.get(k)]
    if missing:
        raise ValueError("请填写 config/.env：" + ", ".join(missing))
    return config, values


def state_file():
    return STATE_DIR / "run.json"


def save(state):
    path = state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)


def state_for(config, values):
    state = json.loads(state_file().read_text())
    if (
        state["api_url"] != values["E2B_API_URL"]
        or state["qoder_url"] != config["qoder"]["base_url"]
    ):
        raise ValueError("运行记录与当前 Endpoint 不匹配")
    return state


def safe_error(error, values):
    message = str(error)
    for key, value in values.items():
        if value and any(k in key for k in ["TOKEN", "KEY", "PASSWORD", "SECRET"]):
            message = message.replace(value, "[REDACTED]")
    return message
