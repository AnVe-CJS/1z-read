#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1z read（原划词解析助手）· Windows 桌面助手

在任意软件里选中文字，稍等片刻弹出气泡，即可把它送到解析面板提问。

设计要点
────────
* 完全基于 Python 标准库（ctypes 直接调用 Win32），不需要装任何第三方包。
* 选区抓取走剪贴板：需要时模拟一次 Ctrl+C，读完文本后把原剪贴板内容还回去。
* 触发方式只有一种：划选后悬停弹气泡（不用快捷键，避免与用户的日常按键冲突）。
  键盘钩子仍然保留，但只用于"用户开始打字时把气泡收起来"。
* 解析面板是一个 Edge / Chrome 的「应用窗口」（--app），因为它必须由
  发布域名的页面发起模型调用（平台要求请求来源与发布域名一致）。
* 本机在 127.0.0.1 上开一个小桥接服务，面板页面轮询它拿到最新选区，
  这样同一个面板窗口可以一直开着，选区不断推入，不用反复开窗。
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import json
import locale
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ──────────────────── 编码基线：一律 UTF-8 ────────────────────
#
# 中文 Windows 上 Python 默认按 cp936(GBK) 处理控制台与子进程管道，而本工具
# 内部数据全是 UTF-8。历史上就是因为某个通道退回本地编码，导致划词选中的
# 中文在面板里变成 "���㿪ʼѧϰ" 这类乱码（详见 SUBPROC_ENCODING 处的说明）。
#
# 这里把三条标准流钉死成 UTF-8；子进程通道另有显式 encoding 参数。
# 全部用 try 兜住：pythonw.exe 下没有控制台，标准流可能是 None。
for _stream_name in ("stdin", "stdout", "stderr"):
    try:
        _s = getattr(sys, _stream_name, None)
        if _s is not None:
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream_name

# 面板宿主：WebView2 进程内独立窗口（不可用时自动退回 Edge 老路径）
import panelwin  # noqa: E402  与本文件同目录（WebView2 面板宿主）

# ──────────────────────────── 常量 ────────────────────────────

APP_NAME = "1z read"
APP_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TextLens"
)
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# 云服务钥匙只存在本机这个文件里：不进源码目录、不随网页下发。
# 网页里没有钥匙就是个空壳，拿到网址的人既发不出模型调用，也抄不走钥匙。
CLOUD_SECRET_PATH = os.path.join(APP_DIR, "cloud.json")

# 面板根地址。默认指向作者发布的那一份；「自带钥匙」的用户（自己在 WorkBuddy 里
# 发布了一份）把自己那份的域名填进 cloud.json / config.json 的 panelBase 即可。
# 别的地方一律走 panel_url() / panel_base()，不要再直接引用常量。
DEFAULT_PANEL_BASE = "https://text-select-ai.app.workbuddy.host/"
PANEL_QUERY = "desktop=1&v=3"
PANEL_WIDTH = 460
PANEL_HEIGHT = 720

BRIDGE_PORT_FIRST = 17890
BRIDGE_PORT_LAST = 17899

CDP_PORT_DEFAULT = 19222
PUSH_TEXT_URL_LIMIT = 3000

# 子进程管道的编码，一律写死 UTF-8，不看系统 locale。
#
# 中文 Windows 上 Python 默认用 cp936(GBK) 编解码子进程管道，而工具内部
# 所有数据（JSON、日志、面板文案）都是 UTF-8。一旦某处退回本地编码就整段
# 乱码 —— 2026-10-13 踩过：助手把选区 JSON 用 cp936 写进 Node 的 stdin，
# cdp_push.mjs 按 utf8 解析，选中的中文全部变成 "���㿪ʼѧϰ"。
# 显式指定后，无论 PYTHONUTF8 有没有开、控制台是什么代码页，结果都一样。
SUBPROC_ENCODING = "utf-8"

# ──────────────────── 独立 exe（PyInstaller）形态 ────────────────────
#
# 打包成 exe 后，入口脚本与内嵌资源会被解到临时目录 sys._MEIPASS，
# __file__ 不再指向源码目录。凡是"和程序放在一起的文件"（app.ico、cdp_push.mjs）
# 都必须经 resource_dir() 定位，否则源码跑得好好的、exe 里就找不到文件。
IS_FROZEN = bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    """程序自带资源（app.ico / cdp_push.mjs）所在目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return base
    return os.path.dirname(os.path.abspath(__file__))


DEFAULT_HOTKEY = "alt+q"    # 全局热键：默认关闭，见 config.json 的 hotkeyEnabled

HOVER_DELAY_MS = 420        # 划选后多久出气泡
# 松手后"鼠标一动就取消"是旧的错误设计：容差再宽也挡不住人松手时手会自然滑动
# （触控板还有惯性），实测用户每次真实划选都被自己取消了。现在只在
# "又开始新的点击 / 敲了键盘"时才取消，见 _cancel_hover。
BUBBLE_HIT_MARGIN = 20      # 点击落在气泡外这么多像素，就顺手把气泡收起来
DRAG_MIN_PX = 8            # 小于这个移动距离不算划选

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"
    ),
]

# ──────────────────────────── 基础工具 ────────────────────────────


LOG_PATH = os.path.join(APP_DIR, "helper.log")
_log_lock = threading.Lock()


def enable_dpi_awareness() -> None:
    """让本进程坐标与屏幕物理像素一致。

    不声明的话，Windows 会把进程里的坐标按缩放比（如 150%）虚拟化：
    钩子报的是物理坐标，Tk 窗口用的是虚拟坐标，气泡就会偏離选区一大截。
    必须在创建任何窗口之前调用。
    """
    try:
        # 首选：Per-Monitor V2（Win10 1703+）
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
            return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()  # 老系统兜底（Vista+）
    except Exception:
        pass


def set_app_user_model_id() -> None:
    """给进程一个明确的应用身份，让任务栏正确归类与显示图标。

    不设的话，任务栏按宿主可执行文件归类 —— 源码运行时宿主是
    pythonw.exe，于是任务栏那组按钮可能顶着 Python 的图标，也会和别的
    python 程序挤成一组。必须早于任何窗口创建（也是 create_window 之前），
    打包成 exe 后 exe 自身带 app.ico，这行只是保证两种形态表现一致。
    """
    try:
        ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID(
            panelwin.APP_USER_MODEL_ID)
    except Exception as exc:
        log("设置应用身份失败（不影响使用）：%s" % exc)


def log(msg: str) -> None:
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    # pythonw 启动时没有控制台，必须同时写文件，否则日志全丢没法排查
    try:
        with _log_lock:
            try:
                if os.path.getsize(LOG_PATH) > 2 * 1024 * 1024:
                    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as fh:
                        tail = fh.readlines()[-400:]
                    with open(LOG_PATH, "w", encoding="utf-8") as fh:
                        fh.writelines(tail)
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def load_config() -> dict:
    cfg = {
        "hotkey": DEFAULT_HOTKEY,
        # 全局热键默认关闭：Alt+Q 是全局注册，会顶掉别的软件的 Alt+Q，
        # 而普通使用场景已被划选气泡覆盖。需要时把这里改成 true
        "hotkeyEnabled": False,
        "port": None,
        # 划选判定：True=只在"从文字上按下"的拖动才出气泡（挡掉拖窗口/滚动条误触）；
        # 如果某个软件用了非标准文字光标导致气泡不弹，把它改成 false 即可恢复旧行为
        "dragTextOnly": True,
        # 关面板时是否弹询问框；closeAction 为"下次不再询问"记住的选择
        "closeAsk": True,
        "closeAction": "background",
        # ── 隐私 / 安全 ──
        # 受保护程序：密码管理器、网银、系统凭据界面里不取词（内置名单见
        # DEFAULT_PROTECTED_APPS / DEFAULT_PROTECTED_TITLES，删不掉，只能关总开关）
        "protectApps": True,
        "excludedApps": [],        # 追加保护的进程名关键词（小写子串），如 "windowsterminal"
        "excludedTitles": [],      # 追加保护的窗口标题关键词（小写子串）
        # 划词取词后把剪贴板恢复原状，不在剪贴板里留下划词内容
        "restoreClipboard": True,
        # 日志里是否记录窗口标题（标题常带文件名/邮件主题，介意就设 false）
        "logTitles": True,
        # 本地桥接 HTTP 服务（历史遗留，前端已改为调试通道直推，默认停用：
        # 开着会让任意网页都能从 127.0.0.1 读走你上一次选中的内容）
        "bridgeHttp": False,
    }
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log("写入配置失败：%s" % exc)


def cleanup_stale_pushes() -> None:
    """清掉历史版本可能残留的推送临时文件。

    旧实现把 payload（含钥匙 + 选区原文）写成 JSON 再交给 Node；进程被强杀时
    finally 来不及跑，文件就留在磁盘上。现在改成走 stdin 不再落盘，这里只是
    兜底清一次老文件。
    """
    for pat in ("push_*.json", "last_push.json"):
        for path in glob.glob(os.path.join(APP_DIR, pat)):
            try:
                os.remove(path)
            except OSError:
                pass


def _read_cloud_file() -> dict:
    """读 cloud.json 原文；读不到 / 不是对象就当空。"""
    try:
        with open(CLOUD_SECRET_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_cloud_secret() -> dict:
    """读取本机的云服务钥匙（%LOCALAPPDATA%/TextLens/cloud.json）。

    内容形如 {"publishableKey": "wbpk_...", "resourceId": "wbcs_..."}。
    文件不存在就返回空 dict——此时面板会显示"请从助手打开"，不会静默降级。
    """
    data = _read_cloud_file()
    if data.get("publishableKey"):
        return {
            "publishableKey": str(data["publishableKey"]),
            "resourceId": str(data.get("resourceId") or ""),
        }
    return {}


def panel_base() -> str:
    """解析面板根地址（带结尾斜杠）。

    优先级：cloud.json 的 panelBase > config.json 的 panelBase > 内置默认。
    自带钥匙的用户把自己发布的那份域名写进 panelBase 就行。
    """
    base = str(_read_cloud_file().get("panelBase") or "").strip()
    if not base:
        base = str(load_config().get("panelBase") or "").strip()
    if not base:
        base = DEFAULT_PANEL_BASE
    if "://" not in base:
        base = "https://" + base
    return base.rstrip("/") + "/"


def panel_url() -> str:
    return panel_base() + "?" + PANEL_QUERY


def panel_host() -> str:
    """面板域名。用来在调试端口的目标列表里认出面板页，别依赖写死的字符串。"""
    return urllib.parse.urlparse(panel_base()).hostname or ""


# ──────────────────── 接入门（用自己的 WorkBuddy） ────────────────────

# WorkBuddy 登录页（登录页内含注册入口）。带上 redirect_uri，登录后直接回到控制台。
WORKBUDDY_SIGNUP_URL = (
    "https://www.workbuddy.cn/login/"
    "?platform=agents&state=0&redirect_uri=https%3A%2F%2Fwww.workbuddy.cn%2Fapp"
)

PUBLISH_PROMPT = (
    "请用 WorkBuddy 打开本项目所在文件夹，帮我开通云服务并发布这个项目，"
    "然后告诉我 publishableKey、resourceId 和发布后的网址。"
)


def is_provisioned() -> bool:
    """本机是否已完成接入（有可用的云服务钥匙）。

    没有钥匙时工具整体不可用：模型通道属于「应用」，本工具不自带公共额度，
    所以每个人必须用自己那份应用。这就是"别人用不了我的额度"的落点。
    """
    return bool(load_cloud_secret())


def gate_prefill() -> dict:
    """向导预填值：已接入的用它当前生效的配置，没接入的留空让使用者自己填。"""
    data = _read_cloud_file()
    panel = str(data.get("panelBase") or "").strip()
    if not panel and data.get("publishableKey"):
        panel = panel_base()   # 已接入但没显式记 panelBase：当前生效的那份就是他的
    return {
        "panelBase": panel,
        "publishableKey": str(data.get("publishableKey") or ""),
        "resourceId": str(data.get("resourceId") or ""),
    }


def save_provision(panel: str, key: str, resource: str) -> tuple:
    """把使用者自己那份应用的接入信息写进 cloud.json。返回 (是否成功, 说明)。

    面板网址必填：服务端按调用来源域名**精确匹配**，用别人的域名配自己的钥匙
    必然报 auth_ 失败 —— 所以"自己发布一份"不是可选项，是唯一正确用法。
    """
    panel = (panel or "").strip()
    key = (key or "").strip()
    resource = (resource or "").strip()

    if not panel:
        return False, "请填你自己发布后的面板网址（WorkBuddy 发布时会给你）。"
    if "://" not in panel:
        panel = "https://" + panel
    if not panel.startswith("https://") or "." not in panel:
        return False, "面板网址看起来不对，应形如 https://xxx.app.workbuddy.host/"
    if not _looks_like_key(key):
        return False, "publishableKey 应以 wbpk_ 开头，请检查是否复制完整。"

    data = _read_cloud_file()
    data["publishableKey"] = key
    data["panelBase"] = panel
    if resource:
        data["resourceId"] = resource
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(CLOUD_SECRET_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        return False, "写入失败：%s" % exc
    return True, CLOUD_SECRET_PATH


# ──────────────────────────── Win32 绑定 ────────────────────────────

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
HGLOBAL = wintypes.HANDLE
HWND = wintypes.HWND

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_C = 0x43

WM_HOTKEY = 0x0312
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WH_KEYBOARD_LL = 13

# 自定义消息：让主线程（消息循环所在线程）注销 / 重注册热键。
# 热键归属注册它的线程，从 worker 线程调 UnregisterHotKey 会失败。
WM_APP_HOTKEY_OFF = 0x0400 + 1
WM_APP_HOTKEY_ON = 0x0400 + 2

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSEMOVE = 0x0200
WM_RBUTTONDOWN = 0x0204
WM_MBUTTONDOWN = 0x0207

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

ERROR_ALREADY_EXISTS = 183

user32.OpenClipboard.argtypes = [HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.EmptyClipboard.argtypes = []
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalFree.argtypes = [HGLOBAL]

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
kernel32.CreateMutexW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

user32.GetForegroundWindow.restype = HWND
user32.GetWindowTextLengthW.argtypes = [HWND]
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]

user32.RegisterHotKey.argtypes = [HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [HWND, ctypes.c_int]

user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wintypes.WPARAM, ctypes.c_void_p]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]

user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(KBDLLHOOKSTRUCT))


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


MOUSEHOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, ctypes.POINTER(MSLLHOOKSTRUCT))

user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL


# ── 光标形状判定：把"拖窗口 / 拖滚动条 / 拖图标"和"划选文字"区分开 ──
#
# 实测（150% 缩放，Chromium 面板）：
#   在文字上按下        → IBEAM（I 型）
#   拖动过程中          → 应用自定义光标
#   松手瞬间            → ARROW（箭头！）
#   在标题栏/桌面按下    → ARROW
# 所以"松手那一刻"的光标没有参考价值，必须在**按下那一刻**采样。
IDC_IBEAM = 32513
CURSOR_SHOWING = 0x00000001


class CURSORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", ctypes.c_void_p),
        ("ptScreenPos", POINT),
    ]


user32.GetCursorInfo.argtypes = [ctypes.POINTER(CURSORINFO)]
user32.GetCursorInfo.restype = wintypes.BOOL
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = ctypes.c_void_p

_ibeam_cache = None


def ibeam_handle():
    """系统标准 I 型光标句柄。程序用 LoadCursor(NULL, IDC_IBEAM) 拿到的都是它。"""
    global _ibeam_cache
    if _ibeam_cache is None:
        try:
            _ibeam_cache = (
                user32.LoadCursorW(
                    None, ctypes.cast(ctypes.c_void_p(IDC_IBEAM), wintypes.LPCWSTR)
                )
                or 0
            )
        except Exception:
            _ibeam_cache = 0
    return _ibeam_cache


def cursor_is_text() -> bool:
    """当前光标是不是文字光标（I 型）。任何异常都当"不是"，绝不误触。"""
    ibeam = ibeam_handle()
    if not ibeam:
        return False
    try:
        ci = CURSORINFO()
        ci.cbSize = ctypes.sizeof(CURSORINFO)
        if not user32.GetCursorInfo(ctypes.byref(ci)):
            return False
        return bool(ci.flags & CURSOR_SHOWING) and ci.hCursor == ibeam
    except Exception:
        return False


GA_ROOT = 2

user32.WindowFromPoint.argtypes = [POINT]
user32.WindowFromPoint.restype = HWND
user32.GetAncestor.argtypes = [HWND, wintypes.UINT]
user32.GetAncestor.restype = HWND


def window_root_at(x: int, y: int) -> int:
    """屏幕坐标处的窗口（取顶层根窗口，忽略 Chrome 的子窗口包装）。"""
    try:
        hwnd = user32.WindowFromPoint(POINT(x, y))
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return int(root or hwnd or 0)
    except Exception:
        return 0


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


# ──────────────────────── 受保护程序（敏感应用） ────────────────────────
#
# 借鉴同类工具的通用安全做法：密码管理器、网银、系统凭据界面里绝不取词。
# 命中后：不弹气泡、不发 Ctrl+C、不唤起面板、日志里连标题都不记。
#
# 判定优先看「进程映像名」而不是窗口标题——标题网页可以随便改，进程名改不了。
# 标题只做兜底补充（覆盖"浏览器里打开的网银页面"这类同进程场景）。

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROC_NAME_BUF = 1024

user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

# 内置保护名单：进程名子串（小写）。用户可在 config.json 的 excludedApps 里
# 继续追加，但删不掉这份内置名单——避免误改把保护整个关掉。
DEFAULT_PROTECTED_APPS = (
    # 密码 / 凭据管理器
    "keepass", "1password", "bitwarden", "lastpass", "dashlane", "enpass",
    "roboform", "nordpass", "keeper", "strongbox", "safeincloud", "passwordwallet",
    # 网银 / 支付 / 券商客户端
    "alipay", "unionpay", "paypal", "icbc", "ccb", "cmbchina", "bank",
    "ebank", "bocnet", "bocclient", "gtja", "htzq", "zszq",
    # 系统凭据界面 / 安全中心
    "credentialuibroker", "securityhealth", "logonui", "lsass", "consent",
)

# 标题兜底关键词（小写）：只放"一旦出现就几乎不可能是正常阅读场景"的词，
# 避免把《密码学导论》这类正经内容误判成敏感窗口。
DEFAULT_PROTECTED_TITLES = (
    "密码管理器", "保存的密码", "密码与自动填充", "密码保险库", "凭据管理器",
    "password manager", "saved password", "credential manager", "keychain access",
    "网上银行", "个人网银", "企业网银", "网银", "手机银行", "个人银行",
)

_PROC_NAME_CACHE = {}


def process_name_of(hwnd: int) -> str:
    """窗口所属进程的映像名（小写、去 .exe）。查不到返回空串。

    按 PID 缓存：钩子回调里每按一次鼠标都要用，不能每次都走 OpenProcess。
    """
    if not hwnd:
        return ""
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_v = int(pid.value)
    except Exception:
        return ""
    if not pid_v:
        return ""
    if pid_v in _PROC_NAME_CACHE:
        return _PROC_NAME_CACHE[pid_v]

    name = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_v)
    if handle:
        try:
            buf = ctypes.create_unicode_buffer(PROC_NAME_BUF)
            size = wintypes.DWORD(PROC_NAME_BUF)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value or "")
                if name.lower().endswith(".exe"):
                    name = name[:-4]
                name = name.lower()
        except Exception:
            name = ""
        finally:
            kernel32.CloseHandle(handle)

    if len(_PROC_NAME_CACHE) > 128:      # 简单容量控制，够用
        _PROC_NAME_CACHE.clear()
    _PROC_NAME_CACHE[pid_v] = name
    return name


def _window_title_lower(hwnd: int) -> str:
    try:
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return (buf.value or "").lower()
    except Exception:
        return ""


def protected_hit(hwnd: int, cfg: dict) -> str:
    """窗口是否属于受保护（敏感）程序。命中返回原因（用于日志），否则返回空串。"""
    if not hwnd or not cfg.get("protectApps", True):
        return ""

    keywords = list(DEFAULT_PROTECTED_APPS)
    keywords += [str(a).lower() for a in (cfg.get("excludedApps") or []) if a]
    name = process_name_of(hwnd)
    if name:
        for kw in keywords:
            if kw in name:
                return "进程名命中「%s」" % kw

    title_keys = list(DEFAULT_PROTECTED_TITLES)
    title_keys += [str(t).lower() for t in (cfg.get("excludedTitles") or []) if t]
    title = _window_title_lower(hwnd)
    if title:
        for kw in title_keys:
            if kw in title:
                return "窗口标题命中「%s」" % kw
    return ""


def short_title(title: str, cfg: dict = None) -> str:
    """写进日志的窗口标题：默认截断到 16 字，logTitles=false 时完全不记。

    窗口标题里经常带着文件名、邮件主题、聊天对象名，按原文落盘属于隐私泄露。
    """
    if cfg is not None and not cfg.get("logTitles", True):
        return "（标题已省略）"
    t = " ".join((title or "").split())
    if not t:
        return "未知窗口"
    return t[:16] + ("…" if len(t) > 16 else "")


# ──────────────────────────── 剪贴板 ────────────────────────────


def read_clipboard_text() -> str:
    for _ in range(6):
        if not user32.OpenClipboard(None):
            time.sleep(0.03)
            continue
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        except Exception:
            return ""
        finally:
            user32.CloseClipboard()
    return ""


def write_clipboard_text(text: str) -> bool:
    for _ in range(6):
        if not user32.OpenClipboard(None):
            time.sleep(0.03)
            continue
        try:
            user32.EmptyClipboard()
            if not text:
                return True
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                kernel32.GlobalFree(handle)
                return False
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(CF_UNICODETEXT, handle):
                kernel32.GlobalFree(handle)
                return False
            return True
        except Exception:
            return False
        finally:
            user32.CloseClipboard()
    return False


def clipboard_seq() -> int:
    try:
        return int(user32.GetClipboardSequenceNumber())
    except Exception:
        return 0


def clipboard_format_count() -> int:
    """剪贴板里有几种格式（0 = 原本就是空的）。

    用来判断"划词前剪贴板是不是空的"：空的话取词完要清干净，
    否则用户随手 Ctrl+V 会粘出刚才划的那段字。
    """
    try:
        return int(user32.CountClipboardFormats())
    except Exception:
        return 0


# ──────────────────────────── 键鼠模拟 ────────────────────────────


def _send_vk(vk: int, up: bool = False) -> bool:
    item = INPUT()
    item.type = INPUT_KEYBOARD
    item.ki.wVk = vk
    item.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    sent = user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
    if sent != 1:
        log("警告：按键注入被系统拒绝（vk=0x%02x, sent=%d）" % (vk, sent))
    return sent == 1


def release_modifiers() -> None:
    """热键触发时修饰键可能仍被按住，先全部松开，否则 Ctrl+C 会变成别的组合。"""
    for vk in (VK_MENU, VK_CONTROL, VK_SHIFT):
        if user32.GetAsyncKeyState(vk) & 0x8000:
            _send_vk(vk, up=True)
            time.sleep(0.01)


def send_ctrl_c() -> None:
    _send_vk(VK_CONTROL)
    time.sleep(0.015)
    _send_vk(VK_C)
    time.sleep(0.015)
    _send_vk(VK_C, up=True)
    time.sleep(0.015)
    _send_vk(VK_CONTROL, up=True)


def foreground_window_title() -> str:
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value
    except Exception:
        return ""


def foreground_hwnd() -> int:
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def force_foreground(hwnd: int, retries: int = 4) -> bool:
    """把指定窗口切到前台。

    后台进程直接 SetForegroundWindow 会被 Windows 静默拒绝，
    通过 AttachThreadInput 挂接到目标窗口的输入线程后调用就能过。

    注意：之前用 keybd_event(VK_MENU) 模拟 Alt 键会被 watchdog 高频触发，
    把用户的真实 Alt 键状态搞乱（Alt 菜单闪烁），还可能在某些情况下让
    进程崩溃。改用纯 AttachThreadInput。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    try:
        # 挂接输入线程（标准解法，让目标窗口的"前台锁"暂时接受我们）
        cur = kernel32.GetCurrentThreadId()
        other = user32.GetWindowThreadProcessId(hwnd, None)
        attached = False
        if other and other != cur:
            user32.AttachThreadInput(cur, other, True)
            attached = True
        try:
            user32.SetForegroundWindow(hwnd)
            if user32.GetForegroundWindow() == hwnd:
                return True
            for _ in range(retries):
                time.sleep(0.06)
                user32.SetForegroundWindow(hwnd)
                if user32.GetForegroundWindow() == hwnd:
                    return True
        finally:
            if attached:
                user32.AttachThreadInput(cur, other, False)
    except Exception as exc:
        log("切换前台失败：%s" % exc)
    return user32.GetForegroundWindow() == hwnd


# ──────────────────────────── 桥接服务 ────────────────────────────


class Bridge:
    """面板页面通过它拉取最新选区。只监听 127.0.0.1。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._payload = {
            "seq": 0,
            "text": "",
            "title": "",
            "url": "",
            "ts": 0,
        }
        self.port = None
        self.httpd = None

    def push(self, text: str, title: str, url: str) -> int:
        with self._lock:
            self._seq += 1
            self._payload = {
                "seq": self._seq,
                "text": text,
                "title": title,
                "url": url,
                "ts": time.time(),
            }
            return self._seq

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._payload)

    def start(self, preferred_port) -> int:
        handler = self._make_handler()
        ports = []
        if preferred_port:
            ports.append(preferred_port)
        ports.extend(
            p for p in range(BRIDGE_PORT_FIRST, BRIDGE_PORT_LAST + 1) if p != preferred_port
        )

        last_error = None
        for port in ports:
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError as exc:
                last_error = exc
                continue
            httpd.daemon_threads = True
            self.httpd = httpd
            self.port = port
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            return port

        raise RuntimeError("桥接端口全部被占用：%s" % last_error)

    def stop(self) -> None:
        if self.httpd:
            try:
                self.httpd.shutdown()
            except Exception:
                pass

    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "TextLensBridge/1.0"
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 静默
                pass

            def _cors(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                # 新旧两套头都带：Chromium 把 Private Network Access 改名为
                # Local Network Access 后，响应头名字也跟着换了
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Allow-Local-Network", "true")
                self.send_header("Access-Control-Request-Private-Network", "true")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")

            def _json(self, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

            def do_GET(self):  # noqa: N802
                path = self.path.split("?", 1)[0].rstrip("/") or "/"
                snap = bridge.snapshot()

                if path == "/health":
                    self._json(
                        {
                            "ok": True,
                            "app": "textlens-bridge",
                            "version": 1,
                            "port": bridge.port,
                            "seq": snap["seq"],
                        }
                    )
                    return

                if path == "/current":
                    self._json(snap)
                    return

                if path == "/js":
                    # JSONP 兜底通道：不走 CORS，任何环境下都能用
                    body = "window.__textLensPush&&window.__textLensPush(%s);" % json.dumps(
                        snap, ensure_ascii=False
                    )
                    raw = body.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(raw)))
                    self._cors()
                    self.end_headers()
                    self.wfile.write(raw)
                    return

                self.send_response(404)
                self.send_header("Content-Length", "0")
                self._cors()
                self.end_headers()

        return Handler


# ──────────────────────────── 面板窗口 ────────────────────────────


def find_browser():
    for path in BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return None


def find_node():
    exe = shutil.which("node")
    if exe:
        return exe
    base = os.path.join(
        os.environ.get("USERPROFILE", ""), ".workbuddy", "binaries", "node", "versions"
    )
    try:
        versions = sorted(os.listdir(base), reverse=True)
    except Exception:
        versions = []
    for ver in versions:
        cand = os.path.join(base, ver, "node.exe")
        if os.path.isfile(cand):
            return cand
    return None


def pick_free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    except Exception:
        return CDP_PORT_DEFAULT
    finally:
        sock.close()


def _no_window_flags() -> int:
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    return flags


_browser_lock = threading.Lock()
CDP_PORT = None  # 面板窗口的调试端口，跨重启复用
_last_spawn_at = 0.0  # 上次拉起面板的时间，防止重复拉起造成“闪窗”
_unlock_lock = threading.Lock()
_last_unlock_at = 0.0  # 上次给面板注入钥匙的时间，用于节流
SPAWN_COOLDOWN_S = 8.0


def _dpi_scale() -> float:
    """当前系统 DPI 缩放（1.0=100%，1.5=150%）。

    Edge 的 --window-size / --window-position 收的是 DIP（逻辑像素），
    而本进程已开启 DPI 感知，GetSystemMetrics 返回的是物理像素。
    两者必须换算——否则 150% 屏上算出的坐标会被 Edge 当成 DIP 再放大 1.5 倍，
    结果窗口跑到屏幕外、尺寸也翻倍（"窗口大小不对 / 看不见"的真凶）。
    """
    try:
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        dc = user32.GetDC(0)
        if dc:
            dpi = gdi32.GetDeviceCaps(dc, 88)  # LOGPIXELSX
            user32.ReleaseDC(0, dc)
            if dpi:
                return float(dpi) / 96.0
    except Exception:
        pass
    return 1.0


def panel_args(browser: str, url: str, cdp_port) -> list:
    profile = os.path.join(APP_DIR, "profile")
    try:
        os.makedirs(profile, exist_ok=True)
    except Exception:
        pass

    scale = _dpi_scale() or 1.0

    # 物理像素下的屏幕尺寸（本进程 DPI 感知，拿到的是物理分辨率）
    screen_w = user32.GetSystemMetrics(0) or 1920
    screen_h = user32.GetSystemMetrics(1) or 1080

    # 期望的物理尺寸：窄长面板，高度不超过屏幕
    phys_w = 560
    phys_h = min(860, max(460, screen_h - 120))

    # 贴右侧留边距，并保证整窗都在屏幕内
    px = screen_w - phys_w - 40
    if px < 0:
        px = 0
    py = 70
    if py + phys_h > screen_h:
        py = max(0, screen_h - phys_h - 20)

    args = [
        browser,
        "--app=" + url,
        "--user-data-dir=" + profile,
        # Edge 收 DIP：物理像素 ÷ 缩放
        "--window-size=%d,%d" % (int(round(phys_w / scale)), int(round(phys_h / scale))),
        "--window-position=%d,%d" % (int(round(px / scale)), int(round(py / scale))),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,msEdgeWelcome,EdgeShoppingCarts",
        # 让 Edge 完全独立于用户日常 Edge 进程（singleton lock）
        "--new-window",
        "--no-default-apps-install",
        # 让 CDP 调试端口无鉴权（避免新版 Edge 的 --remote-allow-origins 限制）
    ]
    if cdp_port:
        args.append("--remote-debugging-port=%d" % cdp_port)
        # 显式声明只绑本机回环：调试端口等于"能读取页面全部内容"的后门，
        # 绝不能出现在局域网可达的地址上
        args.append("--remote-debugging-address=127.0.0.1")
        # Edge 109+ 要求显式声明允许的 origins
        args.append("--remote-allow-origins=*")
    return args


def _spawn_browser(browser: str, url: str, cdp_port) -> None:
    """拉起 Edge / Chrome 面板窗口。

    关键点：
    1. 不要用 DETACHED_PROCESS + CREATE_NO_WINDOW——会让子进程脱离父进程前台锁，
       窗口被默认放到屏幕外，用户看不到（之前踩过的坑）。
    2. 必须把 stdout/stderr 重定向到文件，否则 Python 子进程的管道缓冲区
       满了之后 Edge 自己 exit()，但日志显示"成功"——画面直接消失。
    """
    # Edge 的日志写到 APP_DIR 下的固定文件，便于事后查问题
    edge_log = os.path.join(APP_DIR, "edge.log")
    try:
        # 用 append 模式，多个 helper 实例不会互相覆盖
        logf = open(edge_log, mode="a", encoding="utf-8", buffering=1)
        # 写个分隔符和当前时间，便于区分每次启动
        logf.write("\n=== Edge 启动 %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        logf.flush()
    except Exception:
        logf = open(os.devnull, "w")

    subprocess.Popen(  # noqa: S603
        panel_args(browser, url, cdp_port),
        stdout=logf,
        stderr=subprocess.STDOUT,  # stderr 合并到 stdout，写到同一文件
        stdin=subprocess.DEVNULL,
    )


def panel_alive() -> bool:
    """面板是否健康：调试端口里有页面 + 窗口句柄存在 + 窗口可见。

    只看 CDP 是不够的：Edge --app 进程在窗口被最小化/关闭时仍然在跑，
    但用户看不到——必须把窗口可见性也纳入判断。
    """
    if not CDP_PORT:
        return False
    # 1) CDP 列表里必须有面板页面
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=1.5
        ) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
        host = panel_host()
        has_page = any(
            t.get("type") == "page" and host and host in (t.get("url") or "")
            for t in targets
        )
    except Exception:
        return False
    if not has_page:
        return False
    # 2) 窗口句柄必须存在 + 可见 + 不在最小化状态
    hwnd = find_panel_window()
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.IsIconic(hwnd):
        return False
    if not user32.IsWindowVisible(hwnd):
        return False
    return True


PANEL_WINDOW_TITLE = "1z read"


def find_panel_window() -> int:
    """按标题找面板窗口（Edge --app 窗口的标题就是页面标题）。

    注意：不能靠 CDP 判断窗口状态——页面进程活着但窗口被最小化时，
    CDP 一切正常，用户却看不到面板。
    """
    hwnd = user32.FindWindowW("Chrome_WidgetWin_1", PANEL_WINDOW_TITLE)
    if hwnd and user32.IsWindow(hwnd):
        return int(hwnd)
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        if not user32.IsWindow(h):
            return True
        # 必须是 Chromium 窗口类——气泡 Tk 窗口可能同名，不能误认
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cls, 64)
        if cls.value != "Chrome_WidgetWin_1":
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        if buf.value == PANEL_WINDOW_TITLE:
            found.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def _bring_panel_to_front(hwnd: int) -> None:
    """把面板窗口拉到屏幕中央前台。处理所有"看不到"的场景。"""
    if not hwnd or not user32.IsWindow(hwnd):
        return
    try:
        # 1) 如果被 minimize，先 SW_RESTORE
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.05)
        # 2) 如果被 hide 或屏幕外，强制 SW_SHOW
        if not user32.IsWindowVisible(hwnd):
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.05)
        # 3) 检查窗口是否在屏幕可见区域，掉到屏幕外就拉回中央
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            sw = user32.GetSystemMetrics(0)   # SM_CXSCREEN
            sh = user32.GetSystemMetrics(1)   # SM_CYSCREEN
            off_screen = (
                rect.right < 50
                or rect.bottom < 50
                or rect.left > sw - 50
                or rect.top > sh - 50
            )
            if off_screen:
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                x = max(0, (sw - w) // 2)
                y = max(0, (sh - h) // 2)
                user32.SetWindowPos(
                    hwnd, 0, x, y, 0, 0,
                    0x0040 | 0x0020,  # SWP_NOZORDER | SWP_NOSIZE
                )
                log("面板窗口原本在屏幕外，已拉回 (%d,%d)" % (x, y))
        # 4) BringWindowToTop + force_foreground
        user32.BringWindowToTop(hwnd)
        force_foreground(hwnd)
    except Exception as exc:
        log("拉面板到前台失败：%s" % exc)


def restore_panel_window() -> bool:
    """面板进程还在但窗口看不到时，把它恢复并提到前台。"""
    hwnd = find_panel_window()
    if not hwnd:
        return False
    try:
        _bring_panel_to_front(hwnd)
    except Exception as exc:
        log("恢复面板窗口失败：%s" % exc)
        return False
    return True


def _ensure_panel_webview() -> bool:
    """webview 模式：确保面板窗口可见并注入钥匙。

    本进程 GUI 在跑 → 复用/动态创建；不在跑（二次启动的短命进程）→
    把别的实例持有的面板窗口拉到前台。
    """
    if not is_provisioned():
        log("未接入：不打开面板（那份面板调不动模型）")
        return False

    if panelwin.has_window():
        panelwin.bring_to_front()
        _unlock_soon()
        return True

    if panelwin.gui_started():
        if panelwin.open_dynamic_window(panel_url()):
            _unlock_soon(1.5)
            return True
        log("webview：动态创建面板窗口失败")
        return False

    # 本进程没有 GUI（--open / 二次启动）：面板窗口归首实例管
    if panelwin.find_window_hwnd() and panelwin.bring_to_front():
        log("webview：面板在另一实例中，已拉到前台")
        return True
    log("webview：面板 GUI 未运行，无法打开面板")
    return False


def ensure_panel() -> None:
    """面板没开才打开；已经开着就恢复它（被最小化也要拉回来）。

    绝不新开第二个窗口：同一 user-data-dir 下重复拉起会让新进程闪一下
    就退出，用户看到的就是"窗口一闪就消失"。
    """
    global CDP_PORT, _last_spawn_at

    if panelwin.backend(load_config()) == "webview":
        _ensure_panel_webview()
        return

    # 接入门：没有自己的钥匙时，面板打开也是一片"未连接"，不如不开。
    if not is_provisioned():
        log("未接入：不打开面板（那份面板调不动模型）")
        return

    with _browser_lock:
        cfg = load_config()
        if not CDP_PORT:
            CDP_PORT = int(cfg.get("cdpPort") or 0) or pick_free_port()
            cfg["cdpPort"] = CDP_PORT
            save_config(cfg)

        if panel_alive():
            # 进程和页面都在，窗口也可见——保险地再拉一次前台
            restore_panel_window()
            # 页面可能刚从"锁定态"启动（或刚被刷新过），顺手补一次注入
            _unlock_soon()
            return

        # 进程活着但窗口不可见/被最小化，先尝试恢复，不重启
        hwnd = find_panel_window()
        if hwnd and user32.IsWindow(hwnd):
            _bring_panel_to_front(hwnd)
            log("面板进程还在但窗口不可见，已强制恢复")
            _unlock_soon()
            return

        # 刚拉起过（Edge 可能还在初始化、调试端口未就绪），不重复拉
        if time.time() - _last_spawn_at < SPAWN_COOLDOWN_S:
            restore_panel_window()
            _unlock_soon()
            return

        browser = find_browser()
        if not browser:
            log("没找到 Edge / Chrome，改用默认浏览器打开")
            try:
                os.startfile(panel_url())  # noqa: S606
            except Exception as exc:
                log("打开面板失败：%s" % exc)
            return

        _last_spawn_at = time.time()
        try:
            _spawn_browser(browser, panel_url(), CDP_PORT)
            log("已拉起面板窗口，等待 Edge 创建窗口...")
        except Exception as exc:
            log("启动面板失败：%s" % exc)
            return

        # 主动等 Edge 窗口出现（最长 15 秒），然后强制前台显示。
        # 不等的话：spawn 后立刻 return，窗口还没创建，下次调用又进 cooldown，
        # 用户在那一两秒点任务栏，看到的就是"窗口闪现又消失"。
        # Edge 冷启动可能超过 8 秒，给足余量。
        deadline = time.time() + 15.0
        appeared = 0
        while time.time() < deadline:
            hwnd = find_panel_window()
            if hwnd and user32.IsWindow(hwnd):
                appeared = hwnd
                break
            time.sleep(0.15)
        if appeared:
            # 等一秒让窗口渲染完
            time.sleep(0.6)
            _bring_panel_to_front(appeared)
            log("面板窗口已就位并拉到前台 (hwnd=%d)" % appeared)
            # 页面里不带钥匙，窗口起来后由助手补一次注入。
            # 走后台线程：注入要在页面脚本加载完之后，不能卡住钩子。
            _unlock_soon(1.0)
        else:
            log("面板窗口 15 秒内未出现，可能启动失败")


def _push_via_webview(payload: dict) -> bool:
    """webview 模式：进程内直推（等价 cdp_push.mjs 的两步：先注入钥匙再推选区）。

    钥匙只在进程内存里传递，连 stdin 管道都不经过。
    """
    payload = dict(payload or {})
    secret = payload.pop("__secret", None) or load_cloud_secret()
    if not secret:
        log("webview 推送中止：本机没有 cloud.json（未接入）")
        return False
    result = panelwin.push(payload, secret)
    if payload.get("text"):
        log("webview 推送（%d 字）：%s" % (len(payload.get("text") or ""), result))
    else:
        log("webview 解锁：%s" % result)
    if result in ("ok", "unlocked"):
        return True
    if result == "no-window" and _ensure_panel_webview():
        # 窗口不在了：重开面板再试一次
        result = panelwin.push(payload, secret)
        if result in ("ok", "unlocked"):
            return True
        log("webview 重开面板后仍未成功：%s" % result)
    return False


def push_via_cdp(payload: dict) -> bool:
    """把选区直接注入已打开的面板（不经过网络，不受浏览器安全策略限制）。

    webview 模式：进程内 evaluate_js 直推（panelwin.push）。
    edge 模式：起 Node 跑 cdp_push.mjs 走调试通道。payload 里会附上本机钥匙
    （__secret 保留字段），由接收端先 window.__lzrUnlock 注入页面、再推选区。
    钥匙只走内存/stdin，不落盘、不上网。
    """
    if panelwin.backend(load_config()) == "webview":
        return _push_via_webview(payload)

    node = find_node()
    if not node or not CDP_PORT:
        return False

    script = os.path.join(resource_dir(), "cdp_push.mjs")
    if not os.path.isfile(script):
        log("推送脚本缺失：%s" % script)
        return False

    out = dict(payload)
    secret = load_cloud_secret()
    if secret:
        out["__secret"] = secret

    # 把面板域名告诉 Node —— 它要在调试端口的目标列表里认出面板页。
    # 域名写死在脚本里的做法在「自带钥匙」场景下会认不出自己的面板，所以从这里传。
    env = dict(os.environ)
    host = panel_host()
    if host:
        env["TEXTLENS_URL_MATCH"] = re.escape(host)

    # 走 stdin：payload 里有钥匙和选区原文，落盘既多一份泄露面，又可能因为
    # 进程被强杀（finally 来不及跑）而残留。管道则随进程一起消失。
    data = json.dumps(out, ensure_ascii=False)
    try:
        proc = subprocess.run(  # noqa: S603
            [node, script, str(CDP_PORT)],
            input=data,
            capture_output=True,
            text=True,
            # 必须显式 utf-8：见 SUBPROC_ENCODING 的说明。
            # 不指定时 Windows 中文系统会用 cp936 编码 input，Node 侧按 utf8
            # 解析 → 选区中文全变乱码（本文件最贵的一个坑）。
            encoding=SUBPROC_ENCODING,
            errors="replace",   # 剪贴板理论上可能含孤立代理字符，别让整次推送崩掉
            timeout=45,
            creationflags=_no_window_flags(),
            env=env,
        )
    except Exception as exc:
        log("调试通道推送失败：%s" % exc)
        return False

    if proc.returncode == 0:
        return True
    detail = (proc.stdout or proc.stderr or "").strip()[:200]
    if detail == "bad payload":
        # 走到这里多半是编码没对齐（例如有人把 encoding 参数去掉又退回 cp936）
        log("调试通道推送未成功：%s（payload 解析失败，疑似编码不一致）" % detail)
    else:
        log("调试通道推送未成功：%s" % detail)
    return False


def push_via_cdp_with_mode(payload: dict) -> bool:
    """带动作模式（搜索 / 解析）的推送。"""
    return push_via_cdp(payload)


def unlock_panel() -> bool:
    """只给面板注入钥匙，不推送任何选区。

    面板页面本身不带钥匙（防止公开网址被人白嫖额度），所以窗口起来之后
    必须由助手把它"唤醒"。每次推送也会顺带注入一次，这里主要是覆盖
    "只打开面板、还没选中文字"的情况。
    """
    if panelwin.backend(load_config()) == "webview":
        if not load_cloud_secret():
            log("没有 cloud.json，面板将停在未连接状态")
            return False
        return push_via_cdp({})
    if not CDP_PORT:
        return False
    if not load_cloud_secret():
        log("没有 cloud.json，面板将停在未连接状态")
        return False
    return push_via_cdp({})


def _unlock_soon(delay: float = 0.0) -> None:
    """在后台线程里补一次注入，绝不阻塞钩子线程。

    3 秒内重复调用会被吞掉：注入本身是幂等的（页面第二次会返回 already），
    但没必要为每次划选都多起一个 Node 进程。
    """
    def run():
        if delay:
            time.sleep(delay)
        global _last_unlock_at
        with _unlock_lock:
            now = time.time()
            if now - _last_unlock_at < 3.0:
                return
            _last_unlock_at = now
        try:
            unlock_panel()
        except Exception as exc:
            log("注入钥匙失败：%s" % exc)

    threading.Thread(target=run, daemon=True).start()


def open_with_text(payload: dict) -> None:
    """兜底：把选区放进 URL 直接打开（会新开一个窗口）。"""
    if panelwin.backend(load_config()) == "webview":
        payload = dict(payload or {})
        secret = payload.pop("__secret", None) or load_cloud_secret()
        if not secret:
            log("webview 兜底推送中止：本机没有 cloud.json（未接入）")
            return
        if _ensure_panel_webview():
            result = panelwin.push(payload, secret)
            log("webview 兜底推送：%s" % result)
        return
    params = {
        "text": (payload.get("text") or "")[:PUSH_TEXT_URL_LIMIT],
        "title": payload.get("title") or "",
        "url": payload.get("url") or "",
        "mode": payload.get("mode") or "analyze",
    }
    url = panel_url() + "&" + urllib.parse.urlencode(params)
    browser = find_browser()
    if not browser:
        try:
            os.startfile(url)  # noqa: S606
        except Exception as exc:
            log("打开面板失败：%s" % exc)
        return
    try:
        # 带上调试端口：这个页面同样没有钥匙，起来后要靠 CDP 注入补上
        _spawn_browser(browser, url, CDP_PORT)
    except Exception as exc:
        log("打开面板失败：%s" % exc)
        return
    _unlock_soon(2.0)


# ──────────────────────────── 托盘图标（纯 ctypes） ────────────────────────────
#
# 用户关掉面板窗口时可以选择「后台运行」：这时任务栏上的窗口没了，但助手进程
# 还得活着（继续监听划词），所以必须在通知区域（托盘 / 隐藏图标区）留一个入口，
# 否则用户既看不到它、也没法退出。用 Shell_NotifyIcon 实现，不引第三方库。

WM_TRAY = 0x0400 + 20          # WM_APP + 20：托盘图标的鼠标回调消息
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
WM_NULL = 0x0000
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x0001

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040

TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

SM_CXSMICON = 49

TRAY_CMD_OPEN, TRAY_CMD_QUIT, TRAY_CMD_SETUP = 1, 2, 3


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW（Vista+ 的完整版，cbSize 用结构体真实大小）。"""

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.WORD
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = HWND
user32.DefWindowProcW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.DestroyWindow.argtypes = [HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), HWND, wintypes.UINT, wintypes.UINT
]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.LoadImageW.argtypes = [
    wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.LoadImageW.restype = wintypes.HANDLE
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.DestroyIcon.restype = wintypes.BOOL
user32.CreatePopupMenu.argtypes = []
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.AppendMenuW.restype = wintypes.BOOL
user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, HWND, ctypes.c_void_p,
]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.DestroyMenu.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.MessageBoxW.argtypes = [HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
# 只设 restype：第二个参数其实是 MAKEINTRESOURCE（整数），设了 argtypes 反而没法传
user32.LoadIconW.restype = wintypes.HICON


def _icon_file() -> str:
    """托盘 / 快捷方式用的 .ico（源码运行时在脚本同目录，exe 运行时在内嵌资源里）。"""
    return os.path.join(resource_dir(), "app.ico")


_tray_current = None    # 当前活着的托盘实例（窗口类只注册一次，回调靠它转发）


def _tray_wnd_proc(hwnd, msg, wparam, lparam):
    tray = _tray_current
    try:
        if tray is not None:
            if msg == WM_TRAY:
                ev = int(lparam) & 0xFFFF
                if ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    threading.Thread(target=tray.on_open, daemon=True).start()
                elif ev in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    tray._popup_menu()
            elif msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
    except Exception as exc:
        log("托盘窗口消息处理异常：%s" % exc)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# WNDPROC 对象必须常驻，否则会被 GC 掉导致窗口回调变成野指针
_TRAY_WNDPROC = WNDPROC(_tray_wnd_proc)


class TrayIcon:
    """通知区域图标：左键单击/双击打开面板，右键弹菜单（打开 / 接入设置 / 退出）。

    必须在自己的线程里建窗口类 + 窗口 + 跑消息循环（窗口消息是线程亲和的）。
    """

    CLASS_NAME = "1zReadTrayWindow"

    def __init__(self, on_open, on_quit, on_setup=None) -> None:
        self.on_open = on_open
        self.on_quit = on_quit
        self.on_setup = on_setup
        self.hwnd = 0
        self.ok = False
        self._thread = None
        self._ready = threading.Event()
        self._nid = None
        self._hicon = 0

    # ── 对外接口 ──

    def start(self, timeout: float = 6.0) -> bool:
        if self._thread:
            return self.ok
        self._thread = threading.Thread(target=self._run, daemon=True, name="tray")
        self._thread.start()
        self._ready.wait(timeout)
        return self.ok

    def stop(self, wait: float = 1.5) -> None:
        """让托盘线程自己拆窗口（顺便摘掉图标），避免留下"幽灵图标"。"""
        if not self._thread:
            return
        try:
            if self.hwnd:
                user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass
        self._thread.join(wait)

    def notify(self, title: str, text: str) -> None:
        """气泡提示（Windows 会显示成通知 / 气球）。"""
        if not self.ok or not self.hwnd:
            return
        try:
            nid = self._nid
            if nid is None:
                return
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = (title or "")[:63]
            nid.szInfo = (text or "")[:255]
            nid.dwInfoFlags = NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception as exc:
            log("托盘提示失败：%s" % exc)

    # ── 线程内部 ──

    def _load_icon(self) -> int:
        path = _icon_file()
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        if os.path.isfile(path):
            h = user32.LoadImageW(
                None, path, IMAGE_ICON, size, size, LR_LOADFROMFILE
            )
            if h:
                return int(h)
            h = user32.LoadImageW(
                None, path, IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            if h:
                return int(h)
            log("托盘：图标加载失败，用系统默认图标 %s" % path)
        else:
            log("托盘：找不到图标文件 %s，用系统默认图标" % path)
        return int(user32.LoadIconW(None, 32512))  # IDI_APPLICATION

    def _add_icon(self) -> bool:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = "%s — 正在后台运行（划词可用）" % APP_NAME
        self._nid = nid
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            log("托盘：Shell_NotifyIconW 添加失败 err=%d" % ctypes.get_last_error())
            return False
        return True

    def _remove_icon(self) -> None:
        if self._nid is not None and self.hwnd:
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
        self._nid = None

    def _popup_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        user32.AppendMenuW(hmenu, 0, TRAY_CMD_OPEN, "打开 %s" % APP_NAME)
        user32.AppendMenuW(hmenu, 0, TRAY_CMD_SETUP, "接入设置…")
        user32.AppendMenuW(hmenu, 0, TRAY_CMD_QUIT, "退出 %s" % APP_NAME)
        pt = POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # 不先把窗口设成前台，菜单点到别处不会消失（Win32 老脾气）
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu,
            TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)

        if cmd == TRAY_CMD_OPEN:
            threading.Thread(target=self.on_open, daemon=True).start()
        elif cmd == TRAY_CMD_SETUP:
            if self.on_setup:
                threading.Thread(target=self.on_setup, daemon=True).start()
        elif cmd == TRAY_CMD_QUIT:
            threading.Thread(target=self.on_quit, daemon=True).start()

    def _run(self) -> None:
        global _tray_current
        _tray_current = self
        try:
            hinst = kernel32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = _TRAY_WNDPROC
            wc.hInstance = hinst
            wc.lpszClassName = self.CLASS_NAME
            if not user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS：重复注册，无妨
                    log("托盘：注册窗口类失败 err=%d" % err)
                    self._ready.set()
                    return

            hwnd = user32.CreateWindowExW(
                0, self.CLASS_NAME, "%s 托盘" % APP_NAME, 0,
                0, 0, 0, 0, None, None, hinst, None,
            )
            if not hwnd:
                log("托盘：创建消息窗口失败 err=%d" % ctypes.get_last_error())
                self._ready.set()
                return
            self.hwnd = int(hwnd)
            self._hicon = self._load_icon()

            if not self._add_icon():
                user32.DestroyWindow(hwnd)
                self.hwnd = 0
                self._ready.set()
                return

            self.ok = True
            self._ready.set()
            log("托盘图标：已在通知区域显示（右键可退出）")

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log("托盘线程异常：%s" % exc)
            self._ready.set()
        finally:
            self.ok = False
            self._remove_icon()
            if _tray_current is self:
                _tray_current = None
            if self.hwnd:
                try:
                    user32.DestroyWindow(self.hwnd)
                except Exception:
                    pass
                self.hwnd = 0
            if self._hicon:
                try:
                    user32.DestroyIcon(self._hicon)
                except Exception:
                    pass
                self._hicon = 0

    def _make_proc(self):  # 兼容旧调用：真正的窗口过程是模块级 _TRAY_WNDPROC
        return _TRAY_WNDPROC


# ──────────────────────────── 悬停气泡 ────────────────────────────


def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（smooth 多边形近似圆角，边缘平滑）。"""
    r = max(0.0, min(r, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, **kw)


class BubbleUI:
    """划选后悬停出现的小气泡（AI 搜索 / 解析）。

    Tkinter 必须在同一线程里跑自己的事件循环，所以单独开守护线程，
    其它线程通过队列向它发指令。tkinter 不可用时静默降级。
    外形：白色大圆角胶囊 + 两个扁平按钮；圆角靠 Windows 透明色键抠图实现。
    """

    OPTIONS = [("search", "AI 搜索"), ("analyze", "解析")]

    KEY = "#ff00fe"          # 透明色键（layered window 抠图，胶囊内部不会用到该色）
    BG = "#ffffff"
    BORDER = "#e3e7ef"
    DIVIDER = "#edf0f5"
    FG = "#1b1f26"
    HOVER_BG = "#eef3ff"
    HOVER_FG = "#1f4fd6"
    BTN_PADX = 14            # 按钮左右内边距
    BTN_PADY = 8
    GAP = 8                  # 两按钮间距
    OUT_PAD = 8              # 按钮到胶囊边缘的留白（须保证按钮四角落在圆角内部）
    RADIUS = 20              # 圆角半径（按钮四角都落在圆角内部，见 OUT_PAD 说明）
    FONT = ("Microsoft YaHei UI", 9)

    def __init__(self, on_pick) -> None:
        self.on_pick = on_pick          # on_pick(mode, payload)
        self._queue = queue.Queue()
        self._thread = None
        self._tk = None
        self._canvas = None
        self._buttons = []
        self._payload = None
        self._hide_after = None
        self._ready = threading.Event()
        self._size = (210, 44)
        self._key_ok = False
        self.ok = False
        self.visible = False            # 当前是否正显示着（供热键路径判断）
        self.rect = None                # 显示中气泡的屏幕矩形 (x,y,w,h)，供钩子线程判断点击
        # 关闭询问框（同样是 Tk 窗口，复用这个线程）
        self._tkmod = None
        self._ask_win = None
        self._ask_cb = None
        self._ask_var = None
        # 接入向导（同样是 Tk 窗口，复用这个线程）
        self._gate_win = None
        self._gate_cb = None
        self._gate_vars = None
        self._gate_hint = None

    def start(self) -> bool:
        if self._thread:
            return self.ok
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(6)
        return self.ok

    def show(self, x, y, payload) -> None:
        self._queue.put(("show", (x, y, payload)))

    def hide(self) -> None:
        self._queue.put(("hide", None))

    def _run(self) -> None:
        try:
            import tkinter as tk  # noqa: PLC0415
        except Exception as exc:
            log("tkinter 不可用，悬停气泡关闭：%s" % exc)
            self._ready.set()
            return
        self._tkmod = tk

        try:
            root = tk.Tk()
            self._tk = root
            root.title("1z read·气泡")
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            # 永远保持 visible，让 Tk 主循环 / after() 不会被冻结。
            # 默认放到屏幕外、完全透明，看不见也不抢焦点。
            self._corner_xy = (-2000, -2000)
            root.geometry("210x44+%d+%d" % self._corner_xy)
            try:
                root.attributes("-alpha", 0.0)
            except Exception:
                pass

            # 透明色键：整窗底色设为键色并抠成透明，胶囊圆角外面才是真透明（而非白方块）。
            # 系统不支持时退化成白底，圆角依旧可见（只是四角是白的）。
            self._key_ok = False
            try:
                root.attributes("-transparentcolor", self.KEY)
                self._key_ok = True
            except Exception:
                pass
            corner = self.KEY if self._key_ok else self.BG
            root.configure(bg=corner)

            canvas = tk.Canvas(
                root, bg=corner, highlightthickness=0, bd=0, width=210, height=44
            )
            canvas.pack()
            self._canvas = canvas
            self._size = self._build_ui(tk, canvas)

            root.bind("<Escape>", lambda _e: self._hide())

            self.ok = True
            self._ready.set()
            self._pump(root)
            root.mainloop()
        except Exception as exc:
            log("气泡界面启动失败：%s" % exc)
            self.ok = False
            self._ready.set()

    def _build_ui(self, tk, canvas):
        """画胶囊底 + 两个扁平按钮，返回 (宽, 高)。"""
        btns = []
        for mode, label in self.OPTIONS:
            btn = tk.Button(
                canvas,
                text=label,
                bd=0,
                relief="flat",
                highlightthickness=0,
                padx=self.BTN_PADX,
                pady=self.BTN_PADY,
                bg=self.BG,
                fg=self.FG,
                activebackground=self.HOVER_BG,
                activeforeground=self.HOVER_FG,
                font=self.FONT,
                cursor="hand2",
                takefocus=0,
                command=lambda m=mode: self._pick(m),
            )
            btn.bind(
                "<Enter>",
                lambda _e, b=btn: b.configure(bg=self.HOVER_BG, fg=self.HOVER_FG),
            )
            btn.bind("<Leave>", lambda _e, b=btn: b.configure(bg=self.BG, fg=self.FG))
            btns.append(btn)

        canvas.update_idletasks()
        sizes = [(b.winfo_reqwidth(), b.winfo_reqheight()) for b in btns]
        w = self.OUT_PAD * 2 + sum(s[0] for s in sizes) + self.GAP * (len(sizes) - 1)
        h = self.OUT_PAD * 2 + max(s[1] for s in sizes)
        canvas.configure(width=w, height=h)

        _round_rect(
            canvas, 1, 1, w - 1, h - 1, self.RADIUS,
            fill=self.BG, outline=self.BORDER, width=1,
        )

        x = self.OUT_PAD
        for i, (btn, (bw, bh)) in enumerate(zip(btns, sizes)):
            canvas.create_window(x, h / 2.0, window=btn, anchor="w", width=bw, height=bh)
            x += bw
            if i < len(sizes) - 1:
                mid = x + self.GAP / 2.0
                canvas.create_line(
                    mid, self.OUT_PAD + 6, mid, h - self.OUT_PAD - 6, fill=self.DIVIDER
                )
                x += self.GAP

        self._buttons = btns
        log("气泡外形：%dx%d 圆角%d（透明色键=%s）" % (w, h, self.RADIUS, self._key_ok))
        return (w, h)

    def _pump(self, root) -> None:
        try:
            while True:
                cmd, data = self._queue.get_nowait()
                if cmd == "show":
                    self._do_show(root, data)
                elif cmd == "hide":
                    self._hide()
                elif cmd == "ask":
                    self._do_ask(root, data)
                elif cmd == "ask_hide":
                    self._do_ask_hide()
                elif cmd == "gate":
                    self._do_gate(root, data)
                elif cmd == "gate_hide":
                    self._close_gate()
        except queue.Empty:
            pass
        root.after(80, lambda: self._pump(root))

    def _do_show(self, root, data) -> None:
        x, y, payload = data
        self._payload = payload

        root.update_idletasks()
        w, h = self._size
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()

        px = min(max(0, x - w // 2), max(0, sw - w - 8))
        py = y + 16
        if py + h > sh - 8:
            py = max(0, y - h - 12)

        root.geometry("%dx%d+%d+%d" % (w, h, px, py))
        try:
            root.attributes("-alpha", 1.0)
        except Exception:
            pass
        root.deiconify()
        root.lift()
        self.visible = True
        self.rect = (px, py, w, h)
        log("气泡已显示：rect=%dx%d+%d+%d（选区末端 %d,%d）" % (w, h, px, py, x, y))

        if self._hide_after:
            try:
                root.after_cancel(self._hide_after)
            except Exception:
                pass
        self._hide_after = root.after(15000, self._hide)

    def _hide(self) -> None:
        self.visible = False
        self.rect = None
        if not self._tk:
            return
        try:
            self._tk.attributes("-alpha", 0.0)
            self._tk.geometry("+%d+%d" % self._corner_xy)
        except Exception:
            pass

    def _pick(self, mode: str) -> None:
        payload = self._payload
        self._hide()
        if payload and self.on_pick:
            threading.Thread(target=self.on_pick, args=(mode, payload), daemon=True).start()

    # ── 关闭询问框：复用同一个 Tk 线程 ──

    def ask_close(self, callback) -> None:
        """弹出「后台运行 / 关闭」询问框。callback(action, remember)。"""
        self._queue.put(("ask", callback))

    def hide_ask(self) -> None:
        self._queue.put(("ask_hide", None))

    def _do_ask(self, root, callback) -> None:
        tk = self._tkmod
        if tk is None:
            return
        if self._ask_win is not None:
            try:
                if self._ask_win.winfo_exists():
                    self._ask_cb = callback
                    self._ask_win.lift()
                    self._ask_win.focus_force()
                    return
            except Exception:
                pass

        PAD = 20
        GAP_Y = 12
        BTN_GAP = 10
        UI = ("Microsoft YaHei UI", 9)
        ACCENT = "#1f4fd6"
        ACCENT_HOVER = "#1a44bb"
        SUB_FG = "#6b7280"
        NEUTRAL = "#f1f3f8"
        NEUTRAL_HOVER = "#e4e8f0"

        top = tk.Toplevel(root)
        top.title("%s·关闭" % APP_NAME)   # 无边框窗口不显示标题，但窗口文本仍在（便于抓图/排查）
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        corner = self.KEY if self._key_ok else self.BG
        if self._key_ok:
            try:
                top.attributes("-transparentcolor", self.KEY)
            except Exception:
                pass
        top.configure(bg=corner)

        canvas = tk.Canvas(top, bg=corner, highlightthickness=0, bd=0)
        canvas.pack()

        title = tk.Label(
            canvas, text="关闭 %s 吗？" % APP_NAME, bg=self.BG, fg=self.FG,
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w",
        )
        sub = tk.Label(
            canvas, text="关掉窗口后仍可在后台继续划词使用", bg=self.BG,
            fg=SUB_FG, font=UI, anchor="w",
        )
        var = tk.BooleanVar(value=False)
        chk = tk.Checkbutton(
            canvas, text="下次不再询问", variable=var,
            bg=self.BG, fg="#4b5563", activebackground=self.BG,
            activeforeground="#4b5563", selectcolor="#ffffff",
            font=UI, bd=0, highlightthickness=0, anchor="w", cursor="hand2",
        )
        b_bg = tk.Button(
            canvas, text="后台运行", bd=0, relief="flat", highlightthickness=0,
            padx=18, pady=7, bg=ACCENT, fg="#ffffff",
            activebackground=ACCENT_HOVER, activeforeground="#ffffff",
            font=UI, cursor="hand2",
            command=lambda: self._ask_answer("background"),
        )
        b_quit = tk.Button(
            canvas, text="关闭 %s" % APP_NAME, bd=0, relief="flat",
            highlightthickness=0, padx=18, pady=7, bg=NEUTRAL, fg=self.FG,
            activebackground=NEUTRAL_HOVER, activeforeground=self.FG,
            font=UI, cursor="hand2",
            command=lambda: self._ask_answer("quit"),
        )

        canvas.update_idletasks()
        w_t, h_t = title.winfo_reqwidth(), title.winfo_reqheight()
        w_s, h_s = sub.winfo_reqwidth(), sub.winfo_reqheight()
        w_c, h_c = chk.winfo_reqwidth(), chk.winfo_reqheight()
        w_1, h_1 = b_bg.winfo_reqwidth(), b_bg.winfo_reqheight()
        w_2, h_2 = b_quit.winfo_reqwidth(), b_quit.winfo_reqheight()
        row_w, row_h = w_1 + BTN_GAP + w_2, max(h_1, h_2)

        W = max(w_t, w_s, w_c, row_w) + PAD * 2
        H = PAD * 2 + h_t + 6 + h_s + GAP_Y + h_c + GAP_Y + row_h
        canvas.configure(width=W, height=H)
        _round_rect(
            canvas, 1, 1, W - 1, H - 1, 16,
            fill=self.BG, outline=self.BORDER, width=1,
        )

        y = PAD
        canvas.create_window(PAD, y, window=title, anchor="nw", width=w_t, height=h_t)
        y += h_t + 6
        canvas.create_window(PAD, y, window=sub, anchor="nw", width=w_s, height=h_s)
        y += h_s + GAP_Y
        canvas.create_window(PAD, y, window=chk, anchor="nw", width=w_c, height=h_c)
        y += h_c + GAP_Y
        bx = W - PAD - row_w
        canvas.create_window(
            bx, y + (row_h - h_1) / 2.0, window=b_bg, anchor="nw", width=w_1, height=h_1
        )
        canvas.create_window(
            bx + w_1 + BTN_GAP, y + (row_h - h_2) / 2.0,
            window=b_quit, anchor="nw", width=w_2, height=h_2,
        )

        # 出现在鼠标附近（面板刚被关掉，鼠标就在 × 上），并保证整框在屏幕里
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
            px = root.winfo_pointerx() - W // 2
            py = root.winfo_pointery() - H // 3
            px = min(max(8, px), max(8, sw - W - 8))
            py = min(max(8, py), max(8, sh - H - 8))
        except Exception:
            px = py = 120
        top.geometry("%dx%d+%d+%d" % (int(W), int(H), int(px), int(py)))
        top.deiconify()
        top.lift()
        top.bind("<Escape>", lambda _e: self._ask_answer("cancel"))
        try:
            top.focus_force()
        except Exception:
            pass

        self._ask_win = top
        self._ask_cb = callback
        self._ask_var = var
        log("关闭询问框已显示：%dx%d+%d+%d" % (W, H, px, py))

    def _ask_answer(self, action: str) -> None:
        win, cb, var = self._ask_win, self._ask_cb, self._ask_var
        self._ask_win = None
        self._ask_cb = None
        self._ask_var = None
        remember = False
        try:
            remember = bool(var.get())
        except Exception:
            remember = False
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        log("关闭询问框：选择 %s（记住=%s）" % (action, remember))
        if cb:
            threading.Thread(
                target=cb, args=(action, remember), daemon=True
            ).start()

    def _do_ask_hide(self) -> None:
        win = self._ask_win
        self._ask_win = None
        self._ask_cb = None
        self._ask_var = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    # ── 接入向导：复用同一个 Tk 线程 ──

    def ask_gate(self, on_result, prefill=None) -> None:
        """弹出「接入你自己的 AI 额度」向导。on_result(ok) 在后台线程里被调用。"""
        self._queue.put(("gate", (on_result, prefill or {})))

    def hide_gate(self) -> None:
        self._queue.put(("gate_hide", None))

    def _do_gate(self, root, data) -> None:
        on_result, prefill = data
        tk = self._tkmod
        if tk is None:
            return
        # 已经开着就抬到前台，不重复造窗口
        if self._gate_win is not None:
            try:
                if self._gate_win.winfo_exists():
                    self._gate_cb = on_result
                    self._gate_win.lift()
                    self._gate_win.focus_force()
                    return
            except Exception:
                pass

        UI = ("Microsoft YaHei UI", 9)
        UI_B = ("Microsoft YaHei UI", 11, "bold")
        FG = self.FG
        SUB = "#6b7280"
        ACCENT = "#1f4fd6"
        ACCENT_HOVER = "#1a44bb"
        NEUTRAL = "#f1f3f8"
        NEUTRAL_HOVER = "#e4e8f0"
        WRAP = 448

        top = tk.Toplevel(root)
        top.title("%s · 接入你自己的 AI 额度" % APP_NAME)
        top.attributes("-topmost", True)
        top.resizable(False, False)
        top.configure(bg="#ffffff")

        box = tk.Frame(top, bg="#ffffff", padx=24, pady=18)
        box.pack(fill="both", expand=True)

        tk.Label(box, text="接入你自己的 AI 额度", bg="#ffffff", fg=FG,
                 font=UI_B, anchor="w").pack(fill="x")

        intro = tk.Label(
            box,
            text=("%s 不自带公共 AI 额度。它调用模型用的是「你自己的 WorkBuddy "
                  "应用」，所以用量算在你自己头上，不会花到别人的额度。"
                  % APP_NAME),
            bg="#ffffff", fg=SUB, font=UI, justify="left", anchor="w",
            wraplength=WRAP,
        )
        intro.pack(fill="x", pady=(8, 14))

        row = tk.Frame(box, bg="#ffffff")
        row.pack(fill="x", pady=(0, 14))
        tk.Label(row, text="还没有 WorkBuddy 账号？", bg="#ffffff", fg=SUB,
                 font=UI).pack(side="left")
        tk.Button(
            row, text="去登录 / 注册", bd=0, relief="flat", highlightthickness=0,
            padx=12, pady=4, bg=NEUTRAL, fg=ACCENT, activebackground=NEUTRAL_HOVER,
            activeforeground=ACCENT, font=UI, cursor="hand2",
            command=self._gate_open_signup,
        ).pack(side="left", padx=(8, 0))

        tk.Label(box, text="① 把这段话发给 WorkBuddy，让它帮你发布这个项目：",
                 bg="#ffffff", fg=FG, font=UI, anchor="w").pack(fill="x")
        code = tk.Label(
            box, text=PUBLISH_PROMPT, bg="#f7f8fa", fg="#374151", font=UI,
            justify="left", anchor="w", wraplength=WRAP - 20,
            padx=10, pady=8, bd=1, relief="solid",
        )
        code.pack(fill="x", pady=(6, 6))
        code.bind("<Button-1>", lambda _e: self._gate_copy(top))
        tk.Button(
            box, text="复制提示词", bd=0, relief="flat", highlightthickness=0,
            padx=12, pady=4, bg=NEUTRAL, fg=ACCENT, activebackground=NEUTRAL_HOVER,
            activeforeground=ACCENT, font=UI, cursor="hand2",
            command=lambda: self._gate_copy(top),
        ).pack(anchor="w", pady=(0, 14))

        tk.Label(box, text="② 把 WorkBuddy 给你的三项填在这里：",
                 bg="#ffffff", fg=FG, font=UI, anchor="w").pack(fill="x")

        form = tk.Frame(box, bg="#ffffff")
        form.pack(fill="x", pady=(6, 0))
        vars_ = {}
        fields = (
            ("panelBase", "面板网址", "必填，你发布后拿到的网址"),
            ("publishableKey", "publishableKey", "wbpk_ 开头"),
            ("resourceId", "resourceId", "wbsc_ 开头，可留空"),
        )
        for i, (name, text, tip) in enumerate(fields):
            tk.Label(form, text=text, bg="#ffffff", fg=FG, font=UI,
                     anchor="w").grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(prefill.get(name) or ""))
            vars_[name] = var
            ent = tk.Entry(form, textvariable=var, font=UI, width=40,
                           relief="solid", bd=1, highlightthickness=0)
            ent.grid(row=i, column=1, sticky="we", padx=(10, 10), pady=3)
            if i == 0:
                ent.focus_set()
            tk.Label(form, text=tip, bg="#ffffff", fg="#9ca3af", font=UI,
                     anchor="w").grid(row=i, column=2, sticky="w", pady=3)
        form.columnconfigure(1, weight=1)

        hint = tk.Label(box, text="", bg="#ffffff", fg="#b91c1c", font=UI,
                        anchor="w", justify="left", wraplength=WRAP)
        hint.pack(fill="x", pady=(10, 0))

        bar = tk.Frame(box, bg="#ffffff")
        bar.pack(fill="x", pady=(14, 0))
        tk.Button(
            bar, text="保存并启用", bd=0, relief="flat", highlightthickness=0,
            padx=18, pady=6, bg=ACCENT, fg="#ffffff", activebackground=ACCENT_HOVER,
            activeforeground="#ffffff", font=UI, cursor="hand2",
            command=self._gate_submit,
        ).pack(side="right")
        tk.Button(
            bar, text="稍后再说", bd=0, relief="flat", highlightthickness=0,
            padx=16, pady=6, bg=NEUTRAL, fg=FG, activebackground=NEUTRAL_HOVER,
            activeforeground=FG, font=UI, cursor="hand2",
            command=self._gate_cancel,
        ).pack(side="right", padx=(0, 8))

        self._gate_win = top
        self._gate_cb = on_result
        self._gate_vars = vars_
        self._gate_hint = hint

        top.protocol("WM_DELETE_WINDOW", self._gate_cancel)
        top.bind("<Escape>", lambda _e: self._gate_cancel())
        top.bind("<Return>", lambda _e: self._gate_submit())

        # 说明文字按实际宽度重新换行，避免右边一大块空白
        top.update_idletasks()
        inner = max(300, top.winfo_reqwidth() - 48)
        try:
            intro.configure(wraplength=inner)
            code.configure(wraplength=inner - 20)
            hint.configure(wraplength=inner)
        except Exception:
            pass
        top.update_idletasks()

        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        except Exception:
            sw, sh = 1280, 800
        px = max(8, (sw - w) // 2)
        py = max(8, (sh - h) // 2)
        top.geometry("%dx%d+%d+%d" % (w, h, px, py))
        top.lift()
        try:
            top.focus_force()
        except Exception:
            pass
        log("接入向导已显示：%dx%d+%d+%d" % (w, h, px, py))

    def _gate_copy(self, top) -> None:
        try:
            top.clipboard_clear()
            top.clipboard_append(PUBLISH_PROMPT)
            top.update()
        except Exception as exc:
            log("复制提示词失败：%s" % exc)
            return
        if self._gate_hint is not None:
            try:
                self._gate_hint.configure(text="提示词已复制，粘给 WorkBuddy 即可。",
                                          fg="#0f6e56")
            except Exception:
                pass

    def _gate_open_signup(self) -> None:
        def run():
            try:
                webbrowser.open(WORKBUDDY_SIGNUP_URL)
                log("已打开 WorkBuddy 登录页：%s" % WORKBUDDY_SIGNUP_URL)
            except Exception as exc:
                log("打开 WorkBuddy 登录页失败：%s" % exc)

        threading.Thread(target=run, daemon=True).start()

    def _gate_submit(self) -> None:
        vars_ = self._gate_vars or {}
        try:
            panel = vars_["panelBase"].get()
            key = vars_["publishableKey"].get()
            resource = vars_["resourceId"].get()
        except Exception:
            return
        ok, msg = save_provision(panel, key, resource)
        if not ok:
            log("接入向导：校验未通过（%s）" % msg)
            if self._gate_hint is not None:
                try:
                    self._gate_hint.configure(text=msg, fg="#b91c1c")
                except Exception:
                    pass
            return
        log("接入向导：已写入 %s（面板 %s）" % (msg, panel_url()))
        cb = self._gate_cb
        self._close_gate()
        self._fire_gate(cb, True)

    def _gate_cancel(self) -> None:
        log("接入向导：用户选择稍后再说（工具保持未启用）")
        cb = self._gate_cb
        self._close_gate()
        self._fire_gate(cb, False)

    def _fire_gate(self, cb, ok: bool) -> None:
        if not cb:
            return
        threading.Thread(target=cb, args=(ok,), daemon=True).start()

    def _close_gate(self) -> None:
        win = self._gate_win
        self._gate_win = None
        self._gate_cb = None
        self._gate_vars = None
        self._gate_hint = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass


class Controller:
    def __init__(self, bridge: Bridge, cfg: dict) -> None:
        self.bridge = bridge
        self.cfg = cfg
        self._busy = threading.Lock()
        self._pending = []          # 待处理触发：('hotkey'|'hover', mode)
        self._event = threading.Event()
        self._stop = False
        self._hook = None
        self._mouse_hook = None
        self._mouse_uninstalled = False
        self._drag_state = {
            "down": None,
            "up_at": 0.0,
            "up_xy": (0, 0),
            "down_text": False,   # 按下那一刻光标是否落在文字上（划选判定的核心）
            "blocked": False,     # 按下时命中受保护程序 → 整个拖拽过程都不处理
        }
        # 上一次成功划选的记录：补偿"浏览器在已选中文字上显示箭头/手型光标"
        # 导致的二次划选失效（见 _on_mouse_ll 里的按下分支）
        self._last_sel = {"root": 0, "rect": None, "at": 0.0}
        # 悬停气泡计时：纯 Python 线程内完成，绝不跨线程调 Tk（tkinter 非线程安全，
        # 在钩子线程里 root.after() 会静默失效——这正是气泡曾经不出现的原因）
        self._hover = {"armed": False, "due": 0.0, "xy": (0, 0)}
        self.last = {"text": "", "at": 0.0}
        self._protect_notice_at = 0.0   # 受保护程序的托盘提示节流时间戳
        self.bubble = None          # 类型：BubbleUI；启动后才赋值
        self.bubble_ok = False
        # ── 关面板询问 / 后台托盘 ──
        self.tray = None            # 类型：TrayIcon；用户选了"后台运行"才创建
        self._panel_seen = False    # 面板窗口是否出现过（没出现过就不该弹询问框）
        self._asking = False        # 询问框正开着
        self._gate_prompt_at = 0.0  # 上次弹出接入向导的时间（防刷屏）
        self._main_tid = 0          # 主线程 ID（消息循环所在线程，退出时要给它发 WM_QUIT）

    # ── 触发入口 ──

    def trigger(self, kind: str, mode: str = "analyze") -> None:
        self._pending.append((kind, mode))
        self._event.set()

    def _worker(self) -> None:
        while not self._stop:
            # 0.05s 粒度：既能及时响应悬停计时，开销也可忽略
            self._event.wait(0.05)
            self._event.clear()
            self._check_hover()
            while self._pending:
                kind, mode = self._pending.pop(0)
                try:
                    self._handle(kind, mode)
                except Exception as exc:
                    log("处理触发失败（%s）：%s" % (kind, exc))

    def _check_hover(self) -> None:
        """worker 线程里检查悬停计时是否到点（唯一合法的计时位置）。"""
        h = self._hover
        if not h["armed"] or time.time() < h["due"]:
            return
        h["armed"] = False
        log("悬停计时到点，触发气泡")
        self.trigger("hover")

    def _handle(self, kind, mode="analyze") -> None:
        if not self._busy.acquire(blocking=False):
            return
        try:
            # 敏感窗口（密码管理器 / 网银 / 凭据界面）一律不取词：
            # 热键与悬停气泡两条路径都从这里过，所以卡在这里最保险
            guard_hwnd = int(foreground_hwnd() or 0)
            reason = protected_hit(guard_hwnd, self.cfg)
            if reason:
                self._note_protected(guard_hwnd, reason)
                return

            title = foreground_window_title()

            # 接入门：本机没有自己的钥匙 → 工具整体不工作。
            # 模型通道属于「应用」，本工具不自带公共额度，放过去就等于花发布者的额度。
            if not is_provisioned():
                log("未接入：本次触发被拦下（本工具不提供公共 AI 额度），弹出接入向导")
                self.open_gate()
                return

            if kind == "hover" and self.bubble and self.bubble.ok:
                # 悬停气泡路径：只弹气泡，不在这里抓文本
                # （主线程里发 Ctrl+C 会阻塞消息循环，且松手瞬间剪贴板未必就绪）
                x, y = self._drag_state["up_xy"]
                payload = {
                    "text": "",
                    "title": title,
                    "url": "",
                    "hwnd": int(foreground_hwnd() or 0),
                    "x": x,
                    "y": y,
                }
                self.bubble.show(x, y, payload)
                return
            if kind == "hover":
                log("气泡未启用，本次划选降级为直接解析")

            text = self._capture_via_ctrl_c()

            text = (text or "").strip()
            if text:
                if len(text) > 200000:
                    text = text[:200000]
                now = time.time()
                if text == self.last["text"] and now - self.last["at"] < 1.5:
                    ensure_panel()
                    return
                self.last = {"text": text, "at": now}
                seq = self.bridge.push(text, title, "")
                log("已取到选区 #%d（%d 字，来自「%s」）"
                    % (seq, len(text), short_title(title, self.cfg)))

                ensure_panel()
                payload = {"text": text, "title": title, "url": "", "mode": mode}
                if not push_via_cdp_with_mode(payload):
                    open_with_text(payload)
            else:
                log("没有取到选中文字，只唤起面板")
                ensure_panel()
        finally:
            self._busy.release()

    def _dismiss_bubble_for_capture(self) -> None:
        """气泡开着时会抢走 Ctrl+C（它是前台 topmost 窗口），先收起来并把焦点还给原窗口。

        注意：hide() 是走队列的异步操作，必须等 visible 变 False 再抓取，
        否则复制命令还是发给还没消失的气泡。
        """
        if not self.bubble or not self.bubble.visible:
            return
        payload = getattr(self.bubble, "_payload", None) or {}
        hwnd = int(payload.get("hwnd") or 0)
        self.bubble.hide()
        deadline = time.time() + 0.4
        while self.bubble.visible and time.time() < deadline:
            time.sleep(0.02)
        if hwnd:
            force_foreground(hwnd)
            time.sleep(0.12)

    def _wait_hotkey_released(self, timeout: float = 1.5) -> None:
        """等用户把热键的按键松开，再发 Ctrl+C。

        热键在按键按下的瞬间就触发了，此时用户的键还按着；
        立刻合成 Ctrl+C 会和还按着的键混在一起，目标软件收不到完整的复制命令
        （复现过：拖选后按 Alt+Q，钩子只看到 C 键、Ctrl 按下被吞，剪贴板纹丝不动）。
        """
        spec = (self.cfg.get("hotkey") or DEFAULT_HOTKEY).lower()
        wait_vks = []
        for p in (part.strip() for part in spec.split("+")):
            if p in ("alt", "menu"):
                wait_vks.append(VK_MENU)
            elif p in ("ctrl", "control"):
                wait_vks.append(VK_CONTROL)
            elif p == "shift":
                wait_vks.append(VK_SHIFT)
            elif p in ("win", "super", "meta"):
                wait_vks.append(0x5B)
            elif len(p) == 1:
                wait_vks.append(ord(p.upper()))
        if not wait_vks:
            wait_vks = [VK_MENU, ord("Q")]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in wait_vks):
                return
            time.sleep(0.03)

    WM_COPY = 0x0301
    EDIT_CLASSES = ("Edit", "RichEditD2DPT", "RICHEDIT50W", "RichEdit20W", "Scintilla")

    def _focused_edit_hwnd(self) -> int:
        """前台窗口里「确实有选中内容」的编辑类子窗口（没有则返回 0）。

        沙箱控制台等窗口也带 Edit 子类，光看类名会找错目标，
        所以用 EM_GETSEL 确认控件里真的有选区。
        """
        top = foreground_hwnd()
        if not top:
            return 0
        for cls in self.EDIT_CLASSES:
            child = user32.FindWindowExW(top, 0, cls, None)
            if not child or not user32.IsWindow(child):
                continue
            start = ctypes.c_uint(0)
            end = ctypes.c_uint(0)
            user32.SendMessageW(child, 0x00B0, ctypes.byref(start), ctypes.byref(end))
            if end.value > start.value:
                return int(child)
        return 0

    def _capture_via_wm_copy(self) -> bool:
        """给前台编辑控件发 WM_COPY，不走键盘合成。

        合成按键会被热键抑制逻辑/钩子延迟吃掉（复现过 Ctrl 按下被吞、
        剪贴板纹丝不动），WM_COPY 是最可靠的通道；浏览器等自定义渲染的
        界面没有编辑类子窗口，会返回 False 走回退方案。
        """
        child = self._focused_edit_hwnd()
        if not child:
            log("抓取：前台窗口「%s」没有带选区的编辑控件"
                % short_title(foreground_window_title(), self.cfg))
            return False
        before = clipboard_seq()
        try:
            user32.SendMessageW(child, self.WM_COPY, 0, 0)
        except Exception as exc:
            log("WM_COPY 发送失败：%s" % exc)
            return False
        deadline = time.time() + 0.5
        while time.time() < deadline:
            if clipboard_seq() != before:
                return True
            time.sleep(0.03)
        log("抓取：WM_COPY 发了但剪贴板没变化（控件=%s）" % child)
        return False

    def _hotkey_off(self) -> None:
        """请主线程注销热键（抓取期间避免按键抑制逻辑吃掉合成修饰键）。"""
        if not getattr(self, "_main_tid", 0):
            return
        user32.PostThreadMessageW(self._main_tid, WM_APP_HOTKEY_OFF, 0, 0)
        time.sleep(0.08)

    def _hotkey_on(self) -> None:
        """请主线程重新注册热键。"""
        if not getattr(self, "_main_tid", 0):
            return
        user32.PostThreadMessageW(self._main_tid, WM_APP_HOTKEY_ON, 0, 0)
        time.sleep(0.08)

    def _capture_via_ctrl_c(self) -> str:
        # 气泡正开着时它是前台窗口，Ctrl+C 会发给气泡而不是原软件
        self._dismiss_bubble_for_capture()
        # 等热键按键松开，否则复制命令会和还按着的键混在一起
        self._wait_hotkey_released()

        before_text = read_clipboard_text()
        before_seq = clipboard_seq()
        before_formats = clipboard_format_count()

        # 优先 WM_COPY（不合成按键，最可靠）
        ok = self._capture_via_wm_copy()
        if ok:
            log("抓取：WM_COPY 通道成功")

        if not ok:
            # 回退：合成 Ctrl+C。先注销热键——热键的按键抑制逻辑会把
            # 助手自己补发的修饰键吃掉，只剩 C 键，目标软件收不到复制命令。
            log("抓取：WM_COPY 不可用，回退到合成 Ctrl+C")
            self._hotkey_off()
            try:
                release_modifiers()
                time.sleep(0.04)
                send_ctrl_c()
                deadline = time.time() + 0.7
                while time.time() < deadline:
                    time.sleep(0.04)
                    if clipboard_seq() != before_seq:
                        ok = True
                        break
            finally:
                self._hotkey_on()

        text = ""
        if ok:
            time.sleep(0.05)
            text = read_clipboard_text()

        if self.cfg.get("restoreClipboard", True):
            if before_text:
                # 原本就有文本 → 原样还回去
                write_clipboard_text(before_text)
            elif before_formats:
                # 原本是图片/文件这类非文本内容：已经被我们覆盖、无法还原，
                # 那就清空而不是留着划词内容（宁可少一样东西，也不留痕）
                write_clipboard_text("")
                log("隐私：原剪贴板为非文本内容，已清空以避免留下划词痕迹")
            else:
                # 原本就是空的 → 清干净，避免用户随手 Ctrl+V 粘出刚才划的词
                write_clipboard_text("")
        return text

    # ── 全局热键 ──

    def register_hotkey(self, spec: str, hotkey_id: int = 1) -> bool:
        mods = 0
        key = None
        for part in spec.lower().split("+"):
            part = part.strip()
            if part in ("alt", "menu"):
                mods |= MOD_ALT
            elif part in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif part == "shift":
                mods |= MOD_SHIFT
            elif part in ("win", "super", "meta"):
                mods |= MOD_WIN
            elif part:
                key = part

        if not key:
            log("热键配置看不懂：%s" % spec)
            return False

        if len(key) == 1:
            vk = ord(key.upper())
        elif key.startswith("f") and key[1:].isdigit():
            vk = 0x70 + int(key[1:]) - 1
        else:
            mapping = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B}
            vk = mapping.get(key)
            if vk is None:
                log("不支持的热键按键：%s" % key)
                return False

        # 不用 MOD_NOREPEAT：它会抑制热键相关的按键事件，
        # 连助手自己补发的修饰键（release_modifiers 的 Alt up）都会被系统吞掉，
        # 导致随后合成的 Ctrl+C 只剩 C 键，目标软件收不到完整的复制命令。
        if not user32.RegisterHotKey(None, hotkey_id, mods, vk):
            log("热键 %s 注册失败，可能已被别的软件占用" % spec)
            return False
        return True

    # ── 键盘钩子（只用于"开始打字时收起气泡"，不承担任何触发） ──

    def install_keyboard_hook(self) -> bool:
        if not self._hook_proc_ref:
            return False
        try:
            module = kernel32.GetModuleHandleW(None)
            self._hook = user32.SetWindowsHookExW(
                WH_KEYBOARD_LL, self._hook_proc_ref, module, 0
            )
        except Exception as exc:
            log("安装键盘钩子失败：%s" % exc)
            return False

        if not self._hook:
            log("安装键盘钩子失败（返回空句柄）")
            return False
        return True

    # 钩子回调引用必须常驻，否则会被 GC
    _hook_proc_ref = None

    def _hook_callback(self, code, wparam, lparam):
        if code == 0:
            try:
                info = lparam.contents
                vk = int(info.vkCode)
                down = wparam in (WM_KEYDOWN, WM_SYSKEYDOWN)

                if down:
                    if vk in (VK_CONTROL, VK_SHIFT, VK_MENU):
                        pass
                    elif vk == VK_C and (user32.GetAsyncKeyState(VK_CONTROL) & 0x8000):
                        # 划选完顺手 Ctrl+C 复制是很自然的动作，不打断气泡计时
                        pass
                    else:
                        # 敲了别的键说明用户在干别的（打字），这时别再弹气泡
                        self._cancel_hover("敲了键盘")
            except Exception:
                pass
        return user32.CallNextHookEx(self._hook, code, wparam, lparam)

    # ── 鼠标钩子（划选 + 悬停触发气泡） ──

    _mouse_proc_ref = None

    def install_mouse_hook(self) -> bool:
        if not Controller._mouse_proc_ref:
            return False
        try:
            ibeam_handle()          # 预热：别让首次 LoadCursor 发生在钩子回调里
        except Exception:
            pass
        try:
            module = kernel32.GetModuleHandleW(None)
            self._mouse_hook = user32.SetWindowsHookExW(
                WH_MOUSE_LL, Controller._mouse_proc_ref, module, 0
            )
        except Exception as exc:
            log("安装鼠标钩子失败：%s" % exc)
            return False
        if self._mouse_hook:
            log(
                "鼠标钩子：已安装（监测划选 + 悬停）｜划选判定=仅文字光标"
                if self.cfg.get("dragTextOnly", True)
                else "鼠标钩子：已安装（监测划选 + 悬停）｜划选判定=任意拖动"
            )
        return bool(self._mouse_hook)

    def _on_mouse_ll(self, code, wparam, lparam):
        try:
            if code == 0 and lparam:
                info = lparam.contents
                x, y = int(info.pt.x), int(info.pt.y)

                if wparam == WM_LBUTTONDOWN:
                    self._drag_state["down"] = (x, y)
                    # 0) 受保护程序：密码管理器 / 网银 / 系统凭据界面里一律不处理
                    root_hwnd = window_root_at(x, y)
                    reason = protected_hit(root_hwnd, self.cfg)
                    self._drag_state["blocked"] = bool(reason)
                    if reason:
                        self._drag_state["down_text"] = False
                        self._note_protected(root_hwnd, reason)
                        self._cancel_hover("受保护程序")
                        self._hide_bubble_if_clicked_away(x, y)
                    else:
                        # 1) 划选判定只看"按下那一刻"：在文字上按下 → I 型光标 → 是划选；
                        # 拖标题栏 / 滚动条 / 桌面图标 → 箭头或缩放光标 → 不是划选。
                        # （松手那一刻光标会变成箭头，所以不能等到松手再看）
                        if self.cfg.get("dragTextOnly", True):
                            self._drag_state["down_text"] = cursor_is_text()
                            if not self._drag_state["down_text"]:
                                # 浏览器特性：光标悬停在"已选中的文字"上是箭头/手型。
                                # 若这次按下落在上一次选区的屏幕范围内（同一窗口、
                                # 2 分钟内），仍按划选处理——否则连续两次划选同一段
                                # 文字时第二次永远不弹气泡。
                                ls = self._last_sel
                                if (
                                    ls["rect"]
                                    and time.time() - ls["at"] < 120
                                    and ls["root"] == root_hwnd
                                    and ls["rect"][0] - 8 <= x <= ls["rect"][2] + 8
                                    and ls["rect"][1] - 8 <= y <= ls["rect"][3] + 8
                                ):
                                    self._drag_state["down_text"] = True
                                    log("  按下点在上次选区内，按划选处理")
                        else:
                            self._drag_state["down_text"] = True
                        # 只有"又按下鼠标"才算用户走开了；单纯移动光标不算
                        self._cancel_hover("开始了新的点击")
                        self._hide_bubble_if_clicked_away(x, y)
                elif wparam == WM_LBUTTONUP:
                    if self._drag_state.get("blocked"):
                        # 按下时就在受保护程序里：松手也什么都不做
                        # （只留一行日志，不记窗口标题、不记文字）
                        self._drag_state["down"] = None
                        self._drag_state["blocked"] = False
                        self._drag_state["down_text"] = False
                        self._drag_state["up_at"] = time.time()
                        self._drag_state["up_xy"] = (x, y)
                        log("  已跳过：受保护程序，未取词")
                    else:
                        down = self._drag_state.get("down")
                        moved = False
                        if down:
                            dx = x - down[0]
                            dy = y - down[1]
                            # 用 DRAG_MIN_PX 判定：双击选词、手抖单击都不会误触
                            if (dx * dx + dy * dy) >= DRAG_MIN_PX * DRAG_MIN_PX:
                                moved = True
                        self._drag_state["down"] = None
                        self._drag_state["up_at"] = time.time()
                        self._drag_state["up_xy"] = (x, y)
                        down_text = bool(self._drag_state.get("down_text"))
                        up_text = cursor_is_text()
                        is_text_drag = down_text or up_text
                        log(
                            "划选结束 @%d,%d（起点=%s，移动=%s，文字光标=%s/%s，气泡=%s）"
                            % (x, y, down, moved, down_text, up_text, self.bubble_ok)
                        )
                        if moved:
                            if not is_text_drag:
                                log("  忽略：不是从文字上开始的拖动（拖窗口/滚动条/图标）")
                            else:
                                # 记录本次选区屏幕范围，供"对选中文字再划选"补偿用
                                if down:
                                    self._last_sel = {
                                        "root": window_root_at(x, y),
                                        "rect": (
                                            min(down[0], x),
                                            min(down[1], y),
                                            max(down[0], x),
                                            max(down[1], y),
                                        ),
                                        "at": time.time(),
                                    }
                                if self.bubble_ok:
                                    self._arm_hover()
                                else:
                                    log("  判定为划选，但气泡未启用")
                elif wparam == WM_MOUSEMOVE:
                    if self._drag_state.get("down") is not None:
                        self._drag_state["up_xy"] = (x, y)
                    # 松手之后的移动不再取消气泡：手会自然滑动，取消就等于永远弹不出来
        except Exception as exc:
            log("鼠标钩子回调异常：%s" % exc)
        if self._mouse_hook:
            return user32.CallNextHookEx(self._mouse_hook, code, wparam, lparam)
        return 0

    # ── 悬停计时（worker 线程驱动，不碰 Tk） ──

    def _arm_hover(self) -> None:
        x, y = self._drag_state["up_xy"]
        self._hover.update(
            {
                "armed": True,
                "due": time.time() + HOVER_DELAY_MS / 1000.0,
                "xy": (x, y),
            }
        )
        log("悬停计时已启动（%dms 后出气泡）" % HOVER_DELAY_MS)

    def _cancel_hover(self, why: str) -> None:
        """取消还没到点的悬停计时。只有"新点击 / 敲键盘"这类明确信号才取消。"""
        h = self._hover
        if not h["armed"]:
            return
        h["armed"] = False
        log("悬停计时已取消（%s）" % why)

    def _hide_bubble_if_clicked_away(self, x: int, y: int) -> None:
        """气泡正显示着，用户却在别处按下鼠标 → 顺手收起它。

        点在气泡自身范围内（含 20px 余量）时绝不动它——否则点击按钮的
        mouse-down 会先把气泡隐藏，按钮就永远点不中了。
        """
        b = self.bubble
        if b is None:
            return
        rect = getattr(b, "rect", None)
        if not rect or not getattr(b, "visible", False):
            return
        bx, by, bw, bh = rect
        m = BUBBLE_HIT_MARGIN
        if bx - m <= x <= bx + bw + m and by - m <= y <= by + bh + m:
            return          # 点在气泡上，放行（交给 Tk 处理按钮点击）
        b.hide()
        log("点击在气泡之外，已收起气泡")

    # ── 气泡选单回调 ──

    def _note_protected(self, hwnd: int, reason: str) -> None:
        """受保护程序命中：只记程序名和规则，不记标题、不记文字。

        同时给一次托盘提示（同一个程序 60 秒内只提示一次），否则用户会以为
        "气泡坏了"——这正是之前踩过的坑（静默失败最招人烦）。
        """
        name = process_name_of(hwnd) or "未知程序"
        log("已跳过：受保护程序（%s｜%s）" % (name, reason))
        if self.tray is None or not getattr(self.tray, "ok", False):
            return
        now = time.time()
        if now - self._protect_notice_at < 60:
            return
        self._protect_notice_at = now
        try:
            self.tray.notify(
                "%s：已保护" % APP_NAME,
                "「%s」属于敏感程序，助手不会读取这里的文字。" % name,
            )
        except Exception:
            pass

    def _on_bubble_pick(self, mode: str, payload: dict) -> None:
        log("气泡点击：%s" % mode)
        hwnd = int(payload.get("hwnd") or 0)
        title = payload.get("title") or ""
        reason = protected_hit(hwnd, self.cfg)
        if reason:
            # 气泡理论上不该在敏感窗口出现；万一出现了（比如判定的窗口换了），
            # 点下去也不能取词
            self._note_protected(hwnd, reason)
            return
        if hwnd:
            if force_foreground(hwnd):
                log("焦点已切回原窗口「%s」" % short_title(title, self.cfg))
            else:
                log("警告：焦点没能切回原窗口，可能抓不到文字")
            time.sleep(0.15)

        text = self._capture_via_ctrl_c()
        text = (text or "").strip()
        if not text:
            log("没抓到文字（可能选区已失效或焦点切不过来）。改用 URL 方式打开面板。")
            open_with_text({"text": "（1z read：自动捕获失败，请手动把文字粘贴到面板）",
                            "title": title, "url": "", "mode": mode})
            ensure_panel()
            return

        if len(text) > 200000:
            text = text[:200000]
        now = time.time()
        self.last = {"text": text, "at": now}
        self.bridge.push(text, title, "")
        ensure_panel()
        full = {"text": text, "title": title, "url": payload.get("url") or "", "mode": mode}
        if push_via_cdp_with_mode(full):
            log("已按「%s」推送到面板（%d 字）" % (mode, len(text)))
        else:
            log("调试通道不可用，改用链接方式打开")
            open_with_text(full)

    # ── 关面板询问：关闭 / 后台运行 ──

    def _panel_close_monitor(self) -> None:
        """盯着面板窗口：用户点了 × 把它关掉之后，弹出「关闭 / 后台运行」询问。

        面板是 Edge 的应用窗口，它的 × 由 Chromium 处理，进程内拦不到，
        所以只能"等窗口消失"再问。
        """
        # 面板从没出现过（比如启动失败）时不该弹询问框，先等它第一次出现
        deadline = time.time() + 120
        while not self._stop and time.time() < deadline:
            if find_panel_window():
                self._panel_seen = True
                log("关闭监测：面板已就绪（关掉窗口时会弹询问框）")
                break
            time.sleep(0.6)

        while not self._stop:
            time.sleep(0.6)
            try:
                hwnd = find_panel_window()
            except Exception:
                continue
            if hwnd:
                if not self._panel_seen:
                    self._panel_seen = True      # 面板又被打开了，重新武装
                continue
            if not self._panel_seen:
                continue
            self._panel_seen = False
            log("关闭监测：面板窗口没了（用户点了 ×）")
            try:
                self._on_panel_closed()
            except Exception as exc:
                log("处理面板关闭失败：%s" % exc)

    def _on_panel_closed(self) -> None:
        if self._stop or self._asking:
            return
        cfg = load_config()
        if not cfg.get("closeAsk", True):
            action = cfg.get("closeAction") or "background"
            log("已勾选「下次不再询问」，直接执行记住的选择：%s" % action)
            self._apply_close_action(action)
            return

        self._asking = True
        log("弹出关闭询问框（后台运行 / 关闭 %s）" % APP_NAME)

        def done(action: str, remember: bool) -> None:
            self._asking = False
            if remember:
                c = load_config()
                c["closeAsk"] = False
                c["closeAction"] = action
                save_config(c)
                log("已记住关闭选择：%s（下次不再询问）" % action)
            self._apply_close_action(action)

        if self.bubble_ok and self.bubble is not None:
            self.bubble.ask_close(done)
        else:
            self._ask_via_messagebox(done)

    def _ask_via_messagebox(self, done) -> None:
        """没有 tkinter 时的兜底：系统 MessageBox（是=后台运行，否=关闭）。"""

        def worker():
            MB_YESNOCANCEL = 0x0003
            MB_ICONQUESTION = 0x0020
            MB_SETFOREGROUND = 0x00010000
            MB_TOPMOST = 0x00040000
            text = ("要关闭 %s 吗？\n\n是：后台运行（只留托盘图标）\n"
                    "否：完全关闭\n取消：继续开着" % APP_NAME)
            try:
                ret = user32.MessageBoxW(
                    None, text, APP_NAME,
                    MB_YESNOCANCEL | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST,
                )
            except Exception as exc:
                log("MessageBox 失败：%s" % exc)
                ret = 2
            action = {6: "background", 7: "quit", 2: "cancel"}.get(int(ret), "background")
            done(action, False)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_close_action(self, action: str) -> None:
        if action == "quit":
            self.request_quit("关闭面板时选择：完全关闭")
            return
        if action == "cancel":
            log("用户取消关闭，重新打开面板")
            threading.Thread(target=ensure_panel, daemon=True).start()
            return
        log("用户选择：后台运行（窗口关闭，只在托盘保留图标）")
        self.ensure_tray(notify=True)

    # ── 接入门：本机没有自己的钥匙时，工具整体不工作 ──

    def open_gate(self, force: bool = False) -> None:
        """弹出接入向导。未接入时这是唯一的入口。"""
        now = time.time()
        if not force and now - self._gate_prompt_at < 8.0:
            return
        self._gate_prompt_at = now

        if self.bubble is not None and self.bubble.ok:
            self.bubble.ask_gate(self._on_gate_result, gate_prefill())
            return
        self._gate_fallback_notice()

    def _gate_fallback_notice(self) -> None:
        """没有 tkinter 时的兜底：系统提示 + 命令行指引（绝不静默失败）。"""
        def run():
            cmd = (
                '"%s" --setup' % os.path.abspath(sys.executable)
                if IS_FROZEN
                else "python lens.py --setup"
            )
            text = (
                "%s 还没有接入你自己的 AI 额度，划词暂不可用。\n\n"
                "面板不带钥匙，钥匙只存在本机，所以每个人用的都是自己那份应用。\n\n"
                "接入方式（二选一）：\n"
                "  · 右键托盘图标 →「接入设置」\n"
                "  · 命令行：%s\n\n"
                "还没有 WorkBuddy 账号？先到 %s 登录或注册。\n"
                "（详见同目录「接入说明.txt」）"
            ) % (APP_NAME, cmd, WORKBUDDY_SIGNUP_URL)
            try:
                user32.MessageBoxW(None, text, "%s · 首次接入" % APP_NAME, 0x40)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _on_gate_result(self, ok: bool) -> None:
        """向导关掉之后（后台线程）：成功就同步扩展并打开面板。"""
        if not ok:
            return
        try:
            _sync_extension()
        except Exception as exc:
            log("接入后同步扩展失败：%s" % exc)
        log("接入完成，面板地址：%s" % panel_url())
        try:
            ensure_panel()
        except Exception as exc:
            log("接入后打开面板失败：%s" % exc)

    def ensure_tray(self, notify: bool = False) -> None:
        """创建 / 复用托盘图标（通知区域，默认落在「隐藏的图标」里）。"""
        tip = "划词后仍会弹出功能气泡；右键托盘图标可打开面板或退出。"

        if self.tray is not None and self.tray.ok:
            if notify:
                self.tray.notify("%s 正在后台运行" % APP_NAME, tip)
            return

        self.tray = TrayIcon(
            on_open=self._tray_open_panel,
            on_quit=lambda: self.request_quit("托盘菜单：退出"),
            on_setup=lambda: self.open_gate(force=True),
        )
        if self.tray.start():
            log("托盘图标已创建（通知区域 / 隐藏的图标里）")
            if notify:
                self.tray.notify("%s 正在后台运行" % APP_NAME, tip)
        else:
            log("托盘图标创建失败")

    def _tray_open_panel(self) -> None:
        log("托盘：打开面板")
        if not is_provisioned():
            # 面板打不开，改成把接入向导摆出来（总得给条路走）
            self.open_gate(force=True)
            return
        try:
            ensure_panel()
        except Exception as exc:
            log("从托盘打开面板失败：%s" % exc)

    def request_quit(self, reason: str = "用户退出") -> None:
        """干净退出：摘托盘图标 → 关面板窗口 → 唤醒主线程消息循环结束。"""
        if self._stop:
            return
        self._stop = True
        self._event.set()
        log("准备退出（%s）" % reason)
        if self.bubble is not None:
            try:
                self.bubble.hide_ask()
                self.bubble.hide_gate()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()      # 内部等托盘线程摘掉图标，避免留"幽灵图标"
            except Exception:
                pass
        if panelwin.backend(load_config()) == "webview":
            panelwin.stop()           # 销毁面板窗口 → 主线程 webview.start 返回
        else:
            hwnd = find_panel_window()
            if hwnd:
                try:
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                except Exception:
                    pass
        # 平台线程正阻塞在 GetMessageW 上，必须给它投 WM_QUIT 才会醒
        if self._main_tid:
            try:
                user32.PostThreadMessageW(self._main_tid, WM_QUIT, 0, 0)
            except Exception as exc:
                log("投递退出消息失败：%s" % exc)

    # ── 面板看门狗：仅启动期纠正，绝不干扰用户 ──

    def _panel_watchdog(self) -> None:
        """只在"刚拉起面板后的窗口创建期"纠正一次位置。

        面板一旦正常显示过，就完全交给用户——用户最小化就是最小化，
        绝不会被弹回来（旧版每 1.2 秒抢窗口，导致根本无法最小化）。
        """
        heartbeat = 0
        settled = False
        while not self._stop:
            time.sleep(1.2)
            heartbeat += 1
            # 每 30 秒写一次心跳日志——证明 watchdog 活着
            if heartbeat % 25 == 0:
                log("watchdog 心跳 (#%d)" % heartbeat)
            if settled:
                continue
            try:
                # 只有"本进程刚拉起过面板"的 45 秒内才介入
                # （Edge 冷启动可能 10 秒以上才出窗口，留足余量）
                if time.time() - _last_spawn_at > 45:
                    settled = True
                    continue
                hwnd = find_panel_window()
                if not hwnd or not user32.IsWindow(hwnd):
                    continue

                on_screen = True
                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    sw = user32.GetSystemMetrics(0)
                    sh = user32.GetSystemMetrics(1)
                    on_screen = not (
                        rect.right < 50
                        or rect.bottom < 50
                        or rect.left > sw - 50
                        or rect.top > sh - 50
                    )

                if (
                    user32.IsIconic(hwnd)
                    or not user32.IsWindowVisible(hwnd)
                    or not on_screen
                ):
                    _bring_panel_to_front(hwnd)
                    log("面板启动期位置纠正")
                else:
                    settled = True
            except Exception as exc:
                log("watchdog 异常：%s" % exc)

    # ── 生命周期 ──

    def start(self) -> None:
        controller = self
        # 记录主线程 ID：热键的注销/重注册必须回到这个线程做
        self._main_tid = kernel32.GetCurrentThreadId()

        def hook_proc(code, wparam, lparam):
            return controller._hook_callback(code, wparam, lparam)

        Controller._hook_proc_ref = HOOKPROC(hook_proc)

        def mouse_proc(code, wparam, lparam):
            return controller._on_mouse_ll(code, wparam, lparam)

        Controller._mouse_proc_ref = MOUSEHOOKPROC(mouse_proc)

        threading.Thread(target=self._worker, daemon=True).start()
        if panelwin.backend(self.cfg) == "edge":
            # 这两个线程盯的是 Edge 应用窗口（等它消失/纠正位置）。
            # webview 模式下面板是自己的窗口：关闭走事件回调、位置自己管。
            threading.Thread(target=self._panel_watchdog, daemon=True).start()
            threading.Thread(target=self._panel_close_monitor, daemon=True).start()

        # 启动悬停气泡（tkinter 不可用时静默降级）
        try:
            self.bubble = BubbleUI(self._on_bubble_pick)
            if self.bubble.start():
                self.bubble_ok = True
                log("悬停气泡：已启用（在窗口里选中文字 → 松开 → 稍等 → 小气泡出现）")
            else:
                log("悬停气泡：未启用（tkinter 不可用；仍可从桌面图标打开面板）")
        except Exception as exc:
            log("悬停气泡启动失败：%s" % exc)

        # 鼠标钩子：监测划选并把「松手悬停」转成 hover 触发
        if not self.install_mouse_hook():
            log("鼠标钩子：未安装，悬停气泡不可用")

        # 键盘钩子：只在"用户开始打字"时把气泡收起来，不承担任何触发
        if not self.install_keyboard_hook():
            log("键盘钩子：未安装（打字时气泡不会自动收起）")

        # 全局热键：默认关闭，避免与用户日常按键冲突
        if self.cfg.get("hotkeyEnabled", False):
            hotkey = self.cfg.get("hotkey") or DEFAULT_HOTKEY
            if self.register_hotkey(hotkey):
                log("全局热键：%s" % hotkey.upper())
            else:
                log("全局热键：%s 注册失败（可能被别的软件占用）" % hotkey.upper())
        else:
            log("全局热键：已关闭（config.json 里把 hotkeyEnabled 改为 true 可启用）")

    def message_loop(self) -> None:
        msg = wintypes.MSG()
        while not self._stop:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):
                break
            if msg.message == WM_HOTKEY:
                self.trigger("hotkey")
            elif msg.message == WM_APP_HOTKEY_OFF:
                # worker 线程请求：抓取期间先摘掉热键，避免按键抑制吃掉合成修饰键
                user32.UnregisterHotKey(None, 1)
            elif msg.message == WM_APP_HOTKEY_ON:
                if self.cfg.get("hotkeyEnabled", False):
                    spec = self.cfg.get("hotkey") or DEFAULT_HOTKEY
                    if not self.register_hotkey(spec):
                        log("热键 %s 重新注册失败" % spec.upper())
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def shutdown(self) -> None:
        self._stop = True
        self._event.set()
        # 不留驻用户内容：清掉内存里缓存的选区文本与选区记录
        try:
            self.bridge.push("", "", "")
        except Exception:
            pass
        self.last = {"text": "", "at": 0.0}
        self._last_sel = {"root": 0, "rect": None, "at": 0.0}
        if self.bubble is not None:
            try:
                self.bubble.hide_ask()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self._hook:
            try:
                user32.UnhookWindowsHookEx(self._hook)
            except Exception:
                pass
        try:
            user32.UnregisterHotKey(None, 1)
        except Exception:
            pass
        if self._mouse_hook and not self._mouse_uninstalled:
            try:
                user32.UnhookWindowsHookEx(self._mouse_hook)
            except Exception:
                pass
            self._mouse_uninstalled = True


# ──────────────────────────── 单实例 / 自启 ────────────────────────────


def acquire_single_instance():
    handle = kernel32.CreateMutexW(None, False, "TextLens_SingleInstance_v1")
    if handle and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def startup_dir() -> str:
    appdata = os.environ.get("APPDATA") or ""
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")


def launcher_path() -> str:
    return os.path.join(startup_dir(), "1z read.vbs")


def autostart_command() -> list:
    """开机自启要执行的完整命令行。"""
    if IS_FROZEN:
        # 装成独立 exe 后，自启目标就是 exe 本身：无控制台、不依赖解释器。
        return [os.path.abspath(sys.executable)]
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.isfile(pyw):
        pyw = sys.executable
    return [pyw, os.path.abspath(__file__)]


def install_autostart() -> None:
    # 用 VBS 起进程，避免闪出黑框。
    # VBS 字符串里的引号要成对翻倍，写出来是 sh.Run """路径" "参数""" 这种形式。
    args = " ".join('"%s"' % a for a in autostart_command())
    content = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.Run "%s", 0, False\r\n' % args.replace('"', '""')
    )

    target = launcher_path()
    os.makedirs(startup_dir(), exist_ok=True)
    # 必须让 WSH 能认出中文安装路径：UTF-16LE + BOM 是脚本宿主明确支持的
    # Unicode 脚本格式；写成 UTF-8（无 BOM）会被按系统代码页读，中文目录名必坏。
    with open(target, "wb") as fh:
        fh.write(b"\xff\xfe" + content.encode("utf-16-le"))
    # 迁移：清掉旧版名字的自启文件，避免登录时重复拉起
    current = os.path.basename(target)
    for old in ("划词解析助手.vbs", "划词助手.vbs", "易读.vbs"):
        old_path = os.path.join(startup_dir(), old)
        if old != current and os.path.isfile(old_path):
            try:
                os.remove(old_path)
                log("已清理旧自启文件：%s" % old_path)
            except Exception:
                pass
    log("已设置开机自启：%s" % target)


def uninstall_autostart() -> None:
    target = launcher_path()
    if os.path.isfile(target):
        os.remove(target)
        log("已取消开机自启")
    else:
        log("本来就没有设置开机自启")


# ──────────────────────────── CLI ────────────────────────────


def cmd_capture_once() -> int:
    """抓一次当前选区，打印出来（用于排查问题）。"""
    bridge = Bridge()
    ctrl = Controller(bridge, {})
    time.sleep(0.2)
    title = foreground_window_title()
    text = ctrl._capture_via_ctrl_c()
    print("窗口标题：%s" % title)
    print("选中内容：%r" % text)
    return 0 if text.strip() else 1


# ────────────────────── 自助接入（用自己的钥匙） ──────────────────────

SETUP_GUIDE = """\
1z read · 接入你自己的 AI 额度
================================================

本工具**不自带公共 AI 额度**：面板不带云服务钥匙，钥匙只存在本机
（cloud.json），打开面板时由本助手注入。模型通道属于「应用」，所以每个人
必须用**自己那份应用**，用量算在自己头上，不会花到别人的额度。

没有接入之前，划词不会响应（这就是"别人用不了你的额度"的实现方式）。

先把这个项目发布成你自己的应用，拿到属于你的钥匙：

  1) 用 WorkBuddy 打开本项目所在文件夹
  2) 对 AI 说一句：
       「帮我开通云服务并发布这个项目，然后告诉我 publishableKey、
         resourceId 和发布后的网址」
  3) 把拿到的三项填到下面（面板网址必填，resourceId 可留空）

还没有 WorkBuddy 账号？先到 %s 登录或注册。

更细的说明见同目录的「接入说明.txt」。
""" % WORKBUDDY_SIGNUP_URL


def _looks_like_key(value: str) -> bool:
    return value.startswith("wbpk_") and len(value) > 20


def _sync_extension() -> None:
    """尽力把钥匙同步进浏览器扩展；没有那个脚本就跳过。"""
    if IS_FROZEN:
        # exe 形态不带开发用的 tools/ 目录，这一步没有意义
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(root, "tools", "sync_extension_secret.py")
    if not os.path.isfile(script):
        return
    try:
        proc = subprocess.run(  # noqa: S603
            [sys.executable, script],
            capture_output=True,
            text=True,
            encoding=SUBPROC_ENCODING,
            errors="replace",
            timeout=60,
            creationflags=_no_window_flags(),
        )
        last = (proc.stdout or "").strip().splitlines()
        print("扩展同步：%s" % (last[-1] if last else "完成"))
    except Exception as exc:
        print("扩展同步跳过：%s" % exc)


def cmd_setup(panel: str = "", key: str = "", resource: str = "") -> int:
    """交互式（或带参数）写入 cloud.json。"""
    print(SETUP_GUIDE)

    interactive = bool(sys.stdin) and sys.stdin.isatty()

    def ask(prompt: str, preset: str) -> str:
        if preset:
            return preset.strip()
        if not interactive:
            return ""
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""

    if not key and not interactive:
        print("当前不是交互式终端，请用参数形式：")
        print("    python lens.py --setup --panel-url <你的面板网址> "
              "--key <publishableKey> [--resource <resourceId>]")
        return 1

    panel = ask("面板网址（你自己发布后拿到的网址，必填）：", panel)
    key = ask("publishableKey（wbpk_ 开头）：", key)
    resource = ask("resourceId（wbsc_ 开头，可留空）：", resource)

    ok, msg = save_provision(panel, key, resource)
    if not ok:
        print("\n%s" % msg)
        return 1

    print("\n已写入 %s" % msg)
    print("面板地址：%s" % panel_url())
    _sync_extension()
    print("\n完成。接下来：")
    print("  · 双击桌面「%s」，或运行 python lens.py 启动助手" % APP_NAME)
    print("  · 任意软件里选中文字 → 点气泡，面板应当能正常回答")
    print("  · 面板若停在「未连接」，看日志 %s" % LOG_PATH)
    return 0


def cmd_gate() -> int:
    """只弹出接入向导窗口（已接入的话，可用来改配置）。"""
    enable_dpi_awareness()

    ui = BubbleUI(lambda mode, payload: None)
    if not ui.start():
        print("tkinter 不可用，请改用命令行接入：")
        print("    python lens.py --setup --panel-url <网址> --key <publishableKey>")
        return 1

    done = threading.Event()

    def on_result(ok: bool) -> None:
        if ok:
            try:
                _sync_extension()
            except Exception as exc:
                print("扩展同步跳过：%s" % exc)
            print("已完成接入：%s" % CLOUD_SECRET_PATH)
            print("面板地址：%s" % panel_url())
        else:
            print("已取消接入。工具保持未启用状态。")
        done.set()

    ui.ask_gate(on_result, gate_prefill())
    done.wait(900)
    return 0


def _platform_thread(controller) -> None:
    """webview 模式的平台线程：全局钩子 + 热键 + 消息泵。

    低级钩子的回调只能在安装线程上执行，且该线程必须有消息泵；
    pywebview 强制占用主线程跑 GUI 循环，所以把原来主线程的整套职责
    （装钩子 → 托盘/向导 → GetMessage 泵）整体搬到这条线程。
    面板窗口由主线程 precreate，这里不再有任何阻塞等待 ——
    钩子装好后几毫秒内就进入泵，启动空窗期（输入卡顿的根源）随之消失。
    """
    try:
        controller.start()
        if not is_provisioned():
            # 没接入 = 工具不可用。把向导摆到人面前，并在托盘留常驻入口。
            log("未接入：不打开面板，改为弹出接入向导 + 托盘入口")
            try:
                controller.ensure_tray(notify=True)
            except Exception as exc:
                log("托盘创建失败：%s" % exc)
            try:
                controller.open_gate(force=True)
            except Exception as exc:
                log("弹出接入向导失败：%s" % exc)
        log("平台线程：钩子/热键就绪，进入消息泵")
        controller.message_loop()
    except Exception as exc:
        log("平台线程异常：%s" % exc)
        import traceback
        log(traceback.format_exc())


def cmd_selftest() -> int:
    """打包自检：验证面板运行库在**当前形态**（尤其冻结后的 exe）里真能用。

    只做「能不能把后端 import 起来」这一步，不建窗口、不碰托盘、不弹气泡。
    这一步恰好是打包最容易漏的地方：pywebview 的后端是运行期按平台动态
    import 的（webview/guilib.py），而 pythonnet 在 import 时就要真的把
    .NET 程序集加载起来 —— 缺一个 DLL 就整条面板链路失效。
    输出故意只用 ASCII，省得 frozen 进程的管道按 cp936 解码时花掉。
    安装包自检（tools/check_release.py）靠这一行判断打包是否成功。
    """
    def ascii_text(value) -> str:
        return str(value).encode("ascii", "replace").decode("ascii")

    fields = [
        ("frozen", int(IS_FROZEN)),
        ("backend", panelwin.backend(load_config())),
    ]

    try:
        import webview as _webview_pkg  # noqa: PLC0415
        from webview._version import __version__ as _webview_version  # noqa: PLC0415
        _ = _webview_pkg
        fields.append(("pywebview", ascii_text(_webview_version)))
    except Exception as exc:  # noqa: BLE001
        fields.append(("pywebview", "FAIL:%s" % ascii_text(exc)))

    try:
        import clr  # noqa: F401, PLC0415
        fields.append(("pythonnet", "ok"))
    except Exception as exc:  # noqa: BLE001
        fields.append(("pythonnet", "FAIL:%s" % ascii_text(exc)))

    # 真正的一票：winforms 后端 import 时会 clr.AddReference 并加载
    # Microsoft.Web.WebView2.* 程序集，成功即说明 DLL 都到位了。
    try:
        import webview.platforms.winforms  # noqa: F401, PLC0415
        fields.append(("winforms", "ok"))
    except Exception as exc:  # noqa: BLE001
        fields.append(("winforms", "FAIL:%s" % ascii_text(exc)))

    ok = all(value == "ok" for key, value in fields
             if key in ("pythonnet", "winforms"))
    fields.append(("result", "ok" if ok else "fail"))
    line = "SELFTEST " + " ".join("%s=%s" % item for item in fields)
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001  windowed 且没有重定向时 stdout 可能是 None
        pass
    return 0 if ok else 1


def main() -> int:
    # 第一件事：声明 DPI 感知，必须在创建任何窗口（Tk 气泡）之前
    enable_dpi_awareness()
    # 第二件：声明应用身份，任务栏才会按「1z read」而不是 pythonw.exe 归类
    set_app_user_model_id()

    # 顺手清掉上次被强杀时可能残留的推送临时文件（含钥匙/选区明文）
    cleanup_stale_pushes()

    parser = argparse.ArgumentParser(description="%s 桌面助手" % APP_NAME)
    parser.add_argument("--install", action="store_true", help="设置开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    parser.add_argument("--capture", action="store_true", help="抓取一次当前选区并打印")
    parser.add_argument("--hotkey", type=str, default=None, help="启用并覆盖全局热键，如 alt+q")
    parser.add_argument("--open", action="store_true", help="只打开面板窗口")
    parser.add_argument("--setup", action="store_true",
                        help="接入自己的云服务钥匙（首次使用，交互式）")
    parser.add_argument("--gate", action="store_true",
                        help="只弹出「接入你自己的 AI 额度」向导窗口")
    parser.add_argument("--panel-url", type=str, default="",
                        help="配合 --setup：自己发布的面板网址")
    parser.add_argument("--key", type=str, default="",
                        help="配合 --setup：publishableKey")
    parser.add_argument("--resource", type=str, default="",
                        help="配合 --setup：resourceId（可留空）")
    parser.add_argument("--selftest", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.selftest:
        return cmd_selftest()

    if args.setup:
        return cmd_setup(args.panel_url, args.key, args.resource)

    if args.gate:
        return cmd_gate()

    if args.install:
        install_autostart()
        return 0
    if args.uninstall:
        uninstall_autostart()
        return 0
    if args.capture:
        return cmd_capture_once()
    if args.open:
        ensure_panel()
        return 0

    guard = acquire_single_instance()
    if guard is None:
        log("已经在运行了，第二次启动自动退出")
        if is_provisioned():
            ensure_panel()
        else:
            # 没接入时实例里没有面板也没有托盘，双击图标必须给条活路
            log("尚未接入：这次启动改为弹出接入向导")
            cmd_gate()
        return 0

    cfg = load_config()
    if args.hotkey:
        cfg["hotkey"] = args.hotkey
        cfg["hotkeyEnabled"] = True

    bridge = Bridge()
    port = None
    if cfg.get("bridgeHttp", False):
        try:
            port = bridge.start(cfg.get("port"))
        except Exception as exc:
            log("桥接服务启动失败：%s" % exc)
            port = None
    cfg["port"] = port
    save_config(cfg)

    log("=" * 58)
    log("%s 桌面助手已启动" % APP_NAME)
    if port:
        log("本地桥接 HTTP：http://127.0.0.1:%d（已手动开启）" % port)
    else:
        log("本地桥接 HTTP：已停用（选区走调试通道直推；开着会让任意网页"
            "从 127.0.0.1 读走你上一次选中的内容）")
    log("隐私：受保护程序=%s｜剪贴板还原=%s｜日志记标题=%s"
        % (
            "开" if cfg.get("protectApps", True) else "关",
            "开" if cfg.get("restoreClipboard", True) else "关",
            "开" if cfg.get("logTitles", True) else "关",
        ))
    if is_provisioned():
        log("额度保护：网页不含云服务钥匙，面板由本机注入解锁（cloud.json 已就绪）")
    else:
        log("未接入：本工具不自带公共 AI 额度，接入前划词不会响应")
        log("已弹出接入向导；也可右键托盘图标选「接入设置」，"
            "或运行 python lens.py --setup")
    # 中文乱码排查用：管道编码已写死 UTF-8，这里只报告解释器自身的模式。
    # utf8_mode=False 说明这个进程没开 UTF-8 模式（双击启动通常是 False），
    # 属于正常情况 —— 只要这一行打印出来，推送链路的编码就是对的。
    log("编码：管道=UTF-8（显式指定）｜解释器 utf8_mode=%s｜系统默认=%s"
        % (bool(getattr(sys.flags, "utf8_mode", False)),
           locale.getpreferredencoding(False)))
    log("面板：%s｜地址：%s" % (
        "WebView2 独立窗口" if panelwin.backend(cfg) == "webview"
        else "Edge 应用窗口（兜底）",
        panel_url()))
    extra_apps = cfg.get("excludedApps") or []
    if extra_apps:
        log("额外保护的程序：%s" % ", ".join(str(a) for a in extra_apps))
    log("用法：任意软件里选中文字 → 松开鼠标稍等 → 点气泡里的 AI 搜索/解析")
    if cfg.get("hotkeyEnabled", False):
        log("备用：全局热键 %s 直接解析"
            % (cfg.get("hotkey") or DEFAULT_HOTKEY).upper())
    log("日志：%s" % LOG_PATH)
    log("按 Ctrl+C 退出")
    log("=" * 58)

    controller = Controller(bridge, cfg)
    use_webview = panelwin.backend(cfg) == "webview"

    if use_webview:
        # ── 新路径：主线程跑 WebView2 面板 GUI，钩子/热键/托盘在平台线程 ──
        panelwin.configure(log=log, app_dir=APP_DIR,
                           icon_path=os.path.join(resource_dir(), "app.ico"))
        panelwin.set_close_request_handler(controller._on_panel_closed)
        threading.Thread(target=_platform_thread, args=(controller,),
                         daemon=True, name="platform").start()
        if is_provisioned():
            panelwin.precreate(panel_url())
            # 页面加载完成后注入钥匙（webview 路径没有 ensure_panel 里的那一次）
            _unlock_soon(2.0)
        log("主线程：进入面板 GUI 循环（WebView2 独立窗口）")
        try:
            panelwin.run_gui()
        except Exception as exc:
            log("webview GUI 异常：%s" % exc)
            import traceback
            log(traceback.format_exc())
        finally:
            controller.shutdown()
            bridge.stop()
            log("已退出")
        return 0

    # ── 兜底路径（无 WebView2 Runtime）：老架构，主线程跑钩子泵 ──
    controller.start()

    if is_provisioned():
        try:
            ensure_panel()
        except Exception as exc:
            log("ensure_panel 异常: %s" % exc)
            import traceback
            log(traceback.format_exc())
    else:
        # 没接入 = 工具不可用。不打开那份"打不开"的面板，
        # 把向导摆到人面前，并在托盘留一个常驻入口。
        log("未接入：不打开面板，改为弹出接入向导 + 托盘入口")
        try:
            controller.ensure_tray(notify=True)
        except Exception as exc:
            log("托盘创建失败：%s" % exc)
        try:
            controller.open_gate(force=True)
        except Exception as exc:
            log("弹出接入向导失败：%s" % exc)

    log("进入消息循环（线程已起，watchdog/钩子/bubble 在后台）")
    try:
        controller.message_loop()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log("message_loop 异常: %s" % exc)
        import traceback
        log(traceback.format_exc())
    finally:
        controller.shutdown()
        bridge.stop()
        log("已退出")

    return 0


if __name__ == "__main__":
    sys.exit(main())
