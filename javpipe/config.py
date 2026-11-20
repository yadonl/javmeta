"""配置加载：config.yaml + 环境变量覆盖。

所有本地路径、代理、密码都从配置或环境变量来，代码里不留任何机器相关的值。
"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # 允许只靠环境变量跑，最小安装不装 PyYAML
    yaml = None

DEFAULTS: dict[str, Any] = {
    # 磁力下载落盘目录（qBittorrent 的 save_path 会指向这里）
    "download_root": "~/downloads/jav",
    # 刮削失败隔离区（片子还在，只是元数据没拿到）
    "failed_root": "~/downloads/jav-failed",
    # 成品库根目录（Jellyfin / Emby 直接指向这里）
    "library_root": "~/media/jav",
    # HTTP 代理，如 http://127.0.0.1:7890；留空=直连
    "proxy": "",
    # 站点界面语言（MissAV 用 zh 繁体）
    "language": "zh",

    "qbittorrent": {
        "url": "http://127.0.0.1:8080",
        "username": "admin",
        "password": "",
        "category": "javpipe",
    },

    "sources": {
        # 磁力索引（bitmagnet 的 GraphQL 网关），留空=只用流媒体站点
        "bitmagnet": "",
        "min_seeders": 2,
        "missav": True,
        "jable": True,
    },

    "download": {
        # 磁力：连续 stall_window 秒低于 min_kbps 就放弃，转流媒体
        "stall_window": 180,
        "min_kbps": 200,
        "magnet_timeout": 1800,
        # HLS 分片并发数
        "segment_workers": 8,
        "segment_retries": 5,
        "poll_interval": 20,
    },

    "scraper": {
        "javbus_mirrors": [
            "https://www.javbus.com",
            "https://www.javseen.com",
        ],
        "fallback": ["missav", "jable"],
        "cover_timeout": 40,
        # 失败区同一部试几次还没元数据就跳过（0=不限次）
        "rescue_max_tries": 3,
    },

    "library": {
        "rules": [
            {"dir": "无码", "match": ["-U", "-UNCENSORED", "UNCENSORED-LEAK", "无码", "無碼"]},
            {"dir": "有码/有字幕", "match": ["-C", "CHINESE-SUBTITLE", "中文字幕", "字幕"]},
            {"dir": "有码/4K", "match": ["4K", "2160P", "UHD"]},
            {"dir": "有码/无字幕", "match": ["*"]},
        ],
        "video_name": "{code}.mp4",
        # 多段种子（每段都大于这个体积）视为同一部片的分段，全保留
        "multipart_min_bytes": 1073741824,
        # 小于这个体积的“视频”当广告删掉
        "ad_max_bytes": 52428800,
    },

    # 网页自助下单（javpipe serve / worker）
    "web": {
        # 只监听本机。要给局域网用就显式改成 0.0.0.0，**同时必须设 web.token**
        "host": "127.0.0.1",
        "port": 8892,
        # 想换成自己的静态页就填目录，留空用内置页
        "site_dir": "",
        # 候选池与队列文件（网页读这个、worker 消费这个）
        "candidates": "~/.javpipe/candidates.json",
        "queue_file": "~/.javpipe/queue.jsonl",
        # 非空则提交订单要带 ?token=xxx（放到内网也建议设一个）
        "token": "",
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _expand(v: Any) -> Any:
    if isinstance(v, str):
        return str(Path(v).expanduser()) if v.startswith("~") else v
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


ENV_MAP = {
    "JAVPIPE_PROXY": ("proxy",),
    "JAVPIPE_DOWNLOAD_ROOT": ("download_root",),
    "JAVPIPE_FAILED_ROOT": ("failed_root",),
    "JAVPIPE_LIBRARY_ROOT": ("library_root",),
    "JAVPIPE_QB_URL": ("qbittorrent", "url"),
    "JAVPIPE_QB_USER": ("qbittorrent", "username"),
    "JAVPIPE_QB_PASSWORD": ("qbittorrent", "password"),
    "JAVPIPE_BITMAGNET": ("sources", "bitmagnet"),
    "JAVPIPE_WEB_PORT": ("web", "port"),
    "JAVPIPE_WEB_TOKEN": ("web", "token"),
    "JAVPIPE_WEB_CANDIDATES": ("web", "candidates"),
    "JAVPIPE_WEB_QUEUE": ("web", "queue_file"),
}


def load_config(path: str | os.PathLike | None = None) -> dict:
    """读 YAML（可选）+ 环境变量，返回完整配置。"""
    cfg = copy.deepcopy(DEFAULTS)
    p = Path(path or os.environ.get("JAVPIPE_CONFIG", "config.yaml")).expanduser()
    if p.exists():
        if yaml is None:
            raise RuntimeError(f"读 {p} 需要 PyYAML：pip install pyyaml")
        with open(p, "r", encoding="utf-8") as f:
            _deep_merge(cfg, yaml.safe_load(f) or {})
    for env, keys in ENV_MAP.items():
        val = os.environ.get(env)
        if not val:
            continue
        node = cfg
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = val
    return _expand(cfg)


def cfg_get(cfg: dict, *keys, default=None):
    node: Any = cfg
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return default
        node = node[k]
    return node
