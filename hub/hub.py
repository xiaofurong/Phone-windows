#!/usr/bin/env python3
# control-hub: fnOS(Docker) 上的中转 + 网页服务
#  - 手机访问 http://192.168.31.20:8080  => 网页
#  - 网页按钮 -> 这里 -> 转发给 Windows agent (http://192.168.31.59:8765)
#  - 纯标准库，无任何第三方依赖
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

PORT = int(os.environ.get("HUB_PORT", "8080"))
PC_URL = os.environ.get("PC_AGENT_URL", "http://192.168.31.59:8765").rstrip("/")
TOKEN = os.environ.get("HUB_TOKEN", "qyx2026")

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "public", "index.html")
PUBLIC_DIR = os.path.join(HERE, "public")
# 静态资源 MIME(确保 manifest/SW/图标以正确 Content-Type 返回, 否则 iOS 拒绝)
_STATIC_MIME = {
    ".webmanifest": "application/manifest+json",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

# 手机侧路径 -> Windows agent 路径
RELAY = {
    "/api/volume":    "/volume",
    "/api/audio/set": "/audio/set",
    "/api/app":       "/app",
    "/api/apps":      "/apps",
    "/api/apps/scan": "/apps/scan",
    "/api/apps/save": "/apps/save",
    "/api/brightness":"/brightness",
    "/api/input":     "/input",
    "/api/status":    "/status",
    "/api/monitor/rename": "/monitor/rename",
    "/api/monitor/scale":  "/monitor/scale",
    "/api/monitor/tvswitch": "/monitor/tvswitch",
    "/api/monitor/primary": "/monitor/primary",
    "/api/monitor/blank":   "/monitor/blank",
    "/api/monitor/unblank": "/monitor/unblank",
    "/api/input/recover":   "/input/recover",
    "/api/vinput/mouse": "/vinput/mouse",
    "/api/vinput/key":   "/vinput/key",
    "/api/vinput/text":  "/vinput/text",
    "/api/vinput/pos":   "/vinput/pos",
    "/api/vinput/wheellines": "/vinput/wheellines",
    "/api/box/status":   "/box/status",
    "/api/box/key":      "/box/key",
    "/api/box/tap":      "/box/tap",
    "/api/box/swipe":    "/box/swipe",
    "/api/box/text":     "/box/text",
}

# GET 也能转发的路径(status 只读 + 盒子状态只读 + 应用列表/扫描只读)
GET_RELAY = {"/api/status", "/api/box/status", "/api/apps", "/api/apps/scan"}

def relay(method, path, body=None):
    """转发到 Windows agent，返回 (status, dict)。"""
    target = PC_URL + RELAY[path]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(target, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Token", TOKEN)
    try:
        with urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {"ok": False, "error": str(e)}
    except URLError as e:
        return 502, {"ok": False, "error": "Windows 端不可达: %s" % e.reason}
    except Exception as e:
        return 500, {"ok": False, "error": str(e)}


class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            try:
                with open(INDEX, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self._send(500, {"ok": False, "error": "index.html 缺失"})
            return
        # 静态资源(public/ 下, 供 PWA manifest / icons / sw.js 等)
        path_only = self.path.split("?", 1)[0]
        if path_only != "/" and not path_only.startswith("/api/"):
            rel = path_only.lstrip("/")
            # 防穿越: 拒绝 ..
            if ".." not in rel:
                fp = os.path.join(PUBLIC_DIR, rel)
                if os.path.isfile(fp):
                    ext = os.path.splitext(rel)[1].lower()
                    mime = _STATIC_MIME.get(ext, "application/octet-stream")
                    try:
                        with open(fp, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Length", str(len(data)))
                        # manifest 和 sw 不缓存, 便于升级; 图标可长缓存
                        if ext in (".png", ".jpg", ".jpeg", ".ico"):
                            self.send_header("Cache-Control", "public, max-age=86400")
                        self.end_headers()
                        self.wfile.write(data)
                    except Exception:
                        self._send(500, {"ok": False, "error": "static read failed"})
                    return
        if path_only in GET_RELAY:
            code, obj = relay("GET", path_only)
            self._send(code, obj)
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p not in RELAY:
            self._send(404, {"ok": False, "error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = None
        if length:
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except Exception:
                body = {}
        code, obj = relay("POST", p, body)
        self._send(code, obj)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("control-hub 启动: 本机 :%d  ->  Windows agent %s  (token=%s)" % (PORT, PC_URL, TOKEN))
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
