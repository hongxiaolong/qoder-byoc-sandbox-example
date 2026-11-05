# SPDX-License-Identifier: Apache-2.0
"""云端数据页面与分析产物服务。

演示目标：同一分析 Sandbox 提供可操作的数据源页面、实时看板和可下载产物。
执行流程：浏览器筛选并下载 CSV；Agent 计算后发布 dashboard.json；页面自动读取新版本。
能力体现：HTTP 服务真实运行在 Sandbox，页面呈现实际结果文件，保留实例与文件证据。
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .data import read_rows, selected, csv_bytes, sha


def exports_ready(root, dashboard):
    try:
        files = json.loads((root / "evidence/exports.json").read_text())
        required = {"data.csv", "analysis.xlsx", "analysis.json", "report.html"}
        if {item["name"] for item in files} != required or len(files) != len(required):
            return False
        if json.loads((root / "artifacts/analysis.json").read_text()) != dashboard:
            return False
        for item in files:
            data = (root / "artifacts" / item["name"]).read_bytes()
            if len(data) != item["bytes"] or sha(data) != item["sha256"]:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def create_server(root, port=8000, host="0.0.0.0", *, execution_log="/tmp/demo-worker/worker.jsonl"):
    root = Path(root)
    assets = Path(__file__).resolve().parent / "web"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(
            self, data, content_type="application/json; charset=utf-8", filename=None
        ):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header(
                    "Content-Disposition", f'attachment; filename="{filename}"'
                )
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            params = parse_qs(url.query)
            path = url.path
            try:
                if path == "/health":
                    return self.send(b'{"ready":true,"service":"report-analyst"}')
                if path in ("/", "/source", "/dashboard", "/browser"):
                    name = {
                        "/": "portal.html",
                        "/source": "source.html",
                        "/dashboard": "dashboard.html",
                        "/browser": "browser.html",
                    }[path]
                    return self.send(
                        (assets / name).read_bytes(), "text/html; charset=utf-8"
                    )
                static = {
                    "/assets/style.css": ("style.css", "text/css; charset=utf-8"),
                    "/assets/qoder.svg": ("qoder.svg", "image/svg+xml"),
                    "/assets/agent-sandbox.png": ("agent-sandbox.png", "image/png"),
                    "/assets/live.css": ("live.css", "text/css; charset=utf-8"),
                    **{ "/assets/" + name + ".js": (name + ".js", "text/javascript; charset=utf-8")
                        for name in ["browser", "dashboard", "source", "live"] },
                }
                if path.startswith("/assets/icons/"):
                    name = path.removeprefix("/assets/icons/")
                    icon = assets / "icons" / name
                    if "/" not in name and name.endswith(".svg") and icon.is_file():
                        return self.send(icon.read_bytes(), "image/svg+xml")
                if path in static:
                    filename, mime = static[path]
                    return self.send(
                        (assets / filename).read_bytes(), mime
                    )
                if path == "/assets/plotly.min.js":
                    from plotly.offline import get_plotlyjs

                    return self.send(
                        get_plotlyjs().encode(), "text/javascript; charset=utf-8"
                    )
                if path == "/api/identity":
                    return self.send((root / "identity.json").read_bytes())
                if path == "/api/execution":
                    from .execution import execution_snapshot
                    return self.send(json.dumps(execution_snapshot(root, execution_log), ensure_ascii=False).encode())
                if path in ("/api/source", "/api/download"):
                    rows = selected(
                        read_rows(root / "source.csv"),
                        params.get("month", [""])[0],
                        params.get("region", [""])[0],
                    )
                    if path == "/api/download":
                        identity = json.loads((root / "identity.json").read_text())
                        return self.send(
                            csv_bytes(rows),
                            "text/csv; charset=utf-8",
                            "dataset-" + identity["run_id"] + ".csv",
                        )
                    page = max(1, int(params.get("page", ["1"])[0]))
                    return self.send(
                        json.dumps(
                            {
                                "total": len(rows),
                                "page": page,
                                "rows": rows[(page - 1) * 10 : page * 10],
                            },
                            ensure_ascii=False,
                        ).encode()
                    )
                if path == "/api/dashboard":
                    p = root / "artifacts/dashboard.json"
                    current = json.loads(p.read_text()) if p.exists() else {"waiting": True}
                    current["exports_ready"] = False if current.get("waiting") else exports_ready(root, current)
                    return self.send(
                        json.dumps(current, ensure_ascii=False).encode()
                    )
                if path == "/api/evidence":
                    records = []
                    for name in ["transfer.json", "append.json", "publication.json"]:
                        p = root / "evidence" / name
                        if p.exists():
                            records.append({"kind": name, **json.loads(p.read_text())})
                    return self.send(json.dumps(records, ensure_ascii=False).encode())
                if path == "/api/frame":
                    p = root / "artifacts/browser-frame.png"
                    if p.exists():
                        return self.send(p.read_bytes(), "image/png")
                if path.startswith("/files/"):
                    name = path.removeprefix("/files/")
                    if name not in {
                        "report.html",
                        "data.csv",
                        "analysis.xlsx",
                        "analysis.json",
                        "browser.png",
                    }:
                        raise ValueError("Unknown artifact")
                    if name != "browser.png":
                        current_path = root / "artifacts/dashboard.json"
                        current = json.loads(current_path.read_text()) if current_path.exists() else {}
                        if not current or not exports_ready(root, current):
                            self.send_error(409, "Current report files are still being generated")
                            return
                    mime = (
                        "text/html; charset=utf-8"
                        if name.endswith("html")
                        else "application/octet-stream"
                    )
                    return self.send(
                        (root / "artifacts" / name).read_bytes(), mime, name
                    )
                self.send_error(404)
            except (OSError, ValueError):
                self.send_error(404)

    return ThreadingHTTPServer((host, port), Handler)


def serve(root, port=8000, host="0.0.0.0"):
    create_server(root, port, host).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/workspace")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    serve(args.root, args.port)
