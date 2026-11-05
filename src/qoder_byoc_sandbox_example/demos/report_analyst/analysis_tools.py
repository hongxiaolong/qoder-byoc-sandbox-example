# SPDX-License-Identifier: Apache-2.0
"""Agent 数据分析工具及产物校验。

演示目标：现场执行 Agent 生成的分析代码，产出可交互图表和 Excel，核对真实 CSV。
执行流程：写入并执行 Python；核算输出；发布新版本；追加现场记录后再次计算和导出。
能力体现：动态代码执行、同一环境连续任务、可下载文件和可核对的变化。
"""

import json
import sys
import time

from .data import read_rows, csv_bytes, sha, atomic_json, check_analysis, selected
from qoder_byoc_sandbox_example.environments.self_hosted.tools import Tools


class AnalysisTools(Tools):
    def __init__(self, workdir):
        super().__init__(workdir)
        self.observe = lambda *_args, **_values: None
        (self.root / "artifacts").mkdir(exist_ok=True)
        (self.root / "evidence").mkdir(exist_ok=True)

    def tool_run_python(self, args):
        code = args["code"]
        if len(code) > 50000:
            raise ValueError("Python source exceeds demo limit")
        (self.root / "analysis.py").write_text(code)
        self.observe("python_dispatch", role="analysis", detail="Worker 向 Python 执行器提交代码")
        result = self.tool_bash(
            {"command": f"{sys.executable} analysis.py", "timeout": 90000}
        )
        self.observe("python_return", role="analysis", detail="Python 执行器向 Worker 返回结果")
        return result

    def tool_append_record(self, args):
        category = args["category"]
        if not category or len(category) > 60:
            raise ValueError("Category length must be 1–60")
        from decimal import Decimal

        amount = Decimal(str(args["amount"]))
        if not amount.is_finite() or abs(amount) > 10000000:
            raise ValueError("Amount exceeds demo range")
        path = self.root / "input.csv"
        original = path.read_bytes()
        rows = read_rows(original)
        record = dict(
            date=args.get("date", "2026-10-29"),
            category=category,
            region=args.get("region", "华东"),
            amount=f"{amount:.2f}",
        )
        rows.append(record)
        data = csv_bytes(rows)
        read_rows(data)
        path.write_bytes(data)
        evidence = {
            "before_rows": len(rows) - 1,
            "after_rows": len(rows),
            "before_sha256": sha(original),
            "after_sha256": sha(data),
            "record": record,
        }
        atomic_json(self.root / "evidence/append.json", evidence)
        return json.dumps(evidence, ensure_ascii=False), False

    def tool_publish_dashboard(self, args):
        source = self.path(args.get("analysis_file", "analysis.json"))
        result = json.loads(source.read_text())
        rows = read_rows(self.root / "input.csv")
        expected = check_analysis(result, rows)
        identity = json.loads((self.root / "identity.json").read_text())
        old = self.root / "artifacts/dashboard.json"
        revision = (
            json.loads(old.read_text()).get("revision", 0) + 1 if old.exists() else 1
        )
        published = {
            **result,
            **expected,
            "identity": identity,
            "revision": revision,
            "input_sha256": sha((self.root / "input.csv").read_bytes()),
            "published_at": time.time(),
            "independently_checked": True,
        }
        atomic_json(old, published)
        atomic_json(self.root / "artifacts/analysis.json", published)
        atomic_json(
            self.root / "evidence/publication.json",
            {
                "revision": revision,
                "analysis_sandbox_id": identity["analysis_sandbox_id"],
                "input_sha256": published["input_sha256"],
                "row_count": expected["row_count"],
                "total_amount": expected["total_amount"],
                "checked": True,
            },
        )
        return json.dumps(
            {
                "revision": revision,
                **expected,
                "dashboard": "/dashboard",
                "analysis_sandbox_id": identity["analysis_sandbox_id"],
            },
            ensure_ascii=False,
        ), False

    def tool_export_artifacts(self, args):
        import pandas as pd
        import plotly.graph_objects as go

        current = json.loads((self.root / "artifacts/dashboard.json").read_text())
        rows = read_rows(self.root / "input.csv")
        check_analysis(current, rows)
        rows = selected(
            rows, excluded=current.get("filters", {}).get("exclude_categories", [])
        )
        target = self.root / "artifacts"
        (target / "data.csv").write_bytes(csv_bytes(rows))
        with pd.ExcelWriter(target / "analysis.xlsx", engine="openpyxl") as writer:
            pd.DataFrame(rows).to_excel(writer, sheet_name="Data", index=False)
            pd.DataFrame(
                list(current["category_totals"].items()), columns=["category", "amount"]
            ).to_excel(writer, sheet_name="Categories", index=False)
            pd.DataFrame(
                [
                    {
                        "sandbox_id": current["identity"]["analysis_sandbox_id"],
                        "row_count": current["row_count"],
                        "total_amount": current["total_amount"],
                    }
                ]
            ).to_excel(writer, sheet_name="Evidence", index=False)
        chart = go.Figure(
            go.Bar(
                x=list(current["category_totals"]),
                y=list(current["category_totals"].values()),
                marker_color="#2563eb",
            )
        )
        chart.update_layout(
            title=args.get("title", "报表分析"), template="plotly_white"
        )
        chart.write_html(target / "report.html", include_plotlyjs=True)
        files = [
            {
                "name": n,
                "bytes": (target / n).stat().st_size,
                "sha256": sha((target / n).read_bytes()),
                "url": "/files/" + n,
            }
            for n in ["data.csv", "analysis.xlsx", "analysis.json", "report.html"]
        ]
        atomic_json(self.root / "evidence/exports.json", files)
        return json.dumps(files, ensure_ascii=False), False

    def tool_runtime_evidence(self, args):
        identity = json.loads((self.root / "identity.json").read_text())
        result = {
            "identity": identity,
            "python": sys.version.split()[0],
            "workdir": str(self.root),
            "files": sorted(
                p.name for p in self.root.iterdir() if not p.name.startswith(".")
            ),
        }
        return json.dumps(result, ensure_ascii=False), False
