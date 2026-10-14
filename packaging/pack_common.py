#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1z read 安装 / 卸载程序共用逻辑。

只用标准库：
* 快捷方式 —— 由 cscript 执行一段**纯 ASCII** 的 VBS（WScript.Shell），
  路径通过命令行参数传入（Windows 命令行本身是 Unicode，中文路径不会坏）。
* 开机自启 —— 往启动目录写一个 UTF-16LE + BOM 的 .vbs（WSH 认得的 Unicode 脚本格式）。
* 注册表 —— winreg（HKCU，"应用和功能"里的卸载项，不需要管理员）。
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

# ──────────────────────────── 常量 ────────────────────────────

APP_NAME = "1z read"
APP_EXE = "1z read.exe"
UNINSTALLER_NAME = "卸载程序.exe"
INSTALLER_NAME = "1z read 安装程序.exe"
VERSION = "1.0.0"
PUBLISHER = "1z read"

UNINSTALL_REG = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\1z read"
SHELL_FOLDERS = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"

AUTOSTART_VBS_NAME = "1z read.vbs"
NODE_MIN_MAJOR = 22
NODE_DOWNLOAD = "https://nodejs.org/zh-cn/download"
WORKBUDDY_URL = "https://www.workbuddy.cn/"

NO_WINDOW = 0x08000000          # CREATE_NO_WINDOW
DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]


# ──────────────────────────── 基础 ────────────────────────────

def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    """内嵌载荷所在目录（exe 解包目录 / 源码目录）。"""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def payload_dir() -> str:
    """安装载荷：app/（助手本体）、web/、extension/、docs/、卸载程序.exe。"""
    if is_frozen():
        return os.path.join(resource_dir(), "payload")
    # 源码直跑（调试用）：用 build/stage 里预排好的同一套结构
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build", "stage")
    )


def enable_dpi_awareness() -> None:
    """让向导窗口在高分屏上不发虚（必须在建窗口前调用）。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def run(cmd, timeout=60, **kw):
    """跑一个隐藏窗口的子进程。"""
    kw.setdefault("creationflags", NO_WINDOW)
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", timeout)
    try:
        return subprocess.run(cmd, **kw)
    except Exception as exc:  # noqa: BLE001
        class _R:
            returncode = -1
            stdout = b""
            stderr = str(exc).encode("utf-8", "replace")
        return _R()


def run_detached(exe: str, args=()) -> None:
    """脱离本进程启动（不随向导退出而被回收）。"""
    cmd = [exe, *args]
    try:
        subprocess.Popen(cmd, creationflags=DETACHED, close_fds=True)
    except Exception:
        subprocess.Popen(["cmd", "/c", "start", "", os.path.abspath(exe), *args],
                         creationflags=NO_WINDOW, close_fds=True)


# ──────────────────────────── 系统目录 ────────────────────────────

# 目录覆盖：自检脚本（tools/check_installer.py）用它把安装、快捷方式、自启全部
# 限制在临时目录里，不去动真实的桌面和启动项；受管部署也可以拿它改默认位置。
DIR_OVERRIDES = {
    "desktop": "1ZREAD_DIR_DESKTOP",
    "programs": "1ZREAD_DIR_PROGRAMS",
    "startup": "1ZREAD_DIR_STARTUP",
    "data": "1ZREAD_DIR_DATA",
    "install": "1ZREAD_DIR_INSTALL",
}


def _override(key: str):
    value = (os.environ.get(DIR_OVERRIDES[key]) or "").strip()
    return value or None


def registry_disabled() -> bool:
    """置 1ZREAD_NO_REGISTRY=1 时不动注册表（便携 / 受管部署、自动化测试用）。"""
    return bool((os.environ.get("1ZREAD_NO_REGISTRY") or "").strip()
                not in ("", "0", "false", "False"))


def _shell_folder(name: str, fallback: str) -> str:
    """从注册表读系统的真实目录（桌面可能被 OneDrive 重定向）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SHELL_FOLDERS) as key:
            value, _ = winreg.QueryValueEx(key, name)
            if value and os.path.isdir(value):
                return value
    except Exception:
        pass
    return os.path.join(os.environ.get("USERPROFILE", ""), fallback)


def desktop_dir() -> str:
    return _override("desktop") or _shell_folder("Desktop", "Desktop")


def programs_dir() -> str:
    return _override("programs") or _shell_folder(
        "Programs", r"AppData\Roaming\Microsoft\Windows\Start Menu\Programs")


def startup_dir() -> str:
    override = _override("startup")
    if override:
        return override
    appdata = os.environ.get("APPDATA") or ""
    return os.path.join(appdata, r"Microsoft\Windows\Start Menu\Programs\Startup")


def default_install_dir() -> str:
    override = _override("install")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "Programs", APP_NAME)


def data_dir() -> str:
    """运行期数据（面板网址、本机钥匙、日志）。"""
    override = _override("data")
    if override:
        return override
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "TextLens")


# ──────────────────────────── 快捷方式 ────────────────────────────

_SHORTCUT_VBS = (
    'Set sh = CreateObject("WScript.Shell")\r\n'
    'Set lnk = sh.CreateShortcut(WScript.Arguments(0))\r\n'
    'lnk.TargetPath = WScript.Arguments(1)\r\n'
    'lnk.WorkingDirectory = WScript.Arguments(2)\r\n'
    'lnk.IconLocation = WScript.Arguments(3)\r\n'
    'lnk.Description = WScript.Arguments(4)\r\n'
    'lnk.Save\r\n'
)


def create_shortcut(lnk_path: str, target: str, icon: str = "", desc: str = "",
                    workdir: str = "") -> bool:
    """建一个 .lnk。返回是否成功。"""
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    vbs = os.path.join(tempfile.gettempdir(), "_1zread_lnk.vbs")
    try:
        with open(vbs, "w", encoding="ascii", newline="") as fh:
            fh.write(_SHORTCUT_VBS)
        proc = run(["cscript", "//nologo", vbs, lnk_path, target,
                    workdir or os.path.dirname(target),
                    icon or target, desc or APP_NAME])
        return proc.returncode == 0 and os.path.isfile(lnk_path)
    finally:
        try:
            os.remove(vbs)
        except Exception:
            pass


def remove_file(path: str) -> bool:
    try:
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


# ──────────────────────────── 开机自启 ────────────────────────────

def autostart_path() -> str:
    return os.path.join(startup_dir(), AUTOSTART_VBS_NAME)


def write_autostart(target_exe: str) -> bool:
    """写启动项。exe 自身无控制台，直接 Run 即可（窗口模式=0）。"""
    content = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'sh.Run """%s""", 0, False\r\n' % os.path.abspath(target_exe)
    )
    try:
        os.makedirs(startup_dir(), exist_ok=True)
        with open(autostart_path(), "wb") as fh:
            fh.write(b"\xff\xfe" + content.encode("utf-16-le"))
        return True
    except Exception:
        return False


def remove_autostart() -> bool:
    removed = remove_file(autostart_path())
    # 顺手清掉历史版本留下的自启文件
    for old in ("划词解析助手.vbs", "划词助手.vbs", "易读.vbs"):
        removed = remove_file(os.path.join(startup_dir(), old)) or removed
    return removed


def autostart_enabled() -> bool:
    return os.path.isfile(autostart_path())


# ──────────────────────────── 进程 ────────────────────────────

def kill_app(timeout=10) -> bool:
    """结束正在运行的助手。返回是否真的杀掉了东西。"""
    killed = False
    for name in (APP_EXE,):
        proc = run(["taskkill", "/F", "/IM", name, "/T"], timeout=timeout)
        out = (proc.stdout or b"").decode("utf-8", "replace").lower()
        if "success" in out or "成功" in out:
            killed = True
    return killed


# ──────────────────────────── 环境检测 ────────────────────────────

def node_info():
    """返回 (ok, 描述)。要求 Node 22+。

    只在**兼容模式**下才是必需的：面板正常走进程内 WebView2 窗口，用不着
    Node；只有系统缺 WebView2 运行时、退回「Edge 应用窗口」老路子时，
    才靠 Node 走浏览器调试通道把选区送进面板。
    """
    exe = shutil.which("node")
    if not exe:
        for cand in (
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\nodejs\node.exe"),
        ):
            if cand and os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        return False, "未检测到 Node.js（仅 Edge 兼容模式需要；面板走 WebView2 时不需要）"
    try:
        proc = run([exe, "-v"], timeout=15)
        ver = (proc.stdout or b"").decode("utf-8", "replace").strip().lstrip("v")
        major = int(ver.split(".")[0] or 0)
    except Exception:
        return False, "检测到 Node.js，但无法读取版本"
    if major < NODE_MIN_MAJOR:
        return False, ("Node.js v%s 版本偏低（兼容模式需要 v%d 及以上）：%s"
                       % (ver, NODE_MIN_MAJOR, exe))
    return True, "Node.js v%s（仅兼容模式需要）" % ver


# WebView2 运行时的注册表标识（Edge 官方用的固定 GUID）。
WEBVIEW2_CLIENT_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_info():
    """返回 (ok, 描述)：面板本体（进程内 WebView2 窗口）能不能用。

    Win10 / 11 上 WebView2 运行时随 Edge 一起装，绝大多数机器已经有了；
    少数精简系统可能缺，那时助手会自动退回 Edge 兼容模式。
    """
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_GUID),
        (winreg.HKEY_CURRENT_USER,
         r"Software\Microsoft\EdgeUpdate\Clients\%s" % WEBVIEW2_CLIENT_GUID),
    ):
        try:
            with winreg.OpenKey(root, path) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
                if version:
                    return True, "WebView2 运行时 %s（面板即用它）" % version
        except Exception:
            continue
    return False, "未检测到 WebView2 运行时（面板会退回 Edge 兼容模式）"


def browser_info():
    for path in BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            name = "Edge" if "msedge" in path.lower() else "Chrome"
            return True, "%s 已安装" % name
    return False, "未找到 Edge / Chrome（仅 Edge 兼容模式需要）"


def dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except Exception:
                pass
    return total


# ──────────────────────────── 注册表卸载项 ────────────────────────────

def uninstall_values(install_dir: str, exe_name: str = UNINSTALLER_NAME,
                     extra: dict | None = None) -> dict:
    """卸载项要写的键值（纯计算，方便自检直接校验）。"""
    exe = os.path.join(install_dir, exe_name)
    values = {
        "DisplayName": "%s %s" % (APP_NAME, VERSION),
        "DisplayVersion": VERSION,
        "Publisher": PUBLISHER,
        "InstallLocation": install_dir,
        "DisplayIcon": os.path.join(install_dir, APP_EXE),
        "UninstallString": '"%s"' % exe,
        "QuietUninstallString": '"%s" --silent' % exe,
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": max(1, dir_size(install_dir) // 1024),
    }
    if extra:
        values.update(extra)
    return values


def register_uninstall(install_dir: str, exe_name: str = UNINSTALLER_NAME,
                       extra: dict | None = None) -> bool:
    if registry_disabled():
        return False
    values = uninstall_values(install_dir, exe_name, extra)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_REG, 0,
                                winreg.KEY_WRITE) as key:
            for name, value in values.items():
                kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
                winreg.SetValueEx(key, name, 0, kind, value)
        return True
    except Exception:
        return False


def unregister_uninstall() -> bool:
    if registry_disabled():
        return True
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG)
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


def uninstall_entry_exists() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_REG):
            return True
    except Exception:
        return False


# ──────────────────────────── 删除安装目录 ────────────────────────────

def _schedule_cmd_cleanup(install_dir: str, delay_seconds: int = 3) -> bool:
    """起一个脱离的 cmd，等本进程退出后把目录清干净。

    写成 .cmd 文件再执行，而不是 `cmd /c "ping … & rmdir …"`：后者一旦路径里
    带引号，cmd 的 /c 参数解析就会把整条命令切坏（实测目录删不掉）。
    """
    bat = os.path.join(tempfile.gettempdir(), "_1zread_cleanup.cmd")
    content = (
        "@echo off\r\n"
        "ping -n %d 127.0.0.1 >nul\r\n"
        'rmdir /s /q "%s"\r\n'
        'del /f /q "%%~f0" >nul 2>&1\r\n'
    ) % (max(2, delay_seconds + 1), install_dir)
    try:
        # 批处理由 cmd 按系统代码页读取，中文路径必须用 mbcs 写
        with open(bat, "wb") as fh:
            fh.write(content.encode("mbcs", "replace"))
        subprocess.Popen(["cmd", "/c", bat], creationflags=DETACHED, close_fds=True)
        return True
    except Exception:
        return False


def remove_install_dir(install_dir: str, delay_seconds: int = 3) -> bool:
    """删除安装目录。卸载程序自己就住在里面，正在运行的 exe 删不掉，所以分两步：

    1. 先在进程内尽力删一遍 —— 除了卸载程序自己的那两三个文件，其余文件
       这时就没了（这一步不依赖任何外部进程，成功率高）；
    2. 剩下的尾巴交给一个脱离的 cmd，等本进程退出后再删。
    """
    if not install_dir or not os.path.isdir(install_dir):
        return True
    for _ in range(3):
        shutil.rmtree(install_dir, ignore_errors=True)
        if not os.path.isdir(install_dir):
            return True
        time.sleep(0.3)
    return _schedule_cmd_cleanup(install_dir, delay_seconds)


def open_url(url: str) -> None:
    try:
        os.startfile(url)  # noqa: S606
    except Exception:
        run(["cmd", "/c", "start", "", url])
