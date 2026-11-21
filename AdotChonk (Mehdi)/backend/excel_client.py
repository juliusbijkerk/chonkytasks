"""Excel tracker client: read/update applications.xlsx, fuzzy-match rows,
apply the 'Ghosted' rule, and always back up before writing."""
import datetime as dt
import shutil
from pathlib import Path
from typing import Optional

from openpyxl import load_workbook
from rapidfuzz import fuzz

from . import config

COLUMNS = ["Company", "Role", "Applied Date", "Status", "Last Updated", "Link", "Notes"]


# ---- sample data -------------------------------------------------------------

def generate_sample_tracker(path: Path) -> Path:
    """Create a realistic applications.xlsx with ~15 rows (mix of statuses/dates)."""
    from openpyxl import Workbook

    today = dt.date.today()
    rows = [
        # company, role, applied date, status, link, notes
        ("Acme Corp", "Software Engineer Intern", today - dt.timedelta(days=12), "Applied", "https://acme.example.com/jobs/1", ""),
        ("Globex", "Data Analyst", today - dt.timedelta(days=34), "Applied", "https://globex.example.com/jobs/2", ""),
        ("Initech", "Product Manager", today - dt.timedelta(days=8), "Interview", "https://initech.example.com/jobs/3", "Phone screen done"),
        ("Umbrella", "Backend Developer", today - dt.timedelta(days=26), "Applied", "https://umbrella.example.com/jobs/4", ""),
        ("Hooli", "ML Intern", today - dt.timedelta(days=40), "Applied", "https://hooli.example.com/jobs/5", ""),
        ("Stark Industries", "ML Intern", today - dt.timedelta(days=15), "Applied", "https://stark.example.com/jobs/6", ""),
        ("Wayne Enterprises", "Security Analyst Intern", today - dt.timedelta(days=5), "Applied", "https://wayne.example.com/jobs/7", ""),
        ("Wonka Ltd", "Data Analyst", today - dt.timedelta(days=3), "Interview", "https://wonka.example.com/jobs/8", "Technical round scheduled"),
        ("Soylent", "Frontend Developer", today - dt.timedelta(days=45), "Declined", "https://soylent.example.com/jobs/9", "Feedback requested on 2025-11-01"),
        ("Cyberdyne", "Software Engineer", today - dt.timedelta(days=22), "Applied", "https://cyberdyne.example.com/jobs/10", ""),
        ("Aperture", "QA Engineer", today - dt.timedelta(days=9), "Applied", "https://aperture.example.com/jobs/11", ""),
        ("Massive Dynamic", "Product Manager Intern", today - dt.timedelta(days=2), "Applied", "https://massivedynamic.example.com/jobs/12", ""),
        ("Pied Piper", "Backend Developer", today - dt.timedelta(days=18), "Applied", "https://piedpiper.example.com/jobs/13", ""),
        ("Dunder Mifflin", "Sales Analyst", today - dt.timedelta(days=1), "Offer", "https://dundermifflin.example.com/jobs/14", "Offer received"),
        ("Vandelay Industries", "Data Analyst", today - dt.timedelta(days=29), "Applied", "https://vandelay.example.com/jobs/15", ""),
    ]
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    ws.append(COLUMNS)
    for company, role, applied, status, link, notes in rows:
        ws.append([company, role, applied.isoformat(), status, applied.isoformat(), link, notes])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def ensure_tracker(path: Optional[Path] = None) -> Path:
    path = Path(path or config.TRACKER_PATH)
    if not path.exists():
        generate_sample_tracker(path)
    return path


# ---- IO helpers --------------------------------------------------------------

def backup_tracker(path: Optional[Path] = None) -> Path:
    """Timestamped backup copy. Called before any write."""
    path = ensure_tracker(path)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, bak)
    return bak


def read_rows(path: Optional[Path] = None) -> list[dict]:
    path = ensure_tracker(path)
    wb = load_workbook(path, data_only=True)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if all(v is None for v in r):
            continue
        rows.append(dict(zip(headers, r)))
    return rows


def _save(path: Path, wb) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def _parse_date(v) -> Optional[dt.date]:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


# ---- matching & updates ------------------------------------------------------

def find_match(path: Optional[Path] = None, company: str = "", role: str = "",
               threshold: int = 72) -> Optional[int]:
    """Fuzzy-match (company, role) against tracker rows. Returns 1-based sheet row index."""
    path = ensure_tracker(path)
    wb = load_workbook(path)
    ws = wb.active
    target = f"{company} {role}".strip().lower()
    best_idx, best_score = None, 0
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not row or row[0] is None:
            continue
        cand_company = str(row[0]).strip().lower()
        cand = f"{cand_company} {row[1] or ''}".strip().lower()
        # Company name must match reasonably on its own, otherwise "data analyst" alone
        # would false-positive onto any company with the same role.
        company_score = fuzz.token_set_ratio(company.strip().lower(), cand_company)
        full_score = fuzz.token_set_ratio(target, cand)
        score = 0.7 * company_score + 0.3 * full_score
        if score > best_score:
            best_idx, best_score = i, score
    if best_score >= threshold:
        return best_idx
    return None


def update_row(path: Optional[Path] = None, row_idx: Optional[int] = None, *,
               status: Optional[str] = None, note_append: Optional[str] = None) -> dict:
    """Update Status / Last Updated / Notes of a row. Returns the new row as dict."""
    path = ensure_tracker(path)
    wb = load_workbook(path)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    today = dt.date.today().isoformat()
    if status is not None and "Status" in headers:
        ws.cell(row=row_idx, column=headers.index("Status") + 1, value=status)
    if "Last Updated" in headers:
        ws.cell(row=row_idx, column=headers.index("Last Updated") + 1, value=today)
    if note_append and "Notes" in headers:
        col = headers.index("Notes") + 1
        cur = ws.cell(row=row_idx, column=col).value or ""
        ws.cell(row=row_idx, column=col, value=(cur + " | " + note_append).strip(" |"))
    _save(path, wb)
    return read_rows(path)[row_idx - 2]


def apply_ghosted_rule(path: Optional[Path] = None, days: Optional[int] = None,
                       today: Optional[dt.date] = None) -> list[dict]:
    """Rows with Status 'Applied' and Applied Date older than `days` become 'Ghosted'."""
    path = ensure_tracker(path)
    days = config.GHOSTED_DAYS if days is None else days
    today = today or dt.date.today()
    changed = []
    rows = read_rows(path)
    wb = load_workbook(path)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    for i, row in enumerate(rows, start=2):
        if str(row.get("Status", "")).strip().lower() != "applied":
            continue
        applied = _parse_date(row.get("Applied Date"))
        if applied and (today - applied).days > days:
            note = f"Auto-marked Ghosted (no reply for {(today - applied).days} days)"
            update_row(path, i, status="Ghosted", note_append=note)
            changed.append({"row": i, "company": row.get("Company"), "role": row.get("Role"),
                            "applied_date": applied.isoformat(), "days": (today - applied).days})
    return changed
