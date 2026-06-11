"""
启动本地 HTTP 文件服务，供 Flutter 项目通过 URL 拉取视频和封面。
支持 Range 请求（视频流播放必需）、CORS、正确的 MIME 类型。

用法:
    python serve.py              # 默认端口 8080
    python serve.py -p 9000      # 自定义端口
"""

import argparse
import json
import mimetypes
import os
import re
import socket
import sys
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class VideoHTTPHandler(SimpleHTTPRequestHandler):
    """支持 Range 请求 + CORS + 正确 Content-Type 的文件服务。"""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp4": "video/mp4",
        ".m4s": "video/iso.segment",
        ".m4v": "video/x-m4v",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".flv": "video/x-flv",
    }

    # 全局请求日志（类变量，所有实例共享）
    request_log: list = []
    request_count: int = 0
    start_time: float = 0.0

    def _log_request(self, method, path, code, size=0):
        VideoHTTPHandler.request_count += 1
        VideoHTTPHandler.request_log.insert(0, {
            "time": time.strftime("%H:%M:%S"),
            "method": method,
            "path": path,
            "code": code,
            "size": f"{size/1024:.0f}KB" if size > 1024 else f"{size}B",
        })
        if len(VideoHTTPHandler.request_log) > 50:
            VideoHTTPHandler.request_log.pop()

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Accept-Ranges, Content-Length")
        self.send_header("Accept-Ranges", "bytes")

    def end_headers(self):
        self._cors_headers()
        super().end_headers()

    # ── 仪表盘 ────────────────────────────────────────────

    DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>视频服务 · 仪表盘</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:monospace;background:#0f172a;color:#e2e8f0;padding:20px}
h1{color:#38bdf8;margin-bottom:4px}
.sub{color:#64748b;font-size:13px;margin-bottom:24px}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:8px;animation:pulse 2s infinite}
.dot.ok{background:#22c55e}
.dot.err{background:#ef4444;animation:none}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.card{background:#1e293b;border-radius:8px;padding:16px;margin-bottom:16px}
.card h2{font-size:14px;color:#94a3b8;margin-bottom:12px;text-transform:uppercase;letter-spacing:1px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.stat{text-align:center}
.stat .val{font-size:28px;font-weight:bold;color:#38bdf8}
.stat .lbl{font-size:11px;color:#64748b;margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;color:#64748b;padding:6px 8px;border-bottom:1px solid #334155}
td{padding:6px 8px;border-bottom:1px solid #1e293b}
tr:hover{background:#334155}
.poll-info{float:right;color:#64748b;font-size:11px}
.poll-count{color:#38bdf8}
</style>
</head>
<body>
<h1>🎬 视频流媒体服务</h1>
<p class="sub" id="header-info">加载中...</p>

<div class="card">
<h2>📊 概览</h2>
<div class="stats">
<div class="stat"><div class="val" id="val-videos">-</div><div class="lbl">视频文件</div></div>
<div class="stat"><div class="val" id="val-covers">-</div><div class="lbl">封面文件</div></div>
<div class="stat"><div class="val" id="val-requests">-</div><div class="lbl">总请求数</div></div>
<div class="stat"><div class="val" id="val-qualities">-</div><div class="lbl">可用画质</div></div>
</div>
</div>

<div class="card">
<h2>📁 文件清单</h2>
<table>
<thead><tr><th></th><th>类型</th><th>文件名</th><th>大小</th><th>状态</th></tr></thead>
<tbody id="files-body"><tr><td colspan="5" style="color:#64748b">加载中...</td></tr></tbody>
</table>
</div>

<div class="card">
<h2>📡 最近请求 (<span class="poll-info">实时更新 · <span class="poll-count" id="poll-tick">0</span></span>)</h2>
<table>
<thead><tr><th>时间</th><th>状态</th><th>方法</th><th>路径</th><th>大小</th></tr></thead>
<tbody id="logs-body"><tr><td colspan="5" style="color:#64748b">加载中...</td></tr></tbody>
</table>
</div>

<script>
let tick = 0;
function fmtUptime(s) {
  if (s < 60) return s + 's';
  let h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + 'h ' : '') + m + 'm ' + sec + 's';
}
async function poll() {
  try {
    let r = await fetch('/api/status');
    let d = await r.json();
    document.getElementById('header-info').innerHTML =
      d.ip + ':' + d.port + ' · 运行 ' + fmtUptime(d.uptime_sec || 0) +
      ' · <span class="dot ok"></span>在线';
    document.getElementById('val-videos').textContent = d.total_videos;
    document.getElementById('val-covers').textContent = d.total_covers;
    document.getElementById('val-requests').textContent = d.total_requests;
    document.getElementById('val-qualities').textContent = d.active_qualities || '-';
    let fhtml = '';
    d.files.forEach(function(f) {
      let s = f.accessible ? '<span class="dot ok"></span>' : '<span class="dot err"></span>';
      fhtml += '<tr><td>' + s + '</td><td>' + f.type + '</td><td title="' + f.url + '">' + f.name + '</td><td>' + f.size + '</td><td><code>HTTP ' + f.http_code + '</code></td></tr>';
    });
    document.getElementById('files-body').innerHTML = fhtml || '<tr><td colspan="5" style="color:#64748b">无文件</td></tr>';
    let lhtml = '';
    (d.recent_requests || []).forEach(function(l) {
      let c = (l.code == '200' || l.code == '206') ? '#22c55e' : '#ef4444';
      lhtml += '<tr><td>' + l.time + '</td><td style="color:' + c + '">' + l.code + '</td><td>' + l.method + '</td><td>' + l.path + '</td><td>' + l.size + '</td></tr>';
    });
    document.getElementById('logs-body').innerHTML = lhtml || '<tr><td colspan="5" style="color:#64748b">暂无请求</td></tr>';
    tick++;
    document.getElementById('poll-tick').textContent = tick;
  } catch(e) {
    document.getElementById('header-info').innerHTML = '<span class="dot err"></span>连接失败 · ' + e;
  }
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""

    def _serve_dashboard(self):
        body = self.DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_status_json(self):
        data = self._gather_status()
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _gather_status(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        videos_dir = os.path.join(base_dir, "videos")
        covers_dir = os.path.join(base_dir, "covers")

        uptime_sec = int(time.time() - VideoHTTPHandler.start_time)

        files_info = []
        qualities_seen = set()
        for d, ftype in [(videos_dir, "视频"), (covers_dir, "封面")]:
            if os.path.isdir(d):
                for fname in sorted(os.listdir(d)):
                    fpath = os.path.join(d, fname)
                    if os.path.isfile(fpath):
                        size = os.path.getsize(fpath)
                        if size < 1024:
                            size_str = f"{size}B"
                        elif size < 1024 * 1024:
                            size_str = f"{size/1024:.1f}KB"
                        else:
                            size_str = f"{size/1024/1024:.1f}MB"
                        for q in ["1080p", "720p", "480p", "360p", "4k"]:
                            if q in fname.lower():
                                qualities_seen.add(q)
                        url_path = f"/{ftype == '视频' and 'videos' or 'covers'}/{fname}"
                        files_info.append({
                            "type": ftype,
                            "name": fname,
                            "size": size_str,
                            "size_bytes": size,
                            "url": url_path,
                            "accessible": True,
                            "http_code": 200,
                        })

        return {
            "ip": "192.168.2.8",
            "port": 8080,
            "uptime_sec": uptime_sec,
            "total_videos": sum(1 for f in files_info if f["type"] == "视频"),
            "total_covers": sum(1 for f in files_info if f["type"] == "封面"),
            "active_qualities": ", ".join(sorted(qualities_seen)) if qualities_seen else "-",
            "total_requests": VideoHTTPHandler.request_count,
            "files": files_info,
            "recent_requests": list(VideoHTTPHandler.request_log),
        }

    # ── 请求处理 ──────────────────────────────────────────

    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_dashboard()
            return
        if self.path == "/api/status":
            self._serve_status_json()
            return

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            super().do_GET()
            return

        file_size = os.path.getsize(path)
        range_header = self.headers.get("Range")
        is_head = self.command == "HEAD"

        if range_header:
            self._handle_range_request(path, file_size, range_header, is_head)
        else:
            self._handle_full_request(path, file_size, is_head)

    def _handle_range_request(self, path, file_size, range_header, is_head):
        m = re.match(r"bytes=(\d+)-(\d*)", range_header)
        if not m:
            self.send_error(416, "Requested Range Not Satisfiable")
            return
        start = int(m.group(1))
        end_str = m.group(2)
        end = int(end_str) if end_str else file_size - 1
        if start >= file_size or end >= file_size:
            self.send_error(416, "Requested Range Not Satisfiable")
            return

        length = end - start + 1
        content_type, _ = mimetypes.guess_type(path)
        self.send_response(206)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.send_header("Content-Length", str(length))
        self.end_headers()

        if not is_head:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk_size = min(65536, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    self.wfile.write(data)
                    remaining -= len(data)

    def _handle_full_request(self, path, file_size, is_head):
        content_type, _ = mimetypes.guess_type(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(file_size))
        self.end_headers()
        if not is_head:
            with open(path, "rb") as f:
                while True:
                    data = f.read(65536)
                    if not data:
                        break
                    self.wfile.write(data)

    def log_message(self, format, *args):
        code = args[1] if len(args) > 1 else ""
        marker = ""
        if code == "206":
            marker = "[RANGE]"
        elif code == "200" and getattr(self, "path", "").endswith(".mp4"):
            marker = "[VIDEO]"
        self._log_request(self.command, self.path, code, 0)
        sys.stdout.write(f"  {marker} {self.address_string()} {format % args}\n")
        sys.stdout.flush()


# ── IP / Feed 工具 ──────────────────────────────────────

def get_local_ips() -> list:
    ips = []
    try:
        for name in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not name.startswith("127."):
                ips.append(name)
    except Exception:
        pass
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
            s.close()
        except Exception:
            ips.append("无法获取")
    return ips


def update_feed_json(host: str, port: int, json_path: str = "feed_data.json"):
    if not os.path.exists(json_path):
        return None
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    base = f"http://{host}:{port}"
    for item in data:
        if item.get("qualities"):
            remote_qualities = {}
            for qname, local_path in item["qualities"].items():
                fname = os.path.basename(local_path)
                remote_qualities[qname] = f"{base}/videos/{fname}"
            item["qualities"] = remote_qualities
            default_q = item.get("defaultQuality")
            if default_q and default_q in remote_qualities:
                item["videoUrl"] = remote_qualities[default_q]
            elif remote_qualities:
                item["videoUrl"] = list(remote_qualities.values())[0]
        elif item.get("videoLocalPath"):
            fname = os.path.basename(item["videoLocalPath"])
            item["videoUrl"] = f"{base}/videos/{fname}"
        if item.get("coverLocalPath"):
            fname = os.path.basename(item["coverLocalPath"])
            item["coverUrl"] = f"{base}/covers/{fname}"
    remote_json = "feed_remote.json"
    with open(remote_json, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return remote_json, data


# ── 入口 ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="启动本地 HTTP 文件服务")
    parser.add_argument("-p", "--port", type=int, default=8080, help="端口 (默认: 8080)")
    args = parser.parse_args()

    port = args.port
    ips = get_local_ips()
    host = ips[0]

    print(f"\n{'='*55}")
    print(f"  视频文件服务已启动")
    print(f"{'='*55}")
    print(f"  端口: {port}")
    for ip in ips:
        print(f"  地址: http://{ip}:{port}")
    print(f"{'='*55}")
    print(f"  支持: Range 请求 / CORS / 多线程")
    print(f"{'='*55}")

    result = update_feed_json(host, port)
    if result:
        remote_json, _ = result
        print(f"\n  [OK] {remote_json} 已生成")

    print(f"\n  仪表盘: http://{host}:{port}/")
    print(f"  状态API: http://{host}:{port}/api/status")
    print(f"  按 Ctrl+C 停止服务\n")

    VideoHTTPHandler.start_time = time.time()
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), VideoHTTPHandler)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  服务已停止")
        httpd.server_close()


if __name__ == "__main__":
    main()
