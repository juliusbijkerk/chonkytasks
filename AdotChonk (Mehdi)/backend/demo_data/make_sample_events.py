"""Generate sample_events.json: a realistic 30-minute recorded session containing
3 repetitive workflows (rejection-email handling x6, spreadsheet->web form x3,
weekly report assembly x2). Run: python -m backend.demo_data.make_sample_events"""
import json
import time
from pathlib import Path

OUT = Path(__file__).parent / "sample_events.json"

BASE_MS = int(time.time() * 1000) - 30 * 60 * 1000  # session "happened" in the last 30 min


def ev(session_id, offset_ms, type_, app, detail):
    return {"session_id": session_id, "timestamp_ms": BASE_MS + offset_ms,
            "type": type_, "app": app, "detail": detail}


def rejection_loop(sid, start_ms, search_term, sender, subject):
    """One repetition of: Gmail search -> open rejection -> reply -> Excel tracker update.
    Dwell times scaled so one repetition takes ~3.5 minutes (like real usage)."""
    S = 4.5  # time scale
    def d(gap_ms):
        return int(gap_ms * S)
    e = []
    t = start_ms
    e.append(ev(sid, t, "navigation", "Gmail", {"url": "https://mail.google.com/mail/u/0/#inbox", "title": "Inbox - Gmail"})); t += d(4000)
    e.append(ev(sid, t, "click", "Gmail", {"label": "Search bar"})); t += d(1500)
    e.append(ev(sid, t, "input", "Gmail", {"label": "Search mail", "type": "text"})); t += d(3000)
    e.append(ev(sid, t, "click", "Gmail", {"label": f"Search: {search_term}"})); t += d(6000)  # reading results
    e.append(ev(sid, t, "click", "Gmail", {"label": f"Email from {sender}: {subject}"})); t += d(6000)  # reading email
    e.append(ev(sid, t, "click", "Gmail", {"label": "Reply"})); t += d(2500)
    e.append(ev(sid, t, "input", "Gmail", {"label": "Reply body", "type": "textarea"})); t += d(18000)  # writing reply
    e.append(ev(sid, t, "click", "Gmail", {"label": "Send"})); t += d(2000)
    e.append(ev(sid, t, "tab_switch", "Browser", {"to": "Excel Online"})); t += d(1500)
    e.append(ev(sid, t, "navigation", "Excel Online", {"url": "https://excel.officeapps.live.com/applications.xlsx", "title": "applications.xlsx - Excel"})); t += d(5000)
    e.append(ev(sid, t, "click", "Excel Online", {"label": "Cell C14 (Status)"})); t += d(1000)
    e.append(ev(sid, t, "input", "Excel Online", {"label": "Cell C14", "type": "cell"})); t += d(3000)
    e.append(ev(sid, t, "click", "Excel Online", {"label": "Save"})); t += d(1500)
    e.append(ev(sid, t, "tab_switch", "Browser", {"to": "Gmail"})); t += d(1000)
    e.append(ev(sid, t, "idle", "Gmail", {"seconds": 12}))
    return e, t + 12000


def form_filler_loop(sid, start_ms, candidate):
    """One repetition of: copy row from Sheets -> paste into a web form -> submit."""
    e = []
    t = start_ms
    e.append(ev(sid, t, "navigation", "Sheets", {"url": "https://docs.google.com/spreadsheets/d/candidates", "title": "candidates - Google Sheets"})); t += 4000
    e.append(ev(sid, t, "click", "Sheets", {"label": f"Row: {candidate}"})); t += 1000
    e.append(ev(sid, t, "copy_paste", "Sheets", {"action": "copy"})); t += 1500
    e.append(ev(sid, t, "tab_switch", "Browser", {"to": "CRM portal"})); t += 1000
    e.append(ev(sid, t, "navigation", "Other", {"url": "https://crm.example.com/new-lead", "title": "New lead - CRM"})); t += 5000
    e.append(ev(sid, t, "input", "Other", {"label": "Full name", "type": "text"})); t += 4000
    e.append(ev(sid, t, "input", "Other", {"label": "Email", "type": "email"})); t += 4000
    e.append(ev(sid, t, "copy_paste", "Other", {"action": "paste"})); t += 1000
    e.append(ev(sid, t, "input", "Other", {"label": "Notes", "type": "textarea"})); t += 6000
    e.append(ev(sid, t, "click", "Other", {"label": "Submit lead"})); t += 2500
    e.append(ev(sid, t, "tab_switch", "Browser", {"to": "Sheets"}))
    return e, t + 2000


def report_loop(sid, start_ms):
    """One repetition of: assemble weekly report from Drive + Sheets into a doc."""
    e = []
    t = start_ms
    e.append(ev(sid, t, "navigation", "Drive", {"url": "https://drive.google.com/drive/my-drive", "title": "My Drive"})); t += 4000
    e.append(ev(sid, t, "click", "Drive", {"label": "weekly metrics.xlsx"})); t += 3000
    e.append(ev(sid, t, "copy_paste", "Drive", {"action": "copy"})); t += 1500
    e.append(ev(sid, t, "navigation", "Docs", {"url": "https://docs.google.com/document/d/weekly-report", "title": "Weekly report - Docs"})); t += 5000
    e.append(ev(sid, t, "copy_paste", "Docs", {"action": "paste"})); t += 2000
    e.append(ev(sid, t, "input", "Docs", {"label": "Report body", "type": "textarea"})); t += 15000
    e.append(ev(sid, t, "click", "Docs", {"label": "Share"})); t += 2000
    e.append(ev(sid, t, "idle", "Docs", {"seconds": 10}))
    return e, t + 10000


def main():
    sid = "sample-session-001"
    events = []
    t = 0
    # Workflow 1: rejection emails, repeated 6 times over ~21 minutes
    rejections = [
        ("unfortunately", "Acme Corp Talent Team", "Update on your application - Software Engineer Intern at Acme Corp"),
        ("not moving forward", "Globex Recruiting", "We regret to inform you - Data Analyst at Globex"),
        ("other candidates", "Hooli Jobs", "Your application at Hooli - not selected"),
        ("unfortunately", "Stark Industries HR", "Update - ML Intern application at Stark Industries"),
        ("not selected", "Umbrella Careers", "Update on your application - Backend Developer at Umbrella"),
        ("will not be proceeding", "Nexadyne Careers", "Your application - we will not be proceeding"),
    ]
    for term, sender, subject in rejections:
        loop, t = rejection_loop(sid, t, term, sender, subject)
        events.extend(loop)
    # Workflow 2: copy candidates from Sheets into CRM form, repeated 3 times
    for cand in ["Candidate 41", "Candidate 42", "Candidate 43"]:
        loop, t = form_filler_loop(sid, t, cand)
        events.extend(loop)
    # Workflow 3: weekly report assembly, repeated 2 times
    for _ in range(2):
        loop, t = report_loop(sid, t)
        events.extend(loop)

    OUT.write_text(json.dumps(events, indent=1))
    print(f"Wrote {len(events)} events to {OUT} (session span: {t/60000:.1f} min)")


if __name__ == "__main__":
    main()
