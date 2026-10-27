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
VERSION = "2026.10.27"

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


def http_get(url, timeout=20, raw=False):
    """先直连，失败再逐个试代理。返回 bytes 或 (bytes, headers)。"""
    errs = []
    attempts = [None] + _proxies()
    for px in attempts:
        try:
            handlers = []
            if px:
                handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Accept": "application/vnd.github+json"})
            with opener.open(req, timeout=timeout) as r:
                body = r.read()
                return (body, dict(r.headers)) if raw else body
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
    cfg = load_config()
    if not cfg:
        return {"ok": False, "reason": "no_config",
                "detail": "程序旁边没有 update.json，自动更新没开。"}
    url = API % (cfg["owner"], cfg["repo"])
    try:
        body = http_get(url)
        d = json.loads(body.decode("utf-8", "ignore"))
    except Exception as e:
        return {"ok": False, "reason": "net", "detail": str(e)}

    tag = (d.get("tag_name") or "").lstrip("vV")
    if not tag:
        return {"ok": False, "reason": "bad", "detail": "GitHub 上还没有发布版本。"}

    asset = None
    want = (cfg.get("asset") or "").lower()
    for a in d.get("assets") or []:
        n = (a.get("name") or "").lower()
        if want and n == want:
            asset = a
            break
        if not want and n.endswith(".zip"):
            asset = a
            break
    if asset is None:
        for a in d.get("assets") or []:
            if (a.get("name") or "").lower().endswith(".zip"):
                asset = a
                break
    if asset is None:
        return {"ok": False, "reason": "no_asset",
                "detail": "这个版本没带 zip 包（发布时要把 HextechHelper-win64.zip 传上去）。"}

    return {
        "ok": True,
        "version": VERSION,
        "latest": tag,
        "has_new": newer(tag, VERSION),
        "url": asset.get("browser_download_url"),
        "size": asset.get("size") or 0,
        "notes": (d.get("body") or "")[:500],
        "page": TAG_URL % (cfg["owner"], cfg["repo"], d.get("tag_name") or tag),
        "published": d.get("published_at") or "",
    }


# ---------------- 2. 下载 + 3. 解压 + 写更新脚本 ----------------

def _stage_dir():
    d = os.path.join(tempfile.gettempdir(), "hextech_update")
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


def download_and_stage(info, dest=None):
    """下 zip、解压、写好替换脚本。返回 {ok, script, dest, newdir}。

    dest 一般不用传（默认就是程序自己所在的目录），测试时才指定。
    """
    STATE.update({"phase": "downloading", "done": 0, "total": info.get("size") or 0,
                  "msg": "正在下载新版本…", "err": ""})
    work = _stage_dir()
    pkg = os.path.join(work, "pkg.zip")

    try:
        req = urllib.request.Request(info["url"], headers={"User-Agent": UA})
        handlers = []
        for px in _proxies()[:1]:
            handlers.append(urllib.request.ProxyHandler({"http": px, "https": px}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(req, timeout=60) as r, io.open(pkg, "wb") as f:
            total = int(r.headers.get("Content-Length") or info.get("size") or 0)
            STATE["total"] = total
            while True:
                chunk = r.read(1024 * 512)
                if not chunk:
                    break
                f.write(chunk)
                STATE["done"] += len(chunk)
    except Exception as e:
        STATE.update({"phase": "error", "err": str(e)})
        return {"ok": False, "detail": "下载失败：%s" % e}

    got = os.path.getsize(pkg)
    if info.get("size") and abs(got - int(info["size"])) > 1024:
        STATE.update({"phase": "error", "err": "体积对不上"})
        return {"ok": False, "detail": "下载不完整（%d / %d 字节）" % (got, int(info["size"]))}

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
