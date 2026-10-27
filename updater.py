# -*- coding: utf-8 -*-
"""程序自更新：从 GitHub Releases 取新版，换掉自己再重启。

为什么要单独一个文件：这段逻辑最容易出事（下载、解压、替换正在运行的文件），
放在一个地方，出问题时只看这一个文件。

流程：
1. check()         → 问 GitHub「最新版是哪个」，和本地版本比
2. download()      → 下 zip 到临时目录，核对大小
3. stage()         → 解压到临时目录，写一个 PowerShell 脚本负责「等程序退出 → 覆盖 → 重启」
4. run_staged()    → 启动那个脚本，本程序随后退出（脚本会把它拉起来）
"""
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

UA = "HextechHelper-Updater/1.0"
API = "https://api.github.com/repos/%s/%s/releases/latest"
TAG_URL = "https://github.com/%s/%s/releases/tag/%s"

# 本地版本：每次发版改这一行（日期式，好记也好比）
VERSION = "2026.10.28"

STATE = {"phase": "idle", "done": 0, "total": 0, "msg": "", "err": ""}


# ---------------- 网络（尽量自己处理代理，别指望环境变量） ----------------

def _proxies():
    """Windows 系统代理 + 环境变量，两边都试试。"""
    out = []
    try:
        env = urllib.request.getproxies()
        if env.get("https"):
            out.append(env["https"])
        if env.get("http"):
            out.append(env["http"])
    except Exception:
        pass
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        on, _ = winreg.QueryValueEx(k, "ProxyEnable")
        if on:
            srv, _ = winreg.QueryValueEx(k, "ProxyServer")
            srv = str(srv)
            if "=" in srv:                       # 形如 http=1.2.3.4:7897;https=...
                for part in srv.split(";"):
                    if part.startswith("https="):
                        out.insert(0, "http://" + part.split("=", 1)[1])
                    elif part.startswith("http="):
                        out.append("http://" + part.split("=", 1)[1])
            elif srv:
                out.insert(0, "http://" + srv)
    except Exception:
        pass
    return out


def http_get(url, timeout=20, raw=False, with_url=False, method=None):
    """先直连，失败再逐个试代理。返回 bytes / (bytes, headers) / (bytes, headers, 最终URL)。"""
    errs = []
    attempts = [None] + _proxies()
    for px in attempts:
        try:
            handlers = []
            if px:
                handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
            with opener.open(req, timeout=timeout) as r:
                body = r.read()
                hdrs = dict(r.headers)
                final = r.geturl()
                if with_url:
                    return body, hdrs, final
                return (body, hdrs) if raw else body
        except Exception as e:
            errs.append("%s: %s" % ("直连" if not px else px, e))
    raise RuntimeError("连不上 GitHub（%s）" % "；".join(errs[-2:]))


# ---------------- 版本比较 ----------------

def _nums(v):
    return [int(x) for x in re.findall(r"\d+", v or "")] or [0]


def newer(remote, local):
    a, b = _nums(remote), _nums(local)
    n = max(len(a), len(b))
    a += [0] * (n - len(a))
    b += [0] * (n - len(b))
    return a > b


# ---------------- 读配置 ----------------

def config_path():
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "update.json")


def load_config():
    """update.json：{"owner":"xxx","repo":"yyy","asset":"HextechHelper-win64.zip"}"""
    p = config_path()
    try:
        d = json.load(io.open(p, encoding="utf-8"))
        if d.get("owner") and d.get("repo"):
            return d
    except Exception:
        pass
    return None


# ---------------- 1. 查 ----------------

def check():
    """问「最新版是哪个」。

    走 **releases/latest 的网页跳转**，不用 GitHub API —— API 匿名只有 60 次/小时，
    共享网络（校园网/公司网）里很容易被别人用光，一旦 403 就会出现「检查更新一直失败」。
    跳转这条路没有配额限制，也不需要 token。
    """
    cfg = load_config()
    if not cfg:
        return {"ok": False, "reason": "no_config",
                "detail": "程序旁边没有 update.json，自动更新没开。"}
    owner, repo = cfg["owner"], cfg["repo"]
    asset_name = cfg.get("asset") or "HextechHelper-win64.zip"

    latest = "https://github.com/%s/%s/releases/latest" % (owner, repo)
    try:
        _, headers, final_url = http_get(latest, timeout=20, with_url=True)
    except Exception as e:
        return {"ok": False, "reason": "net", "detail": str(e)}

    tag = ""
    m = re.search(r"/releases/tag/([^/?#]+)", final_url or "")
    if m:
        tag = m.group(1)
    tag = tag.lstrip("vV")
    if not tag:
        return {"ok": False, "reason": "bad", "detail": "GitHub 上还没有发布版本。"}

    # 资源固定在这个地址上，不用 API 也能下
    asset_url = "https://github.com/%s/%s/releases/download/v%s/%s" % (
        owner, repo, tag, asset_name)
    size = 0
    try:
        _, h2, _ = http_get(asset_url, timeout=25, with_url=True, method="HEAD")
        size = int(h2.get("Content-Length") or 0)
    except Exception:
        pass                       # 拿不到体积不影响升级，下载时会校验

    return {
        "ok": True,
        "version": VERSION,
        "latest": tag,
        "has_new": newer(tag, VERSION),
        "url": asset_url,
        "size": size,
        "notes": "",
        "page": "https://github.com/%s/%s/releases/tag/v%s" % (owner, repo, tag),
        "published": "",
    }


# ---------------- 2. 下载 + 3. 解压 + 写更新脚本 ----------------

def _stage_dir():
    d = os.path.join(tempfile.gettempdir(), "hextech_update")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def _open_stream(url, offset=0, timeout=45):
    """打开下载流：**先直连**，失败才逐个试代理。

    踩过的坑：直接拿 `_proxies()[0]` 去下，那个代理只有 30KB/s，
    101MB 的包要下一个小时还断流；直连实测 4.9MB/s，25 秒就完了。
    """
    last = None
    for px in [None] + _proxies():
        try:
            handlers = []
            if px:
                handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
            opener = urllib.request.build_opener(*handlers)
            headers = {"User-Agent": UA}
            if offset:
                headers["Range"] = "bytes=%d-" % offset
            req = urllib.request.Request(url, headers=headers)
            return opener.open(req, timeout=timeout)
        except Exception as e:
            last = e
    raise last if last else RuntimeError("连不上")


def _download(url, path, want_size=0, tries=12):
    """带断点续传的下载：断了就从已下的字节接着下，不会再从头来。"""
    got = os.path.getsize(path) if os.path.exists(path) else 0
    if want_size and got >= want_size:
        return got
    STATE["done"] = got

    for _ in range(tries):
        try:
            r = _open_stream(url, offset=got)
            code = getattr(r, "status", 200) or 200
            if got and code != 206:        # 服务器不支持续传 → 从头来
                got = 0
            mode = "ab" if got else "wb"
            with io.open(path, mode) as f:
                while True:
                    chunk = r.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    STATE["done"] = got
                    if want_size and got >= want_size:
                        break
            try:
                r.close()
            except Exception:
                pass
        except Exception as e:
            STATE["err"] = str(e)
            time.sleep(1.2)
        if want_size and got >= want_size:
            break
        if not want_size and got > 0:
            break
        time.sleep(0.8)
    return got


def download_and_stage(info, dest=None):
    """下 zip、解压、写好替换脚本。返回 {ok, script, dest, newdir}。

    dest 一般不用传（默认就是程序自己所在的目录），测试时才指定。
    """
    STATE.update({"phase": "downloading", "done": 0, "total": info.get("size") or 0,
                  "msg": "正在下载新版本…", "err": ""})
    work = _stage_dir()
    pkg = os.path.join(work, "pkg.zip")
    want = int(info.get("size") or 0)

    try:
        got = _download(info["url"], pkg, want)
    except Exception as e:
        STATE.update({"phase": "error", "err": str(e)})
        return {"ok": False, "detail": "下载失败：%s" % e}

    if want and abs(got - want) > 1024:
        STATE.update({"phase": "error", "err": "下载不完整"})
        return {"ok": False, "detail": "下载不完整（%.1f / %.1f MB）。网络断过，再点一次会接着下。"
                                       % (got / 1048576.0, want / 1048576.0)}

    STATE.update({"phase": "extracting", "msg": "正在解压…"})
    newdir = os.path.join(work, "new")
    try:
        with zipfile.ZipFile(pkg) as z:
            z.extractall(newdir)
    except Exception as e:
        STATE.update({"phase": "error", "err": str(e)})
        return {"ok": False, "detail": "解压失败：%s" % e}

    # zip 里可能套了一层文件夹
    entries = [x for x in os.listdir(newdir) if not x.startswith("__")]
    src = newdir
    if len(entries) == 1 and os.path.isdir(os.path.join(newdir, entries[0])):
        src = os.path.join(newdir, entries[0])
    if not os.path.exists(os.path.join(src, "海克斯决策助手.exe")):
        STATE.update({"phase": "error", "err": "包里没有主程序"})
        return {"ok": False, "detail": "这个 zip 里没找到「海克斯决策助手.exe」，不是程序的包？"}

    dest = dest or os.path.dirname(os.path.abspath(sys.executable))
    ps1 = os.path.join(work, "apply.ps1")
    # 注意：PowerShell 5.1 读无 BOM 的 UTF-8 会按 GBK 解，中文路径直接乱码 → 必须 utf-8-sig
    script = u"""$ErrorActionPreference = 'SilentlyContinue'
$pidApp = %d
while (Get-Process -Id $pidApp -ErrorAction SilentlyContinue) { Start-Sleep -Milliseconds 300 }
Start-Sleep -Milliseconds 700
robocopy "%s" "%s" /E /NFL /NDL /NJH /NJS /R:2 /W:1 | Out-Null
if (Test-Path "%s") { Start-Process -FilePath "%s" -WorkingDirectory "%s" }
Start-Sleep -Seconds 3
Remove-Item -Recurse -Force "%s" -ErrorAction SilentlyContinue
""" % (os.getpid(), src, dest, os.path.join(dest, "海克斯决策助手.exe"), dest, dest, work)
    io.open(ps1, "w", encoding="utf-8-sig").write(script)

    STATE.update({"phase": "ready", "msg": "下载好了，点「立即重启并升级」"})
    return {"ok": True, "script": ps1, "dest": dest, "newdir": src,
            "from": VERSION, "to": info.get("latest")}


def run_staged(script):
    """启动更新脚本（脱离本进程），调用方随后关窗口退出。"""
    flags = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-WindowStyle", "Hidden", "-File", script],
                     creationflags=flags, close_fds=True)
    return True
