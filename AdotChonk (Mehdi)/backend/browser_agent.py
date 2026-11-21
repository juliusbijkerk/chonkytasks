"""Browser agent: the Chrome extension does the work in the user's own Gmail tab.

Division of labour
  * extension (background.js + content.js): opens Gmail, searches, reads the result
    list, opens each email, clicks Reply, types the reply. It NEVER clicks Send:
    replies stay as Gmail drafts for the user to review.
  * backend (this module): Mistral decides which emails are rejections and writes
    the replies; updates the tracker file (with backup); logs every step as run
    events so the dashboard's live feed shows what the agent is doing.

The extension drives the run and calls these endpoints in order:
  start -> classify -> (drafted)* -> finish      plus `event` for progress lines.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config, db, excel_client
from .agent import classify_email, draft_reply

router = APIRouter(prefix="/api/browser-agent")

# English + French rejection phrases (Gmail search syntax)
GMAIL_QUERY = ('newer_than:60d ("unfortunately" OR "regret to inform" OR "not moving forward" '
               'OR "other candidates" OR "not selected" OR "will not be proceeding" '
               'OR "malheureusement" OR "pas été retenue" OR "pas été retenu" OR "ne pas donner suite")')
MAX_EMAILS = 8

_backups: dict[str, str] = {}   # run_id -> backup file name (one backup per run)
_lock = threading.Lock()


class StartBody(BaseModel):
    task_id: Optional[str] = None


class EventBody(BaseModel):
    kind: str = "info"
    message: str
    data: dict = {}


class EmailIn(BaseModel):
    id: str                       # Gmail thread id
    from_name: str = ""
    from_addr: str = ""
    subject: str = ""
    snippet: str = ""


class ClassifyBody(BaseModel):
    emails: list[EmailIn]


class DraftedBody(BaseModel):
    email_id: str
    to: str = ""
    subject: str = ""
    body: str = ""
    company: str = ""
    role: str = ""


class FinishBody(BaseModel):
    aborted: bool = False
    error: Optional[str] = None


def _run_or_404(run_id: str) -> dict:
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@router.post("/start")
def start(body: StartBody):
    if body.task_id and not db.get_task(body.task_id):
        raise HTTPException(404, "task not found")
    run_id = db.create_run(body.task_id, "browser", False)
    if body.task_id:
        db.set_task_status(body.task_id, "running")
    db.add_run_event(run_id, "info", "[browser agent] starting in your Chrome - replies will be left as "
                                     "Gmail drafts, nothing is sent", {"engine": "browser_agent"})
    return {"run_id": run_id, "query": GMAIL_QUERY, "max_emails": MAX_EMAILS}


@router.post("/{run_id}/event")
def event(run_id: str, body: EventBody):
    _run_or_404(run_id)
    db.add_run_event(run_id, body.kind, body.message, body.data)
    return {"ok": True}


@router.post("/{run_id}/classify")
def classify(run_id: str, body: ClassifyBody):
    """Mistral: which emails are rejections + a feedback-request reply for each."""
    _run_or_404(run_id)
    emails = body.emails[:MAX_EMAILS]

    def one(e: EmailIn) -> dict:
        email = {"id": e.id, "from_name": e.from_name, "from_addr": e.from_addr,
                 "subject": e.subject, "snippet": e.snippet, "body": e.snippet}
        if db.draft_exists_for_thread(e.id):
            return {"id": e.id, "is_rejection": False, "skip": "already handled in an earlier run"}
        cls = classify_email(email)
        out = {"id": e.id, "is_rejection": cls.is_rejection, "company": cls.company,
               "role": cls.role, "confidence": cls.confidence, "reason": cls.reason}
        if cls.is_rejection:
            subject, reply = draft_reply(email, cls)
            out.update({"draft_subject": subject, "draft_body": reply})
        return out

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(one, emails))

    for e, r in zip(emails, results):
        if r.get("skip"):
            msg = f"skip - {e.subject[:70]} ({r['skip']})"
        else:
            msg = (f"{'REJECTION' if r['is_rejection'] else 'skip'} - {e.subject[:70]} "
                   f"(company={r.get('company') or '?'}, role={r.get('role') or '?'})")
        db.add_run_event(run_id, "classify", msg, {"email_id": e.id, **{k: v for k, v in r.items()
                                                                     if k != "draft_body"}})
    return {"results": results}


@router.post("/{run_id}/drafted")
def drafted(run_id: str, body: DraftedBody):
    """The extension typed a reply into Gmail: record it + update the tracker row."""
    _run_or_404(run_id)
    db.save_draft(run_id, body.email_id, body.to, body.subject, body.body, thread_id=body.email_id)
    db.set_draft_status_by_thread(body.email_id, "in_gmail")
    tracker = "no tracker row"
    try:
        with _lock:
            if run_id not in _backups:
                bak = excel_client.backup_tracker()
                _backups[run_id] = bak.name
                db.add_run_event(run_id, "safety", f"Tracker backed up to {bak.name}", {})
        note = f"Feedback requested on {time.strftime('%Y-%m-%d')}"
        done = False
        if config.EXCEL_DESKTOP:
            try:   # visibly, in the Excel desktop app
                from .desktop import excel_desktop
                with _lock:
                    r = excel_desktop.update_status(body.company, body.role, "Declined", note=note)
                done = True
                if r.get("matched"):
                    tracker = f"Excel: tracker row {r['row']} -> Declined"
            except Exception as e:
                db.add_run_event(run_id, "warning", f"Excel app automation unavailable ({e}); "
                                                    "updating the file directly", {})
        if not done:
            idx = excel_client.find_match(company=body.company, role=body.role)
            if idx is not None:
                excel_client.update_row(row_idx=idx, status="Declined", note_append=note)
                tracker = f"tracker row {idx} -> Declined"
    except PermissionError:
        tracker = "tracker is open in Excel - close it so AutoPilot can update it"
        db.add_run_event(run_id, "warning", tracker, {})
    except Exception as e:
        tracker = f"tracker update failed: {e}"
        db.add_run_event(run_id, "warning", tracker, {})
    db.add_run_event(run_id, "action", f"Draft typed in Gmail for {body.company or body.to or 'email'} "
                                       f"(not sent) · {tracker}", {"email_id": body.email_id})
    return {"ok": True, "tracker": tracker}


@router.post("/{run_id}/finish")
def finish(run_id: str, body: FinishBody):
    run = _run_or_404(run_id)
    events = db.get_run_events(run_id)
    rejections = sum(1 for e in events if e["kind"] == "classify" and e["data"].get("is_rejection"))
    drafts = db.list_drafts(run_id=run_id)
    rows_updated = sum(1 for e in events if e["kind"] == "action" and "-> Declined" in e["message"])

    ghosted = []
    if drafts and not body.aborted:
        try:
            if config.EXCEL_DESKTOP:
                try:
                    from .desktop import excel_desktop
                    ghosted = excel_desktop.apply_ghosted()
                except Exception:
                    ghosted = excel_client.apply_ghosted_rule()
            else:
                ghosted = excel_client.apply_ghosted_rule()
            if ghosted:
                db.add_run_event(run_id, "action", f"Ghosted rule applied to {len(ghosted)} stale rows", {})
        except Exception as e:
            db.add_run_event(run_id, "warning", f"ghosted rule skipped: {e}", {})

    elapsed = round(time.time() - run["started_at"], 1)
    manual_minutes = round(len(drafts) * config.MINUTES_PER_EMAIL, 1)
    weekly = manual_minutes * config.WORKDAYS_PER_WEEK
    report = {
        "mode": "browser", "demo_mode": False, "engine": "browser_agent",
        "rejections": rejections, "drafts_created": len(drafts), "rows_updated": rows_updated,
        "ghosted": ghosted, "agent_seconds": elapsed,
        "manual_minutes_equivalent": manual_minutes,
        "weekly_minutes_saved": round(weekly, 1),
        "weekly_hours_saved": round(weekly / 60, 2),
        "monthly_saving_gbp": round(weekly / 60 * 4.33 * config.HOURLY_RATE, 2),
        "aborted": body.aborted, "error": body.error,
    }
    if body.error:
        db.add_run_event(run_id, "error", f"[browser agent] {body.error}", {})
    db.add_run_event(run_id, "done",
                     f"[browser agent] {'stopped by you' if body.aborted else 'finished'} in {elapsed}s: "
                     f"{len(drafts)} draft(s) typed in Gmail (none sent), {rows_updated} tracker row(s) updated",
                     {"report": report})
    db.finish_run(run_id, report)
    if run.get("task_id"):
        db.set_task_status(run["task_id"], "automated" if drafts else "new")
    _backups.pop(run_id, None)
    return report
