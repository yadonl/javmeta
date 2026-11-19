"""片源探测：磁力索引 + 流媒体站点（MissAV / Jable）的搜索与详情解析。

设计原则：每个站点只暴露两个函数 —— search(番号) 和 detail(URL)，
上层 process 不关心站点差异。
"""
from __future__ import annotations

import html as _html
import json
import re
import urllib.request
from typing import Any

from bs4 import BeautifulSoup

MISSAV_MIRRORS = ("https://missav.ai", "https://missav.ws", "https://missav.live")
JABLE_HOST = "https://jable.tv"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


# ── 番号归一化 ────────────────────────────────────────────────────────
def norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def code_matches(code: str, *texts: str) -> bool:
    c = norm(code)
    return bool(c) and any(c in norm(t) for t in texts)


# ── Dean Edwards JS unpacker（MissAV 用这个藏 m3u8）──────────────────
def unpack_js_eval(script: str) -> str | None:
    m = re.search(
        r"eval\(function\(p,a,c,k,e,d\)\{.*?\}\('(.*?)',\s*(\d+),\s*(\d+),\s*'([^']*)'\s*\.split\('\|'\)",
        script, re.DOTALL)
    if not m:
        return None
    packed, a, c, keys = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    if a <= 1 or c < 0 or c > 200000:  # 畸形参数会让 to_base 死循环
        return None

    def to_base(n: int, base: int) -> str:
        digits = "0123456789abcdefghijklmnopqrstuvwxyz"
        if n == 0:
            return "0"
        s = ""
        while n:
            s = digits[n % base] + s
            n //= base
        return s

    lookup = {to_base(i, a): (keys[i] if i < len(keys) and keys[i] else to_base(i, a))
              for i in range(c)}
    return re.sub(r"\b(\w+)\b", lambda mm: lookup.get(mm.group(0), mm.group(0)), packed)


def _clean_list(items: list[str]) -> list[str]:
    noise = ("排行", "VR", "登入", "註冊", "女優", "標籤", "類別", "全部", "登入", "注册")
    out = []
    for x in items:
        s = re.sub(r"\s+", " ", _html.unescape(x)).strip()
        if not s or len(s) > 20 or any(k in s for k in noise):
            continue
        if s not in out:
            out.append(s)
    return out


# ── MissAV ────────────────────────────────────────────────────────────
def missav_search(scraper, code: str, lang: str = "zh") -> list[dict]:
    """搜索页 → [{url,title,duration}]，只保留番号精确命中的卡片。"""
    q = urllib.parse.quote(code, safe="")
    path = f"/{lang}/search/{q}" if lang else f"/search/{q}"
    hits: list[dict] = []
    for host in MISSAV_MIRRORS:
        try:
            r = scraper.get(host + path, retries=2)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        soup = BeautifulSoup(r.content, "html.parser")
        cards = soup.select("div.thumbnail") or soup.select("div.group, article.video-item")
        for card in cards:
            a = card.select_one("a[href]")
            if not a:
                continue
            url = a.get("href", "")
            if url.startswith("/"):
                url = host + url
            if "/search/" in url:
                continue
            last = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
            if not re.search(r"\d", last):
                continue
            img = card.select_one("img")
            title = (img.get("alt", "") if img else "") or ""
            ta = card.select_one("div.my-2 a, div.truncate a")
            if ta:
                title = ta.get_text(strip=True) or title
            dur = card.select_one("span.absolute.bottom-1.right-1")
            hits.append({"url": url, "title": title,
                         "duration": dur.get_text(strip=True) if dur else "",
                         "site": "missav"})
        if hits:
            break
    good = [h for h in hits if code_matches(code, h["url"], h["title"])]
    # 无码流出排在后面：优先正片
    good.sort(key=lambda h: 1 if "uncensored" in h["url"] else 0)
    return good


def missav_detail(scraper, url: str) -> dict:
    """详情页 → 元数据 + m3u8 地址 + 需要的 Referer。"""
    r = scraper.get(url, retries=2)
    if r.status_code != 200:
        raise RuntimeError(f"MissAV 详情 HTTP {r.status_code}")
    t = r.text
    host = re.match(r"(https?://[^/]+)", url).group(1)
    d: dict[str, Any] = {"url": url, "site": "missav",
                         "referer": host + "/",
                         "m3u8": "", "cover": "", "title": "", "date": "",
                         "actresses": [], "genres": [], "duration": ""}

    m = re.search(r'og:title"\s+content="([^"]+)"', t)
    d["title"] = _html.unescape(m.group(1)).strip() if m else ""
    m = re.search(r'og:image"\s+content="([^"]+)"', t)
    d["cover"] = m.group(1) if m else ""
    m = re.search(r'name="release_date"\s+content="([\d-]+)"', t)
    d["date"] = m.group(1) if m else ""
    m = re.search(r'<meta[^>]+property="og:duration"[^>]+content="(\d+)"', t)
    if m:
        secs = int(m.group(1))
        d["duration"] = f"{secs // 60}min"
    d["actresses"] = _clean_list(re.findall(r'href="[^"]*/actresses/[^"]*"[^>]*>([^<]+)', t))
    d["genres"] = _clean_list(re.findall(r'href="[^"]*/genres/[^"]*"[^>]*>([^<]+)', t))

    for script in re.findall(r"<script[^>]*>(.*?)</script>", t, re.DOTALL):
        if "eval(function" not in script or "m3u8" not in script:
            continue
        up = unpack_js_eval(script)
        if not up:
            continue
        mm = (re.search(r"source\s*=\s*[\\']*(https?://[^'\\;\s]+\.m3u8)", up)
              or re.search(r"(https?://[^'\\;\s]+\.m3u8)", up))
        if mm:
            d["m3u8"] = mm.group(1)
            break
    if not d["m3u8"]:
        raise RuntimeError("MissAV 页面里没找到 m3u8（可能改版）")
    return d


# ── Jable ─────────────────────────────────────────────────────────────
def jable_search(scraper, code: str) -> list[dict]:
    url = f"{JABLE_HOST}/search/{code.lower()}/"
    try:
        r = scraper.get(url, retries=2)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    soup = BeautifulSoup(r.content, "html.parser")
    box = soup.find("div", id=lambda x: x and x.startswith("list_videos"))
    if box is None:
        return []
    hits = []
    for card in box.select("div.video-img-box"):
        detail = card.select_one("div.detail")
        if not detail or not detail.h6 or not detail.h6.a:
            continue
        a = detail.h6.a
        img = card.select_one("img")
        lab = card.select_one("span.label")
        hits.append({"url": a.get("href", ""),
                     "title": str(a.string or ""),
                     "thumbnail": img.get("data-src", "") if img else "",
                     "duration": lab.string if lab else "",
                     "site": "jable"})
    return [h for h in hits if code_matches(code, h["url"], h["title"])]


def jable_detail(scraper, url: str) -> dict:
    r = scraper.get(url, retries=2)
    if r.status_code != 200:
        raise RuntimeError(f"Jable 详情 HTTP {r.status_code}")
    t = r.text
    host = re.match(r"(https?://[^/]+)", url).group(1)
    d: dict[str, Any] = {"url": url, "site": "jable", "referer": host + "/",
                         "m3u8": "", "cover": "", "title": "", "date": "",
                         "actresses": [], "genres": [], "duration": ""}
    m = re.search(r'og:title"\s+content="([^"]+)"', t)
    d["title"] = _html.unescape(m.group(1)).strip() if m else ""
    m = re.search(r'og:image"\s+content="([^"]+)"', t)
    d["cover"] = m.group(1) if m else ""
    m = re.search(r"(20\d{2}-\d{2}-\d{2})", t)
    d["date"] = m.group(1) if m else ""
    d["actresses"] = _clean_list(re.findall(
        r'href="[^"]*/models/[^"]*"[^>]*>([^<]+)', t))
    d["genres"] = _clean_list(re.findall(
        r'href="[^"]*/tags/[^"]*"[^>]*>([^<]+)', t))
    m = re.search(r"(https://[^\s\"']+\.m3u8)", t)
    if not m:
        raise RuntimeError("Jable 页面里没找到 m3u8（可能改版）")
    d["m3u8"] = m.group(1)
    return d


def site_search(scraper, code: str, cfg: dict) -> list[dict]:
    """按配置顺序问一遍站点，返回候选列表（每个带 site 字段）。"""
    hits: list[dict] = []
    if cfg["sources"].get("missav", True):
        hits += missav_search(scraper, code, cfg.get("language", "zh"))
    if cfg["sources"].get("jable", True):
        hits += jable_search(scraper, code)
    return hits


def site_detail(scraper, hit: dict) -> dict:
    if hit.get("site") == "jable" or "jable" in hit["url"]:
        return jable_detail(scraper, hit["url"])
    return missav_detail(scraper, hit["url"])


# ── bitmagnet（可选）──────────────────────────────────────────────────
_BM_Q = """
query($q: String!, $res: [Int]) {
  torrentContent {
    search(input: {queryString: $q, videoResolution: $res, limit: 20}) {
      items { infoHash torrent { name size seeders } }
    }
  }
}
"""

_BM_Q_ANY = """
query($q: String!) {
  torrentContent {
    search(input: {queryString: $q, limit: 20}) {
      items { infoHash torrent { name size seeders } }
    }
  }
}
"""


def bitmagnet_search(cfg: dict, code: str, resolution: int | None = None) -> list[dict]:
    """磁力索引搜索；未配置 bitmagnet 时返回空。"""
    endpoint = cfg["sources"].get("bitmagnet") or ""
    if not endpoint:
        return []
    body = json.dumps({"query": _BM_Q if resolution else _BM_Q_ANY,
                       "variables": {"q": code, "res": [resolution]} if resolution
                       else {"q": code}}).encode()
    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": "application/json"})
    proxy = cfg.get("proxy") or ""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=40) as r:
            data = json.loads(r.read().decode())
        items = data["data"]["torrentContent"]["search"].get("items") or []
    except Exception:
        return []
    out = []
    for it in items:
        if not it.get("infoHash"):
            continue
        t = it.get("torrent") or {}
        out.append({"infoHash": it["infoHash"],
                    "name": t.get("name") or "",
                    "size": int(t.get("size") or 0),
                    "seeders": int(t.get("seeders") or 0)})
    return out


def bitmagnet_pick(cfg: dict, code: str) -> dict | None:
    """seeders 够数里挑体积最大的一条（体积≈画质）。"""
    min_seeders = int(cfg["sources"].get("min_seeders", 2))
    hits = [h for h in bitmagnet_search(cfg, code, 2160)
            or bitmagnet_search(cfg, code, 1080)
            or bitmagnet_search(cfg, code)]
    hits = [h for h in hits if h["seeders"] >= min_seeders]
    if not hits:
        return None
    hits.sort(key=lambda h: -h["size"])
    return hits[0]
