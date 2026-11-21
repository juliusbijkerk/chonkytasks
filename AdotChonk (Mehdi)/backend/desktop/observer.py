"""Whole-computer observer (Windows). Runs inside the server as a thread and, while
a recording session is open, adds desktop events to it - complementing the Chrome
extension (which keeps recording browser detail).

Recorded (privacy by design - never typed text, never which keys, never message content):
  app_switch  the app you are working in (only after ~1.2 s in it: Alt-Tab flicks are noise)
              + document name from the window title (emails/numbers masked); NO title at all
              for browsers, mail and chat apps (their titles show subjects / people)
  click       the name of the button / menu / tab clicked; text fields -> "text field",
              list rows / cells -> "list item" (their names are content)
  input       "typing" - once per burst per window, never the keys
  copy_paste  Ctrl+C / Ctrl+V / Ctrl+X          input "save" for Ctrl+S, "undo" for Ctrl+Z
  idle        > 10 s without keyboard/mouse

Ignored as noise: AutoPilot's own windows (dashboard tab, overlays), Windows shell
(taskbar, desktop, Alt-Tab, Start/Search, notifications), and everything while an
AutoPilot agent is acting (its actions are not the user's).
Clicks inside Chrome are left to the extension (no double counting).
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .. import db

user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

POLL_S = 0.04
DWELL_S = 1.2            # stay this long in a window before it counts as a switch
TYPING_GAP_S = 8.0
IDLE_S = 10.0
LIVE_EVERY_S = 120       # live sessions: re-detect tasks this often...
LIVE_MIN_NEW = 8         # ...if at least this many new actions were recorded

APP_NAMES = {
    "excel.exe": "Excel", "winword.exe": "Word", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook",
    "olk.exe": "Outlook", "onenote.exe": "OneNote", "explorer.exe": "File Explorer", "notepad.exe": "Notepad",
    "code.exe": "VS Code", "ms-teams.exe": "Teams", "teams.exe": "Teams", "slack.exe": "Slack",
    "chrome.exe": "Browser", "msedge.exe": "Browser", "firefox.exe": "Browser", "brave.exe": "Browser",
    "opera.exe": "Browser", "spotify.exe": "Spotify", "whatsapp.exe": "WhatsApp", "discord.exe": "Discord",
    "windowsterminal.exe": "Terminal", "cmd.exe": "Terminal", "powershell.exe": "Terminal",
    "acrord32.exe": "PDF reader", "acrobat.exe": "PDF reader", "calc.exe": "Calculator",
    "calculatorapp.exe": "Calculator", "mspaint.exe": "Paint", "zoom.exe": "Zoom", "thunderbird.exe": "Thunderbird",
}
NO_TITLE_APPS = {"Browser", "Outlook", "Thunderbird", "Teams", "Slack", "WhatsApp", "Discord", "Zoom"}
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "MultitaskingViewFrame",
                 "ForegroundStaging", "XamlExplorerHostIslandWindow", "TaskListThumbnailWnd",
                 "Windows.UI.Core.CoreWindow", "NotifyIconOverflowWindow", "TopLevelWindowForOverflowXamlIsland",
                 "#32768", "TkTopLevel", "Tk"}
SHELL_TITLES = re.compile(r"^(search|start|recherche|démarrer|task switching|basculement.*|new notification|"
                          r"notification.*|action center|program manager|tk)$", re.I)
OWN_TITLES = re.compile(r"autopilot", re.I)
LABELLED = {"ButtonControl", "MenuItemControl", "TabItemControl", "HyperlinkControl", "SplitButtonControl",
            "CheckBoxControl", "RadioButtonControl", "TreeItemControl", "MenuBarControl", "HeaderItemControl"}
TEXTY = {"EditControl", "DocumentControl", "ComboBoxControl"}
CONTENTY = {"ListItemControl", "DataItemControl", "TableControl", "DataGridControl", "CustomControl"}

VK_LBUTTON, VK_CONTROL = 0x01, 0x11
TYPING_KEYS = [0x08, 0x0D, 0x20] + list(range(0x30, 0x3A)) + list(range(0x41, 0x5B)) + list(range(0x60, 0x70)) \
              + list(range(0xBA, 0xC1)) + list(range(0xDB, 0xE0))
SHORTCUTS = {0x43: ("copy_paste", {"action": "copy"}), 0x56: ("copy_paste", {"action": "paste"}),
             0x58: ("copy_paste", {"action": "cut"}), 0x53: ("input", {"label": "save", "type": "shortcut"}),
             0x5A: ("input", {"label": "undo", "type": "shortcut"})}


def _mask(s: str, n: int = 50) -> str:
    s = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", s or "")
    s = re.sub(r"\b\d[\d\s().-]{6,}\d\b", "[number]", s)
    return s.strip()[:n]


def _exe_of(hwnd: int) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return "__self__"
    h = kernel32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(520)
        size = wt.DWORD(520)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(h)


def _class_of(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title_of(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _window_info(hwnd: int) -> Optional[dict]:
    """None for noise windows (shell / AutoPilot's own / empty)."""
    if not hwnd:
        return None
    root = user32.GetAncestor(hwnd, 2) or hwnd                  # GA_ROOT
    cls, title, exe = _class_of(root), _title_of(root), _exe_of(root)
    if exe == "__self__" or cls in SHELL_CLASSES or SHELL_TITLES.match(title.strip()) or OWN_TITLES.search(title):
        return None
    if exe in ("python.exe", "pythonw.exe") and "tk" in cls.lower():
        return None                                              # an AutoPilot overlay
    if not title.strip() and exe == "explorer.exe":
        return None
    app = APP_NAMES.get(exe) or (Path(exe).stem.capitalize() if exe else "Other")
    doc = ""
    if app not in NO_TITLE_APPS and title:
        parts = [p.strip() for p in re.split(r"\s[-—|]\s", title) if p.strip()]
        doc = _mask(parts[0] if parts else title)
        if doc.lower() == app.lower():
            doc = ""
    return {"hwnd": root, "app": app, "exe": exe, "doc": doc}


def _idle_seconds() -> float:
    class LII(ctypes.Structure):
        _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]
    lii = LII()
    lii.cbSize = ctypes.sizeof(LII)
    user32.GetLastInputInfo(ctypes.byref(lii))
    return (kernel32.GetTickCount() - lii.dwTime) / 1000.0


def _down(vk: int) -> bool:
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


class DesktopObserver:
    def __init__(self):
        self.session_id: Optional[str] = None
        self.cur: Optional[dict] = None          # window currently counted
        self.cand: Optional[dict] = None         # window waiting for dwell
        self.cand_since = 0.0
        self.last_typing: dict[int, float] = {}
        self.prev_keys: set[int] = set()
        self.prev_lbutton = False
        self.idle_sent = False
        self.buffer: list[dict] = []
        self.last_flush = 0.0
        self.last_check = 0.0
        self.paused = False
        self.analysing = False
        self.last_analysis = time.time()
        self.analysed_count = 0

    # ---- events ----
    def _ev(self, type_: str, app: str, detail: dict):
        self.buffer.append({"timestamp_ms": int(time.time() * 1000), "type": type_, "app": app,
                            "detail": {**detail, "source": "desktop"}})

    def _flush(self, force=False):
        if self.buffer and self.session_id and (force or time.time() - self.last_flush > 2):
            db.add_events(self.session_id, self.buffer)
            self.buffer = []
            self.last_flush = time.time()

    # ---- session / pause state (checked ~1/s) ----
    def _refresh_state(self):
        s = db.latest_session(open_only=True)
        sid = s["id"] if s else None
        if sid != self.session_id:
            self._flush(force=True)
            self.session_id, self.cur, self.cand = sid, None, None
            self.last_typing.clear()
            self.analysed_count, self.last_analysis = 0, time.time()
            if sid:
                self._show_indicator(sid)
        # pause while an AutoPilot agent is acting on the computer
        self.paused = bool(db._query("SELECT 1 FROM runs WHERE status='running' AND mode IN ('desktop','browser') LIMIT 1"))
        # live sessions: re-detect tasks every LIVE_EVERY_S when enough new actions arrived
        if s and "live" in (s.get("label") or "") and not self.analysing \
                and time.time() - self.last_analysis > LIVE_EVERY_S:
            n = db.count_events(sid)
            if n - self.analysed_count >= LIVE_MIN_NEW:
                self.analysing, self.last_analysis = True, time.time()
                threading.Thread(target=self._live_analyse, args=(sid, n), daemon=True).start()

    def _live_analyse(self, sid: str, n: int):
        try:
            from .. import analyzer
            analyzer.analyze_session(sid)
            self.analysed_count = n
        except Exception as e:
            print(f"[desktop observer] live analysis failed: {e}")
        finally:
            self.analysing = False

    def _show_indicator(self, sid: str):
        """Red on-screen 'RECORDING' glow + dock, in its own process (tkinter)."""
        import subprocess
        import sys
        from .. import config
        try:
            subprocess.Popen([sys.executable, "-m", "backend.desktop.rec_indicator", "--session", sid],
                             cwd=str(config.ROOT))
        except Exception as e:
            print(f"[desktop observer] indicator not shown: {e}")

    # ---- one poll ----
    def _poll(self, auto):
        now = time.time()
        fg = user32.GetForegroundWindow()

        # app / document switches with dwell (Alt-Tab flicks never count)
        info = _window_info(fg)
        if info and (not self.cur or info["hwnd"] != self.cur["hwnd"] or info["doc"] != self.cur["doc"]):
            if not self.cand or self.cand["hwnd"] != info["hwnd"] or self.cand["doc"] != info["doc"]:
                self.cand, self.cand_since = info, now
            elif now - self.cand_since >= DWELL_S:
                self.cur = self.cand
                self._ev("app_switch", info["app"], {"title": info["doc"] or info["app"]})
        elif info and self.cur and info["hwnd"] == self.cur["hwnd"]:
            self.cand = None
        app = (self.cur or {}).get("app", "Other")
        in_scope = bool(info) and bool(self.cur) and info["hwnd"] == self.cur["hwnd"]

        # clicks (element under the cursor, labelled controls only)
        lb = _down(VK_LBUTTON)
        if lb and not self.prev_lbutton and in_scope and info["exe"] != "chrome.exe":
            try:
                pt = wt.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                c = auto.ControlFromPoint(pt.x, pt.y)
                t = c.ControlTypeName if c else ""
                name = (c.Name or "").strip() if c else ""
                if t in TEXTY:
                    label, tag = "text field", "field"
                elif t in CONTENTY:
                    label, tag = "list item", "row"
                elif t in LABELLED and name and len(name) <= 40 and "\n" not in name:
                    label, tag = _mask(name, 40), t.replace("Control", "").lower()
                else:
                    label, tag = "", (t.replace("Control", "").lower() or "element")
                self._ev("click", app, {"label": label, "tag": tag})
            except Exception:
                pass
        self.prev_lbutton = lb

        # keyboard: shortcuts as actions, typing as one event per burst (never the keys)
        keys = {vk for vk in TYPING_KEYS if _down(vk)}
        new = keys - self.prev_keys
        if new and in_scope:
            if _down(VK_CONTROL):
                for vk in new:
                    if vk in SHORTCUTS:
                        typ, det = SHORTCUTS[vk]
                        self._ev(typ, app, dict(det))
            elif now - self.last_typing.get(fg, 0) > TYPING_GAP_S:
                self._ev("input", app, {"label": "typing", "type": "keyboard"})
            if not _down(VK_CONTROL):
                self.last_typing[fg] = now
        self.prev_keys = keys

        # idle
        if _idle_seconds() > IDLE_S:
            if not self.idle_sent:
                self.idle_sent = True
                self._ev("idle", app, {"seconds": int(_idle_seconds())})
        else:
            self.idle_sent = False

    def loop(self):
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            while True:
                try:
                    if time.time() - self.last_check > 1.0:
                        self.last_check = time.time()
                        self._refresh_state()
                    if not self.session_id or self.paused:
                        self.prev_keys, self.prev_lbutton = set(), False
                        time.sleep(0.5)
                        continue
                    self._poll(auto)
                    self._flush()
                except Exception as e:
                    print(f"[desktop observer] {e}")
                    time.sleep(1)
                time.sleep(POLL_S)


_started = False


def start() -> None:
    global _started
    if _started or os.name != "nt":
        return
    _started = True
    threading.Thread(target=DesktopObserver().loop, name="desktop-observer", daemon=True).start()
