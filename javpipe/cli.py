"""命令行入口：javpipe <子命令>。

    javpipe fetch SSIS-885 --dry-run    # 只探源，不下
    javpipe fetch SSIS-885              # 下到 download_root
    javpipe scrape                      # 扫下载目录，刮削入库
    javpipe run SSIS-885                # fetch + scrape 一条龙
    javpipe add https://jable.tv/videos/ssis-885/   # 从网页链接直接排片
    javpipe rescue                      # 救失败区
    javpipe status                      # 看库存
    javpipe doctor                      # 体检
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .config import load_config
from .http import make_scraper
from .library import status as lib_status
from .pipeline import doctor, fetch_one, log, scan_downloads, scan_failed


def code_from_url(u: str) -> str:
    """从 MissAV / Jable / 任意路径里抠番号。"""
    s = u.strip()
    # 站点后缀先剥：/cn/sone-543-chinese-subtitle → sone-543
    s = re.sub(r"(?i)(-(uncensored-leak|chinese-subtitles?|english-subtitles?|"
               r"reducing-mosaic|reduced-mosaic))/?$", "", s)
    s = s.rstrip("/")
    slug = s.rsplit("/", 1)[-1].split("?")[0].split("#")[0]
    # 番号形态：字母+数字（SSIS-885 / SONE543）或无码纯数字（092014_887 / 081012-097）
    m = (re.search(r"([a-zA-Z]{2,10}[-_]?\d{2,6})", slug)
         or re.search(r"(\d{4,6}[-_]\d{2,4})", slug)
         or re.search(r"([a-zA-Z]{2,10}[-_]?\d{2,6})", s))
    if not m:
        return ""
    return m.group(1).upper().replace("_", "-")


def normalize_code(x: str) -> str:
    x = x.strip().upper().replace("_", "-")
    if x.startswith("HTTP"):
        return code_from_url(x)
    return x


def cmd_fetch(args, cfg, scraper):
    codes = [normalize_code(c) for c in args.codes]
    results = []
    for c in codes:
        if not c:
            log("跳过无法识别的番号")
            continue
        log(f"=== {c} ===")
        try:
            results.append(fetch_one(cfg, scraper, c, dry_run=args.dry_run,
                                     force=args.force))
        except Exception as e:
            log(f"  ✗ {type(e).__name__}: {e}")
            results.append({"code": c, "status": "error", "error": str(e)})
    _summary(results)
    return 0


def cmd_run(args, cfg, scraper):
    cmd_fetch(args, cfg, scraper)
    if args.dry_run:
        return 0
    log("\n=== 刮削入库 ===")
    scan_downloads(cfg, scraper)
    return 0


def cmd_scrape(args, cfg, scraper):
    res = scan_downloads(cfg, scraper, dry_run=args.dry_run)
    _summary(res)
    return 0


def cmd_rescue(args, cfg, scraper):
    res = scan_failed(cfg, scraper, dry_run=args.dry_run, reset_tries=args.force)
    _summary(res)
    return 0


def cmd_status(args, cfg, scraper):
    st = lib_status(cfg)
    print(f"媒体库   {st['library']}")
    for k, v in st["categories"].items():
        print(f"  {k:<12} {v} 部")
    print(f"台账     已处理 {st['processed']} / 失败 {st['failed']}")
    print(f"待刮削   {st['download_queue']} 个目录")
    print(f"失败区   {st['failed_queue']} 个目录")
    return 0


def cmd_doctor(args, cfg, scraper):
    return doctor(cfg, scraper)


def cmd_serve(args, cfg, scraper):
    from .serve import serve
    serve(cfg, host=args.host, port=args.port, site=args.site)
    return 0


def cmd_worker(args, cfg, scraper):
    from .worker import run_loop, run_once
    if args.once:
        r = run_once(cfg, scraper, dry_run=args.dry_run, force=args.force)
        print(f"处理结果: {r}" if r else "队列里没有待办")
        return 0 if (r or {}).get("status") != "failed" else 1
    done = run_loop(cfg, scraper, max_items=args.max_items, interval=args.interval,
                    dry_run=args.dry_run, force=args.force)
    ok = sum(1 for r in done if r.get("status") in ("downloaded", "skipped"))
    print(f"本轮处理 {len(done)} 部，成功 {ok}")
    return 0


def cmd_rank(args, cfg, scraper):
    from .rank import build
    build(cfg, urls=args.from_urls or (), from_file=args.from_file or "",
          cookie=args.cookie or "", site=args.site or "",
          limit=args.limit or 0, dry_run=args.dry_run)
    return 0


def _summary(results):
    if not results:
        return
    ok = sum(1 for r in results if r.get("status") in ("downloaded", "moved", "dup"))
    print(f"\n===== 汇总 {ok}/{len(results)} 成功 =====")
    for r in results:
        print(f"  {r['code']:<14} {r.get('status'):<12} {r.get('source') or r.get('target') or ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="javpipe", description="番号 → 下载 → 刮削 → 媒体库")
    ap.add_argument("-c", "--config", default=None, help="配置文件路径（默认 ./config.yaml）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, help_ in (("fetch", cmd_fetch, "按番号下载"),
                            ("run", cmd_run, "下载 + 刮削入库"),
                            ("add", cmd_run, "从网页链接排片（同 run）")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("codes", nargs="+", help="番号或站点链接")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--force", action="store_true", help="忽略去重强制重下")
        p.set_defaults(func=fn)

    p = sub.add_parser("scrape", help="扫下载目录，刮削后入库")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_scrape)

    p = sub.add_parser("rescue", help="扫失败区，补元数据后入库")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="清空 rescue 计数（试够 3 次被跳过的那些再试一轮）")
    p.set_defaults(func=cmd_rescue)

    p = sub.add_parser("status", help="库存概览")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("doctor", help="环境体检")
    p.set_defaults(func=cmd_doctor)

    # ── 网页自助下单 ──────────────────────────────────────────────
    p = sub.add_parser("serve", help="起网页（候选池 → 勾选 → 提交）")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--site", default="", help="自定义静态页目录（默认用内置页）")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="消费队列，自动下载")
    p.add_argument("--once", action="store_true", help="只处理一个就退出（适合 crontab）")
    p.add_argument("--max-items", type=int, default=0)
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("rank", help="构建候选池（抓榜单页 或 手填清单）")
    p.add_argument("--from", dest="from_urls", action="append", metavar="URL",
                   help="榜单/列表页地址，可重复")
    p.add_argument("--from-file", default="", help="番号清单文件（一行一个）")
    p.add_argument("--cookie", default="", help="站点 Cookie（Cloudflare 站点需要）")
    p.add_argument("--site", default="", help="来源标签")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_rank)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    scraper = make_scraper(cfg.get("proxy", ""))
    return args.func(args, cfg, scraper) or 0


if __name__ == "__main__":
    sys.exit(main())
