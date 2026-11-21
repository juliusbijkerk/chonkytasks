"""Desktop-Excel observer.

The Chrome extension only sees the browser, so a spreadsheet opened in the Excel
desktop app is invisible to it. While a recording session is open, this thread
polls the folders in config.WATCH_DIRS (non-recursively) and adds events to the
session:

  * Excel creates a hidden lock file "~$<name>.xlsx" when it opens a workbook
    -> navigation event  (app "Excel", title "<name>.xlsx")
  * the workbook's modified time changes when it is saved
    -> input event       (app "Excel", label "save workbook")
  * the lock file disappears when it is closed
    -> tab_switch event  (app "Excel", "closed <name>.xlsx")

Privacy: only the file NAME is recorded, never its contents. Data stays local.
"""
import threading
import time
from pathlib import Path

from . import config, db

EXTS = (".xlsx", ".xlsm", ".xls", ".csv")
POLL_S = 1.0


def _snapshot() -> tuple[set[str], dict[str, float]]:
    """(names of workbooks currently open in Excel, {path: mtime} of workbooks)."""
    open_books: set[str] = set()
    mtimes: dict[str, float] = {}
    for d in config.WATCH_DIRS:
        try:
            for entry in Path(d).iterdir():
                name = entry.name
                low = name.lower()
                if not low.endswith(EXTS):
                    continue
                if name.startswith("~$"):
                    open_books.add(name[2:])          # Excel lock file -> workbook is open
                elif entry.is_file():
                    mtimes[str(entry)] = entry.stat().st_mtime
        except OSError:
            continue
    return open_books, mtimes


def _event(kind: str, label: str) -> dict:
    now_ms = int(time.time() * 1000)
    if kind == "open":
        return {"timestamp_ms": now_ms, "type": "navigation", "app": "Excel",
                "detail": {"title": label, "url": "excel-desktop"}}
    if kind == "save":
        return {"timestamp_ms": now_ms, "type": "input", "app": "Excel",
                "detail": {"label": f"save {label}", "type": "file"}}
    return {"timestamp_ms": now_ms, "type": "tab_switch", "app": "Excel",
            "detail": {"to": f"closed {label}"}}


def _loop() -> None:
    session_id = None
    prev_open: set[str] = set()
    prev_mtimes: dict[str, float] = {}
    while True:
        try:
            s = db.latest_session(open_only=True)
            sid = s["id"] if s else None
            if sid != session_id:
                # new session (or none): take a fresh baseline so files that were
                # already open/saved before recording are not reported as events,
                # except workbooks already open, which count as "opened" once.
                session_id = sid
                prev_open, prev_mtimes = _snapshot()
                if sid and prev_open:
                    db.add_events(sid, [_event("open", n) for n in sorted(prev_open)])
            elif sid:
                cur_open, cur_mtimes = _snapshot()
                evs = [_event("open", n) for n in sorted(cur_open - prev_open)]
                for path, mt in cur_mtimes.items():
                    if path in prev_mtimes and mt > prev_mtimes[path] + 0.5:
                        evs.append(_event("save", Path(path).name))
                evs += [_event("close", n) for n in sorted(prev_open - cur_open)]
                if evs:
                    db.add_events(sid, evs)
                prev_open, prev_mtimes = cur_open, cur_mtimes
        except Exception as e:  # never let the watcher die
            print(f"[file_watcher] {e}")
        time.sleep(POLL_S)


_started = False


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_loop, name="excel-file-watcher", daemon=True).start()
