"""HTTP 层：带浏览器指纹的会话（绕 Cloudflare）+ 重试 + 代理。"""
from __future__ import annotations

import time
from typing import Any

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

try:
    import requests as _plain
except ImportError:  # pragma: no cover
    _plain = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


class Scraper:
    """轻量会话包装：curl_cffi 优先（和真 Chrome 同指纹），退回 requests。"""

    def __init__(self, proxy: str = "", impersonate: str = "chrome", timeout: int = 40):
        self.proxy = proxy
        self.timeout = timeout
        if cffi_requests is not None:
            self._s = cffi_requests.Session(impersonate=impersonate)
            if proxy:
                self._s.proxies = {"http": proxy, "https": proxy}
            self._lib = "curl_cffi"
        elif _plain is not None:  # pragma: no cover
            self._s = _plain.Session()
            if proxy:
                self._s.proxies = {"http": proxy, "https": proxy}
            self._s.headers.update({"User-Agent": UA,
                                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            self._lib = "requests"
        else:  # pragma: no cover
            raise RuntimeError("需要安装 curl_cffi 或 requests")

    def get(self, url: str, *, retries: int = 3, backoff: float = 1.5, **kwargs: Any):
        kwargs.setdefault("timeout", self.timeout)
        last = None
        for i in range(retries):
            try:
                r = self._s.get(url, **kwargs)
                if r.status_code in (403, 429, 503) and i < retries - 1:
                    time.sleep(backoff * (i + 1))
                    continue
                return r
            except Exception as e:  # 网络抖动 / 代理超时
                last = e
                if i < retries - 1:
                    time.sleep(backoff * (i + 1))
        raise RuntimeError(f"GET {url} 失败: {last}")

    def post(self, url: str, **kwargs: Any):
        kwargs.setdefault("timeout", self.timeout)
        return self._s.post(url, **kwargs)

    @property
    def backend(self) -> str:
        return self._lib


def make_scraper(proxy: str = "", **kw) -> Scraper:
    return Scraper(proxy=proxy, **kw)
