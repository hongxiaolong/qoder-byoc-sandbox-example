# SPDX-License-Identifier: Apache-2.0
"""Browser Sandbox 内的只读数据页代理。

模块定位：处理沙箱公网网关对 HTML 强制添加下载头的行为，保留上游访问鉴权。
主要逻辑：仅监听 Browser Sandbox 回环地址、仅代理配置的分析服务；页面去下载头，
    CSV 端点保留附件语义。令牌从环境读取，不写入页面或文件。
支撑能力：云端 Chromium 正常打开数据页，并通过真实浏览器下载文件。
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import urllib.error
import urllib.request


def create_server(upstream, token, port=8088):
    upstream = upstream.rstrip("/")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            req = urllib.request.Request(
                upstream + self.path, headers={"e2b-traffic-access-token": token}
            )
            try:
                with opener.open(req, timeout=20) as response:
                    body = response.read(12 * 1024 * 1024 + 1)
                    if len(body) > 12 * 1024 * 1024:
                        raise ValueError("Source response exceeds limit")
                    self.send_response(response.status)
                    for k, v in response.headers.items():
                        if k.lower() in {"content-type", "cache-control"} or (
                            k.lower() == "content-disposition"
                            and self.path.startswith("/api/download")
                        ):
                            self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            except urllib.error.HTTPError as exc:
                exc.close()
                self.send_error(502)
            except Exception:
                self.send_error(502)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    upstream = os.environ.pop("DEMO_UPSTREAM")
    token = os.environ.pop("DEMO_TRAFFIC_TOKEN")
    create_server(upstream, token).serve_forever()


if __name__ == "__main__":
    main()
