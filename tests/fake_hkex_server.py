"""本地假披露易：一个真的 HTTP 服务器，行为对齐真站点。

为什么要造这个而不是只用假 session：
假 session 只能验证「拿到 JSON 之后怎么处理」，验证不了
**会话建立、cookie 下发、cookie 回传**这条链路 ——
而这恰恰是最容易静默失效的一环（缓存把 warm-up 吃掉就是个例子）。

这个服务器刻意做了两件真站点会做的事：
1. 只有 `/search/titlesearch.xhtml` 才下发 cookie
2. **servlet 请求不带 cookie 就返回 403**

所以用真实的 `listing.run()` 打它，能一次性验证：
建会话 → 拿 cookie → 带 cookie 查询 → rowRange 翻页 → 去重 → 落盘。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SESSION_COOKIE = "JSESSIONID"


def make_records(day: str, n: int) -> list[dict]:
    return [{
        "NEWS_ID": f"{day.replace('-', '')}_{i}",
        "DATE_TIME": f"{day} 08:{i:02d}",
        "STOCK_CODE": f"{i % 5 + 1:05d}",
        "STOCK_NAME": f"公司{i}",
        "TITLE": f"公告{i} &amp; 附件<div class='tip'>提示</div>",
        "FILE_LINK": f"/listedco/listconews/sehk/2026/0105/{day}_{i}.pdf",
        "FILE_INFO": "PDF 500KB",
    } for i in range(n)]


class _Handler(BaseHTTPRequestHandler):
    # 由 serve() 注入
    day_data: dict[str, int] = {}
    require_cookie: bool = True
    hits: dict[str, int] = {}

    def log_message(self, *args):      # 别把测试输出刷屏
        pass

    def _send(self, code: int, body: bytes, ctype: str, cookie: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", f"{SESSION_COOKIE}=abc123; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        type(self).hits[parsed.path] = type(self).hits.get(parsed.path, 0) + 1

        # 检索页：下发 cookie
        if parsed.path == "/search/titlesearch.xhtml":
            self._send(200, b"<html><body>search page</body></html>",
                       "text/html; charset=utf-8", cookie=True)
            return

        # PDF：也要 cookie（对齐 download_pdf 复用 self.session 的行为）
        if parsed.path.endswith(".pdf"):
            if self.require_cookie and SESSION_COOKIE not in (
                    self.headers.get("Cookie") or ""):
                self._send(403, b"no session", "text/plain")
                return
            self._send(200, b"%PDF-1.4\n%%EOF\n", "application/pdf")
            return

        # 检索接口：没有 cookie 就拒绝 —— 真站点的关键行为
        if parsed.path == "/search/titleSearchServlet.do":
            if self.require_cookie and SESSION_COOKIE not in (
                    self.headers.get("Cookie") or ""):
                self._send(403, b"session required", "text/plain")
                return

            day = qs.get("fromDate", [""])[0]
            day_iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
            total = type(self).day_data.get(day_iso, 0)
            row_range = int(qs.get("rowRange", ["100"])[0])

            recs = make_records(day_iso, total)[:row_range]
            body = json.dumps({
                "hasNextRow": total > row_range,
                "loadedRecord": len(recs),
                "recordCnt": total,
                "result": json.dumps(recs, ensure_ascii=False),
            }, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return

        self._send(404, b"not found", "text/plain")


class FakeHKEXServer:
    """上下文管理器：`with FakeHKEXServer({...}) as base_url:`"""

    def __init__(self, day_data: dict[str, int], *, require_cookie: bool = True):
        _Handler.day_data = dict(day_data)
        _Handler.require_cookie = require_cookie
        _Handler.hits = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def hits(self) -> dict[str, int]:
        return dict(_Handler.hits)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
