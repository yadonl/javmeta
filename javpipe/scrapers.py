"""刮削：JavBus 优先，MissAV / Jable 兜底，统一产出 NFO + 封面。

为什么要有兜底：JavBus 对一部分番号（新番、无码流出、侧链番号）会 404 或
报「您進入的網址有誤」，而 MissAV / Jable 有完整元数据。兜底让整条链路
不会因为一个站点缺页就卡住。
"""
from __future__ import annotations

import html as _html
import os
import re
from typing import Any

from bs4 import BeautifulSoup

from .sources import jable_detail, missav_detail, missav_search, jable_search

# JavBus 会拦「年龄验证 / 番号不存在 / 站点维护」这几种页面，命中就换下一个镜像
AGE_WALL_MARKS = (
    "所在地區年齡檢測", "年齡驗證", "年龄验证", "您是否年滿", "是否年滿 18",
    "您進入的網址有誤", "お探しのページ", "ページが見つかりません",
    "Age Verification", "agecheck",
)


# ── JavBus ────────────────────────────────────────────────────────────
def javbus_fetch(scraper, code: str, cfg: dict) -> dict | None:
    """抓 JavBus 详情；拿不到返回 None（不抛异常，让上层走兜底）。"""
    mirrors = cfg["scraper"].get("javbus_mirrors") or []
    for host in mirrors:
        url = f"{host.rstrip('/')}/{code.upper()}"
        try:
            # JavBus 的年龄验证：带 age=verified 直接进
            r = scraper.get(url, headers={"Cookie": "age=verified; existmag=all"}, retries=2)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        t = r.text
        if any(k in t for k in AGE_WALL_MARKS):
            continue
        d = parse_javbus(t, code, url)
        if d.get("title") or d.get("cover"):
            d["site"] = "javbus"
            return d
    return None


def parse_javbus(t: str, code: str, url: str) -> dict:
    soup = BeautifulSoup(t, "html.parser")
    d: dict[str, Any] = {"url": url, "title": "", "cover": "", "date": "",
                         "runtime": 0, "studio": "", "label": "",
                         "actresses": [], "genres": [], "fanarts": []}

    h3 = soup.select_one("div.container h3")
    if h3:
        d["title"] = h3.get_text(strip=True)

    img = soup.select_one("a.bigImage img")
    if img:
        d["cover"] = img.get("src", "") or ""
        big = soup.select_one("a.bigImage")
        if big and big.get("href"):
            d["cover"] = big["href"]

    info = soup.select_one("div.movie-information")
    if info:
        for p in info.select("p"):
            txt = p.get_text(" ", strip=True)
            if "発売日" in txt or "發行日期" in txt:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
                d["date"] = m.group(1) if m else ""
            elif "収録時間" in txt or "長度" in txt:
                m = re.search(r"(\d+)\s*分", txt)
                d["runtime"] = int(m.group(1)) if m else 0
            elif "メーカー" in txt or "製作商" in txt:
                d["studio"] = txt.split(":")[-1].strip()
            elif "レーベル" in txt or "發行商" in txt:
                d["label"] = txt.split(":")[-1].strip()

    d["genres"] = _dedup([a.get_text(strip=True)
                          for a in soup.select("div.genre a, span.genre a")])
    d["actresses"] = _dedup([a.get_text(strip=True)
                             for a in soup.select("#avatar-waterfall .avatar-box span")])
    for s in soup.select("a.sample-box")[:8]:
        if s.get("href"):
            d["fanarts"].append(s["href"])
    return d


def _dedup(xs: list[str]) -> list[str]:
    out = []
    for x in xs:
        x = re.sub(r"\s+", " ", (x or "")).strip()
        if x and x not in out:
            out.append(x)
    return out


# ── 兜底：MissAV / Jable ──────────────────────────────────────────────
def fallback_fetch(scraper, code: str, cfg: dict) -> dict | None:
    """按配置顺序试站点，把详情页元数据规范成和 JavBus 一样的结构。"""
    for site in cfg["scraper"].get("fallback") or []:
        try:
            if site == "missav":
                hits = missav_search(scraper, code, cfg.get("language", "zh"))
            elif site == "jable":
                hits = jable_search(scraper, code)
            else:
                continue
        except Exception:
            continue
        if not hits:
            continue
        try:
            if site == "missav":
                dd = missav_detail(scraper, hits[0]["url"])
            else:
                dd = jable_detail(scraper, hits[0]["url"])
        except Exception:
            continue
        mins = 0
        if hits[0].get("duration"):
            parts = [int(x) for x in re.findall(r"\d+", hits[0]["duration"])]
            if len(parts) == 3:
                mins = parts[0] * 60 + parts[1] + (1 if parts[2] >= 30 else 0)
            elif len(parts) == 2:
                mins = parts[0]
        return {"url": dd["url"], "site": dd["site"], "title": dd["title"],
                "cover": dd["cover"], "date": dd["date"], "runtime": mins,
                "studio": "", "label": "", "actresses": dd["actresses"],
                "genres": dd["genres"], "fanarts": [],
                "source_hit": hits[0]}
    return None


def scrape_meta(scraper, code: str, cfg: dict) -> dict | None:
    """先 JavBus，再兜底。返回的 dict 一定带 site / title / cover。"""
    meta = javbus_fetch(scraper, code, cfg)
    if meta:
        return meta
    return fallback_fetch(scraper, code, cfg)


# ── 落盘：NFO + 封面 ──────────────────────────────────────────────────
NFO_TMPL = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<movie>
  <title>{title}</title>
  <originaltitle>{original}</originaltitle>
  <sorttitle>{code}</sorttitle>
  <plot>{plot}</plot>
  <outline>{plot}</outline>
  <premiered>{date}</premiered>
  <releasedate>{date}</releasedate>
  <runtime>{runtime}</runtime>
  <studio>{studio}</studio>
  <set><name>{code}</name></set>
{taxonomy}
  <art>
{art}
  </art>
</movie>
"""


def write_nfo(folder: str, code: str, meta: dict, *, poster: str = "",
              fanart: str = "") -> str:
    """在 folder 里写 <CODE>.nfo + 返回路径。"""
    code = code.upper()
    title = (meta.get("title") or code).strip()
    plot = meta.get("plot") or (
        f"【發行日期】{meta.get('date','')}，【長度】{meta.get('runtime',0)}分鐘，{title}")
    genres = _dedup([code] + list(meta.get("genres") or []))
    actors = _dedup(list(meta.get("actresses") or []))
    taxonomy = ""
    if actors:
        taxonomy += "  <actor>\n" + "".join(
            f"    <name>{_html.escape(a)}</name>\n" for a in actors) + "  </actor>\n"
    taxonomy += "".join(f"  <genre>{_html.escape(g)}</genre>\n" for g in genres)
    art = ""
    if poster:
        art += f"    <poster>{poster}</poster>\n"
    if fanart:
        art += f"    <fanart>{fanart}</fanart>\n"
    nfo = NFO_TMPL.format(
        title=_html.escape(title), original=_html.escape(title), code=code,
        plot=_html.escape(plot), date=meta.get("date", "") or "",
        runtime=int(meta.get("runtime") or 0),
        studio=_html.escape(meta.get("studio", "") or ""),
        taxonomy=taxonomy, art=art)
    path = os.path.join(folder, f"{code}.nfo")
    with open(path, "w", encoding="utf-8") as f:
        f.write(nfo)
    return path


def save_cover(scraper, cover_url: str, folder: str, code: str, cfg: dict,
               *, fanart_urls=None) -> tuple[str, str]:
    """下载封面，写 <CODE>-poster.jpg 和 <CODE>-fanart-1.jpg，返回文件名对。

    fanart 优先用 JavBus 的剧照（真截图），拿不到才复用封面——只写封面两份是假的 fanart。
    """
    timeout = cfg["scraper"].get("cover_timeout", 40)

    def _grab(url: str) -> bytes:
        if not url:
            return b""
        try:
            r = scraper.get(url, retries=2, timeout=timeout)
            if r.status_code == 200 and len(r.content) >= 2000:
                return r.content
        except Exception:
            pass
        return b""

    cover = _grab(cover_url)
    if not cover:
        return "", ""
    poster = f"{code.upper()}-poster.jpg"
    fanart = f"{code.upper()}-fanart-1.jpg"
    shot = b""
    for u in (fanart_urls or [])[:3]:       # 前几张剧照试到一张能下的就行
        shot = _grab(u)
        if shot:
            break
    with open(os.path.join(folder, poster), "wb") as f:
        f.write(cover)
    with open(os.path.join(folder, fanart), "wb") as f:
        f.write(shot or cover)
    return poster, fanart
