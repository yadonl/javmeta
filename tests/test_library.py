"""纯逻辑单元测试（不联网、不碰真实媒体库）。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from javpipe.cli import code_from_url, normalize_code          # noqa: E402
from javpipe.config import DEFAULTS, load_config                # noqa: E402
from javpipe.library import (already_have, classify, clean_folder,  # noqa: E402
                             code_from_name, library_index, load_ledger,
                             mark_processed, probe_height, video_name)
from javpipe.sources import code_matches, norm, unpack_js_eval   # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    c = load_config(None)
    c["download_root"] = str(tmp_path / "dl")
    c["failed_root"] = str(tmp_path / "failed")
    c["library_root"] = str(tmp_path / "lib")
    for k in ("download_root", "failed_root", "library_root"):
        Path(c[k]).mkdir(parents=True)
    return c


# ── 番号解析 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("url,expected", [
    ("https://jable.tv/videos/ssis-885/", "SSIS-885"),
    ("https://missav.ai/cn/sone-543-chinese-subtitle", "SONE-543"),
    ("https://missav.ai/dm1151/092014_887", "092014-887"),
    ("DLDSS-569", "DLDSS-569"),
    ("dldss_569", "DLDSS-569"),
])
def test_code_from_url(url, expected):
    assert code_from_url(url) == expected


def test_normalize_code():
    assert normalize_code(" ssis_885 ") == "SSIS-885"


def test_code_matches():
    assert code_matches("SSIS-885", "https://x/a/ssis-885-chinese-subtitle")
    assert not code_matches("SSIS-885", "https://x/a/ssis-886")


# ── 分类与命名 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("code,filename,height,expected", [
    ("ABC-123-U", "ABC-123-U.mp4", 1080, "无码"),
    ("ABC-123-C", "ABC-123-C.mp4", 1080, "有码/有字幕"),
    ("ABC-123", "ABC-123-4K.mp4", 2160, "有码/4K"),
    ("ABC-123", "ABC-123.mp4", 1080, "有码/无字幕"),
    # 标题带 4K 但实际 720p —— 不许进 4K 目录
    ("ABC-124", "ABC-124-4K.mp4", 720, "有码/无字幕"),
])
def test_classify(cfg, code, filename, height, expected):
    assert classify(cfg, code, filename=filename, height=height) == expected


def test_probe_height(tmp_path):
    assert probe_height(str(tmp_path / "nope.mp4")) == 0


def test_video_name(cfg):
    assert video_name(cfg, "abc-123") == "ABC-123.mp4"


def test_code_from_name():
    assert code_from_name("hhd800.com@MIRD-277-A.mp4") == "MIRD-277"
    assert code_from_name("489155.com@DASS-952-U.mp4") == "DASS-952-U"


# ── 去重 ──────────────────────────────────────────────────────────────
def test_dedup_ledger(cfg):
    mark_processed(cfg, "ABC-123")
    assert "台账" in (already_have(cfg, "ABC-123") or "")


def test_dedup_library(cfg):
    (Path(cfg["library_root"]) / "有码" / "无字幕" / "XYZ-999").mkdir(parents=True)
    assert "库内已有" in (already_have(cfg, "XYZ-999") or "")


def test_dedup_failed_has_video(cfg):
    """失败区有完整片子时不许重下 —— 这是最容易白下 10GB 的坑。"""
    d = Path(cfg["failed_root"]) / "FAIL-001"
    d.mkdir(parents=True)
    (d / "FAIL-001.mp4").write_bytes(b"0" * (120 * 1024 * 1024))
    assert "等刮削" in (already_have(cfg, "FAIL-001") or "")


def test_library_index(cfg):
    (Path(cfg["library_root"]) / "无码" / "AAA-111").mkdir(parents=True)
    (Path(cfg["library_root"]) / "有码" / "有字幕" / "BBB-222").mkdir(parents=True)
    idx = library_index(cfg)
    assert idx["AAA-111"] == "无码/AAA-111"
    assert idx["BBB-222"] == "有码/有字幕/BBB-222"


# ── 目录规范化 ────────────────────────────────────────────────────────
def _mk(path: Path, name: str, mb: int):
    p = path / name
    p.write_bytes(b"0" * (mb * 1024 * 1024))
    return p


def test_clean_folder_single(cfg, tmp_path):
    cfg["library"]["ad_max_bytes"] = 30 * 1024 * 1024
    d = tmp_path / "SINGLE-001"
    d.mkdir()
    _mk(d, "hhd800.com@SINGLE-001.mp4", 60)
    _mk(d, "广告.mp4", 2)                       # 广告，应删
    main, deleted = clean_folder(cfg, str(d), "SINGLE-001")
    assert main == "SINGLE-001.mp4"
    assert deleted == 1
    assert not (d / "广告.mp4").exists()


def test_clean_folder_multipart(cfg, tmp_path):
    """多段种子：4 段都超过分段阈值时全保留，命名 partN —— 删掉就等于丢数据。"""
    cfg["library"]["ad_max_bytes"] = 3 * 1024 * 1024
    cfg["library"]["multipart_min_bytes"] = 3 * 1024 * 1024
    d = tmp_path / "MULTI-002"
    d.mkdir()
    for p in "ABCD":
        _mk(d, f"hhd800.com@MULTI-002-{p}.mp4", 5)
    _mk(d, "18+游戏大全(996gg.cc)-广告.mp4", 2)
    main, deleted = clean_folder(cfg, str(d), "MULTI-002")
    assert main == "MULTI-002-part1.mp4"
    names = sorted(x.name for x in d.iterdir())
    assert names == [f"MULTI-002-part{i}.mp4" for i in (1, 2, 3, 4)]
    assert deleted == 1


# ── JS packer 解包 ────────────────────────────────────────────────────
def test_unpack_js_eval():
    packed = ("eval(function(p,a,c,k,e,d){e=function(c){return c};if(!''.replace(/^/,String))"
              "{while(c--){d[c]=k[c]||c}k=[function(e){return d[e]}];e=function(){return'\\\\w+'};"
              "c=1};while(c--){if(k[c]){p=p.replace(new RegExp('\\\\b'+e(c)+'\\\\b','g'),k[c])}}"
              "return p}('0=\"1://2.3/4.5\"',6,6,'source|https|cdn|com|playlist|m3u8'.split('|'),0,{}))")
    out = unpack_js_eval(packed)
    assert out and "https://cdn.com/playlist.m3u8" in out


def test_unpack_js_eval_bad_input():
    assert unpack_js_eval("nothing here") is None


def test_norm():
    assert norm("ssis-885") == "SSIS885"
    assert norm("") == ""

# ── 落库：搬完必须按字节对账 ──────────────────────────────────────────
def _mk_folder(cfg, code, nbytes):
    cfg["library"]["ad_max_bytes"] = 512
    cfg["library"]["multipart_min_bytes"] = 512 * 1024 * 1024
    d = Path(cfg["download_root"]) / code
    d.mkdir(parents=True)
    (d / f"hhd800.com@{code}.mp4").write_bytes(b"0" * nbytes)
    return d


def test_ingest_moves(cfg):
    from javpipe.library import ingest
    d = _mk_folder(cfg, "ABC-123", 4096)
    state, target = ingest(cfg, str(d), "ABC-123", log=lambda *a, **k: None)
    assert state == "moved"
    assert (Path(target) / "ABC-123.mp4").stat().st_size == 4096
    assert "ABC-123" in load_ledger(cfg)["processed"]
    assert not d.exists()


def test_ingest_catches_incomplete_move(cfg, monkeypatch):
    """搬运"报成功"但字节没到位（断流/截断）：必须判 verify-fail，且不能记台账。"""
    from javpipe import library
    d = _mk_folder(cfg, "ABC-124", 4096)

    def fake_move(src, dst):
        dst = Path(dst)
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "ABC-124.mp4").write_bytes(b"0" * 1024)   # 只到了一部分
        return str(dst)

    monkeypatch.setattr(library.shutil, "move", fake_move)
    state, _target = library.ingest(cfg, str(d), "ABC-124", log=lambda *a, **k: None)
    assert state == "verify-fail"
    assert "ABC-124" not in library.load_ledger(cfg)["processed"]
