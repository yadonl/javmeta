"""编排：探测 → 下载 → 刮削 → 入库。

一条命令跑完一部：
    javpipe fetch SSIS-885     # 只下到 download_root
    javpipe scrape             # 扫 download_root，刮削后入库
    javpipe run SSIS-885       # 上面两步连着做
    javpipe rescue             # 扫 failed_root，补元数据后入库
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from .downloaders import QBittorrent, find_videos, hls_download
from .library import (already_have, classify, clean_folder, ingest, mark_failed,
                      mark_processed)
from .scrapers import save_cover, scrape_meta, write_nfo
from .sources import bitmagnet_pick, site_detail, site_search


def log(msg: str):
    print(msg, flush=True)


def _safe_dir(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum() or c in "-_")


# ── 单部：探测 + 下载 ─────────────────────────────────────────────────
def fetch_one(cfg: dict, scraper, code: str, *, dry_run: bool = False,
              force: bool = False) -> dict:
    code = code.upper()
    t0 = time.time()
    dl_root = Path(cfg["download_root"]) / _safe_dir(code)

    if not force:
        why = already_have(cfg, code)
        if why:
            log(f"  跳过：{why}")
            return {"code": code, "status": "skipped", "why": why}

    # 1) 磁力优先（有 bitmagnet 且开着）
    if cfg["sources"].get("bitmagnet"):
        pick = bitmagnet_pick(cfg, code)
        if pick:
            log(f"  磁力命中 {pick['seeders']} 种子 / {pick['size']/2**30:.2f}GB")
            if dry_run:
                return {"code": code, "status": "dry-magnet", "torrent": pick}
            try:
                return _download_magnet(cfg, scraper, code, pick, dl_root, t0)
            except Exception as e:
                log(f"  磁力失败（{e}），转流媒体")

    # 2) 流媒体兜底
    hits = site_search(scraper, code, cfg)
    if not hits:
        log("  三路全无命中")
        return {"code": code, "status": "notfound"}
    log(f"  站点命中 {len(hits)} 条（{hits[0]['site']}）")
    if dry_run:
        return {"code": code, "status": "dry-stream", "hit": hits[0]}
    return _download_stream(cfg, scraper, code, hits[0], dl_root, t0)


def _download_magnet(cfg: dict, scraper, code: str, pick: dict, dl_root: Path, t0: float) -> dict:
    qb = QBittorrent(cfg)
    dl_root.mkdir(parents=True, exist_ok=True)
    name = pick["name"] or code
    magnet = (f"magnet:?xt=urn:btih:{pick['infoHash']}"
              f"&dn={__import__('urllib.parse', fromlist=['quote']).quote(name)}")
    if not qb.add_magnet(magnet, str(dl_root), tag=code):
        raise RuntimeError("投种失败")
    dcfg = cfg["download"]
    state, info = qb.wait(
        pick["infoHash"], timeout=int(dcfg.get("magnet_timeout", 1800)),
        stall_window=int(dcfg.get("stall_window", 180)),
        min_bps=int(dcfg.get("min_kbps", 200)) * 1024,
        poll=int(dcfg.get("poll_interval", 20)))
    if state == "done":
        main, deleted = clean_folder(cfg, str(dl_root), code)
        log(f"  磁力完成 → {main or '(无视频)'}，耗时 {time.time()-t0:.0f}s")
        vids = find_videos(str(dl_root))
        return {"code": code, "status": "downloaded", "source": "magnet",
                "folder": str(dl_root), "files": len(vids),
                "bytes": sum(sz for _f, sz in vids)}
    # 停滞/超时：清掉半成品，别占空间
    qb.delete(pick["infoHash"], delete_files=True)
    shutil.rmtree(dl_root, ignore_errors=True)
    raise RuntimeError(f"磁力{state}（{int((info or {}).get('dlspeed', 0)/1024)}KB/s）")


def _download_stream(cfg: dict, scraper, code: str, hit: dict, dl_root: Path, t0: float) -> dict:
    detail = site_detail(scraper, hit)
    dl_root.mkdir(parents=True, exist_ok=True)
    out = dl_root / f"{_safe_dir(code)}.mp4"
    log(f"  流媒体 {detail['site']} → {out.name}")
    hls_download(scraper, detail["m3u8"], str(out),
                 referer=detail.get("referer", ""),
                 workers=int(cfg["download"].get("segment_workers", 8)),
                 retries=int(cfg["download"].get("segment_retries", 5)),
                 log=log)
    # 顺手把元数据落成 NFO（省一次抓取）
    poster, fanart = save_cover(scraper, detail.get("cover", ""), str(dl_root), code, cfg)
    meta = {"title": detail.get("title", ""), "date": detail.get("date", ""),
            "runtime": 0, "genres": detail.get("genres", []),
            "actresses": detail.get("actresses", []), "studio": ""}
    write_nfo(str(dl_root), code, meta, poster=poster, fanart=fanart)
    size = out.stat().st_size
    log(f"  完成 {size/2**30:.2f}GB，耗时 {time.time()-t0:.0f}s")
    return {"code": code, "status": "downloaded", "source": detail["site"],
            "folder": str(dl_root), "files": 1, "bytes": size}


# ── 批量：刮削 + 入库 ─────────────────────────────────────────────────
def scrape_folder(cfg: dict, scraper, folder: Path, *, dry_run: bool = False,
                  move_failed: bool = True) -> dict:
    name = folder.name
    code = name.upper()
    log(f"[{code}] 刮削中…")
    meta = scrape_meta(scraper, code, cfg)
    if not meta:
        log("  JavBus 与兜底站点都没有元数据")
        if move_failed and not dry_run:
            fail = Path(cfg["failed_root"]) / _safe_dir(code)
            fail.parent.mkdir(parents=True, exist_ok=True)
            if fail.exists():
                shutil.rmtree(fail, ignore_errors=True)
            shutil.move(str(folder), str(fail))
            mark_failed(cfg, code)
            log(f"  → 已移入失败区 {fail}")
        return {"code": code, "status": "nometa"}
    log(f"  元数据来源 {meta.get('site')}：{meta.get('title','')[:60]}")

    if dry_run:
        return {"code": code, "status": "dry", "meta": meta}

    main, deleted = clean_folder(cfg, str(folder), code)
    if deleted:
        log(f"  清理无用文件 {deleted} 个")
    if not main:
        log("  目录里没有可用视频")
        return {"code": code, "status": "novideo"}

    poster, fanart = save_cover(scraper, meta.get("cover", ""), str(folder), code, cfg)
    write_nfo(str(folder), code, meta, poster=poster, fanart=fanart)

    state, target = ingest(cfg, str(folder), code, meta=meta, log=log)
    log(f"  {'✅' if state in ('moved', 'dup') else '⚠️'} {state} → {target}")
    return {"code": code, "status": state, "target": target}


def scan_downloads(cfg: dict, scraper, *, dry_run: bool = False) -> list[dict]:
    root = Path(cfg["download_root"])
    if not root.exists():
        log(f"下载目录不存在：{root}")
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        # 还在下（qB 的 .!qB 或 aria2 的 .part）就跳过
        busy = any(f.endswith((".!qB", ".part")) for _r, _ds, fs in os.walk(d) for f in fs)
        if busy:
            log(f"[{d.name}] 下载中，跳过")
            continue
        if not find_videos(str(d)):
            continue
        out.append(scrape_folder(cfg, scraper, d, dry_run=dry_run))
    return out


def scan_failed(cfg: dict, scraper, *, dry_run: bool = False) -> list[dict]:
    root = Path(cfg["failed_root"])
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        out.append(scrape_folder(cfg, scraper, d, dry_run=dry_run, move_failed=False))
    return out


# ── 体检 ──────────────────────────────────────────────────────────────
def doctor(cfg: dict, scraper) -> int:
    """检查依赖、路径、qB、站点可达性。返回 0=全绿。"""
    bad = 0
    log("== 依赖 ==")
    for mod, hint in (("curl_cffi", "pip install curl_cffi"),
                      ("bs4", "pip install beautifulsoup4"),
                      ("yaml", "pip install pyyaml")):
        try:
            __import__(mod)
            log(f"  ✓ {mod}")
        except ImportError:
            log(f"  ✗ {mod} 缺失：{hint}")
            bad += 1
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe):
            log(f"  ✓ {exe}")
        else:
            log(f"  ✗ {exe} 不在 PATH")
            bad += 1

    log("== 路径 ==")
    for key in ("download_root", "failed_root", "library_root"):
        p = Path(cfg[key])
        if p.exists():
            ro, wo = os.access(p, os.R_OK), os.access(p, os.W_OK)
            log(f"  {'✓' if ro and wo else '✗'} {key} = {p}"
                f"{'' if ro and wo else '  ← 需要读写权限（去重和入库都依赖它）'}")
            bad += 0 if (ro and wo) else 1
        else:
            ok = p.parent.exists()
            log(f"  {'✓' if ok else '✗'} {key} = {p}（不存在，父目录{'可用' if ok else '也没有'}）")
            bad += 0 if ok else 1

    log("== qBittorrent ==")
    try:
        qb = QBittorrent(cfg)
        log(f"  ✓ {qb.backend()}  save_path={qb.save_path()}")
    except Exception as e:
        log(f"  ✗ {e}")
        bad += 1

    log("== 站点 ==")
    try:
        hits = site_search(scraper, "SSIS-885", cfg)
        log(f"  {'✓' if hits else '✗'} 搜索可达（命中 {len(hits)} 条）")
        bad += 0 if hits else 1
    except Exception as e:
        log(f"  ✗ 搜索失败: {e}")
        bad += 1

    log(f"\n{'全绿' if bad == 0 else f'{bad} 项有问题'}")
    return 0 if bad == 0 else 1
