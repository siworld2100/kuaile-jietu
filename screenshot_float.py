# -*- coding: utf-8 -*-
"""
截图悬浮工具 v3 (Screenshot Pinner)
====================================
- Ctrl+Alt+A (可自定义) 框选截图: 8方向手柄调整选区 + 实时尺寸标签
- 编辑器: 画笔/矩形/椭圆/箭头/文字 + 5色色板 + 撤销/清空, 完成后贴图悬浮
- 悬浮贴图无边框置顶, 右键可「编辑此贴图」继续修改标注
- 系统托盘: 左键截图, 右键菜单(截图/设置/开机自启/退出)
- 单实例互斥, 崩溃日志写入 %APPDATA%\\ScreenshotFloat\\error.log
"""

import ctypes
import json
import math
import os
import sys
import threading
import time
import traceback
import tkinter as tk
import winreg
from ctypes import wintypes
from io import BytesIO
from tkinter import filedialog, messagebox, ttk

# ---- Windows 高 DPI 适配, 保证截取坐标与实际像素一致 ----
if sys.platform == "win32":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

from PIL import Image, ImageDraw, ImageFont, ImageGrab, ImageTk  # noqa: E402

APP_NAME = "ScreenshotFloat"     # 内部标识: 互斥体/配置目录/注册表键, 保持英文避免迁移
APP_TITLE = "快乐截图"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "error.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MIN_SCALE, MAX_SCALE = 0.1, 8.0
MIN_REGION = 10          # 最小选区尺寸(px)
UI_FONT = "Microsoft YaHei"
PEN_WIDTH = 3
TEXT_SIZE = 16
TOOLBAR_H = 46
COLORS = ["#E24B4A", "#EF9F27", "#378ADD", "#1D9E75", "#2C2C2A"]

try:
    import keyboard  # 全局热键
except ImportError:
    keyboard = None


def _write_log(text: str):
    """把异常文本写入日志文件 (打包后无控制台, 必须落盘排障)。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(text + "\n" + "-" * 60 + "\n")
    except Exception:
        pass


def log_error():
    _write_log(traceback.format_exc())


def _thread_excepthook(args):
    if args.exc_type is not SystemExit:
        _write_log("[thread] " + "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback)))


sys.excepthook = lambda t, v, tb: (
    None if t is SystemExit else _write_log(
        "".join(traceback.format_exception(t, v, tb))))
threading.excepthook = _thread_excepthook


# ============================ 配置 ============================

def load_config():
    default = {"hotkey": "ctrl+alt+a", "quit_hotkey": "ctrl+alt+x", "autostart": True}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        default.update({k: v for k, v in cfg.items() if k in default})
    except Exception:
        pass
    return default


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable: bool):
    """开机自启: HKCU Run 注册表项, 无需管理员权限。"""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            if getattr(sys, "frozen", False):
                cmd = f'"{sys.executable}"'
            else:
                cmd = f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


_K32 = ctypes.WinDLL("kernel32", use_last_error=True)


def acquire_single_instance() -> bool:
    """命名互斥体防止多开。返回 False 表示已有实例在运行。"""
    _K32.CreateMutexW(None, False, APP_NAME + "_Mutex")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def make_tray_icon() -> Image.Image:
    """程序化生成托盘图标 (相机造型), 免去外部 ico 资源。"""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 14, 60, 54], radius=10, fill=(28, 116, 233, 255))
    d.rounded_rectangle([22, 4, 42, 16], radius=4, fill=(28, 116, 233, 255))
    d.ellipse([20, 22, 44, 46], outline="white", width=4)
    d.ellipse([28, 30, 36, 38], fill="white")
    d.ellipse([48, 20, 56, 28], fill="white")
    return img


def copy_image_to_clipboard(img: Image.Image) -> bool:
    try:
        import win32clipboard
    except ImportError:
        return False
    with BytesIO() as buf:
        img.save(buf, "BMP")
        dib = buf.getvalue()[14:]
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()
    return True


# ============================ 标注渲染 (PIL 导出) ============================

def _load_font(size):
    try:
        return ImageFont.truetype("msyh.ttc", size)
    except Exception:
        try:
            return ImageFont.truetype("simhei.ttf", size)
        except Exception:
            return ImageFont.load_default()


def _arrow_head(x0, y0, x1, y1, width):
    """返回箭头头部两条线的端点。"""
    ang = math.atan2(y1 - y0, x1 - x0)
    length = max(12, width * 4)
    spread = 0.45
    pts = []
    for da in (spread, -spread):
        pts.append((x1 - length * math.cos(ang + da),
                    y1 - length * math.sin(ang + da)))
    return pts


def render_annotations(img: Image.Image, annos) -> Image.Image:
    """把标注列表重绘到截图副本上, 返回新图 (不修改原图)。"""
    out = img.copy()
    d = ImageDraw.Draw(out)
    for a in annos:
        color, w = a["color"], a.get("width", PEN_WIDTH)
        t = a["type"]
        if t == "pen":
            pts = [tuple(p) for p in a["points"]]
            if len(pts) == 1:
                x, y = pts[0]
                d.ellipse([x - w, y - w, x + w, y + w], fill=color)
            else:
                d.line(pts, fill=color, width=w, joint="curve")
        elif t == "rect":
            d.rectangle([a["x0"], a["y0"], a["x1"], a["y1"]], outline=color, width=w)
        elif t == "ellipse":
            d.ellipse([a["x0"], a["y0"], a["x1"], a["y1"]], outline=color, width=w)
        elif t == "arrow":
            d.line([a["x0"], a["y0"], a["x1"], a["y1"]], fill=color, width=w)
            for hx, hy in _arrow_head(a["x0"], a["y0"], a["x1"], a["y1"], w):
                d.line([a["x1"], a["y1"], hx, hy], fill=color, width=w)
        elif t == "text":
            d.text((a["x"], a["y"]), a["text"], fill=color, font=_load_font(a["size"]))
    return out


def add_tooltip(widget, text, delay=450):
    """悬停延时显示的小提示气泡, 移开/点击即消失。"""
    state = {"tw": None, "after": None}

    def show():
        state["after"] = None
        if state["tw"] is not None:
            return
        x = widget.winfo_rootx() + widget.winfo_width() // 2
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        tw = tk.Toplevel(widget)
        tw.wm_overrideredirect(True)
        tw.wm_attributes("-topmost", True)
        tk.Label(tw, text=text, bg="#2c2c2c", fg="white",
                 font=(UI_FONT, 9), padx=7, pady=3).pack()
        tw.update_idletasks()
        x -= tw.winfo_width() // 2
        tw.geometry(f"+{max(0, x)}+{y}")
        state["tw"] = tw

    def leave(_e=None):
        if state["after"] is not None:
            widget.after_cancel(state["after"])
            state["after"] = None
        if state["tw"] is not None:
            state["tw"].destroy()
            state["tw"] = None

    def enter(_e=None):
        if state["after"] is None and state["tw"] is None:
            state["after"] = widget.after(delay, show)

    widget.bind("<Enter>", enter)
    widget.bind("<Leave>", leave)
    widget.bind("<ButtonPress-1>", leave, add="+")


COLOR_NAMES = {"#E24B4A": "红色", "#EF9F27": "橙色", "#378ADD": "蓝色",
               "#1D9E75": "绿色", "#2C2C2A": "黑色"}
ACCENT = "#07C160"          # 微信绿

# ---- Windows 原生全局热键 (RegisterHotKey, 不受其他钩子软件干扰) ----
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_NOREPEAT = 0x4000
MOD_MAP = {"ctrl": 0x0002, "alt": 0x0001, "shift": 0x0004, "windows": 0x0008,
           "win": 0x0008}
VK_MAP = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "esc": 0x1B,
          "escape": 0x1B, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
          "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
          "pageup": 0x21, "page up": 0x21, "pagedown": 0x22,
          "page down": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
          "right": 0x27, "printscreen": 0x2C, "print screen": 0x2C,
          "capslock": 0x14, "pause": 0x13}


def hotkey_to_vk_mods(hotkey: str):
    """把 'ctrl+alt+z' 解析为 (修饰键掩码, 虚拟键码)；无法识别返回 None。"""
    mods, key = 0, None
    for part in hotkey.split("+"):
        p = part.strip().lower()
        if not p:
            continue
        if p in MOD_MAP:
            mods |= MOD_MAP[p]
        elif key is None:
            key = p
    if key is None:
        return None
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        vk = VK_MAP.get(key)
        if vk is None:
            return None
    return mods, vk


# ============================ 选区框选 ============================


class RegionSelector:
    """全屏半透明遮罩: 拖拽框选, 松开鼠标立即截图悬浮。Esc/右键取消。"""

    def __init__(self, root: tk.Tk, on_selected, on_cancel):
        self.on_selected = on_selected
        self.on_cancel = on_cancel
        self.region = None      # [x0, y0, x1, y1] 画布坐标 == 屏幕坐标
        self.mode = None        # None / drawing
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)
        self.top.attributes("-alpha", 0.28)
        self.top.configure(bg="black", cursor="crosshair")
        self.top.geometry(f"{self.top.winfo_screenwidth()}x"
                          f"{self.top.winfo_screenheight()}+0+0")

        self.canvas = tk.Canvas(self.top, bg="black", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self._cancel())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.focus_force()

    def _refresh(self):
        self.canvas.delete("ui")
        if not self.region:
            return
        x0, y0, x1, y1 = self.region
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=ACCENT,
                                     width=2, tags="ui")
        # 四角白色圆点手柄 (微信风格)
        for hx, hy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            self.canvas.create_oval(hx - 4, hy - 4, hx + 4, hy + 4,
                                    fill="white", outline=ACCENT, width=1,
                                    tags="ui")
        # 实时尺寸标签
        label = f"{abs(x1 - x0)} × {abs(y1 - y0)}"
        ly = y0 - 20 if y0 >= 22 else y1 + 4
        self.canvas.create_rectangle(x0, ly, x0 + 74, ly + 18,
                                     fill=ACCENT, outline="", tags="ui")
        self.canvas.create_text(x0 + 37, ly + 9, text=label, fill="white",
                                font=(UI_FONT, 10), tags="ui")

    def _on_press(self, e):
        self.region = [e.x, e.y, e.x, e.y]
        self.mode = "drawing"

    def _on_motion(self, e):
        if self.mode == "drawing":
            self.region[2], self.region[3] = e.x, e.y
            self._refresh()

    def _on_release(self, e):
        if self.mode != "drawing":
            return
        x0, y0, x1, y1 = self.region
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if bbox[2] - bbox[0] < MIN_REGION or bbox[3] - bbox[1] < MIN_REGION:
            self.region = None  # 误触, 忽略
            self.mode = None
            self._refresh()
            return
        self._destroy()
        try:
            self.on_selected(bbox)
        except Exception:
            log_error()

    def _cancel(self):
        self._destroy()
        try:
            self.on_cancel()
        except Exception:
            log_error()

    def _destroy(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ============================ 编辑器 ============================

class EditorOverlay:
    """无边框编辑窗口 (微信截图风格): 标注可选中/拖动/删除。
    Enter/完成 确认, Esc 取消, Ctrl+Z 撤销, 右键删除选中标注。"""

    TOOLS = [("pen", "\u270e", "画笔"), ("rect", "\u25ad", "矩形"),
             ("ellipse", "\u25cb", "椭圆"), ("arrow", "\u2192", "箭头"),
             ("text", "T", "文字 (双击已有文字可修改)")]
    def __init__(self, root, img, bbox, on_done, on_cancel=None, annotations=None):
        self.root = root
        self.img = img
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.annos = list(annotations or [])
        self.tool = "pen"
        self.color = COLORS[0]
        self.width = PEN_WIDTH
        self.sel = None          # 选中标注下标
        self.mode = None         # None / move
        self._cur = None         # 进行中的标注
        self._mv = None          # 拖动基准点
        self._cursor = "pencil"
        self._photo = ImageTk.PhotoImage(img)

        x0, y0, x1, y1 = bbox
        sh = root.winfo_screenheight()
        sw = root.winfo_screenwidth()
        below = y1 + TOOLBAR_H + 10 < sh
        top_y = y0 if below else max(0, y0 - TOOLBAR_H - 10)

        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.attributes("-topmost", True)

        self.canvas = tk.Canvas(self.top, width=img.width, height=img.height,
                                highlightthickness=0, bg="black",
                                cursor="crosshair")
        self.bar = tk.Frame(self.top, bg="white",
                            highlightthickness=1, highlightbackground="#e3e3e3")
        self._build_toolbar()
        # 实测工具条需要宽度, 窗口宽度取 max(截图宽, 工具条宽), 杜绝截断
        self.bar.update_idletasks()
        bar_w = self.bar.winfo_reqwidth() + 8
        bar_h = self.bar.winfo_reqheight() + 2
        self.win_w = max(img.width, bar_w)
        win_h = img.height + max(TOOLBAR_H, bar_h)
        pad = max(0, (self.win_w - img.width) // 2)
        ex = min(max(0, x0 - (self.win_w - img.width) // 2),
                 max(0, sw - self.win_w))
        self.top.geometry(f"{self.win_w}x{win_h}+{ex}+{top_y}")
        if below:
            self.canvas.pack(padx=(pad, self.win_w - img.width - pad))
            self.bar.pack(fill="x")
        else:
            self.bar.pack(fill="x")
            self.canvas.pack(padx=(pad, self.win_w - img.width - pad))

        # 绘制截图底图 + 已有标注 (顺序不可少)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self._redraw()

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_right_click)
        self.canvas.bind("<Motion>", self._on_hover)
        self.top.bind("<Return>", lambda e: self._confirm())
        self.top.bind("<Escape>", lambda e: self._cancel())
        self.top.bind("<Control-z>", lambda e: self._undo())
        self.top.bind("<Delete>", self._delete_sel)
        self.top.grab_set()
        self.top.focus_set()

    # ---- 工具条 (微信风格: 白底纯图标, 悬停气泡提示, 绿色主色) ----
    def _sep(self):
        tk.Frame(self.bar, bg="#e6e6e6", width=1
                 ).pack(side="left", fill="y", padx=6, pady=9)

    def _style_tool_lbl(self, lbl, key):
        active = self.tool == key
        lbl.configure(bg="#e8f8ee" if active else "white",
                      fg=ACCENT if active else "#333333")

    def _style_width_lbl(self, lbl, w):
        active = self.width == w
        lbl.configure(bg="#e8f8ee" if active else "white",
                      fg=ACCENT if active else "#333333")

    def _style_color_lbl(self, lbl, c):
        active = self.color == c
        lbl.configure(highlightthickness=2 if active else 1,
                      highlightbackground=ACCENT if active else "#cfcfcf")

    def _build_toolbar(self):
        self._tool_btns = {}
        for key, glyph, name in self.TOOLS:
            lbl = tk.Label(self.bar, text=glyph, bg="white", fg="#333333",
                           font=(UI_FONT, 13), width=3, cursor="hand2")
            lbl.pack(side="left", padx=3, pady=6)
            lbl.bind("<Button-1>", lambda e, k=key: self._set_tool(k))
            lbl.bind("<Enter>", lambda e, w=lbl, k=key:
                     None if self.tool == k else w.configure(bg="#f2f2f2"))
            lbl.bind("<Leave>", lambda e, w=lbl, k=key: self._style_tool_lbl(w, k))
            add_tooltip(lbl, name)
            self._tool_btns[key] = lbl

        self._sep()

        self._width_btns = {}
        for glyph, w, fs in (("•", 2, 9), ("•", 4, 13), ("•", 6, 17)):
            lbl = tk.Label(self.bar, text=glyph, bg="white", fg="#333333",
                           font=(UI_FONT, fs), width=2, cursor="hand2")
            lbl.pack(side="left", padx=2, pady=6)
            lbl.bind("<Button-1>", lambda e, ww=w: self._set_width(ww))
            lbl.bind("<Enter>", lambda e, w=lbl, ww=w:
                     None if self.width == ww else w.configure(bg="#f2f2f2"))
            lbl.bind("<Leave>", lambda e, w=lbl, ww=w: self._style_width_lbl(w, ww))
            add_tooltip(lbl, f"{w}px 线宽")
            self._width_btns[w] = lbl

        self._sep()

        self._color_btns = {}
        for c in COLORS:
            lbl = tk.Label(self.bar, bg=c, width=2, cursor="hand2")
            lbl.pack(side="left", padx=3, pady=8)
            lbl.bind("<Button-1>", lambda e, col=c: self._set_color(col))
            add_tooltip(lbl, COLOR_NAMES[c])
            self._color_btns[c] = lbl

        self._sep()

        lbl_undo = tk.Label(self.bar, text="↺", bg="white", fg="#333333",
                            font=(UI_FONT, 13), width=3, cursor="hand2")
        lbl_undo.pack(side="left", padx=3, pady=6)
        lbl_undo.bind("<Button-1>", lambda e: self._undo())
        add_tooltip(lbl_undo, "撤销 (Ctrl+Z)")

        lbl_clear = tk.Label(self.bar, text="清空", bg="white", fg="#333333",
                             font=(UI_FONT, 10), padx=6, pady=6, cursor="hand2")
        lbl_clear.pack(side="left", padx=2, pady=6)
        lbl_clear.bind("<Button-1>", lambda e: self._clear())
        add_tooltip(lbl_clear, "清空所有标注")

        lbl_cancel = tk.Label(self.bar, text="✕", bg="white", fg="#666666",
                              font=(UI_FONT, 13), width=3, cursor="hand2")
        lbl_cancel.pack(side="right", padx=6, pady=6)
        lbl_cancel.bind("<Button-1>", lambda e: self._cancel())
        lbl_cancel.bind("<Enter>", lambda e: lbl_cancel.configure(
            bg="#fdeaea", fg="#e24b4a"))
        lbl_cancel.bind("<Leave>", lambda e: lbl_cancel.configure(bg="white", fg="#666666"))
        add_tooltip(lbl_cancel, "取消 (Esc)")

        lbl_ok = tk.Label(self.bar, text="✓", bg=ACCENT, fg="white",
                          font=(UI_FONT, 13), width=3, cursor="hand2")
        lbl_ok.pack(side="right", padx=2, pady=6)
        lbl_ok.bind("<Button-1>", lambda e: self._confirm())
        lbl_ok.bind("<Enter>", lambda e: lbl_ok.configure(bg="#06ad56"))
        lbl_ok.bind("<Leave>", lambda e: lbl_ok.configure(bg=ACCENT))
        add_tooltip(lbl_ok, "完成 (Enter)")

        self._set_tool(self.tool)
        self._set_width(self.width)
        self._set_color(self.color)

    def _set_tool(self, key):
        self.tool = key
        for k, lbl in self._tool_btns.items():
            self._style_tool_lbl(lbl, k)
        self._cursor = "pencil" if key == "pen" else (
            "tcross" if key == "text" else "crosshair")
        self.canvas.configure(cursor=self._cursor)

    def _set_width(self, w):
        self.width = w
        for ww, lbl in self._width_btns.items():
            self._style_width_lbl(lbl, ww)

    def _set_color(self, color):
        self.color = color
        for c, lbl in self._color_btns.items():
            self._style_color_lbl(lbl, c)

    # ---- 标注绘制 (canvas) ----
    def _draw_item(self, a, tag):
        c = self.canvas
        color, w = a["color"], a.get("width", PEN_WIDTH)
        t = a["type"]
        if t == "pen":
            pts = [v for p in a["points"] for v in p]
            if len(pts) >= 4:
                c.create_line(*pts, fill=color, width=w, smooth=True,
                              capstyle="round", joinstyle="round", tags=tag)
        elif t == "rect":
            c.create_rectangle(a["x0"], a["y0"], a["x1"], a["y1"],
                               outline=color, width=w, tags=tag)
        elif t == "ellipse":
            c.create_oval(a["x0"], a["y0"], a["x1"], a["y1"],
                          outline=color, width=w, tags=tag)
        elif t == "arrow":
            c.create_line(a["x0"], a["y0"], a["x1"], a["y1"],
                          fill=color, width=w, tags=tag, capstyle="round")
            for hx, hy in _arrow_head(a["x0"], a["y0"], a["x1"], a["y1"], w):
                c.create_line(a["x1"], a["y1"], hx, hy,
                              fill=color, width=w, tags=tag, capstyle="round")
        elif t == "text":
            c.create_text(a["x"], a["y"], text=a["text"], fill=color,
                          font=(UI_FONT, a["size"]), anchor="nw", tags=tag)

    def _redraw(self):
        self.canvas.delete("anno")
        for i, a in enumerate(self.annos):
            self._draw_item(a, (f"a{i}", "anno"))
        self._refresh_sel()

    def _refresh_sel(self):
        self.canvas.delete("sel")
        if self.sel is None or self.sel >= len(self.annos):
            return
        bb = self.canvas.bbox(f"a{self.sel}")
        if bb:
            self.canvas.create_rectangle(*bb, outline=ACCENT, width=1,
                                         dash=(4, 2), tags="sel")

    # ---- 选中 / 移动 / 删除 ----
    def _hit_anno(self, x, y):
        hit = None
        for it in self.canvas.find_overlapping(x - 4, y - 4, x + 4, y + 4):
            for t in self.canvas.gettags(it):
                if len(t) > 1 and t[0] == "a" and t[1:].isdigit():
                    hit = int(t[1:])  # 迭代顺序自底向上, 取最上层
        return hit

    @staticmethod
    def _shift_anno(a, dx, dy):
        if a["type"] == "pen":
            a["points"] = [(x + dx, y + dy) for x, y in a["points"]]
        elif a["type"] == "text":
            a["x"] += dx
            a["y"] += dy
        else:
            a["x0"] += dx
            a["y0"] += dy
            a["x1"] += dx
            a["y1"] += dy

    def _delete_sel(self, e=None):
        if self.sel is not None and self.sel < len(self.annos):
            self.annos.pop(self.sel)
            self.sel = None
            self._redraw()

    # ---- 鼠标交互 ----
    def _on_press(self, e):
        hit = self._hit_anno(e.x, e.y)
        if hit is not None:
            # 任何工具下, 按住已有标注即可拖动 (文字用双击进入编辑)
            self.sel = hit
            self._refresh_sel()
            self.mode = "move"
            self._mv = (e.x, e.y)
            return
        self.sel = None
        self._refresh_sel()
        if self.tool == "text":
            self._input_text(e.x, e.y)
            return
        if self.tool == "pen":
            self._cur = {"type": "pen", "points": [(e.x, e.y)],
                         "color": self.color, "width": self.width}
        else:
            self._cur = {"type": self.tool, "x0": e.x, "y0": e.y,
                         "x1": e.x, "y1": e.y, "color": self.color,
                         "width": self.width}

    def _on_motion(self, e):
        if self.mode == "move" and self.sel is not None:
            dx = e.x - self._mv[0]
            dy = e.y - self._mv[1]
            self._mv = (e.x, e.y)
            self._shift_anno(self.annos[self.sel], dx, dy)
            self._redraw()
            return
        if not self._cur:
            return
        if self._cur["type"] == "pen":
            self._cur["points"].append((e.x, e.y))
            pts = [v for p in self._cur["points"][-2:] for v in p]
            if len(pts) == 4:
                self.canvas.create_line(*pts, fill=self._cur["color"],
                                        width=self._cur["width"], smooth=True,
                                        capstyle="round", tags="temp")
        else:
            self._cur["x1"], self._cur["y1"] = e.x, e.y
            self.canvas.delete("temp")
            self._draw_item(self._cur, "temp")

    def _on_release(self, e):
        if self.mode == "move":
            self.mode = None
            return
        if not self._cur:
            return
        a, self._cur = self._cur, None
        if a["type"] == "pen":
            if len(a["points"]) < 2:
                return
        elif abs(a["x1"] - a["x0"]) < 3 and abs(a["y1"] - a["y0"]) < 3:
            self.canvas.delete("temp")
            return
        self.annos.append(a)
        self.canvas.delete("temp")
        self._redraw()

    def _on_double(self, e):
        """双击文字标注进入再编辑。"""
        hit = self._hit_anno(e.x, e.y)
        if hit is not None and self.annos[hit]["type"] == "text":
            self.mode = None
            self._cur = None
            a = self.annos[hit]
            self._input_text(a["x"], a["y"], index=hit, initial=a["text"])

    def _on_hover(self, e):
        if self._cur or self.mode == "move":
            return
        hit = self._hit_anno(e.x, e.y)
        self.canvas.configure(cursor="fleur" if hit is not None else self._cursor)

    def _on_right_click(self, e):
        hit = self._hit_anno(e.x, e.y)
        if hit is not None:
            self.annos.pop(hit)
            self.sel = None
            self._redraw()

    # ---- 文字输入 / 再编辑 ----
    def _input_text(self, x, y, index=None, initial=""):
        popup = tk.Toplevel(self.top)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(highlightthickness=1, highlightbackground="#1c74e9")
        entry = tk.Entry(popup, font=(UI_FONT, TEXT_SIZE), width=26)
        entry.pack(padx=3, pady=3)
        entry.insert(0, initial)
        sx, sy = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        popup.geometry(f"+{sx + x}+{sy + y}")

        def commit(_e=None):
            text = entry.get().strip()
            popup.destroy()
            if not text:
                return
            if index is None:
                self.annos.append({"type": "text", "x": x, "y": y,
                                   "text": text, "color": self.color,
                                   "size": TEXT_SIZE})
            else:
                self.annos[index]["text"] = text
            self._redraw()

        popup.bind("<Return>", commit)
        popup.bind("<Escape>", lambda e: popup.destroy())
        entry.bind("<FocusOut>", commit)
        entry.focus_set()

    # ---- 操作 ----
    def _undo(self):
        if self.annos:
            self.annos.pop()
            self.sel = None
            self._redraw()

    def _clear(self):
        if self.annos:
            self.annos = []
            self.sel = None
            self._redraw()

    def _confirm(self):
        img = render_annotations(self.img, self.annos)
        cb, annos = self.on_done, self.annos
        self._destroy()
        if cb:
            try:
                cb(img, annos)
            except Exception:
                log_error()

    def _cancel(self):
        cb = self.on_cancel
        self._destroy()
        if cb:
            try:
                cb()
            except Exception:
                log_error()

    def _destroy(self):
        try:
            self.top.grab_release()
        except Exception:
            pass
        try:
            self.top.destroy()
        except Exception:
            pass


# ============================ 悬浮贴图 ============================

class FloatImage:
    """无边框置顶的悬浮截图窗口, 支持再次编辑标注。"""

    def __init__(self, app, img: Image.Image, bbox, annotations=None):
        self.app = app
        self.root = app.root
        self.original = img
        self.annotations = list(annotations or [])
        self.bbox = tuple(bbox)
        self.scale = 1.0
        self._photo = None

        self.top = tk.Toplevel(self.root)
        self.top.overrideredirect(True)      # 无边框, 无任何窗口装饰
        self.top.attributes("-topmost", True)
        self.top.geometry(f"{img.width}x{img.height}+{bbox[0]}+{bbox[1]}")

        self.label = tk.Label(self.top, bd=0, highlightthickness=0)
        self.label.pack()
        self._render()

        self.label.bind("<ButtonPress-1>", self._on_move_start)
        self.label.bind("<B1-Motion>", self._on_move)
        self.label.bind("<Double-Button-1>", lambda e: self.close())
        self.label.bind("<MouseWheel>", self._on_zoom)
        self.label.bind("<Button-4>", self._on_zoom)
        self.label.bind("<Button-5>", self._on_zoom)
        self.label.bind("<Button-3>", self._on_menu)
        self.top.bind("<Escape>", lambda e: self.close())
        self.top.focus_set()

    def _render(self):
        w = max(1, int(self.original.width * self.scale))
        h = max(1, int(self.original.height * self.scale))
        resized = self.original.resize((w, h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        self.label.configure(image=self._photo, width=w, height=h)

    def _on_move_start(self, e):
        self._dx = e.x_root - self.top.winfo_x()
        self._dy = e.y_root - self.top.winfo_y()

    def _on_move(self, e):
        self.top.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _on_zoom(self, e):
        if getattr(e, "num", None) == 4 or getattr(e, "delta", 0) > 0:
            factor = 1.1
        else:
            factor = 1 / 1.1
        new_scale = min(MAX_SCALE, max(MIN_SCALE, self.scale * factor))
        if abs(new_scale - self.scale) < 1e-6:
            return
        self.scale = new_scale
        self._render()

    def _on_menu(self, e):
        menu = tk.Menu(self.top, tearoff=0)
        menu.add_command(label="编辑此贴图", command=lambda: self.app.open_float_editor(self))
        menu.add_command(label="保存为 PNG...", command=self._save)
        menu.add_command(label="复制到剪贴板", command=self._copy)
        menu.add_separator()
        menu.add_command(label="关闭 (双击/Esc)", command=self.close)
        menu.tk_popup(e.x_root, e.y_root)

    def _save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile="screenshot.png",
            filetypes=[("PNG 图片", "*.png")])
        if path:
            self.original.save(path)

    def _copy(self):
        if not copy_image_to_clipboard(self.original):
            messagebox.showinfo("提示", "复制图片需要 pywin32 支持")

    def apply_edit(self, img: Image.Image, annotations):
        """编辑器确认后更新贴图内容与标注。"""
        self.original = img
        self.annotations = list(annotations)
        self.scale = 1.0
        self.top.geometry(f"{img.width}x{img.height}"
                          f"+{self.top.winfo_x()}+{self.top.winfo_y()}")
        self._render()

    def close(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ============================ 设置窗口 ============================

class SettingsWindow:
    """可视化设置: 直接按键录入快捷键。"""

    def __init__(self, app):
        self.app = app
        self.pending = {}       # 待应用的快捷键改动 {field: hotkey}
        self.capturing = None   # 正在录入的字段
        self._hook = None

        self.win = tk.Toplevel(app.root)
        self.win.title(f"{APP_TITLE} - 设置")
        self.win.resizable(False, False)
        self.win.attributes("-topmost", True)
        self.win.configure(padx=20, pady=16)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.bind("<Destroy>", self._on_destroy)

        frm = ttk.Frame(self.win)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="截图快捷键").grid(row=0, column=0, sticky="w", pady=6, padx=(0, 12))
        self.var_capture = tk.StringVar(value=app.config["hotkey"].upper())
        self.ent_capture = ttk.Entry(frm, textvariable=self.var_capture, width=22, state="readonly")
        self.ent_capture.grid(row=0, column=1, pady=6)
        self.btn_capture = ttk.Button(frm, text="修改", width=8, command=lambda: self._start_capture("hotkey"))
        self.btn_capture.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(frm, text="退出快捷键").grid(row=1, column=0, sticky="w", pady=6, padx=(0, 12))
        self.var_quit = tk.StringVar(value=app.config["quit_hotkey"].upper())
        self.ent_quit = ttk.Entry(frm, textvariable=self.var_quit, width=22, state="readonly")
        self.ent_quit.grid(row=1, column=1, pady=6)
        self.btn_quit = ttk.Button(frm, text="修改", width=8, command=lambda: self._start_capture("quit_hotkey"))
        self.btn_quit.grid(row=1, column=2, padx=(8, 0))

        self.var_autostart = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(frm, text="开机自动启动", variable=self.var_autostart)\
            .grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 2))

        self.lbl_tip = ttk.Label(frm, text="点击「修改」后直接按下想要的组合键, Esc 取消",
                                 foreground="#888888")
        self.lbl_tip.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 10))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="e")
        ttk.Button(btns, text="保存", command=self._save).pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=self.close).pack(side="left", padx=4)

        self.win.update_idletasks()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"+{(sw - self.win.winfo_width()) // 2}+{(sh - self.win.winfo_height()) // 2}")
        self.win.grab_set()
        self.win.focus_set()

    # ---- 快捷键录入 ----
    def _start_capture(self, field):
        if self._hook or keyboard is None:
            if keyboard is None:
                messagebox.showerror("错误", "keyboard 库不可用", parent=self.win)
            return
        self.capturing = field
        self.lbl_tip.configure(text="请直接按下想要的组合键…  (Esc 取消)", foreground="#1c74e9")
        self.btn_capture.configure(state="disabled")
        self.btn_quit.configure(state="disabled")
        self._hook = keyboard.hook(self._on_capture_key)

    def _on_capture_key(self, e):
        if self.capturing is None:
            return  # 钩子残留时静默忽略, 绝不能在键盘监听线程里抛异常
        try:
            if e.event_type != "down":
                return
            mods = {m for m in ("ctrl", "alt", "shift", "windows")
                    if keyboard.is_pressed(m)}
            if e.name in mods:
                return  # 还在按修饰键, 等待最终按键
            if e.name == "esc" and not mods:
                return self._stop_capture(cancel=True)
            hotkey = keyboard.get_hotkey_name(sorted(mods | {e.name}))
            other = "quit_hotkey" if self.capturing == "hotkey" else "hotkey"
            if hotkey == self.app.config[other]:
                messagebox.showwarning(
                    "冲突",
                    f"该组合键已被「{'截图' if other == 'hotkey' else '退出'}」占用",
                    parent=self.win)
                return self._stop_capture(cancel=True)
            self.pending[self.capturing] = hotkey
            {"hotkey": self.var_capture,
             "quit_hotkey": self.var_quit}[self.capturing].set(hotkey.upper())
            self._stop_capture()
        except Exception:
            log_error()
            self._stop_capture(cancel=True)

    def _stop_capture(self, cancel=False):
        if self._hook:
            try:
                keyboard.unhook(self._hook)
            except Exception:
                pass
            self._hook = None
        self.capturing = None
        self.btn_capture.configure(state="normal")
        self.btn_quit.configure(state="normal")
        if cancel:
            # 还原显示为已保存值
            self.var_capture.set(self.app.config["hotkey"].upper())
            self.var_quit.set(self.app.config["quit_hotkey"].upper())
        self.lbl_tip.configure(text="点击「修改」后直接按下想要的组合键, Esc 取消", foreground="#888888")

    # ---- 保存 ----
    def _save(self):
        want = self.var_autostart.get()
        try:
            if want != get_autostart():
                set_autostart(want)
        except OSError as e:
            messagebox.showerror("错误", f"设置开机自启失败:\n{e}", parent=self.win)
            return
        changed = bool(self.pending)
        if self.pending:
            self.app.config.update(self.pending)
        if want != self.app.config["autostart"]:
            self.app.config["autostart"] = want
            changed = True
        if changed:
            save_config(self.app.config)
            self.app.register_hotkeys()
            status = self.app.wait_hotkey_status(1.0)
            if status and status.get("fail"):
                messagebox.showwarning(
                    "快捷键冲突",
                    "以下快捷键注册失败（可能被其他程序占用）：\n"
                    + "\n".join(status["fail"])
                    + "\n\n请换一个组合键。", parent=self.win)
                return
        self.close()

    def close(self):
        try:
            self.win.grab_release()
            self.win.destroy()
        except Exception:
            pass

    def _on_destroy(self, e):
        if e.widget is self.win:
            self._stop_capture(cancel=True)


# ============================ 主程序 ============================

class App:
    def __init__(self):
        if not acquire_single_instance():
            ctypes.windll.user32.MessageBoxW(
                0, f"{APP_TITLE} 已在运行中, 请查看系统托盘图标。", APP_TITLE, 0x40)
            sys.exit(0)

        self.config = load_config()
        if not os.path.exists(CONFIG_PATH):
            save_config(self.config)
        # 每次启动同步: 让自启注册表与配置保持一致 (自愈)
        try:
            if self.config["autostart"] != get_autostart():
                set_autostart(self.config["autostart"])
        except OSError:
            log_error()

        self.root = tk.Tk()
        # tkinter 回调异常默认走 stderr (--noconsole 下丢失), 必须落盘
        self.root.report_callback_exception = lambda t, v, tb: _write_log(
            "".join(traceback.format_exception(t, v, tb)))
        # 所有窗口(含设置)统一使用应用图标, 覆盖 tkinter 默认羽毛图标
        self._app_icon = ImageTk.PhotoImage(make_tray_icon())
        self.root.iconphoto(True, self._app_icon)
        self.root.withdraw()          # 主窗口完全隐藏, 程序无任何可见窗口
        self.selector = None
        self.editor = None
        self._last_float = None
        self.settings_win = None
        self.tray = None
        self.hotkey_handles = []

        self.register_hotkeys()
        self.setup_tray()
        if "--selftest" in sys.argv:
            self.root.after(1500, self._selftest)
        self.root.mainloop()

    # ---- 全局热键 (Windows 原生 RegisterHotKey, 独立线程消息循环) ----
    def register_hotkeys(self):
        self._hotkey_gen = getattr(self, "_hotkey_gen", 0) + 1
        gen = self._hotkey_gen
        self._hotkey_status = None
        tid = getattr(self, "_hotkey_tid", None)
        if tid:  # 通知旧线程退出 (退出时会注销热键)
            ctypes.windll.user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            deadline = time.time() + 1.0
            while self._hotkey_tid is not None and time.time() < deadline:
                time.sleep(0.01)
        actions = [(self.config["hotkey"], "capture"),
                   (self.config["quit_hotkey"], "quit")]
        threading.Thread(target=self._hotkey_loop, args=(gen, actions),
                         daemon=True).start()

    def _hotkey_loop(self, gen, actions):
        user32 = ctypes.windll.user32
        self._hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
        ok, fail, ids = [], [], {}
        for i, (hotkey, action) in enumerate(actions):
            parsed = hotkey_to_vk_mods(hotkey)
            if parsed is None:
                fail.append(f"{hotkey}（无法识别）")
                continue
            mods, vk = parsed
            kid = 0xB100 + i
            if user32.RegisterHotKey(None, kid, mods | MOD_NOREPEAT, vk):
                ids[kid] = action
                ok.append(hotkey)
            else:
                fail.append(hotkey)  # 注册失败 = 被其他程序占用
        self._hotkey_status = {"ok": ok, "fail": fail}
        if fail:
            _write_log("[hotkey] 注册失败(可能被占用): " + ", ".join(fail))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and gen == self._hotkey_gen:
                action = ids.get(msg.wParam)
                if action == "capture":
                    self.root.after(0, self._start_selector)
                elif action == "quit":
                    self.root.after(0, self.quit)
        for kid in ids:
            user32.UnregisterHotKey(None, kid)
        self._hotkey_tid = None

    def wait_hotkey_status(self, timeout=1.0):
        """等待热键注册结果, 返回 {'ok': [...], 'fail': [...]} 或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._hotkey_status is not None:
                return self._hotkey_status
            time.sleep(0.02)
        return None

    # ---- 托盘 ----
    def setup_tray(self):
        try:
            import pystray
        except ImportError:
            log_error()
            return

        def on_ui(fn):  # 托盘线程 -> tkinter 主线程
            return lambda *a: self.root.after(0, fn)

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: f"截图  ({self.config['hotkey'].upper()})",
                on_ui(self._start_selector), default=True),
            pystray.MenuItem("设置...", on_ui(self.open_settings)),
            pystray.MenuItem("开机自启",
                             on_ui(self._toggle_autostart),
                             checked=lambda item: get_autostart()),
            pystray.MenuItem("退出", on_ui(self.quit)),
        )
        self.tray = pystray.Icon(APP_NAME, make_tray_icon(), APP_TITLE, menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    def _toggle_autostart(self):
        try:
            new_state = not get_autostart()
            set_autostart(new_state)
            self.config["autostart"] = new_state
            save_config(self.config)
        except OSError as e:
            messagebox.showerror("错误", f"设置开机自启失败:\n{e}")
        if self.tray:
            self.tray.update_menu()

    def open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.win.focus_force()
                return
            except Exception:
                self.settings_win = None
        self.settings_win = SettingsWindow(self)

    # ---- 截图流程: 框选 -> 直接贴图; 编辑按需(右键贴图) ----
    def _start_selector(self):
        if self.editor is not None:
            return  # 编辑中, 忽略
        if self.selector is not None:
            # 自愈: 残留的已销毁遮罩不应永久阻塞截图
            try:
                if self.selector.top.winfo_exists():
                    return  # 正在框选中, 忽略重复触发
            except Exception:
                pass
            self.selector = None
        self.selector = RegionSelector(
            self.root, self._on_region_confirmed, self._cancel_selector)

    def _cancel_selector(self):
        self.selector = None

    def _on_region_confirmed(self, bbox):
        self.selector = None

        def grab():
            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
                self._last_float = FloatImage(self, img, bbox, annotations=[])
            except Exception:
                log_error()
        self.root.after(150, grab)  # 等遮罩销毁后再截图

    def _editor_cancel(self):
        self.editor = None

    def _open_editor(self, img, bbox, annotations, on_done):
        self.editor = EditorOverlay(
            self.root, img, bbox, on_done=on_done,
            on_cancel=self._editor_cancel, annotations=annotations)

    def open_float_editor(self, f: FloatImage):
        if self.editor is not None:
            return
        x, y = f.top.winfo_x(), f.top.winfo_y()
        bbox = (x, y, x + f.original.width, y + f.original.height)
        self.editor = EditorOverlay(
            self.root, f.original, bbox,
            on_done=lambda img, annos, _f=f: self._float_edited(_f, img, annos),
            on_cancel=self._editor_cancel,
            annotations=f.annotations)

    def _float_edited(self, f: FloatImage, img, annotations):
        self.editor = None
        f.apply_edit(img, annotations)

    # ---- 自检: 验证编辑器+导出+悬浮全链路, 结果写 error.log ----
    def _selftest(self):
        try:
            img = ImageGrab.grab(bbox=(60, 60, 460, 360))
            annos = [{"type": "rect", "x0": 10, "y0": 10, "x1": 100, "y1": 80,
                      "color": "#E24B4A", "width": PEN_WIDTH},
                     {"type": "text", "x": 20, "y": 100, "text": "测试文字",
                      "color": "#378ADD", "size": TEXT_SIZE}]
            exported = render_annotations(img, annos)
            _write_log(f"[selftest] export size: {exported.size}")
            st = self.wait_hotkey_status(1.5)
            _write_log(f"[selftest] hotkey register status: {st}")

            def _done(i, a):
                self._last_float = FloatImage(
                    self, i, (60, 60, 60 + i.width, 60 + i.height), annotations=a)
            self._open_editor(img, (60, 60, 460, 360), annos, on_done=_done)
            shown = bool(self.editor and self.editor.top.winfo_exists())
            _write_log(f"[selftest] editor shown: {shown}")
            if shown:
                # 断言底图已绘制 (防止画布只剩黑底)
                n_img = len([it for it in self.editor.canvas.find_all()
                             if self.editor.canvas.type(it) == "image"])
                _write_log(f"[selftest] canvas image items: {n_img}")
                self.editor.top.after(700, self._selftest_confirm)
                return
            self.root.after(200, self.quit)
        except Exception:
            _write_log("[selftest] EXC:\n" + traceback.format_exc())
            self.root.after(200, self.quit)

    def _selftest_confirm(self):
        try:
            self.editor._confirm()
            ok = bool(self._last_float and self._last_float.top.winfo_exists())
            _write_log(f"[selftest] float image alive: {ok}")
        except Exception:
            _write_log("[selftest] EXC:\n" + traceback.format_exc())
        self.root.after(200, self.quit)

    def quit(self):
        if self.editor is not None:
            self.editor._destroy()
        if self.settings_win is not None:
            self.settings_win.close()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    App()
