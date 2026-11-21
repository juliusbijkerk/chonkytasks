"""Drive the real Excel desktop app, visibly (COM automation via pywin32).

Used instead of openpyxl when EXCEL_DESKTOP=1: the user watches Excel open the
tracker, jump to the matching row, change Status and save. Works even when the
workbook is already open in Excel (openpyxl can't write a file Excel has locked).
"""
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from rapidfuzz import fuzz

from .. import config

Log = Callable[[str], None]


def _excel():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        xl = win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        xl = win32com.client.Dispatch("Excel.Application")
    xl.Visible = True
    return xl


def _bring_to_front(xl) -> None:
    try:
        import win32con
        import win32gui
        hwnd = xl.Hwnd
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _workbook(xl, path: Path):
    target = str(path.resolve()).lower()
    for wb in xl.Workbooks:
        if str(wb.FullName).lower() == target:
            return wb
    return xl.Workbooks.Open(str(path.resolve()))


def _columns(ws) -> tuple[dict, list]:
    values = ws.UsedRange.Value or ()
    if not values:
        return {}, []
    header = [str(h or "").strip().lower() for h in values[0]]

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n) + 1
        return None

    cols = {"company": col("company", "entreprise"), "role": col("role", "position", "poste"),
            "status": col("status", "statut"), "applied": col("applied date", "date applied", "date"),
            "updated": col("last updated", "updated"), "notes": col("notes", "note")}
    return cols, list(values[1:])


def update_status(company: str, role: str = "", status: str = "Declined",
                  note: Optional[str] = None, path: Optional[Path] = None,
                  pace: float = 0.6, log: Log = print) -> dict:
    """Visibly set Status for the row best matching company (+role). Saves the workbook."""
    path = Path(path or config.TRACKER_PATH)
    xl = _excel()
    _bring_to_front(xl)
    wb = _workbook(xl, path)
    wb.Activate()
    ws = wb.Worksheets(1)
    ws.Activate()
    cols, rows = _columns(ws)
    if not cols.get("company") or not cols.get("status"):
        return {"matched": False, "error": "tracker needs Company and Status columns"}

    best, best_score = None, 0
    for i, r in enumerate(rows):
        c = str(r[cols["company"] - 1] or "")
        score = fuzz.token_set_ratio(company.lower(), c.lower())
        if role and cols.get("role"):
            score = score * 0.75 + fuzz.token_set_ratio(role.lower(), str(r[cols["role"] - 1] or "").lower()) * 0.25
        if score > best_score:
            best, best_score = i + 2, score       # +2: header row, 1-based
    if best is None or best_score < 75:
        log(f"Excel: no row matches '{company}'")
        return {"matched": False}

    # visible: select the company cell, then the status cell, then write
    ws.Cells(best, cols["company"]).Select()
    time.sleep(pace)
    cell = ws.Cells(best, cols["status"])
    cell.Select()
    time.sleep(pace)
    old = cell.Value
    cell.Value = status
    if cols.get("updated"):
        ws.Cells(best, cols["updated"]).Value = date.today().isoformat()
    if note and cols.get("notes"):
        prev = str(ws.Cells(best, cols["notes"]).Value or "")
        ws.Cells(best, cols["notes"]).Value = (prev + " | " if prev else "") + note
    time.sleep(pace)
    wb.Save()
    log(f"Excel: row {best} ({company}) Status '{old}' -> '{status}', saved")
    return {"matched": True, "row": best, "old": old}


def apply_ghosted(days: Optional[int] = None, path: Optional[Path] = None,
                  pace: float = 0.35, log: Log = print) -> list[dict]:
    """Visibly mark 'Applied' rows older than N days as 'Ghosted'."""
    days = days or config.GHOSTED_DAYS
    path = Path(path or config.TRACKER_PATH)
    xl = _excel()
    _bring_to_front(xl)
    wb = _workbook(xl, path)
    wb.Activate()
    ws = wb.Worksheets(1)
    cols, rows = _columns(ws)
    if not cols.get("status") or not cols.get("applied"):
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for i, r in enumerate(rows):
        if str(r[cols["status"] - 1] or "").strip().lower() != "applied":
            continue
        raw = r[cols["applied"] - 1]
        try:
            applied = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw)[:10])
            applied = applied.replace(tzinfo=None)
        except Exception:
            continue
        if applied < cutoff:
            cell = ws.Cells(i + 2, cols["status"])
            cell.Select()
            time.sleep(pace)
            cell.Value = "Ghosted"
            if cols.get("updated"):
                ws.Cells(i + 2, cols["updated"]).Value = date.today().isoformat()
            out.append({"row": i + 2, "company": str(r[cols["company"] - 1] or "") if cols.get("company") else ""})
    if out:
        wb.Save()
        log(f"Excel: {len(out)} stale row(s) -> Ghosted, saved")
    return out
