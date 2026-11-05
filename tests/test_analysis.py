# SPDX-License-Identifier: Apache-2.0
"""演示目标：用真实临时文件验证报表分析工具的计算与交付。
执行流程：执行分析代码，追加记录，独立核算；通过 HTTP 下载并读取 Excel 与 HTML。
能力体现：发现错误分析、导出内容不一致与越界访问，避免只检查预设返回值。"""

import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request

import openpyxl

from qoder_byoc_sandbox_example.demos.report_analyst.analysis_tools import (
    AnalysisTools,
)
from qoder_byoc_sandbox_example.demos.report_analyst.data import (
    seed_csv,
    read_rows,
    selected,
    aggregate,
    csv_bytes,
    check_analysis,
)
from qoder_byoc_sandbox_example.demos.report_analyst.service import create_server


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tools = AnalysisTools(self.root)
        self.rows = selected(read_rows(seed_csv()), "2026-10", "华东")
        (self.root / "input.csv").write_bytes(csv_bytes(self.rows))
        (self.root / "source.csv").write_bytes(seed_csv())
        (self.root / "identity.json").write_text(
            json.dumps(
                {
                    "run_id": "test",
                    "analysis_sandbox_id": "analysis-a",
                    "browser_sandbox_id": "browser-b",
                }
            )
        )

    def publish(self, excluded=()):
        result = {
            **aggregate(read_rows(self.root / "input.csv"), excluded),
            "filters": {"exclude_categories": list(excluded)},
            "title": "测试",
        }
        (self.root / "analysis.json").write_text(json.dumps(result))
        out, error = self.tools.execute("publish_dashboard", {})
        self.assertFalse(error, out)
        return json.loads(out)

    def test_incorrect_model_result_is_rejected_before_publication(self):
        bad = aggregate(self.rows)
        bad["total_amount"] += 1
        (self.root / "analysis.json").write_text(json.dumps(bad))
        out, error = self.tools.execute("publish_dashboard", {})
        self.assertTrue(error)
        self.assertIn("independent", out)
        self.assertFalse((self.root / "artifacts/dashboard.json").exists())

    def test_nonfinite_and_wrong_group_values_are_rejected(self):
        for value in [float("nan"), float("inf"), -999]:
            bad = aggregate(self.rows)
            bad["category_totals"]["计算"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                check_analysis(bad, self.rows)

    def test_append_and_restore_categories_increase_total_by_exact_amount(self):
        first = self.publish()
        highest = max(first["category_totals"], key=first["category_totals"].get)
        smaller = self.publish([highest])
        self.assertLess(smaller["row_count"], first["row_count"])
        text, error = self.tools.execute(
            "append_record", {"category": "现场演示", "amount": 6666}
        )
        self.assertFalse(error, text)
        final = self.publish()
        self.assertEqual(final["row_count"], first["row_count"] + 1)
        self.assertAlmostEqual(
            final["total_amount"] - first["total_amount"], 6666, places=2
        )
        self.assertEqual(final["category_totals"]["现场演示"], 6666)

    def test_real_excel_and_offline_html_export(self):
        self.publish()
        out, error = self.tools.execute("export_artifacts", {})
        self.assertFalse(error, out)
        book = openpyxl.load_workbook(
            self.root / "artifacts/analysis.xlsx", read_only=True, data_only=True
        )
        rows = list(book["Data"].values)
        self.assertEqual(len(rows) - 1, len(self.rows))
        self.assertAlmostEqual(
            sum(float(r[3]) for r in rows[1:]),
            aggregate(self.rows)["total_amount"],
            places=2,
        )
        html = (self.root / "artifacts/report.html").read_text()
        self.assertIn("plotly.js", html)
        self.assertGreater(len(html), 100000)

    def test_agent_python_runs_and_writes_real_json(self):
        code = "import json\nfrom pathlib import Path\nPath('analysis.json').write_text(json.dumps({'executed': True}))\nprint('python-executed')"
        out, error = self.tools.execute("run_python", {"code": code})
        self.assertFalse(error, out)
        self.assertIn("python-executed", out)
        self.assertTrue(
            json.loads((self.root / "analysis.json").read_text())["executed"]
        )

    def test_python_boundary_markers_follow_validation_and_real_execution(self):
        stages = []
        self.tools.observe = lambda stage, **values: stages.append(stage)
        _, error = self.tools.execute("run_python", {"code": "x" * 50001})
        self.assertTrue(error)
        self.assertEqual(stages, [], "validation errors must not claim interpreter dispatch")
        _, error = self.tools.execute("run_python", {"code": "print('boundary-check')"})
        self.assertFalse(error)
        self.assertEqual(stages, ["python_dispatch", "python_return"])

    def test_http_filter_download_and_path_restriction(self):
        server = create_server(self.root, 0, "127.0.0.1")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(
            base + "/api/download?month=2026-10&region=%E5%8D%8E%E4%B8%9C"
        ) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(read_rows(response.read()), self.rows)
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(base + "/files/identity.json")
        self.assertEqual(exc.exception.code, 404)
        exc.exception.close()


if __name__ == "__main__":
    unittest.main()
