#!/usr/bin/env python3
"""
Jira JSM Queue Overlay
======================
A lightweight Windows desktop overlay that monitors Jira Service Management
queues in real time.

Requirements:  Python 3.9+ (Windows)
               pip install requests pystray Pillow  (auto-installed on first run)

Quick start:   pythonw jira_overlay.pyw   (or double-click launch_overlay.bat)

Configuration: jira_config.json  (created on first run, excluded from git)
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import functools
import importlib.util
import json
import os
import struct
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
import winsound
from datetime import datetime, timedelta
from typing import Optional
import tkinter as tk
from tkinter import messagebox

# ---------------------------------------------------------------------------
# Dependency bootstrap — must run before any third-party import
# ---------------------------------------------------------------------------

_BOOTSTRAP_DEPS = [("requests", "requests"), ("PIL", "Pillow"), ("pystray", "pystray")]


def _bootstrap() -> None:
    """Install missing third-party packages with a progress UI on first run."""
    missing = [(mod, pkg) for mod, pkg in _BOOTSTRAP_DEPS
               if importlib.util.find_spec(mod) is None]
    if not missing:
        return

    win = tk.Tk()
    win.title("Jira Overlay — First Run Setup")
    win.configure(bg="#1a1a2e")
    win.resizable(False, False)
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"360x130+{sw // 2 - 180}+{sh // 2 - 65}")

    tk.Label(win, text="Installing required packages…",
             font=("Segoe UI", 11, "bold"), fg="#e2e8f0", bg="#1a1a2e").pack(pady=(22, 4))
    lbl = tk.Label(win, text="", font=("Segoe UI", 9), fg="#a0aec0", bg="#1a1a2e")
    lbl.pack()
    track = tk.Frame(win, bg="#2d3748", height=5)
    track.pack(fill="x", padx=24, pady=(12, 0))
    bar = tk.Frame(track, bg="#0052cc", height=5, width=0)
    bar.place(x=0, y=0, height=5)
    win.update()

    errors: list[str] = []
    for i, (mod, pkg) in enumerate(missing):
        lbl.config(text=f"pip install {pkg}")
        win.update()
        rc = subprocess.call(
            [sys.executable, "-m", "pip", "install", "--quiet", pkg],
            creationflags=0x08000000,
        )
        if rc != 0:
            errors.append(pkg)
        track.update_idletasks()
        bar.place(width=int(track.winfo_width() * (i + 1) / len(missing)))
        win.update()

    if errors:
        lbl.config(text=f"Failed to install: {', '.join(errors)}", fg="#fc8181")
        win.after(4000, win.destroy)
    else:
        lbl.config(text="All done — starting overlay…")
        win.after(900, win.destroy)
    win.mainloop()


_bootstrap()

# Third-party imports — guaranteed present after bootstrap
import requests
from requests.auth import HTTPBasicAuth
from PIL import Image, ImageDraw, ImageFont
import pystray

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jira_config.json")
STARTUP_NAME = "JiraOverlay"
STARTUP_REG  = r"Software\Microsoft\Windows\CurrentVersion\Run"
_SNAP_MARGIN  = 60   # px from a corner to snap

# UI colour palette
_C = {
    "bg":        "#16213e",
    "bg_dark":   "#1a1a2e",
    "border":    "#0052cc",
    "divider":   "#2d3748",
    "input":     "#2d3748",
    "fg":        "#a0aec0",
    "fg_bright": "#e2e8f0",
    "fg_dim":    "#4a5568",
    "accent":    "#0052cc",
    "accent_hi": "#0040a0",
    "alert":     "#fc8181",
    "warn":      "#fbd38d",
    "ok":        "#68d391",
    "info":      "#7ec8e3",
}

# Tray icon font — loaded once, reused on every badge update
_tray_font: Optional[ImageFont.FreeTypeFont] = None


def _get_tray_font() -> ImageFont.ImageFont:
    """Return a cached 64-pt bold font for tray badge rendering."""
    global _tray_font
    if _tray_font is not None:
        return _tray_font
    for path in ("C:/Windows/Fonts/arialbd.ttf",
                 "C:/Windows/Fonts/arial.ttf",
                 "C:/Windows/Fonts/calibrib.ttf"):
        try:
            _tray_font = ImageFont.truetype(path, 64)
            return _tray_font
        except Exception:
            pass
    try:
        _tray_font = ImageFont.load_default(size=56)
    except TypeError:
        _tray_font = ImageFont.load_default()
    return _tray_font


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def load_config() -> Optional[dict]:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    return None


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------------------
# Windows helpers
# ---------------------------------------------------------------------------

def _xml_escape(text: str) -> str:
    """Escape characters that are special in XML attribute/text values."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;"))


def send_toast(title: str, body: str) -> None:
    """Fire a Windows 10/11 toast notification via PowerShell WinRT."""
    t, b = _xml_escape(title), _xml_escape(body)
    ps = f"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$id = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml('<toast duration="short"><visual><binding template="ToastGeneric"><text hint-maxLines="1">{t}</text><text>{b}</text></binding></visual></toast>')
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show((New-Object Windows.UI.Notifications.ToastNotification($doc)))
"""
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-NonInteractive", "-Command", ps],
        creationflags=0x08000000,
    )


def play_alert() -> None:
    try:
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        pass


def startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, STARTUP_NAME)
        return True
    except FileNotFoundError:
        return False


def set_startup(enable: bool) -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, STARTUP_NAME, 0, winreg.REG_SZ,
                                  f'pythonw "{os.path.abspath(__file__)}"')
            else:
                try:
                    winreg.DeleteValue(k, STARTUP_NAME)
                except FileNotFoundError:
                    pass
    except OSError:
        pass


class _MONITORINFO(ctypes.Structure):
    """Maps to the Windows MONITORINFO struct used by GetMonitorInfoW."""
    _fields_ = [
        ("cbSize",    ctypes.c_uint32),
        ("rcMonitor", ctypes.c_int32 * 4),
        ("rcWork",    ctypes.c_int32 * 4),
        ("dwFlags",   ctypes.c_uint32),
    ]


def get_monitor_rect(x: int, y: int) -> Optional[tuple[int, int, int, int]]:
    """Return (left, top, right, bottom) of the monitor nearest to (x, y)."""
    try:
        pt  = ctypes.wintypes.POINT(x, y)
        mon = ctypes.windll.user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            l, t, r, b = info.rcMonitor
            return l, t, r, b
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tray icon image builder
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=8)
def _make_tray_image(count: int, alert: bool) -> Image.Image:
    """Render a circular badge icon; result cached by (count, alert) key."""
    size  = 128
    img   = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw  = ImageDraw.Draw(img)
    color = (220, 80, 60) if alert else (0, 82, 204)
    draw.ellipse([2, 2, size - 2, size - 2], fill=color)
    text  = str(count) if count < 100 else "99+"
    font  = _get_tray_font()
    bbox  = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        ((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1]),
        text, fill=(255, 255, 255), font=font,
    )
    return img


# ---------------------------------------------------------------------------
# Corner positions helper (cached — avoids redundant dict construction)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=16)
def _corner_positions(sw: int, sh: int, w: int, h: int) -> dict[str, tuple[int, int]]:
    return {
        "top-left":     (10,           10),
        "top-right":    (sw - w - 10,  10),
        "bottom-left":  (10,           sh - h - 60),
        "bottom-right": (sw - w - 20,  sh - h - 60),
    }


# ---------------------------------------------------------------------------
# Dark-themed tooltip (always appears above the target widget)
# ---------------------------------------------------------------------------

class _Tooltip:
    """Styled tooltip that appears above its widget after a short hover delay."""

    def __init__(self, widget: tk.Widget, text: str, delay: int = 600) -> None:
        self._widget = widget
        self._text   = text
        self._delay  = delay
        self._job:  Optional[str]         = None
        self._tip:  Optional[tk.Toplevel] = None
        widget.bind("<Enter>",        self._schedule, add=True)
        widget.bind("<Leave>",        self._cancel,   add=True)
        widget.bind("<ButtonPress-1>", self._cancel,  add=True)

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self._widget.after(self._delay, self._show)

    def _cancel(self, _e=None) -> None:
        if self._job:
            self._widget.after_cancel(self._job)
            self._job = None
        if self._tip:
            self._tip.destroy()
            self._tip = None

    def _show(self) -> None:
        if self._tip:
            return
        tip = tk.Toplevel(self._widget)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        tip.configure(bg=_C["border"])          # 1-px accent-colour border

        tk.Label(tip, text=self._text,
                 font=("Segoe UI", 8),
                 fg=_C["fg_bright"], bg=_C["input"],
                 padx=8, pady=5).pack(padx=1, pady=1)

        tip.update_idletasks()
        tw = tip.winfo_reqwidth()
        th = tip.winfo_reqheight()
        wx = self._widget.winfo_rootx()
        wy = self._widget.winfo_rooty()
        ww = self._widget.winfo_width()
        sw = tip.winfo_screenwidth()

        # Centre horizontally over the widget; always place ABOVE it
        x = max(4, min(wx + (ww - tw) // 2, sw - tw - 4))
        y = max(4, wy - th - 6)

        tip.geometry(f"+{x}+{y}")
        self._tip = tip


# ---------------------------------------------------------------------------
# Setup window
# ---------------------------------------------------------------------------

class SetupWindow:
    def __init__(self) -> None:
        self.root   = tk.Tk()
        self.result: Optional[dict] = None
        self.root.title("Jira Overlay — Setup")
        self.root.configure(bg=_C["bg_dark"])
        self.root.resizable(False, False)
        self._build()
        self.root.mainloop()

    def _build(self) -> None:
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"430x295+{sw // 2 - 215}+{sh // 2 - 147}")

        tk.Label(self.root, text="Jira Overlay Setup",
                 font=("Segoe UI", 14, "bold"),
                 fg=_C["fg_bright"], bg=_C["bg_dark"]
                 ).grid(row=0, column=0, columnspan=2, pady=(20, 16))

        fields = [
            ("Jira Domain", "yourcompany.atlassian.net", False),
            ("Email",       "you@company.com",           False),
            ("API Token",   "paste your token here",     True),
        ]
        self._vars: dict[str, tuple[tk.StringVar, str]] = {}
        for i, (label, hint, secret) in enumerate(fields, 1):
            tk.Label(self.root, text=f"{label}:", font=("Segoe UI", 9),
                     fg=_C["fg"], bg=_C["bg_dark"], anchor="e"
                     ).grid(row=i, column=0, sticky="e", padx=(20, 8), pady=6)
            var = tk.StringVar()
            entry = tk.Entry(self.root, textvariable=var, width=36,
                             show="*" if secret else "",
                             bg=_C["input"], fg=_C["fg_dim"],
                             insertbackground="white", relief="flat",
                             font=("Segoe UI", 9))
            entry.grid(row=i, column=1, sticky="ew", padx=(0, 20), pady=6, ipady=5)
            entry.insert(0, hint)
            entry.bind("<FocusIn>",
                       lambda ev, w=entry, h=hint:
                           (w.get() == h) and (w.delete(0, "end") or w.config(fg=_C["fg_bright"])))
            entry.bind("<FocusOut>",
                       lambda ev, w=entry, h=hint:
                           (not w.get()) and (w.insert(0, h) or w.config(fg=_C["fg_dim"])))
            self._vars[label] = (var, hint)

        token_url = "https://id.atlassian.com/manage-profile/security/api-tokens"
        link = tk.Label(self.root,
                        text="🔑  Generate an API token at id.atlassian.com",
                        font=("Segoe UI", 8, "underline"),
                        fg=_C["accent"], bg=_C["bg_dark"], cursor="hand2")
        link.grid(row=4, column=0, columnspan=2, pady=(0, 8))
        link.bind("<ButtonRelease-1>", lambda _e: webbrowser.open(token_url))
        link.bind("<Enter>", lambda _e: link.config(fg="#3399ff"))
        link.bind("<Leave>", lambda _e: link.config(fg=_C["accent"]))

        tk.Button(self.root, text="Save & Launch", command=self._save,
                  bg=_C["accent"], fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=8, cursor="hand2",
                  activebackground=_C["accent_hi"], activeforeground="white"
                  ).grid(row=5, column=0, columnspan=2, pady=(4, 20))
        self.root.columnconfigure(1, weight=1)

    def _save(self) -> None:
        values: dict[str, str] = {}
        for label, (var, hint) in self._vars.items():
            v = var.get().strip()
            if not v or v == hint:
                messagebox.showerror("Missing", f"{label} is required.", parent=self.root)
                return
            values[label] = v
        domain = (values["Jira Domain"].strip("/")
                  .replace("https://", "").replace("http://", ""))
        self.result = {"domain": domain, "email": values["Email"],
                       "token": values["API Token"]}
        save_config(self.result)
        self.root.destroy()


# ---------------------------------------------------------------------------
# Service desk picker
# ---------------------------------------------------------------------------

class PickDeskWindow:
    def __init__(self, desks: list[dict]) -> None:
        self.root   = tk.Tk()
        self.result: Optional[dict] = None
        self.root.title("Jira Overlay — Pick Desk")
        self.root.configure(bg=_C["bg_dark"])
        self.root.resizable(False, False)
        self._build(desks)
        self.root.mainloop()

    def _build(self, desks: list[dict]) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"360x{90 + len(desks) * 44}+{sw // 2 - 180}+{sh // 2 - 100}")
        tk.Label(self.root, text="Which service desk to monitor?",
                 font=("Segoe UI", 11, "bold"),
                 fg=_C["fg_bright"], bg=_C["bg_dark"]).pack(pady=(18, 10))
        for desk in desks:
            tk.Button(self.root,
                      text=f"{desk['projectName']}  ({desk['projectKey']})",
                      command=lambda d=desk: self._pick(d),
                      bg=_C["input"], fg=_C["fg_bright"], font=("Segoe UI", 10),
                      relief="flat", padx=12, pady=8, cursor="hand2",
                      activebackground=_C["fg_dim"], activeforeground="white"
                      ).pack(fill="x", padx=24, pady=4)

    def _pick(self, desk: dict) -> None:
        self.result = desk
        self.root.destroy()


# ---------------------------------------------------------------------------
# Newly-opened ticket tooltip
# ---------------------------------------------------------------------------

class NewTicketTooltip:
    """Frameless popup listing alert-queue tickets; each row opens in browser."""

    def __init__(self, parent: tk.Tk, issues: list[dict],
                 domain: str, on_mark_seen=None) -> None:
        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=_C["input"])

        if not issues:
            tk.Label(self.win, text="  No tickets  ",
                     font=("Segoe UI", 9), fg=_C["fg"],
                     bg=_C["input"], pady=8).pack()
            return

        tk.Label(self.win, text="  ALERT QUEUE — click to open  ",
                 font=("Segoe UI", 7, "bold"), fg=_C["fg_dim"],
                 bg=_C["input"], pady=4).pack(fill="x")
        tk.Frame(self.win, bg=_C["fg_dim"], height=1).pack(fill="x")

        for issue in issues[:15]:
            key     = issue.get("issueKey") or issue.get("key", "")
            summary = (issue.get("fields") or {}).get("summary", "(no summary)")
            short   = summary[:55] + "…" if len(summary) > 55 else summary
            url     = f"https://{domain}/browse/{key}"
            self._make_issue_row(url, key, short)

        if on_mark_seen:
            tk.Frame(self.win, bg=_C["fg_dim"], height=1).pack(fill="x", pady=(4, 0))
            tk.Button(self.win, text="✓  Mark all as seen",
                      command=lambda: (on_mark_seen(), self.destroy()),
                      bg=_C["input"], fg=_C["ok"], font=("Segoe UI", 8),
                      relief="flat", pady=4, cursor="hand2",
                      activebackground="#3d4f6e", activeforeground=_C["ok"]
                      ).pack(fill="x", padx=8, pady=(0, 4))

    def _make_issue_row(self, url: str, key: str, summary: str) -> None:
        row = tk.Frame(self.win, bg=_C["input"], cursor="hand2")
        row.pack(fill="x", padx=8, pady=2)
        lbl_key = tk.Label(row, text=key, font=("Segoe UI", 8, "bold"),
                           fg=_C["accent"], bg=_C["input"], width=10, anchor="w")
        lbl_key.pack(side="left")
        lbl_txt = tk.Label(row, text=summary, font=("Segoe UI", 8),
                           fg=_C["fg"], bg=_C["input"], anchor="w")
        lbl_txt.pack(side="left")

        def _hover_in(_e: tk.Event, r=row) -> None:
            for w in [r, *r.winfo_children()]:
                try:
                    w.configure(bg="#3d4f6e")
                except tk.TclError:
                    pass

        def _hover_out(_e: tk.Event, r=row) -> None:
            for w in [r, *r.winfo_children()]:
                try:
                    w.configure(bg=_C["input"])
                except tk.TclError:
                    pass

        for widget in (row, lbl_key, lbl_txt):
            widget.bind("<ButtonRelease-1>", lambda _e, u=url: webbrowser.open(u))
            widget.bind("<Enter>", _hover_in)
            widget.bind("<Leave>", _hover_out)

    def place(self, x: int, y: int, screen_w: int, screen_h: int) -> None:
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()
        tx = max(0, min(x, screen_w - w - 10))
        ty = max(0, y - h - 6)
        self.win.geometry(f"+{tx}+{ty}")

    def destroy(self) -> None:
        self.win.destroy()


# ---------------------------------------------------------------------------
# Main overlay
# ---------------------------------------------------------------------------

class JiraOverlay:
    """
    Main application window.

    Instance attributes
    -------------------
    config            Current config dict (mutated in place, flushed via save_config)
    queues            Visible queue dicts after filtering
    _all_queue_names  All queue names before hidden-queue filter (for Settings)
    new_issues        Issues currently in the alert queue
    completed_today   Count (int or "N+") of tickets closed today
    sla_compliance    Compliance % or None if SLA unavailable
    _sla_unavailable  True once confirmed the SLA JQL field doesn't exist
    _settings_changed Re-fetch flag set when settings saved during an in-flight fetch
    _in_settings      True while the inline settings panel is showing
    _anim_job         Pending root.after() id for the settings fade animation
    _queue_geom       (w,h,x,y) saved before settings panel expands
    loading           True while a background fetch is running
    error_msg         Last fetch error string or None
    _error_count      Consecutive error count (drives exponential back-off)
    _prev_newly       Alert-queue count from the previous cycle (None = first)
    _seen_newly_ids   Set of ticket keys already notified (deduplication)
    _snooze_until     Datetime when snooze expires, or None
    _prev_counts      {queue_name: last_count} for flash-on-change detection
    _last_refresh     Datetime of the last successful fetch
    _visible          True when the overlay window is shown (not withdrawn)
    _fading           True while a show/hide alpha fade is running
    _show_job         Pending root.after() id for a delayed _do_show call
    _tooltip          Active NewTicketTooltip, or None
    _refresh_job      Pending root.after() id for the next auto-refresh
    _tray             pystray.Icon instance
    _tray_last        (count, alert) tuple from last tray update (skip dup redraws)
    _sw / _sh         Screen width / height (pixels)
    """

    _SETTINGS_W = 420   # overlay width when inline settings are open

    def __init__(self, config: dict) -> None:
        self.config            = config
        self.queues: list[dict]      = []
        self._all_queue_names: list[str] = []
        self.new_issues: list[dict]  = []
        self.completed_today         = 0
        self.sla_compliance: Optional[int] = None
        self._sla_unavailable  = False
        self._settings_changed = False
        self._in_settings      = False
        self._anim_job: Optional[str] = None
        self._queue_geom: Optional[tuple[int, int, int, int]] = None
        self.loading           = False
        self.error_msg: Optional[str] = None   # hard error requiring user action
        self._conn_error: bool = False          # transient network error (keep last data)
        self._error_count      = 0
        self._prev_newly: Optional[int] = None
        self._seen_newly_ids: set[str]  = set()
        self._snooze_until: Optional[datetime] = None
        self._prev_counts: dict[str, int] = {}
        self._last_refresh: Optional[datetime] = None
        self._visible          = False
        self._fading           = False
        self._show_job: Optional[str] = None
        self._tooltip: Optional[NewTicketTooltip] = None
        self._refresh_job: Optional[str] = None
        self._tray: Optional[pystray.Icon] = None
        self._tray_last: tuple[int, bool] = (-1, False)
        # Drag state
        self._drag_x0 = self._drag_y0 = 0
        self._drag_win_x = self._drag_win_y = 0
        self._did_drag = False

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0)
        self.root.configure(bg=_C["border"])   # 1-px accent border visible between root and frame
        self.root.geometry("260x60+100+100")
        self._sw = self.root.winfo_screenwidth()
        self._sh = self.root.winfo_screenheight()

        self._build_ui()
        self.root.withdraw()
        self._start_tray()
        self._fetch()
        self.root.after(30_000, self._update_relative_time)

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def _alert_queues(self) -> set[str]:
        """Set of queue names that trigger overlay + notifications."""
        cfg = self.config
        names = cfg.get("alertQueues") or ([cfg["alertQueue"]] if cfg.get("alertQueue") else [])
        return {n for n in names if n}

    def _jira_url(self, path: str = "") -> str:
        """Build a base Jira URL for this instance."""
        dom = self.config.get("domain", "")
        return f"https://{dom}{path}"

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _start_tray(self) -> None:
        pk        = self.config.get("projectKey", "")
        desk_name = self.config.get("serviceDeskName", "Service Desk")
        sd_path   = (f"/jira/servicedesk/projects/{pk}/queues" if pk
                     else "/jira/servicedesk")
        sd_url    = self._jira_url(sd_path)

        menu = pystray.Menu(
            pystray.MenuItem("Show / Hide", self._tray_toggle, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit",        lambda: self.root.after(0, self.root.destroy)),
        )
        self._tray = pystray.Icon("JiraOverlay", _make_tray_image(0, False),
                                  "Jira Overlay", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_toggle(self) -> None:
        if self._visible:
            self.root.after(0, self._do_hide)
        else:
            self.root.after(0, self._do_show)

    def _update_tray(self, count: int, alert: bool,
                     queue_name: str = "Newly Opened") -> None:
        """Refresh tray icon and tooltip; skips if state is unchanged."""
        if not self._tray or (count, alert) == self._tray_last:
            return
        self._tray_last = (count, alert)
        self._tray.icon  = _make_tray_image(count, alert)
        noun = "ticket" if count == 1 else "tickets"
        self._tray.title = (f"Jira — {queue_name}: {count} {noun}"
                            if count else f"Jira — {queue_name}: 0")

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        BG = _C["bg"]
        self.frame = tk.Frame(self.root, bg=BG, padx=12, pady=8)
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        # Header
        hdr = tk.Frame(self.frame, bg=BG)
        hdr.pack(fill="x")
        desk = self.config.get("serviceDeskName", "JIRA")
        self.lbl_title = tk.Label(hdr, text=f"● {desk.upper()}",
                                  font=("Segoe UI", 8, "bold"),
                                  fg=_C["accent"], bg=BG, cursor="hand2")
        self.lbl_title.pack(side="left")
        self.lbl_title.bind("<Enter>", lambda _e: self.lbl_title.config(fg="#3399ff"))
        self.lbl_title.bind("<Leave>", lambda _e: self.lbl_title.config(fg=_C["accent"]))
        self.lbl_title.bind("<ButtonRelease-1>", lambda _e: self._open_service_desk())
        _Tooltip(self.lbl_title, "Open service desk in Jira")
        self.btn_gear = tk.Label(hdr, text="⚙", font=("Segoe UI", 10),
                                 fg=_C["fg_dim"], bg=BG, cursor="hand2")
        self.btn_gear.pack(side="right")
        self.btn_gear.bind("<ButtonRelease-1>", lambda _e: self._toggle_settings())
        self.btn_gear.bind("<Enter>", lambda _e: self.btn_gear.config(fg=_C["fg"]))
        self.btn_gear.bind("<Leave>", lambda _e: self.btn_gear.config(fg=_C["fg_dim"]))
        _Tooltip(self.btn_gear, "Settings")

        tk.Frame(self.frame, bg=_C["divider"], height=1).pack(fill="x", pady=(4, 4))

        # Main (queue) panel
        self.main_panel = tk.Frame(self.frame, bg=BG)
        self.main_panel.pack(fill="both", expand=True)

        self.rows_frame = tk.Frame(self.main_panel, bg=BG)
        self.rows_frame.pack(fill="both", expand=True)
        self.row_widgets: dict[str, dict] = {}

        tk.Frame(self.main_panel, bg=_C["divider"], height=1).pack(fill="x", pady=(6, 2))
        done_row = tk.Frame(self.main_panel, bg=BG, cursor="hand2")
        done_row.pack(fill="x", pady=1)
        self.lbl_done = tk.Label(done_row, text="✓  Completed today — —",
                                 font=("Segoe UI", 9), fg=_C["ok"], bg=BG,
                                 cursor="hand2")
        self.lbl_done.pack(side="left")
        self.lbl_sla = tk.Label(done_row, text="",
                                font=("Segoe UI", 8, "bold"), fg=_C["ok"], bg=BG,
                                cursor="hand2")
        self.lbl_sla.pack(side="right")

        def _done_hover(bg: str) -> None:
            for w in (done_row, self.lbl_done, self.lbl_sla):
                w.configure(bg=bg)

        for w in (done_row, self.lbl_done, self.lbl_sla):
            w.bind("<ButtonPress-1>",   self._drag_press)
            w.bind("<B1-Motion>",       self._drag_move)
            w.bind("<ButtonRelease-1>", lambda _e: self._open_completed_today())
            w.bind("<Enter>", lambda _e: _done_hover("#1e3a2e"))
            w.bind("<Leave>", lambda _e: _done_hover(BG))
        _Tooltip(self.lbl_done, "Open completed tickets in Jira")

        tk.Frame(self.main_panel, bg=_C["divider"], height=1).pack(fill="x", pady=(4, 2))

        # Footer: action buttons + status
        footer = tk.Frame(self.main_panel, bg=BG)
        footer.pack(fill="x")
        self.btn_snooze_icon = self._footer_btn(
            footer, "💤", self._snooze_cycle, tip="Snooze alerts")
        self._footer_btn(footer, "↻", self._fetch,
                         tip="Refresh now").pack(side="left", padx=(0, 8))
        self.btn_snooze_icon.pack(side="left", padx=(0, 8))
        self._footer_btn(footer, "⊟", self._do_hide,
                         tip="Hide overlay").pack(side="left", padx=(0, 8))
        self._footer_btn(footer, "✕", self.root.destroy,
                         color=_C["alert"], tip="Quit").pack(side="left")
        self.lbl_status = tk.Label(footer, text="Connecting…",
                                   font=("Segoe UI", 7),
                                   fg=_C["fg_dim"], bg=BG, anchor="e")
        self.lbl_status.pack(side="right")

        # Settings panel (hidden until ⚙ clicked)
        self.settings_panel = tk.Frame(self.frame, bg=BG)

        # Bind drag to structural chrome widgets
        for w in (self.root, self.frame, hdr):
            self._bind_drag(w)
        # Title participates in drag press/move but release opens the service desk
        self.lbl_title.bind("<ButtonPress-1>", self._drag_press)
        self.lbl_title.bind("<B1-Motion>",     self._drag_move)
        self.lbl_title.bind("<ButtonRelease-1>",
                            lambda _e: self._open_service_desk()
                            if not self._did_drag else self._drag_release(_e))
        hdr.bind("<Double-Button-1>",            lambda _e: self._snap_to_nearest_corner())
        self.lbl_title.bind("<Double-Button-1>", lambda _e: self._snap_to_nearest_corner())

    @staticmethod
    def _footer_btn(parent: tk.Frame, text: str, cmd,
                    color: str = _C["fg_dim"],
                    tip: str = "") -> tk.Label:
        btn = tk.Label(parent, text=text, font=("Segoe UI", 10),
                       fg=color, bg=_C["bg"], cursor="hand2")
        btn.bind("<ButtonRelease-1>", lambda _e: cmd())
        btn.bind("<Enter>",  lambda _e: btn.config(fg=_C["fg_bright"]))
        btn.bind("<Leave>",  lambda _e: btn.config(fg=color))
        btn.bind("<ButtonPress-1>",   lambda e: None)
        if tip:
            _Tooltip(btn, tip)
        return btn

    def _bind_drag(self, w: tk.Widget) -> None:
        w.bind("<ButtonPress-1>",   self._drag_press)
        w.bind("<B1-Motion>",       self._drag_move)
        w.bind("<ButtonRelease-1>", self._drag_release)

    def _make_row(self, queue: dict) -> dict:
        name   = queue["name"]
        qid    = queue["id"]
        is_new = name in self._alert_queues
        w_cfg  = self.config.get("overlayWidth", 260)
        BG     = _C["bg"]

        nc = _C["info"]  if is_new else _C["fg"]
        cc = _C["info"]  if is_new else _C["fg_bright"]
        max_chars = max(12, (w_cfg - 100) // 7)
        label = name if len(name) <= max_chars else name[:max_chars - 1] + "…"

        row = tk.Frame(self.rows_frame, bg=BG, cursor="hand2")
        row.pack(fill="x", pady=1)
        lbl_name  = tk.Label(row, text=label, font=("Segoe UI", 9),
                             fg=nc, bg=BG, anchor="w")
        lbl_name.pack(side="left")
        dot = tk.Label(row, text="", font=("Segoe UI", 9),
                       fg=_C["alert"], bg=BG)
        dot.pack(side="right")
        lbl_count = tk.Label(row, text="—", font=("Segoe UI", 9, "bold"),
                             fg=cc, bg=BG, width=5, anchor="e")
        lbl_count.pack(side="right")

        pk  = self.config.get("projectKey", "")
        url = (self._jira_url(f"/jira/servicedesk/projects/{pk}/queues/custom/{qid}")
               if pk else self._jira_url("/jira/servicedesk"))

        for w in (row, lbl_name, lbl_count, dot):
            w.bind("<ButtonPress-1>",   self._drag_press)
            w.bind("<B1-Motion>",       self._drag_move)
            w.bind("<ButtonRelease-1>", lambda e, u=url: self._row_click(e, u))

        if is_new:
            for w in (row, lbl_name, lbl_count, dot):
                w.bind("<Enter>", self._tooltip_show)
                w.bind("<Leave>", self._tooltip_hide)
        else:
            _Tooltip(row, "Click to open queue in Jira")

        return {"row": row, "lbl_name": lbl_name,
                "lbl_count": lbl_count, "dot": dot,
                "is_new": is_new, "base_cc": cc}

    # ── Drag ─────────────────────────────────────────────────────────────────

    def _drag_press(self, e: tk.Event) -> None:
        self._drag_x0    = e.x_root
        self._drag_y0    = e.y_root
        self._drag_win_x = self.root.winfo_x()
        self._drag_win_y = self.root.winfo_y()
        self._did_drag   = False

    def _drag_move(self, e: tk.Event) -> None:
        dx = e.x_root - self._drag_x0
        dy = e.y_root - self._drag_y0
        if abs(dx) > 4 or abs(dy) > 4:
            self._did_drag = True
        self.root.geometry(f"+{self._drag_win_x + dx}+{self._drag_win_y + dy}")

    def _drag_release(self, e: tk.Event) -> None:
        if not self._did_drag:
            self._did_drag = False
            return
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        corners = _corner_positions(self._sw, self._sh, w, h)
        snapped = next(
            (name for name, (cx, cy) in corners.items()
             if abs(x - cx) < _SNAP_MARGIN and abs(y - cy) < _SNAP_MARGIN),
            None,
        )
        self.config.pop("lastX", None)
        self.config.pop("lastY", None)
        self.config.pop("snapCorner", None)
        if snapped:
            cx, cy = corners[snapped]
            self.root.geometry(f"+{cx}+{cy}")
            self.config["snapCorner"] = snapped
        else:
            self.config["lastX"], self.config["lastY"] = x, y
        save_config(self.config)
        self._did_drag = False

    def _row_click(self, _e: tk.Event, url: str) -> None:
        if not self._did_drag:
            webbrowser.open(url)
        self._did_drag = False

    def _snooze_cycle(self) -> None:
        """If snoozed, cancel immediately; otherwise show duration picker."""
        if self._is_snoozed():
            self._cancel_snooze()
        else:
            self._show_snooze_popup()

    def _show_snooze_popup(self) -> None:
        """Small popup with snooze duration choices, positioned above the 💤 button."""
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=_C["divider"])   # 1-px border effect

        inner = tk.Frame(popup, bg=_C["input"])
        inner.pack(padx=1, pady=1)

        for label, minutes in [("Snooze 15 min", 15),
                                ("Snooze 30 min", 30),
                                ("Snooze 1 hour", 60)]:
            tk.Button(inner, text=label,
                      command=lambda m=minutes, p=popup: (p.destroy(), self._snooze(m)),
                      bg=_C["input"], fg=_C["fg_bright"],
                      font=("Segoe UI", 9), relief="flat",
                      padx=16, pady=5, cursor="hand2",
                      activebackground=_C["accent"],
                      activeforeground="white").pack(fill="x")

        popup.update_idletasks()
        bx = self.btn_snooze_icon.winfo_rootx()
        by = self.btn_snooze_icon.winfo_rooty()
        ph = popup.winfo_reqheight()
        popup.geometry(f"+{bx}+{max(0, by - ph - 4)}")

        popup.focus_force()
        popup.bind("<FocusOut>",
                   lambda _e: popup.destroy() if popup.winfo_exists() else None)

    # ── Corner snapping ───────────────────────────────────────────────────────

    def _snap_to_nearest_corner(self) -> None:
        self.root.update_idletasks()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        corners = _corner_positions(self._sw, self._sh, w, h)
        name, (cx, cy) = min(
            corners.items(),
            key=lambda kv: (x - kv[1][0]) ** 2 + (y - kv[1][1]) ** 2,
        )
        self.root.geometry(f"+{cx}+{cy}")
        self.config.pop("lastX", None)
        self.config.pop("lastY", None)
        self.config["snapCorner"] = name
        save_config(self.config)

    # ── Tooltip ───────────────────────────────────────────────────────────────

    def _tooltip_show(self, _e: tk.Event) -> None:
        if self._tooltip or not self.new_issues:
            return
        self._tooltip = NewTicketTooltip(
            self.root, self.new_issues, self.config["domain"],
            on_mark_seen=self._mark_all_seen)
        self._tooltip.place(self.root.winfo_x(), self.root.winfo_y(),
                            self._sw, self._sh)

    def _tooltip_hide(self, e: tk.Event) -> None:
        rx = self.root.winfo_rootx()
        ry = self.root.winfo_rooty()
        rw = self.root.winfo_width()
        rh = self.root.winfo_height()
        if not (rx <= e.x_root <= rx + rw and ry <= e.y_root <= ry + rh):
            if self._tooltip:
                self._tooltip.destroy()
                self._tooltip = None

    def _mark_all_seen(self) -> None:
        self._seen_newly_ids = set()

    def _open_service_desk(self) -> None:
        if self._did_drag:
            return
        pk  = self.config.get("projectKey", "")
        url = (self._jira_url(f"/jira/servicedesk/projects/{pk}/queues")
               if pk else self._jira_url("/jira/servicedesk"))
        webbrowser.open(url)

    def _open_completed_today(self) -> None:
        if self._did_drag:
            return
        pk  = self.config.get("projectKey", "")
        extra = self.config.get("completedTodayFilter", "").strip()
        jql = f"project = {pk} AND statusCategory = Done AND updated >= startOfDay()"
        if extra:
            jql += f" AND {extra}"
        webbrowser.open(self._jira_url(f"/issues/?jql={jql.replace(' ', '+')}"))

    # ── Service-desk discovery ────────────────────────────────────────────────

    def _discover_service_desk(self) -> Optional[dict]:
        """Probe accessible service desks; shows picker if multiple found."""
        cfg  = self.config
        auth = HTTPBasicAuth(cfg["email"], cfg["token"])
        try:
            r = requests.get(
                self._jira_url("/rest/servicedeskapi/servicedesk"),
                auth=auth, timeout=10)
            r.raise_for_status()
            desks = r.json().get("values", [])
        except Exception:
            return None

        accessible = [
            d for d in desks
            if requests.get(
                self._jira_url(f"/rest/servicedeskapi/servicedesk/{d['id']}/queue"),
                params={"limit": 1}, auth=auth, timeout=5
            ).ok
        ]

        if not accessible:
            return None
        if len(accessible) == 1:
            return accessible[0]

        # Multiple desks — ask the user (must run on main thread)
        self._desk_pick: Optional[dict] = None
        picked = threading.Event()

        def _show_picker() -> None:
            p = PickDeskWindow(accessible)
            self._desk_pick = p.result or accessible[0]
            picked.set()

        self.root.after(0, _show_picker)
        picked.wait(timeout=120)
        return self._desk_pick

    def _show_alert_queue_picker(self) -> None:
        """Modal queue picker — runs on main thread; re-fetches on selection."""
        win = tk.Toplevel()
        win.title("Jira Overlay — Choose Alert Queue")
        win.configure(bg=_C["bg_dark"])
        win.resizable(False, False)
        win.attributes("-topmost", True)

        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        tk.Label(win, text="Which queue should trigger alerts?",
                 font=("Segoe UI", 11, "bold"),
                 fg=_C["fg_bright"], bg=_C["bg_dark"]).pack(pady=(18, 4))
        tk.Label(win,
                 text="The overlay will appear and notifications will fire\n"
                      "when new tickets arrive in this queue.",
                 font=("Segoe UI", 8), fg=_C["fg_dim"],
                 bg=_C["bg_dark"], justify="center").pack(pady=(0, 10))

        container = tk.Frame(win, bg=_C["bg_dark"])
        container.pack(fill="both", expand=True, padx=20, pady=(0, 16))
        canvas = tk.Canvas(container, bg=_C["bg_dark"], highlightthickness=0, width=320)
        sb     = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        sf     = tk.Frame(canvas, bg=_C["bg_dark"])
        sf.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sf, anchor="nw", width=320)
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<MouseWheel>",
                    lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def pick(name: str) -> None:
            self.config["alertQueue"]  = name
            self.config["alertQueues"] = [name]
            save_config(self.config)
            win.destroy()

        for q in self.queues:
            tk.Button(sf, text=q["name"], command=lambda n=q["name"]: pick(n),
                      bg=_C["input"], fg=_C["fg_bright"], font=("Segoe UI", 10),
                      relief="flat", padx=12, pady=7, cursor="hand2",
                      activebackground=_C["accent"], activeforeground="white"
                      ).pack(fill="x", pady=3, padx=4)

        win.update_idletasks()
        btn_h   = min(sf.winfo_reqheight(), sh - 250)
        total_h = btn_h + 130
        win.geometry(f"380x{total_h}+{sw // 2 - 190}+{sh // 2 - total_h // 2}")
        canvas.configure(height=btn_h)
        win.focus_force()
        win.wait_window(win)

        if self.config.get("alertQueue"):
            self._fetch()

    # ── Fetch ─────────────────────────────────────────────────────────────────

    def _fetch(self) -> None:
        """Kick off a background data refresh (no-op if one is already running)."""
        if self.loading:
            return
        if self._refresh_job:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        self.loading = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self) -> None:
        try:
            self._fetch_data()
        except requests.HTTPError as exc:
            self._error_count += 1
            self.error_msg  = f"HTTP {exc.response.status_code} — check credentials"
            self._conn_error = False
            self.queues      = []
        except requests.ConnectionError:
            # Transient — keep last known queue data; show subtle status only
            self._error_count += 1
            self._conn_error   = True
        except Exception as exc:
            self._error_count += 1
            self.error_msg   = f"{type(exc).__name__}: {str(exc)[:40]}"
            self._conn_error  = False
            self.queues       = []
        finally:
            self.loading = False
            self.root.after(0, self._update_ui)
            if self._settings_changed:
                self._settings_changed = False
                self._refresh_job = self.root.after(0, self._fetch)
            else:
                base  = self.config.get("refreshSeconds", 30) * 1_000
                delay = min(base * (2 ** self._error_count), 5 * 60 * 1_000)
                self._refresh_job = self.root.after(delay, self._fetch)

    def _fetch_data(self) -> None:
        """Core data-fetch logic (called from worker thread)."""
        cfg  = self.config
        auth = HTTPBasicAuth(cfg["email"], cfg["token"])

        # Discover service desk on first run
        if not cfg.get("serviceDeskId"):
            desk = self._discover_service_desk()
            if not desk:
                self.error_msg = "No accessible service desk found"
                return
            cfg.update({
                "serviceDeskId":   desk["id"],
                "serviceDeskName": desk["projectName"],
                "projectKey":      desk.get("projectKey", ""),
            })
            save_config(cfg)
            self.root.after(0, lambda n=desk["projectName"]:
                self.lbl_title.config(text=f"● {n.upper()}"))

        sd_id = cfg["serviceDeskId"]

        # Backfill projectKey if absent (e.g. settings save stripped it)
        if not cfg.get("projectKey"):
            try:
                ri = requests.get(
                    self._jira_url(f"/rest/servicedeskapi/servicedesk/{sd_id}"),
                    auth=auth, timeout=10)
                if ri.ok:
                    cfg["projectKey"] = ri.json().get("projectKey", "")
                    save_config(cfg)
            except Exception:
                pass

        # Queue counts
        r = requests.get(
            self._jira_url(f"/rest/servicedeskapi/servicedesk/{sd_id}/queue"),
            params={"includeCount": "true"}, auth=auth, timeout=10)
        r.raise_for_status()

        skip_kw   = {k.strip().upper() for k in cfg.get("skipKeywords", []) if k.strip()}
        max_count = cfg.get("maxQueueSize", 0)
        hidden    = set(cfg.get("hiddenQueues", []))

        all_queues = [
            q for q in r.json().get("values", [])
            if not any(kw in q["name"].upper() for kw in skip_kw)
            and (max_count == 0 or q.get("issueCount", 0) <= max_count)
        ]
        self._all_queue_names = [q["name"] for q in all_queues]
        self.queues = [q for q in all_queues if q["name"] not in hidden]

        # Show alert-queue picker on first run
        alert_qs = self._alert_queues
        if not alert_qs and self.queues:
            self.root.after(0, self._show_alert_queue_picker)
            return

        # Fetch issues inside the alert queue (for tooltip)
        nq = next((q for q in self.queues if q["name"] in alert_qs), None)
        if nq and nq.get("issueCount", 0) > 0:
            ir = requests.get(
                self._jira_url(
                    f"/rest/servicedeskapi/servicedesk/{sd_id}/queue/{nq['id']}/issue"),
                params={"start": 0, "limit": 15}, auth=auth, timeout=10)
            self.new_issues = ir.json().get("values", []) if ir.ok else []
        else:
            self.new_issues = []

        # Completed today + SLA compliance
        self._fetch_completion_stats(cfg, auth)

        self.error_msg    = None
        self._conn_error  = False
        self._error_count = 0

    def _fetch_completion_stats(self, cfg: dict, auth: HTTPBasicAuth) -> None:
        """Fetch completed-today count and optional SLA compliance."""
        pk = cfg.get("projectKey", "")
        if not pk:
            self.completed_today = 0
            self.sla_compliance  = None
            return

        base_jql = f"project = {pk} AND statusCategory = Done AND updated >= startOfDay()"
        extra = cfg.get("completedTodayFilter", "").strip()
        if extra:
            base_jql += f" AND {extra}"

        try:
            cr = requests.post(
                self._jira_url("/rest/api/3/search/jql"),
                json={"jql": base_jql, "maxResults": 200, "fields": ["status"]},
                auth=auth, timeout=10)
            if not cr.ok:
                self.completed_today = 0
                self.sla_compliance  = None
                return

            data    = cr.json()
            total_n = len(data.get("issues", []))
            self.completed_today = (f"{total_n}+" if not data.get("isLast", True)
                                    else total_n)

            if self._sla_unavailable or total_n == 0:
                self.sla_compliance = None
                return

            sla_field = cfg.get("slaField", "Time to resolution")
            sla_jql   = base_jql + f' AND "{sla_field}" = slaBreached()'
            sr = requests.post(
                self._jira_url("/rest/api/3/search/jql"),
                json={"jql": sla_jql, "maxResults": 200, "fields": ["status"]},
                auth=auth, timeout=10)
            if sr.ok:
                breached = len(sr.json().get("issues", []))
                self.sla_compliance = round((total_n - breached) / total_n * 100)
            else:
                body = sr.text.lower()
                if any(token in body for token in ("does not exist", "unknown", "field")):
                    self._sla_unavailable = True
                self.sla_compliance = None

        except Exception:
            self.completed_today = 0
            self.sla_compliance  = None

    # ── Update UI ─────────────────────────────────────────────────────────────

    def _update_ui(self) -> None:
        if self._conn_error:
            # Transient network loss — keep existing rows, show subtle status
            self.lbl_status.config(text="Reconnecting…", fg=_C["warn"])
            return

        if self.error_msg:
            # Hard error (auth failure, server error) — show prominently
            for rw in self.row_widgets.values():
                rw["row"].destroy()
            self.row_widgets.clear()
            err = tk.Label(self.rows_frame, text=self.error_msg,
                           font=("Segoe UI", 8), fg=_C["alert"], bg=_C["bg"])
            err.pack()
            self._bind_drag(err)
            self.lbl_status.config(text="Tap ↻ to retry", fg=_C["alert"])
            self.root.after(10, self._resize)
            return

        # Sync row widgets with current queue list
        current_names = {q["name"] for q in self.queues}
        for name in list(self.row_widgets):
            if name not in current_names:
                self.row_widgets[name]["row"].destroy()
                del self.row_widgets[name]

        alert_qs    = self._alert_queues
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
                rw["lbl_count"].config(fg=_C["alert"] if count > 0 else _C["info"])
                rw["dot"].config(text="●" if count > 0 else "")

            prev = self._prev_counts.get(name)
            if prev is not None and count != prev:
                self._flash_row(rw)
            self._prev_counts[name] = count

        # Deduplication — notify only for genuinely new ticket IDs
        current_ids = {i.get("issueKey") or i.get("key", "") for i in self.new_issues}
        truly_new   = current_ids - self._seen_newly_ids
        self._seen_newly_ids = current_ids

        snoozed       = self._is_snoozed()
        has_new_alert = bool(truly_new) and self._prev_newly is not None

        # Sync snooze button colour
        self.btn_snooze_icon.config(fg=_C["warn"] if snoozed else _C["fg_dim"])

        if has_new_alert and not snoozed:
            # Build a lookup of key → summary from the already-fetched issues
            summaries = {
                (i.get("issueKey") or i.get("key", "")): (
                    (i.get("fields") or {}).get("summary", "")
                )
                for i in self.new_issues
            }
            if len(truly_new) == 1:
                key     = next(iter(truly_new))
                summary = summaries.get(key, "")
                title   = f"Jira — {key}"
                body    = summary[:120] if summary else "New ticket in alert queue"
            else:
                title   = f"Jira — {len(truly_new)} new tickets"
                lines   = []
                for key in list(truly_new)[:3]:
                    s = summaries.get(key, "")
                    lines.append(f"{key}: {s[:60]}" if s else key)
                if len(truly_new) > 3:
                    lines.append(f"…and {len(truly_new) - 3} more")
                body = "\n".join(lines)
            if self.config.get("notificationsEnabled", True):
                send_toast(title, body)
            if self.config.get("soundEnabled", True):
                play_alert()

        self._prev_newly   = newly_count
        self._last_refresh = datetime.now()

        self.lbl_done.config(text=f"✓  Completed today — {self.completed_today}")
        if self.sla_compliance is not None:
            pct   = self.sla_compliance
            color = (_C["ok"] if pct >= 95 else
                     _C["warn"] if pct >= 80 else _C["alert"])
            self.lbl_sla.config(text=f"SLA {pct}%", fg=color)
        else:
            self.lbl_sla.config(text="")

        tray_queue = self.config.get("trayBadgeQueue", "Newly Opened")
        tray_count = next(
            (q.get("issueCount", 0) for q in self.queues
             if tray_queue.lower() in q["name"].lower()),
            newly_count,
        )
        self._update_tray(tray_count, newly_count > 0, tray_queue)

        if snoozed:
            until = self._snooze_until.strftime("%H:%M")  # type: ignore[union-attr]
            self.lbl_status.config(text=f"Snoozed until {until}", fg=_C["warn"])
        else:
            self.lbl_status.config(text="Updated just now", fg=_C["fg_dim"])

        show = (newly_count > 0 or self.config.get("alwaysVisible", False)) and not snoozed
        if show:
            self.root.after(10, self._resize)
            if not self._visible and not self._show_job:
                delay = 2_500 if has_new_alert else 0
                self._show_job = self.root.after(delay, self._do_show)
        else:
            if self._show_job:
                self.root.after_cancel(self._show_job)
                self._show_job = None
            if self._visible:
                self._do_hide()

    # ── Row flash ─────────────────────────────────────────────────────────────

    def _flash_row(self, rw: dict) -> None:
        row      = rw["row"]
        children = list(row.winfo_children())

        def pulse(step: int = 0) -> None:
            bg = "#2a3f5f" if step % 2 == 0 else _C["bg"]
            try:
                for w in [row, *children]:
                    w.configure(bg=bg)
            except tk.TclError:
                return
            if step < 5:
                self.root.after(100, lambda: pulse(step + 1))

        pulse()

    # ── Relative time ──────────────────────────────────────────────────────────

    def _update_relative_time(self) -> None:
        if self._last_refresh and not self.error_msg and not self._conn_error and not self._is_snoozed():
            secs = (datetime.now() - self._last_refresh).total_seconds()
            if secs < 60:
                rel = "just now"
            elif secs < 3_600:
                rel = f"{int(secs // 60)}m ago"
            else:
                rel = f"{int(secs // 3_600)}h ago"
            self.lbl_status.config(text=f"Updated {rel}", fg=_C["fg_dim"])
        self.root.after(30_000, self._update_relative_time)

    # ── Snooze ────────────────────────────────────────────────────────────────

    def _snooze(self, minutes: int) -> None:
        self._snooze_until = datetime.now() + timedelta(minutes=minutes)
        self._update_ui()   # immediate: amber button, status text, hide if needed

    def _cancel_snooze(self) -> None:
        self._snooze_until = None
        self._update_ui()

    def _is_snoozed(self) -> bool:
        if self._snooze_until and datetime.now() < self._snooze_until:
            return True
        self._snooze_until = None
        return False

    # ── Show / hide with alpha fade ───────────────────────────────────────────

    def _do_show(self) -> None:
        self._show_job = None
        if self._visible or self._fading:
            return
        self._fading  = True
        self._visible = True
        target = self.config.get("alpha", 0.93)
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self._resize()
        self._fade(0.0, target, target / 14)

    def _do_hide(self) -> None:
        if not self._visible or self._fading:
            return
        if self._tooltip:
            self._tooltip.destroy()
            self._tooltip = None
        self._fading  = True
        self._visible = False
        alpha = float(self.root.attributes("-alpha"))
        self._fade(alpha, 0.0, -(alpha / 14), on_done=self.root.withdraw)

    def _fade(self, current: float, target: float,
              step: float, on_done=None) -> None:
        nxt  = current + step
        done = (step > 0 and nxt >= target) or (step < 0 and nxt <= target)
        self.root.attributes("-alpha", max(0.0, min(1.0, target if done else nxt)))
        if done:
            self._fading = False
            if on_done:
                on_done()
        else:
            self.root.after(14, lambda: self._fade(nxt, target, step, on_done))

    # ── Resize / reposition ───────────────────────────────────────────────────

    def _resize(self) -> None:
        self.root.update_idletasks()
        w = self.config.get("overlayWidth", 260)
        self.root.geometry(f"{w}x{self.frame.winfo_reqheight() + 4}")
        self._reposition()

    def _reposition(self) -> None:
        self.root.update_idletasks()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        corners = _corner_positions(self._sw, self._sh, w, h)

        corner = self.config.get("snapCorner")
        if corner and corner in corners:
            x, y = corners[corner]
        elif "lastX" in self.config and "lastY" in self.config:
            x, y = self.config["lastX"], self.config["lastY"]
        else:
            x, y = corners["bottom-right"]

        mr = get_monitor_rect(x + w // 2, y + h // 2)
        if mr:
            ml, mt, mr2, mb = mr
            x = max(ml, min(x, mr2 - w))
            y = max(mt, min(y, mb - h - 4))
        else:
            x, y = corners["bottom-right"]
            self.config.pop("lastX", None)
            self.config.pop("lastY", None)
            self.config["snapCorner"] = "bottom-right"

        self.root.geometry(f"+{x}+{y}")

    # ── Inline settings ───────────────────────────────────────────────────────

    def _toggle_settings(self) -> None:
        if self._in_settings:
            self._close_settings()
            return
        self._in_settings = True
        if not self._visible:
            self._visible = True
            self.root.attributes("-alpha", self.config.get("alpha", 0.93))
            self.root.deiconify()
            self.root.update_idletasks()

        self._queue_geom = (
            self.root.winfo_width(), self.root.winfo_height(),
            self.root.winfo_x(),    self.root.winfo_y(),
        )
        self.btn_gear.config(text="✕")
        self.main_panel.pack_forget()
        self._build_settings_panel()
        self.settings_panel.pack(fill="both", expand=True)

        alpha = float(self.root.attributes("-alpha"))
        self._sfade(alpha, 0.0, on_done=self._apply_settings_geom)

    def _apply_settings_geom(self) -> None:
        cw, ch, cx, cy = self._queue_geom  # type: ignore[misc]
        tw  = self._SETTINGS_W
        tx  = self._sw - tw - 20
        bot = cy + ch
        ty  = max(20, bot - int(self._sh * 0.9))
        self.root.geometry(f"{tw}x{bot - ty}+{tx}+{ty}")
        self._sfade(0.0, self.config.get("alpha", 0.93))

    def _close_settings(self) -> None:
        alpha = float(self.root.attributes("-alpha"))
        self._sfade(alpha, 0.0, on_done=self._apply_queue_geom)

    def _apply_queue_geom(self) -> None:
        self._in_settings = False
        self.btn_gear.config(text="⚙")
        self.settings_panel.pack_forget()
        self.main_panel.pack(fill="both", expand=True)
        if self._queue_geom:
            qw, qh, qx, qy = self._queue_geom
            self.root.geometry(f"{qw}x{qh}+{qx}+{qy}")
        self._sfade(0.0, self.config.get("alpha", 0.93))

    def _sfade(self, current: float, target: float, on_done=None) -> None:
        """Settings-specific alpha fade (independent of the _fading overlay flag)."""
        if self._anim_job:
            self.root.after_cancel(self._anim_job)
            self._anim_job = None
        step = (target - current) / 10
        self._sfade_step(current, target, step, on_done)

    def _sfade_step(self, current: float, target: float,
                    step: float, on_done) -> None:
        nxt  = current + step
        done = (step > 0 and nxt >= target) or (step < 0 and nxt <= target)
        self.root.attributes("-alpha", max(0.0, min(1.0, target if done else nxt)))
        if done:
            self._anim_job = None
            if on_done:
                on_done()
        else:
            self._anim_job = self.root.after(
                16, lambda: self._sfade_step(nxt, target, step, on_done))

    def _build_settings_panel(self) -> None:
        """Rebuild inline settings content from current config."""
        for child in self.settings_panel.winfo_children():
            child.destroy()

        cfg         = self.config
        queue_names = self._all_queue_names or [q["name"] for q in self.queues]
        BG, FG, SEL = _C["bg"], _C["fg"], _C["input"]

        # Scrollable body
        wrap   = tk.Frame(self.settings_panel, bg=BG)
        wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb     = tk.Scrollbar(wrap, orient="vertical", command=canvas.yview)
        body   = tk.Frame(canvas, bg=BG)
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        cw_id = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(cw_id, width=e.width))
        scroll_cmd = lambda e: canvas.yview_scroll(-1 * (e.delta // 120), "units")
        canvas.bind("<MouseWheel>", scroll_cmd)
        body.bind(  "<MouseWheel>", scroll_cmd)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # ── Section helpers (defined after body so they target body) ──────────
        def section(title: str) -> None:
            tk.Label(body, text=title.upper(), font=("Segoe UI", 7, "bold"),
                     fg=_C["fg_dim"], bg=BG).pack(anchor="w", padx=12, pady=(10, 0))
            tk.Frame(body, bg=_C["divider"], height=1).pack(fill="x", padx=12, pady=(2, 2))

        def check(parent: tk.Widget, text: str, var: tk.BooleanVar) -> None:
            tk.Checkbutton(parent, text=text, variable=var,
                           fg=FG, bg=BG, selectcolor=SEL,
                           activeforeground=_C["fg_bright"], activebackground=BG,
                           font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=1)

        # Refresh interval
        section("Refresh interval")
        rf = tk.Frame(body, bg=BG)
        rf.pack(fill="x", padx=16, pady=(0, 4))
        s_refresh = tk.IntVar(value=cfg.get("refreshSeconds", 30))
        for secs, lbl in [(15, "15 s"), (30, "30 s"), (60, "1 min"),
                          (120, "2 min"), (300, "5 min")]:
            tk.Radiobutton(rf, text=lbl, variable=s_refresh, value=secs,
                           fg=FG, bg=BG, selectcolor=SEL,
                           activeforeground=_C["fg_bright"], activebackground=BG,
                           font=("Segoe UI", 9)).pack(side="left", padx=(0, 8))

        # Alerts
        section("Alerts")
        s_notif = tk.BooleanVar(value=cfg.get("notificationsEnabled", True))
        s_sound = tk.BooleanVar(value=cfg.get("soundEnabled", True))
        check(body, "Desktop notification", s_notif)
        check(body, "Sound alert",          s_sound)

        # Appearance
        section("Appearance")
        af = tk.Frame(body, bg=BG)
        af.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(af, text="Transparency", font=("Segoe UI", 9),
                 fg=FG, bg=BG).grid(row=0, column=0, sticky="w")
        s_alpha = tk.DoubleVar(value=cfg.get("alpha", 0.93))
        tk.Scale(af, from_=0.3, to=1.0, resolution=0.05, orient="horizontal",
                 variable=s_alpha, length=140, bg=BG, fg=FG,
                 troughcolor=SEL, highlightthickness=0, sliderrelief="flat"
                 ).grid(row=0, column=1, padx=(8, 0))
        tk.Label(af, text="Width", font=("Segoe UI", 9),
                 fg=FG, bg=BG).grid(row=1, column=0, sticky="w", pady=(6, 0))
        s_width = tk.IntVar(value=cfg.get("overlayWidth", 260))
        tk.Scale(af, from_=200, to=420, resolution=10, orient="horizontal",
                 variable=s_width, length=140, bg=BG, fg=FG,
                 troughcolor=SEL, highlightthickness=0, sliderrelief="flat"
                 ).grid(row=1, column=1, padx=(8, 0), pady=(6, 0))
        s_always = tk.BooleanVar(value=cfg.get("alwaysVisible", False))
        check(body, "Always show (dashboard mode)", s_always)

        # Queue filtering
        section("Queue filtering")
        kf = tk.Frame(body, bg=BG)
        kf.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(kf, text="Hide queues whose name contains:",
                 font=("Segoe UI", 8), fg=FG, bg=BG).pack(anchor="w")
        s_skip_kw = tk.StringVar(value=", ".join(cfg.get("skipKeywords", [])))
        tk.Entry(kf, textvariable=s_skip_kw, bg=SEL, fg=_C["fg_bright"],
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2, 6))
        tk.Label(kf, text="Hide queues with more than N tickets (0 = no limit):",
                 font=("Segoe UI", 8), fg=FG, bg=BG).pack(anchor="w")
        s_max_q = tk.IntVar(value=cfg.get("maxQueueSize", 0))
        tk.Spinbox(kf, from_=0, to=999_999, increment=1_000,
                   textvariable=s_max_q, width=10, bg=SEL, fg=_C["fg_bright"],
                   insertbackground="white", relief="flat", font=("Segoe UI", 9),
                   buttonbackground=SEL).pack(anchor="w", ipady=3, pady=(2, 0))

        # Queues to show
        hidden_set = set(cfg.get("hiddenQueues", []))
        s_queue_vars: dict[str, tk.BooleanVar] = {}
        if queue_names:
            section("Queues to show")
            for name in queue_names:
                var = tk.BooleanVar(value=name not in hidden_set)
                s_queue_vars[name] = var
                check(body, name, var)

        # Tray badge
        section("Tray badge shows")
        s_tray_badge = tk.StringVar(value=cfg.get("trayBadgeQueue", ""))
        for name in queue_names:
            tk.Radiobutton(body, text=name, variable=s_tray_badge, value=name,
                           fg=FG, bg=BG, selectcolor=SEL,
                           activeforeground=_C["fg_bright"], activebackground=BG,
                           font=("Segoe UI", 9)).pack(anchor="w", padx=16)

        # Alert queues
        section("Alert queues (trigger overlay + notifications)")
        current_alerts = set(cfg.get("alertQueues",
                             [cfg["alertQueue"]] if cfg.get("alertQueue") else []))
        s_alert_vars: dict[str, tk.BooleanVar] = {}
        for name in queue_names:
            var = tk.BooleanVar(value=name in current_alerts)
            s_alert_vars[name] = var
            check(body, name, var)

        # SLA & completed today
        section("SLA & completed today")
        sf = tk.Frame(body, bg=BG)
        sf.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(sf, text="SLA field name:",
                 font=("Segoe UI", 9), fg=FG, bg=BG).pack(anchor="w")
        s_sla = tk.StringVar(value=cfg.get("slaField", "Time to resolution"))
        tk.Entry(sf, textvariable=s_sla, bg=SEL, fg=_C["fg_bright"],
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2, 6))
        tk.Label(sf, text='Extra JQL filter (e.g. "assignee is not EMPTY"):',
                 font=("Segoe UI", 8), fg=FG, bg=BG).pack(anchor="w")
        s_completed_filter = tk.StringVar(value=cfg.get("completedTodayFilter", ""))
        tk.Entry(sf, textvariable=s_completed_filter, bg=SEL, fg=_C["fg_bright"],
                 insertbackground="white", relief="flat", font=("Segoe UI", 9)
                 ).pack(fill="x", ipady=3, pady=(2, 0))

        # System
        section("System")
        s_startup = tk.BooleanVar(value=startup_enabled())
        check(body, "Run on Windows startup", s_startup)
        tk.Frame(body, bg=BG, height=4).pack()
        tk.Button(body, text="⚙  Reconfigure credentials…",
                  command=self._reconfigure, bg=SEL, fg=FG,
                  font=("Segoe UI", 9), relief="flat", padx=8, pady=4,
                  cursor="hand2").pack(anchor="w", padx=16, pady=(0, 8))

        # Save / Cancel
        def _save() -> None:
            set_startup(s_startup.get())
            alert_list = [n for n, v in s_alert_vars.items() if v.get()]
            self.config.update({
                "refreshSeconds":       s_refresh.get(),
                "notificationsEnabled": s_notif.get(),
                "soundEnabled":         s_sound.get(),
                "hiddenQueues":         [n for n, v in s_queue_vars.items() if not v.get()],
                "trayBadgeQueue":       s_tray_badge.get(),
                "alpha":                round(s_alpha.get(), 2),
                "overlayWidth":         s_width.get(),
                "alwaysVisible":        s_always.get(),
                "alertQueues":          alert_list,
                "alertQueue":           alert_list[0] if alert_list else "",
                "slaField":             s_sla.get().strip() or "Time to resolution",
                "completedTodayFilter": s_completed_filter.get().strip(),
                "skipKeywords":         [k.strip() for k in s_skip_kw.get().split(",")
                                         if k.strip()],
                "maxQueueSize":         int(s_max_q.get()),
            })
            save_config(self.config)
            for rw in self.row_widgets.values():
                rw["row"].destroy()
            self.row_widgets.clear()
            if self.loading:
                self._settings_changed = True
            else:
                self._fetch()
            self._close_settings()

        bf = tk.Frame(self.settings_panel, bg=BG)
        bf.pack(fill="x", pady=(4, 0))
        tk.Button(bf, text="✓  Save", command=_save,
                  bg=_C["accent"], fg="white", font=("Segoe UI", 9, "bold"),
                  relief="flat", padx=10, pady=5, cursor="hand2",
                  activebackground=_C["accent_hi"], activeforeground="white"
                  ).pack(side="left", padx=(0, 4))
        tk.Button(bf, text="✕  Cancel", command=self._close_settings,
                  bg=SEL, fg=FG, font=("Segoe UI", 9),
                  relief="flat", padx=10, pady=5, cursor="hand2"
                  ).pack(side="left")

    def _reconfigure(self) -> None:
        if messagebox.askyesno("Reconfigure",
                               "Clear saved credentials and restart setup?",
                               parent=self.root):
            if self._tray:
                self._tray.stop()
            if os.path.exists(CONFIG_FILE):
                os.remove(CONFIG_FILE)
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
        if self._tray:
            self._tray.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config()
    if not config:
        setup  = SetupWindow()
        config = setup.result
        if not config:
            return
    JiraOverlay(config).run()


if __name__ == "__main__":
    main()
