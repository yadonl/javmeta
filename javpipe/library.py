"""媒体库：分类、命名、去重、落库台账。

分类和命名规则全部走配置（config.yaml 的 library.rules / video_name），
这里只负责执行。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .downloaders import VIDEO_EXT, find_videos

LEDGER_NAME = ".javpipe-processed.json"


# ── 分辨率探测 ────────────────────────────────────────────────────────
def probe_height(path: str) -> int:
    """真实视频高度；失败返回 0（不猜）。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60).stdout
        for line in out.splitlines():
            line = line.strip().rstrip(",")
            if line.isdigit():
                return int(line)
    except Exception:
        pass
    return 0


# ── 分档 ──────────────────────────────────────────────────────────────
def classify(cfg: dict, code: str, *, filename: str = "", meta: dict | None = None,
             height: int = 0) -> str:
    """按规则决定落哪个子目录（返回相对 library_root 的路径）。

    4K 类规则不看标题文字，只看真实分辨率 —— 站点标题常常带 "4K" 字样而片子是 720p，
    按文字匹配会把一堆 1080p 塞进 4K 目录。
    """
    hay = " ".join([code or "", filename or "",
                    " ".join((meta or {}).get("genres") or []),
                    (meta or {}).get("title", "")]).upper()
    for rule in cfg["library"].get("rules") or []:
        d = rule["dir"]
        pats = rule.get("match") or []
        if "4K" in d.upper() or "2160" in d.upper():
            if height >= 2000:
                return d
            continue                      # 没到 2K 就跳过这条规则
        for p in pats:
            if p == "*":
                return d
            if p.upper() in hay:
                return d
    return "有码/无字幕"


def video_name(cfg: dict, code: str) -> str:
    tmpl = cfg["library"].get("video_name") or "{code}.mp4"
    return tmpl.format(code=code.upper())


# ── 台账 ──────────────────────────────────────────────────────────────
def ledger_path(cfg: dict) -> Path:
    return Path(cfg["download_root"]) / LEDGER_NAME


def load_ledger(cfg: dict) -> dict:
    p = ledger_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"processed": [], "failed": []}


def save_ledger(cfg: dict, data: dict):
    p = ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def mark_processed(cfg: dict, code: str):
    d = load_ledger(cfg)
    if code not in d["processed"]:
        d["processed"].append(code)
        save_ledger(cfg, d)


def mark_failed(cfg: dict, code: str):
    d = load_ledger(cfg)
    if code not in d["failed"]:
        d["failed"].append(code)
        save_ledger(cfg, d)


# rescue 次数：失败区里同一部试过几次还没元数据，记在台账里，够数就别再骚扰站点
def bump_failed_try(cfg: dict, code: str) -> int:
    d = load_ledger(cfg)
    tries = d.setdefault("failed_tries", {})
    k = code.upper()
    tries[k] = int(tries.get(k, 0)) + 1
    save_ledger(cfg, d)
    return tries[k]


def failed_tries(cfg: dict, code: str) -> int:
    return int((load_ledger(cfg).get("failed_tries") or {}).get(code.upper(), 0))


def reset_failed_tries(cfg: dict, code: str | None = None):
    d = load_ledger(cfg)
    tries = d.get("failed_tries") or {}
    if code is None:
        tries.clear()
    else:
        tries.pop(code.upper(), None)
    d["failed_tries"] = tries
    save_ledger(cfg, d)


# ── 去重 ──────────────────────────────────────────────────────────────
def library_index(cfg: dict) -> dict[str, str]:
    """扫库，返回 {番号: 相对路径}。目录名就是番号。

    跳过点开头的目录（`.verify-fail` 隔离区、用户自己的隐藏目录）—— 否则隔离区里的
    半成品会被当成「库内已有」，以后再也不重下。
    """
    root = Path(cfg["library_root"])
    idx: dict[str, str] = {}
    if not root.exists():
        return idx
    for dirpath, dirnames, _files in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for dn in dirnames:
            rel = str(Path(dirpath, dn).relative_to(root))
            idx.setdefault(dn.upper(), rel)
    return idx


def _has_video(folder: Path, min_bytes: int = 100 * 1024 * 1024) -> bool:
    return any(sz >= min_bytes for _f, sz in find_videos(str(folder)))


def already_have(cfg: dict, code: str) -> str | None:
    """返回去重原因（None = 没有）。顺序：台账 → 库 → 失败区 → 下载区。"""
    code = code.upper()
    if code in load_ledger(cfg).get("processed", []):
        return f"台账已处理: {code}"
    idx = library_index(cfg)
    if code in idx:
        return f"库内已有: {idx[code]}"
    for base in (cfg["failed_root"], cfg["download_root"]):
        p = Path(base) / code
        if p.is_dir() and _has_video(p):
            return f"{Path(base).name} 已有完整片: {code}（等刮削，不重下）"
    return None


# ── 规范化 ────────────────────────────────────────────────────────────
_PREFIXES = ("hhd800.com@", "489155.com@", "activehlj.com@", "169bbs.com@",
             "996gg.cc@", "www.")


def _bare(fn: str) -> str:
    for p in _PREFIXES:
        if fn.lower().startswith(p):
            return fn[len(p):]
    return fn


def code_from_name(name: str) -> str:
    m = re.search(r"([A-Za-z]+[-_]?\d{3,})([-_]([uU][cC]?|[cC]))?", _bare(name))
    if not m:
        return ""
    return m.group(1).upper().replace("_", "-") + (m.group(2) or "").upper()


def clean_folder(cfg: dict, folder: str, code: str, *, log=None) -> tuple[str, int]:
    """删广告、给视频统一命名。返回 (主视频文件名, 删掉的文件数)。

    只有 <`ad_max_bytes` 的广告才真删。其它视频一个都不删：既不是分段
    （<`multipart_min_bytes`）又不止一个时，多余的那几个挪到
    `<download_root>/.extras/` 隔离，让人自己看——静默删掉半个片子比占点空间糟糕得多。
    """
    libcfg = cfg["library"]
    ad_max = int(libcfg.get("ad_max_bytes", 52428800))
    multi_min = int(libcfg.get("multipart_min_bytes", 1073741824))
    folder_p = Path(folder)

    vids = [(f, sz) for f, sz in find_videos(folder)]
    if not vids:
        return "", 0
    # 小体积“视频”= 广告，删
    ads = [f for f, sz in vids if sz < ad_max]
    vids = [(f, sz) for f, sz in vids if sz >= ad_max]
    deleted = 0
    for f in ads:
        try:
            os.remove(f)
            deleted += 1
        except OSError:
            pass
    if not vids:
        return "", deleted

    big = [v for v in vids if v[1] >= multi_min]
    if len(big) >= 2:                       # 分段种子：全保留，命名 partN
        keep = sorted(big, key=lambda x: x[0].lower())
        for i, (f, _sz) in enumerate(keep, 1):
            ext = os.path.splitext(f)[1] or ".mp4"
            target = folder_p / f"{code.upper()}-part{i}{ext}"
            if Path(f) != target:
                try:
                    os.replace(f, target)
                except OSError:
                    pass
        main = f"{code.upper()}-part1{os.path.splitext(keep[0][0])[1] or '.mp4'}"
        return main, deleted

    vids.sort(key=lambda x: -x[1])
    main_src = vids[0][0]
    ext = os.path.splitext(main_src)[1]
    target = folder_p / (video_name(cfg, code)[:-4] + ext if video_name(cfg, code).endswith(".mp4")
                         else video_name(cfg, code))
    extras = [f for f, _sz in vids[1:]]      # 其它大文件：不删，挪去隔离区
    if extras:
        hold = Path(cfg.get("download_root") or ".") / ".extras" / f"{code.upper()}-{int(time.time())}"
        moved = 0
        for f in extras:
            try:
                hold.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(f, hold / os.path.basename(f))
                except OSError:
                    shutil.move(f, str(hold / os.path.basename(f)))
                moved += 1
            except OSError:
                pass
        if log:
            log(f"    另有 {moved} 个视频不是正片（≥{ad_max // 2**20}MB），没删，放在 {hold}")
    if Path(main_src) != target:
        try:
            os.replace(main_src, target)
        except OSError:
            target = Path(main_src)
    return target.name, deleted


# ── 落库 ──────────────────────────────────────────────────────────────
def ingest(cfg: dict, folder: str, code: str, *, meta: dict | None = None,
           dry_run: bool = False, log=print) -> tuple[str, str]:
    """把 folder 搬进 library_root/<分类>/<番号>/。返回 (状态, 目标路径)。"""
    code = code.upper()
    folder_p = Path(folder)
    if not folder_p.is_dir():
        return "missing", str(folder_p)

    main_name, deleted = clean_folder(cfg, folder, code, log=log)
    if deleted:
        log(f"    清理无用文件 {deleted} 个")
    if not main_name:
        return "novideo", str(folder_p)

    height = probe_height(str(folder_p / main_name))
    category = classify(cfg, code, filename=main_name, meta=meta, height=height)
    target = Path(cfg["library_root"]) / category / code
    if dry_run:
        return "dry", str(target)

    # 目标已存在：体积一致算重复，删掉源目录
    if target.exists():
        src_vids = find_videos(str(folder_p))
        dst_vids = find_videos(str(target))
        src_size = sum(sz for _f, sz in src_vids)
        dst_size = sum(sz for _f, sz in dst_vids)
        if src_vids and abs(src_size - dst_size) <= max(2 * 1024 * 1024, dst_size * 0.002):
            shutil.rmtree(folder_p, ignore_errors=True)
            return "dup", str(target)
        return "exists", str(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    before = {os.path.basename(f): sz for f, sz in find_videos(str(folder_p))}
    shutil.move(str(folder_p), str(target))
    # 搬完按字节对账：跨盘/跨网络的搬运有"报成功却没搬全"的，一律以目标端实到字节为准，
    # 对不上就不记台账（下次 rescue 还能再来一次），绝不静默当成成功。
    after = {os.path.basename(f): sz for f, sz in find_videos(str(target))}
    bad = [(n, sz, after.get(n)) for n, sz in before.items() if after.get(n) != sz]
    if bad:
        # 半拷贝绝不能留在 <分类>/<番号>：库内去重是按目录名算的，留在原位就会被
        # 当成「已入库」，以后既不下也不再试。挪进 .verify-fail（不删、不进索引）。
        quarantine = Path(cfg["library_root"]) / ".verify-fail" / f"{code}-{int(time.time())}"
        try:
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(quarantine))
        except Exception as e:  # noqa: BLE001 —— 隔离失败也不能谎报成功
            log(f"    ⚠️ 落库未通过校验 {bad[:3]}，且隔离失败（{e}）→ 仍在 {target}，未记台账")
            return "verify-fail", str(target)
        log(f"    ⚠️ 落库未通过校验 {bad[:3]}（已隔离到 {quarantine}，未记台账）")
        return "verify-fail", str(quarantine)
    mark_processed(cfg, code)
    return "moved", str(target)


def status(cfg: dict) -> dict:
    root = Path(cfg["library_root"])
    cats = {}
    if root.exists():
        try:
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.name.startswith("."):
                    try:
                        cats[d.name] = len([x for x in d.iterdir() if x.is_dir()])
                    except PermissionError:
                        cats[d.name] = -1
        except PermissionError:
            cats["(无读取权限)"] = -1
    led = load_ledger(cfg)
    return {"library": str(root), "categories": cats,
            "processed": len(led.get("processed", [])),
            "failed": len(led.get("failed", [])),
            "download_queue": _count_dirs(cfg["download_root"]),
            "failed_queue": _count_dirs(cfg["failed_root"])}


def _count_dirs(base: str) -> int:
    p = Path(base)
    if not p.exists():
        return 0
    try:
        return len([d for d in p.iterdir() if d.is_dir() and not d.name.startswith(".")])
    except PermissionError:
        return -1
