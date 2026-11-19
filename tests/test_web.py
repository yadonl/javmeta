"""网页自助下单的测试：候选池解析、队列状态机、HTTP 接口、worker 流转。"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from javpipe import queue, rank, serve, worker


@pytest.fixture()
def cfg(tmp_path):
    (tmp_path / "media").mkdir()
    (tmp_path / "dl").mkdir()
    (tmp_path / "failed").mkdir()
    return {
        "download_root": str(tmp_path / "dl"),
        "failed_root": str(tmp_path / "failed"),
        "library_root": str(tmp_path / "media"),
        "proxy": "",
        "library": {"rules": [], "video_name": "{code}.mp4",
                    "multipart_min_bytes": 10 ** 9, "ad_max_bytes": 0},
        "sources": {}, "download": {}, "scraper": {}, "qbittorrent": {},
        "web": {
            "host": "127.0.0.1", "port": 0, "site_dir": "",
            "candidates": str(tmp_path / "candidates.json"),
            "queue_file": str(tmp_path / "queue.jsonl"),
            "token": "",
        },
    }


# ── 候选池 ────────────────────────────────────────────────────────────
LISTING = """
<div><a href="/en/sone-682" title="SONE-682 新人"><img data-src="https://img/1.jpg" alt="SONE-682"></a></div>
<div><a href="/en/abc-123"><img src="https://img/2.jpg" alt="ABC-123 标题"></a></div>
<div><a href="/en/nothing"><img src="https://img/3.jpg" alt="没有番号"></a></div>
"""


def test_parse_listing_keeps_cards_with_code():
    got = rank.parse_listing(LISTING, base="https://site.tv/new/")
    assert [g["code"] for g in got] == ["SONE-682", "ABC-123"]
    assert got[0]["cover"] == "https://img/1.jpg"
    assert got[0]["url"] == "https://site.tv/en/sone-682"


def test_build_from_file_skips_library(cfg):
    (Path(cfg["library_root"]) / "TEST-001").mkdir()
    f = Path(cfg["web"]["candidates"]).parent / "codes.txt"
    f.write_text("TEST-001\nTEST-002\n", encoding="utf-8")
    data = rank.build(cfg, from_file=str(f), log=lambda *_: None)
    assert [i["code"] for i in data["items"]] == ["TEST-002"]
    assert rank.load(cfg)["items"][0]["code"] == "TEST-002"


# ── 队列 ──────────────────────────────────────────────────────────────
def test_queue_dedupe_and_state(cfg):
    assert queue.enqueue(cfg, ["SONE-682", "sone-682", "ABC-123"]) == ["SONE-682", "ABC-123"]
    assert queue.enqueue(cfg, ["SONE-682"]) == []          # 已在队列里，不重复收单
    assert queue.counts(cfg)["pending"] == 2
    queue.set_status(cfg, "SONE-682", "done")
    assert queue.counts(cfg)["done"] == 1
    assert queue.next_pending(cfg) == "ABC-123"
    queue.set_status(cfg, "ABC-123", "done")
    assert queue.next_pending(cfg) is None
    assert queue.enqueue(cfg, ["ABC-123"]) == []           # 已下过，不再收


# ── HTTP 接口 ─────────────────────────────────────────────────────────
@pytest.fixture()
def server(cfg):
    httpd_holder = {}

    def _run():
        import http.server as hs
        serve.Handler.cfg = cfg
        serve.Handler.site = serve.BUILTIN_SITE
        srv = hs.ThreadingHTTPServer(("127.0.0.1", 0), serve.Handler)
        httpd_holder["s"] = srv
        srv.serve_forever()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(50):
        if "s" in httpd_holder:
            break
        threading.Event().wait(0.05)
    base = f"http://127.0.0.1:{httpd_holder['s'].server_address[1]}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 别让环境代理劫持本地请求
    yield base, op
    httpd_holder["s"].shutdown()


def test_page_and_apis(cfg, server):
    base, op = server
    rank.build(cfg, from_file="", log=lambda *_: None)
    Path(cfg["web"]["candidates"]).write_text(json.dumps({"built": 1, "items": [
        {"code": "SONE-682", "title": "t", "cover": "", "url": ""}]}), encoding="utf-8")

    with op.open(base + "/") as r:
        assert r.status == 200 and "自助下单" in r.read().decode()
    with op.open(base + "/api/list") as r:
        j = json.loads(r.read())
    assert j["count"] == 1 and j["items"][0]["code"] == "SONE-682"

    req = urllib.request.Request(base + "/api/queue", method="POST",
                                 data=json.dumps({"items": [{"code": "SONE-682"}]}).encode(),
                                 headers={"Content-Type": "application/json"})
    with op.open(req) as r:
        assert json.loads(r.read())["accepted"] == 1
    with op.open(base + "/api/state") as r:
        assert json.loads(r.read())["counts"]["pending"] == 1


# ── worker ────────────────────────────────────────────────────────────
def test_worker_done_when_already_in_library(cfg, monkeypatch):
    queue.enqueue(cfg, ["TEST-001"])
    monkeypatch.setattr(worker, "fetch_one",
                        lambda *a, **k: {"code": "TEST-001", "status": "skipped", "why": "库里有了"})
    r = worker.run_once(cfg, scraper=None, scrape_after=False)
    assert r["status"] == "skipped"
    assert queue.latest(cfg)["TEST-001"]["status"] == "done"


def test_worker_failed_on_notfound(cfg, monkeypatch):
    queue.enqueue(cfg, ["ZZZZ-000"])
    monkeypatch.setattr(worker, "fetch_one",
                        lambda *a, **k: {"code": "ZZZZ-000", "status": "notfound"})
    worker.run_once(cfg, scraper=None, scrape_after=False)
    assert queue.latest(cfg)["ZZZZ-000"]["status"] == "failed"


def test_worker_dry_run_keeps_pending(cfg, monkeypatch):
    queue.enqueue(cfg, ["SONE-682"])
    monkeypatch.setattr(worker, "fetch_one",
                        lambda *a, **k: {"code": "SONE-682", "status": "dry-stream"})
    worker.run_once(cfg, scraper=None, dry_run=True, scrape_after=False)
    assert queue.latest(cfg)["SONE-682"]["status"] == "pending"


def test_worker_empty_queue(cfg):
    assert worker.run_once(cfg, scraper=None) is None
