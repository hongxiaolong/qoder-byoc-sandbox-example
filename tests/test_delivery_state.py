# SPDX-License-Identifier: Apache-2.0
"""发布看板不能提前暴露上一版本或尚未生成的导出文件。"""

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error

from qoder_byoc_sandbox_example.demos.report_analyst.analysis_tools import AnalysisTools
from qoder_byoc_sandbox_example.demos.report_analyst.data import aggregate, csv_bytes, read_rows, seed_csv, selected
from qoder_byoc_sandbox_example.demos.report_analyst.service import create_server


class DeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        rows = selected(read_rows(seed_csv()), "2026-10", "华东")
        (self.root / "input.csv").write_bytes(csv_bytes(rows))
        (self.root / "identity.json").write_text(json.dumps({"run_id": "delivery-test", "analysis_sandbox_id": "a", "browser_sandbox_id": "b"}))
        (self.root / "analysis.json").write_text(json.dumps(aggregate(rows)))
        self.tools = AnalysisTools(self.root)
        self.server = create_server(self.root, 0, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def dashboard(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/api/dashboard") as response:
            return json.load(response)

    def execute(self, name):
        text, error = self.tools.execute(name, {})
        self.assertFalse(error, text)

    def test_downloads_wait_for_export_of_the_current_revision(self):
        self.assertFalse(self.dashboard()["exports_ready"])
        self.execute("publish_dashboard")
        self.assertFalse(self.dashboard()["exports_ready"])
        self.execute("export_artifacts")
        self.assertTrue(self.dashboard()["exports_ready"])
        self.execute("publish_dashboard")
        current = self.dashboard()
        self.assertEqual(current["revision"], 2)
        self.assertFalse(current["exports_ready"])
        with self.assertRaises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/files/data.csv")
        self.assertEqual(blocked.exception.code, 409)
        blocked.exception.close()
        self.execute("export_artifacts")
        self.assertTrue(self.dashboard()["exports_ready"])
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/files/data.csv") as response:
            self.assertEqual(read_rows(response.read()), read_rows(self.root / "input.csv"))

    def test_same_size_corruption_disables_downloads(self):
        self.execute("publish_dashboard")
        self.execute("export_artifacts")
        self.assertTrue(self.dashboard()["exports_ready"])
        path = self.root / "artifacts/data.csv"
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        self.assertFalse(self.dashboard()["exports_ready"])

    def test_frontend_scripts_have_explicit_javascript_routes(self):
        for name in ["live", "dashboard", "browser", "source"]:
            with self.subTest(script=name):
                with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/assets/{name}.js") as response:
                    self.assertIn("javascript", response.headers["Content-Type"])
                    self.assertRegex(response.read(), rb"['\"]use strict['\"]")
        with urllib.request.urlopen(f"http://127.0.0.1:{self.server.server_port}/assets/qoder.svg") as response:
            self.assertEqual(response.headers["Content-Type"], "image/svg+xml")
            self.assertIn(b"<svg", response.read())
