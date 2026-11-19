"""下载器：qBittorrent（磁力）+ HLS 分片（流媒体站点）。

两条路都返回「本地视频文件路径」，上层不用关心来源。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m4v", ".mov")


# ── qBittorrent Web API ───────────────────────────────────────────────
class QBittorrent:
    def __init__(self, cfg: dict):
        q = cfg["qbittorrent"]
        self.base = q["url"].rstrip("/")
        self.user = q.get("username", "admin")
        self.password = q.get("password", "")
        self.category = q.get("category", "")
        self._cookie = ""
        self._login()

    # -- 基础 ---------------------------------------------------------
    def _login(self):
        try:
            r = self._post("/api/v2/auth/login",
                           data={"username": self.user, "password": self.password},
                           headers={"Referer": self.base, "Origin": self.base})
        except Exception as e:
            raise RuntimeError(f"qBittorrent 连不上（{self.base}）: {e}") from e
        body = (r.text or "").strip()
        # 老版本返回 "Ok."，4.5+ 返回 204 空体；失败一律带 "Fails."
        if "Fails" in body or (not body and getattr(r, "status", 0) not in (200, 204)):
            raise RuntimeError("qBittorrent 登录失败：账号或密码不对（或 IP 被 ban）")
        self._cookie = "; ".join(f"{k}={v}" for k, v in r.cookies.items())
        if not self._cookie:
            raise RuntimeError("qBittorrent 登录成功但没拿到 cookie（WebUI 的 CSRF 保护？）")

    def _post(self, path, **kw):
        return _req(self.base + path, method="POST", cookie=self._cookie, **kw)

    def _get(self, path, **kw):
        return _req(self.base + path, method="GET", cookie=self._cookie, **kw)

    def version(self) -> str:
        r = self._get("/api/v2/app/version")
        return r.text.strip()

    def backend(self) -> str:
        try:
            r = self._get("/api/v2/app/buildInfo")
            return f"qBittorrent {self.version()}"
        except Exception:
            return "qBittorrent (unknown)"

    def save_path(self) -> str:
        r = self._get("/api/v2/app/preferences")
        return (r.json() or {}).get("save_path", "")

    def set_save_path(self, path: str):
        import json as _json
        self._post("/api/v2/app/setPreferences",
                   data={"json": _json.dumps({"save_path": path})},
                   headers={"Content-Type": "application/x-www-form-urlencoded"})

    # -- 任务 ---------------------------------------------------------
    def add_magnet(self, magnet: str, save_path: str, tag: str = "") -> bool:
        data = {"urls": magnet, "savepath": save_path}
        if self.category:
            data["category"] = self.category
        if tag:
            data["tags"] = tag
        r = self._post("/api/v2/torrents/add", data=data)
        return "Ok" in (r.text or "")

    def info(self, infohash: str) -> dict | None:
        r = self._get(f"/api/v2/torrents/info?hashes={infohash}")
        arr = r.json() or []
        return arr[0] if arr else None

    def files(self, infohash: str) -> list[dict]:
        r = self._get(f"/api/v2/torrents/files?hashes={infohash}")
        return r.json() or []

    def delete(self, infohash: str, delete_files: bool = True):
        self._post("/api/v2/torrents/delete",
                   data={"hashes": infohash, "deleteFiles": "true" if delete_files else "false"})

    def wait(self, infohash: str, *, timeout: int = 1800, stall_window: int = 180,
             min_bps: int = 200 * 1024, poll: int = 20) -> tuple[str, dict | None]:
        """等到下完；连续 stall_window 秒低于 min_bps 就放弃。
        返回 (state, info) —— state: done | stalled | timeout | error
        """
        t0 = time.time()
        slow_since = None
        last = None
        while time.time() - t0 < timeout:
            try:
                info = self.info(infohash)
            except Exception as e:
                return "error", {"error": str(e)}
            if not info:
                return "error", {"error": "任务不在 qB 里（可能被删）"}
            last = info
            st = info.get("state", "")
            if st in ("pausedUP", "queuedUP", "uploading", "stalledUP", "forcedUP"):
                return "done", info
            if st in ("error", "missingFiles"):
                return "error", info
            speed = float(info.get("dlspeed") or 0)
            if speed < min_bps:
                slow_since = slow_since or time.time()
                if time.time() - slow_since >= stall_window:
                    return "stalled", info
            else:
                slow_since = None
            time.sleep(poll)
        return "timeout", last


def _req(url: str, *, method: str = "GET", cookie: str = "", timeout: int = 30, **kw):
    import urllib.parse
    import urllib.request
    data = kw.pop("data", None)
    headers = kw.pop("headers", {}) or {}
    if cookie:
        headers["Cookie"] = cookie
    if data is not None and isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")

        class _R:
            def __init__(self, text, hdrs, status):
                self.text = text
                self.headers = hdrs
                self.status = status

            def json(self):
                import json as _j
                return _j.loads(self.text)

            @property
            def cookies(self):
                out = {}
                for k, v in self.headers.items():
                    if k.lower() == "set-cookie":
                        for part in v.split(";"):
                            if "=" in part:
                                kk, vv = part.split("=", 1)
                                out[kk.strip()] = vv.strip()
                return out

        return _R(body, r.headers, r.status)


# ── HLS（m3u8 → ts 分片 → mp4）────────────────────────────────────────
def _http_get(scraper, url, *, referer: str = "", binary: bool = False, retries: int = 4):
    headers = {"Referer": referer} if referer else {}
    last = None
    for i in range(retries):
        try:
            r = scraper.get(url, headers=headers, retries=1)
            if r.status_code == 200:
                return r.content if binary else r.text
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.2 * (i + 1))
    raise RuntimeError(f"拉取失败 {url}: {last}")


def pick_variant(scraper, m3u8_url: str, referer: str = "") -> str:
    """master playlist 里挑码率最高的一路；media playlist 原样返回。"""
    text = _http_get(scraper, m3u8_url, referer=referer)
    if "#EXT-X-STREAM-INF" not in text:
        return m3u8_url
    best_bw, best_url = -1, None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if ln.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", ln)
            bw = int(m.group(1)) if m else 0
            nxt = next((x for x in lines[i + 1:] if not x.startswith("#")), None)
            if nxt and bw > best_bw:
                best_bw, best_url = bw, urljoin(m3u8_url, nxt)
    return best_url or m3u8_url


def hls_download(scraper, m3u8_url: str, out_path: str, *, referer: str = "",
                 workers: int = 8, retries: int = 5, log=print,
                 max_segments: int | None = None) -> str:
    """下 HLS：分片并发 + 断点续传 + ffmpeg 合成。返回最终 mp4 路径。

    max_segments 只给自检用（下一小段验证通路），正常调用留 None 全下。
    """
    media = pick_variant(scraper, m3u8_url, referer)
    playlist = _http_get(scraper, media, referer=referer)
    base = media.rsplit("/", 1)[0] + "/"
    segs = [urljoin(base, ln.strip()) for ln in playlist.splitlines()
            if ln.strip() and not ln.startswith("#")]
    if max_segments:
        segs = segs[:max_segments]
    if not segs:
        raise RuntimeError("playlist 里没有分片")
    log(f"    分片 {len(segs)} 个，并发 {workers}")

    out_path = str(out_path)
    cache = Path(out_path + ".segments")
    cache.mkdir(parents=True, exist_ok=True)

    def fetch(idx_seg):
        idx, url = idx_seg
        fp = cache / f"{idx:06d}.ts"
        if fp.exists() and fp.stat().st_size > 1024:
            return fp
        data = _http_get(scraper, url, referer=referer, binary=True, retries=retries)
        fp.write_bytes(data)
        return fp

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, (i, u)): i for i, u in enumerate(segs)}
        for fu in as_completed(futs):
            fu.result()
            done += 1
            if done % 200 == 0 or done == len(segs):
                log(f"    分片 {done}/{len(segs)}")

    list_file = cache / "concat.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for i in range(len(segs)):
            f.write(f"file '{(cache / f'{i:06d}.ts').resolve()}'\n")

    tmp_out = out_path + ".part.mp4"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
           "-i", str(list_file), "-c", "copy", "-bsf:a", "aac_adtstoasc",
           "-movflags", "+faststart", tmp_out]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        # 有站点的音轨不是 AAC，去掉 filter 再试一次
        cmd2 = [c for c in cmd if c not in ("-bsf:a", "aac_adtstoasc")]
        r = subprocess.run(cmd2, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 合成失败: {r.stderr[:300]}")
    shutil.move(tmp_out, out_path)
    shutil.rmtree(cache, ignore_errors=True)
    return out_path


def find_videos(folder: str) -> list[tuple[str, int]]:
    """目录（含一层子目录）里的视频文件，返回 [(路径, 字节数)]。"""
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(VIDEO_EXT) and not f.endswith(".!qB"):
                fp = os.path.join(root, f)
                try:
                    out.append((fp, os.path.getsize(fp)))
                except OSError:
                    pass
    return out


def ffprobe_info(path: str) -> dict:
    """读时长/分辨率，用于验收和命名。"""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
           "-show_entries", "stream=width,height,codec_name",
           "-show_entries", "format=duration", "-of", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        d = __import__("json").loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        return {"width": st.get("width"), "height": st.get("height"),
                "codec": st.get("codec_name"),
                "duration": float((d.get("format") or {}).get("duration") or 0)}
    except Exception as e:
        return {"error": str(e)}
