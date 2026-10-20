# -*- coding: utf-8 -*-
"""夸克更新助手 —— 结构复刻「薄荷启动器」：版本对比 / 公告 / 一键更新 / 启动程序

配置文件、状态、日志都放在 %LOCALAPPDATA%\\QuarkUpdateHelper（隐藏），
exe 旁边只会出现下载下来的程序本身。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import customtkinter as ctk
from tkinter import messagebox

sys.path.insert(0, str(Path(__file__).resolve().parent))
import quark_watch as qw
import quark_login as ql

APP_NAME = "夸克更新助手"


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource(name: str) -> str:
    b = getattr(sys, "_MEIPASS", None)
    if b:
        p = Path(b) / name
        if p.exists():
            return str(p)
    return str(base_dir() / name)


# 内置 Cookie（首次运行写入隐藏配置，用户无需填写）
# 真实值在 secret_local.py，那个文件不进 Git —— 账号凭据不能进仓库。
# 缺少它时退化成空串，程序会直接走扫码登录，功能不受影响。
try:
    from secret_local import BUILTIN_COOKIE
except Exception:
    BUILTIN_COOKIE = ""

DEFAULT_CFG = {
    "share_url": "https://pan.quark.cn/s/76d3b2071339",
    "pwd_id": "76d3b2071339",
    "passcode": "",
    "root_fid": "68bc200f0d914e1ca21a8d72e47d9cf0",
    "download_dir": "",          # 空 = exe 同目录
    "flat": True,                # 平铺，不建子目录
    "drive_folder": "夸克同步",
    "cookie": BUILTIN_COOKIE,
    "auto_launch": True,
    "launch_pattern": "猫薄荷*.exe",
    "auto_check_hours": 6,       # 常驻时每几小时自动查一次，0 = 关闭
    "proxy": "auto",
    "help_url": "https://docs.qq.com/doc/DTkNUTG5tY0ZacUZBN",
}

VER_RE = re.compile(r"(\d+(?:\.\d+)*)")
OWN_NAMES = {"夸克更新助手.exe", "QuarkWatcher.exe"}


def ensure_config() -> dict:
    cfg = {}
    if qw.CONFIG_FILE.exists():
        try:
            cfg = json.loads(qw.CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    changed = False
    for k, v in DEFAULT_CFG.items():
        if k == "download_dir":
            continue                      # 空值表示「跟随 exe」，不固化到磁盘
        if k == "cookie" and k in cfg:
            continue                      # 用户主动清空过就尊重，别再塞回内置账号
        if k not in cfg or cfg[k] in (None, ""):
            cfg[k] = v
            changed = True
    if changed:
        save_config(cfg)
    if not cfg.get("download_dir"):
        cfg["download_dir"] = str(base_dir())   # 仅运行时解析，不写回
    return cfg


def save_config(cfg: dict) -> None:
    qw.CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def ver_tuple(s: str) -> tuple[int, ...]:
    return tuple(int(x) for x in s.split(".")) if s else (0,)


def pick_latest(exes: list[dict], pattern: str = "") -> dict | None:
    """挑网盘里最新的那个。

    先用文件名 pattern 过滤（避免把「薄荷启动器.exe」这种一起下载），
    再按 updated_at 取最新 —— 这样对方改名只要改 pattern 就行，
    不依赖文件名里是否带版本号。
    """
    if not exes:
        return None
    cands = exes
    if pattern:
        import fnmatch
        hit = [f for f in exes if fnmatch.fnmatch(f["name"], pattern)]
        if hit:
            cands = hit
    return max(cands, key=lambda f: f.get("updated_at") or 0)


def _install_crash_hook() -> None:
    def hook(exc_type, exc, tb):
        import traceback
        try:
            (qw.DATA_DIR / "crash.log").write_text(
                "".join(traceback.format_exception(exc_type, exc, tb)), encoding="utf-8")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook
    try:
        import tkinter
        tkinter.Tk.report_callback_exception = lambda self, t, v, tb: hook(t, v, tb)
    except Exception:
        pass


class TextHandler(logging.Handler):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget

    def emit(self, record):
        msg = self.format(record)
        try:
            self.widget.after(0, self._append, msg)
        except Exception:
            pass

    def _append(self, msg):
        self.widget.configure(state="normal")
        self.widget.insert("end", msg + "\n")
        self.widget.see("end")
        self.widget.yview_moveto(1.0)      # 最后一行别被下边框裁掉
        self.widget.configure(state="disabled")


class App(ctk.CTk):
    """打开即检查：有新版就下载并启动，没有就启动本地最新版，然后自己退出。"""

    F = "Microsoft YaHei UI"

    # 暖色调
    C_BG = "#1b1410"
    C_CARD = "#261c16"
    C_LINE = "#3d2d21"
    C_MAIN = "#e08a3c"
    C_MAIN_H = "#f0a05a"
    C_TEXT = "#f4e9dd"
    C_DIM = "#a2907e"
    C_OK = "#9cc06a"
    C_WARN = "#e0a33c"
    C_ERR = "#d9603f"

    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("dark")
        self.cfg = ensure_config()

        self.title(APP_NAME)
        self.geometry("470x392")
        self.resizable(False, False)
        self.configure(fg_color=self.C_BG)
        try:
            ico = resource("app.ico")
            if Path(ico).exists():
                self.iconbitmap(ico)
        except Exception:
            pass

        self.busy = False
        self.remote_ver = ""
        self.local_ver = ""
        self.local_exe = ""
        self.local_mtime = 0.0
        self.share_title = ""
        self._login_evt = None
        self._login_cookie = ""
        self._keep_open = False

        self._build()
        self.bind("<Enter>", self._pin_open)
        self.bind("<Button-1>", self._pin_open)

        qw.setup_logging()
        qw.log.addHandler(TextHandler(self.logbox))

        self.after(300, lambda: self.run(self.task_start))

    # -------------------------------------------------------------- UI
    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=18, pady=(13, 0))
        ctk.CTkLabel(top, text=APP_NAME, font=(self.F, 16, "bold"),
                     text_color=self.C_TEXT).pack(side="left")
        self.lbl_state = ctk.CTkLabel(top, text="启动中…", font=(self.F, 11),
                                      text_color=self.C_MAIN, height=14)
        self.lbl_state.pack(side="right")

        self.lbl_share = ctk.CTkLabel(self, text="正在连接…", font=(self.F, 10),
                                      text_color=self.C_DIM, anchor="w", height=14)
        self.lbl_share.pack(fill="x", padx=18)

        vf = ctk.CTkFrame(self, fg_color=self.C_CARD, corner_radius=9,
                          border_width=1, border_color=self.C_LINE)
        vf.pack(fill="x", padx=18, pady=(8, 8))
        self.lbl_local = ctk.CTkLabel(vf, text="本地 —", font=(self.F, 12),
                                      text_color=self.C_TEXT)
        self.lbl_local.pack(side="left", padx=14, pady=8)
        self.lbl_remote = ctk.CTkLabel(vf, text="网盘 —", font=(self.F, 12),
                                       text_color=self.C_TEXT)
        self.lbl_remote.pack(side="right", padx=14, pady=8)

        self.logbox = ctk.CTkTextbox(self, font=("Consolas", 10), wrap="word",
                                     fg_color=self.C_CARD, text_color=self.C_TEXT,
                                     border_width=1, border_color=self.C_LINE,
                                     corner_radius=8)
        self.logbox.pack(fill="both", expand=True, padx=18)
        # 内层 tk.Text 默认贴边，首字会被圆角/边框啃掉半个
        try:
            self.logbox._textbox.configure(padx=10, pady=8)
        except Exception:
            pass
        self.logbox.configure(state="disabled")

        self.progress = ctk.CTkProgressBar(self, height=5, corner_radius=3,
                                           fg_color=self.C_LINE,
                                           progress_color=self.C_MAIN)
        self.progress.pack(fill="x", padx=18, pady=(8, 2))
        self.progress.set(0)

        bot = ctk.CTkFrame(self, fg_color="transparent")
        bot.pack(fill="x", padx=18, pady=(2, 11))
        self.lbl_hint = ctk.CTkLabel(bot, text="", font=(self.F, 10),
                                     text_color=self.C_DIM, height=14)
        self.lbl_hint.pack(side="left")
        ctk.CTkButton(bot, text="⚙", width=28, height=24, corner_radius=6,
                      fg_color="transparent", hover_color=self.C_CARD,
                      text_color=self.C_DIM, border_width=1,
                      border_color=self.C_LINE, command=self.open_settings
                      ).pack(side="right")
        ctk.CTkLabel(bot, text="by Echo", font=(self.F, 9),
                     text_color="#9d8571").pack(side="right", padx=(0, 8))

    # -------------------------------------------------------------- 小工具
    def set_state(self, text: str, color: str = ""):
        color = color or self.C_MAIN
        self.after(0, lambda: self.lbl_state.configure(text=text, text_color=color))

    def say(self, msg: str):
        qw.log.info(msg)

    def set_hint(self, text: str):
        self.after(0, lambda: self.lbl_hint.configure(text=text))

    def refresh_labels(self):
        if self.local_exe and self.local_mtime:
            lv = f"本地 v{self.local_ver} · {datetime.fromtimestamp(self.local_mtime):%m-%d %H:%M}"
        else:
            lv = "本地 未安装"
        rv = f"网盘 v{self.remote_ver}" if self.remote_ver else "网盘 —"
        self.after(0, lambda: (self.lbl_local.configure(text=lv),
                               self.lbl_remote.configure(text=rv)))

    def run(self, fn):
        if self.busy:
            return
        self.busy = True

        def wrapper():
            try:
                fn()
            except Exception as e:
                qw.log.error("出错：%s", e)
                self.set_state("出了点问题", self.C_ERR)
                self.set_hint(str(e)[:40])
            finally:
                self.busy = False

        threading.Thread(target=wrapper, daemon=True).start()

    # -------------------------------------------------------------- 本地扫描
    def dl_dir(self) -> Path:
        return Path(ensure_config().get("download_dir") or base_dir())

    def scan_local(self) -> tuple[str, str]:
        """返回 (版本号文本, exe 路径)；同时记录 self.local_mtime。

        先按「文件名匹配」筛，筛不到就放宽成「目录里最新的 exe」——
        对方改了名字也不会失效，只要把匹配规则改一下即可。
        """
        import fnmatch
        self.local_mtime = 0.0
        d = self.dl_dir()
        if not d.exists():
            return "", ""

        own = OWN_NAMES | {Path(sys.executable).name}
        pat = (ensure_config().get("launch_pattern") or "*.exe").strip()

        def usable(p: Path) -> bool:
            return (p.is_file() and p.name not in own
                    and not p.name.startswith("~") and p.suffix.lower() == ".exe")

        pool = [p for p in d.glob("*.exe") if usable(p)]
        if not pool:
            return "", ""
        cands = [p for p in pool if fnmatch.fnmatch(p.name, pat)] if pat else pool
        if not cands:
            qw.log.info("没有文件匹配「%s」，改为按修改时间取最新", pat)
            cands = pool

        newest = max(cands, key=lambda p: p.stat().st_mtime)
        self.local_mtime = newest.stat().st_mtime
        m = VER_RE.search(newest.stem)
        ver = m.group(1) if m else datetime.fromtimestamp(
            self.local_mtime).strftime("%m%d")
        return ver, str(newest)

    # -------------------------------------------------------------- 主流程
    def task_start(self):
        self.set_state("检查更新…", self.C_WARN)
        self.local_ver, self.local_exe = self.scan_local()

        try:
            info, files = qw.fetch_snapshot(self.cfg)
        except Exception as e:
            # 网盘连不上不该白屏：有本地版本就直接开，别耽误用
            qw.log.error("连不上分享：%s", e)
            self.set_state("网络不通", self.C_ERR)
            self.remote_ver = ""
            self.refresh_labels()
            if self.local_exe:
                self.say("网络不通，直接启动本地已有版本")
                self.set_hint("网络不通，启动本地版本")
                self.launch_and_exit(self.local_exe)
            else:
                self.set_hint("网络不通，稍后再试")
            return

        self.share_title = info["title"]
        self.after(0, lambda: self.lbl_share.configure(text=f"来源：{info['title']}"))

        pat = (self.cfg.get("launch_pattern") or "*.exe").strip()
        exes = [f for f in files if f["name"].lower().endswith(".exe")]
        latest = pick_latest(exes, pat)
        self.remote_ver = ""
        if latest:
            m = VER_RE.search(Path(latest["name"]).stem)
            self.remote_ver = m.group(1) if m else ""

        self.refresh_labels()

        # 是否已是最新：比「网盘那个文件的更新时间」和「本地文件的修改时间」。
        # 对方改名、改版本号都不影响；留 60 秒容差应对时钟误差。
        remote_ts = (latest.get("updated_at") or 0) / 1000.0 if latest else 0
        fresh = bool(self.local_exe) and self.local_mtime >= remote_ts - 60

        if not fresh:
            qw.log.info("网盘最新：%s（%s）", latest["name"] if latest else "—",
                        datetime.fromtimestamp(remote_ts).strftime("%m-%d %H:%M")
                        if remote_ts else "无时间")

        if fresh:
            self.set_state("已是最新", self.C_OK)
            self.say(f"本地 v{self.local_ver} 已是最新，直接启动")
            if not self.cfg.get("auto_launch", True):
                self.set_hint("已是最新（auto_launch 已关闭）")
                return
            self.set_hint("已是最新，正在启动")
            self.launch_and_exit(self.local_exe)
            return

        if not latest:
            self.set_state("分享里没找到程序", self.C_ERR)
            if self.local_exe:
                self.launch_and_exit(self.local_exe)
            return

        # 需要下载 → 先确认登录态
        self.set_state("校验登录…", self.C_WARN)
        if not self._cookie_ok():
            self.say("登录已失效，请扫码登录")
            self.set_state("等待扫码", self.C_WARN)
            if not self._ask_login():
                self.set_state("未登录，无法更新", self.C_ERR)
                lv, le = self.scan_local()
                if le:
                    self.set_hint("未登录，先启动本地已有版本")
                    self.launch_and_exit(le)
                return

        self.set_state(f"更新到 v{self.remote_ver or '最新'}…", self.C_WARN)
        self.set_hint("正在下载最新版")
        self.after(0, lambda: (self.progress.configure(mode="indeterminate"),
                               self.progress.start()))
        path = self._download(latest, info)
        self.after(0, self._stop_progress)

        if path:
            if not self.cfg.get("auto_launch", True):
                self.set_state("已下载", self.C_OK)
                self.say(f"已下载最新版：{Path(path).name}（auto_launch 已关闭，不自动启动）")
                self.set_hint("已下载，未自动启动")
                self.refresh_labels()
                return
            self.launch_and_exit(path)
            return

        self.set_state("下载失败", self.C_ERR)
        lv, le = self.scan_local()
        if le:
            self.say("下载失败，改为启动本地已有版本")
            self.launch_and_exit(le)
        else:
            self.set_hint("下载失败，请稍后重试")

    def _cookie_ok(self) -> bool:
        st, msg = ql.QrLogin.verify(self.cfg.get("cookie") or "")
        self.say(msg)
        if st == "net":
            # 网络抖一下不算登录失效，先去试下载，真没权限会在转存那步报
            return True
        return st == "ok"

    def _download(self, latest: dict, info: dict) -> str:
        s_anon = qw.session_with(None, with_cookie=False, proxy=self.cfg.get("proxy"))
        targets = qw._annotate_dirs(self.cfg, s_anon, info["stoken"], [latest])
        n = qw.do_download(self.cfg, {}, info, targets)
        if not n:
            return ""
        state = qw.load_state()
        state.setdefault("files", {})[latest["fid"]] = {
            "name": latest["name"], "size": latest["size"],
            "updated_at": latest["updated_at"],
        }
        state["last_check"] = datetime.now().isoformat(timespec="seconds")
        qw.save_state(state)
        _, p = self.scan_local()
        return p

    def _stop_progress(self):
        def f():
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(1)
        try:
            self.after(0, f)
        except Exception:
            pass

    def launch_and_exit(self, path: str):
        try:
            qw.log.info("启动：%s", Path(path).name)
            os.startfile(path)
            self.set_state("已启动", self.C_OK)
        except OSError as e:
            if getattr(e, "winerror", None) == 1223:
                # 对方程序清单里要管理员权限，UAC 被点了「否」
                qw.log.error("启动被取消：%s 需要管理员权限，UAC 弹窗里要点「是」",
                             Path(path).name)
                self.set_state("需要管理员权限", self.C_WARN)
                self.set_hint("请在 UAC 弹窗点「是」后重新打开")
            else:
                qw.log.error("启动失败：%s", e)
                self.set_state("启动失败", self.C_ERR)
                self.set_hint("可手动到安装目录运行")
            return
        except Exception as e:
            qw.log.error("启动失败：%s", e)
            self.set_state("启动失败", self.C_ERR)
            self.set_hint("可手动到安装目录运行")
            return
        self.after(900, lambda: self._close_soon(6))

    def _close_soon(self, n: int):
        """倒计时自动关闭；鼠标碰到窗口或点一下就取消，方便进去换号/看日志"""
        if getattr(self, "_keep_open", False):
            self.set_hint("窗口已保留，可点 ⚙ 更换账号")
            return
        if n <= 0:
            self.destroy()
            return
        self.set_hint(f"{n} 秒后自动关闭（鼠标移入可保留）")
        self.after(1000, lambda: self._close_soon(n - 1))

    def _pin_open(self, _evt=None):
        self._keep_open = True

    # -------------------------------------------------------------- 扫码登录
    def _ask_login(self) -> bool:
        """等用户扫完码。返回是否真的换了新 Cookie（取消/超时都算 False）"""
        self._login_evt = threading.Event()
        self._login_cookie = ""
        self.after(0, self._open_login)
        if not self._login_evt.wait(300):
            self.say("扫码超时")
            return False
        if not self._login_cookie:
            self.say("已取消扫码登录")
            return False
        self.cfg["cookie"] = self._login_cookie
        disk = dict(self.cfg)
        disk["download_dir"] = ""          # 不固化，保持跟随 exe
        save_config(disk)
        self.say("登录成功，Cookie 已更新")
        return True

    def _open_login(self):
        win = ctk.CTkToplevel(self)
        win.title("扫码登录")
        win.geometry("350x432")
        win.configure(fg_color=self.C_BG)
        win.transient(self)
        win.resizable(False, False)
        try:
            win.after(120, win.grab_set)
        except Exception:
            pass

        box = ctk.CTkLabel(win, text="正在获取二维码…", width=270, height=270,
                           fg_color="#ffffff", corner_radius=8,
                           text_color="#666666")
        box.pack(pady=(16, 8))
        tip = ctk.CTkLabel(win, text="用微信扫一扫",
                           font=(self.F, 12), text_color=self.C_TEXT)
        tip.pack()
        sub = ctk.CTkLabel(win, text="", font=(self.F, 10), text_color=self.C_DIM,
                           wraplength=290)
        sub.pack(pady=(2, 4))

        qr = ql.QrLogin()
        stop = threading.Event()
        state = {"ready": False}

        def load_qr():
            try:
                box.configure(image="")
            except Exception:
                pass
            box.configure(text="正在获取二维码…")
            sub.configure(text="", text_color=self.C_DIM)

            def work():
                try:
                    content = qr.new_token()
                    pil = qr.render(content, 270)
                    state["ready"] = True

                    def show():
                        if stop.is_set():
                            return
                        cimg = ctk.CTkImage(light_image=pil, dark_image=pil,
                                            size=(270, 270))
                        box.configure(image=cimg, text="")
                        box._img = cimg
                        sub.configure(text="等待扫码…", text_color=self.C_DIM)
                    self.after(0, show)
                except Exception as e:
                    state["ready"] = False
                    self.after(0, lambda m=str(e): (
                        box.configure(text="二维码没取到\n点下面「重新获取」", image=""),
                        sub.configure(text=m[:80], text_color=self.C_ERR)))
            threading.Thread(target=work, daemon=True).start()

        def poller():
            while not stop.is_set():
                time.sleep(2.0)
                if stop.is_set():
                    return
                if not state["ready"]:
                    continue
                st, detail = qr.poll()
                if stop.is_set():
                    return
                if st == "ok":
                    try:
                        ck = qr.finish(detail)
                        vst, msg = ql.QrLogin.verify(ck)
                        if vst == "expired":
                            self.after(0, lambda m=msg: sub.configure(
                                text=m, text_color=self.C_ERR))
                            stop.set()
                            return
                    except Exception as e:
                        self.after(0, lambda m=str(e): sub.configure(
                            text=f"换取登录失败：{m}", text_color=self.C_ERR))
                        stop.set()
                        return
                    self._login_cookie = ck
                    self.after(0, lambda: sub.configure(text="登录成功",
                                                        text_color=self.C_OK))
                    self.after(400, lambda: (win.destroy(), self._login_evt.set()))
                    return
                if st == "scanned":
                    self.after(0, lambda: sub.configure(
                        text="已扫码，请在手机上点“确认登录”", text_color=self.C_OK))
                elif st == "error":
                    self.after(0, lambda d=detail: sub.configure(
                        text=d, text_color=self.C_ERR))

        threading.Thread(target=poller, daemon=True).start()
        load_qr()

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 14))

        ctk.CTkButton(bar, text="刷新二维码", height=32, corner_radius=7,
                      fg_color=self.C_MAIN, hover_color=self.C_MAIN_H,
                      text_color="#20160f", font=(self.F, 12),
                      command=load_qr).pack(fill="x")

        def on_close():
            stop.set()
            self._login_cookie = ""
            try:
                win.destroy()
            finally:
                if self._login_evt:
                    self._login_evt.set()

        win.protocol("WM_DELETE_WINDOW", on_close)
    # -------------------------------------------------------------- 设置
    def open_settings(self):
        cfg = ensure_config()
        win = ctk.CTkToplevel(self)
        win.title("设置")
        win.geometry("410x410")
        win.configure(fg_color=self.C_BG)
        win.transient(self)

        def field(label, value):
            ctk.CTkLabel(win, text=label, font=(self.F, 11),
                         text_color=self.C_DIM, anchor="w").pack(
                fill="x", padx=18, pady=(10, 2))
            e = ctk.CTkEntry(win, fg_color=self.C_CARD, border_color=self.C_LINE,
                             text_color=self.C_TEXT)
            e.insert(0, value or "")
            e.pack(fill="x", padx=18)
            return e

        e_url = field("分享链接", cfg.get("share_url"))
        _cur = cfg.get("download_dir") or ""
        try:
            if _cur and Path(_cur).resolve() == base_dir():
                _cur = ""
        except Exception:
            pass
        e_dir = field("下载目录（留空 = 与程序同目录）", _cur)
        e_pat = field("程序文件名（支持 * 通配，对方改名就改这里）",
                      cfg.get("launch_pattern") or "猫薄荷*.exe")

        st, msg = "ok", ""
        try:
            st, msg = ql.QrLogin.verify(cfg.get("cookie") or "")
        except Exception as e:
            st, msg = "expired", f"校验失败：{e}"
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(fill="x", padx=18, pady=(14, 0))
        ctk.CTkLabel(row, text=f"登录状态：{msg}", font=(self.F, 11),
                     text_color=self.C_OK if st == "ok" else
                     (self.C_WARN if st == "net" else self.C_ERR)).pack(side="left")

        def relogin():
            win.destroy()

            def work():
                try:
                    if self._ask_login():
                        self.set_state("登录已更新", self.C_OK)
                except Exception as e:
                    qw.log.error("登录失败：%s", e)

            threading.Thread(target=work, daemon=True).start()

        ctk.CTkButton(row, text="扫码登录", width=86, height=28, corner_radius=7,
                      fg_color=self.C_MAIN, hover_color=self.C_MAIN_H,
                      text_color="#20160f", command=relogin).pack(side="right", padx=(0, 6))

        def do_save():
            url = e_url.get().strip()
            new = dict(cfg)
            new.update({
                "share_url": url,
                "download_dir": e_dir.get().strip(),
                "launch_pattern": e_pat.get().strip() or "*.exe",
            })
            if "/s/" in url:
                new["pwd_id"] = url.split("/s/")[-1].split("?")[0].strip("/")
            if url != cfg.get("share_url"):
                new["root_fid"] = ""
            if not new.get("download_dir"):
                new.pop("download_dir", None)   # 留空 = 跟随 exe
            save_config(new)
            self.cfg = ensure_config()
            win.destroy()
            self.say("设置已保存")
            self.run(self.task_start)

        ctk.CTkButton(win, text="保存", height=36, corner_radius=8,
                      fg_color=self.C_MAIN, hover_color=self.C_MAIN_H,
                      text_color="#20160f", font=(self.F, 13, "bold"),
                      command=do_save).pack(fill="x", padx=18, pady=18)


if __name__ == "__main__":
    _install_crash_hook()
    try:
        App().mainloop()
    except Exception:
        import traceback
        try:
            (qw.DATA_DIR / "crash.log").write_text(traceback.format_exc(),
                                                   encoding="utf-8")
        except Exception:
            pass
        raise
