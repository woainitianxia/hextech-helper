# -*- coding: utf-8 -*-
"""
屏幕识别：看出游戏里那三个海克斯叫什么。

三步：
1. 抓画面 —— 优先直接抓「游戏窗口」本身（PrintWindow），窗口被挡住也能抓，
   所以不用把工具窗口缩下去，也就没有那个卡顿。抓不到才退回截桌面。
2. 找字 —— 只在游戏窗口中间那块找，先把图缩小再做文字检测（快），
   只把「大字号」的文字块送去识别（海克斯名字一定是大字），聊天、小字全跳过。
3. 对名字 —— 不指望 OCR 认全，而是拿 235 个海克斯名字去比，允许错一两个字。

实测：整轮约 0.3~0.7 秒（改之前是 5~8 秒）。
"""
import ctypes
import io
import json
import os
import threading
import time
from ctypes import wintypes
from difflib import SequenceMatcher

BASE = os.path.dirname(os.path.abspath(__file__))

# 扫描区域（相对游戏窗口宽高的比例）
REGION = (0.05, 0.15, 0.95, 0.85)

# 文字检测时把长边压到这个尺寸（原图一像素不丢，识别用的是原图）
DET_MAX = 736

# 三档筛选：先只认大字（快），不够再放宽（稳），最后整个窗口再找一遍
# 第三档的存在理由：刷新/重抽之后卡片位置、字号都可能跟第一次不一样，
# 只在中间那条带上找会漏。宁可多花 0.2 秒，也不要直接报「找不到」。
TIERS = [
    {"min_h_ratio": 0.020, "max_boxes": 14},
    {"min_h_ratio": 0.010, "max_boxes": 30},
    {"min_h_ratio": 0.007, "max_boxes": 45, "full": True},
]

MATCH_THRESHOLD = 0.62
MIN_NAME_LEN = 2


def _score(n, nn):
    """一段认出来的字（n）和海克斯名（nn）有多像，0~1。

    完全一样就是 1.0。包含关系不再一刀切给高分：
    「灵魂虹吸」包含「虹吸」，但「虹吸」只有一半覆盖率，分不到 0.8，
    这样同一处只会留下「灵魂虹吸」。
    """
    if n == nn:
        return 1.0
    r = SequenceMatcher(None, n, nn).ratio()
    if nn in n:
        cover = len(nn) / float(len(n))
        if len(nn) >= 3 and cover >= 0.5:
            r = max(r, 0.70 + 0.22 * cover)
    elif n in nn:
        cover = len(n) / float(len(nn))
        if cover >= 0.6:
            r = max(r, 0.68 + 0.22 * cover)
    return r


def _norm(s):
    """归一化：去掉标点空格，全角转半角，只留中英文数字"""
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:      # 全角转半角
            ch = chr(o - 0xFEE0)
        if ch.isalnum() or "\u4e00" <= ch <= "\u9fff":
            out.append(ch.lower())
    return "".join(out)


# ============ 抓画面 ============

# 优先用 Windows Graphics Capture：直接抓「游戏窗口自己的内容」，
# 不关心谁压在上面（工具窗口、微信、浏览器都不影响）。
# 实测：工具窗口盖住 90% 屏幕时，抓到的仍是干净的游戏画面（亮度 134）。
_WGC = {"tried": False, "ok": False, "err": ""}
_wgc_lock = threading.Lock()      # 同一时刻只允许一条捕获会话（同时开两条会卡死）


def wgc_available():
    """WGC 能用吗。

    注意：实测这个库在多线程下会**卡住整个进程**（持有 GIL，连 faulthandler 都打不出栈），
    所以默认**不启用**，只在设了环境变量 HEXTECH_WGC=1 时才走这条路（留给以后排查/实验）。
    """
    if os.environ.get("HEXTECH_WGC") != "1":
        return False
    if not _WGC["tried"]:
        _WGC["tried"] = True
        try:
            import windows_capture  # noqa: F401
            _WGC["ok"] = True
        except Exception as e:
            _WGC["ok"] = False
            _WGC["err"] = str(e)
    return _WGC["ok"]


def _wgc_frames(hwnd, max_frames=4, timeout=2.4):
    """逐个吐出游戏窗口的帧（BGR）。最多 max_frames 帧 / timeout 秒。

    一条捕获会话里连取几帧，比「一帧开一次会话」快得多。
    """
    import queue

    from windows_capture import WindowsCapture

    if not _wgc_lock.acquire(timeout=3.0):
        return                      # 上一次会话还没关干净，这一轮直接跳过
    q = queue.Queue(maxsize=8)
    state = {"stop": False}
    closed = threading.Event()

    cap = WindowsCapture(window_hwnd=hwnd, cursor_capture=False, draw_border=False,
                         minimum_update_interval=120)

    @cap.event
    def on_frame_arrived(frame, control):
        if state["stop"]:
            try:
                control.stop()
            except Exception:
                pass
            return
        try:
            # frame_buffer 是 BGRA，前三个通道正好是 BGR
            q.put_nowait(frame.frame_buffer[:, :, :3].copy())
        except Exception:
            pass

    @cap.event
    def on_closed():
        closed.set()

    ctrl = None
    try:
        ctrl = cap.start_free_threaded()
        t0 = time.time()
        n = 0
        while n < max_frames and time.time() - t0 < timeout:
            try:
                arr = q.get(timeout=0.7)
            except queue.Empty:
                continue
            n += 1
            yield arr
    finally:
        state["stop"] = True
        try:
            if ctrl is not None:
                ctrl.stop()
        except Exception:
            pass
        closed.wait(1.2)             # 等它真的关掉，再放锁给下一次
        try:
            _wgc_lock.release()
        except Exception:
            pass

class _BMI(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _Win:
    """Windows 窗口抓取（PrintWindow）。只在 Windows 上可用，失败就返回 None。"""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.gdi32 = ctypes.windll.gdi32
        self._hwnd = None
        self._cached = None      # (w, h, memdc, bmp, buf, arr) 复用的抓图缓冲
        # PrintWindow 对无边框/独占全屏的《英雄联盟》一律返回全黑（实测 pw2/pw0 都是）。
        # 更坑的是它在 1080p 的 DX 窗口上**偶尔会阻塞好几秒**（实测两次抓图间隔 21 秒的元凶）。
        # 所以默认直接不走这条路；要排查再设 HEXTECH_PW=1。
        self._pw_dead = os.environ.get("HEXTECH_PW") != "1"
        self._pw_black = 0
        self._setup()

    def _setup(self):
        u, g = self.user32, self.gdi32
        u.GetWindowDC.argtypes = [wintypes.HWND]
        u.GetWindowDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        u.IsWindow.argtypes = [wintypes.HWND]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        g.CreateCompatibleDC.argtypes = [wintypes.HDC]
        g.CreateCompatibleDC.restype = wintypes.HDC
        g.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        g.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        g.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        g.SelectObject.restype = wintypes.HGDIOBJ
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteDC.argtypes = [wintypes.HDC]
        g.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]

    def find_game(self):
        """找英雄联盟的对局窗口。

        判据只有两条：窗口类名是 `RiotWindowClass`，且尺寸像一局游戏的窗口（≥640×360）。
        **不再看标题**——标题在有些状态下是空的/本地化的，之前就是因为这个，
        用户明明在游戏里却报「没找到游戏窗口」。多个候选时取面积最大的那个。
        """
        u = self.user32
        if self._hwnd and u.IsWindow(self._hwnd):
            r = self.rect(self._hwnd)
            if r and r[2] >= 640 and r[3] >= 360:
                return self._hwnd
        self._hwnd = None

        best = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _):
            if not u.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            u.GetClassNameW(hwnd, cls, 256)
            if cls.value != "RiotWindowClass":
                return True
            r = wintypes.RECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w < 640 or h < 360:
                return True
            if not best or w * h > best[0]:
                best[:] = [w * h, hwnd]
            return True

        u.EnumWindows(EnumProc(cb), 0)
        if best:
            self._hwnd = best[1]
        return self._hwnd

    def grab(self, hwnd):
        """抓窗口内容，返回 (BGR ndarray, 宽, 高) 或 None。

        返回的是**复用缓冲区上的视图**：下一次 grab 会覆盖同一块内存。
        这样每次扫描不用重新分配 6~8 MB，也不会有任何东西落到硬盘。
        调用方必须在下次 grab 之前用完（Scanner 里有锁保证）。
        """
        import numpy as np

        u, g = self.user32, self.gdi32
        if not hwnd or not u.IsWindow(hwnd):
            return None
        if self._pw_dead:
            return None          # 这条路已经证明抓不到了，直接让上层去截桌面
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        w, h = r.right - r.left, r.bottom - r.top
        if w < 320 or h < 200:
            return None

        c = self._cached
        if not c or c[0] != w or c[1] != h:
            self.release()
            hdc = u.GetWindowDC(hwnd)
            if not hdc:
                return None
            memdc = g.CreateCompatibleDC(hdc)
            bmp = g.CreateCompatibleBitmap(hdc, w, h)
            u.ReleaseDC(hwnd, hdc)
            if not memdc or not bmp:
                if memdc:
                    g.DeleteDC(memdc)
                if bmp:
                    g.DeleteObject(bmp)
                return None
            g.SelectObject(memdc, bmp)
            buf = ctypes.create_string_buffer(w * h * 4)
            # GetDIBits 给的是 BGRA，前三个通道正好是 BGR —— 和 mss 那条路一致
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)[:, :, :3]
            c = (w, h, memdc, bmp, buf, arr)
            self._cached = c

        bmi = _BMI()
        bmi.biSize = ctypes.sizeof(_BMI)
        bmi.biWidth = w
        bmi.biHeight = -h          # 负数 = 从上到下
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        # PW_RENDERFULLCONTENT=2：能抓到被别的窗口挡住的内容
        u.PrintWindow(hwnd, c[2], 2)
        g.GetDIBits(c[2], c[3], 0, h, c[4], ctypes.byref(bmi), 0)
        return c[5], w, h

    def pw_result(self, ok):
        """记一次 PrintWindow 的战果。连续两次全黑就永久放弃这条路。"""
        if ok:
            self._pw_black = 0
            return
        self._pw_black += 1
        if self._pw_black >= 2 and not self._pw_dead:
            self._pw_dead = True
            self.release()      # 缓冲再也用不上了，马上还给系统

    def release(self):
        """把复用的位图/缓冲还回去（程序退出时调一次就够了）"""
        c = self._cached
        self._cached = None
        if not c:
            return
        _, _, memdc, bmp, _buf, _arr = c
        # 顺序不能反：先删 DC（会把位图摘下来），再删位图。
        # 位图还选在 DC 里的时候 DeleteObject 会失败，句柄就漏了。
        try:
            self.gdi32.DeleteDC(memdc)
        except Exception:
            pass
        try:
            self.gdi32.DeleteObject(bmp)
        except Exception:
            pass

    def rect(self, hwnd):
        u = self.user32
        if not hwnd or not u.IsWindow(hwnd):
            return None
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)


_win = None


def _winapi():
    global _win
    if _win is None:
        try:
            _win = _Win()
        except Exception:
            _win = False
    return _win or None


_mss_sct = None
_mss_lock = threading.Lock()


def _reset_screen():
    """把 mss 的截图句柄丢掉，下次重新建。

    mss 在切分辨率、切显示器、长时间闲置之后会突然抓不出东西（不报错，直接抛异常）。
    以前一旦碰上，后面每一次都失败——这也是「只能识别一次」的一个可能来源。
    """
    global _mss_sct
    with _mss_lock:
        try:
            if _mss_sct is not None:
                _mss_sct.close()
        except Exception:
            pass
        _mss_sct = None


def _grab_screen(rect=None):
    """截桌面（只在抓窗口失败时兜底用）。rect=(left,top,w,h) 只截那一块，否则整屏。

    mss 的句柄复用一份，不每次新建。结果是一份新的 numpy 数组，用完即弃。
    """
    global _mss_sct
    import numpy as np
    from PIL import Image
    import mss as _mss

    with _mss_lock:
        if _mss_sct is None:
            _mss_sct = _mss.MSS()
        if rect:
            shot = _mss_sct.grab({"left": rect[0], "top": rect[1],
                                  "width": rect[2], "height": rect[3]})
        else:
            shot = _mss_sct.grab(_mss_sct.monitors[1])
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    return np.array(img)[:, :, ::-1].copy()


def grab_frame():
    """抓一帧游戏画面。返回 (BGR ndarray, 来源说明)；找不到游戏窗口时 (None, "no_game")。

    顺序：① 直接抓游戏窗口（WGC，遮挡也不影响）→ ② 抓窗口（PrintWindow）
    → ③ 截桌面那块（兜底，会受别的窗口影响）。
    """
    w = _winapi()
    hwnd = w.find_game() if w else None
    if not hwnd:
        return None, "no_game"

    # ① 首选：直接抓窗口内容
    if wgc_available():
        gen = None
        try:
            gen = _wgc_frames(hwnd, max_frames=1, timeout=1.2)
            for arr in gen:
                if arr is not None and float(arr.mean()) > 6:
                    return arr, "wgc"
                break
        except Exception as e:
            _WGC["err"] = str(e)
        finally:
            try:
                if gen is not None:
                    gen.close()
            except Exception:
                pass

    got = w.grab(hwnd)
    if got:
        arr = got[0]
        # 全黑说明这游戏是独占全屏/无边框，PrintWindow 抓不到 → 退回截这个窗口所在的屏幕区域
        if arr.mean() > 6:
            w.pw_result(True)
            return arr, "game"
        w.pw_result(False)
        arr = None
    rect = w.rect(hwnd)
    if rect:
        try:
            return _capture_screen(rect), "screen"
        except Exception:
            pass
    return None, "no_game"


# ---- 工具窗口自己会不会挡在游戏前面 ----
# 优先方案：给工具窗口挂 WDA_EXCLUDEFROMCAPTURE（见 app.py），抓屏根本看不见它，什么都不用做。
# 老系统挂不上时才用后备方案：抓之前把它压到最底层，抓完放回来。

_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2
_HWND_BOTTOM = 1
_SWP = 0x0001 | 0x0002 | 0x0010      # NOSIZE | NOMOVE | NOACTIVATE

# WDA_EXCLUDEFROMCAPTURE：窗口「看得见但抓不到」
_WDA_EXCLUDEFROMCAPTURE = 0x11

_helper = {"hwnd": None, "wda": False, "topmost": False}


def set_helper(hwnd=None, wda=False):
    """app.py 启动后把自己的窗口句柄告诉这里。"""
    _helper["hwnd"] = hwnd
    _helper["wda"] = bool(wda)


def set_helper_topmost(v):
    _helper["topmost"] = bool(v)


def helper_overlaps(rect):
    """工具窗口现在是不是正挡在游戏这块区域前面（粗判：只比矩形）"""
    hwnd = _helper["hwnd"]
    if not hwnd or not rect:
        return False
    u = ctypes.windll.user32
    try:
        if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd):
            return False
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return False
        gl, gt, gw, gh = rect
        return not (r.right <= gl or r.left >= gl + gw or r.bottom <= gt or r.top >= gt + gh)
    except Exception:
        return False


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _top_window_at(x, y):
    """这个屏幕坐标上，最上面那个顶层窗口是谁"""
    u = ctypes.windll.user32
    try:
        u.WindowFromPoint.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [_POINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        h = u.WindowFromPoint(_POINT(int(x), int(y)))
        if not h:
            return None
        r = u.GetAncestor(wintypes.HWND(h), 2)      # GA_ROOT
        return int(r) if r else int(h)
    except Exception:
        return None


def _band_points(rect):
    gl, gt, gw, gh = rect
    for fx in (0.20, 0.50, 0.80):
        for fy in (0.22, 0.38, 0.55):
            yield gl + gw * fx, gt + gh * fy


def game_is_front(rect, game_hwnd):
    """游戏窗口是不是正露在这条带上（没被别的窗口压着）"""
    if not rect or not game_hwnd:
        return False
    for x, y in _band_points(rect):
        if _top_window_at(x, y) != game_hwnd:
            return False
    return True


def raise_game(game_hwnd, rect=None):
    """把游戏窗口顶到最前面（不抢焦点，只是改 Z 序）。

    为什么需要：抓图是从桌面截「游戏窗口那一块矩形」，谁在最上层就截到谁。
    用户点识别时，工具、微信、浏览器都可能在游戏上面——截出来的就是它们。
    实测撞到过：截到的是一整个微信聊天窗，识别结果里全是聊天里的字。
    """
    if not game_hwnd:
        return False
    u = ctypes.windll.user32
    try:
        if u.IsIconic(wintypes.HWND(game_hwnd)):
            u.ShowWindow(wintypes.HWND(game_hwnd), 9)      # SW_RESTORE
            time.sleep(0.20)
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos(wintypes.HWND(game_hwnd), wintypes.HWND(0),   # HWND_TOP
                       0, 0, 0, 0, _SWP)
        return True
    except Exception:
        return False


# 三张卡大致占的区域（相对游戏窗口）。工具只要压到这块，抓图就会被它污染。
CARD_AREA = (0.10, 0.04, 0.90, 0.75)


def _rect_hit(a, b):
    """两个矩形 (x,y,w,h) 有没有相交"""
    return not (a[0] + a[2] <= b[0] or a[0] >= b[0] + b[2]
                or a[1] + a[3] <= b[1] or a[1] >= b[1] + b[3])


def _helper_rect():
    hwnd = _helper["hwnd"]
    if not hwnd:
        return None
    u = ctypes.windll.user32
    try:
        if not u.IsWindow(wintypes.HWND(hwnd)) or not u.IsWindowVisible(wintypes.HWND(hwnd)):
            return None
        if u.IsIconic(wintypes.HWND(hwnd)):
            return None
        r = wintypes.RECT()
        if not u.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except Exception:
        return None


def _card_rect(rect):
    gl, gt, gw, gh = rect
    return (gl + gw * CARD_AREA[0], gt + gh * CARD_AREA[1],
            gw * (CARD_AREA[2] - CARD_AREA[0]), gh * (CARD_AREA[3] - CARD_AREA[1]))


def shove_blockers(rect, game_hwnd, keep_hwnd=None):
    """把压在「三张卡那块」上的**别人的窗口**临时挪到最底层，抓完再还原。

    实测环境里有 5 个「始终最前」的窗口（ChatGPT 客户端、雷蛇录屏浮层、Radmin VPN…），
    其中 ChatGPT 会稳定压在游戏上面——不管怎么提游戏，抓到的都是它。
    这里只动「真的挡在卡片那块」的窗口，并且记住它原来是不是最前，抓完照原样放回去。
    """
    saved = []
    if not rect or not game_hwnd:
        return saved
    mid = _card_rect(rect)
    seen = set()
    pts = [(mid[0] + mid[2] * fx, mid[1] + mid[3] * fy)
           for fx in (0.15, 0.5, 0.85) for fy in (0.25, 0.5, 0.75)]
    for x, y in pts:
        h = _top_window_at(x, y)
        if not h or h == game_hwnd or h == keep_hwnd or h in seen:
            continue
        seen.add(h)
        try:
            u = ctypes.windll.user32
            ex = u.GetWindowLongW(wintypes.HWND(h), -20)
            saved.append((h, bool(ex & 0x8)))
            _set_z(h, _HWND_BOTTOM)
        except Exception:
            pass
    return saved


def restore_blockers(saved):
    for h, was_top in saved or []:
        try:
            _set_z(h, _HWND_TOPMOST if was_top else _HWND_NOTOPMOST)
        except Exception:
            pass


def bring_to_front(hwnd):
    """把工具窗口提到用户看得见的地方（不抢焦点）。

    先设最前再落回：最前那一层里「谁最后动谁在最上面」，
    所以这一步能把它抬到普通窗口（含被我们临时顶上去的游戏）上面。
    """
    if not hwnd:
        return
    _set_z(hwnd, _HWND_TOPMOST)
    if not _helper["topmost"]:
        _set_z(hwnd, _HWND_NOTOPMOST)


def tool_covers(rect):
    """工具窗口有没有压到「三张卡那块」。

    用**矩形相交**判断，不用 WindowFromPoint 逐点问。为什么：
    `WindowFromPoint` 在 WebView2 上会返回它自己的顶层窗口（属于 msedgewebview2.exe），
    拿到的句柄既不等于「工具窗口句柄」，也看不出是谁——逐点问会漏判，
    漏判的后果就是「抓到了工具自己还没让它让开」。几何相交没有这个歧义。

    工具本来就在游戏后面时多压一次也无害（它本来就看不见）。
    """
    tr = _helper_rect()
    if not tr or not rect:
        return False
    gl, gt, gw, gh = rect
    ca = (gl + gw * CARD_AREA[0], gt + gh * CARD_AREA[1],
          gw * (CARD_AREA[2] - CARD_AREA[0]), gh * (CARD_AREA[3] - CARD_AREA[1]))
    return _rect_hit(tr, ca)


def _set_z(hwnd, after):
    """改窗口的 Z 序（after=-1 置顶，-2 取消置顶，1 压到最底）。"""
    try:
        u = ctypes.windll.user32
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        return bool(u.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(after),
                                   0, 0, 0, 0, _SWP))
    except Exception:
        return False


def set_window_topmost(hwnd, on):
    """置顶/取消置顶。

    直接调 Win32，不走 pywebview 的 window.on_top —— 那个属性是在 Windows 窗体线程
    之外被赋值的（JS 调用跑在别的线程），属于跨线程操作窗口，是「点置顶就卡住」的
    头号嫌疑。SetWindowPos 本身线程安全，从哪个线程调都行。
    """
    ok = _set_z(hwnd, _HWND_TOPMOST if on else _HWND_NOTOPMOST)
    if ok:
        _helper["topmost"] = bool(on)
    return ok


def wda_active():
    """问系统：工具窗口现在真的是「抓屏看不见」吗？

    不再相信启动时那一次的结果——置顶、切显示器、显卡驱动重置都可能让它失效。
    每次抓图前问一句，失效就当场重挂，这样才不会出现「第一次能认，之后不行」。
    """
    hwnd = _helper["hwnd"]
    if not hwnd:
        return False
    u = ctypes.windll.user32
    try:
        if not u.IsWindow(wintypes.HWND(hwnd)):
            return False
        u.GetWindowDisplayAffinity.argtypes = [wintypes.HWND,
                                               ctypes.POINTER(wintypes.DWORD)]
        u.GetWindowDisplayAffinity.restype = wintypes.BOOL
        dw = wintypes.DWORD()
        if u.GetWindowDisplayAffinity(wintypes.HWND(hwnd), ctypes.byref(dw)):
            return dw.value == _WDA_EXCLUDEFROMCAPTURE
    except Exception:
        pass
    return False


def repair_wda():
    """重挂一次「抓屏看不见」。返回是否成功。"""
    hwnd = _helper["hwnd"]
    if not hwnd:
        return False
    try:
        u = ctypes.windll.user32
        u.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.SetWindowDisplayAffinity.restype = wintypes.BOOL
        ok = bool(u.SetWindowDisplayAffinity(wintypes.HWND(hwnd),
                                             _WDA_EXCLUDEFROMCAPTURE))
        _helper["wda"] = ok
        return ok
    except Exception:
        return False


def _capture_screen(rect):
    """截桌面上的这块区域。

    工具窗口要是正压着这条带，先把它压到最底层，截完再放回来。
    这是唯一可靠的路子（见 tool_covers 里的说明：WDA 挡不住 WebView2 的内容）。
    """
    hwnd = _helper["hwnd"]
    push = tool_covers(rect)
    if push:
        _set_z(hwnd, _HWND_BOTTOM)
        time.sleep(0.08)          # 等合成器把这一帧画出来
    try:
        return _grab_screen(rect)
    finally:
        if push:
            _set_z(hwnd, _HWND_TOPMOST if _helper["topmost"] else _HWND_NOTOPMOST)
    if push:
        _set_z(hwnd, _HWND_BOTTOM)
        time.sleep(0.05)          # 等合成器把这一帧画出来（约 3 帧）
    try:
        return _grab_screen(rect)
    finally:
        if push:
            _set_z(hwnd, _HWND_TOPMOST if _helper["topmost"] else _HWND_NOTOPMOST)


def game_status():
    """给界面用：游戏窗口在不在、多大、抓图走哪条路"""
    w = _winapi()
    hwnd = w.find_game() if w else None
    if not hwnd:
        return {"found": False}
    r = w.rect(hwnd)
    return {"found": True, "rect": r, "printwindow": not w._pw_dead,
            "wda": wda_active(), "topmost": _helper["topmost"]}


# ============ 识别 ============

class Scanner:
    def __init__(self, names):
        """names: [(索引, 海克斯名)]"""
        self._names = list(names)
        self._build()
        self._ocr = None
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()   # 同一时刻只允许一次扫描（缓冲是复用的）

    def _build(self):
        self.items = [(i, n, _norm(n)) for i, n in self._names
                      if len(_norm(n)) >= MIN_NAME_LEN]

    def set_names(self, names):
        """换一份名字表（联网更新到新海克斯时用）。返回实际生效的条数。"""
        with self._lock:
            self._names = list(names)
            self._build()
            return len(self.items)

    def warm(self):
        """把两个模型都先跑一遍。

        检测模型第一次要 7 秒（建 ONNX 会话 + 首次推理），以前只预热了识别模型，
        所以用户点第一下「识别」要干等 7 秒，看着就像卡死。这里两个都热。
        """
        import numpy as np

        ok = True
        try:
            # 检测：用接近真实抓图的形状，免得第一次真扫时再付一遍尺寸相关的开销
            self._detect(np.full((DET_MAX * 9 // 16, DET_MAX, 3), 250, dtype=np.uint8))
        except Exception:
            ok = False
        try:
            self._read_crops([np.full((80, 300, 3), 255, dtype=np.uint8)])
        except Exception:
            ok = False
        return ok

    def _engine(self):
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            # 默认是「短边拉到 736」= 放大，白白多算。改成「长边压到 736」。
            for op in ocr.text_detector.preprocess_op:
                if hasattr(op, "limit_type"):
                    op.limit_type = "max"
                    op.limit_side_len = DET_MAX
            self._ocr = ocr
        return self._ocr

    def _detect(self, small):
        ocr = self._engine()
        boxes, _ = ocr.text_detector(small)
        if boxes is None or len(boxes) < 1:
            return []
        return ocr.sorted_boxes(boxes)

    def _read_crops(self, crops):
        """识别一批小图，**返回的条数必须和传入的一一对应**。

        这里踩过一个隐蔽的坑：以前是 `[r[0] for r in res if r and r[0]]`，
        把认不出字的那些过滤掉了，于是后面 zip(cand, texts) 整体错位——
        第 3 个框的文字被当成第 2 个框的。表现就是「有时候认得出、有时候认错/认不出」。
        """
        ocr = self._engine()
        res, _ = ocr.text_recognizer(crops)
        out = []
        for r in (res or []):
            out.append((r[0] or "") if r else "")
        if len(out) < len(crops):          # 引擎少给了几条就补空，宁可空也不许错位
            out.extend([""] * (len(crops) - len(out)))
        return out[:len(crops)]

    def _pipeline(self, arr, tier):
        """缩小检测 → 框放大回原图 → 只识别大字号 → 返回 [(文字, x, y)]"""
        import numpy as np
        from PIL import Image

        h, w = arr.shape[:2]
        scale = DET_MAX / float(max(h, w))
        if scale < 1:
            small = np.array(Image.fromarray(arr).resize(
                (max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR))
        else:
            small, scale = arr, 1.0

        boxes = self._detect(small)
        if not boxes:
            return []

        inv = 1.0 / scale if scale > 0 else 1.0
        cand = []
        for b in boxes:
            p = np.asarray(b, dtype=np.float32) * inv
            bh = max(np.linalg.norm(p[0] - p[3]), np.linalg.norm(p[1] - p[2]))
            bw = max(np.linalg.norm(p[0] - p[1]), np.linalg.norm(p[2] - p[3]))
            if bh < h * tier["min_h_ratio"]:
                continue
            if bh <= 0 or bw / bh > 14:      # 太扁的（血条、分隔线）跳过
                continue
            if bw > w * 0.75:                # 太宽的（横幅）跳过
                continue
            cand.append((bh, p))

        if not cand:
            return []
        cand.sort(key=lambda x: -x[0])
        cand = cand[:tier["max_boxes"]]

        ocr = self._engine()
        crops = ocr.get_crop_img_list(arr, [c[1] for c in cand])
        with self._lock:
            texts = self._read_crops(crops)

        out = []
        for (bh, p), t in zip(cand, texts):
            t = (t or "").strip()
            if not t:
                continue
            out.append((t, float(p[:, 0].mean()), float(p[:, 1].mean())))
        return out

    def scan(self, force=False, deep=False):
        """单次扫描：抓一帧认一次。返回 {ok, hits:[{i,name,score,text}], ms, src}

        加锁：抓图缓冲是复用的，两次扫描不能交叉。
        """
        with self._scan_lock:
            return self._scan(force, deep)

    def scan_burst(self, seconds=5.0, max_frames=2):
        """【线上走这条】一次点击 = 连拍 2 帧（第 2 帧才铺整屏兜底）。

        为什么是 2 帧：实测第一档（只看大字）在三张真图上都是 tier=0 就全中，
        铺整屏那一档只是兜底，放在最后一帧就够；帧数多了纯属让用户多等。
        """
        if not self._scan_lock.acquire(timeout=15):
            return {"ok": False, "reason": "busy", "hits": [], "ms": 0,
                    "detail": "上一次识别还卡着，等一下再点。"}
        try:
            t0 = time.time()
            w = _winapi()
            g = w.find_game() if w else None
            if not g:
                return {"ok": False, "reason": "no_game", "src": "no_game", "hits": [],
                        "ms": 0, "detail": "没找到游戏窗口。先在游戏里开一局。"}

            best, frames, src = None, 0, "wgc"
            used = "wgc"

            # ---------- ① 首选：直接抓窗口内容，不碰任何窗口的 Z 序 ----------
            if wgc_available():
                gen = None
                try:
                    gen = _wgc_frames(g, max_frames=max_frames, timeout=seconds)
                    for arr in gen:
                        if arr is None:
                            continue
                        frames += 1
                        # 前几帧只走快速档；最后一帧才允许铺整屏（慢，但容错高）
                        deep = (frames >= max_frames)
                        r = self.process(arr, "wgc", deep=deep)
                        if best is None or len(r.get("hits", [])) > len(best.get("hits", [])):
                            best = r
                        if len(r.get("hits", [])) >= 3 or frames >= max_frames:
                            break
                except Exception as e:
                    _WGC["err"] = str(e)
                    best = None
                finally:
                    try:
                        if gen is not None:
                            gen.close()          # 显式关掉，别等垃圾回收
                    except Exception:
                        pass

            # ---------- ② 兜底：截屏幕（会让开工具、把游戏顶前） ----------
            if best is None:
                used = "screen"
                r = self._burst_screen(w, g, t0, seconds, max_frames)
                if r is not None:
                    frames = r.pop("_frames", 0)
                    src = r.pop("_src", "screen")
                    best = r

            if best is None:
                return {"ok": False, "reason": "capture_failed", "hits": [], "ms": 0,
                        "detail": "找到游戏窗口了，但抓不到画面。再点一次试试。"}
            if not best.get("ok", True):
                return best                     # 已经是「抓不到画面」这类具体错误
            best["frames"] = frames
            best["src"] = best.get("src") or src
            best["burst_ms"] = int((time.time() - t0) * 1000)
            best["pushed"] = best.get("pushed", False)
            best["raised"] = best.get("raised", False)
            best["path"] = used
            return best
        finally:
            try:
                self._scan_lock.release()
            except Exception:
                pass

    def _burst_screen(self, w, g, t0, seconds, max_frames):
        """截屏幕连拍。抓之前确保「屏幕上最上面的是游戏」：

        ① 自己的工具窗口压着卡片区 → 压到最底层；
        ② 游戏被别的窗口（微信、浏览器）压着 → 把游戏临时设成「始终最前」，
           这样它一定盖在最上面；抓完再还原（它本来不是最前就改回去）。
        实测：不这么做的话，抓到过整屏白底的聊天窗（亮度 244）、也抓到过工具自己。
        """
        tool = _helper["hwnd"]
        pushed = False
        raised = False
        was_top = False
        shoved = []
        u = ctypes.windll.user32
        try:
            rect = w.rect(g) if g else None
            if tool and rect and tool_covers(rect):
                pushed = _set_z(tool, _HWND_BOTTOM)
                if pushed:
                    time.sleep(0.12)
            if g:
                try:
                    was_top = bool(u.GetWindowLongW(wintypes.HWND(g), -20) & 0x8)
                    # 即使它本来就是「最前」也要再提一次：最前那一层里「谁最后动谁在最上面」，
                    # 浏览器/微信如果比它晚被提起，就压在它上面了（实测抓到过整屏浏览器）。
                    _set_z(g, _HWND_TOPMOST)
                    raised = True
                    time.sleep(0.15)     # 让合成器把游戏铺到最上面
                    # 还有「始终最前」的别人压着（实测是 ChatGPT 客户端）→ 临时挪开
                    shoved = shove_blockers(rect, g, keep_hwnd=tool)
                    if shoved:
                        time.sleep(0.15)
                except Exception:
                    raised = False

            best, frames = None, 0
            deadline = t0 + seconds
            fails = 0
            while True:
                arr, src = grab_frame()
                if arr is None:
                    # 抓失败别立刻报「没找到游戏窗口」：上一帧可能刚好在切窗口/过场，
                    # 丢掉 mss 的缓存句柄、等 200ms 再来（最多补 3 次）。
                    fails += 1
                    if fails <= 3:
                        _reset_screen()
                        time.sleep(0.2)
                        continue
                    break
                if float(arr.mean()) < 3.0:
                    break
                frames += 1
                # 最后一帧才铺整屏（慢），前面都用快速档
                r = self.process(arr, src, deep=(frames >= max_frames))
                if best is None or len(r.get("hits", [])) > len(best.get("hits", [])):
                    best = r
                if len(r.get("hits", [])) >= 3 or frames >= max_frames or time.time() >= deadline:
                    break
                time.sleep(0.12)
        finally:
            restore_blockers(shoved)
            if raised and not was_top:
                _set_z(g, _HWND_NOTOPMOST)   # 它本来不是「最前」就改回去
            if pushed:
                bring_to_front(tool)

        if best is None:
            # 窗口是找到了（g 非空），但一帧都没抓出来——不能说「没找到游戏窗口」
            return {"ok": False, "reason": "capture_failed", "hits": [],
                    "ms": int((time.time() - t0) * 1000),
                    "detail": "找到游戏窗口了，但这一下抓不到画面（可能刚好在切场景）。再点一次试试。",
                    "pushed": pushed, "raised": raised,
                    "_frames": frames, "_src": "screen"}
        best["pushed"] = pushed
        best["raised"] = raised
        best["_frames"] = frames
        best["_src"] = best.get("src") or "screen"
        return best

    def _scan(self, force=False, deep=False):
        t0 = time.time()
        try:
            arr, src = grab_frame()
        except Exception as e:
            return {"ok": False, "reason": "capture_failed", "detail": str(e), "hits": []}

        if arr is None:
            return {"ok": False, "reason": "no_game", "src": src, "hits": [], "ms": 0,
                    "detail": "没找到游戏窗口。先在游戏里开一局（对局中才能抓画面）。"}

        # 整屏全黑 —— 独占全屏（真全屏）时桌面截图拿不到画面，换成无边框才行
        if float(arr.mean()) < 3.0:
            return {"ok": False, "reason": "black_frame", "src": src, "hits": [],
                    "ms": int((time.time() - t0) * 1000),
                    "detail": "抓到的画面是黑的，游戏开的可能是独占全屏。"}

        return self.process(arr, src, t0, deep=deep)

    def process(self, arr, src="frame", t0=None, deep=False):
        """对已经抓到的一帧做识别。抽出来是为了让测试能跑和线上完全一样的代码。"""
        import time as _t
        t0 = _t.time() if t0 is None else t0

        h, w = arr.shape[:2]
        box = (int(w * REGION[0]), int(h * REGION[1]), int(w * REGION[2]), int(h * REGION[3]))
        crop = arr[box[1]:box[3], box[0]:box[2]]

        # 先在中间那条带上由严到宽找两轮（够用就立刻收手，省时间也不容易误认）。
        # 两轮都没结果才铺到整屏再找一遍——宁可多花 0.2 秒，也别轻易说「找不到」。
        hits, used_tier, texts, boxes = [], 0, [], 0
        try:
            for ti, tier in enumerate(TIERS):
                # 铺整屏那一档最慢，只有明确说 deep 才跑，别让每次点击都背上它
                if tier.get("full") and not deep:
                    continue
                dets = self._pipeline(arr if tier.get("full") else crop, tier)
                t = [d[0] for d in dets]
                got = self._match(dets, (arr if tier.get("full") else crop).shape[0])
                boxes = max(boxes, len(dets))       # 诊断用：最多看到几个文字块
                if len(got) > len(hits):
                    hits, used_tier = got, ti
                if len(t) > len(texts):             # 诊断用：保留信息量最大的那次
                    texts = t
                if len(hits) >= (2 if tier.get("full") else 3):
                    break
        except Exception as e:
            arr = None
            crop = None
            return {"ok": False, "reason": "ocr_failed", "detail": str(e), "hits": []}

        bright = float(arr.mean())
        result = {
            "ok": True,
            "hits": hits,
            "texts": texts[:30],
            "ms": int((time.time() - t0) * 1000),
            "src": src,
            "tier": used_tier,
            "cached": False,
            # ---- 认不出来时靠这几条自己说明原因，不用猜 ----
            "bright": round(bright, 1),
            "boxes": boxes,               # 检出多少个文字块：0 = 这一帧真的没字
            "wda": wda_active(),          # 工具窗口现在是不是真的「抓屏看不见」
            "topmost": _helper["topmost"],
            "size": [w, h],
        }

        # 画面只在这一刻之前有用。显式断掉引用，别让 6 MB 的帧多活一秒
        arr = None
        crop = None
        dets = None
        return result

    @staticmethod
    def _signature(crop):
        """粗指纹：缩到 32x18 再按 32 级量化，动画闪烁不会让它变"""
        try:
            import numpy as np
            from PIL import Image
            g = Image.fromarray(crop).convert("L").resize((32, 18), Image.BILINEAR)
            a = (np.array(g) // 32).astype(np.uint8)
            return a.tobytes()
        except Exception:
            return None

    def _match(self, dets, crop_h):
        """拿 235 个名字去比。dets: [(文字, x, y)]

        关键点：每个识别框**只认一个**名字（取分最高的那个）。
        以前允许一个框同时认成好几个名字，于是「灵魂虹吸」会顺带把「虹吸」也认出来，
        把三选一的名额挤掉一个——实测就是这个错。
        """
        norm = []
        for t, x, y in dets:
            n = _norm(t)
            if 2 <= len(n) <= 12:        # 最长的海克斯名 7 个字，超过的就是说明/击杀通告
                norm.append((n, t, x, y))
        if not norm:
            return []

        cand = []
        for n, raw, x, y in norm:
            best_idx, best_s = None, 0.0
            for idx, name, nn in self.items:
                if abs(len(n) - len(nn)) > 6:
                    continue
                s = _score(n, nn)
                if s > best_s:
                    best_s, best_idx = s, idx
            if best_idx is not None and best_s >= MATCH_THRESHOLD:
                cand.append((best_s, best_idx, self._name_of(best_idx), raw, x, y))

        if not cand:
            return []

        # 同一个位置只留分最高的那条（名字和说明被切成两行时，两行会指向同一处）
        cand.sort(key=lambda z: -z[0])
        kept = []
        for c in cand:
            if any(abs(c[4] - k[4]) < 24 and abs(c[5] - k[5]) < 12 for k in kept):
                continue
            kept.append(c)

        out = [{"i": i, "name": nm, "score": round(s, 3), "text": t,
                "x": round(x, 1), "y": round(y, 1)} for s, i, nm, t, x, y in kept]
        if len(out) <= 3:
            out.sort(key=lambda z: (z["x"], z["y"]))
            return out

        # 超过 3 个：三选一在屏幕上排成一行，按高度聚成簇，取最大的那一簇
        tol = crop_h * 0.06
        out.sort(key=lambda z: z["y"])
        groups, cur = [], [out[0]]
        for it in out[1:]:
            if abs(it["y"] - cur[-1]["y"]) <= tol:
                cur.append(it)
            else:
                groups.append(cur)
                cur = [it]
        groups.append(cur)
        groups.sort(key=len, reverse=True)
        picked = groups[0][:3]
        picked.sort(key=lambda z: z["x"])
        return picked

    def _name_of(self, idx):
        for i, n, _ in self.items:
            if i == idx:
                return n
        return ""


# ---- 供 app.py 调用的单例 ----
_scanner = None


def init(names_json_path=None, warm=False):
    global _scanner
    if _scanner is not None:
        if warm:
            _scanner.warm()
        return _scanner
    path = names_json_path or os.path.join(BASE, "names.json")
    names = json.load(io.open(path, encoding="utf-8"))
    _scanner = Scanner([(x[0], x[1]) for x in names])
    if warm:
        _scanner.warm()
    return _scanner


def preload():
    """启动时在后台把模型读进内存，并把窗口捕获的会话先开一次。

    捕获会话第一次要建 D3D 设备，实测 **2.7 秒**；不预热的话，用户第一次点识别就要干等这么久。
    第二次开始 150 毫秒内就能拿到帧。
    """
    def job():
        try:
            init(warm=True)
        except Exception:
            pass
    t = threading.Thread(target=job, daemon=True)
    t.start()
    return t


def release():
    """把复用的抓图缓冲还给系统（退出时用）"""
    w = _winapi()
    if w:
        try:
            w.release()
        except Exception:
            pass
    global _mss_sct
    try:
        if _mss_sct is not None:
            _mss_sct.close()
            _mss_sct = None
    except Exception:
        pass


def set_names(names):
    """运行中替换名字表（联网更新用）。"""
    s = init()
    return s.set_names(names)


def check(force=False, deep=False):
    """单次扫描（给前端手动点一下用）"""
    s = init()
    return s.scan(force=force, deep=deep)


def check_burst(seconds=1.9):
    """线上入口：一次点击盯一小段时间，认满三个立刻收手。"""
    s = init()
    return s.scan_burst(seconds=seconds)
