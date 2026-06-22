#!/usr/bin/env python3
"""
Jira JSM Queue Overlay
======================
A lightweight Windows desktop overlay that monitors your Jira Service Management
queues in real time.

Requirements
------------
  Python 3.9+ (Windows)  — https://python.org
  pip install requests pystray Pillow   (auto-installed on first run)

Quick start
-----------
  Run:  pythonw jira_overlay.pyw   (or double-click launch_overlay.bat)
  First launch opens a setup dialog asking for:
    • Jira domain   e.g. yourcompany.atlassian.net
    • Email         your Atlassian account email
    • API token     generate at id.atlassian.com/manage-profile/security/api-tokens

  The overlay then discovers your service desks and queues automatically.
  You choose which queue is the "alert queue" — the one that pops the overlay
  and fires desktop notifications when new tickets arrive.

Features
--------
  • Live queue counts, auto-refreshing (default 30 s, configurable)
  • Desktop notification + sound when new tickets land in your alert queue
  • Per-ticket deduplication — one alert per ticket, not one per refresh
  • Snooze alerts for 15 min / 30 min / 1 hour
  • Tickets completed today counter with SLA compliance %
  • System tray icon with count badge
  • Corner snapping, position memory, fade in/out
  • Configurable transparency, width, always-visible dashboard mode
  • Right-click any queue row to open it in Jira

Configuration is stored in jira_config.json next to this script.
"""

import ctypes
import ctypes.wintypes
import importlib.util
import json
import os
import struct
import subprocess
import sys
import math
import threading
import time
import webbrowser
import winreg
import winsound
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Dependency bootstrap — runs before any third-party imports
# ---------------------------------------------------------------------------

_DEPS = [("requests", "requests"), ("PIL", "Pillow"), ("pystray", "pystray")]

def _bootstrap():
    missing = [(mod, pkg) for mod, pkg in _DEPS
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    win = tk.Tk()
    win.title("Jira Overlay — First Run Setup")
    win.configure(bg="#1a1a2e")
    win.resizable(False, False)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"360x130+{sw//2-180}+{sh//2-65}")

    tk.Label(win, text="Installing required packages…",
             font=("Segoe UI", 11, "bold"), fg="#e2e8f0", bg="#1a1a2e").pack(pady=(22, 4))
    lbl = tk.Label(win, text="", font=("Segoe UI", 9), fg="#a0aec0", bg="#1a1a2e")
    lbl.pack()

    track = tk.Frame(win, bg="#2d3748", height=5)
    track.pack(fill="x", padx=24, pady=(12, 0))
    bar = tk.Frame(track, bg="#0052cc", height=5, width=0)
    bar.place(x=0, y=0, height=5)
    win.update()

    for i, (mod, pkg) in enumerate(missing):
        lbl.config(text=f"pip install {pkg}")
        win.update()
        subprocess.call(
            [sys.executable, "-m", "pip", "install", "--quiet", pkg],
            creationflags=0x08000000,
        )
        track.update_idletasks()
        bar.place(width=int(track.winfo_width() * (i + 1) / len(missing)))
        win.update()

    lbl.config(text="All done — starting overlay…")
    win.update()
    win.after(900, win.destroy)
    win.mainloop()

_bootstrap()

# Third-party imports — guaranteed present after bootstrap
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image, ImageDraw, ImageFont
import pystray

CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jira_config.json")
STARTUP_NAME = "JiraOverlay"
STARTUP_REG  = r"Software\Microsoft\Windows\CurrentVersion\Run"
SNAP_MARGIN   = 60   # px from a corner to trigger snap


def corner_positions(sw, sh, w, h):
    return {
        "top-left":     (10,          10),
        "top-right":    (sw - w - 10, 10),
        "bottom-left":  (10,          sh - h - 60),
        "bottom-right": (sw - w - 20, sh - h - 60),
    }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def send_toast(title: str, msg: str):
    t = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
    m = msg.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;").replace('"',"&quot;")
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$id  = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml('<toast duration="short"><visual><binding template="ToastGeneric"><text hint-maxLines="1">{t}</text><text>{m}</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show((New-Object Windows.UI.Notifications.ToastNotification($doc)))
"""
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-NonInteractive", "-Command", ps],
        creationflags=0x08000000,
    )


def play_alert():
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def startup_enabled() -> bool:
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_READ)
        winreg.QueryValueEx(k, STARTUP_NAME)
        winreg.CloseKey(k)
        return True
    except FileNotFoundError:
        return False


def set_startup(enable: bool):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_SET_VALUE)
        if enable:
            winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ,
                              f'pythonw "{os.path.abspath(__file__)}"')
        else:
            try:
                winreg.DeleteValue(k, STARTUP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(k)
    except Exception:
        pass


def get_monitor_rect(x, y):
    """Return (left, top, right, bottom) of the monitor nearest to (x, y)."""
    try:
        pt  = ctypes.wintypes.POINT(x, y)
        mon = ctypes.windll.user32.MonitorFromPoint(pt, 2)
        buf = ctypes.create_string_buffer(40)
        ctypes.c_uint32.from_buffer(buf, 0).value = 40
        if ctypes.windll.user32.GetMonitorInfoW(mon, buf):
            _, ml, mt, mr, mb = struct.unpack_from("5i", buf, 0)
            return ml, mt, mr, mb
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tray icon image
# ---------------------------------------------------------------------------

def _make_tray_image(count: int = 0, alert: bool = False) -> Image.Image:
    size  = 128
    img   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    color = (220, 80, 60) if alert else (0, 82, 204)
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)
    text  = str(count) if count < 100 else "99+"
    font  = None
    for path, pts in [("C:/Windows/Fonts/arialbd.ttf", 64),
                      ("C:/Windows/Fonts/arial.ttf",   64),
                      ("C:/Windows/Fonts/calibrib.ttf", 64)]:
        try:
            font = ImageFont.truetype(path, pts); break
        except Exception:
            pass
    if font is None:
        try:    font = ImageFont.load_default(size=56)
        except Exception: font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
              text, fill=(255, 255, 255), font=font)
    return img


# ---------------------------------------------------------------------------
# Setup window
# ---------------------------------------------------------------------------

class SetupWindow:
    def __init__(self):
        self.root   = tk.Tk()
        self.result = None
        self.root.title("Jira Overlay — Setup")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(False, False)
        self._build()
        self.root.mainloop()

    def _build(self):
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"430x295+{sw//2-215}+{sh//2-147}")

        tk.Label(self.root, text="Jira Overlay Setup",
                 font=("Segoe UI", 14, "bold"), fg="#e2e8f0", bg="#1a1a2e"
                 ).grid(row=0, column=0, columnspan=2, pady=(20, 16))

        rows = [("Jira Domain", "yourcompany.atlassian.net", False),
                ("Email",       "you@company.com",           False),
                ("API Token",   "paste your token here",     True)]
        self.vars = {}
        for i, (lbl, hint, secret) in enumerate(rows, 1):
            tk.Label(self.root, text=lbl + ":", font=("Segoe UI", 9),
                     fg="#a0aec0", bg="#1a1a2e", anchor="e"
                     ).grid(row=i, column=0, sticky="e", padx=(20, 8), pady=6)
            var = tk.StringVar()
            e = tk.Entry(self.root, textvariable=var, width=36,
                         show="*" if secret else "",
                         bg="#2d3748", fg="#4a5568", insertbackground="white",
                         relief="flat", font=("Segoe UI", 9))
            e.grid(row=i, column=1, sticky="ew", padx=(0, 20), pady=6, ipady=5)
            e.insert(0, hint)
            e.bind("<FocusIn>",  lambda ev, w=e, h=hint: (w.get()==h) and (w.delete(0,"end") or w.config(fg="#e2e8f0")))
            e.bind("<FocusOut>", lambda ev, w=e, h=hint: (not w.get()) and (w.insert(0,h) or w.config(fg="#4a5568")))
            self.vars[lbl] = (var, hint)

        url = "https://id.atlassian.com/manage-profile/security/api-tokens"
        link = tk.Label(self.root,
                        text="🔑  Generate an API token at id.atlassian.com",
                        font=("Segoe UI", 8, "underline"), fg="#0052cc", bg="#1a1a2e",
                        cursor="hand2")
        link.grid(row=4, column=0, columnspan=2, pady=(0, 8))
        link.bind("<ButtonRelease-1>", lambda e: webbrowser.open(url))
        link.bind("<Enter>", lambda e: link.config(fg="#3399ff"))
        link.bind("<Leave>", lambda e: link.config(fg="#0052cc"))
        tk.Button(self.root, text="Save & Launch", command=self._save,
                  bg="#0052cc", fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=8, cursor="hand2",
                  activebackground="#0040a0", activeforeground="white"
                  ).grid(row=5, column=0, columnspan=2, pady=(4, 20))
        self.root.columnconfigure(1, weight=1)

    def _save(self):
        vals = {}
        for lbl, (var, hint) in self.vars.items():
            v = var.get().strip()
            if v == hint or not v:
                messagebox.showerror("Missing", f"{lbl} is required.", parent=self.root)
                return
            vals[lbl] = v
        domain = vals["Jira Domain"].strip("/").replace("https://","").replace("http://","")
        self.result = {"domain": domain, "email": vals["Email"], "token": vals["API Token"]}
        save_config(self.result)
        self.root.destroy()


# ---------------------------------------------------------------------------
# Service desk picker
# ---------------------------------------------------------------------------

class PickDeskWindow:
    def __init__(self, desks):
        self.root   = tk.Tk()
        self.result = None
        self.root.title("Jira Overlay — Pick Desk")
        self.root.configure(bg="#1a1a2e")
        self.root.resizable(False, False)
        self._build(desks)
        self.root.mainloop()

    def _build(self, desks):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"360x{90+len(desks)*44}+{sw//2-180}+{sh//2-100}")
        tk.Label(self.root, text="Which service desk to monitor?",
                 font=("Segoe UI", 11, "bold"), fg="#e2e8f0", bg="#1a1a2e"
                 ).pack(pady=(18, 10))
        for d in desks:
            tk.Button(self.root, text=f"{d['projectName']}  ({d['projectKey']})",
                      command=lambda x=d: self._pick(x),
                      bg="#2d3748", fg="#e2e8f0", font=("Segoe UI", 10),
                      relief="flat", padx=12, pady=8, cursor="hand2",
                      activebackground="#4a5568", activeforeground="white"
                      ).pack(fill="x", padx=24, pady=4)

    def _pick(self, desk):
        self.result = desk
        self.root.destroy()




# ---------------------------------------------------------------------------
# Newly-opened ticket tooltip (clickable + mark as seen)
# ---------------------------------------------------------------------------

class NewTicketTooltip:
    def __init__(self, parent, issues, domain, on_mark_seen=None):
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg="#2d3748")

        if not issues:
            tk.Label(self.win, text="  No tickets  ",
                     font=("Segoe UI", 9), fg="#a0aec0", bg="#2d3748", pady=8).pack()
            return

        tk.Label(self.win, text="  ALERT QUEUE — click to open  ",
                 font=("Segoe UI", 7, "bold"), fg="#4a5568", bg="#2d3748", pady=4
                 ).pack(fill="x")
        tk.Frame(self.win, bg="#4a5568", height=1).pack(fill="x")

        for issue in issues[:15]:
            key     = issue.get("issueKey") or issue.get("key", "")
            summary = (issue.get("fields") or {}).get("summary", "(no summary)")
            short   = summary[:55] + "…" if len(summary) > 55 else summary
            url     = f"https://{domain}/browse/{key}"

            row = tk.Frame(self.win, bg="#2d3748", cursor="hand2")
            row.pack(fill="x", padx=8, pady=2)
            lk = tk.Label(row, text=key, font=("Segoe UI", 8, "bold"),
                          fg="#0052cc", bg="#2d3748", width=10, anchor="w")
            lk.pack(side="left")
            ls = tk.Label(row, text=short, font=("Segoe UI", 8),
                          fg="#a0aec0", bg="#2d3748", anchor="w")
            ls.pack(side="left")

            def _hi(e, r=row):
                for w in [r]+list(r.winfo_children()):
                    try: w.configure(bg="#3d4f6e")
                    except Exception: pass

            def _lo(e, r=row):
                for w in [r]+list(r.winfo_children()):
                    try: w.configure(bg="#2d3748")
                    except Exception: pass

            for w in [row, lk, ls]:
                w.bind("<ButtonRelease-1>", lambda e, u=url: webbrowser.open(u))
                w.bind("<Enter>", _hi)
                w.bind("<Leave>", _lo)

        if on_mark_seen:
            tk.Frame(self.win, bg="#4a5568", height=1).pack(fill="x", pady=(4, 0))
            tk.Button(self.win, text="✓  Mark all as seen",
                      command=lambda: (on_mark_seen(), self.destroy()),
                      bg="#2d3748", fg="#68d391", font=("Segoe UI", 8),
                      relief="flat", pady=4, cursor="hand2",
                      activebackground="#3d4f6e", activeforeground="#68d391"
                      ).pack(fill="x", padx=8, pady=(0, 4))

    def place(self, x, y, screen_w, screen_h):
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        tx = max(0, min(x, screen_w - w - 10))
        ty = max(0, y - h - 6)
        self.win.geometry(f"+{tx}+{ty}")

    def destroy(self):
        self.win.destroy()


# ---------------------------------------------------------------------------
# Main overlay
# ---------------------------------------------------------------------------

class JiraOverlay:
    def __init__(self, config):
        self.config           = config
        self.queues           = []        # visible queues (respects hiddenQueues)
        self._all_queue_names = []        # all queue names before hiding — for Settings
        self.new_issues       = []
        self.completed_today  = 0
        self.sla_compliance   = None  # int % or None if unavailable
        self._sla_unavailable  = False # set True after confirmed field-not-found
        self._settings_changed = False # re-fetch immediately after settings save
        self._in_settings      = False # True while inline settings panel is showing
        self._anim_job         = None  # pending after() id for settings animation
        self._queue_geom       = None  # (w,h,x,y) saved before settings expands
        self.loading           = False
        self.error_msg       = None
        self._error_count    = 0
        self._prev_newly     = None
        self._seen_newly_ids = set()
        self._snooze_until   = None
        self._prev_counts    = {}
        self._last_refresh   = None
        self._visible        = False
        self._fading         = False
        self._show_job       = None
        self._drag_x0        = 0
        self._drag_y0        = 0
        self._drag_win_x     = 0
        self._drag_win_y     = 0
        self._did_drag       = False
        self._tooltip        = None
        self._refresh_job    = None
        self._tray           = None

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0)
        self.root.configure(bg="#0052cc")   # 1-px accent border
        self.root.geometry("260x60+100+100")
        self._sw = self.root.winfo_screenwidth()
        self._sh = self.root.winfo_screenheight()

        self._build_ui()
        self.root.withdraw()
        self._start_tray()
        self._fetch()
        self.root.after(30_000, self._update_relative_time)

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _start_tray(self):
        pk        = self.config.get("projectKey", "")
        dom       = self.config.get("domain", "")
        desk_name = self.config.get("serviceDeskName", "Service Desk")
        sd_url    = (f"https://{dom}/jira/servicedesk/projects/{pk}/queues"
                     if pk else f"https://{dom}/jira/servicedesk")
        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide",                    self._tray_toggle, default=True),
            pystray.MenuItem(f"Open {desk_name} in Jira",     lambda: webbrowser.open(sd_url)),
            pystray.MenuItem("Refresh now",       lambda: self.root.after(0, self._fetch)),
            pystray.MenuItem("Settings…",         lambda: self.root.after(0, self._toggle_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",              lambda: self.root.after(0, self.root.destroy)),
        )
        self._tray = pystray.Icon("JiraOverlay", _make_tray_image(0, False), "Jira Overlay", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_toggle(self):
        if self._visible:
            self.root.after(0, self._do_hide)
        else:
            self.root.after(0, self._do_show)

    def _update_tray(self, count: int, alert: bool, queue_name: str = "Newly Opened"):
        if self._tray:
            self._tray.icon  = _make_tray_image(count, alert)
            noun   = "ticket" if count == 1 else "tickets"
            self._tray.title = (f"Jira — {queue_name}: {count} {noun}"
                                if count else f"Jira — {queue_name}: 0")

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.frame = tk.Frame(self.root, bg="#16213e", padx=12, pady=8)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        # ── Header (always visible) ──────────────────────────────────────────
        hdr = tk.Frame(self.frame, bg="#16213e")
        hdr.pack(fill="x")
        desk = self.config.get("serviceDeskName", "JIRA")
        self.lbl_title = tk.Label(hdr, text=f"● {desk.upper()}",
                                   font=("Segoe UI", 8, "bold"), fg="#0052cc", bg="#16213e")
        self.lbl_title.pack(side="left")
        # Gear button — opens inline settings
        self.btn_gear = tk.Label(hdr, text="⚙", font=("Segoe UI", 10),
                                  fg="#4a5568", bg="#16213e", cursor="hand2")
        self.btn_gear.pack(side="right")
        self.btn_gear.bind("<ButtonRelease-1>", lambda e: self._toggle_settings())
        self.btn_gear.bind("<Enter>", lambda e: self.btn_gear.config(fg="#a0aec0"))
        self.btn_gear.bind("<Leave>", lambda e: self.btn_gear.config(fg="#4a5568"))

        tk.Frame(self.frame, bg="#2d3748", height=1).pack(fill="x", pady=(4, 4))

        # ── Main panel (queue mode) ──────────────────────────────────────────
        self.main_panel = tk.Frame(self.frame, bg="#16213e")
        self.main_panel.pack(fill="both", expand=True)

        self.rows_frame = tk.Frame(self.main_panel, bg="#16213e")
        self.rows_frame.pack(fill="both", expand=True)
        self.row_widgets: dict = {}

        tk.Frame(self.main_panel, bg="#2d3748", height=1).pack(fill="x", pady=(6, 2))
        done_row = tk.Frame(self.main_panel, bg="#16213e")
        done_row.pack(fill="x", pady=1)
        self.lbl_done = tk.Label(done_row, text="✓  Completed today — —",
                                  font=("Segoe UI", 9), fg="#68d391", bg="#16213e")
        self.lbl_done.pack(side="left")
        self.lbl_sla = tk.Label(done_row, text="", font=("Segoe UI", 8, "bold"),
                                 fg="#68d391", bg="#16213e")
        self.lbl_sla.pack(side="right")
        for w in [done_row, self.lbl_done, self.lbl_sla]:
            w.bind("<ButtonPress-1>",   self._drag_press)
            w.bind("<B1-Motion>",       self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e: self._open_completed_today())

        tk.Frame(self.main_panel, bg="#2d3748", height=1).pack(fill="x", pady=(4, 2))

        # ── Action buttons + status (replaces right-click menu) ──────────────
        footer = tk.Frame(self.main_panel, bg="#16213e")
        footer.pack(fill="x")

        def _fbtn(parent, text, cmd, tip_color="#4a5568"):
            b = tk.Label(parent, text=text, font=("Segoe UI", 10), fg=tip_color,
                         bg="#16213e", cursor="hand2")
            b.bind("<ButtonRelease-1>", lambda e: cmd())
            b.bind("<Enter>",  lambda e: b.config(fg="#e2e8f0"))
            b.bind("<Leave>",  lambda e: b.config(fg=tip_color))
            b.bind("<ButtonPress-1>",   self._drag_press)
            b.bind("<B1-Motion>",       self._drag_move)
            return b

        _fbtn(footer, "↻", self._fetch).pack(side="left", padx=(0, 8))
        self.btn_snooze_icon = _fbtn(footer, "💤", self._snooze_cycle)
        self.btn_snooze_icon.pack(side="left", padx=(0, 8))
        _fbtn(footer, "⊟", self._do_hide).pack(side="left", padx=(0, 8))
        _fbtn(footer, "✕", self.root.destroy, "#fc8181").pack(side="left")

        self.lbl_status = tk.Label(footer, text="Connecting…",
                                    font=("Segoe UI", 7), fg="#4a5568", bg="#16213e", anchor="e")
        self.lbl_status.pack(side="right")

        # ── Settings panel (hidden until ⚙ is clicked) ──────────────────────
        self.settings_panel = tk.Frame(self.frame, bg="#16213e")

        self._bind(self.root)
        self._bind(self.frame)
        self._bind(hdr)
        self._bind(self.lbl_title)
        hdr.bind("<Double-Button-1>", lambda e: self._snap_to_nearest_corner())
        self.lbl_title.bind("<Double-Button-1>", lambda e: self._snap_to_nearest_corner())

    def _bind(self, w):
        w.bind("<ButtonPress-1>",   self._drag_press)
        w.bind("<B1-Motion>",       self._drag_move)
        w.bind("<ButtonRelease-1>", self._drag_release)

    def _make_row(self, queue: dict):
        name   = queue["name"]
        qid    = queue["id"]
        alert_qs = set(self.config.get("alertQueues", [self.config.get("alertQueue","")]))
        is_new = name in alert_qs
        w_cfg  = self.config.get("overlayWidth", 260)

        row = tk.Frame(self.rows_frame, bg="#16213e", cursor="hand2")
        row.pack(fill="x", pady=1)

        nc = "#7ec8e3" if is_new else "#a0aec0"
        cc = "#7ec8e3" if is_new else "#e2e8f0"
        max_chars = max(12, (w_cfg - 100) // 7)
        short = name if len(name) <= max_chars else name[:max_chars - 1] + "…"

        lbl_name  = tk.Label(row, text=short, font=("Segoe UI", 9), fg=nc, bg="#16213e", anchor="w")
        lbl_name.pack(side="left")
        dot       = tk.Label(row, text="", font=("Segoe UI", 9), fg="#fc8181", bg="#16213e")
        dot.pack(side="right")
        lbl_count = tk.Label(row, text="—", font=("Segoe UI", 9, "bold"),
                              fg=cc, bg="#16213e", width=5, anchor="e")
        lbl_count.pack(side="right")

        pk  = self.config.get("projectKey", "")
        url = (f"https://{self.config['domain']}/jira/servicedesk/projects/{pk}/queues/custom/{qid}"
               if pk else f"https://{self.config['domain']}/jira/servicedesk")

        for w in [row, lbl_name, lbl_count, dot]:
            w.bind("<ButtonPress-1>",   self._drag_press)
            w.bind("<B1-Motion>",       self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e, u=url: self._row_click(e, u))

        if is_new:
            for w in [row, lbl_name, lbl_count, dot]:
                w.bind("<Enter>", self._tooltip_show)
                w.bind("<Leave>", self._tooltip_hide)

        return {"row": row, "lbl_name": lbl_name, "lbl_count": lbl_count,
                "dot": dot, "is_new": is_new, "base_cc": cc}

    # ── Drag ─────────────────────────────────────────────────────────────────

    def _drag_press(self, e):
        self._drag_x0    = e.x_root
        self._drag_y0    = e.y_root
        self._drag_win_x = self.root.winfo_x()
        self._drag_win_y = self.root.winfo_y()
        self._did_drag   = False

    def _drag_move(self, e):
        dx, dy = e.x_root - self._drag_x0, e.y_root - self._drag_y0
        if abs(dx) > 4 or abs(dy) > 4:
            self._did_drag = True
        self.root.geometry(f"+{self._drag_win_x+dx}+{self._drag_win_y+dy}")

    def _drag_release(self, e):
        if self._did_drag:
            x, y = self.root.winfo_x(), self.root.winfo_y()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            corners = corner_positions(self._sw, self._sh, w, h)

            # Snap if near a corner
            snapped = None
            for name, (cx, cy) in corners.items():
                if abs(x - cx) < SNAP_MARGIN and abs(y - cy) < SNAP_MARGIN:
                    snapped = name
                    self.root.geometry(f"+{cx}+{cy}")
                    break

            self.config.pop("lastX", None)
            self.config.pop("lastY", None)
            self.config.pop("snapCorner", None)
            if snapped:
                self.config["snapCorner"] = snapped
            else:
                self.config["lastX"], self.config["lastY"] = x, y
            save_config(self.config)
        self._did_drag = False

    def _row_click(self, e, url):
        if not self._did_drag:
            webbrowser.open(url)
        self._did_drag = False

    def _snooze_cycle(self):
        """Toggle snooze: active → cancel, inactive → 30 min."""
        if self._is_snoozed():
            self._cancel_snooze()
            self.btn_snooze_icon.config(fg="#4a5568")
        else:
            self._snooze(30)
            self.btn_snooze_icon.config(fg="#fbd38d")

    # ── Corner snapping ───────────────────────────────────────────────────────

    def _snap_to_nearest_corner(self, e=None):
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        corners = corner_positions(self._sw, self._sh, w, h)
        best = min(corners.items(), key=lambda kv: (x-kv[1][0])**2 + (y-kv[1][1])**2)
        name, (cx, cy) = best
        self.root.geometry(f"+{cx}+{cy}")
        self.config.pop("lastX", None)
        self.config.pop("lastY", None)
        self.config["snapCorner"] = name
        save_config(self.config)

    # ── Tooltip ───────────────────────────────────────────────────────────────

    def _tooltip_show(self, e):
        if self._tooltip or not self.new_issues:
            return
        self._tooltip = NewTicketTooltip(
            self.root, self.new_issues, self.config["domain"],
            on_mark_seen=self._mark_all_seen)
        self._tooltip.place(self.root.winfo_x(), self.root.winfo_y(), self._sw, self._sh)

    def _tooltip_hide(self, e):
        rx, ry = self.root.winfo_rootx(), self.root.winfo_rooty()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        if not (rx <= e.x_root <= rx+rw and ry <= e.y_root <= ry+rh):
            if self._tooltip:
                self._tooltip.destroy()
                self._tooltip = None

    def _mark_all_seen(self):
        self._seen_newly_ids = set()

    def _open_completed_today(self):
        if self._did_drag:
            return
        pk  = self.config.get("projectKey", "")
        dom = self.config.get("domain", "")
        jql = f"project = {pk} AND statusCategory = Done AND updated >= startOfDay()"
        webbrowser.open(f"https://{dom}/issues/?jql={jql.replace(' ', '+')}")

    # ── Service-desk discovery ────────────────────────────────────────────────

    def _discover_service_desk(self):
        cfg = self.config
        try:
            r = requests.get(f"https://{cfg['domain']}/rest/servicedeskapi/servicedesk",
                             auth=HTTPBasicAuth(cfg["email"], cfg["token"]), timeout=10)
            r.raise_for_status()
            desks = r.json().get("values", [])
        except Exception:
            return None

        accessible = []
        for d in desks:
            try:
                t = requests.get(
                    f"https://{cfg['domain']}/rest/servicedeskapi/servicedesk/{d['id']}/queue",
                    params={"limit": 1},
                    auth=HTTPBasicAuth(cfg["email"], cfg["token"]), timeout=5)
                if t.ok:
                    accessible.append(d)
            except Exception:
                pass

        if not accessible: return None
        if len(accessible) == 1: return accessible[0]

        self._desk_pick = None
        self.root.after(0, lambda: self._run_desk_picker(accessible))
        t = 90
        while self._desk_pick is None and t > 0:
            time.sleep(0.1); t -= 0.1
        return self._desk_pick

    def _run_desk_picker(self, desks):
        p = PickDeskWindow(desks)
        self._desk_pick = p.result or desks[0]

    def _show_alert_queue_picker(self):
        """Modal Toplevel — runs on main thread, re-fetches when done."""
        win = tk.Toplevel()
        win.title("Jira Overlay — Choose Alert Queue")
        win.configure(bg="#1a1a2e")
        win.resizable(False, False)
        win.attributes("-topmost", True)

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()

        tk.Label(win, text="Which queue should trigger alerts?",
                 font=("Segoe UI", 11, "bold"), fg="#e2e8f0", bg="#1a1a2e"
                 ).pack(pady=(18, 4))
        tk.Label(win,
                 text="The overlay will appear and notifications will fire\n"
                      "when new tickets arrive in this queue.",
                 font=("Segoe UI", 8), fg="#4a5568", bg="#1a1a2e", justify="center"
                 ).pack(pady=(0, 10))

        # Scrollable list
        container = tk.Frame(win, bg="#1a1a2e")
        container.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        canvas = tk.Canvas(container, bg="#1a1a2e", highlightthickness=0, width=320)
        sb     = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        sf     = tk.Frame(canvas, bg="#1a1a2e")
        sf.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sf, anchor="nw", width=320)
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-1*(e.delta//120), "units"))
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def pick(name):
            self.config["alertQueue"]  = name
            self.config["alertQueues"] = [name]
            save_config(self.config)
            win.destroy()

        for q in self.queues:
            tk.Button(sf, text=q["name"],
                      command=lambda n=q["name"]: pick(n),
                      bg="#2d3748", fg="#e2e8f0", font=("Segoe UI", 10),
                      relief="flat", padx=12, pady=7, cursor="hand2",
                      activebackground="#0052cc", activeforeground="white"
                      ).pack(fill="x", pady=3, padx=4)

        win.update_idletasks()
        btn_h      = min(sf.winfo_reqheight(), sh - 250)
        total_h    = btn_h + 130
        win.geometry(f"380x{total_h}+{sw//2-190}+{sh//2-total_h//2}")
        canvas.configure(height=btn_h)

        win.focus_force()
        win.wait_window(win)

        if self.config.get("alertQueue"):
            self._fetch()

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch(self):
        if self.loading: return
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        self.loading = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        try:
            cfg = self.config

            if not cfg.get("serviceDeskId"):
                desk = self._discover_service_desk()
                if not desk:
                    self.error_msg = "No accessible service desk found"
                    return
                cfg["serviceDeskId"]   = desk["id"]
                cfg["serviceDeskName"] = desk["projectName"]
                cfg["projectKey"]      = desk.get("projectKey", "")
                save_config(cfg)
                self.root.after(0, lambda n=desk["projectName"]:
                    self.lbl_title.config(text=f"● {n.upper()}"))

            sd_id  = cfg["serviceDeskId"]
            auth   = HTTPBasicAuth(cfg["email"], cfg["token"])
            hidden = set(cfg.get("hiddenQueues", []))

            # Re-populate projectKey if missing
            if not cfg.get("projectKey"):
                try:
                    ri = requests.get(
                        f"https://{cfg['domain']}/rest/servicedeskapi/servicedesk/{sd_id}",
                        auth=auth, timeout=10)
                    if ri.ok:
                        cfg["projectKey"] = ri.json().get("projectKey", "")
                        save_config(cfg)
                except Exception:
                    pass

            r = requests.get(
                f"https://{cfg['domain']}/rest/servicedeskapi/servicedesk/{sd_id}/queue",
                params={"includeCount": "true"}, auth=auth, timeout=10)
            r.raise_for_status()

            skip_kw   = {k.strip().upper() for k in cfg.get("skipKeywords", []) if k.strip()}
            max_count = cfg.get("maxQueueSize", 0)  # 0 = no limit
            all_queues = [
                q for q in r.json().get("values", [])
                if not any(kw in q["name"].upper() for kw in skip_kw)
                and (max_count == 0 or q.get("issueCount", 0) <= max_count)
            ]
            self._all_queue_names = [q["name"] for q in all_queues]
            self.queues = [q for q in all_queues if q["name"] not in hidden]

            alert_qs = set(cfg.get("alertQueues", [cfg.get("alertQueue","")]))
            alert_qs.discard("")
            if not alert_qs and self.queues:
                self.root.after(0, self._show_alert_queue_picker)
                return
            nq = next((q for q in self.queues if q["name"] in alert_qs), None)
            if nq and nq.get("issueCount", 0) > 0:
                ir = requests.get(
                    f"https://{cfg['domain']}/rest/servicedeskapi/servicedesk/{sd_id}/queue/{nq['id']}/issue",
                    params={"start": 0, "limit": 15}, auth=auth, timeout=10)
                self.new_issues = ir.json().get("values", []) if ir.ok else []
            else:
                self.new_issues = []

            # Tickets completed today (assigned only)
            try:
                pk       = cfg.get("projectKey", "")
                base_jql = f'project = {pk} AND statusCategory = Done AND updated >= startOfDay()'
                extra    = cfg.get("completedTodayFilter", "").strip()
                if extra:
                    base_jql += f' AND {extra}'
                cr = requests.post(
                    f"https://{cfg['domain']}/rest/api/3/search/jql",
                    json={"jql": base_jql, "maxResults": 200, "fields": ["status"]},
                    auth=auth, timeout=10)
                if cr.ok:
                    data = cr.json()
                    n    = len(data.get("issues", []))
                    self.completed_today = f"{n}+" if not data.get("isLast", True) else n
                    total_n = n

                    # SLA compliance — only attempt if not already confirmed unavailable
                    if not self._sla_unavailable and total_n > 0:
                        sla_field = cfg.get("slaField", "Time to resolution")
                        sla_jql   = base_jql + f' AND "{sla_field}" = slaBreached()'
                        sr = requests.post(
                            f"https://{cfg['domain']}/rest/api/3/search/jql",
                            json={"jql": sla_jql, "maxResults": 200, "fields": ["status"]},
                            auth=auth, timeout=10)
                        if sr.ok:
                            breached = len(sr.json().get("issues", []))
                            self.sla_compliance = round((total_n - breached) / total_n * 100)
                        else:
                            # Field unknown / SLAs not configured — stop querying
                            body = sr.text.lower()
                            if any(x in body for x in ("does not exist", "unknown", "field")):
                                self._sla_unavailable = True
                            self.sla_compliance = None
                    elif self._sla_unavailable:
                        self.sla_compliance = None
                else:
                    self.completed_today = 0
                    self.sla_compliance  = None
            except Exception:
                self.completed_today = 0
                self.sla_compliance  = None

            self.error_msg    = None
            self._error_count = 0

        except requests.HTTPError as e:
            self._error_count += 1
            self.error_msg = f"HTTP {e.response.status_code} — check credentials"
            self.queues    = []
        except requests.ConnectionError:
            self._error_count += 1
            self.error_msg = "Connection failed — retrying…"
            self.queues    = []
        except Exception as e:
            self._error_count += 1
            self.error_msg = str(e)[:50]
            self.queues    = []
        finally:
            self.loading = False
            self.root.after(0, self._update_ui)
            if self._settings_changed:
                # Settings were saved while this fetch was in-flight — re-fetch immediately
                self._settings_changed = False
                self._refresh_job = self.root.after(0, self._fetch)
            else:
                base  = self.config.get("refreshSeconds", 30) * 1000
                delay = min(base * (2 ** self._error_count), 5 * 60 * 1000)
                self._refresh_job = self.root.after(delay, self._fetch)

    # ── Update UI ─────────────────────────────────────────────────────────────

    def _update_ui(self):
        if self.error_msg:
            for w in self.row_widgets.values():
                w["row"].destroy()
            self.row_widgets.clear()
            err = tk.Label(self.rows_frame, text=self.error_msg,
                           font=("Segoe UI", 8), fg="#fc8181", bg="#16213e")
            err.pack()
            self._bind(err)
            self.lbl_status.config(text="Error — right-click to retry", fg="#fc8181")
            self.root.after(10, self._resize)
            return

        current_names = [q["name"] for q in self.queues]
        for name in list(self.row_widgets):
            if name not in current_names:
                self.row_widgets[name]["row"].destroy()
                del self.row_widgets[name]

        alert_qs    = set(self.config.get("alertQueues", [self.config.get("alertQueue", "")]))
        alert_qs.discard("")
        newly_count = 0
        for q in self.queues:
            name   = q["name"]
            count  = q.get("issueCount", 0)
            is_new = name in alert_qs

            if name not in self.row_widgets:
                self.row_widgets[name] = self._make_row(q)

            rw = self.row_widgets[name]
            rw["lbl_count"].config(text=str(count))

            if is_new:
                newly_count = count
                if count > 0:
                    rw["lbl_count"].config(fg="#fc8181")
                    rw["dot"].config(text="●")
                else:
                    rw["lbl_count"].config(fg="#7ec8e3")
                    rw["dot"].config(text="")

            # Flash row if count changed
            prev = self._prev_counts.get(name)
            if prev is not None and count != prev:
                self._flash_row(rw)
            self._prev_counts[name] = count

        # Deduplication — notify only for new ticket IDs
        current_ids = {i.get("issueKey") or i.get("key","") for i in self.new_issues}
        truly_new   = current_ids - self._seen_newly_ids
        self._seen_newly_ids = current_ids

        snoozed      = self._is_snoozed()
        has_new_alert = bool(truly_new) and self._prev_newly is not None

        if has_new_alert and not snoozed:
            if len(truly_new) == 1:
                msg = f"New ticket: {next(iter(truly_new))}"
            else:
                msg = f"{len(truly_new)} new tickets need attention"
            if self.config.get("notificationsEnabled", True):
                send_toast("Jira — Newly Opened", msg)
            if self.config.get("soundEnabled", True):
                play_alert()

        self._prev_newly   = newly_count
        self._last_refresh = datetime.now()
        self.lbl_done.config(text=f"✓  Completed today — {self.completed_today}")
        if self.sla_compliance is not None:
            pct   = self.sla_compliance
            color = "#68d391" if pct >= 95 else "#fbd38d" if pct >= 80 else "#fc8181"
            self.lbl_sla.config(text=f"SLA {pct}%", fg=color)
        else:
            self.lbl_sla.config(text="")

        tray_queue = self.config.get("trayBadgeQueue", "Newly Opened")
        tray_count = next((q.get("issueCount",0) for q in self.queues
                           if tray_queue.lower() in q["name"].lower()), newly_count)
        self._update_tray(tray_count, newly_count > 0, tray_queue)

        if snoozed:
            until = self._snooze_until.strftime("%H:%M")
            self.lbl_status.config(text=f"Snoozed until {until}  ·  right-click to cancel",
                                   fg="#fbd38d")
        else:
            self.lbl_status.config(text="Updated just now",
                                   fg="#4a5568")

        always = self.config.get("alwaysVisible", False)
        show   = (newly_count > 0 or always) and not snoozed

        if show:
            self.root.after(10, self._resize)
            if not self._visible and not self._show_job:
                # Delay overlay if notification was just sent — let toast render first
                delay = 2500 if has_new_alert else 0
                self._show_job = self.root.after(delay, self._do_show)
        else:
            if self._show_job:
                self.root.after_cancel(self._show_job)
                self._show_job = None
            if self._visible:
                self._do_hide()

    # ── Flash row on count change ─────────────────────────────────────────────

    def _flash_row(self, rw: dict):
        row      = rw["row"]
        children = list(row.winfo_children())
        orig_bg  = "#16213e"
        flash_bg = "#2a3f5f"

        def pulse(step=0):
            bg = flash_bg if step % 2 == 0 else orig_bg
            try:
                for w in [row] + children:
                    w.configure(bg=bg)
            except Exception:
                pass
            if step < 5:
                self.root.after(100, lambda: pulse(step + 1))

        pulse()

    # ── Relative time ──────────────────────────────────────────────────────────

    def _update_relative_time(self):
        if self._last_refresh and not self.error_msg and not self._is_snoozed():
            secs = (datetime.now() - self._last_refresh).total_seconds()
            if secs < 60:
                rel = "just now"
            elif secs < 3600:
                rel = f"{int(secs//60)}m ago"
            else:
                rel = f"{int(secs//3600)}h ago"
            self.lbl_status.config(
                text=f"Updated {rel}", fg="#4a5568")
        self.root.after(30_000, self._update_relative_time)

    # ── Snooze ────────────────────────────────────────────────────────────────

    def _snooze(self, minutes: int):
        self._snooze_until = datetime.now() + timedelta(minutes=minutes)
        if self._visible:
            self._do_hide()

    def _cancel_snooze(self):
        self._snooze_until = None
        self._update_ui()

    def _is_snoozed(self) -> bool:
        if self._snooze_until and datetime.now() < self._snooze_until:
            return True
        self._snooze_until = None
        return False

    # ── Show / hide ───────────────────────────────────────────────────────────

    def _do_show(self):
        self._show_job = None
        if self._visible or self._fading:
            return
        self._fading  = True
        self._visible = True
        target = self.config.get("alpha", 0.93)
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self._resize()
        self._fade(0, target, target / 14)

    def _do_hide(self):
        if not self._visible or self._fading:
            return
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None
        self._fading  = True
        self._visible = False
        alpha = float(self.root.attributes("-alpha"))
        self._fade(alpha, 0, -(alpha / 14), on_done=self.root.withdraw)

    def _fade(self, current, target, step, on_done=None):
        nxt  = current + step
        done = (step > 0 and nxt >= target) or (step < 0 and nxt <= target)
        self.root.attributes("-alpha", max(0.0, min(1.0, target if done else nxt)))
        if done:
            self._fading = False
            if on_done: on_done()
        else:
            self.root.after(14, lambda: self._fade(nxt, target, step, on_done))

    # ── Resize / reposition ───────────────────────────────────────────────────

    def _resize(self):
        self.root.update_idletasks()
        w = self.config.get("overlayWidth", 260)
        self.root.geometry(f"{w}x{self.frame.winfo_reqheight() + 4}")
        self._reposition()

    def _reposition(self):
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        corners = corner_positions(self._sw, self._sh, w, h)

        corner = self.config.get("snapCorner")
        if corner and corner in corners:
            x, y = corners[corner]
        elif "lastX" in self.config and "lastY" in self.config:
            x, y = self.config["lastX"], self.config["lastY"]
        else:
            x, y = corners["bottom-right"]

        # Off-screen guard — reset to bottom-right if fully off any monitor
        mr = get_monitor_rect(x + w // 2, y + h // 2)
        if mr:
            ml2, mt2, mr2, mb2 = mr
            x = max(ml2, min(x, mr2 - w))
            y = max(mt2, min(y, mb2 - h - 4))
        else:
            x, y = corners["bottom-right"]
            self.config.pop("lastX", None)
            self.config.pop("lastY", None)
            self.config["snapCorner"] = "bottom-right"

        self.root.geometry(f"+{x}+{y}")

    # ── Inline settings ───────────────────────────────────────────────────────

    # ── Settings animation constants ─────────────────────────────────────────
    _SETTINGS_W    = 420   # width in settings mode
    _ANIM_STEPS    = 20    # frames
    _ANIM_MS       = 12    # ms per frame  (~240 ms total)

    def _toggle_settings(self):
        if self._in_settings:
            self._settings_anim(closing=True)
            return
        self._in_settings = True
        # Show overlay if hidden (transparent while we measure)
        if not self._visible:
            self._visible = True
            self.root.attributes("-alpha", self.config.get("alpha", 0.93))
            self.root.deiconify()
            self.root.update_idletasks()

        self.btn_gear.config(text="✕")
        self.main_panel.pack_forget()
        self._build_settings_panel()
        self.settings_panel.pack(fill="both", expand=True)
        self.root.update_idletasks()
        self._settings_anim(closing=False)

    def _settings_anim(self, closing: bool):
        """Start the open/close size animation."""
        if self._anim_job:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None

        self.root.update_idletasks()
        cw = self.root.winfo_width()
        ch = self.root.winfo_height()
        cx = self.root.winfo_x()
        cy = self.root.winfo_y()

        if not closing:
            # Save queue geometry so we can restore it exactly
            self._queue_geom = (cw, ch, cx, cy)
            # Target: wide, tall — bottom-right corner stays fixed
            tw  = self._SETTINGS_W
            tx  = self._sw - tw - 20
            bot = cy + ch                   # fixed bottom edge
            ty  = max(20, bot - int(self._sh * 0.92))
            th  = bot - ty
            start, end = (cw, ch, cx, cy), (tw, th, tx, ty)
        else:
            # Animate back to saved queue dimensions
            qw, qh, qx, qy = self._queue_geom or (cw, ch, cx, cy)
            start, end = (cw, ch, cx, cy), (qw, qh, qx, qy)

        self._run_anim(start, end, step=0, closing=closing)

    def _run_anim(self, start, end, step: int, closing: bool):
        t    = step / self._ANIM_STEPS
        ease = (1 - math.cos(math.pi * t)) / 2
        sw, sh, sx, sy = start
        ew, eh, ex, ey = end
        w = int(sw + (ew - sw) * ease)
        h = int(sh + (eh - sh) * ease)
        x = int(sx + (ex - sx) * ease)
        y = int(sy + (ey - sy) * ease)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        if step < self._ANIM_STEPS:
            self._anim_job = self.root.after(
                self._ANIM_MS,
                lambda: self._run_anim(start, end, step + 1, closing))
        else:
            self._anim_job = None
            if closing:
                self._in_settings = False
                self.btn_gear.config(text="⚙")
                self.settings_panel.pack_forget()
                self.main_panel.pack(fill="both", expand=True)
                self.root.after(10, self._resize)

    def _build_settings_panel(self):
        """Destroy and rebuild the settings panel content from current config."""
        for w in self.settings_panel.winfo_children():
            w.destroy()

        cfg         = self.config
        queue_names = self._all_queue_names or [q["name"] for q in self.queues]
        BG, FG, SEL = "#16213e", "#a0aec0", "#2d3748"

        # ── Scrollable body ──────────────────────────────────────────────────
        wrap   = tk.Frame(self.settings_panel, bg=BG)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb     = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        body   = tk.Frame(canvas, bg=BG)
        body.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        cw_id  = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw_id, width=e.width))
        def _scroll(e): canvas.yview_scroll(-1*(e.delta//120), "units")
        canvas.bind("<MouseWheel>", _scroll)
        body.bind("<MouseWheel>", _scroll)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # ── Helpers — defined AFTER body so they pack into body, not settings_panel
        def section(text):
            tk.Label(body, text=text.upper(), font=("Segoe UI", 7, "bold"),
                     fg="#4a5568", bg=BG).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Frame(body, bg="#2d3748", height=1).pack(fill="x", padx=12, pady=(2, 2))

        def check(parent, text, var):
            tk.Checkbutton(parent, text=text, variable=var, fg=FG, bg=BG,
                           selectcolor=SEL, activeforeground="#e2e8f0", activebackground=BG,
                           font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=1)

        # Refresh interval
        section("Refresh interval")
        rf = tk.Frame(body, bg=BG); rf.pack(fill="x", padx=16, pady=(0, 4))
        self._s_refresh = tk.IntVar(value=cfg.get("refreshSeconds", 30))
        for secs, lbl in [(15,"15 s"),(30,"30 s"),(60,"1 min"),(120,"2 min"),(300,"5 min")]:
            tk.Radiobutton(rf, text=lbl, variable=self._s_refresh, value=secs,
                           fg=FG, bg=BG, selectcolor=SEL, activeforeground="#e2e8f0",
                           activebackground=BG, font=("Segoe UI", 9)).pack(side="left", padx=(0,8))

        # Alerts
        section("Alerts")
        self._s_notif = tk.BooleanVar(value=cfg.get("notificationsEnabled", True))
        self._s_sound = tk.BooleanVar(value=cfg.get("soundEnabled", True))
        check(body, "Desktop notification", self._s_notif)
        check(body, "Sound alert",           self._s_sound)

        # Appearance
        section("Appearance")
        af = tk.Frame(body, bg=BG); af.pack(fill="x", padx=16, pady=(0,4))
        tk.Label(af, text="Transparency", font=("Segoe UI", 9), fg=FG, bg=BG).grid(row=0,column=0,sticky="w")
        self._s_alpha = tk.DoubleVar(value=cfg.get("alpha", 0.93))
        tk.Scale(af, from_=0.3, to=1.0, resolution=0.05, orient="horizontal", variable=self._s_alpha,
                 length=140, bg=BG, fg=FG, troughcolor=SEL, highlightthickness=0, sliderrelief="flat"
                 ).grid(row=0, column=1, padx=(8,0))
        tk.Label(af, text="Width", font=("Segoe UI", 9), fg=FG, bg=BG).grid(row=1,column=0,sticky="w",pady=(6,0))
        self._s_width = tk.IntVar(value=cfg.get("overlayWidth", 260))
        tk.Scale(af, from_=200, to=420, resolution=10, orient="horizontal", variable=self._s_width,
                 length=140, bg=BG, fg=FG, troughcolor=SEL, highlightthickness=0, sliderrelief="flat"
                 ).grid(row=1, column=1, padx=(8,0), pady=(6,0))
        self._s_always = tk.BooleanVar(value=cfg.get("alwaysVisible", False))
        check(body, "Always show (dashboard mode)", self._s_always)

        # Queue filtering
        section("Queue filtering")
        kf = tk.Frame(body, bg=BG); kf.pack(fill="x", padx=16, pady=(0,4))
        tk.Label(kf, text="Hide queues whose name contains:", font=("Segoe UI", 8), fg=FG, bg=BG
                 ).pack(anchor="w")
        self._s_skip_kw = tk.StringVar(value=", ".join(cfg.get("skipKeywords", [])))
        tk.Entry(kf, textvariable=self._s_skip_kw, bg=SEL, fg="#e2e8f0",
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2,6))
        tk.Label(kf, text="Hide queues with more than N tickets (0=no limit):", font=("Segoe UI", 8), fg=FG, bg=BG
                 ).pack(anchor="w")
        self._s_max_q = tk.IntVar(value=cfg.get("maxQueueSize", 0))
        tk.Spinbox(kf, from_=0, to=999999, increment=1000, textvariable=self._s_max_q,
                   width=10, bg=SEL, fg="#e2e8f0", insertbackground="white",
                   relief="flat", font=("Segoe UI", 9), buttonbackground=SEL
                   ).pack(anchor="w", ipady=3, pady=(2,0))

        # Queues to show
        if queue_names:
            section("Queues to show")
            hidden = set(cfg.get("hiddenQueues", []))
            self._s_queue_vars = {n: tk.BooleanVar(value=n not in hidden) for n in queue_names}
            for name, var in self._s_queue_vars.items():
                check(body, name, var)
        else:
            self._s_queue_vars = {}

        # Tray badge
        section("Tray badge shows")
        self._s_tray_badge = tk.StringVar(value=cfg.get("trayBadgeQueue", ""))
        for name in queue_names:
            tk.Radiobutton(body, text=name, variable=self._s_tray_badge, value=name,
                           fg=FG, bg=BG, selectcolor=SEL, activeforeground="#e2e8f0",
                           activebackground=BG, font=("Segoe UI", 9)).pack(anchor="w", padx=16)

        # Alert queues
        section("Alert queues (trigger overlay + notifications)")
        current_alerts = set(cfg.get("alertQueues",
                             [cfg.get("alertQueue","")] if cfg.get("alertQueue") else []))
        self._s_alert_vars = {}
        for name in queue_names:
            var = tk.BooleanVar(value=name in current_alerts)
            self._s_alert_vars[name] = var
            check(body, name, var)

        # SLA + completed filter
        section("SLA & completed today")
        sf = tk.Frame(body, bg=BG); sf.pack(fill="x", padx=16, pady=(0,4))
        tk.Label(sf, text="SLA field name:", font=("Segoe UI", 9), fg=FG, bg=BG).pack(anchor="w")
        self._s_sla = tk.StringVar(value=cfg.get("slaField", "Time to resolution"))
        tk.Entry(sf, textvariable=self._s_sla, bg=SEL, fg="#e2e8f0",
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2,6))
        tk.Label(sf, text='Extra JQL filter (e.g. "assignee is not EMPTY"):', font=("Segoe UI", 8), fg=FG, bg=BG
                 ).pack(anchor="w")
        self._s_completed_filter = tk.StringVar(value=cfg.get("completedTodayFilter", ""))
        tk.Entry(sf, textvariable=self._s_completed_filter, bg=SEL, fg="#e2e8f0",
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2,0))

        # System
        section("System")
        self._s_startup = tk.BooleanVar(value=startup_enabled())
        check(body, "Run on Windows startup", self._s_startup)
        tk.Frame(body, bg=BG, height=4).pack()
        tk.Button(body, text="⚙  Reconfigure credentials…", command=self._reconfigure,
                  bg=SEL, fg=FG, font=("Segoe UI", 9), relief="flat",
                  padx=8, pady=4, cursor="hand2"
                  ).pack(anchor="w", padx=16, pady=(0, 8))

        # ── Save / Cancel buttons ────────────────────────────────────────────
        bf = tk.Frame(self.settings_panel, bg=BG)
        bf.pack(fill="x", pady=(4, 0))
        tk.Button(bf, text="✓  Save", command=self._save_settings,
                  bg="#0052cc", fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, pady=5, cursor="hand2",
                  activebackground="#0040a0", activeforeground="white"
                  ).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="✕  Cancel", command=self._cancel_settings,
                  bg=SEL, fg=FG, font=("Segoe UI", 9),
                  relief="flat", padx=10, pady=5, cursor="hand2"
                  ).pack(side="left")

    def _save_settings(self):
        set_startup(self._s_startup.get())
        hidden      = [n for n, v in self._s_queue_vars.items() if not v.get()]
        alert_list  = [n for n, v in self._s_alert_vars.items() if v.get()]
        new_cfg = {
            "refreshSeconds":       self._s_refresh.get(),
            "notificationsEnabled": self._s_notif.get(),
            "soundEnabled":         self._s_sound.get(),
            "hiddenQueues":         hidden,
            "trayBadgeQueue":       self._s_tray_badge.get(),
            "alpha":                round(self._s_alpha.get(), 2),
            "overlayWidth":         self._s_width.get(),
            "alwaysVisible":        self._s_always.get(),
            "alertQueues":          alert_list,
            "alertQueue":           alert_list[0] if alert_list else "",
            "slaField":             self._s_sla.get().strip() or "Time to resolution",
            "completedTodayFilter": self._s_completed_filter.get().strip(),
            "skipKeywords":         [k.strip() for k in self._s_skip_kw.get().split(",") if k.strip()],
            "maxQueueSize":         int(self._s_max_q.get()),
        }
        self.config.update(new_cfg)
        save_config(self.config)
        # Rebuild rows then animate close
        for rw in self.row_widgets.values():
            rw["row"].destroy()
        self.row_widgets.clear()
        if self.loading:
            self._settings_changed = True
        else:
            self._fetch()
        self._settings_anim(closing=True)

    def _cancel_settings(self):
        self._settings_anim(closing=True)

    def _reconfigure(self):
        if messagebox.askyesno("Reconfigure", "Clear saved credentials and restart setup?",
                               parent=self.root):
            if self._tray: self._tray.stop()
            if os.path.exists(CONFIG_FILE): os.remove(CONFIG_FILE)
            self.root.destroy()

    def run(self):
        self.root.mainloop()
        if self._tray: self._tray.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    if not config:
        setup  = SetupWindow()
        config = setup.result
        if not config:
            return
    JiraOverlay(config).run()


if __name__ == "__main__":
    main()
