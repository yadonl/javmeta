"""下载队列：网页上勾选的东西落在这里，worker 从这里取活。

设计原则（和整个项目一致）：**文件即真相**，不用数据库、不加依赖。
每行一条 JSON 记录，同一番号的状态取最后一条：

    {"ts": 1763600000, "type": "av", "code": "SONE-682",
     "status": "pending|downloading|done|failed", "note": ""}

多进程安全靠 `flock`：网页服务和 worker 可以同时读写。
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path

ACTIVE = ("pending", "downloading")
FINAL = ("done", "failed")


def queue_path(cfg: dict) -> Path:
    raw = (cfg.get("web") or {}).get("queue_file") or "~/.javpipe/queue.jsonl"
    p = Path(raw).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append(cfg: dict, row: dict) -> None:
    p = queue_path(cfg)
    with open(p, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)      # 网页服务 + worker 并发写也安全
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        fcntl.flock(f, fcntl.LOCK_UN)


def rows(cfg: dict) -> list[dict]:
    p = queue_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def latest(cfg: dict) -> dict[str, dict]:
    """番号 → 最新一条记录（状态机就靠它）。"""
    st: dict[str, dict] = {}
    for r in rows(cfg):
        k = (r.get("code") or "").strip().upper()
        if k:
            st[k] = r
    return st


def enqueue(cfg: dict, codes, *, kind: str = "av", note: str = "") -> list[str]:
    """收单。已在队列里/已下过的直接跳过，不重复收。"""
    cur = latest(cfg)
    added = []
    for c in codes:
        c = (c or "").strip().upper()
        if not c:
            continue
        old = cur.get(c) or {}
        if old.get("status") in ("pending", "downloading", "done"):
            continue
        row = {"ts": int(time.time()), "type": kind, "code": c,
               "status": "pending", "note": note}
        _append(cfg, row)
        cur[c] = row                      # 同一批里重复提交的也不重复收
        added.append(c)
    return added


def set_status(cfg: dict, code: str, status: str, note: str = "", kind: str = "av") -> None:
    _append(cfg, {"ts": int(time.time()), "type": kind, "code": code.upper(),
                  "status": status, "note": note})


def next_pending(cfg: dict) -> str | None:
    """下一个待办；同一时间只放一个出去（顺序下载，不抢带宽）。"""
    for code, r in latest(cfg).items():
        if r.get("status") == "pending":
            return code
    return None


def counts(cfg: dict) -> dict[str, int]:
    out = {"pending": 0, "downloading": 0, "done": 0, "failed": 0}
    for r in latest(cfg).values():
        s = r.get("status") or "pending"
        out[s] = out.get(s, 0) + 1
    return out


def clear_finished(cfg: dict) -> int:
    """把 done/failed 的记录压成一行（队列文件不无限长）。返回压缩掉的行数。"""
    p = queue_path(cfg)
    if not p.exists():
        return 0
    before = rows(cfg)
    if not before:
        return 0
    keep = [{"ts": int(time.time()), "type": r.get("type", "av"),
             "code": (r.get("code") or "").upper(), "status": r.get("status"),
             "note": r.get("note", "")} for r in latest(cfg).values()]
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep),
                   encoding="utf-8")
    os.replace(tmp, p)
    return len(before) - len(keep)
