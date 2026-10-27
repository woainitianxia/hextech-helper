# -*- coding: utf-8 -*-
"""
海克斯决策助手 · 本地数据服务

作用：替浏览器去 aramkit.cn 取最新排行，取回来交给页面。
      浏览器自己做不到这件事（对方没开跨域许可），所以需要一个本地的中间人。

用法：双击同目录下的「启动联网版.bat」
关闭：直接关掉那个黑色窗口
"""
import http.server
import socketserver
import urllib.request
import urllib.error
import json
import re
import os
import sys
import time
import threading
import webbrowser

PORT = 8765
CACHE_TTL = 600          # 缓存 10 分钟，避免频繁打扰对方服务器
LIST_URL = "https://aramkit.cn/zh-CN/augments"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HERE = os.path.dirname(os.path.abspath(__file__))
HTML_NAME = "海克斯决策助手.html"

_cache = {"ts": 0.0, "data": None, "err": None}
_lock = threading.Lock()


def clean(x):
    x = re.sub(r"<[^>]+>", "", x)
    x = x.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", x).strip()


def to_num(s):
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", s or "")
    return float(m.group(1)) if m else None


def fetch_ranks():
    """抓列表页并解析全部条目。返回 {version, updated, total, items:[...]}"""
    req = urllib.request.Request(LIST_URL, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read()
    html = raw.decode("utf-8", "ignore")

    # 页面上的版本号和统计日期
    ver = re.search(r"游戏版本:\s*v(\d+\.\d+)", html) or re.search(r"v(\d+\.\d+)", html)
    date = re.search(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})"', html) or \
           re.search(r"(\d{4}-\d{2}-\d{2})", html)

    rows = re.findall(
        r'<a href="/zh-CN/augments/([a-z0-9\-]+)" class="table-row augment-table-row">(.*?)</a>',
        html, re.S)
    if not rows:
        raise RuntimeError("页面结构变了，一条都没解析到")

    items = []
    for slug, body in rows:
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)

        m = re.search(r"<strong>([^<]*)</strong>", body)
        name = clean(m.group(1)) if m else ""

        tier = re.search(r'tier-badge" data-tier="([^"]+)"', body)
        rar = re.search(r'data-rarity="([^"]+)"', body)
        stages = [int(x) for x in re.findall(r'stage-availability-badge">阶段\s*(\d)</span>', body)]

        # 关键：这里必须用 [^<]* 不能跨标签，否则会把「阶段 1」的 1 当成胜率
        stats = re.findall(r"<strong[^>]*>([^<]*)</strong><small[^>]*>([^<]*)</small>", body)
        win = pick = None
        for v, lab in stats:
            lab = clean(lab)
            if "胜率" in lab and win is None:
                win = to_num(clean(v))
            elif "选取率" in lab and pick is None:
                pick = to_num(clean(v))

        items.append({
            "s": slug,
            "n": name,
            "t": tier.group(1) if tier else "",
            "r": rar.group(1) if rar else "",
            "g": stages,
            "w": win,
            "p": pick,
        })

    return {
        "ok": True,
        "version": ("v" + ver.group(1)) if ver else "",
        "updated": date.group(1) if date else "",
        "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(items),
        "items": items,
    }


def get_data(force=False):
    with _lock:
        now = time.time()
        if not force and _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
            d = dict(_cache["data"])
            d["cached"] = True
            d["age"] = int(now - _cache["ts"])
            return d
        try:
            d = fetch_ranks()
            _cache["data"] = d
            _cache["ts"] = now
            _cache["err"] = None
            out = dict(d)
            out["cached"] = False
            out["age"] = 0
            return out
        except Exception as e:
            _cache["err"] = str(e)
            if _cache["data"]:
                d = dict(_cache["data"])
                d["cached"] = True
                d["age"] = int(now - _cache["ts"])
                d["stale"] = True
                d["err"] = str(e)
                return d
            raise


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HextechHelper/1.0"

    def log_message(self, fmt, *args):
        # 只打印关键信息，别刷屏
        if "/api/" in (args[0] if args else ""):
            sys.stdout.write("  [%s] %s\n" % (time.strftime("%H:%M:%S"), args[0] if args else ""))
            sys.stdout.flush()

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/ranks":
            force = "refresh=1" in self.path
            try:
                self._json(get_data(force=force))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 502)
            return

        if path == "/api/ping":
            self._json({"ok": True, "v": 1})
            return

        if path in ("/", "/index.html"):
            fp = os.path.join(HERE, HTML_NAME)
            if not os.path.exists(fp):
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(("找不到 %s，请确认它和 server.py 在同一个文件夹。" % HTML_NAME).encode("utf-8"))
                return
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._cors()
        self.end_headers()
        self.wfile.write(b"404")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    url = "http://127.0.0.1:%d/" % PORT
    print("")
    print("  ==================================================")
    print("   海克斯决策助手 · 联网版")
    print("  ==================================================")
    print("")
    print("   浏览器会自动打开：%s" % url)
    print("   如果没自动打开，把这个地址复制到浏览器里也行。")
    print("")
    print("   ★ 这个黑色窗口不要关，关了页面就断线了。")
    print("   ★ 用完之后，直接关掉这个窗口就行。")
    print("")
    try:
        httpd = Server(("127.0.0.1", PORT), Handler)
    except OSError:
        print("   端口 %d 被占用了。" % PORT)
        print("   可能是已经开过一个了 —— 先去浏览器看看 %s 能不能打开。" % url)
        print("")
        input("   按回车关掉这个窗口...")
        return

    print("   服务已启动，正在打开浏览器...")
    print("")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
