# 架构说明

## 数据流

```
                   ┌──────────────┐
   番号 / 链接 ───▶ │  cli.py      │  参数解析、番号归一化
                   └──────┬───────┘
                          ▼
                   ┌──────────────┐
                   │  library.py  │  去重（台账→库→失败区→下载区）
                   └──────┬───────┘
                          ▼
                   ┌──────────────┐
                   │  sources.py  │  bitmagnet / MissAV / Jable 探源
                   └──────┬───────┘
              ┌───────────┴───────────┐
              ▼                       ▼
      downloaders.QBittorrent   downloaders.hls_download
      （磁力 + 停滞判定）        （分片并发 + ffmpeg）
              └───────────┬───────────┘
                          ▼
                   download_root/<番号>/
                          ▼
                   ┌──────────────┐
                   │ scrapers.py  │  JavBus → MissAV/Jable 兜底
                   └──────┬───────┘
                          ▼
                   ┌──────────────┐
                   │  library.py  │  分类 → 命名 → NFO → 移动
                   └──────┬───────┘
                          ▼
              library_root/<分类>/<番号>/
```

## 模块职责

| 文件 | 职责 | 不做什么 |
|:--|:--|:--|
| `config.py` | YAML + 环境变量合并，路径展开 | 不校验业务规则 |
| `http.py` | curl_cffi 会话、重试、代理 | 不解析页面 |
| `sources.py` | 站点搜索/详情/m3u8 提取、磁力索引查询 | 不下载、不落盘 |
| `downloaders.py` | qB API、HLS 分片下载与合成、ffprobe | 不知道番号和分类 |
| `scrapers.py` | 元数据抓取、NFO 生成、封面保存 | 不移动文件 |
| `library.py` | 分类规则、命名、去重、台账、移动 | 不发网络请求 |
| `pipeline.py` | 把上面串成 fetch / scrape / rescue / doctor | 不实现细节 |
| `cli.py` | 子命令入口、番号归一化 | — |

## 几个刻意的设计

**失败区永不删除。** 刮削失败通常只是元数据缺失，片子是完整的。删除等于白下几个 GB。
`failed_root` 里的东西只有两种情况会消失：成功入库，或人工删除。

**去重要看四处。** 只查下载区是最常见的错误：片子已经下好、只是刮削没过、被挪进失败区，
下一次任务就当成"没下过"再下一遍。`already_have()` 因此把失败区也算进去。

**分段种子不能按"留最大的"处理。** 一个种子里的多个大文件可能是同一部片的 part，
删掉其余的等于损坏。判据是"多个文件都超过 `multipart_min_bytes`"。

**磁力的停滞判定必须有。** tracker 报的 seeders 和真实连得上的 peer 是两件事，
一个 8GB 种子可能以 160KB/s 爬 14 小时。默认 180 秒 / 200KB/s 低于此值就弃，
转流媒体通常能快两个数量级。

**NFO 兜底要独立于播放。** 元数据抓不到时，片子照样入库（只是 Jellyfin 里显示番号），
不能因为一个站点缺页就卡住整条链路。

## 扩展一个站点

1. 在 `sources.py` 加 `xxx_search(scraper, code)` 和 `xxx_detail(scraper, url)`
2. detail 返回的 dict 必须含：`m3u8`、`referer`、`title`、`cover`、`date`
3. 在 `site_search()` / `site_detail()` 里挂上
4. 在 `config.example.yaml` 的 `sources` 下加开关

只用了一个 `scraper` 会话对象（curl_cffi），没有全局状态，所以站点之间不会互相污染。
