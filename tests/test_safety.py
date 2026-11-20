"""护栏测试：这几处以前会「静默丢数据」，改完必须一直是绿的（不联网）。"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from javpipe import downloaders, library, pipeline, queue   # noqa: E402
from javpipe.config import DEFAULTS, load_config             # noqa: E402
from javpipe.library import (clean_folder, ingest, library_index,   # noqa: E402
                             load_ledger)


@pytest.fixture()
def cfg(tmp_path):
    c = load_config(None)
    c["download_root"] = str(tmp_path / "dl")
    c["failed_root"] = str(tmp_path / "failed")
    c["library_root"] = str(tmp_path / "lib")
    c["web"]["queue_file"] = str(tmp_path / "state" / "queue.jsonl")
    c["web"]["candidates"] = str(tmp_path / "state" / "candidates.json")
    for k in ("download_root", "failed_root", "library_root"):
        Path(c[k]).mkdir(parents=True)
    return c


def _mk(path: Path, name: str, mb: int) -> Path:
    p = path / name
    p.write_bytes(b"0" * (mb * 1024 * 1024))
    return p


# ── 1. qBittorrent 5.x 的完成态 ───────────────────────────────────────
class _FakeQB:
    def __init__(self, info: dict):
        self._info = info

    def info(self, _hash):
        return self._info


@pytest.mark.parametrize("info,expected", [
    ({"state": "stoppedUP", "progress": 1, "dlspeed": 0}, "done"),   # qB 5.x 的「下完」
    ({"state": "moving", "progress": 1, "dlspeed": 0}, "done"),      # 5.x 在搬文件
    ({"state": "stalledUP", "progress": 1, "dlspeed": 0}, "done"),
    ({"state": "missingFiles", "progress": 1, "dlspeed": 0}, "error"),
    ({"state": "stoppedDL", "progress": 0.42, "dlspeed": 0}, "stalled"),  # 只是停了，没下完
    ({"state": "downloading", "progress": 0.99, "dlspeed": 0}, "stalled"),
    # 将来 qB 再改状态名也不至于把下完的片子删了：progress 齐了且不在动的状态 → done
    ({"state": "someFutureStateName", "progress": 1, "dlspeed": 0}, "done"),
])
def test_qb_done_states(info, expected):
    """qB 5.3 的真机状态名叫 stoppedUP；漏了它下完的种子会被当停滞删掉。"""
    state, _ = downloaders.QBittorrent.wait(
        _FakeQB(info), "hash", timeout=1, stall_window=0, min_bps=1, poll=0.01)
    assert state == expected


# ── 2. 队列：取活即占位 + 僵尸回收 ────────────────────────────────────
def test_claim_is_exclusive(cfg):
    queue.enqueue(cfg, ["AAA-111", "BBB-222"])
    got = {queue.claim_next(cfg), queue.claim_next(cfg)}
    assert got == {"AAA-111", "BBB-222"}
    assert queue.claim_next(cfg) is None          # 两单都被占位了，不会重复派


def test_claim_reclaims_stale_downloading(cfg):
    """worker 被 kill 留下的 downloading 必须能回收，否则这一部永远卡死。"""
    queue.enqueue(cfg, ["CCC-333"])
    assert queue.claim_next(cfg) == "CCC-333"
    assert queue.claim_next(cfg) is None

    qp = queue.queue_path(cfg)                    # 把记录改成 7 小时前
    rows = [json.loads(x) for x in qp.read_text().splitlines() if x.strip()]
    for r in rows:
        r["ts"] = int(time.time() - 7 * 3600)
    qp.write_text("".join(json.dumps(r) + "\n" for r in rows))

    assert queue.claim_next(cfg, stale_after=6 * 3600) == "CCC-333"


# ── 3. HLS：分片原子落盘，坏片不沿用 ─────────────────────────────────
def _stub_hls(monkeypatch, seg_body=lambda url: b"\x47" * 2000):
    calls: list[str] = []

    def fake_get(_scraper, url, *, referer="", binary=False, retries=3):
        calls.append(url)
        if url.endswith(".m3u8"):
            return "#EXTM3U\n0.ts\n1.ts\n"
        return seg_body(url)

    def fake_run(cmd, **_kw):
        Path(cmd[-1]).write_bytes(b"y" * 64)
        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(downloaders, "pick_variant", lambda _s, u, r="": u)
    monkeypatch.setattr(downloaders, "_http_get", fake_get)
    monkeypatch.setattr(downloaders.subprocess, "run", fake_run)
    return calls


def test_hls_refetches_truncated_and_half_written_segments(tmp_path, monkeypatch):
    calls = _stub_hls(monkeypatch)
    cache = tmp_path / "X-001.mp4.segments"
    cache.mkdir()
    (cache / "000000.ts").write_bytes(b"0" * 100)        # 截断残留（<1024）
    (cache / "000001.ts").write_bytes(b"0" * 5000)       # 有 5000 字节，但旁边留着 .tmp → 不可信
    (cache / "000001.ts.tmp").write_bytes(b"0" * 900)

    out = downloaders.hls_download(None, "https://x/a.m3u8", str(tmp_path / "X-001.mp4"),
                                   log=lambda *a, **k: None)
    assert Path(out).exists()
    assert calls.count("https://x/0.ts") == 1
    assert calls.count("https://x/1.ts") == 1


def test_hls_rejects_tiny_segment(tmp_path, monkeypatch):
    _stub_hls(monkeypatch, seg_body=lambda _u: b"x" * 10)
    with pytest.raises(RuntimeError):
        downloaders.hls_download(None, "https://x/a.m3u8", str(tmp_path / "Y-002.mp4"),
                                 log=lambda *a, **k: None)


# ── 4. 找视频：HLS 缓存和半成品不算正片 ──────────────────────────────
def test_find_videos_skips_cache_and_partials(tmp_path):
    d = tmp_path / "ABC-123"
    (d / "ABC-123.mp4.segments").mkdir(parents=True)
    (d / "ABC-123.mp4.segments" / "000000.ts").write_bytes(b"0" * 2000)
    (d / "ABC-123.mp4").write_bytes(b"0" * 3000)
    (d / "ABC-123.mp4.part.mp4").write_bytes(b"0" * 4000)
    (d / "ABC-123.mkv.!qB").write_bytes(b"0" * 5000)
    names = [Path(f).name for f, _sz in downloaders.find_videos(str(d))]
    assert names == ["ABC-123.mp4"]


# ── 5. 落库校验不过 → 隔离，不许留下「假已入库」 ─────────────────────
def test_verify_fail_goes_to_quarantine(cfg, monkeypatch):
    cfg["library"]["ad_max_bytes"] = 1024
    d = Path(cfg["download_root"]) / "ABC-125"
    d.mkdir(parents=True)
    (d / "ABC-125.mp4").write_bytes(b"0" * 8192)

    real_move = shutil.move

    def truncating_move(src, dst):            # 模拟「报成功但只到了一部分」
        real_move(src, dst)
        f = Path(dst) / "ABC-125.mp4"
        if f.exists() and f.stat().st_size > 1024:
            f.write_bytes(b"0" * 1024)

    monkeypatch.setattr(library.shutil, "move", truncating_move)
    state, target = ingest(cfg, str(d), "ABC-125", log=lambda *a, **k: None)

    assert state == "verify-fail"
    assert ".verify-fail" in target
    assert "ABC-125" not in library_index(cfg)               # 不会被当成库内已有
    assert "ABC-125" not in load_ledger(cfg)["processed"]


def test_library_index_skips_dot_dirs(cfg):
    (Path(cfg["library_root"]) / ".verify-fail" / "BAD-001").mkdir(parents=True)
    (Path(cfg["library_root"]) / "有码" / "无字幕" / "GOOD-001").mkdir(parents=True)
    idx = library_index(cfg)
    assert "BAD-001" not in idx
    assert "GOOD-001" in idx


# ── 6. 单文件模式：多出来的大文件不删，挪去 .extras ──────────────────
def test_clean_folder_keeps_unexpected_extra_video(cfg, tmp_path):
    cfg["library"]["ad_max_bytes"] = 30 * 1024 * 1024
    cfg["library"]["multipart_min_bytes"] = 1024 ** 3
    d = tmp_path / "EXT-001"
    d.mkdir()
    _mk(d, "EXT-001.mp4", 60)
    _mk(d, "EXT-001-cd2.mp4", 40)          # 不是分段也不是广告：不许删

    main, deleted = clean_folder(cfg, str(d), "EXT-001")
    assert main == "EXT-001.mp4"
    assert deleted == 0
    assert [p.name for p in d.iterdir()] == ["EXT-001.mp4"]
    extras = list((Path(cfg["download_root"]) / ".extras").rglob("*.mp4"))
    assert [p.name for p in extras] == ["EXT-001-cd2.mp4"]


# ── 7. 失败区已有同名目录：旧的一份挪进 .archive，绝不 rmtree ─────────
def test_failed_collision_archives_old_copy(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "scrape_meta", lambda *_a, **_k: None)
    old = Path(cfg["failed_root"]) / "DUP-001"
    old.mkdir(parents=True)
    (old / "DUP-001.mp4").write_bytes(b"0" * 300)
    new = Path(cfg["download_root"]) / "DUP-001"
    new.mkdir()
    (new / "DUP-001.mp4").write_bytes(b"1" * 100)

    pipeline.scrape_folder(cfg, None, new, move_failed=True)

    arch = list((Path(cfg["failed_root"]) / ".archive").iterdir())
    assert len(arch) == 1
    assert (arch[0] / "DUP-001.mp4").read_bytes()[:1] == b"0"      # 旧的一份还在
    assert (Path(cfg["failed_root"]) / "DUP-001" / "DUP-001.mp4").read_bytes()[:1] == b"1"


# ── 8. rescue 试够次数就不再骚扰站点 ─────────────────────────────────
def test_rescue_stops_after_max_tries(cfg, monkeypatch):
    monkeypatch.setattr(pipeline, "scrape_meta", lambda *_a, **_k: None)
    cfg["scraper"]["rescue_max_tries"] = 2
    (Path(cfg["failed_root"]) / "NOPE-001").mkdir()

    assert len(pipeline.scan_failed(cfg, None)) == 1
    assert len(pipeline.scan_failed(cfg, None)) == 1
    assert pipeline.scan_failed(cfg, None) == []                   # 第 3 次起跳过
    assert len(pipeline.scan_failed(cfg, None, reset_tries=True)) == 1


# ── 9. 网页默认不对外 ────────────────────────────────────────────────
def test_web_defaults_to_loopback():
    assert DEFAULTS["web"]["host"] == "127.0.0.1"


def test_serve_refuses_public_bind_without_token(cfg):
    from javpipe.serve import serve
    with pytest.raises(SystemExit):
        serve(cfg, host="0.0.0.0")


def test_serve_allows_public_bind_with_token(cfg, monkeypatch):
    from javpipe import serve as serve_mod

    class Boom(Exception):
        pass

    def fake_server(*_a, **_kw):
        raise Boom

    cfg["web"]["token"] = "s3cret"
    monkeypatch.setattr(serve_mod, "ThreadingHTTPServer", fake_server)
    with pytest.raises(Boom):                     # 过了护栏，走到真正监听那一步
        serve_mod.serve(cfg, host="0.0.0.0", log=lambda *a, **k: None)
