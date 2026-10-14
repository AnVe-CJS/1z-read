#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""1z read 卸载程序（图形界面）。

卸载动作：结束助手进程 → 删除开机自启 → 删除桌面/开始菜单快捷方式 →
删除「应用和功能」里的注册表项 → （可选）删除本机配置 → 删除安装目录本身。

因为卸载程序就住在安装目录里，最后一步由一段脱离的 cmd 在进程退出后执行，
否则 exe 自己被占用会删不掉。

命令行：
    "卸载程序.exe"                   图形界面
    "卸载程序.exe" --silent          静默卸载（保留配置），给「应用和功能」用
    "卸载程序.exe" --silent --purge  静默卸载并删除配置
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import messagebox, ttk

import pack_common as pc

# 与安装程序同理：日志管道一律 UTF-8，别让中文在 cp936 往返里花掉。
for _stream_name in ("stdout", "stderr"):
    try:
        getattr(sys, _stream_name).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
del _stream_name

BG = "#f4f6f9"
CARD = "#ffffff"
FG = "#1f2430"
MUTED = "#6b7280"
DANGER = "#c0392b"

UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_SM = ("Microsoft YaHei UI", 9)
UI_FONT_LG = ("Microsoft YaHei UI", 15, "bold")


def current_install_dir() -> str:
    """卸载程序所在目录就是安装目录（源码直跑时退回默认安装目录）。"""
    if pc.is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return pc.default_install_dir()


def do_uninstall(install_dir: str, purge: bool, log=print, kill: bool = True,
                 remove_registry: bool = True, remove_dir: bool = True) -> bool:
    """真正的卸载动作。log 是一个接受字符串的回调（GUI 与静默模式共用）。

    kill / remove_registry 只给自检脚本用（不去结束真实进程、不动注册表）。
    """
    log("· 结束正在运行的 1z read…")
    if not kill:
        log("  已跳过（自检模式）")
    elif pc.kill_app():
        log("  已结束运行中的实例")
        time.sleep(1.0)
    else:
        log("  没有正在运行的实例")

    log("· 删除开机自启…")
    log("  已删除" if pc.remove_autostart() else "  没有找到自启项")

    log("· 删除快捷方式…")
    gone = []
    if pc.remove_file(os.path.join(pc.desktop_dir(), "%s.lnk" % pc.APP_NAME)):
        gone.append("桌面")
    if pc.remove_file(os.path.join(pc.programs_dir(), "%s.lnk" % pc.APP_NAME)):
        gone.append("开始菜单")
    log("  已删除：%s" % ("、".join(gone) if gone else "无"))

    log("· 删除「应用和功能」卸载项…")
    if not remove_registry:
        log("  已跳过（自检模式）")
    else:
        log("  已删除" if pc.unregister_uninstall() else "  删除失败")

    if purge:
        log("· 删除本机配置与历史…")
        shutil.rmtree(pc.data_dir(), ignore_errors=True)
        log("  已删除 %s" % pc.data_dir())
    else:
        log("· 保留本机配置：%s" % pc.data_dir())

    looks_like_install = os.path.isfile(os.path.join(install_dir, pc.APP_EXE))
    if not remove_dir:
        log("· 已跳过安装目录删除（自检模式）")
    elif not looks_like_install:
        log("· 当前目录不是完整安装目录，跳过删除")
    elif pc.remove_install_dir(install_dir):
        log("· 安装目录已清理：%s" % install_dir)
    else:
        log("! 安装目录没能完全删除，请手动删掉：%s" % install_dir)
    return True


class UninstallerApp:
    def __init__(self, root: tk.Tk, install_dir: str) -> None:
        self.root = root
        self.install_dir = install_dir
        self.busy = False

        root.title("1z read 卸载")
        root.configure(bg=BG)
        root.resizable(False, False)
        self._center(560, 420)
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

        band = tk.Frame(self.root, bg=DANGER, height=80)
        band.pack(fill="x")
        band.pack_propagate(False)
        tk.Label(band, text="卸载 1z read", bg=DANGER, fg="#ffffff",
                 font=UI_FONT_LG).place(x=24, y=8)
        tk.Label(band, text="将移除程序本体、快捷方式、开机自启与卸载项",
                 bg=DANGER, fg="#ffe1dd", font=UI_FONT_SM).place(x=26, y=40)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=16, pady=12)

        tk.Label(body, text="安装位置", bg=BG, fg=MUTED, font=UI_FONT_SM).pack(anchor="w")
        card = tk.Frame(body, bg=CARD)
        card.pack(fill="x", pady=(4, 10))
        tk.Label(card, text=self.install_dir, bg=CARD, fg=FG, font=UI_FONT_SM,
                 anchor="w", justify="left", wraplength=500).pack(
            anchor="w", padx=12, pady=10)

        self.var_purge = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="同时删除本机配置与历史（含面板网址与钥匙）",
            variable=self.var_purge,
        ).pack(anchor="w")
        tk.Label(body, text=pc.data_dir(), bg=BG, fg=MUTED, font=UI_FONT_SM,
                 anchor="w", justify="left", wraplength=500).pack(
            anchor="w", padx=24, pady=(0, 10))

        self.log_box = tk.Text(body, height=6, font=("Consolas", 9), bg="#0f172a",
                               fg="#e6edf7", bd=0, relief="flat", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.configure(state="disabled")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill="x", padx=16, pady=(0, 14))
        self.btn_go = ttk.Button(btns, text="开始卸载", command=self.on_uninstall)
        self.btn_go.pack(side="right")
        ttk.Button(btns, text="取消", command=self.root.destroy).pack(
            side="right", padx=(0, 8))

    # ── 日志 ────────────────────────────────────────────────
    def log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def log_async(self, text: str) -> None:
        self.root.after(0, lambda: self.log(text))

    # ── 流程 ────────────────────────────────────────────────
    def on_uninstall(self) -> None:
        if self.busy:
            return
        purge = self.var_purge.get()
        extra = "\n\n同时会删除本机配置与历史（不可恢复）。" if purge else ""
        if not messagebox.askyesno(
                "确认卸载",
                "确定要卸载 1z read 吗？%s\n\n安装位置：\n%s" % (extra, self.install_dir)):
            return
        self.busy = True
        self.btn_go.configure(state="disabled")
        threading.Thread(target=self._worker, args=(purge,), daemon=True).start()

    def _worker(self, purge: bool) -> None:
        try:
            do_uninstall(self.install_dir, purge, self.log_async)
            self.root.after(0, self._finish)
        except Exception:
            lines = traceback.format_exc().splitlines()
            self.log_async("× 卸载过程中出错：")
            for line in lines:
                self.log_async("  " + line)
            self.busy = False
            self.root.after(0, lambda: messagebox.showerror(
                "卸载", "卸载过程中出错，详情见下方日志。"))

    def _finish(self) -> None:
        keep = "" if self.var_purge.get() else "\n本机配置已保留在：\n%s" % pc.data_dir()
        messagebox.showinfo("卸载完成",
                            "1z read 已卸载。\n\n安装目录会在本窗口关闭后自动清理。" + keep)
        self.root.destroy()


def main(argv: list[str]) -> int:
    args = [a.lower() for a in argv]
    silent = "--silent" in args or "/s" in args or "/silent" in args
    purge = "--purge" in args or "/purge" in args

    if silent:
        try:
            return 0 if do_uninstall(current_install_dir(), purge, lambda _t: None) else 1
        except Exception:
            return 1

    try:
        pc.enable_dpi_awareness()
        root = tk.Tk()
        UninstallerApp(root, current_install_dir())
        root.mainloop()
        return 0
    except Exception:
        try:
            messagebox.showerror("卸载", traceback.format_exc())
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
