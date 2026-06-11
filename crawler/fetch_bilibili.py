"""
从 B站 抓取视频数据和下载视频文件，输出 Flutter FeedItem 格式的 JSON。

═══ 快速开始 ═══
  # 未登录（最高 720P）
  python fetch_bilibili.py -u "BV1EpccznEyu" --download

  # 登录后（最高 4K）—— 三种方式任选
  python fetch_bilibili.py -u "BV1EpccznEyu" --download --cookie "SESSDATA=xxx;bili_jct=xxx;..."
  python fetch_bilibili.py -u "BV1EpccznEyu" --download --cookie-file cookies.txt
  set BILIBILI_COOKIE=SESSDATA=xxx;bili_jct=xxx;...   # 环境变量

═══ 如何获取 Cookie ═══
  1. 浏览器打开 bilibili.com 并登录
  2. F12 → Application → Cookies → bilibili.com
  3. 复制以下字段，拼成字符串：
     SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx; DedeUserID__ckMd5=xxx; buvid3=xxx
     至少需要 SESSDATA 和 bili_jct
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests

# ── 配置 ──────────────────────────────────────────────────
OUTPUT_JSON = "feed_data.json"
VIDEO_DIR = "videos"
COVER_DIR = "covers"
REQUEST_DELAY = 1.0
FFMPEG = "D:/FFmpeg/ffmpeg-2026-06-08-git-6028720d70-full_build/bin/ffmpeg.exe"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}


# ── Cookie 管理 ───────────────────────────────────────────

def load_cookie(cli_cookie: str = None, cli_file: str = None) -> str:
    """优先级: CLI --cookie > CLI --cookie-file > 环境变量 BILIBILI_COOKIE"""
    if cli_cookie:
        return cli_cookie.strip()
    if cli_file and os.path.exists(cli_file):
        return Path(cli_file).read_text(encoding="utf-8").strip()
    return os.environ.get("BILIBILI_COOKIE", "")


def apply_cookie(cookie: str) -> None:
    """将 cookie 字符串写入全局 HEADERS 和 requests session。"""
    if not cookie:
        return
    HEADERS["Cookie"] = cookie
    # 同时更新 session，确保重定向等场景也携带
    requests.Session().cookies.update(parse_cookie_string(cookie))


def parse_cookie_string(cookie_str: str) -> dict:
    """将 cookie 字符串解析为 {key: value} 字典。"""
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            result[k.strip()] = v.strip()
    return result


def check_login() -> dict | None:
    """验证 cookie 是否有效，返回用户信息或 None。"""
    if "Cookie" not in HEADERS:
        return None
    try:
        resp = requests.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers=HEADERS,
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0 and data["data"].get("isLogin"):
            return {
                "name": data["data"]["uname"],
                "mid": data["data"]["mid"],
                "level": data["data"]["vip"]["label"]["text"]
                if data["data"].get("vip", {}).get("label")
                else "普通",
            }
    except Exception:
        pass
    return None


# ── 辅助函数 ──────────────────────────────────────────────

def extract_bvid(url_or_id: str) -> str:
    if re.match(r"^BV[a-zA-Z0-9]{10}$", url_or_id):
        return url_or_id
    m = re.search(r"(BV[a-zA-Z0-9]{10})", url_or_id)
    if m:
        return m.group(1)
    raise ValueError(f"无法从输入中提取 BVID: {url_or_id}")


def api_get(endpoint: str, params: dict = None) -> dict:
    url = f"https://api.bilibili.com{endpoint}"
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"API 错误 (code={data.get('code')}): {data.get('message')}"
        )
    return data["data"]


# ── 数据抓取 ──────────────────────────────────────────────

def fetch_video_info(bvid: str) -> dict:
    return api_get("/x/web-interface/view", {"bvid": bvid})


def fetch_video_stat(bvid: str) -> dict:
    return api_get("/x/web-interface/archive/stat", {"bvid": bvid})


def fetch_video_tags(aid: int) -> list[str]:
    try:
        data = api_get("/x/tag/archive/tags", {"aid": aid})
        return [t["tag_name"] for t in data] if isinstance(data, list) else []
    except Exception:
        return []


# B站画质 qn 值映射
QN_MAP = {
    "240p": 6,   "360p": 16,  "480p": 32,  "720p": 64,
    "1080p": 80, "1080p+": 112, "1080p60": 116, "4k": 120,
    "hdr": 125,  "dolby": 126, "8k": 127,
}
QN_REVERSE = {v: k.upper() for k, v in QN_MAP.items()}

# B站 quality_id → 短画质名（用于文件名）
QN_SHORT = {
    6: "240p", 16: "360p", 32: "480p", 64: "720p",
    74: "720p60", 80: "1080p", 112: "1080p_high", 116: "1080p60",
    120: "4k", 125: "hdr", 126: "dolby", 127: "8k",
}


def _build_quality_map(support_formats: list) -> dict:
    """从 support_formats 构建 {qn: human_name} 映射。"""
    qm = {}
    for v in support_formats:
        qid = v.get("quality", v.get("id", ""))
        name = v.get("new_description") or v.get("display_desc") or v.get("name", "")
        if qid:
            qm[qid] = name
    return qm


def fetch_video_url(bvid: str, cid: int, target_qualities: list[str] | str = "highest") -> dict:
    """获取多个画质的视频+音频播放地址。

    B站 DASH manifest 一次返回所有可用画质的视频流，无需多次请求。
    target_qualities: "highest" / "all" / ["1080p", "720p", "480p"]
    """
    data = api_get("/x/player/playurl", {
        "bvid": bvid, "cid": cid, "qn": 127,
        "fnval": 4048, "fnver": 0, "fourk": 1,
    })

    # 画质名称映射
    quality_map = _build_quality_map(data.get("support_formats", []))

    # 解析所有 DASH 视频流，按画质分组
    dash = data.get("dash", {})
    all_video_streams = dash.get("video", [])
    audio_streams = dash.get("audio", [])

    # 取最高码率音频
    audio_url = ""
    if audio_streams:
        best_audio = max(audio_streams, key=lambda x: x.get("bandwidth", 0))
        audio_url = best_audio.get("baseUrl") or best_audio.get("base_url", "")

    # 按画质 qn 分组视频流（同一画质可能有多个 codec，取最高码率）
    quality_streams = {}
    for vs in all_video_streams:
        qn = vs.get("id", 0)
        bw = vs.get("bandwidth", 0)
        url = vs.get("baseUrl") or vs.get("base_url", "")
        if qn not in quality_streams or bw > quality_streams[qn]["bandwidth"]:
            quality_streams[qn] = {"bandwidth": bw, "url": url}

    # 可用画质列表（降序）
    accept_qn = data.get("accept_quality", [])

    # 构建 streams 输出
    def _make_entry(qn):
        name = quality_map.get(qn, QN_REVERSE.get(qn, str(qn)))
        short = QN_SHORT.get(qn, str(qn))
        return name, {"video_url": quality_streams[qn]["url"], "audio_url": audio_url, "short": short}

    if target_qualities == "highest":
        best_qn = accept_qn[0] if accept_qn else max(quality_streams.keys())
        name, entry = _make_entry(best_qn)
        streams = {name: entry}
    elif target_qualities == "all":
        streams = {}
        for qn in accept_qn:
            if qn in quality_streams:
                name, entry = _make_entry(qn)
                streams[name] = entry
    else:
        wanted_qn = set()
        for q in target_qualities:
            q_lower = q.lower()
            if q_lower in QN_MAP:
                wanted_qn.add(QN_MAP[q_lower])
        available = [qn for qn in accept_qn if qn in wanted_qn and qn in quality_streams]
        if not available:
            available = [accept_qn[0]] if accept_qn else [max(quality_streams.keys())]
        streams = {}
        for qn in available:
            name, entry = _make_entry(qn)
            streams[name] = entry

    # durl 回退
    if not streams and data.get("durl"):
        streams["默认"] = {"video_url": data["durl"][0]["url"], "audio_url": "", "short": "default"}

    return {
        "streams": streams,
        "audio_url": audio_url,
        "accept_quality": accept_qn,
    }


# ── 横竖屏判断 ────────────────────────────────────────────

def get_orientation(dimension: dict) -> str:
    if not dimension:
        return "horizontal"
    w = dimension.get("width", 1920)
    h = dimension.get("height", 1080)
    rotate = dimension.get("rotate", 0)
    if rotate in (90, 270):
        w, h = h, w
    return "vertical" if h > w else "horizontal"


# ── 分类映射 ──────────────────────────────────────────────

CATEGORY_MAP = {
    1:  "动画,二次元",      4:  "游戏,电竞",
    13: "番剧,动漫",        36: "知识,科普",
    119:"知识,科普,鬼畜",   129:"舞蹈,宅舞",
    155:"时尚,穿搭",        160:"生活,Vlog",
    165:"美食,吃播",        181:"生活,搞笑",
    188:"科技,数码",        202:"资讯,新闻",
    211:"娱乐,综艺",        217:"动物,萌宠",
    223:"汽车,测评",        234:"运动,健身",
}

def tid_to_ai_tag(tid: int, tags: list[str]) -> str:
    parts = []
    if tid in CATEGORY_MAP:
        parts.extend(CATEGORY_MAP[tid].split(","))
    parts.extend(tags[:3])
    return ",".join(parts[:6])


def tid_to_search_keyword(tid: int, tags: list[str]) -> str:
    parts = []
    if tid in CATEGORY_MAP:
        parts.append(CATEGORY_MAP[tid].split(",")[0])
    parts.extend(tags[:2])
    parts.append("热门推荐")
    return ",".join(parts[:4])


# ── 音视频合并 ────────────────────────────────────────────

def merge_video_audio(video_path: str, audio_path: str, output_path: str) -> bool:
    """用 ffmpeg 合并视频和音频轨道。"""
    import subprocess
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=300)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] ffmpeg 合并失败: {e.stderr.decode()[:300] if e.stderr else e}")
        return False


# ── 下载 ──────────────────────────────────────────────────

def download_file(url: str, save_path: str, extra_headers: dict = None) -> bool:
    if os.path.exists(save_path):
        print(f"  [SKIP] 已存在: {save_path}")
        return True
    try:
        h = {**HEADERS}
        if extra_headers:
            h.update(extra_headers)
        h["Referer"] = "https://www.bilibili.com/"
        resp = requests.get(url, headers=h, stream=True, timeout=120)
        resp.raise_for_status()
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = os.path.getsize(save_path) / 1024 / 1024
        print(f"  [OK] 下载完成 ({size_mb:.1f}MB): {save_path}")
        return True
    except Exception as e:
        print(f"  [FAIL] 下载失败: {e}")
        return False


# ── 主逻辑 ────────────────────────────────────────────────

def process_video(bvid: str, download: bool = False, target_qualities="highest") -> dict | None:
    print(f"\n{'='*60}")
    print(f"处理: {bvid}")

    info = fetch_video_info(bvid)
    aid = info["aid"]
    cid = info["cid"]
    title = info["title"]
    description = info.get("desc", "").strip()[:200]
    cover_url = info["pic"]
    author = info["owner"]["name"]
    tid = info.get("tid", 0)

    dimension = info.get("dimension", {})
    orientation = get_orientation(dimension)

    print(f"  标题: {title}")
    print(f"  作者: {author}")
    print(f"  分区: {tid}")
    print(f"  分辨率: {dimension.get('width', '?')}x{dimension.get('height', '?')} ({orientation})")

    stat = info.get("stat", {})
    if not stat:
        try:
            stat = fetch_video_stat(bvid)
        except Exception:
            pass
    like_count = stat.get("like", 0)
    comment_count = stat.get("reply", 0)
    share_count = stat.get("share", 0)

    # 并发请求：标签 + 播放地址
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        fut_tags = pool.submit(fetch_video_tags, aid)
        fut_video = pool.submit(fetch_video_url, bvid, cid, target_qualities)

        tags = fut_tags.result()
        print(f"  标签: {tags}")

        try:
            video_data = fut_video.result()
            streams = video_data["streams"]
            audio_url = video_data["audio_url"]
            quality_names = list(streams.keys())
        except Exception as e:
            print(f"  [WARN] 获取视频直链失败: {e}")
            streams = {}
            audio_url = ""
            quality_names = []

    ai_tag = tid_to_ai_tag(tid, tags)
    related_keyword = tid_to_search_keyword(tid, tags)

    default_quality = quality_names[0] if quality_names else "未知"
    print(f"  画质: {', '.join(quality_names)}")

    feed_item = {
        "id": bvid,
        "title": title,
        "description": description or title,
        "coverUrl": cover_url,
        "coverLocalPath": "",
        "videoUrl": "",
        "videoLocalPath": "",
        "qualities": {},
        "defaultQuality": default_quality,
        "type": "video",
        "aiTag": ai_tag,
        "relatedSearchKeyword": related_keyword,
        "author": author,
        "commentCount": comment_count,
        "likeCount": like_count,
        "shareCount": share_count,
        "orientation": orientation,
        "resolution": f"{dimension.get('width', '?')}x{dimension.get('height', '?')}",
        "quality": default_quality,
        "sourceUrl": f"https://www.bilibili.com/video/{bvid}",
    }

    if download and streams:
        ext = os.path.splitext(urllib.parse.urlparse(cover_url).path)[1] or ".jpg"
        ext = ext.split("?")[0]
        cover_path = os.path.join(COVER_DIR, f"{bvid}{ext}")
        b_headers = {"Referer": "https://www.bilibili.com/"}

        # 下载封面
        download_file(cover_url, cover_path)
        if os.path.exists(cover_path):
            feed_item["coverLocalPath"] = cover_path.replace("\\", "/")

        # 按画质逐个下载并合并
        for quality_name, s in streams.items():
            v_url = s["video_url"]
            short = s.get("short", quality_name.replace(" ", ""))
            merged_path = os.path.join(VIDEO_DIR, f"{bvid}_{short}.mp4")
            dash_video = os.path.join(VIDEO_DIR, f"{bvid}_{short}_video.m4s")
            dash_audio = os.path.join(VIDEO_DIR, f"{bvid}_audio.m4s")

            if os.path.exists(merged_path):
                print(f"  [SKIP] {quality_name} 已存在")
                feed_item["qualities"][quality_name] = merged_path.replace("\\", "/")
                continue

            if audio_url:
                # 并行下载视频轨 + 音频轨
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    pool.submit(download_file, v_url, dash_video, b_headers)
                    if not os.path.exists(dash_audio):
                        pool.submit(download_file, audio_url, dash_audio, b_headers)

                if os.path.exists(dash_video) and os.path.exists(dash_audio):
                    print(f"  合并 {quality_name}...")
                    if merge_video_audio(dash_video, dash_audio, merged_path):
                        size_mb = os.path.getsize(merged_path) / 1024 / 1024
                        print(f"  [OK] {quality_name} ({size_mb:.1f}MB): {merged_path}")
                        os.remove(dash_video)
                    else:
                        print(f"  [WARN] 合并失败 {quality_name}")
            else:
                download_file(v_url, merged_path, b_headers)
                size_mb = os.path.getsize(merged_path) / 1024 / 1024 if os.path.exists(merged_path) else 0
                print(f"  [OK] {quality_name} ({size_mb:.1f}MB): {merged_path}")

            if os.path.exists(merged_path):
                feed_item["qualities"][quality_name] = merged_path.replace("\\", "/")

        # 清理共享音频临时文件
        if os.path.exists(dash_audio):
            os.remove(dash_audio)

        # 最高画质设为默认
        if feed_item["qualities"]:
            feed_item["videoLocalPath"] = list(feed_item["qualities"].values())[0]
    elif streams:
        # 不下载，用 CDN 直链
        for qn, s in streams.items():
            feed_item["qualities"][qn] = s["video_url"]
        if feed_item["qualities"]:
            feed_item["videoUrl"] = list(feed_item["qualities"].values())[0]

    return feed_item


def main():
    parser = argparse.ArgumentParser(
        description="从 B站 抓取视频数据，输出 Flutter FeedItem JSON",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
═══ 获取 Cookie ═══
  1. 浏览器打开 bilibili.com 并登录
  2. F12 → Application → Cookies → bilibili.com
  3. 复制 SESSDATA 和 bili_jct 的值，拼接为: SESSDATA=xxx; bili_jct=xxx

═══ 示例 ═══
  python fetch_bilibili.py -u BV1EpccznEyu --download
  python fetch_bilibili.py -u BV1EpccznEyu --download --cookie "SESSDATA=xxx; bili_jct=xxx"
  python fetch_bilibili.py -u BV1EpccznEyu --download --cookie-file cookies.txt
  python fetch_bilibili.py -f urls.txt --download --cookie-file cookies.txt
  python fetch_bilibili.py -u BV1EpccznEyu --download --qualities 1080p,720p,480p
  python fetch_bilibili.py -u BV1EpccznEyu --download --qualities all
        """,
    )
    parser.add_argument("-u", "--urls", help="B站视频 URL 或 BVID，多个用逗号分隔")
    parser.add_argument("-f", "--file", help="包含 URL/BVID 的文本文件，每行一个")
    parser.add_argument("--download", action="store_true", help="同时下载视频和封面")
    parser.add_argument("-o", "--output", default=OUTPUT_JSON, help=f"输出 JSON 文件 (默认: {OUTPUT_JSON})")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY, help=f"请求间隔秒数 (默认: {REQUEST_DELAY})")
    parser.add_argument("--cookie", help="B站登录 Cookie 字符串 (格式: SESSDATA=xxx; bili_jct=xxx)")
    parser.add_argument("--cookie-file", help="存储 Cookie 的文本文件路径")
    parser.add_argument("--qualities", default="highest",
                        help="下载画质: highest(默认) / all / 1080p,720p,480p")
    args = parser.parse_args()

    # ── 加载 Cookie ──
    cookie = load_cookie(cli_cookie=args.cookie, cli_file=args.cookie_file)
    if cookie:
        apply_cookie(cookie)
        user = check_login()
        if user:
            print(f"[LOGIN] 已登录: {user['name']} (Lv{user.get('level', '?')})")
        else:
            print("[WARN] Cookie 可能已过期，将以游客身份继续")
    else:
        print("[INFO] 未提供 Cookie，以游客身份运行（最高 720P）")
        print("       获取方式见: python fetch_bilibili.py --help")

    # ── 收集输入 ──
    bvids = []
    if args.urls:
        for u in args.urls.split(","):
            bvids.append(extract_bvid(u.strip()))
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    bvids.append(extract_bvid(line))

    if not bvids:
        print("未提供 BVID。请使用 -u 或 -f 指定。使用 --help 查看帮助。")
        sys.exit(1)

    # 解析画质参数
    qualities = args.qualities
    if qualities and qualities != "highest" and qualities != "all":
        qualities = [q.strip() for q in qualities.split(",")]

    print(f"共 {len(bvids)} 个视频待处理")
    if args.download:
        print(f"下载模式：封面 → {COVER_DIR}/ , 视频 → {VIDEO_DIR}/")
        print(f"画质: {qualities}")

    results = []
    for i, bvid in enumerate(bvids):
        try:
            item = process_video(bvid, download=args.download, target_qualities=qualities)
            if item:
                results.append(item)
        except Exception as e:
            print(f"  [FAIL] 处理 {bvid} 失败: {e}")

        if i < len(bvids) - 1:
            time.sleep(args.delay)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*60}")
    print(f"完成！共处理 {len(results)}/{len(bvids)} 个视频")
    print(f"JSON 已写入: {os.path.abspath(args.output)}")

    if results:
        print(f"\n── 数据示例 ──")
        item = results[0]
        print(f"  id: {item['id']}")
        print(f"  画质: {list(item.get('qualities', {}).keys())}")
        print(f"  默认画质: {item.get('defaultQuality', '?')}")


if __name__ == "__main__":
    main()
