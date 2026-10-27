# -*- coding: utf-8 -*-
"""
海克斯决策助手 · 桌面版

一个窗口，里面就是那个网页。额外多做三件事：
1. 自动读你当前在玩的英雄和双方阵容（只在有对局时可读）
2. 替你去 ARAMKit 取最新排行
3. 认出游戏里三选一的那三个海克斯（直接抓游戏窗口，不用你把窗口缩下去）

不注入游戏、不读内存、不改游戏文件。

关于缓存：这个程序**不会把任何截图写到硬盘**。识别时画面只存在内存里，
用完立刻释放。唯一会落在硬盘上的，是 Edge 内核（WebView2）自己的运行数据，
固定放在 %LOCALAPPDATA%\\HextechHelper\\webview，**退出时会自动删掉**。
"""
import ctypes
import io
import json
import os
import shutil
import sys
import threading
import time
import traceback
from ctypes import wintypes

import webview

import server
import ocr_scan
import updater
from live import get_live

HTML_FILE = "index.html"
HTML_FILE_ALT = "海克斯决策助手.html"

APP_DIR_NAME = "HextechHelper"
CLEAN_RESULT = {"removed": 0, "freed": 0, "names": []}

# WDA_EXCLUDEFROMCAPTURE：让这个窗口「对抓屏透明」——屏幕上看得见，
# 但截屏/录屏/我们自己的识别都拿不到它。Win10 2004 起支持。
WDA_EXCLUDEFROMCAPTURE = 0x11
WDA_NONE = 0x00

SELF = {"hwnd": None, "wda": False, "topmost": False}
UPD = {}          # 自更新：暂存下载好的包


def base_dir():
    # PyInstaller 打包后，资源被解到 _MEIPASS
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


# ---------------- 本窗口句柄 / 抓屏透明 / 置顶 ----------------

def _find_own_window():
    """按标题找自己的窗口（拿不到 pywebview 的 native 时兜底）"""
    u = ctypes.windll.user32
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n:
                b = ctypes.create_unicode_buffer(n + 2)
                u.GetWindowTextW(hwnd, b, n + 2)
                if "海克斯决策助手" in b.value:
                    found.append(hwnd)
        return True

    u.EnumWindows(EnumProc(cb), 0)
    return found[0] if found else None


def attach_window(window, tries=40):
    """等窗口真的建出来，再给它挂上「抓屏看不见」，并把句柄交给识别模块。"""
    hwnd = None
    for _ in range(tries):
        try:
            native = getattr(window, "native", None)
            if native is not None and hasattr(native, "Handle"):
                hwnd = int(native.Handle.ToInt64())
                if hwnd:
                    break
        except Exception:
            hwnd = None
        time.sleep(0.1)
    if not hwnd:
        hwnd = _find_own_window()
    if not hwnd:
        return None

    wda = False
    try:
        u = ctypes.windll.user32
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.SetWindowDisplayAffinity.restype = wintypes.BOOL
        wda = bool(u.SetWindowDisplayAffinity(wintypes.HWND(hwnd), WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        wda = False

    SELF["hwnd"] = hwnd
    SELF["wda"] = wda
    ocr_scan.set_helper(hwnd, wda)
    return hwnd



# ---------------- 缓存目录：固定一处 + 退出即清 ----------------

def _local_base():
    base = os.environ.get("LOCALAPPDATA")
    if not base or not os.path.isdir(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return base


def storage_dir():
    """Edge 内核的运行数据都放这一个固定目录，不再每次启动新建临时目录。"""
    return os.path.join(_local_base(), APP_DIR_NAME, "webview")


def data_dir():
    """自己存东西的地方（**退出时不清**）：联网更新下来的海克斯名单放这里。"""
    d = os.path.join(_local_base(), APP_DIR_NAME, "data")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def names_path():
    return os.path.join(data_dir(), "names.json")


def load_names():
    """启动时用哪份名单：优先用上次联网更新存下来的（它包含烘焙进程序的那些）。"""
    baked_path = os.path.join(base_dir(), "names.json")
    try:
        baked = json.load(io.open(baked_path, encoding="utf-8"))
    except Exception:
        baked = []
    p = names_path()
    if os.path.exists(p):
        try:
            saved = json.load(io.open(p, encoding="utf-8"))
            pairs = saved.get("pairs") or []
            if len(pairs) >= len(baked) and len(pairs) >= 50:
                return [[int(i), str(n)] for i, n in pairs], "联网更新版"
        except Exception:
            pass
    return baked, "程序自带"


def _pid_path():
    return os.path.join(_local_base(), APP_DIR_NAME, "last.pid")


def _alive(pid):
    """判断某个进程还在不在（Windows）"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
    except Exception:
        pass
    return False


def _another_instance_running():
    """有没有另一个自己正在跑。跑着的话不能动缓存目录，否则会把它弄崩。"""
    try:
        p = _pid_path()
        if not os.path.exists(p):
            return False
        pid = int(io.open(p, encoding="utf-8").read().strip() or 0)
        if pid and pid != os.getpid() and _alive(pid):
            return True
    except Exception:
        pass
    return False


def _write_pid():
    try:
        os.makedirs(os.path.dirname(_pid_path()), exist_ok=True)
        io.open(_pid_path(), "w", encoding="utf-8").write(str(os.getpid()))
    except Exception:
        pass


def wipe_dir(path, tries=5):
    """删目录。被占用（比如内核还没退干净）就等一会儿重试。"""
    for i in range(tries):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            time.sleep(0.3 * (i + 1))
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    return not os.path.isdir(path)


def dir_size(path):
    total = 0
    try:
        for dp, _dn, fn in os.walk(path):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


STALE_AGE = 600          # 只清理 10 分钟前就没人动过的，正在跑的一律不碰


def _old_enough(path, secs=STALE_AGE):
    try:
        return (time.time() - os.path.getmtime(path)) > secs
    except Exception:
        return False


def clean_stale_temp():
    """清掉旧版本留在系统临时目录里的东西。

    只删两类，两个都要求「10 分钟没被改动过」，正在运行的程序一律不碰：
    1. 名字以 _MEI 开头 —— 旧单文件版 PyInstaller 的解压目录
    2. 目录里只有一个 EBWebView 子目录 —— 本程序早期版本每次启动留下的

    认不出来的、或者还在用的，全部跳过，不碰别人的东西。
    """
    import tempfile

    removed = 0
    freed = 0
    names = []
    tmp = tempfile.gettempdir()

    # 1) 旧单文件版的解压目录
    try:
        for name in os.listdir(tmp):
            if not name.startswith("_MEI"):
                continue
            d = os.path.join(tmp, name)
            if not os.path.isdir(d) or not _old_enough(d):
                continue
            sz = dir_size(d)
            if wipe_dir(d, tries=1):
                removed += 1
                freed += sz
                names.append(name)
    except Exception:
        pass

    # 2) 早期版本每次启动留下的临时 EBWebView 目录
    try:
        for name in os.listdir(tmp):
            d = os.path.join(tmp, name)
            if not os.path.isdir(d):
                continue
            if not os.path.isdir(os.path.join(d, "EBWebView")):
                continue
            if not _old_enough(d):
                continue
            # 只有「整个目录里就一个 EBWebView」才认定是早期版本留下的
            try:
                if sorted(os.listdir(d)) != ["EBWebView"]:
                    continue
            except Exception:
                continue
            sz = dir_size(d)
            if wipe_dir(d, tries=1):
                removed += 1
                freed += sz
                names.append(name)
    except Exception:
        pass

    return {"removed": removed, "freed": freed, "names": names[:20]}


class Api:
    """暴露给页面调用的方法"""

    def get_live(self):
        return get_live()

    def get_ranks(self, force=False):
        try:
            return server.get_data(force=bool(force))
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def scan_screen(self, force=False, deep=False):
        """抓一段时间的游戏画面，认出上面的海克斯名字。画面只在内存里，不落盘。

        一次点击 = Python 侧连拍 1.9 秒（躲开卡片翻转动画），认满三个就停。
        """
        try:
            r = ocr_scan.check_burst()
        except Exception as e:
            r = {"ok": False, "reason": "error", "detail": str(e), "hits": []}
        # 抓完把窗口提回来，用户才看得见结果（抓图时它可能被让开了）
        try:
            ocr_scan.bring_to_front(SELF["hwnd"])
        except Exception:
            pass
        return r

    def game_window(self):
        """游戏窗口在不在（用来告诉用户「先开一局」）"""
        try:
            return ocr_scan.game_status()
        except Exception:
            return {"found": False}

    def preload(self):
        """页面打开时叫一下，提前把识别模型读进内存"""
        ocr_scan.preload()
        return True

    def sync_names(self, pairs):
        """页面把「最新的完整名单」回传过来，识别器立刻换成这份，并保存到本地。

        页面每次连上服务器拿到新卡片都会调它 —— 以后出新海克斯不用等我重新打包。
        pairs: [[索引, 名字], ...]，索引必须和页面上那份数据数组一致。
        """
        try:
            clean = [[int(i), str(n)] for i, n in (pairs or [])
                     if isinstance(n, str) and n.strip()]
        except Exception:
            return {"ok": False, "detail": "名单格式不对"}
        if len(clean) < 50:
            return {"ok": False, "detail": "名单太少（%d 条），不敢用" % len(clean)}

        saved = False
        try:
            io.open(names_path(), "w", encoding="utf-8").write(json.dumps(
                {"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "pairs": clean},
                ensure_ascii=False))
            saved = True
        except Exception:
            pass

        try:
            n = ocr_scan.set_names([(i, name) for i, name in clean])
        except Exception as e:
            return {"ok": False, "detail": str(e), "saved": saved}

        return {"ok": True, "count": n, "saved": saved,
                "where": names_path() if saved else ""}

    # ---------- 程序自更新（GitHub Releases） ----------

    def app_version(self):
        cfg = _safe(updater.load_config)
        return {"version": updater.VERSION,
                "auto": bool(cfg),
                "repo": ("%s/%s" % (cfg["owner"], cfg["repo"])) if cfg else ""}

    def app_check_update(self):
        try:
            return updater.check()
        except Exception as e:
            return {"ok": False, "reason": "error", "detail": str(e)}

    def app_download_update(self, info):
        """点「升级」后：后台下载 + 解压 + 准备替换脚本。页面轮询 app_update_state()。"""
        def job():
            try:
                r = updater.download_and_stage(info or {})
                UPD["staged"] = r
            except Exception as e:
                updater.STATE.update({"phase": "error", "err": str(e)})
        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def app_update_state(self):
        s = dict(updater.STATE)
        s["staged"] = bool(UPD.get("staged", {}).get("ok"))
        return s

    def app_apply_update(self):
        """真正替换：启动脚本 → 关掉自己。300ms 后窗口消失，脚本负责覆盖并重新拉起。"""
        st = UPD.get("staged") or {}
        if not st.get("ok") or not st.get("script"):
            return {"ok": False, "detail": "还没下载好"}
        try:
            updater.run_staged(st["script"])
        except Exception as e:
            return {"ok": False, "detail": str(e)}

        def bye():
            time.sleep(0.4)
            for w in webview.windows:
                try:
                    w.destroy()
                except Exception:
                    pass
            time.sleep(0.2)
            os._exit(0)          # 保证进程立刻退，别让文件被占着
        threading.Thread(target=bye, daemon=True).start()
        return {"ok": True}

    def storage_info(self):
        """告诉页面现在缓存占多少（自检用）"""
        d = storage_dir()
        return {
            "dir": d,
            "mb": round(dir_size(d) / 1048576.0, 1),
            "exists": os.path.isdir(d),
            "screenshots": 0,   # 从来不往硬盘写截图
            "cleaned": CLEAN_RESULT,
        }

    def set_on_top(self, v=True):
        """置顶。直接改窗口 Z 序，不碰 pywebview 的 on_top 属性。

        旧写法 `w.on_top = v` 是在 JS 回调线程里给 Windows 窗体赋属性，属于跨线程
        动窗口，会把界面卡住。SetWindowPos 从任何线程调都安全。
        """
        hwnd = SELF["hwnd"] or _find_own_window()
        if not hwnd:
            return {"ok": False, "reason": "no_window"}
        SELF["hwnd"] = hwnd
        v = bool(v)
        ok = ocr_scan.set_window_topmost(hwnd, v)
        if ok:
            SELF["topmost"] = v
            ocr_scan.set_helper_topmost(v)
        return {"ok": ok, "on_top": SELF["topmost"], "wda": SELF["wda"]}

    def self_check(self):
        """自检：窗口、抓屏透明、游戏窗口、识别模型各是什么状态"""
        s = {
            "hwnd": SELF["hwnd"],
            "wda": SELF["wda"],
            "topmost": SELF["topmost"],
        }
        try:
            s.update(ocr_scan.game_status())
        except Exception as e:
            s["game_error"] = str(e)
        return s

    def quit(self):
        for w in webview.windows:
            w.destroy()


# ---------------- 全局热键：不切窗口也能识别 ----------------
# 为什么要有它：点按钮就必须把工具切到前台，游戏就被压在后面——
# 卡片可能跟着窗口失去焦点一起收起来，抓图自然抓不到。
# 用全局热键，焦点一直留在游戏里，抓的就是「你眼下看到的那一屏」。

HOTKEYS = [0x77, 0x78, 0x76]      # F8 / F9 / F6
HK = {"id": 0, "vk": 0}


def _hot_scan(window):
    """热键触发的一次识别：抓 → 认 → 把结果丢回页面 → 把窗口提到前面"""
    log = os.path.join(__import__("tempfile").gettempdir(), "hextech_hotkey.log")
    try:
        r = ocr_scan.check_burst()
    except Exception as e:
        r = {"ok": False, "reason": "error", "detail": str(e), "hits": []}
    try:
        io.open(log, "a", encoding="utf-8").write(
            "%s 热键触发 hits=%s pushed=%s raised=%s frames=%s 亮度=%s\n"
            % (time.strftime("%H:%M:%S"), [h["name"] for h in r.get("hits", [])],
               r.get("pushed"), r.get("raised"), r.get("frames"), r.get("bright")))
    except Exception:
        pass
    try:
        import json
        window.evaluate_js("window.hotkeyScan && window.hotkeyScan(%s)"
                           % json.dumps(r, ensure_ascii=False))
    except Exception:
        pass
    # 结果出来了就把窗口提到最前，让用户直接看到（放到最后做，别影响抓图）
    try:
        hwnd = SELF["hwnd"]
        if hwnd:
            u = ctypes.windll.user32
            u.SetForegroundWindow(wintypes.HWND(hwnd))
            if not SELF["topmost"]:
                # 顶一下再落回来：视觉上不闪，但会被提到最前
                ocr_scan._set_z(hwnd, -1)
                ocr_scan._set_z(hwnd, -2)
    except Exception:
        pass


def register_hotkey(window):
    """在后台线程里注册全局热键并等它触发。返回实际注册到的键。"""
    import threading

    def loop():
        u = ctypes.windll.user32
        for i, vk in enumerate(HOTKEYS):
            if u.RegisterHotKey(None, 0xB000 + i, 0, vk):
                HK["id"], HK["vk"] = 0xB000 + i, vk
                break
        if not HK["id"]:
            return
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312 and msg.wParam == HK["id"]:      # WM_HOTKEY
                threading.Thread(target=_hot_scan, args=(window,), daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(0.25)
    return HK["vk"]


def main():
    path = os.path.join(base_dir(), HTML_FILE)
    if not os.path.exists(path):
        alt = os.path.join(base_dir(), HTML_FILE_ALT)
        if os.path.exists(alt):
            path = alt
    if not os.path.exists(path):
        html = ("<html><body style='background:#0b0f19;color:#e6edf7;font-family:sans-serif;padding:40px'>"
                "<h2>找不到页面文件</h2><p>请把 %s 放在程序同一个文件夹里。</p></body></html>" % HTML_FILE)
        webview.create_window("海克斯决策助手", html=html, width=760, height=320)
        webview.start()
        return

    store = storage_dir()

    # 上一个自己没退干净的话，先把它留下的缓存清掉
    if not _another_instance_running():
        wipe_dir(store, tries=2)
    try:
        os.makedirs(store, exist_ok=True)
    except Exception:
        store = None      # 建不出来就让内核自己挑地方，别因此启动失败

    _write_pid()

    api = Api()
    window = webview.create_window(
        "海克斯决策助手 · 海克斯大乱斗",
        url="file:///" + path.replace("\\", "/"),
        js_api=api,
        width=1320,
        height=900,
        min_size=(1000, 660),
        text_select=True,
    )

    # 模型有 200 多 MB，趁用户还在看首页时先在后台读进来
    threading.Thread(target=lambda: _safe(ocr_scan.preload), daemon=True).start()

    # 顺手清掉旧版本留在系统临时目录里的东西（只碰 10 分钟没动过、认得出是自己的）
    def _clean():
        r = _safe(clean_stale_temp)
        if r:
            CLEAN_RESULT.update(r)
    threading.Thread(target=_clean, daemon=True).start()

    # 窗口一建好就给它挂「抓屏看不见」，否则置顶之后识别到的是工具自己
    def _after_start():
        _safe(attach_window, window)
        # 用上次联网更新存下来的名单（比烘焙进程序的更新），识别器跟着换
        try:
            pairs, where = load_names()
            n = ocr_scan.set_names([(i, name) for i, name in pairs])
            SELF["names_from"] = where
            SELF["names_count"] = n
        except Exception:
            pass
        vk = _safe(register_hotkey, window)
        if vk:
            _safe(window.evaluate_js, "window.setHotkey && window.setHotkey(%d)" % vk)

    try:
        webview.start(_after_start, debug=False, storage_path=store)
    finally:
        # 关窗之后把这一份缓存删干净，不留在硬盘上
        if store:
            wipe_dir(store, tries=6)
        try:
            os.remove(_pid_path())
        except Exception:
            pass


def scantest():
    """诊断模式：`海克斯决策助手.exe --scantest`

    真的开一个窗口、真的把它摆在屏幕中间压住游戏，再走一遍线上识别，
    把结果写到 %TEMP%\\hextech_scantest.txt。用来回答一个具体问题：
    「工具压在游戏前面的时候，抓到的到底是游戏还是它自己？」
    """
    import json
    import tempfile

    out = {}
    window = webview.create_window("海克斯决策助手 · 扫描自检",
                                   html="<body style='background:#0a0e17'></body>",
                                   width=1200, height=800, x=220, y=120)

    def job(win):
        try:
            hwnd = attach_window(win)
            time.sleep(1.0)
            if hwnd:
                ocr_scan.set_helper(hwnd, SELF["wda"])
                ocr_scan.set_window_topmost(hwnd, True)   # 故意压到游戏上面
                time.sleep(0.4)
            out["self"] = {"hwnd": SELF["hwnd"], "wda": SELF["wda"]}
            out["game"] = ocr_scan.game_status()
            out["scan"] = ocr_scan.check_burst(2.2)
        except Exception:
            out["error"] = traceback.format_exc()
        finally:
            try:
                win.destroy()
            except Exception:
                pass

    def watchdog(win):
        time.sleep(20)
        try:
            win.destroy()
        except Exception:
            pass

    threading.Thread(target=job, args=(window,), daemon=True).start()
    threading.Thread(target=watchdog, args=(window,), daemon=True).start()

    store = storage_dir()
    try:
        os.makedirs(store, exist_ok=True)
    except Exception:
        store = None
    try:
        webview.start(debug=False, storage_path=store)
    finally:
        if store:
            wipe_dir(store, tries=6)

    p = os.path.join(tempfile.gettempdir(), "hextech_scantest.txt")
    try:
        io.open(p, "w", encoding="utf-8").write(
            json.dumps(out, ensure_ascii=False, indent=1))
    except Exception:
        pass


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception:
        return None


def selftest():
    """诊断模式：`海克斯决策助手.exe --selftest`

    不开窗口，只把「识别引擎能不能加载、游戏窗口认不认得、模型跑不跑得动」查一遍，
    结果写到系统临时目录的 hextech_selftest.txt（看完可以删）。
    """
    import platform
    import tempfile

    import numpy as np

    lines = ["海克斯决策助手 自检", "=" * 34,
             "系统: %s" % platform.platform(),
             "程序目录: %s" % base_dir()]
    t0 = time.time()
    try:
        s = ocr_scan.init()
        ok = s.warm()
        lines.append("① 识别引擎加载: %s（%.1f 秒）" % ("成功" if ok else "失败", time.time() - t0))
        t1 = time.time()
        dets = s._pipeline(np.full((300, 500, 3), 40, dtype=np.uint8), ocr_scan.TIERS[0])
        lines.append("② 试跑一帧: 成功，%.0f ms，检出 %d 处文字" % ((time.time() - t1) * 1000, len(dets)))
    except Exception:
        lines.append("① 识别引擎出错:\n" + traceback.format_exc())

    try:
        st = ocr_scan.game_status()
        if st.get("found"):
            lines.append("③ 游戏窗口: 找到了 %s（PrintWindow=%s）"
                         % (st.get("rect"), st.get("printwindow")))
        else:
            lines.append("③ 游戏窗口: 没找到（对局中才能抓画面）")
    except Exception:
        lines.append("③ 游戏窗口检查出错:\n" + traceback.format_exc())

    lines.append("④ 本程序窗口句柄: %s（抓图时若正压着游戏，会自动让开一下）" % SELF["hwnd"])

    p = os.path.join(tempfile.gettempdir(), "hextech_selftest.txt")
    try:
        io.open(p, "w", encoding="utf-8").write("\n".join(lines))
    except Exception:
        pass
    return p, "\n".join(lines)


def fatal(msg):
    """没有控制台时的报错出口：写日志 + 弹窗"""
    try:
        log_path = os.path.join(os.path.dirname(sys.executable), "启动错误.log")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg[-800:], "海克斯决策助手 · 启动失败", 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _p, _txt = selftest()
        raise SystemExit(0)
    if "--scantest" in sys.argv:
        try:
            scantest()
        except Exception:
            fatal(traceback.format_exc())
        raise SystemExit(0)
    try:
        main()
    except Exception:
        fatal(traceback.format_exc())
