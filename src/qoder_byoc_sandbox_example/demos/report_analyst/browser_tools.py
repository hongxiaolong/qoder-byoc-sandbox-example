# SPDX-License-Identifier: Apache-2.0
"""云端浏览器操作与跨 Sandbox 文件传递。

演示目标：Agent 选择筛选条件，真实操作 Browser 模板中的 Chromium 下载 CSV。
执行流程：通过 CDP 访问演示数据页、选择条件、下载至浏览器沙箱，SDK 读取后写入
    分析沙箱；核对两端 SHA-256，保存截图与传递清单。
能力体现：浏览器工具执行、不同模板协作、可观察中间过程和显式文件传递。
"""

import json
import time
from pathlib import Path

from .analysis_tools import AnalysisTools
from .data import FIELDS, read_rows, sha, atomic_json


class WebTools(AnalysisTools):
    def __init__(self, workdir, browser_config, credentials):
        super().__init__(workdir)
        self.browser_config = browser_config
        self.credentials = credentials
        self.playwright = None
        self.browser = None
        self.observe = lambda *_args, **_values: None

    def tool_download_report(self, args):
        from qoder_byoc_sandbox_example.sandbox.client import Sandbox
        from playwright.sync_api import sync_playwright

        config = self.browser_config
        self.observe("browser_connect", role="browser", detail="Worker 通过 CDP 连接 Chromium")
        opts = dict(
            api_key=self.credentials["E2B_API_KEY"],
            api_url=config["api_url"],
            domain=config["domain"],
        )
        remote = Sandbox.connect(config["sandbox_id"], **opts)
        try:
            probe = remote.commands.run(
                "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8088/health",
                timeout=5,
            )
        except Exception as exc:
            from qoder_byoc_sandbox_example.sandbox.client import (
                CommandExitException,
            )

            if not isinstance(exc, CommandExitException):
                raise
            probe = exc
        if probe.stdout.strip() != "200":
            remote.files.write(
                "/tmp/demo-source-proxy.py",
                (Path(__file__).with_name("source_proxy.py")).read_bytes(),
            )
            remote.commands.run(
                "exec python3 /tmp/demo-source-proxy.py > /tmp/demo-source-proxy.log 2>&1",
                background=True,
                timeout=0,
                envs={
                    "DEMO_UPSTREAM": config["source_url"].removesuffix("/source"),
                    "DEMO_TRAFFIC_TOKEN": self.credentials["ANALYSIS_TRAFFIC_TOKEN"],
                },
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                self.check_cancelled()
                try:
                    if (
                        remote.commands.run(
                            "curl -fsS http://127.0.0.1:8088/health", timeout=5
                        ).exit_code
                        == 0
                    ):
                        break
                except Exception:
                    pass
                time.sleep(0.2)
            else:
                raise TimeoutError("Browser source proxy did not become ready")
        if self.playwright is None:
            self.check_cancelled()
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.connect_over_cdp(
                config["cdp_url"],
                headers={"X-Access-Token": self.credentials["BROWSER_ACCESS_TOKEN"]},
                timeout=30000,
            )
        context = self.browser.contexts[0]
        context.set_extra_http_headers(
            {"e2b-traffic-access-token": self.credentials["ANALYSIS_TRAFFIC_TOKEN"]}
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.set_viewport_size({"width": 1280, "height": 800})
        identity = json.loads((self.root / "identity.json").read_text())
        directory = "/tmp/report-analysis-" + identity["run_id"]
        remote.commands.run("mkdir -p " + directory)
        filename = "dataset-" + identity["run_id"] + ".csv"
        browser_path = directory + "/" + filename
        # The official image's downloads-manager extension can override the CDP
        # directory. Both candidates remain real files downloaded by Chromium.
        candidates = [browser_path, "/home/user/browsertool/downloads/" + filename]
        for candidate in candidates:
            try:
                remote.files.remove(candidate)
            except Exception as exc:
                from qoder_byoc_sandbox_example.sandbox.client import (
                    NotFoundException,
                )

                if not isinstance(exc, NotFoundException):
                    raise
        cdp = self.browser.new_browser_cdp_session()
        cdp.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": directory, "eventsEnabled": True},
        )
        page.goto(
            "http://127.0.0.1:8088/source", wait_until="networkidle", timeout=30000
        )
        self.check_cancelled()
        self.observe("browser_filter", role="browser", detail="在网页选择筛选条件")
        page.locator("#month").select_option(args.get("month", "2026-10"))
        page.locator("#region").select_option(args.get("region", "华东"))
        page.get_by_role("button", name="应用筛选", exact=True).click()
        page.wait_for_function("!document.body.dataset.loading")
        # Deliberate short visibility pause for the live audience, not readiness checking.
        time.sleep(0.8)
        page.screenshot(path=str(self.root / "artifacts/browser-frame.png"))
        page.get_by_role("button", name="下一页", exact=True).click()
        self.observe("browser_download", role="browser", detail="翻页并通过 Chromium 下载 CSV")
        page.wait_for_function("!document.body.dataset.loading")
        time.sleep(0.8)
        page.get_by_role("link", name="下载筛选后的 CSV", exact=True).click()
        deadline = time.monotonic() + 25
        data = None
        while time.monotonic() < deadline:
            self.check_cancelled()
            for candidate in candidates:
                try:
                    data = remote.files.read(candidate, format="bytes")
                    if data:
                        browser_path = candidate
                        break
                except Exception as exc:
                    from qoder_byoc_sandbox_example.sandbox.client import (
                        NotFoundException,
                    )

                    if not isinstance(exc, NotFoundException):
                        raise
            if data:
                break
            time.sleep(0.3)
        if not data:
            raise TimeoutError(
                "Browser download did not produce the expected remote CSV"
            )
        rows = read_rows(data)
        if not rows:
            raise ValueError("The selected data set is empty")
        dest = self.root / "input.csv"
        self.check_cancelled()
        self.observe("csv_transfer", role="transfer", detail="从 Browser Sandbox 读取 CSV，写入 Analysis Sandbox")
        dest.write_bytes(data)
        page.screenshot(path=str(self.root / "artifacts/browser.png"))
        evidence = {
            "browser_sandbox_id": config["sandbox_id"],
            "analysis_sandbox_id": identity["analysis_sandbox_id"],
            "source_url": config["source_url"],
            "browser_path": browser_path,
            "analysis_path": str(dest),
            "rows": len(rows),
            "bytes": len(data),
            "source_sha256": sha(data),
            "target_sha256": sha(dest.read_bytes()),
            "schema": {"date": "YYYY-MM-DD", "category": "string", "region": "string", "amount": "decimal text"},
            "sample_rows": [{field: str(row[field])[:120] for field in FIELDS} for row in rows[:3]],
            "filters": {
                "month": args.get("month", "2026-10"),
                "region": args.get("region", "华东"),
            },
            "method": "Chromium download -> Browser Sandbox file -> SDK read -> analysis Sandbox file",
        }
        atomic_json(self.root / "evidence/transfer.json", evidence)
        return json.dumps(evidence, ensure_ascii=False), False
