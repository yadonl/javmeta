"""网页自助下单：一个端口，一堆封面，勾完提交，剩下的自动下载。

    javpipe serve                      # 0.0.0.0:8892，用内置页面
    javpipe serve --port 9000 --site ./my_site   # 换端口 / 换成自己的静态页

页面只做三件事：**看（候选池）、勾、提交**。真正的下载由 `javpipe worker` 干，
两者通过队列文件解耦——网页挂了不影响下载，worker 停了网页照样能收单。

接口（也可以自己写页面调）：

    GET  /api/list    候选池 + 每部是否已入库 / 在队列里
    GET  /api/state   队列计数与每部状态
    POST /api/queue   {"items": [{"code": "SONE-682"}]}  →  {"accepted": 1}
                      配了 web.token 时要带 ?token=xxx 或 X-Token 头
"""
from __future__ import annotations

import json
import os
import re
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import queue, rank
from .library import library_index

BUILTIN_SITE = Path(__file__).parent / "web"


class Handler(SimpleHTTPRequestHandler):
    cfg: dict = {}
    site: Path = BUILTIN_SITE

    def __init__(self, *a, **kw):          # 静态根指向 site_dir（内置页或自定义页）
        super().__init__(*a, directory=str(type(self).site), **kw)

    # ── 工具 ──────────────────────────────────────────────────────────
    def log_message(self, fmt, *a):        # 别把每条请求刷进日志
        pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        token = (self.cfg.get("web") or {}).get("token") or ""
        if not token:
            return True
        q = parse_qs(urlparse(self.path).query)
        got = (q.get("token") or [""])[0] or self.headers.get("X-Token", "")
        return got == token

    # ── 路由 ──────────────────────────────────────────────────────────
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/list":
            return self._api_list()
        if path == "/api/state":
            return self._api_state()
        if path in ("/", "/index.html") and not (self.site / "index.html").exists():
            return self._json({"error": "no page"}, 404)
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/queue":
            return self._json({"error": "not found"}, 404)
        if not self._authorized():
            return self._json({"error": "bad token"}, 403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        codes = [it.get("code") for it in (payload.get("items") or []) if isinstance(it, dict)]
        if not codes and payload.get("code"):
            codes = [payload["code"]]
        added = queue.enqueue(self.cfg, codes, note="web")
        return self._json({"accepted": len(added), "skipped": len(codes) - len(added),
                           "queued": added})

    # ── 接口实现 ──────────────────────────────────────────────────────
    def _api_list(self):
        data = rank.load(self.cfg)
        have = library_index(self.cfg)
        qstate = queue.latest(self.cfg)
        items = []
        for it in data.get("items", []):
            code = (it.get("code") or "").upper()
            if not code:
                continue
            st = (qstate.get(code) or {}).get("status") or ""
            items.append({**it, "code": code,
                          "in_library": code in have,
                          "queue_status": st})
        self._json({"built": data.get("built", 0), "count": len(items), "items": items})

    def _api_state(self):
        st = queue.latest(self.cfg)
        self._json({"counts": queue.counts(self.cfg),
                    "items": {k: {"status": v.get("status"), "note": v.get("note", "")}
                              for k, v in st.items()}})


def serve(cfg: dict, *, host: str | None = None, port: int | None = None,
          site: str = "", log=print) -> None:
    web = cfg.get("web") or {}
    host = host or web.get("host") or "0.0.0.0"
    port = int(port or web.get("port") or 8892)
    site_dir = Path(site or web.get("site_dir") or BUILTIN_SITE).expanduser()

    Handler.cfg = cfg
    Handler.site = site_dir

    httpd = ThreadingHTTPServer((host, port), Handler)
    ip = host if host != "0.0.0.0" else "127.0.0.1"
    log(f"网页自助下单已启动： http://{ip}:{port}")
    log(f"  候选池 {rank.candidates_path(cfg)}")
    log(f"  队列   {queue.queue_path(cfg)}")
    log(f"  页面   {site_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        httpd.server_close()
