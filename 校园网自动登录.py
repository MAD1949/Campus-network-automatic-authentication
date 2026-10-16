# -*- coding: utf-8 -*-
"""
校园网自动登录工具（Python + Tkinter + 系统托盘版）
=====================================================
适用认证系统：Dr.COM 城市热点（默认 http://172.16.0.66/）

功能：
  - 首次运行填写配置（认证地址/账号/密码/运营商），保存为 config.json
  - 开机自启（注册表 Run 项，pythonw 静默运行）后自动读取配置登录
  - 常驻系统托盘：右键图标查看状态/立即登录/配置/自启/退出
  - 托盘图标颜色：绿=在线 灰=离线 红=登录失败（悬停显示详细状态）
  - 断线自动重连：每 60 秒检测，离线自动重新登录
  - 关闭配置窗口自动最小化到托盘，不退出程序

认证协议（逆向自门户页面，已实测验证）：
  - 状态查询：GET  {portal}/drcom/chkstatus?callback=drN&jsVersion=4.X&v=N&lang=zh
              响应为 JSONP，"result":1 表示已在线
  - 获取本机认证IP：GET {portal}/ 首页内嵌 v4ip='x.x.x.x'
  - 登录：POST {portal}/drcom/login（application/x-www-form-urlencoded）
          DDDDD=账号 & upass=密码 & 0MKKey=123456 & R1=0 & R2= & R3=0 & R6=0
          & para=00 & v4ip=本机IP & v6ip= & terminal_type=1 & lang=zh
          成功标志：响应包含 Dr.COMWebLoginID_3.htm
          失败标志：响应包含 Dr.COMWebLoginID_2.htm（附 msga 错误信息）

用法：
  python 校园网自动登录.py            # 配置窗口 + 托盘（已有配置则直接托盘+自动登录）
  python 校园网自动登录.py --login    # 仅托盘静默模式（开机自启使用）

依赖：pystray、Pillow（pip install pystray pillow）
配置保存在同目录 config.json，日志写入同目录 login.log。
基于诺亚的 PowerShell 原版脚本改编。
"""

import json
import os
import queue
import random
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

try:
    import winreg
except ImportError:  # 非 Windows 平台禁用自启功能
    winreg = None

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False

IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:  # PyInstaller 打包后，资源放在 exe 同目录
    SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "login.log")

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "CampusNetAutoLogin"

DEFAULT_PORTAL = "http://172.16.0.66"
ISP_NAMES = ["校园网(本地)", "移动", "联通", "电信"]
ISP_SUFFIXES = ["", "", "@lt", "@dx"]  # 对应 Dr.COM 门户运营商后缀

MONITOR_INTERVAL = 60000  # 断线检测间隔（毫秒）

TITLE_FONT = ("Microsoft YaHei", 15, "bold")
LABEL_FONT = ("Microsoft YaHei", 10)
LOG_FONT = ("Microsoft YaHei", 9)


# ---------------------------------------------------------------- 基础工具

def log(message):
    """写日志文件；在控制台运行时同时打印（pythonw 下自动忽略）"""
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line)
    except Exception:
        pass


def _rand():
    return random.randint(500, 99999)


def normalize_portal(url):
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


def http_post(url, params, timeout=10):
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


# ---------------------------------------------------------------- 认证协议

def check_online(portal):
    """查询认证系统在线状态，返回 (是否在线, 在线账号)"""
    try:
        url = "%s/drcom/chkstatus?callback=dr%d&jsVersion=4.X&v=%d&lang=zh" % (
            portal, _rand(), _rand())
        text = http_get(url)
        if re.search(r'"result":\s*1', text):
            m = re.search(r'"uid":\s*"([^"]*)"', text)
            return True, (m.group(1) if m else "")
    except Exception as e:
        log("状态查询失败: %s" % e)
    return False, ""


def get_user_ip(portal):
    """从门户首页解析本机认证 IP（v4ip='x.x.x.x'），失败时用 TCP 连接兜底"""
    try:
        text = http_get(portal + "/")
        m = re.search(r"v4ip='(\d{1,3}(?:\.\d{1,3}){3})'", text)
        if m:
            return m.group(1)
    except Exception as e:
        log("获取门户页面失败: %s" % e)
    try:
        host = urllib.parse.urlsplit(portal).hostname or portal
        s = socket.create_connection((host, 80), timeout=5)
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def do_login(cfg, progress=None, notify=None):
    """执行登录流程。progress(msg) 用于输出进度；notify(title, text) 用于结果通知。
    返回 True/False。"""
    if progress is None:
        progress = lambda msg: None
    portal = normalize_portal(cfg.get("portalHost"))

    online, uid = check_online(portal)
    if online:
        msg = "已在线" + ("（账号 %s）" % uid if uid else "") + "，无需登录"
        progress(msg)
        if notify:
            notify("校园网", "已在线，无需登录")
        return True

    ip = get_user_ip(portal)
    if not ip:
        progress("无法获取本机认证IP")
        if notify:
            notify("登录失败", "无法获取本机认证IP")
        return False

    progress("开始登录，账号: %s，IP: %s" % (cfg.get("userId"), ip))

    for attempt in range(1, 4):
        progress("第 %d 次尝试..." % attempt)
        try:
            body = {
                "DDDDD": cfg.get("userId", ""),
                "upass": cfg.get("password", ""),
                "0MKKey": "123456",
                "R1": "0", "R2": "", "R3": "0", "R6": "0", "para": "00",
                "v4ip": ip, "v6ip": "",
                "terminal_type": "1", "lang": "zh",
                "callback": "dr%d" % _rand(), "jsVersion": "4.X",
                "v": str(_rand()),
            }
            text = http_post(portal + "/drcom/login", body)
            if "Dr.COMWebLoginID_3.htm" in text:
                progress("登录成功")
                if notify:
                    notify("登录成功", "账号: %s  IP: %s" % (cfg.get("userId"), ip))
                return True
            if "Dr.COMWebLoginID_2.htm" in text:
                m = re.search(r"msga='([^']*)'", text)
                progress("登录失败: %s" % (m.group(1) if m else "未知错误"))
            else:
                progress("未知响应: %s" % text[:120])
        except Exception as e:
            progress("请求失败: %s" % e)
        time.sleep(2)

    progress("登录失败，请检查账号密码")
    if notify:
        notify("登录失败", "请检查账号密码")
    return False


def wait_network(portal, timeout=30):
    """等待认证服务器可达（TCP 探测），返回是否成功"""
    host = urllib.parse.urlsplit(portal).hostname or portal
    for i in range(1, timeout + 1):
        try:
            socket.create_connection((host, 80), timeout=2).close()
            log("认证服务器已可达 (等待 %d 秒)" % i)
            return True
        except OSError:
            log("等待网络... (%d/%d)" % (i, timeout))
            time.sleep(1)
    return False


# ---------------------------------------------------------------- 配置读写

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 开机自启

def pythonw_path():
    """优先使用 pythonw.exe（无控制台窗口）"""
    exe = os.path.abspath(sys.executable)
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def autostart_enabled():
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            winreg.QueryValueEx(k, AUTOSTART_NAME)
            return True
    except OSError:
        return False


def set_autostart(enable):
    """写入/删除注册表 HKCU Run 启动项，返回是否成功"""
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                            0, winreg.KEY_SET_VALUE) as k:
            if enable:
                if IS_FROZEN:
                    cmd = '"%s" --login' % os.path.abspath(sys.executable)
                else:
                    cmd = '"%s" "%s" --login' % (pythonw_path(), os.path.abspath(__file__))
                winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, AUTOSTART_NAME)
                except OSError:
                    pass
        return True
    except OSError as e:
        log("设置开机自启失败: %s" % e)
        return False


# ---------------------------------------------------------------- 通知窗口

def show_notify(title, message, duration=5000):
    """右下角深色通知气泡，duration 毫秒后自动关闭（托盘/静默模式下使用）"""
    try:
        root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        frm = tk.Frame(root, bg="#2d2d2d", padx=16, pady=12)
        tk.Label(frm, text=title, bg="#2d2d2d", fg="#ffffff",
                 font=("Microsoft YaHei", 11, "bold")).pack(anchor="w")
        tk.Label(frm, text=message, bg="#2d2d2d", fg="#dddddd",
                 font=("Microsoft YaHei", 9), wraplength=260,
                 justify="left").pack(anchor="w")
        frm.pack()
        root.update_idletasks()
        w, h = root.winfo_width(), root.winfo_height()
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("+%d+%d" % (sw - w - 20, sh - h - 70))
        root.bind("<Button-1>", lambda e: root.destroy())
        root.after(duration, root.destroy)
        root.mainloop()
    except Exception:
        pass


# ---------------------------------------------------------------- 托盘图标

def _make_icon_image(rgb):
    """绘制圆形 WiFi 图标（64x64）"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=rgb + (255,))
    white = (255, 255, 255, 255)
    d.arc([14, 16, 50, 52], 205, 335, fill=white, width=6)
    d.arc([22, 24, 42, 44], 205, 335, fill=white, width=6)
    d.ellipse([29, 31, 35, 37], fill=white)
    return img


ICON_GREEN = _make_icon_image((46, 204, 113))    # 在线
ICON_GREY = _make_icon_image((149, 165, 166))    # 离线/未知
ICON_RED = _make_icon_image((231, 76, 60))       # 登录失败


# ---------------------------------------------------------------- 图形界面（含托盘）

class CampusApp(tk.Tk):

    def __init__(self, headless=False):
        super().__init__()
        self.headless = headless
        self.title("校园网自动登录（Dr.COM 版）")
        self.resizable(False, True)
        self.queue = queue.Queue()
        self.worker = None
        self.icon = None
        self.last_status = None  # (online, uid)

        cfg = load_config()
        self.auto_relogin = bool(cfg.get("autoRelogin", True))

        self._build()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.withdraw()  # 先隐藏，再按需显示，避免闪烁

        if TRAY_AVAILABLE:
            self._start_tray()
        else:
            log("未安装 pystray，系统托盘不可用（pip install pystray pillow）")

        has_cfg = bool(cfg.get("userId") and cfg.get("password"))
        if not has_cfg:
            # 首次运行：显示配置窗口
            self._show_window()
            self._append("首次使用：请填写认证信息后点击「保存并登录」")
        else:
            if not TRAY_AVAILABLE and not self.headless:
                self._show_window()
            self._initial_login()

        self.after(150, self._poll)
        self.after(MONITOR_INTERVAL, self._monitor_tick)

    # ---------- 界面构建 ----------

    def _build(self):
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="校园网自动登录", font=TITLE_FONT).pack()
        ttk.Label(outer, text="Dr.COM 城市热点认证 · 自定义配置",
                  foreground="#777", font=LABEL_FONT).pack(pady=(2, 12))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.portal_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()
        self.isp_var = tk.StringVar()

        rows = [
            ("认证地址：", ttk.Entry(form, textvariable=self.portal_var, width=32)),
            ("账　号：", ttk.Entry(form, textvariable=self.user_var, width=32)),
            ("密　码：", ttk.Entry(form, textvariable=self.pass_var, width=32, show="*")),
            ("运营商：", ttk.Combobox(form, textvariable=self.isp_var, values=ISP_NAMES,
                                     state="readonly", width=30)),
        ]
        for r, (label, widget) in enumerate(rows):
            ttk.Label(form, text=label, font=LABEL_FONT).grid(
                row=r, column=0, sticky="e", padx=(0, 8), pady=4)
            widget.grid(row=r, column=1, sticky="we", pady=4)

        opts = ttk.Frame(outer)
        opts.pack(fill="x", pady=(8, 2))
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self.notify_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="开机自动登录", variable=self.autostart_var).pack(side="left")
        ttk.Checkbutton(opts, text="显示登录通知", variable=self.notify_var).pack(side="left", padx=15)

        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=10)
        self.login_btn = ttk.Button(btns, text="保存并登录", command=self.on_save_login)
        self.login_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        ttk.Button(btns, text="仅保存", command=self.on_save).pack(
            side="left", expand=True, fill="x", padx=2)
        self.check_btn = ttk.Button(btns, text="检查状态", command=self.on_check)
        self.check_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        ttk.Label(outer, text="运行日志", font=LABEL_FONT).pack(anchor="w")
        self.log_text = scrolledtext.ScrolledText(
            outer, height=10, width=54, state="disabled", font=LOG_FONT)
        self.log_text.pack(fill="both", expand=True)

    # ---------- 托盘 ----------

    def _start_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("校园网自动登录", None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("配置窗口", self._tray_action("show"), default=True),
            pystray.MenuItem("立即登录", self._tray_action("login")),
            pystray.MenuItem("检查状态", self._tray_action("check")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自动登录", self._tray_action("toggle_autostart"),
                             checked=lambda item: autostart_enabled()),
            pystray.MenuItem("断线自动重连", self._tray_action("toggle_autorelogin"),
                             checked=lambda item: self.auto_relogin),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开日志文件", self._tray_action("open_log")),
            pystray.MenuItem("退出", self._tray_action("quit")),
        )
        self.icon = pystray.Icon("CampusNetLogin", ICON_GREY,
                                 "校园网自动登录 - 初始化", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _tray_action(self, name):
        def handler(icon, item):
            self.queue.put(("action", name))
        return handler

    def _refresh_tray_menu(self):
        try:
            if self.icon:
                self.icon.update_menu()
        except Exception:
            pass

    def _set_tray_status(self, online, uid="", fail=False):
        self.last_status = (online, uid)
        if not self.icon:
            return
        try:
            if fail:
                self.icon.icon = ICON_RED
                self.icon.title = "校园网自动登录 - 登录失败"
            elif online:
                self.icon.icon = ICON_GREEN
                self.icon.title = "校园网自动登录 - 已在线" + (" (%s)" % uid if uid else "")
            else:
                self.icon.icon = ICON_GREY
                self.icon.title = "校园网自动登录 - 离线"
        except Exception:
            pass

    def _handle_tray_action(self, name):
        if name == "show":
            self._show_window()
        elif name == "login":
            self._start_login(from_tray=True)
        elif name == "check":
            self.on_check()
        elif name == "toggle_autostart":
            enable = not autostart_enabled()
            set_autostart(enable)
            self.autostart_var.set(enable)
            log("开机自启: %s" % ("开启" if enable else "关闭"))
            self._refresh_tray_menu()
        elif name == "toggle_autorelogin":
            self.auto_relogin = not self.auto_relogin
            cfg = load_config()
            cfg["autoRelogin"] = self.auto_relogin
            save_config(cfg)
            log("断线自动重连: %s" % ("开启" if self.auto_relogin else "关闭"))
            self._refresh_tray_menu()
        elif name == "open_log":
            try:
                os.startfile(LOG_FILE)
            except Exception as e:
                log("打开日志失败: %s" % e)
        elif name == "quit":
            self.quit_app()

    def _show_window(self):
        self.deiconify()
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry("+%d+%d" % ((self.winfo_screenwidth() - w) // 2,
                                  (self.winfo_screenheight() - h) // 3))
        self.lift()

    def on_close(self):
        """关闭窗口：有托盘则最小化到托盘，否则退出"""
        if self.icon:
            self.withdraw()
        else:
            self.quit_app()

    def quit_app(self):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass
        self.destroy()

    # ---------- 配置 ----------

    def _load_settings(self):
        cfg = load_config()
        self.portal_var.set(normalize_portal(cfg.get("portalHost")) or DEFAULT_PORTAL)
        uid = cfg.get("userId", "")
        self.user_var.set(uid.split("@")[0] if uid else "")
        self.pass_var.set(cfg.get("password", ""))
        isp = cfg.get("ispType", 0)
        self.isp_var.set(ISP_NAMES[isp] if 0 <= isp < len(ISP_NAMES) else ISP_NAMES[0])
        self.notify_var.set(bool(cfg.get("showNotify", True)))
        self._append("提示：填写配置后点击「保存并登录」；开机自启状态：%s" % (
            "已开启" if autostart_enabled() else "未开启"))

    def _collect_config(self):
        portal = normalize_portal(self.portal_var.get())
        user = self.user_var.get().strip()
        password = self.pass_var.get()
        if not portal or not user or not password:
            raise ValueError("请填写认证地址、账号和密码！")
        isp = ISP_NAMES.index(self.isp_var.get()) if self.isp_var.get() in ISP_NAMES else 0
        if "@" not in user:
            user += ISP_SUFFIXES[isp]
        return {
            "portalHost": portal,
            "userId": user,
            "password": password,
            "ispType": isp,
            "autoStart": self.autostart_var.get(),
            "showNotify": self.notify_var.get(),
            "autoRelogin": self.auto_relogin,
        }

    # ---------- 登录与检测 ----------

    def _initial_login(self):
        """启动时自动登录（使用已保存的配置）"""
        cfg = load_config()
        if not (cfg.get("userId") and cfg.get("password")):
            if self.headless:
                show_notify("校园网自动登录", "未找到配置，请打开配置窗口填写")
            return
        log("读取已保存配置，自动登录（账号: %s）" % cfg.get("userId"))
        if self.headless:
            portal = normalize_portal(cfg.get("portalHost")) or DEFAULT_PORTAL
            wait_network(portal)
            time.sleep(3)
        self._run_login_thread(cfg, notify=bool(cfg.get("showNotify", True)))

    def _run_login_thread(self, cfg, notify=False):
        if self.worker and self.worker.is_alive():
            return False
        self._set_busy(True)

        def progress(msg):
            log(msg)
            self.queue.put(("log", msg))

        def run():
            ok = do_login(cfg, progress=progress,
                          notify=show_notify if notify else None)
            portal = normalize_portal(cfg.get("portalHost")) or DEFAULT_PORTAL
            online, uid = check_online(portal)
            self.queue.put(("status", online, uid, not ok))
            self.queue.put(("done", ok))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()
        return True

    def _start_login(self, from_tray=False):
        if from_tray:
            cfg = load_config()
            if not (cfg.get("userId") and cfg.get("password")):
                show_notify("校园网自动登录", "未找到配置，请先配置账号")
                self._show_window()
                return
            hidden = self.state() == "withdrawn"
            self._run_login_thread(cfg, notify=hidden and bool(cfg.get("showNotify", True)))
        else:
            try:
                cfg = self._collect_config()
            except ValueError as e:
                messagebox.showwarning("提示", str(e), parent=self)
                return
            self._run_login_thread(cfg, notify=False)

    # ---------- 事件处理 ----------

    def on_save(self, silent=False):
        try:
            cfg = self._collect_config()
        except ValueError as e:
            messagebox.showwarning("提示", str(e), parent=self)
            return False
        save_config(cfg)
        if cfg["autoStart"]:
            state = "已开启" if set_autostart(True) else "开启失败"
        else:
            set_autostart(False)
            state = "已关闭"
        log("配置已保存，用户: %s" % cfg["userId"])
        self._append("配置已保存（账号: %s，开机自启: %s）" % (cfg["userId"], state))
        self._refresh_tray_menu()
        if not silent:
            messagebox.showinfo("已保存", "配置已保存。", parent=self)
        return True

    def on_save_login(self):
        if self.on_save(silent=True):
            cfg = load_config()
            self._run_login_thread(cfg, notify=False)

    def on_check(self):
        if self.worker and self.worker.is_alive():
            return
        portal = normalize_portal(self.portal_var.get()) or DEFAULT_PORTAL
        self._set_busy(True)

        def run():
            online, uid = check_online(portal)
            self.queue.put(("status", online, uid, False))
            self.queue.put(("done", None))

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

    def _monitor_tick(self):
        """托盘常驻：周期性检测在线状态，离线且开启自动重连时重新登录"""
        try:
            if not (self.worker and self.worker.is_alive()):
                cfg = load_config()
                portal = normalize_portal(cfg.get("portalHost"))
                if not portal and self.state() != "withdrawn":
                    portal = normalize_portal(self.portal_var.get())
                if portal:
                    def run():
                        online, uid = check_online(portal)
                        self.queue.put(("monitor", online, uid))
                    threading.Thread(target=run, daemon=True).start()
        except Exception:
            pass
        self.after(MONITOR_INTERVAL, self._monitor_tick)

    # ---------- 后台消息轮询 ----------

    def _poll(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1])
                elif kind == "status":
                    self._set_tray_status(item[1], item[2], fail=item[3])
                elif kind == "monitor":
                    online, uid = item[1], item[2]
                    self._set_tray_status(online, uid)
                    if not online and self.auto_relogin:
                        if not (self.worker and self.worker.is_alive()):
                            log("检测到离线，自动重连")
                            cfg = load_config()
                            self._run_login_thread(cfg, notify=True)
                elif kind == "action":
                    self._handle_tray_action(item[1])
                elif kind == "done":
                    self._set_busy(False)
                    ok = item[1]
                    if ok is True and self.state() != "withdrawn":
                        messagebox.showinfo("校园网", "登录成功！", parent=self)
                    elif ok is False and self.state() != "withdrawn":
                        messagebox.showerror("校园网", "登录失败，详见运行日志。", parent=self)
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.login_btn.configure(state=state)
        self.check_btn.configure(state=state)

    def _append(self, text):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


# ---------------------------------------------------------------- 入口

def main():
    headless = "--login" in sys.argv[1:]
    app = CampusApp(headless=headless)
    app.mainloop()


if __name__ == "__main__":
    main()
