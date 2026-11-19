"""候选池构建：抓站点列表页 → 出 `candidates.json`，给网页勾选用。

    javpipe rank --from https://example.tv/new-release/ --cover-prefix ""
    javpipe rank --from-file my_codes.txt          # 完全手工，不联网

为什么要单独一步：榜单源的可达性取决于你自己的网络（多数站点套 Cloudflare），
所以「抓不到」是常态、不是 bug —— 抓不到就报错退出，不编数据。
抓不到时还有两条路：`--from-file` 手填番号，或者把任意工具导出的清单塞进候选池。

产物（`web.candidates` 指向它）：

    {"built": 1763600000,
     "items": [{"code": "SONE-682", "title": "...", "site": "...",
                "url": "https://...", "cover": "https://..."}]}
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .http import make_scraper
from .library import library_index

# 番号形态：ABC-123 / SONE-682 / 092014_887 / 081012-097
_CODE_RE = re.compile(r"\b([A-Za-z]{2,10})[-_ ]?(\d{2,6})\b")
_UNCENSORED_RE = re.compile(r"\b(\d{4,6})[-_](\d{2,4})\b")
_IMG_RE = re.compile(r"""<img[^>]+(?:data-src|data-original|src)=["']([^"']+)["']""", re.I)
_A_RE = re.compile(r"""<a\s+[^>]*href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def candidates_path(cfg: dict) -> Path:
    raw = (cfg.get("web") or {}).get("candidates") or "~/.javpipe/candidates.json"
    p = Path(raw).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def code_from_text(*texts: str) -> str:
    """从标题/链接里抠番号（先认 XXXX-000，再认无码的纯数字形态）。"""
    for t in texts:
        if not t:
            continue
        m = _CODE_RE.search(t)
        if m:
            return f"{m.group(1).upper()}-{m.group(2)}"
        m = _UNCENSORED_RE.search(t)
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return ""


def parse_listing(html: str, *, base: str = "", limit: int = 0) -> list[dict]:
    """从任意列表页里抠出「链接 + 图 + 标题」组成的卡片。"""
    from urllib.parse import urljoin

    items, seen = [], set()
    for href, inner in _A_RE.findall(html or ""):
        img = _IMG_RE.search(inner)
        text = _TAG_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        alt = ""
        m_alt = re.search(r"""alt=["']([^"']+)["']""", inner, re.I)
        if m_alt:
            alt = m_alt.group(1)
        url = urljoin(base, href) if base else href
        code = code_from_text(text, alt, url)
        if not code or code in seen:
            continue
        seen.add(code)
        items.append({
            "code": code,
            "title": text or alt,
            "url": url,
            "cover": img.group(1) if img else "",
        })
        if limit and len(items) >= limit:
            break
    return items


def build(cfg: dict, *, urls=(), from_file: str = "", cookie: str = "",
          site: str = "", limit: int = 0, dry_run: bool = False, log=print) -> dict:
    """抓列表页 / 读清单 → 写 candidates.json（已入库的自动剔掉）。"""
    items: list[dict] = []
    have = library_index(cfg)

    if from_file:
        txt = Path(from_file).expanduser().read_text(encoding="utf-8")
        for line in txt.splitlines():
            code = code_from_text(line) or line.strip().upper()
            if code and code not in have:
                items.append({"code": code, "title": "", "url": "", "cover": "",
                              "site": site or "manual"})

    if urls:
        scraper = make_scraper(cfg.get("proxy", ""))
        for u in urls:
            try:
                kw = {"headers": {"Cookie": cookie}} if cookie else {}
                r = scraper.get(u, **kw)
            except Exception as e:
                log(f"[WARN] {u} 抓取失败: {e}")
                continue
            if r.status_code != 200:
                log(f"[WARN] {u} HTTP {r.status_code}（多半是 Cloudflare，需要 --cookie）")
                continue
            got = parse_listing(r.text, base=u, limit=limit)
            log(f"[OK] {u} 解析到 {len(got)} 个卡片")
            for it in got:
                if it["code"] in have or any(x["code"] == it["code"] for x in items):
                    continue
                it["site"] = site or (u.split("/")[2] if "//" in u else "")
                items.append(it)
            if limit and len(items) >= limit:
                break

    if not items:
        log("[WARN] 候选池为空：站点不可达就换 --from-file（手填番号清单）")
    data = {"built": int(time.time()), "items": items[:limit or None]}
    if dry_run:
        log(f"[dry-run] 候选 {len(items)} 个，不落盘")
        return data

    p = candidates_path(cfg)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"候选池已写入 {p}（{len(data['items'])} 个）")
    return data


def load(cfg: dict) -> dict:
    p = candidates_path(cfg)
    if not p.exists():
        return {"built": 0, "items": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"built": 0, "items": []}
