"""On-screen indicator while the laptop agent is in control.

* a full-screen, click-through, always-on-top frame whose edges glow and
  "breathe" (indigo/violet - same family as the browser agent's overlay)
* a small dock on the right edge: logo, "LAPTOP AGENT", current step, timer,
  a Stop button, and the reminder that Esc stops the agent

Built with tkinter (stdlib). Must run on the main thread; the agent runs in a
worker thread and talks to it through `set_step()` / `close()` (thread-safe).
"""
import ctypes
import queue
import time
import tkinter as tk
from typing import Callable

KEY = "#ff00fe"                 # colour made fully transparent (the centre of the frame)
EDGE = 22                        # glow band width (px)
GLOW = [(165, 180, 252), (129, 140, 248), (99, 102, 241), (79, 70, 229), (67, 56, 202), (55, 48, 163)]
GLOW_REC = [(254, 202, 202), (248, 113, 113), (239, 68, 68), (220, 38, 38), (185, 28, 28), (153, 27, 27)]

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x80000, 0x20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_TOPMOST = 0x80, 0x8000000, 0x8


def _hwnd(win: tk.Misc) -> int:
    return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()


def _set_exstyle(win: tk.Misc, add: int) -> None:
    u = ctypes.windll.user32
    h = _hwnd(win)
    u.SetWindowLongW(h, GWL_EXSTYLE, u.GetWindowLongW(h, GWL_EXSTYLE) | add)


def _lerp(a, b, t):
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


class Overlay:
    def __init__(self, on_stop: Callable[[], None], title: str = "LAPTOP AGENT", glow=None,
                 stop_text: str = "■  Stop agent", hint: str = "Esc or mouse to a screen corner = emergency stop",
                 logo_bg: str = "#6366f1", alpha_range=(0.45, 0.85)):
        self.on_stop = on_stop
        self.alpha_range = alpha_range
        glow = glow or GLOW
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.t0 = time.time()
        self.prev_fg = ctypes.windll.user32.GetForegroundWindow()

        self.root = tk.Tk()
        self.root.withdraw()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()

        # ---- glowing frame (click-through) ----
        f = tk.Toplevel(self.root)
        f.overrideredirect(True)
        f.geometry(f"{sw}x{sh}+0+0")
        f.attributes("-topmost", True)
        f.attributes("-transparentcolor", KEY)
        f.attributes("-alpha", 0.75)
        c = tk.Canvas(f, width=sw, height=sh, bg=KEY, highlightthickness=0)
        c.pack()
        for i in range(EDGE):                      # concentric rectangles: bright edge -> deep indigo
            t = i / (EDGE - 1)
            seg = t * (len(glow) - 1)
            col = _lerp(glow[int(seg)], glow[min(int(seg) + 1, len(glow) - 1)], seg - int(seg))
            c.create_rectangle(i, i, sw - 1 - i, sh - 1 - i, outline=col, width=1)
        self.frame = f
        f.update_idletasks()
        _set_exstyle(f, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)

        # ---- dock (clickable) ----
        d = tk.Toplevel(self.root)
        d.overrideredirect(True)
        d.attributes("-topmost", True)
        d.attributes("-alpha", 0.94)
        d.configure(bg="#111428")
        w, h = 236, 214
        d.geometry(f"{w}x{h}+{sw - w - 30}+{sh // 2 - h // 2}")
        top = tk.Frame(d, bg="#111428")
        top.pack(fill="x", padx=14, pady=(12, 4))
        tk.Label(top, text="✈", font=("Segoe UI Emoji", 16), bg=logo_bg, fg="white",
                 width=2).pack(side="left")
        tk.Label(top, text=f"  AutoPilot\n  {title}", justify="left", font=("Segoe UI", 9, "bold"),
                 bg="#111428", fg="#c7d2fe").pack(side="left")
        self.time_lbl = tk.Label(top, text="0:00", font=("Segoe UI", 10, "bold"), bg="#111428", fg="white")
        self.time_lbl.pack(side="right")
        self.step_lbl = tk.Label(d, text="Starting…", wraplength=w - 28, justify="left", anchor="w",
                                 font=("Segoe UI", 9), bg="#111428", fg="#eef0ff", height=4)
        self.step_lbl.pack(fill="x", padx=14, pady=4)
        tk.Button(d, text=stop_text, command=self._stop, font=("Segoe UI", 10, "bold"),
                  bg="#7f1d1d", fg="#fecaca", activebackground="#b91c1c", activeforeground="white",
                  relief="flat", cursor="hand2").pack(fill="x", padx=14, pady=(4, 2), ipady=4)
        tk.Label(d, text=hint, font=("Segoe UI", 7),
                 bg="#111428", fg="#8b90b8").pack(pady=(2, 8))
        self.dock = d
        d.update_idletasks()
        _set_exstyle(d, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)

        self._restore_focus()
        self._phase = 0.0
        self.root.after(60, self._animate)
        self.root.after(100, self._pump)
        self.root.after(500, self._tick)

    # ---- thread-safe API ----
    def set_step(self, text: str) -> None:
        self.q.put(("step", text))

    def close(self) -> None:
        self.q.put(("close", None))

    def run(self) -> None:
        self.root.mainloop()

    # ---- internals (Tk thread) ----
    def _restore_focus(self):
        try:
            if self.prev_fg:
                ctypes.windll.user32.SetForegroundWindow(self.prev_fg)
        except Exception:
            pass

    def _stop(self):
        self.step_lbl.config(text="Stopping…")
        self.on_stop()

    def _animate(self):
        import math
        self._phase += 0.06
        lo, hi = self.alpha_range
        try:
            self.frame.attributes("-alpha", lo + (hi - lo) * (0.5 + 0.5 * math.sin(self._phase)))
            if int(self._phase / 0.06) % 30 == 0:        # re-assert on top every ~2 s
                self.frame.attributes("-topmost", True)
                self.dock.attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(60, self._animate)

    def _tick(self):
        s = int(time.time() - self.t0)
        try:
            self.time_lbl.config(text=f"{s // 60}:{s % 60:02d}")
        except tk.TclError:
            return
        self.root.after(500, self._tick)

    def _pump(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "step":
                    self.step_lbl.config(text=val[:220])
                elif kind == "close":
                    self.root.after(1200, self.root.destroy)
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._pump)
