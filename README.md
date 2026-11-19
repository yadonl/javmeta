# javpipe

> 一条命令把**番号**变成**媒体库里能播的成品**：探源 → 下载 → 刮削 → 入库。
> 中文 | [English](#english)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20macOS%20%7C%20NAS-lightgrey)]()

为 **NAS 无人值守**场景写的媒体流水线。你在手机或网页上挑好片，丢一个番号（或一个链接）进来，
剩下的交给 cron：能找到 4K 磁力就用磁力，磁力不动就自动切流媒体；JavBus 没收录就换
MissAV / Jable 补元数据；最后按 `无码 / 有码-有字幕 / 有码-4K / 有码-无字幕` 归档成
Jellyfin 直接认得的结构。

```
番号/链接 ──▶ 探源 ────────▶ 下载 ────────────▶ 刮削 ──────────▶ 入库
             ├ bitmagnet     ├ qBittorrent       ├ JavBus          ├ 分类规则
             ├ MissAV        └ HLS 分片并发      ├ MissAV 兜底      ├ 命名规范
             └ Jable                              └ Jable 兜底      └ NFO+封面
                                                 (失败→隔离区→自动救援)
```

## 为什么会有这个项目

现成的工具大多只解决其中一段：下载器不管刮削，刮削器不管下载，刮削失败的片子直接躺在
硬盘里没人管。真正跑起来最难的是**边界情况**：

- 磁力 tracker 报 5 个种，实际只连上 1 个，8GB 下到 14 小时（需要自动判定停滞并换源）
- JavBus 对一部分番号返回「您進入的網址有誤」，而 MissAV 上有完整数据
- 刮削失败的片子会被下一次任务当成"没下过"，**白下 10GB**
- 种子里的 2MB 广告 mp4 会让脚本把整包当成品处理
- 多段种子（A/B/C/D 四个文件）被"只留最大文件"的逻辑删掉 3 段，**丢 27GB**

这些坑这个项目全部踩过，也全部写进了代码。

## 特性

| 能力 | 说明 |
|:--|:--|
| **双下载通道** | qBittorrent 磁力（含停滞自动放弃）+ HLS 分片并发下载（断点续传、ffmpeg 合成） |
| **多源兜底** | 磁力 → MissAV → Jable，逐级降级，不会因为一个站点挂了就断链 |
| **刮削兜底** | JavBus 优先；缺页时用 MissAV/Jable 的 `og:title`、发行日期、女优、分类生成 NFO |
| **四级去重** | 台账 → 媒体库 → 失败区 → 下载区，任何一处有完整片子就不重下 |
| **失败救援** | 刮削不过的片子进隔离区（不删！），`javpipe rescue` 反复尝试补元数据，3 次不过自动闭嘴 |
| **分段种子保护** | 多个 >1GB 视频视为同一部片的分段，全保留并命名 `-partN` |
| **广告清理** | <50MB 的"视频"和推广文件自动删，正片不碰 |
| **Jellyfin 原生** | 目录名 = 番号，`<CODE>.nfo` + `poster` + `fanart`，抓不到也不影响播放 |
| **零机器绑定** | 路径、代理、密码全走配置/环境变量，代码里没有任何写死的本机路径 |

## 安装

```bash
git clone https://github.com/yadonl/javpipe.git
cd javpipe
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
```

外部依赖只有三个，都是现成的：

```bash
# Debian/Ubuntu
sudo apt install ffmpeg
# 群晖/威联通/飞牛/NAS：装好容器或套件里的 ffmpeg 即可，只要在 PATH 里
# qBittorrent：随便一个能开 WebUI 的实例（NoX 版就够）
```

`javpipe doctor` 会把依赖、路径、qB 连接、站点可达性一次检查完：

```
== 依赖 ==
  ✓ curl_cffi
  ✓ bs4
  ✓ ffmpeg
  ✓ ffprobe
== 路径 ==
  ✓ download_root = /volume1/downloads/jav
  ✓ library_root = /volume1/media/jav
== qBittorrent ==
  ✓ qBittorrent 4.6.2  save_path=/volume1/downloads/jav
== 站点 ==
  ✓ 搜索可达（命中 2 条）
```

## 配置

```bash
cp config.example.yaml config.yaml
```

最少要改三个路径和一个 qB 密码：

```yaml
download_root: /volume1/downloads/jav
failed_root:   /volume1/downloads/jav-failed
library_root:  /volume1/media/jav
proxy: "http://127.0.0.1:7890"     # 境外站点基本都需要
qbittorrent:
  url: http://127.0.0.1:8080
  password: ""                      # 别写进 git，用下面这个环境变量
```

密码走环境变量更安全：

```bash
export JAVPIPE_QB_PASSWORD='...'
```

## 用法

```bash
# 1. 只看探源结果，什么都不下
javpipe fetch SSIS-885 --dry-run

# 2. 下载（磁力优先，停滞自动切流媒体）
javpipe fetch SSIS-885

# 3. 一次搞定：下载 + 刮削 + 入库
javpipe run SSIS-885 MIRD-277

# 4. 直接丢网页链接，自动抠番号
javpipe add https://jable.tv/videos/ssis-885/

# 5. 扫下载目录（适合手动丢进去的片子）
javpipe scrape

# 6. 救失败区
javpipe rescue

# 7. 看库存
javpipe status
```

跑起来长这样：

```
=== EBWH-367 ===
  磁力命中 5 种子 / 8.00GB
  磁力 stalled（162KB/s），转流媒体
  站点命中 1 条（jable）
    分片 2594 个，并发 8
    分片 400/2594
    ...
  完成 8.01GB，耗时 143s

===== 汇总 1/1 成功 =====
```

## 它是怎么工作的

### 1. 探源（`sources.py`）

三种来源并行地问，问到了按优先级用：

- **bitmagnet**（可选）：公开磁力索引的 GraphQL 网关，按 2160p → 1080p → 任意 的顺序查，
  取 `seeders ≥ 2` 里体积最大的一条（体积≈画质）
- **MissAV**：搜索页 → 番号精确命中的卡片 → 详情页。m3u8 藏在 Dean Edwards JS packer 里，
  项目自带解包器；镜像 `missav.ai / .ws / .live` 自动试
- **Jable**：搜索页 → 详情页 → 页面里第一个 m3u8

### 2. 下载（`downloaders.py`）

- **磁力**：投种 → 轮询 → 连续 `stall_window` 秒低于 `min_kbps` 就判定停滞。
  停滞会把种子和半成品**一起删掉**（不占空间），然后转流媒体
- **流媒体**：master playlist 里挑码率最高的一路 → 分片并发下载（默认 8 并发，支持断点续传）
  → `ffmpeg -f concat -c copy` 合成 mp4 → `+faststart` 让 Jellyfin 能秒开拖动

### 3. 刮削（`scrapers.py`）

- **JavBus** 优先：标题、发行日期、时长、片商、女优、分类、封面、剧照
- **MissAV / Jable 兜底**：JavBus 报「您進入的網址有誤」或 404 时，从 `og:title`、
  `release_date`、`/actresses/`、`/genres/` 拼出等价元数据
- 输出标准 `<CODE>.nfo` + `poster` + `fanart`，Jellyfin / Emby / Kodi 都能读

### 4. 入库（`library.py`）

分类按配置里的规则自上而下匹配（番号、文件名、标题、分类词），命中即停：

```
library_root/
├── 无码/            ← -U / UNCENSORED / 无码
├── 有码/
│   ├── 有字幕/      ← -C / CHINESE-SUBTITLE / 中文字幕
│   ├── 4K/          ← ffprobe 实测高度 ≥2160（不看标题里的 "4K" 字样）
│   └── 无字幕/      ← 兜底
```

入库时做四件事：清广告 → 视频统一命名 → 写 NFO + 封面 → 移动目录（同一磁盘是原子 rename）。

> 4K 判定走 `ffprobe` 实测分辨率，不是文本匹配。站点标题经常写 "4K" 而实际只有 720p，
> 按文字分类会把一堆低码率片塞进 4K 目录。

## 去重：为什么它不会白下

`already_have()` 按顺序查四处，任何一处命中就跳过：

| 顺序 | 查什么 | 为什么 |
|:--|:--|:--|
| 1 | `.javpipe-processed.json` 台账 | 已经入库过的直接跳过 |
| 2 | 媒体库目录名 | 库里已经有这个番号 |
| 3 | **失败区**的完整片子 | 刮削没过 ≠ 没下过（**这条最容易被漏掉，漏了就是白下 10GB**） |
| 4 | 下载区的完整片子 | 下好了还没刮 |

## 定时跑

```bash
# crontab -e
# 每 15 分钟扫一次下载目录（刮削 + 入库）
*/15 * * * * cd /path/to/javpipe && .venv/bin/javpipe scrape >> /var/log/javpipe.log 2>&1
# 每小时救一次失败区，错开 5 分钟
5 * * * *      cd /path/to/javpipe && .venv/bin/javpipe rescue >> /var/log/javpipe.log 2>&1
```

`examples/cron.txt` 里是可直接复制的版本。

## 接 Jellyfin / Emby

把 `library_root` 加成一个媒体库，类型选**电影（Movies）**，勾上：

- ✅ 优先使用内嵌信息 / 本地 NFO
- ✅ 实时监控（新入库的片子会自动刷出来）

NFO 里的 `<set>` 按番号分组，同一番号的多段片会聚成一部。

## 常见问题

**Q：报 Cloudflare 403 / 页面解析失败？**
A：`curl_cffi` 必须装（它才有浏览器指纹）。装完还是 403 就是 IP 信誉问题，加代理：
`proxy: "http://127.0.0.1:7890"`。

**Q：磁力一直不开始下载？**
A：看 `min_kbps`。tracker 报的 seeders 和真实连得上的 peer 是两回事，`180s / 200KB/s`
是实测比较稳的判定线。想更激进就调小 `stall_window`。

**Q：为什么我下的 4K 变成 1080p？**
A：不走磁力、只走流媒体时，MissAV 多数只提供 720p/1080p。要 4K 就得配 bitmagnet
（或者手动把磁力丢进 qB，`javpipe scrape` 会接手）。

**Q：入库后 Jellyfin 里没有封面？**
A：封面下载失败（图床抽风）不影响入库。跑 `javpipe rescue` 重试，或手动放一张
`<CODE>-poster.jpg` 进去。

**Q：刮削失败的片子会丢吗？**
A：不会。进 `<failed_root>/<番号>/`，片子完整，只是元数据没拿到。`javpipe rescue`
会一直尝试，3 次不过写 `.skip` 标记不再骚扰站点。

## 免责声明

本项目只提供**技术实现**，不含任何内容、索引或资源。使用者需自行确保其行为符合所在地
法律法规，并仅将本工具用于有合法访问权限的私有内容整理。作者不对使用者的行为承担任何责任。

## English

**javpipe** is a self-hosted media pipeline for NAS: give it a code (or a page URL) and it
finds a source, downloads it, scrapes metadata, and files it into a Jellyfin-ready library.

- Two download paths: qBittorrent magnets (with stall detection and automatic fallback) and
  concurrent HLS segment downloads.
- Multi-source fallback: magnets → MissAV → Jable; JavBus first for metadata, MissAV/Jable
  as fallback so missing pages never break the chain.
- Four-level dedup (ledger → library → failed area → download area) so nothing is ever
  downloaded twice.
- Failure rescue: un-scrapable items go to an isolated `failed/` dir (never deleted) and are
  retried by `javpipe rescue`.
- Multipart-torrent protection: several >1GB files are treated as parts of one title, not as
  junk to delete.
- Everything machine-specific (paths, proxy, passwords) lives in `config.yaml` / env vars.

```bash
pip install -e . && javpipe doctor
javpipe run SSIS-885
```

MIT License.

## License

MIT © yadonl
