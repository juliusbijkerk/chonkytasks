"""General laptop agent: Mistral operates Windows apps with mouse and keyboard.

Loop:  observe -> decide -> act -> (repeat) until done / failed / stopped.

observe : the foreground window's UI Automation tree (buttons, fields, menus,
          list items... each with an id, name and on-screen position), the list
          of open windows, and a downscaled screenshot.
decide  : Mistral (vision model, JSON mode) returns ONE action.
act     : pyautogui / uiautomation perform it on the real desktop.

Safety
  * Esc (not sent by the agent itself), the dock's Stop button, the dashboard's
    Stop, or moving the mouse into a screen corner (pyautogui fail-safe) stop it.
  * never types into password / card fields; refuses credential-looking text.
  * irreversible actions (send, delete, pay, purchase, submit, install, settings,
    shut down...) require the user's approval from the dashboard (ask_user).
  * at most MAX_STEPS actions per run.
"""
import base64
import ctypes
import io
import json
import re
import threading
import time
from typing import Callable, Optional

from .. import config, db
from ..mistral_util import chat_complete, parse_json_response

MAX_STEPS = 30
MAX_ELEMENTS = 110
SHOT_WIDTH = 1280
MODEL = None   # default: config.MISTRAL_LARGE_MODEL (vision-capable)

INTERACTIVE = {"ButtonControl", "EditControl", "MenuItemControl", "ListItemControl", "TabItemControl",
               "HyperlinkControl", "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
               "TreeItemControl", "DataItemControl", "DocumentControl", "SplitButtonControl",
               "MenuBarControl", "ToolBarControl", "TextControl", "HeaderItemControl", "SpinnerControl"}
SENSITIVE_FIELD = re.compile(r"password|mot de passe|passcode|\bpin\b|card number|cvv|cvc|security code|iban", re.I)
IRREVERSIBLE = re.compile(r"\b(send|envoyer|delete|supprimer|remove|erase|format|pay|payer|purchase|buy|acheter|"
                          r"checkout|order|submit|install|uninstall|shut ?down|restart|sign out|log ?out|"
                          r"empty (the )?(recycle bin|trash)|transfer|virement)\b", re.I)

SYSTEM = f"""You are AutoPilot's laptop agent. You operate a Windows 11 computer for the user with the
mouse and keyboard to complete their GOAL. Each turn you get: the goal, your recent actions and their
results, the open windows, the focused window's UI elements (id, type, name, center x,y in screen
pixels) and a screenshot (scaled; `scale` converts screenshot px to screen px).

Reply with STRICT JSON: {{"thought": "<one short sentence>", "action": {{...}}}}
ONE action per turn, one of:
  {{"type":"click","id":<element id>}}            {{"type":"double_click","id":<id>}}
  {{"type":"right_click","id":<id>}}             {{"type":"click_xy","x":<screenshot px>,"y":<screenshot px>}}
  {{"type":"type","text":"...","id":<optional id to click first>,"enter":<bool>}}
  {{"type":"key","keys":"ctrl+s"}}   (e.g. "enter", "tab", "ctrl+f", "alt+tab", "win", "f2", "ctrl+home")
  {{"type":"scroll","direction":"up|down","amount":<1-10>,"id":<optional id>}}
  {{"type":"open_app","name":"excel"}}          (Windows search + Enter)
  {{"type":"focus_window","title":"<part of a window title>"}}
  {{"type":"wait","seconds":<1-5>}}
  {{"type":"ask_user","question":"..."}}        (REQUIRED before anything irreversible)
  {{"type":"done","summary":"..."}}             {{"type":"fail","reason":"..."}}
Rules:
- Prefer element ids over click_xy. Verify the result on the next turn before moving on.
- NEVER type passwords, card numbers or other credentials; if a login is needed, ask_user.
- NEVER send, delete, pay, purchase, submit forms, install software or change system settings
  without first using ask_user and getting approval.
- Stay on the goal; do not browse unrelated content. Finish with done as soon as the goal is met.
- In Excel: Ctrl+Home goes to A1; the Name Box (left of the formula bar) jumps to a cell like D5."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _esc_pressed() -> bool:
    return bool(ctypes.windll.user32.GetAsyncKeyState(0x1B) & 0x8000)


class Stopped(Exception):
    pass


class ComputerAgent:
    def __init__(self, run_id: str, goal: str, step_cb: Callable[[str], None] = lambda s: None):
        import pyautogui
        import uiautomation as auto
        self.pg, self.auto = pyautogui, auto
        pyautogui.FAILSAFE = True          # mouse into a corner -> exception -> stop
        pyautogui.PAUSE = 0.08
        self.run_id, self.goal, self.step_cb = run_id, goal, step_cb
        self.stop_flag = threading.Event()
        self.sending_keys = False
        self.history: list[str] = []
        self.elements: dict[int, dict] = {}
        self.scale = 1.0
        self.last_event_id = 0
        threading.Thread(target=self._watch_esc, daemon=True).start()

    # ---- events / control ----
    def emit(self, kind: str, message: str, data: Optional[dict] = None):
        db.add_run_event(self.run_id, kind, message, data or {})

    def stop(self):
        self.stop_flag.set()

    def _watch_esc(self):
        while not self.stop_flag.is_set():
            if _esc_pressed() and not self.sending_keys:
                self.emit("warning", "Esc pressed - stopping")
                self.stop_flag.set()
            time.sleep(0.05)

    def _check(self):
        if self.stop_flag.is_set():
            raise Stopped()
        for e in db.get_run_events(self.run_id, after_id=self.last_event_id):
            self.last_event_id = e["id"]
            if e["kind"] == "stop_request":
                self.stop_flag.set()
                raise Stopped()

    # ---- observe ----
    def _windows(self) -> list[str]:
        out = []
        for w in self.auto.GetRootControl().GetChildren():
            try:
                if w.Name and not w.IsOffscreen and w.ControlTypeName == "WindowControl":
                    out.append(w.Name[:80])
            except Exception:
                pass
        return out[:15]

    def _elements(self) -> tuple[str, list[dict]]:
        auto = self.auto
        fg = auto.ControlFromHandle(ctypes.windll.user32.GetForegroundWindow())
        title = (fg.Name if fg else "") or ""
        els, t0 = [], time.time()
        self.elements = {}
        if not fg:
            return title, els
        for ctrl, depth in auto.WalkControl(fg, includeTop=False, maxDepth=14):
            if time.time() - t0 > 2.5 or len(els) >= MAX_ELEMENTS:
                break
            try:
                tname = ctrl.ControlTypeName
                if tname not in INTERACTIVE or ctrl.IsOffscreen:
                    continue
                name = (ctrl.Name or "").strip()
                if not name and tname not in ("EditControl", "DocumentControl", "ComboBoxControl"):
                    continue
                r = ctrl.BoundingRectangle
                if r.width() <= 2 or r.height() <= 2:
                    continue
                eid = len(els) + 1
                info = {"id": eid, "type": tname.replace("Control", ""), "name": name[:60],
                        "x": (r.left + r.right) // 2, "y": (r.top + r.bottom) // 2}
                try:
                    vp = ctrl.GetValuePattern() if tname in ("EditControl", "ComboBoxControl") else None
                    if vp and not SENSITIVE_FIELD.search(name):
                        info["value"] = (vp.Value or "")[:40]
                except Exception:
                    pass
                els.append(info)
                self.elements[eid] = {**info, "ctrl": ctrl}
            except Exception:
                continue
        return title, els

    def _screenshot(self) -> str:
        from PIL import ImageGrab
        img = ImageGrab.grab(all_screens=False)
        self.scale = img.width / SHOT_WIDTH if img.width > SHOT_WIDTH else 1.0
        if self.scale != 1.0:
            img = img.resize((SHOT_WIDTH, int(img.height / self.scale)))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

    # ---- decide ----
    def _decide(self, step: int) -> dict:
        title, els = self._elements()
        state = {"goal": self.goal, "step": f"{step}/{MAX_STEPS}", "recent_actions": self.history[-8:],
                 "open_windows": self._windows(), "focused_window": title, "elements": els,
                 "scale": round(self.scale, 3)}
        shot = self._screenshot()
        state["scale"] = round(self.scale, 3)
        content = [{"type": "text", "text": json.dumps(state, ensure_ascii=False)},
                   {"type": "image_url", "image_url": shot}]
        for attempt in range(3):
            try:
                # the large model "thinks" before answering: leave room for it, keep it short
                resp = chat_complete(messages=[{"role": "user", "content": content}], system=SYSTEM,
                                     model=MODEL or config.MISTRAL_LARGE_MODEL,
                                     response_format={"type": "json_object"}, temperature=0.1,
                                     max_tokens=2000, timeout=90)
                choice = resp["choices"][0]
                data = parse_json_response(choice["message"]["content"])
                if data and isinstance(data.get("action"), dict):
                    return data
                self.emit("warning", f"unusable model answer (finish={choice.get('finish_reason')}): "
                                     f"{str(choice['message'].get('content'))[-160:]}")
            except Exception as e:
                self.emit("warning", f"Mistral call failed ({e}), retrying")
        return {"thought": "no valid answer", "action": {"type": "fail", "reason": "model gave no valid action"}}

    # ---- safety ----
    def _needs_approval(self, action: dict, thought: str) -> Optional[str]:
        t = action.get("type")
        target = ""
        if action.get("id") in self.elements:
            target = self.elements[action["id"]]["name"]
        if t in ("click", "double_click") and IRREVERSIBLE.search(target):
            return f"Click '{target}'?"
        if t == "key" and action.get("keys", "").lower() in ("ctrl+enter", "alt+s", "delete", "shift+delete"):
            return f"Press {action['keys']} (may send/delete)?"
        if t == "type" and action.get("enter") and IRREVERSIBLE.search(thought):
            return f"Type '{action.get('text', '')[:40]}' and press Enter?"
        return None

    def _ask_user(self, question: str) -> bool:
        self.emit("question", question, {"awaiting": True})
        self.step_cb("Waiting for your approval on the dashboard…\n" + question)
        t0 = time.time()
        while time.time() - t0 < 300:
            if self.stop_flag.is_set():
                raise Stopped()
            for e in db.get_run_events(self.run_id, after_id=self.last_event_id):
                self.last_event_id = e["id"]
                if e["kind"] == "stop_request":
                    raise Stopped()
                if e["kind"] == "user_answer":
                    return bool(e["data"].get("approve"))
            time.sleep(0.5)
        self.emit("warning", "No answer within 5 minutes - treating as 'no'")
        return False

    # ---- act ----
    def _point(self, action: dict) -> tuple[int, int]:
        if "id" in action and action["id"] in self.elements:
            el = self.elements[action["id"]]
            try:
                r = el["ctrl"].BoundingRectangle          # live position
                return (r.left + r.right) // 2, (r.top + r.bottom) // 2
            except Exception:
                return el["x"], el["y"]
        if "x" in action and "y" in action:
            return int(action["x"] * self.scale), int(action["y"] * self.scale)
        raise ValueError("action has no valid target")

    def _type(self, text: str):
        self.sending_keys = True
        try:
            if text.isascii():
                self.pg.write(text, interval=0.025)
            else:                                           # unicode: paste
                self.auto.SetClipboardText(text)
                self.pg.hotkey("ctrl", "v")
        finally:
            self.sending_keys = False

    def _act(self, action: dict) -> str:
        pg, t = self.pg, action.get("type")
        if t in ("click", "double_click", "right_click", "click_xy"):
            x, y = self._point(action)
            pg.moveTo(x, y, duration=0.25)
            {"double_click": pg.doubleClick, "right_click": pg.rightClick}.get(t, pg.click)(x, y)
            name = self.elements.get(action.get("id"), {}).get("name", "")
            return f"{t} at ({x},{y}) {name!r}"
        if t == "type":
            text = str(action.get("text", ""))
            if "id" in action and action["id"] in self.elements:
                if SENSITIVE_FIELD.search(self.elements[action["id"]]["name"]):
                    return "REFUSED: that looks like a password/credential field"
                x, y = self._point(action)
                pg.click(x, y)
            self._type(text)
            if action.get("enter"):
                pg.press("enter")
            return f"typed {len(text)} chars" + (" + Enter" if action.get("enter") else "")
        if t == "key":
            keys = [k.strip().lower() for k in str(action.get("keys", "")).split("+") if k.strip()]
            keys = ["win" if k in ("windows", "super", "meta") else k for k in keys]
            self.sending_keys = True
            try:
                pg.hotkey(*keys)
            finally:
                time.sleep(0.15)
                self.sending_keys = False
            return f"pressed {'+'.join(keys)}"
        if t == "scroll":
            if "id" in action and action["id"] in self.elements:
                pg.moveTo(*self._point(action), duration=0.2)
            n = max(1, min(10, int(action.get("amount", 3))))
            pg.scroll((1 if action.get("direction") == "up" else -1) * n * 120)
            return f"scrolled {action.get('direction')} {n}"
        if t == "open_app":
            name = str(action.get("name", ""))
            pg.press("win")
            time.sleep(0.9)
            self._type(name)
            time.sleep(1.2)
            pg.press("enter")
            time.sleep(2.5)
            return f"opened '{name}' via Windows search"
        if t == "focus_window":
            title = str(action.get("title", "")).lower()
            for w in self.auto.GetRootControl().GetChildren():
                if title and title in (w.Name or "").lower():
                    w.SetActive()
                    w.SetFocus()
                    time.sleep(0.5)
                    return f"focused '{w.Name[:60]}'"
            return f"no window matching '{title}'"
        if t == "wait":
            time.sleep(max(1, min(5, float(action.get("seconds", 2)))))
            return "waited"
        return f"unknown action {t}"

    # ---- main loop ----
    def run(self) -> dict:
        # UI Automation is COM: it must be initialised in the thread that uses it
        with self.auto.UIAutomationInitializerInThread():
            return self._run()

    def _run(self) -> dict:
        _dpi_aware()
        t0 = time.time()
        status, summary, n = "failed", "", 0
        self.emit("info", f"[laptop agent] goal: {self.goal}", {"engine": "laptop_agent"})
        try:
            for step in range(1, MAX_STEPS + 1):
                self._check()
                n = step
                self.step_cb(f"Step {step}: looking at the screen…")
                decision = self._decide(step)
                self._check()
                action, thought = decision["action"], str(decision.get("thought", ""))[:200]
                t = action.get("type")
                self.step_cb(f"Step {step}: {thought}")
                if t == "done":
                    status, summary = "done", action.get("summary", "")
                    self.emit("done", f"[laptop agent] done: {summary}")
                    break
                if t == "fail":
                    summary = action.get("reason", "")
                    self.emit("error", f"[laptop agent] gave up: {summary}")
                    break
                if t == "ask_user":
                    ok = self._ask_user(action.get("question", "Continue?"))
                    self.history.append(f"asked user: {action.get('question')} -> {'approved' if ok else 'denied'}")
                    self.emit("info", f"You {'approved' if ok else 'denied'}: {action.get('question')}")
                    continue
                q = self._needs_approval(action, thought)
                if q and not self._ask_user(q):
                    self.history.append(f"{json.dumps(action)} -> DENIED by user")
                    self.emit("info", f"Denied: {q}")
                    continue
                try:
                    result = self._act(action)
                except self.pg.FailSafeException:
                    self.emit("warning", "Mouse moved to a screen corner - emergency stop")
                    raise Stopped()
                except Exception as e:
                    result = f"ERROR {e}"
                self.history.append(f"{json.dumps(action, ensure_ascii=False)[:160]} -> {result}")
                self.emit("tool_call", f"{thought}  →  {result}", {"action": action})
                time.sleep(0.6)
            else:
                summary = f"stopped after {MAX_STEPS} steps"
                self.emit("warning", f"[laptop agent] {summary}")
        except Stopped:
            status, summary = "stopped", "stopped by you"
            self.emit("done", "[laptop agent] stopped by you")
        except Exception as e:
            summary = str(e)
            self.emit("error", f"[laptop agent] crashed: {e}")
        finally:
            self.stop_flag.set()
        return {"mode": "desktop", "engine": "laptop_agent", "goal": self.goal, "status": status,
                "summary": summary, "steps": n, "agent_seconds": round(time.time() - t0, 1)}
