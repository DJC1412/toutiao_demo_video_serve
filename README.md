# bilibili-video-server

从 B站 抓取视频（元数据 + 音视频），启动本地 HTTP 服务，供 Flutter / 前端项目通过 URL 直接拉取和播放。

## 项目结构

```
├── crawler/
│   ├── fetch_bilibili.py    # 抓取脚本
│   └── requirements.txt
├── serve/
│   └── serve.py             # HTTP 流媒体服务
├── videos/                  # 下载的视频（gitignore）
├── covers/                  # 封面图（gitignore）
└── feed_remote.json         # FeedItem 数据，服务启动时自动生成
```

## 环境要求

- Python 3.8+
- [FFmpeg](https://www.ffmpeg.org/)（音视频合并）

```bash
pip install -r crawler/requirements.txt
```

## 快速开始

### 1. 配置 Cookie

> ⚠️ **必须提供自己的 B站 Cookie**，否则只能下载 720P 且部分视频会失败。

浏览器登录 [bilibili.com](https://www.bilibili.com) → F12 → Application → Cookies → `bilibili.com`，复制 `SESSDATA` 和 `bili_jct`，写入 `crawler/cookies.txt`：

```
SESSDATA=你的值; bili_jct=你的值
```

### 2. 抓取视频

```bash
# 单视频，最高画质
python crawler/fetch_bilibili.py -u "BV1EpccznEyu" --download --cookie-file crawler/cookies.txt

# 多画质（支持切换清晰度）
python crawler/fetch_bilibili.py -u "BV1EpccznEyu" --download \
    --cookie-file crawler/cookies.txt --qualities 1080p,720p,480p

# 所有可用画质
python crawler/fetch_bilibili.py -u "BV1EpccznEyu" --download \
    --cookie-file crawler/cookies.txt --qualities all

# 批量（每行一个 URL 或 BVID）
python crawler/fetch_bilibili.py -f urls.txt --download --cookie-file crawler/cookies.txt
```

### 3. 启动服务

```bash
python serve/serve.py
```

启动后终端显示本机 IP 和端口，手机 / 模拟器连同一 WiFi 即可访问。

## 服务接口

| 端点 | 方法 | 说明 |
|---|---|---|
| `/feed_remote.json` | GET | 所有视频的 FeedItem 数据（含多画质 URL） |
| `/videos/{filename}` | GET | 视频流，支持 Range 请求（206） |
| `/covers/{filename}` | GET | 封面图片 |
| `/api/status` | GET | 服务状态 JSON（文件清单、请求日志） |
| `/` | GET | 实时仪表盘 Web 页面 |

## FeedItem 数据格式

```json
{
  "id": "BV1EpccznEyu",
  "title": "视频标题",
  "author": "UP主名称",
  "coverUrl": "http://192.168.x.x:8080/covers/BV1EpccznEyu.jpg",
  "videoUrl": "http://192.168.x.x:8080/videos/BV1EpccznEyu_1080p.mp4",
  "defaultQuality": "1080P 高清",
  "qualities": {
    "1080P 高清": "http://192.168.x.x:8080/videos/BV1EpccznEyu_1080p.mp4",
    "720P 准高清": "http://192.168.x.x:8080/videos/BV1EpccznEyu_720p.mp4",
    "480P 标清": "http://192.168.x.x:8080/videos/BV1EpccznEyu_480p.mp4"
  },
  "orientation": "vertical",
  "likeCount": 500028,
  "commentCount": 17141,
  "shareCount": 29431,
  "aiTag": "分区,标签1,标签2"
}
```

## Flutter 接入

```dart
// 1. 拉取数据
final resp = await http.get(Uri.parse('http://192.168.x.x:8080/feed_remote.json'));
final items = json.decode(resp.body);

// 2. 播放
_controller = VideoPlayerController.networkUrl(Uri.parse(items[0]['videoUrl']));

// 3. 切换画质（进度不丢失）
Future<void> switchQuality(String qualityName) async {
  final url = items[0]['qualities'][qualityName];
  final pos = _controller.value.position;
  await _controller.dispose();
  _controller = VideoPlayerController.networkUrl(Uri.parse(url));
  await _controller.initialize();
  await _controller.seekTo(pos);
  await _controller.play();
}
```

> Android: `AndroidManifest.xml` 添加 `android:usesCleartextTraffic="true"`

## 实现要点

- **DASH 音视频合并** — B站 DASH 流音视频分离，脚本抓取后 ffmpeg 合并为单文件 mp4
- **MOOV 前置** — `-movflags +faststart`，ExoPlayer 流式播放必需
- **Range 请求** — 自定义 HTTP Handler 手动实现 `206 Partial Content`，支持拖动进度条
- **CORS** — 所有响应自动注入跨域头
- **多线程** — `ThreadingMixIn`，多客户端同时拉流不阻塞
