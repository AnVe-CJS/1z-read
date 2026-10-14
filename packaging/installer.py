#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1z read 安装程序。

整个安装载荷（助手 exe、网页源码、浏览器扩展、说明文件、卸载程序）都内嵌在
本安装程序内部，所以对外只需要传这一个 exe。

安装动作：结束旧实例 → 复制文件 → 建快捷方式 → 写开机自启 → 注册"应用和功能"
里的卸载项（HKCU，不需要管理员）→ 可选立即启动。

命令行（给脚本化 / 静默安装用）：
    "1z read 安装程序.exe"                          图形向导
    "1z read 安装程序.exe" --silent                 静默装到默认目录（建桌面快捷方式+自启）
    "1z read 安装程序.exe" --silent --dir D --no-desktop --no-autostart --launch
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pack_common as pc

# 跨进程的日志输出一律写死 UTF-8：中文 Windows 上 Python 默认按 cp936 编解码
# 管道，而调用方（自检脚本、部署脚本）按 UTF-8 读 —— 不指定就整段中文乱码。
# 安装向导的 GUI 用的是 tkinter 文本框（str），不受影响；这里管的是 --silent。
for _stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, _stream_name).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream_name

# ──────────────────────────── 配色 ────────────────────────────

BG = "#f4f6f9"
CARD = "#ffffff"
FG = "#1f2430"
MUTED = "#6b7280"
ACCENT = "#2f6feb"
OK = "#0f9268"
WARN = "#c2410c"

UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SM = ("Microsoft YaHei UI", 9)
UI_FONT_LG = ("Microsoft YaHei UI", 15, "bold")


# ──────────────────────────── 安装逻辑 ────────────────────────────

def do_install(dest: str, make_desktop: bool = True, autostart: bool = True,
               launch: bool = False, log=print, progress=None,
               kill: bool = True, write_registry: bool = True) -> bool:
    """把载荷装到 dest。log/progress 是回调（图形界面与静默模式共用）。

    kill / write_registry 只给自检脚本用：在受管环境里跑测试时不方便结束真实
    进程、也不该去动注册表，这两步就单独验证。
    """
    def step(pct: int) -> None:
        if progress:
            progress(pct)

    payload = pc.payload_dir()
    if not os.path.isdir(payload):
        log("× 安装载荷缺失：%s" % payload)
        return False

    log("安装目录：%s" % dest)
    log("")

    # 1) 结束旧实例
    step(5)
    log("· 检查正在运行的 1z read…")
    if not kill:
        log("  已跳过（自检模式）")
    elif pc.kill_app():
        log("  已结束正在运行的实例")
        time.sleep(1.0)
    else:
        log("  没有正在运行的实例")

    # 2) 复制助手本体
    step(20)
    os.makedirs(dest, exist_ok=True)
    src_app = os.path.join(payload, "app")
    if not os.path.isdir(src_app):
        log("× 载荷里没有 app/ 目录")
        return False
    # 升级安装：先清掉上个版本的运行库与主程序，避免残留旧文件
    for stale in ("_internal", pc.APP_EXE):
        target = os.path.join(dest, stale)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        elif os.path.isfile(target):
            try:
                os.remove(target)
            except Exception:
                pass
    log("· 复制程序文件…")
    shutil.copytree(src_app, dest, dirs_exist_ok=True)
    log("  完成（%d 个条目）" % len(os.listdir(src_app)))

    # 3) 附加资料：网页源码 / 浏览器扩展 / 说明
    step(45)
    for name in ("web", "extension", "docs"):
        src = os.path.join(payload, name)
        if not os.path.isdir(src):
            continue
        shutil.copytree(src, os.path.join(dest, name), dirs_exist_ok=True)
        log("· 复制 %s/" % name)

    # 4) 卸载程序
    step(60)
    unins_src = os.path.join(payload, pc.UNINSTALLER_NAME)
    if os.path.isfile(unins_src):
        shutil.copy2(unins_src, os.path.join(dest, pc.UNINSTALLER_NAME))
        log("· 放入卸载程序")

    exe = os.path.join(dest, pc.APP_EXE)
    icon = exe

    # 5) 快捷方式
    step(72)
    made = []
    if make_desktop:
        lnk = os.path.join(pc.desktop_dir(), "%s.lnk" % pc.APP_NAME)
        if pc.create_shortcut(lnk, exe, icon, "%s · 划词即可问 AI" % pc.APP_NAME):
            made.append("桌面")
    lnk = os.path.join(pc.programs_dir(), "%s.lnk" % pc.APP_NAME)
    if pc.create_shortcut(lnk, exe, icon, "%s · 划词即可问 AI" % pc.APP_NAME):
        made.append("开始菜单")
    log("· 快捷方式：%s" % ("、".join(made) if made else "未创建"))

    # 6) 开机自启
    step(84)
    if autostart:
        if pc.write_autostart(exe):
            log("· 已设置开机自动启动")
        else:
            log("! 开机自启设置失败（不影响使用）")
    else:
        pc.remove_autostart()
        log("· 未设置开机自启")

    # 7) 注册卸载项
    step(92)
    if not write_registry:
        log("· 已跳过卸载项写入（自检模式）")
    elif pc.register_uninstall(dest):
        log('· 已写入「应用和功能」卸载项')
    else:
        log("! 卸载项写入失败（可在安装目录里直接运行卸载程序）")

    # 8) 启动
    step(100)
    if launch:
        pc.run_detached(exe)
        log("· 已启动 1z read，请在托盘图标里完成「接入设置」")

    log("")
    log("安装完成。")
    return True


def install_problem(dest: str) -> str:
    """返回不能安装的原因，空串表示没问题。"""
    target = os.path.abspath(dest)
    if not target or len(target) < 6:
        return "安装路径太短。"
    if os.path.dirname(target) in (target, ""):
        return "不能选盘符根目录。"
    windir = os.path.abspath(os.environ.get("WINDIR", r"C:\Windows")).lower()
    if target.lower() == windir or target.lower().startswith(windir + os.sep):
        return "不能装到系统目录里。"
    return ""


def is_our_install(path: str) -> bool:
    return (os.path.isfile(os.path.join(path, pc.APP_EXE))
            or os.path.isfile(os.path.join(path, pc.UNINSTALLER_NAME)))


# ──────────────────────────── 图形界面 ────────────────────────────

class InstallerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.checks: list[tk.Widget] = []
        self.busy = False
        self.node_ok = False
        self.webview_ok = False
        self.install_ok = False

        root.title("%s 安装向导" % pc.APP_NAME)
        root.configure(bg=BG)
        root.resizable(False, False)
        self._center(600, 570)
        self._build()

    def _center(self, w: int, h: int) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                            max(0, (sh - h) // 3)))

    def _build(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TCheckbutton", background=CARD, foreground=FG, font=UI_FONT)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure("TButton", font=UI_FONT, padding=(14, 7))
        style.configure("Accent.TButton", font=UI_FONT, padding=(18, 9))

        band = tk.Frame(self.root, bg=ACCENT, height=86)
        band.pack(fill="x")
        band.pack_propagate(False)
        tk.Label(band, text=pc.APP_NAME, bg=ACCENT, fg="#ffffff",
                 font=UI_FONT_LG).place(x=24, y=10)
        tk.Label(band, text="划词即可问 AI · 安装向导  v%s" % pc.VERSION,
                 bg=ACCENT, fg="#dbe6ff", font=UI_FONT_SM).place(x=26, y=44)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        tk.Label(body, text="安装位置", bg=BG, fg=MUTED, font=UI_FONT_SM).pack(anchor="w")
        row = tk.Frame(body, bg=CARD)
        row.pack(fill="x", pady=(4, 10))
        self.var_dir = tk.StringVar(value=pc.default_install_dir())
        entry = tk.Entry(row, textvariable=self.var_dir, font=UI_FONT, bd=1,
                         relief="solid", highlightthickness=0, bg="#ffffff", fg=FG,
                         insertbackground=FG)
        entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(10, 6), pady=10)
        ttk.Button(row, text="浏览…", command=self._pick_dir).pack(
            side="left", padx=(0, 10), pady=10)

        tk.Label(body, text="安装选项", bg=BG, fg=MUTED, font=UI_FONT_SM).pack(anchor="w")
        opt = tk.Frame(body, bg=CARD)
        opt.pack(fill="x", pady=(4, 10))
        self.var_desktop = tk.BooleanVar(value=True)
        self.var_autostart = tk.BooleanVar(value=True)
        self.var_launch = tk.BooleanVar(value=True)
        for text, var in (
            ("创建桌面快捷方式", self.var_desktop),
            ("开机自动启动（登录后自动在托盘运行）", self.var_autostart),
            ("安装完成后立即启动", self.var_launch),
        ):
            ttk.Checkbutton(opt, text=text, variable=var).pack(anchor="w", padx=12, pady=3)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="环境检查", bg=BG, fg=FG, font=UI_FONT).pack(side="left")
        tk.Button(head, text="重新检测", bd=0, relief="flat", bg=BG, fg=ACCENT,
                  activebackground=BG, cursor="hand2", font=UI_FONT_SM,
                  command=self.refresh_checks).pack(side="right")
        self.check_box = tk.Frame(body, bg=CARD)
        self.check_box.pack(fill="x", pady=(4, 10))
        self.refresh_checks()

        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(0, 6))
        self.log_box = tk.Text(body, height=7, font=("Consolas", 9), bg="#0f172a",
                               fg="#e6edf7", bd=0, relief="flat", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        self.btn_install = ttk.Button(btns, text="开始安装", style="Accent.TButton",
                                      command=self.on_install)
        self.btn_install.pack(side="right")
        self.btn_close = ttk.Button(btns, text="退出", command=self.root.destroy)
        self.btn_close.pack(side="right", padx=(0, 8))

    # ── 环境检查 ────────────────────────────────────────────
    def refresh_checks(self) -> None:
        for widget in self.checks:
            widget.destroy()
        self.checks.clear()

        # 面板本体：进程内 WebView2 窗口。Win10/11 随 Edge 一起装，一般都有。
        wv_ok, wv_text = pc.webview2_info()
        self.webview_ok = wv_ok
        self._check_row(wv_ok, wv_text, warn_only=True)

        # Node 只在缺 WebView2、退回 Edge 兼容模式时才需要，所以是「提示」不是「拦路」
        node_ok, node_text = pc.node_info()
        self.node_ok = node_ok
        self._check_row(node_ok, node_text, link=(not node_ok and not wv_ok),
                        warn_only=True, info=wv_ok)

        br_ok, br_text = pc.browser_info()
        self._check_row(br_ok, br_text, warn_only=True, info=True)

    def _check_row(self, ok: bool, text: str, link: bool = False,
                   warn_only: bool = False, info: bool = False) -> None:
        """一行环境检测结果。

        ok=✓ 满足；info=· 没满足但眼下不影响用（灰）；warn_only=! 建议处理（橙）；
        其余=× 必须处理（橙，配「去下载」链接）。
        """
        row = tk.Frame(self.check_box, bg=CARD)
        row.pack(fill="x")
        self.checks.append(row)
        if ok:
            mark, color, fg = "✓", OK, FG
        elif info:
            mark, color, fg = "·", MUTED, MUTED
        else:
            mark, color, fg = ("!" if warn_only else "×"), WARN, WARN
        tk.Label(row, text=mark, bg=CARD, fg=color, font=UI_FONT,
                 width=2).pack(side="left", padx=(12, 0), pady=4)
        tk.Label(row, text=text, bg=CARD, fg=fg, font=UI_FONT_SM,
                 anchor="w", justify="left", wraplength=420).pack(side="left", pady=4)
        if link:
            tk.Button(row, text="去下载", bd=0, relief="flat", bg=CARD, fg=ACCENT,
                      activebackground=CARD, cursor="hand2", font=UI_FONT_SM,
                      command=lambda: pc.open_url(pc.NODE_DOWNLOAD)).pack(
                side="right", padx=(6, 12))

    # ── 交互 ────────────────────────────────────────────────
    def _pick_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择安装目录",
            initialdir=self.var_dir.get() or os.path.expanduser("~"))
        if not chosen:
            return
        chosen = os.path.abspath(chosen)
        if os.path.basename(chosen).lower() != pc.APP_NAME.lower():
            chosen = os.path.join(chosen, pc.APP_NAME)
        self.var_dir.set(chosen)

    def log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def log_async(self, text: str) -> None:
        self.root.after(0, lambda: self.log(text))

    def progress_async(self, value: int) -> None:
        self.root.after(0, lambda: self.progress.configure(value=value))

    def on_install(self) -> None:
        if self.busy:
            return
        dest = self.var_dir.get().strip().strip('"')
        if not dest:
            messagebox.showwarning("安装向导", "请先选择安装目录。")
            return
        problem = install_problem(dest)
        if problem:
            messagebox.showerror("安装向导", "%s\n\n%s" % (problem, dest))
            return
        if os.path.isdir(dest) and os.listdir(dest) and not is_our_install(dest):
            if not messagebox.askyesno(
                    "安装向导",
                    "目录里已经有别的东西：\n%s\n\n继续会往里写入 1z read 的文件，"
                    "但不会删除原有内容。要继续吗？" % dest):
                return

        self.busy = True
        self.btn_install.configure(state="disabled")
        self.btn_close.configure(state="disabled")
        self.progress.configure(value=0)
        opts = (dest, self.var_desktop.get(), self.var_autostart.get(), self.var_launch.get())
        threading.Thread(target=self._worker, args=opts, daemon=True).start()

    def _worker(self, dest: str, make_desktop: bool, autostart: bool, launch: bool) -> None:
        try:
            ok = do_install(dest, make_desktop, autostart, launch,
                            log=self.log_async, progress=self.progress_async)
            if ok:
                self.install_ok = True
                self.root.after(0, self._after_install)
        except Exception:
            lines = traceback.format_exc().splitlines()
            self.log_async("× 安装失败：")
            for line in lines:
                self.log_async("  " + line)
            self.root.after(0, lambda: messagebox.showerror(
                "安装向导", "安装过程中出错，详情见下方日志。"))
        finally:
            self.busy = False
            self.root.after(0, lambda: self.btn_install.configure(state="normal"))
            self.root.after(0, lambda: self.btn_close.configure(state="normal"))

    def _after_install(self) -> None:
        note = ("安装完成。\n\n"
                "下一步：右键托盘里的 1z read →「接入设置」，"
                "花两分钟把自己的面板网址与钥匙填进去，工具才能用。\n"
                "更细的步骤见安装目录下的「接入说明.txt」。")
        if not self.webview_ok and not self.node_ok:
            note += ("\n\n提醒：这台机器既没有检测到 WebView2 运行时，也没有 Node.js %d+，"
                     "面板可能打不开。装个 Edge（自带 WebView2）或 Node.js 即可。"
                     % pc.NODE_MIN_MAJOR)
        messagebox.showinfo("安装向导", note)


# ──────────────────────────── 入口 ────────────────────────────

def run_silent(argv: list[str]) -> int:
    # 参数比较不区分大小写，但取值必须用原样大小写（路径大小写不能被改）
    lower = [a.lower() for a in argv]

    def has(flag: str) -> bool:
        return flag in lower

    def opt_value(flag: str, default: str = "") -> str:
        if flag in lower:
            idx = lower.index(flag)
            if idx + 1 < len(argv):
                return argv[idx + 1]
        return default

    dest = opt_value("--dir") or pc.default_install_dir()
    problem = install_problem(dest)
    if problem:
        print("× %s" % problem)
        return 2
    ok = do_install(
        dest,
        make_desktop=not has("--no-desktop"),
        autostart=not has("--no-autostart"),
        launch=has("--launch"),
        log=lambda text: print(text, flush=True),
    )
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    args = [a.lower() for a in argv]
    if "--silent" in args or "/s" in args or "/silent" in args:
        try:
            return run_silent(argv)
        except Exception:
            traceback.print_exc()
            return 1

    try:
        pc.enable_dpi_awareness()
        root = tk.Tk()
        InstallerApp(root)
        root.mainloop()
        return 0
    except Exception:
        try:
            messagebox.showerror("安装向导", traceback.format_exc())
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
