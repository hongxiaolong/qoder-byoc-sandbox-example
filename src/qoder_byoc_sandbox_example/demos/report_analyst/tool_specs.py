# SPDX-License-Identifier: Apache-2.0
"""Qoder 工具契约：名称、用途与输入 Schema。"""


def tool(name, description, properties, required=()):
    return {
        "type": "custom",
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool("runtime_evidence", "Read workspace identity and real Sandbox IDs only when not already known. Reuse IDs from prior successful results; download_report also returns both IDs.", {}),
    tool(
        "download_report",
        "Operate Chromium in the Browser Sandbox, filter and download CSV to analysis input.csv. Returns both Sandbox IDs, matching file hashes, schema and up to three sample rows so analysis can normally run without a separate inspection call.",
        {
            "month": {"type": "string", "enum": ["2026-09", "2026-10"]},
            "region": {"type": "string", "enum": ["全部", "华东", "华南", "华北"]},
        },
        ["month", "region"],
    ),
    tool(
        "run_python",
        "Execute your generated Python code in the analysis Sandbox. Read input.csv and write analysis.json following the skill schema. Use download_report's schema/sample to normally analyze in one execution; inspect or retry when genuinely needed. pandas, plotly, openpyxl are installed.",
        {"code": {"type": "string"}},
        ["code"],
    ),
    tool(
        "publish_dashboard",
        "Independently validate analysis.json against input.csv and publish a new dashboard revision. Returns checked totals and grouped data.",
        {"analysis_file": {"type": "string"}},
    ),
    tool(
        "append_record",
        "Append exactly one user-requested record to input.csv; returns before/after count and hash. Call once per requested record.",
        {
            "category": {"type": "string"},
            "amount": {"type": "number"},
            "date": {"type": "string"},
            "region": {"type": "string"},
        },
        ["category", "amount"],
    ),
    tool(
        "export_artifacts",
        "Export checked CSV, Excel, JSON and standalone interactive HTML. Return actual file sizes, hashes and download links.",
        {"title": {"type": "string"}},
    ),
]
