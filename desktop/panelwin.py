# -*- coding: utf-8 -*-
"""WebView2 面板宿主 —— 进程内独立窗口，替代独立 Edge 进程。

背景
----
旧方案：面板 = 独立 Edge `--app` 窗口（458MB profile、独立进程、系统标题栏直角），
推送靠 Node + CDP。缺点：启动慢（拉起独立 Edge + 冷 profile）、窗口外观不可控、
依赖 Node.js。

新方案：用 WebView2（Edge 内核的嵌入版，Win10/11 自带）把同一个网页嵌进
本进程的无边框窗口里：圆角（Win10 无 DWM 圆角，用 SetWindowRgn 裁剪）、
无浏览器痕迹、推送从「起 Node 子进程走 CDP」变成进程内 evaluate_js。

线程模型（webview 模式）
------------------------
  主线程    ：run_gui()（pywebview 强制要求主线程；其 GUI 循环即消息泵）
  平台线程  ：lens.py 的全局钩子 + 热键 + GetMessage 泵（原主线程逻辑整体搬迁；
              低级钩子只要求「安装线程有消息泵」，不要求是进程主线程）
  Tk 线程   ：气泡 / 询问框（不变）
  托盘线程  ：TrayIcon 自带（不变）

钥匙与推送
----------
页面本身不带钥匙（防止白嫖额度）。webview 模式下钥匙只在本进程内存里，
用 evaluate_js 调 window.__lzrUnlock 注入 —— 连 stdin 管道都不经过，
比旧的「Node 子进程 + CDP」泄露面更小。
"""

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

try:
    import webview as _webview
except Exception:                                    # pragma: no cover
    _webview = None

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi")
except Exception:                                    # pragma: no cover
    dwmapi = None

WINDOW_TITLE = "1z read"          # pywebview 窗口标题（也是找 hwnd 的依据）
CORNER_RADIUS = 14                # 圆角半径（CSS 逻辑像素，用时按 DPI 放大）
PANEL_W = 560                     # 设计宽度（CSS 逻辑像素，与旧 panel_args 一致）
PANEL_H = 860                     # 设计高度（CSS 逻辑像素，放不下工作区时会收）
STORAGE_DIR_NAME = "webview_storage"

# pywebview 的 WEBVIEW2 runtime 检测 GUID（EdgeUpdate Clients）
_WV2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# ── Win32 窗口动作常量 ──
SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9
GWL_STYLE = -16
WS_MINIMIZEBOX = 0x00020000       # 不给这三个位，任务栏点击/右键菜单会失灵
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SPI_GETWORKAREA = 0x0030          # 工作区（排除任务栏）—— 最大化按它算

# ── DWM 非客户区（方形边框/投影的根源，见 _no_dwm_frame） ──
DWMWA_NCRENDERING_ENABLED = 1
DWMWA_NCRENDERING_POLICY = 2
DWMNCRP_DISABLED = 1

# ── 任务栏 / Alt+Tab 图标 ──
# pywebview 的 WinForms 窗口自己不带图标，任务栏按钮就退回 .NET 的
# 「通用窗口」图标（一个灰扑扑的小方块）——用户看到的「图标不对」。
# 对策：按 DPI 加载两档 app.ico，用 WM_SETICON 装到窗口上（大图标给
# Alt+Tab / 任务栏，小图标给标题栏和窗口菜单），同时写进窗口类，
# 免得以后 WinForms 新建的对话框又没有图标。
WM_SETICON = 0x0080
WM_GETICON = 0x007F
ICON_SMALL = 0
ICON_BIG = 1
ICON_SMALL2 = 2
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
GCLP_HICON = -14                  # SetClassLongPtr 索引：类大图标
GCLP_HICONSM = -34                # 类小图标
ICON_BIG_CSS = 32                 # 大图标设计尺寸（逻辑像素）
ICON_SMALL_CSS = 16

# 进程级的「应用身份」：不设的话任务栏按宿主 exe（pythonw.exe）归类，
# 分组按钮可能显示 Python 的图标、也可能和别的 python 程序混成一组。
# 打包成 exe 后 exe 自己就带 app.ico，这行只是让源码运行也表现一致。
APP_USER_MODEL_ID = "TextLens.1zread"


class _MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

_state = {
    "window": None,
    "gui_started": False,
    "gui_loop_over": threading.Event(),
    "ready": threading.Event(),          # 页面 loaded
    "hwnd": 0,
    "lock": threading.RLock(),
    "log": lambda msg: None,
    "app_dir": "",
    "on_close_request": None,            # fn()：页面 X / Alt+F4 → 转询问流程
    "decorated": False,
    "zoomed": False,                     # 自管最大化（不走 ShowWindow(SW_MAXIMIZE)）
    "saved_rect": None,                  # 最大化前的窗口矩形
    "quitting": False,                   # 真退出时的标志（放行 destroy）
    "shell_ok": False,                   # 外壳补丁（拖动区 + 窗口按钮）是否已生效
    "icon_path": "",                     # app.ico 的位置（没给就自己找）
    "icons": None,                       # (big, small) HICON —— 必须常驻，Windows 不接管
}


# ──────────────────────────── 可用性与模式 ────────────────────────────

def _runtime_ok() -> bool:
    """本机是否装了 WebView2 Runtime（Win10/11 绝大多数预装）。"""
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env)
        if not base:
            continue
        d = os.path.join(base, "Microsoft", "EdgeWebView", "Application")
        try:
            for name in os.listdir(d):
                if name[:1].isdigit():
                    return True
        except OSError:
            pass
    try:
        import winreg
        for root, path in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % _WV2_GUID),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\EdgeUpdate\Clients\%s" % _WV2_GUID),
        ):
            try:
                with winreg.OpenKey(root, path) as key:
                    if winreg.QueryValueEx(key, "pv")[0]:
                        return True
            except OSError:
                continue
    except Exception:
        pass
    return False


def available() -> bool:
    """webview 模式在本机是否可用。"""
    return _webview is not None and _runtime_ok()


def backend(cfg: dict = None) -> str:
    """决定面板载体："webview" 或 "edge"（旧路径兜底）。

    config.json 的 panelBackend 可强制指定：auto / webview / edge。
    """
    want = str(((cfg or {}).get("panelBackend")) or "auto").lower()
    if want == "edge":
        return "edge"
    if want == "webview":
        return "webview" if available() else "edge"
    return "webview" if available() else "edge"


def configure(log=None, app_dir: str = "", icon_path: str = "") -> None:
    """由 lens.py 注入依赖（避免循环 import）。"""
    if log is not None:
        _state["log"] = log
    _state["app_dir"] = app_dir
    if icon_path:
        _state["icon_path"] = icon_path


def set_close_request_handler(fn) -> None:
    _state["on_close_request"] = fn


# ──────────────────────────── 几何 / DPI ────────────────────────────

def _dpi_scale() -> float:
    try:
        dc = user32.GetDC(0)
        if dc:
            dpi = gdi32.GetDeviceCaps(dc, 88)   # LOGPIXELSX
            user32.ReleaseDC(0, dc)
            if dpi:
                return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def _panel_geom():
    """面板的目标位置与尺寸。

    返回值一律是【物理像素】—— MoveWindow / SetWindowRect 都按物理算。
    PANEL_W / PANEL_H 是「CSS 逻辑像素」的设计值：150% 缩放的屏幕上必须
    乘 scale，否则窗口只有 560 物理像素宽，页面只剩 373 CSS px 可用，
    右边整条被裁掉（✕、发送按钮都看不见）。2026-10-14 用户截图实锤的 bug。
    高度按工作区收窄：860 CSS px × 1.5 = 1290 物理，1080p 屏放不下。
    """
    scale = _dpi_scale() or 1.0
    wa = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0):
        wa = wintypes.RECT(0, 0,
                           user32.GetSystemMetrics(0) or 1920,
                           user32.GetSystemMetrics(1) or 1080)
    work_w = max(300, wa.right - wa.left)
    work_h = max(300, wa.bottom - wa.top)

    phys_w = min(int(round(PANEL_W * scale)), work_w - int(round(80 * scale)))
    phys_h = min(int(round(PANEL_H * scale)), work_h - int(round(60 * scale)))
    phys_w = max(300, phys_w)
    phys_h = max(300, phys_h)

    px = wa.right - phys_w - int(round(40 * scale))
    py = wa.top + int(round(70 * scale))
    if py + phys_h > wa.bottom:                     # 放不下就贴着工作区底部
        py = max(wa.top, wa.bottom - phys_h - int(round(20 * scale)))
    return max(wa.left, px), py, phys_w, phys_h


def _find_hwnd(timeout: float = 10.0) -> int:
    """按标题找自己的面板窗口句柄（本进程）。"""
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)
        if buf.value == WINDOW_TITLE:
            found.append(hwnd)
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        found.clear()
        user32.EnumWindows(cb, 0)
        if found:
            return found[0]
        time.sleep(0.2)
    return 0


def _panel_hwnd():
    """当前面板窗口句柄（缓存优先，失效再按标题找）。"""
    hwnd = _state["hwnd"]
    if hwnd and user32.IsWindow(hwnd):
        return hwnd
    return find_window_hwnd(0.6)


def _set_rounded(hwnd: int, rounded: bool = True) -> None:
    """圆角裁剪开关。最大化时必须撤掉：region 是窗口坐标系的，
    窗口变大后旧 region 会像「挖了个洞」一样把内容切掉。"""
    try:
        if not rounded:
            user32.SetWindowRgn(hwnd, 0, True)
            return
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return
        radius = int(round(CORNER_RADIUS * (_dpi_scale() or 1.0)))
        rgn = gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1,
                                       radius * 2, radius * 2)
        user32.SetWindowRgn(hwnd, rgn, True)
        _no_dwm_frame(hwnd)   # 几何一变就顺手兜底（WinForms 可能翻回去）
    except Exception as exc:
        _state["log"]("webview：设置圆角失败：%s" % exc)


def _enable_window_controls(hwnd: int) -> None:
    """补上最小化 / 最大化 / 系统菜单位。

    pywebview 的无边框窗口是 WS_POPUP，没有这三个位：任务栏按钮点不动、
    右键菜单里也没有「最小化 / 关闭窗口」。只补位不补 WS_CAPTION，
    所以不会突然长出一条系统标题栏。
    """
    try:
        style = int(user32.GetWindowLongW(hwnd, GWL_STYLE)) & 0xFFFFFFFF
        want = style | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU
        if want == style:
            return
        signed = want - (1 << 32) if want >= (1 << 31) else want
        user32.SetWindowLongW(hwnd, GWL_STYLE, signed)
        user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
        _state["log"]("webview：窗口样式补位 0x%08X → 0x%08X（最小化/最大化/系统菜单）"
                      % (style, want))
    except Exception as exc:
        _state["log"]("webview：窗口样式补位失败：%s" % exc)


def _no_dwm_frame(hwnd: int) -> None:
    """关掉 DWM 沿窗口【矩形】画的方形细边框和投影。

    根因：pywebview 对 frameless 窗口默认 shadow=True，创建窗口时会
    DwmExtendFrameIntoClientArea(四边 1px) + NCRENDERING_POLICY=ENABLED，
    合成器便沿窗口矩形渲染一圈细边框和投影。SetWindowRgn 的圆角只裁得到
    窗口自己的表面，裁不掉合成器画的这一圈 → 用户看到「圆角外面还有
    一圈方形边框」。对策：扩边收回 0 + 非客户区渲染整个关掉。
    注意 WinForms 改窗口状态时可能把它翻回去，show_panel 等路径会兜底。
    """
    if not hwnd or dwmapi is None:
        return
    try:
        m = _MARGINS()                      # 全 0：不再把边框扩进窗口
        dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
        val = ctypes.c_int(DWMNCRP_DISABLED)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_NCRENDERING_POLICY,
                                     ctypes.byref(val), ctypes.sizeof(val))
        enabled = ctypes.c_int(-1)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_NCRENDERING_ENABLED,
                                     ctypes.byref(enabled),
                                     ctypes.sizeof(enabled))
        was = _state.get("dwm_frame_off")
        if enabled.value != was:
            _state["dwm_frame_off"] = enabled.value
            _state["log"]("webview：DWM 方形边框/投影已关闭"
                          "（NCRENDERING_ENABLED=%d，应为 0）" % enabled.value)
    except Exception as exc:
        _state["log"]("webview：关闭 DWM 边框失败：%s" % exc)


def _icon_path() -> str:
    """app.ico 在哪：优先 lens.py 给的路径，其余按 exe / 源码 / 数据目录兜底。"""
    given = _state.get("icon_path") or ""
    cands = [given] if given else []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        cands.append(os.path.join(base, "app.ico"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.ico"))
    if _state.get("app_dir"):
        cands.append(os.path.join(_state["app_dir"], "app.ico"))
    for path in cands:
        if path and os.path.isfile(path):
            return path
    return ""


def _load_icons():
    """按 DPI 加载大/小两档图标，返回 (big, small)；失败给 (0, 0)。

    句柄缓存在 _state 里常驻：Windows 不接管所有权，句柄一释放
    任务栏就变成空白图标。
    """
    with _state["lock"]:
        cached = _state.get("icons")
        if cached is not None:
            return cached
        path = _icon_path()
        if not path:
            _state["log"]("webview：没找到 app.ico，任务栏图标只能用系统默认")
            _state["icons"] = (0, 0)
            return (0, 0)

        scale = _dpi_scale() or 1.0
        big = small = 0
        try:
            user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                          ctypes.c_uint, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_uint]
            user32.LoadImageW.restype = ctypes.c_void_p
            big = user32.LoadImageW(None, path, IMAGE_ICON,
                                    int(round(ICON_BIG_CSS * scale)),
                                    int(round(ICON_BIG_CSS * scale)),
                                    LR_LOADFROMFILE) or 0
            small = user32.LoadImageW(None, path, IMAGE_ICON,
                                      int(round(ICON_SMALL_CSS * scale)),
                                      int(round(ICON_SMALL_CSS * scale)),
                                      LR_LOADFROMFILE) or 0
        except Exception as exc:
            _state["log"]("webview：图标加载失败：%s" % exc)

        if not big and small:
            big = small
        if not small and big:
            small = big
        _state["icons"] = (big, small)
        if big or small:
            _state["log"]("webview：窗口图标已加载 %s（大=%dpx 小=%dpx）"
                          % (path, int(round(ICON_BIG_CSS * scale)),
                             int(round(ICON_SMALL_CSS * scale))))
        else:
            _state["log"]("webview：图标文件打不开，任务栏图标只能用系统默认：%s"
                          % path)
        return (big, small)


def _apply_icon(hwnd: int) -> None:
    """把 app.ico 装到窗口上（任务栏 / Alt+Tab 都读这里）。

    WinForms 在显示/隐藏或最小化时会自己重置一部分窗口属性，所以和
    样式位一样走幂等兜底：每次 _polish() 都比一下当前值，不对就补。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return
    big, small = _load_icons()
    if not big and not small:
        return
    try:
        user32.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                        ctypes.c_void_p, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                            ctypes.c_void_p]
        user32.SetClassLongPtrW.restype = ctypes.c_void_p

        changed = []
        for which, handle, name in ((ICON_BIG, big, "大"),
                                    (ICON_SMALL, small, "小"),
                                    (ICON_SMALL2, small, "小2")):
            if not handle:
                continue
            cur = user32.SendMessageW(hwnd, WM_GETICON, which, None) or 0
            if cur != handle:
                user32.SendMessageW(hwnd, WM_SETICON, which, handle)
                changed.append(name)
        # 类图标：任务栏在 WM_GETICON 取不到时才回落这里，顺手一起补
        user32.SetClassLongPtrW(hwnd, GCLP_HICON, big)
        if small:
            user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, small)
        if changed:
            # 已经显示在任务栏上的窗口，改完图标要刷一次框架才看得见
            user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                | SWP_FRAMECHANGED)
            _state["log"]("webview：任务栏图标已装上（%s）" % "/".join(changed))
    except Exception as exc:
        _state["log"]("webview：设置窗口图标失败：%s" % exc)


def _region_ok(hwnd: int) -> bool:
    """圆角 region 是否还在（WinForms 改状态时可能把它清掉）。

    圆角矩形 region 必然是 COMPLEXREGION(3)；窗口没有 region 时
    GetWindowRgn 会报 SIMPLEREGION 且矩形=窗口矩形，光比尺寸会误判，
    所以先认类型。
    """
    if not hwnd:
        return True
    try:
        if _state["zoomed"]:
            return True                      # 最大化时本来就故意不裁
        rgn_box = wintypes.RECT()
        rtype = user32.GetWindowRgn(hwnd, ctypes.byref(rgn_box))
        if rtype != 3:                       # 0=失败 1=空 2=简单矩形：region 没了
            return False
        box = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(box)):
            return True
        return (rgn_box.right - rgn_box.left == box.right - box.left
                and rgn_box.bottom - rgn_box.top == box.bottom - box.top)
    except Exception:
        return True


def _polish(hwnd: int) -> None:
    """窗口装饰总入口：样式位 + DWM 边框 + 圆角 + 图标。全部幂等，可反复兜底。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return
    _enable_window_controls(hwnd)
    _no_dwm_frame(hwnd)
    _apply_icon(hwnd)
    if not _region_ok(hwnd):
        _set_rounded(hwnd, not _state["zoomed"])


def _decorate() -> None:
    """按物理像素精确摆放 + 裁圆角。pywebview 的 x/y/width/height
    单位语义在 DPI 缩放下不可靠，直接 Win32 一步到位。"""
    hwnd = _find_hwnd(timeout=6.0)
    if not hwnd:
        _state["log"]("webview：没找到面板窗口句柄，跳过几何校正")
        return
    px, py, pw, ph = _panel_geom()
    user32.MoveWindow(hwnd, px, py, pw, ph, True)
    _state["hwnd"] = hwnd
    _state["zoomed"] = False
    _state["saved_rect"] = None
    _polish(hwnd)
    _state["decorated"] = True
    _state["log"]("webview：面板就位 hwnd=%d %dx%d@(%d,%d) 圆角=%dpx"
                  % (hwnd, pw, ph, px, py, CORNER_RADIUS))


def find_window_hwnd(timeout: float = 2.0) -> int:
    """跨进程安全：按标题找面板窗口（给二次启动拉前台用）。"""
    if _state["hwnd"] and user32.IsWindow(_state["hwnd"]):
        return _state["hwnd"]
    return _find_hwnd(timeout=timeout)


# ──────────────────────────── 窗口创建 ────────────────────────────

def _make_window(url: str):
    px, py, pw, ph = _panel_geom()
    scale = _dpi_scale() or 1.0
    win = _webview.create_window(
        WINDOW_TITLE, url,
        js_api=_PanelApi(),
        x=int(px / scale), y=int(py / scale),
        width=int(pw / scale), height=int(ph / scale),
        frameless=True,
        easy_drag=False,             # 只允许 pywebview-drag-region 拖动，保住文本选择
        shadow=False,                # 关掉 pywebview 默认开的 DWM 投影（画在矩形上，是方的）
        background_color="#ffffff",
    )
    win.events.loaded += _on_loaded
    win.events.closing += _on_closing
    win.events.closed += _on_closed
    return win


def precreate(url: str) -> None:
    """在 webview.start() 之前创建窗口（主线程调用）。"""
    if _webview is None:
        return
    try:
        _state["window"] = _make_window(url)
    except Exception as exc:
        _state["log"]("webview：预创建面板失败：%s" % exc)


def _on_loaded():
    _state["ready"].set()
    _state["loaded_count"] = _state.get("loaded_count", 0) + 1
    _state["log"]("webview：页面加载完成（第 %d 次）" % _state["loaded_count"])
    if not _state["decorated"]:
        try:
            _decorate()
        except Exception as exc:
            _state["log"]("webview：窗口装饰失败：%s" % exc)
    # 外壳补丁必须每次加载都补：线上那份页面可能还是旧版（无拖动区、无窗口按钮），
    # 补丁自己幂等，页面以后带上了就自动跳过。loaded 事件本身跑在独立线程，
    # 这里再起一个线程是为了不等它。
    threading.Thread(target=inject_shell, daemon=True).start()


def _on_closing():
    """用户点 X / Alt+F4 / 任务栏关闭 → 阻止默认销毁，转「后台运行 / 退出」询问。

    注意退出路径：面板自己 stop() 时也是走这个回调，若一律拦下，
    窗口永远销毁不掉、GUI 循环不退出 —— 所以 quitting 时必须放行。
    """
    if _state["quitting"]:
        _state["log"]("webview：退出流程，放行窗口销毁")
        return True
    request_close("标题栏/任务栏")
    return False                       # False = 阻止默认关闭（窗口已隐藏）


def _on_closed():
    _state["log"]("webview：面板窗口已销毁")
    _state["window"] = None
    _state["hwnd"] = 0
    _state["ready"].clear()
    _state["zoomed"] = False
    _state["saved_rect"] = None
    _state["decorated"] = False
    _state["shell_ok"] = False


# ──────────────────────────── 页面 API（js_api） ────────────────────────────

class _PanelApi:
    """暴露给页面：window.pywebview.api.*

    这些方法由 pywebview 在**独立线程**里调用（不是 GUI 线程），
    但内部绝不能反过来调 evaluate_js —— 那会等 GUI 线程，直接死锁。
    """

    def win_action(self, action: str = "min"):
        """页面窗口按钮：min / max / close。"""
        _state["log"]("webview：页面请求窗口动作 %r" % action)
        return perform(action)

    def close_panel(self, action: str = "close"):
        """兼容旧调用：页面 × 按钮走这里，转与 Alt+F4 相同的询问流程。"""
        _state["log"]("webview：页面请求关闭面板（%s）" % action)
        return perform("close")


# ──────────────────────────── GUI 循环 ────────────────────────────

def run_gui(on_started=None) -> None:
    """主线程调用，阻塞到 GUI 退出。"""
    if _webview is None:
        return
    storage = os.path.join(_state["app_dir"] or os.path.expanduser("~"),
                           STORAGE_DIR_NAME)
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:
        pass
    _state["gui_started"] = True
    _state["log"]("webview：进入面板 GUI 循环（storage=%s）" % storage)
    try:
        _webview.start(func=on_started, private_mode=False, storage_path=storage)
    finally:
        _state["gui_started"] = False
        _state["gui_loop_over"].set()
        _state["log"]("webview：GUI 循环结束")


def gui_started() -> bool:
    return _state["gui_started"]


def has_window() -> bool:
    return _state["window"] is not None


def panel_visible() -> bool:
    hwnd = _state["hwnd"] or find_window_hwnd(0.5)
    return bool(hwnd) and bool(user32.IsWindowVisible(hwnd))


def show_panel(focus: bool = True) -> bool:
    hwnd = _state["hwnd"] or find_window_hwnd(1.0)
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, 5)          # SW_SHOW
    _polish(hwnd)                       # WinForms 显示/隐藏时会重置样式，兜底补回
    if focus:
        user32.SetForegroundWindow(hwnd)
    return True


def hide_panel() -> None:
    hwnd = _state["hwnd"] or find_window_hwnd(1.0)
    if hwnd:
        user32.ShowWindow(hwnd, 0)      # SW_HIDE


def bring_to_front() -> bool:
    hwnd = _state["hwnd"] or find_window_hwnd(1.0)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
    else:
        user32.ShowWindow(hwnd, 5)      # SW_SHOW
    _polish(hwnd)
    user32.SetForegroundWindow(hwnd)
    return True


# ──────────────────────────── 窗口动作（最小化 / 最大化 / 关闭） ────────────────────────────

def minimize_panel() -> bool:
    """最小化（任务栏里还在，点一下能回来）。"""
    hwnd = _panel_hwnd()
    if not hwnd:
        return False
    user32.ShowWindow(hwnd, SW_MINIMIZE)
    _state["log"]("webview：面板已最小化")
    return True


def toggle_maximize() -> bool:
    """最大化 / 还原。

    不用 ShowWindow(SW_MAXIMIZE)：WinForms 的最大化会自己重排尺寸，
    和我们的 SetWindowRgn 圆角打架。这里自己按工作区（排除任务栏）
    摆一次，还原时回填原矩形，全程可控。
    """
    hwnd = _panel_hwnd()
    if not hwnd:
        return False
    if _state["zoomed"]:
        rect = _state["saved_rect"]
        _set_rounded(hwnd, True)
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if rect:
            user32.SetWindowPos(hwnd, HWND_TOP, rect[0], rect[1], rect[2], rect[3],
                                SWP_NOZORDER | SWP_SHOWWINDOW)
        _state["zoomed"] = False
        _state["log"]("webview：面板已还原 %s" % (rect,))
        return True

    cur = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(cur)):
        return False
    wa = wintypes.RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0):
        return False
    _state["saved_rect"] = (cur.left, cur.top,
                            cur.right - cur.left, cur.bottom - cur.top)
    _set_rounded(hwnd, False)
    user32.SetWindowPos(hwnd, HWND_TOP, wa.left, wa.top,
                        wa.right - wa.left, wa.bottom - wa.top,
                        SWP_NOZORDER | SWP_SHOWWINDOW)
    _state["zoomed"] = True
    _state["log"]("webview：面板已最大化 %dx%d@(%d,%d)"
                  % (wa.right - wa.left, wa.bottom - wa.top, wa.left, wa.top))
    return True


def request_close(reason: str = "") -> bool:
    """关闭面板窗口：隐藏 + 转「后台运行 / 退出」询问（与 Alt+F4 同一条路）。"""
    hwnd = _panel_hwnd()
    if not hwnd:
        return False
    if _state["zoomed"]:
        rect = _state["saved_rect"]
        _state["zoomed"] = False
        if rect:
            user32.SetWindowPos(hwnd, HWND_TOP, rect[0], rect[1], rect[2], rect[3],
                                SWP_NOZORDER)
            _set_rounded(hwnd, True)
    user32.ShowWindow(hwnd, SW_HIDE)
    _state["log"]("webview：面板关闭请求（%s），窗口已隐藏，转询问流程" % (reason or "未知"))
    cb = _state["on_close_request"]
    if cb:
        threading.Thread(target=cb, daemon=True).start()
    return True


def perform(action: str) -> str:
    """窗口动作总入口：min / max / close。给页面按钮和外部调用共用。"""
    action = (action or "").strip().lower()
    if action in ("min", "minimize"):
        return "ok" if minimize_panel() else "no-window"
    if action in ("max", "maximize", "full", "fullscreen"):
        return "ok" if toggle_maximize() else "no-window"
    if action in ("close", "hide", "quit"):
        return "ok" if request_close("页面按钮") else "no-window"
    _state["log"]("webview：未知窗口动作 %r" % action)
    return "unknown-action"


def open_dynamic_window(url: str) -> bool:
    """GUI 已在跑但没有窗口时动态创建（未接入 → 接入成功的路径）。"""
    with _state["lock"]:
        if _state["window"] is not None:
            return True
        if not _state["gui_started"]:
            return False
        try:
            _state["shell_ok"] = False
            _state["window"] = _make_window(url)
            _state["ready"].clear()
            _state["log"]("webview：动态创建面板窗口")
            threading.Thread(target=inject_shell, daemon=True).start()
            return True
        except Exception as exc:
            _state["log"]("webview：动态创建面板失败：%s" % exc)
            return False


# ──────────────────────────── 外壳补丁（拖动区 + 窗口按钮） ────────────────────────────

# 为什么要在桌面端注入，而不是只改 web/？
# 面板页来自**线上域名**（云端按域名精确匹配钥匙，不能换成本地文件），
# 线上那份是什么版本不由本机决定。旧版页面既没有 .pywebview-drag-region，
# 也没有窗口按钮（✕ 只在 iframe 场景才显示）→ 窗口拖不动、关不掉。
# 这段补丁让「桌面端」自己补齐窗口外壳，页面从此怎么改都不影响可用性。
#
# 与页面自带的控件怎么共存：两边靠同一套约定去重，绝不能各绑一次
# （否则一次点击会触发两遍：双击顶栏 = 最大化又立刻还原，看着像没反应）。
#   · 页面自带控件时会把 window.__lzrShellNative 置 true → 补丁不再绑事件；
#   · 补丁只创建自己造的那一份（#lzr-winctl 不存在时才建）；
#   · 两边的点击回调都用 e.__lzrHandled 占位，谁先到谁执行，双保险。
SHELL_JS = """
(function () {
  var SID = 'lzr-winctl';
  function api() { return (window.pywebview && window.pywebview.api) || null; }
  function fire(ev, act) {
    if (ev) {
      if (ev.__lzrHandled) { return true; }
      ev.__lzrHandled = true;
    }
    var p = api();
    if (p && p.win_action) { p.win_action(act); return true; }
    return false;
  }
  function paint() {
    if (document.getElementById(SID + '-css')) { return; }
    var s = document.createElement('style');
    s.id = SID + '-css';
    s.textContent = [
      '#lzr-winctl{display:flex;align-items:center;gap:2px;flex:0 0 auto;'
        + 'margin-left:2px;padding-left:6px;border-left:1px solid var(--border,#e6e8ec)}',
      '#lzr-winctl button{width:26px;height:24px;display:inline-flex;align-items:center;'
        + 'justify-content:center;padding:0;margin:0;font-family:inherit;font-size:12px;'
        + 'line-height:1;color:var(--text-dim,#5c6470);background:transparent;'
        + 'border:1px solid transparent;border-radius:6px;cursor:pointer;'
        + '-webkit-user-select:none;user-select:none}',
      '#lzr-winctl button:hover{background:var(--bg-soft,#f6f7f9);'
        + 'border-color:var(--border,#e6e8ec);color:var(--text,#1b1f26)}',
      '#lzr-winctl button[data-a="close"]:hover{background:#fdecec;'
        + 'border-color:#f6d4d4;color:#d64545}'
    ].join('');
    (document.head || document.documentElement).appendChild(s);
  }
  function noDrag(el) {
    if (!el || el.__lzrNoDrag) { return; }
    el.__lzrNoDrag = true;
    el.addEventListener('mousedown', function (e) { e.stopPropagation(); }, true);
    el.addEventListener('dblclick', function (e) { e.stopPropagation(); }, true);
  }
  function isUi(el, stopAt) {
    while (el && el !== stopAt) {
      var t = el.tagName;
      if (t === 'BUTTON' || t === 'SELECT' || t === 'INPUT' || t === 'A' || t === 'TEXTAREA') {
        return true;
      }
      el = el.parentNode;
    }
    return false;
  }
  function bindBox(box, mine) {
    noDrag(box);
    if (!mine && window.__lzrShellNative) { return; }   // 页面自己绑过了
    if (box.__lzrBound) { return; }
    box.__lzrBound = true;
    box.addEventListener('click', function (e) {
      var b = e.target && e.target.closest ? e.target.closest('button') : null;
      if (b) { fire(e, b.getAttribute('data-a')); }
    }, true);
  }
  function apply() {
    var tb = document.querySelector('header.topbar') || document.querySelector('.topbar');
    if (tb && !tb.classList.contains('pywebview-drag-region')) {
      tb.classList.add('pywebview-drag-region');       // 顶栏 = 拖动区
    }
    if (tb) {
      Array.prototype.forEach.call(tb.querySelectorAll('button,select,a,input,textarea'),
                                  noDrag);             // 顶栏里的控件不跟着拖窗口
      if (!tb.__lzrDbl) {
        tb.__lzrDbl = true;
        tb.addEventListener('dblclick', function (e) {
          if (window.__lzrShellNative || isUi(e.target, tb)) { return; }
          fire(e, 'max');                              // 双击顶栏 = 最大化/还原
        });
      }
    }
    var box = document.getElementById(SID);
    if (box) {                                          // 页面自带的（新版 web/）
      box.classList.remove('hidden');
      bindBox(box, false);
      return !!(tb && tb.classList.contains('pywebview-drag-region'));
    }
    if (!tb) { return false; }
    box = document.createElement('div');
    box.id = SID;
    box.className = 'lzr-winctl';
    box.innerHTML =
      '<button type="button" data-a="min" title="最小化">─</button>'
      + '<button type="button" data-a="max" title="最大化 / 还原">▢</button>'
      + '<button type="button" data-a="close" title="关闭">✕</button>';
    (document.querySelector('.topbar-actions') || tb).appendChild(box);
    bindBox(box, true);
    paint();
    return true;
  }
  if (window.__lzrShellTimer) { clearInterval(window.__lzrShellTimer); }
  var tries = 0;
  window.__lzrShellTimer = setInterval(function () {
    tries += 1;
    if (apply() || tries > 20) { clearInterval(window.__lzrShellTimer); }
  }, 300);
  window.addEventListener('pywebviewready', apply);
  if (document.readyState !== 'loading') { apply(); }
  else { document.addEventListener('DOMContentLoaded', apply); }
  return apply() ? 'ok' : 'pending';
})()
"""


def inject_shell(attempts: int = 10, interval: float = 0.6) -> bool:
    """把窗口外壳（拖动区 + 最小化/最大化/关闭）注入面板页面。

    幂等：重复调用无副作用。返回是否确认生效。
    """
    win = _state["window"]
    if win is None:
        return False
    for i in range(max(1, attempts)):
        raw = None
        try:
            raw = win.evaluate_js(SHELL_JS)
        except Exception as exc:
            _state["log"]("webview：外壳注入异常（第 %d 次）：%s" % (i + 1, exc))
        if raw == "ok" or raw is True:
            if not _state["shell_ok"]:
                _state["log"]("webview：外壳补丁生效（顶栏可拖动 + 最小化/最大化/关闭）")
            _state["shell_ok"] = True
            return True
        time.sleep(interval)
    _state["log"]("webview：外壳补丁注入未确认（页面可能没顶栏，返回值=%r）" % (raw,))
    return False


def stop() -> None:
    """退出 GUI（销毁窗口，webview.start 随即返回）。"""
    win = _state["window"]
    if win is not None:
        _state["quitting"] = True      # 放行 closing 回调，否则销毁会被自己拦下
        try:
            win.destroy()
            return
        except Exception as exc:
            _state["log"]("webview：销毁面板窗口失败：%s" % exc)
    # 没有窗口对象时直接结束循环
    try:
        _webview.destroy()
    except Exception:
        pass


# ──────────────────────────── 钥匙注入与推送 ────────────────────────────

def _wait_ready(timeout: float) -> bool:
    if _state["ready"].is_set():
        return True
    _state["log"]("webview：等待面板页面加载…")
    return _state["ready"].wait(timeout)


def push(payload: dict, secret: dict = None,
         retries: int = 3, timeout: float = 8.0) -> str:
    """进程内推送：先注入钥匙再推选区（等价旧 cdp_push.mjs 的两步）。

    返回值与旧通道一致：ok / unlocked / no-secret / not-ready / error:*。
    """
    win = _state["window"]
    if win is None:
        return "no-window"
    if not _wait_ready(timeout):
        return "not-ready"

    # secret 缺失时绝不能把空对象喂给 __lzrUnlock（会得到 bad-secret）
    if secret and secret.get("publishableKey"):
        unlock_part = (
            "if(window.__lzrUnlock){try{r.unlock=window.__lzrUnlock(%s);}"
            "catch(e){r.unlock='error:'+e;}}" % json.dumps(secret)
        )
    else:
        unlock_part = "r.unlock='skip';"
    expr = (
        "(function(){try{var r={};"
        "%s"
        "if(window.__textLensDesktopPush){"
        "try{r.push=!!window.__textLensDesktopPush(%s);}"
        "catch(e){r.error=String(e);}}"
        "return JSON.stringify(r);}catch(e){return JSON.stringify({error:String(e)});}})()"
    ) % (unlock_part, json.dumps(payload or {}))

    last = "not-ready"
    for _ in range(max(1, retries)):
        try:
            raw = win.evaluate_js(expr)
        except Exception as exc:
            last = "error:%s" % exc
            _state["log"]("webview：evaluate_js 异常 %s" % exc)
            time.sleep(0.4)
            continue
        _state["log"]("webview：evaluate_js 返回 %r" % (raw,))
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        unlock = data.get("unlock")
        pushed = data.get("push") is True
        if data.get("error"):
            last = "error:%s" % data["error"]
        elif pushed:
            return "ok"
        elif not (payload or {}).get("text"):
            if unlock in ("ok", "already"):
                return "unlocked"
            last = "no-secret" if unlock in (None, "skip") else str(unlock)
        else:
            last = str(unlock or "not-ready")
        time.sleep(0.4)
    return last


def unlock(secret: dict) -> str:
    """只注入钥匙（打开面板但还没有选区时）。"""
    return push({}, secret=secret)
