"""队列消费者：`javpipe worker` 从这里把网页上的勾选变成真的下载。

一个 worker = 顺序处理队列（同一时间只下一个，不抢带宽）。状态回写到队列文件：

    pending → downloading → done / failed

跑法（crontab 每 5 分钟一次即可，队列空了立刻退出，不占资源）：

    */5 * * * * cd /path/to/javmeta && .venv/bin/javpipe worker >> /var/log/javmeta.log 2>&1
"""
from __future__ import annotations

import time

from . import queue
from .pipeline import fetch_one, log, scan_downloads

OK = ("downloaded", "moved", "dup")
HARD_FAIL = ("notfound", "nometa", "novideo")


def run_once(cfg: dict, scraper, *, dry_run: bool = False, force: bool = False,
             scrape_after: bool = True) -> dict | None:
    """处理一个待办。返回结果 dict；队列空则返回 None。"""
    code = queue.next_pending(cfg)
    if not code:
        return None

    queue.set_status(cfg, code, "downloading")
    log(f"=== 队列取到 {code} ===")
    try:
        r = fetch_one(cfg, scraper, code, dry_run=dry_run, force=force)
    except Exception as e:                     # 下载器崩了也不能卡死队列
        queue.set_status(cfg, code, "failed", f"异常: {e}")
        return {"code": code, "status": "failed", "why": str(e)}

    st = r.get("status") or ""
    if st in ("skipped",):
        queue.set_status(cfg, code, "done", r.get("why") or "已在库里，跳过")
        return r
    if dry_run or st.startswith("dry"):
        queue.set_status(cfg, code, "pending", f"dry-run: {st}")   # 探源不算真下
        log(f"   dry-run({st})，队列保持 pending")
        return r
    if st not in OK:
        queue.set_status(cfg, code, "failed", st or "unknown")
        log(f"   ✗ {code} {st}")
        return r

    queue.set_status(cfg, code, "done", r.get("source") or r.get("target") or "")
    log(f"   ✓ {code} 下载完成")
    if scrape_after:
        try:
            scan_downloads(cfg, scraper)
        except Exception as e:                 # 刮削失败不影响「已下」的事实
            log(f"   刮削异常（不致命）: {e}")
    return r


def run_loop(cfg: dict, scraper, *, max_items: int = 0, interval: float = 5,
             dry_run: bool = False, force: bool = False) -> list[dict]:
    """连续消费直到队列空 / 达到 max_items。"""
    done: list[dict] = []
    while True:
        r = run_once(cfg, scraper, dry_run=dry_run, force=force)
        if r is None:
            break
        done.append(r)
        if max_items and len(done) >= max_items:
            break
        time.sleep(interval)
    if not done:
        log("队列里没有待办")
    return done
