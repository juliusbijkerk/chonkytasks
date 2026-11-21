"""Automation agent: the rejection-email flow.

Pipeline (deterministic fallback, always works in Demo Mode):
  fetch candidate emails -> classify (Mistral small / keyword fallback)
  -> fuzzy-match tracker row -> draft polite feedback request (draft only!)
  -> update applications.xlsx (with backup) -> apply Ghosted rule -> report.

When a Mistral API key is present and USE_MISTRAL_AGENT=1, the flow is driven
as an AGENT: the model decides which tools to call and in what order
(search_gmail, classify_email, read_tracker, update_tracker_row,
create_gmail_draft, apply_ghosted_rule). We first try a real Mistral Agent
(beta.agents.create + agents.complete); if the org forbids agent creation we
fall back to a ReAct tool-calling loop on chat.complete with the large model.
Either way, every tool call is recorded as a run event for the UI feed.
"""
import json
import re
import time
from typing import Callable, Optional

from . import config, db, excel_client
from .mistral_util import chat_complete, content_text, parse_json_response
from .models import ClassifyResult

REJECTION_QUERY = ('newer_than:60d ("unfortunately" OR "regret to inform" OR "not moving forward" '
                   'OR "other candidates" OR "not selected" OR "will not be proceeding")')

REJECTION_KEYWORDS = [
    "unfortunately", "regret to inform", "not moving forward", "other candidates",
    "not selected", "will not be proceeding", "decided not to", "not be moving forward",
    "we won't be moving forward", "we will not be moving forward",
]
NON_REJECTION_KEYWORDS = ["interview invitation", "next steps", "offer", "we'd love to",
                          "application received", "confirm receipt", "newsletter", "digest"]


# ---------------------------------------------------------------------------
# Email sources
# ---------------------------------------------------------------------------

def load_demo_emails() -> list[dict]:
    with open(config.DEMO_DATA / "sample_emails.json", encoding="utf-8") as f:
        return json.load(f)


def fetch_emails(demo_mode: bool) -> tuple[list[dict], str]:
    """Return (emails, source_description). Normalised dict shape for both modes."""
    if demo_mode:
        return load_demo_emails(), "demo dataset (sample_emails.json)"
    from . import gmail_client
    raw = gmail_client.search_messages(REJECTION_QUERY, max_results=20)
    emails = [{
        "id": m["id"], "thread_id": m.get("thread_id"), "from_addr": m["from_addr"],
        "from_name": m["from_name"], "subject": m["subject"], "date": m["date"],
        "snippet": m.get("snippet", ""), "body": m.get("body", ""),
    } for m in raw]
    return emails, "Gmail API"


# ---------------------------------------------------------------------------
# Classification (Mistral small with JSON mode; keyword fallback)
# ---------------------------------------------------------------------------

CLASSIFY_SYSTEM = """You classify job-application emails. Return STRICT JSON:
{"is_rejection": bool, "company": str, "role": str, "confidence": float, "reason": str}
is_rejection=true ONLY if an employer or recruiter clearly declines the candidate's application for a
JOB or INTERNSHIP (unfortunately, regret to inform, not moving forward, other candidates, not
selected, will not be proceeding...). Everything else is NOT a rejection: interview invitations,
application acknowledgements, reminders to finish an application, job ads / "we're hiring"
newsletters, offers, and any non-job process - visa, immigration, university/admissions, banking,
tickets/support cases being "closed", government or administrative notices.
When unsure, answer false with low confidence.
company = the hiring company's name. role = the job title only (e.g. "Data Analyst"); never a
person's name or the email subject - use "" if no job title is stated."""


def classify_email(email: dict) -> ClassifyResult:
    if config.has_api_key():
        for attempt in range(2):
            try:
                resp = chat_complete(
                    messages=[{"role": "user", "content": json.dumps({
                        "from": email.get("from_name"), "subject": email.get("subject"),
                        "snippet": email.get("snippet"), "body": (email.get("body") or "")[:1500]},
                        ensure_ascii=False)}],
                    system=CLASSIFY_SYSTEM,
                    model=config.MISTRAL_SMALL_MODEL,
                    response_format={"type": "json_object"},
                    temperature=0.0, max_tokens=300,
                )
                data = parse_json_response(resp["choices"][0]["message"]["content"])
                if data is None:
                    raise ValueError("non-JSON classify response")
                return ClassifyResult(**data)
            except Exception as e:
                print(f"[agent] classify attempt {attempt + 1} failed: {e}")
    # ---- deterministic fallback ----
    text = f"{email.get('subject','')} {email.get('snippet','')} {email.get('body','')}".lower()
    is_rej = any(k in text for k in REJECTION_KEYWORDS) and not any(
        k in text for k in NON_REJECTION_KEYWORDS[:4])
    company, role = _extract_company_role(email)
    reason = "matched rejection phrase" if is_rej else "no rejection phrase found"
    return ClassifyResult(is_rejection=is_rej, company=company, role=role,
                          confidence=0.85 if is_rej else 0.9, reason=reason)


def _extract_company_role(email: dict) -> tuple[str, str]:
    subj = email.get("subject", "")
    m = re.search(r"[-–—]\s*(.+?)\s+at\s+(.+)$", subj, re.IGNORECASE) or \
        re.search(r"at\s+(.+)$", subj, re.IGNORECASE)
    role, company = "", ""
    if m and m.lastindex == 2:
        role, company = m.group(1).strip(), m.group(2).strip()
    elif m:
        company = m.group(1).strip()
    if not company:
        m2 = re.search(r"([A-Z][\w&.'-]*(?:\s+[A-Z][\w&.'-]*){0,2})\s+(?:Talent Team|Recruiting|HR|Jobs|Careers)\b",
                       email.get("body", ""))
        if m2:
            company = m2.group(1).strip()
    # strip trailing status phrases like "- not selected"
    company = re.split(r"\s+[-\u2013\u2014]\s+", company)[0].strip() if company else company
    return company.rstrip(".,"), role.rstrip(".,")


# ---------------------------------------------------------------------------
# Draft replies (Mistral small; template fallback)
# ---------------------------------------------------------------------------

DRAFT_SYSTEM = """You write short, polite, professional reply emails asking for feedback after a job
rejection. 3-5 sentences. Mention the role and company. Do NOT promise anything, do not sound bitter.
Return STRICT JSON: {"subject": str, "body": str}. Sign off with the user's name."""


def draft_reply(email: dict, cls: ClassifyResult) -> tuple[str, str]:
    """Return (subject, body)."""
    if config.has_api_key():
        for attempt in range(2):
            try:
                resp = chat_complete(
                    messages=[{"role": "user", "content": json.dumps({
                        "from_name": email.get("from_name"),
                        "company": cls.company, "role": cls.role,
                        "subject": email.get("subject"),
                        "user_name": config.USER_NAME}, ensure_ascii=False)}],
                    system=DRAFT_SYSTEM,
                    model=config.MISTRAL_SMALL_MODEL,
                    response_format={"type": "json_object"},
                    temperature=0.4, max_tokens=400,
                )
                data = parse_json_response(resp["choices"][0]["message"]["content"])
                if data is None:
                    raise ValueError("non-JSON draft response")
                return data.get("subject") or f"Re: {email.get('subject','')}", data.get("body", "")
            except Exception as e:
                print(f"[agent] draft attempt {attempt + 1} failed: {e}")
    # ---- template fallback ----
    subject = f"Re: {email.get('subject', 'Your application')}"
    body = (f"Hi {email.get('from_name', 'there')},\n\n"
            f"Thank you for letting me know about your decision on the {cls.role or 'role'} "
            f"at {cls.company or 'your company'}.\n\n"
            f"If possible, I would really appreciate any brief feedback on my application - "
            f"it would help me a lot in my ongoing search.\n\n"
            f"Thank you again for your time and consideration.\n\n"
            f"Best regards,\n{config.USER_NAME}")
    return subject, body


# ---------------------------------------------------------------------------
# Agent tool specs (used by both the Mistral Agent and the ReAct tool loop)
# ---------------------------------------------------------------------------

_AGENT_ID: Optional[str] = None
_AGENT_TRIED = False

TOOL_SPECS = [
    {"type": "function", "function": {"name": "search_gmail",
     "description": "Search candidate emails (rejection phrases) in Gmail or the demo dataset.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "classify_email",
     "description": "Classify one email: is it a rejection? Extract company and role.",
     "parameters": {"type": "object", "properties": {"email_id": {"type": "string"}}, "required": ["email_id"]}}},
    {"type": "function", "function": {"name": "read_tracker",
     "description": "Read the applications tracker rows (Company, Role, Applied Date, Status...).",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "update_tracker_row",
     "description": "Set a tracker row's Status (e.g. Declined) and append a Notes entry.",
     "parameters": {"type": "object", "properties": {
         "company": {"type": "string"}, "role": {"type": "string"},
         "status": {"type": "string"}, "note": {"type": "string"}},
         "required": ["company", "status"]}}},
    {"type": "function", "function": {"name": "create_gmail_draft",
     "description": "Create a Gmail draft reply asking for feedback. NEVER sends.",
     "parameters": {"type": "object", "properties": {
         "email_id": {"type": "string"}, "to": {"type": "string"},
         "subject": {"type": "string"}, "body": {"type": "string"}},
         "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {"name": "apply_ghosted_rule",
     "description": "Mark 'Applied' rows older than 21 days with no reply as Ghosted.",
     "parameters": {"type": "object", "properties": {}}}},
]

AGENT_INSTRUCTIONS = """You are AutoPilot's Rejection Handler Agent. Goal: handle job-rejection emails.
For each candidate email from search_gmail: classify it; skip non-rejections; for rejections,
match company/role to the tracker (read_tracker, update_tracker_row to set Status=Declined with a
note), and create_gmail_draft with a polite feedback request (NEVER send). Finally call
apply_ghosted_rule. Work email by email and report a JSON summary:
{"handled": n, "drafts": n, "updated_rows": n, "ghosted": n, "unmatched": [...]}"""


def _ensure_mistral_agent() -> Optional[str]:
    """Try to create a hosted Mistral Agent once per process (POST /v1/agents).
    Returns agent_id or None - some orgs forbid agent creation ("Use Managed
    Agents instead"), in which case we use the ReAct tool-calling loop."""
    global _AGENT_ID, _AGENT_TRIED
    if _AGENT_TRIED:
        return _AGENT_ID
    _AGENT_TRIED = True
    if not config.has_api_key():
        return None
    try:
        import httpx
        from .mistral_util import BASE, _headers
        r = httpx.post(f"{BASE}/agents", headers=_headers(), timeout=60, json={
            "name": "autopilot-rejection-handler",
            "model": config.MISTRAL_LARGE_MODEL,
            "instructions": AGENT_INSTRUCTIONS,
            "tools": TOOL_SPECS,
        })
        r.raise_for_status()
        _AGENT_ID = r.json().get("id")
        print(f"[agent] created Mistral Agent {_AGENT_ID}")
    except Exception as e:
        print(f"[agent] agent creation unavailable for this org; using tool-calling loop ({str(e)[:80]})")
        _AGENT_ID = None
    return _AGENT_ID


# ---------------------------------------------------------------------------
# The run itself
# ---------------------------------------------------------------------------

def run_rejection_flow(task_id: Optional[str], mode: str, demo_mode: bool,
                       emit: Callable[[str, str, dict], None]) -> dict:
    """Deterministic pipeline. mode: dry_run (no writes) | draft | auto."""
    start = time.time()
    emails, source = fetch_emails(demo_mode)
    emit("info", f"Fetched {len(emails)} candidate emails from {source}", {"count": len(emails)})

    write = mode != "dry_run"
    backup_path = None
    if write:
        backup_path = excel_client.backup_tracker()
        emit("safety", f"Tracker backed up to {backup_path.name}", {"backup": str(backup_path)})

    per_email, drafts_created, rows_updated, unmatched = [], 0, 0, []
    for email in emails:
        cls = classify_email(email)
        emit("classify",
             f"{'REJECTION' if cls.is_rejection else 'skip'} - {email.get('subject','')[:70]} "
             f"(company={cls.company or '?'}, role={cls.role or '?'}, conf={cls.confidence})",
             {"email_id": email.get("id"), "is_rejection": cls.is_rejection,
              "company": cls.company, "role": cls.role, "reason": cls.reason})
        if not cls.is_rejection:
            per_email.append({"email_id": email.get("id"), "subject": email.get("subject"),
                              "result": "skipped_not_rejection"})
            continue
        row_idx = excel_client.find_match(company=cls.company, role=cls.role)
        subject, body = draft_reply(email, cls)
        draft_id = None
        if write:
            draft_id = db.save_draft(None, email.get("id"), email.get("from_addr"),
                                     subject, body, email.get("thread_id"))
            if mode == "auto":
                db.set_draft_status(draft_id, "approved")
        matched = row_idx is not None
        if matched and write:
            today = time.strftime("%Y-%m-%d")
            excel_client.update_row(row_idx=row_idx, status="Declined",
                                    note_append=f"Feedback requested on {today}")
            rows_updated += 1
        if not matched:
            unmatched.append({"company": cls.company, "role": cls.role, "email_id": email.get("id")})
        if write and draft_id:
            drafts_created += 1
        emit("action",
             f"{'Would draft' if not write else 'Drafted'} reply to {email.get('from_name')} "
             f"({'tracker row updated' if matched and write else 'no tracker match' if not matched else 'dry run'})",
             {"email_id": email.get("id"), "draft_id": draft_id, "row": row_idx, "matched": matched})
        per_email.append({"email_id": email.get("id"), "subject": email.get("subject"),
                          "from": email.get("from_addr"), "company": cls.company, "role": cls.role,
                          "result": "drafted" if write else "would_draft",
                          "tracker_row": row_idx, "matched": matched, "draft_id": draft_id,
                          "draft_subject": subject, "draft_body": body})

    ghosted = []
    if write:
        ghosted = excel_client.apply_ghosted_rule()
        if ghosted:
            emit("action", f"Ghosted rule applied to {len(ghosted)} stale 'Applied' rows",
                 {"ghosted": ghosted})

    elapsed_s = round(time.time() - start, 2)
    n_rejections = sum(1 for p in per_email if p["result"] in ("drafted", "would_draft"))
    manual_minutes = round(n_rejections * config.MINUTES_PER_EMAIL, 1)
    weekly_minutes_saved = manual_minutes * config.WORKDAYS_PER_WEEK
    report = {
        "mode": mode, "demo_mode": demo_mode, "email_source": source,
        "emails_found": len(emails), "rejections": n_rejections,
        "drafts_created": drafts_created, "rows_updated": rows_updated,
        "ghosted": ghosted, "unmatched": unmatched, "per_email": per_email,
        "backup": str(backup_path) if backup_path else None,
        "agent_seconds": elapsed_s,
        "manual_minutes_equivalent": manual_minutes,
        "weekly_minutes_saved": round(weekly_minutes_saved, 1),
        "weekly_hours_saved": round(weekly_minutes_saved / 60, 2),
        "monthly_saving_gbp": round(weekly_minutes_saved / 60 * 4.33 * config.HOURLY_RATE, 2),
        "minutes_per_email_assumed": config.MINUTES_PER_EMAIL,
    }
    emit("done", f"Run finished in {elapsed_s}s: {n_rejections} rejections, {drafts_created} drafts, "
                 f"{rows_updated} rows updated, {len(ghosted)} ghosted. "
                 f"~{report['weekly_hours_saved']} h/week saved (~GBP {report['monthly_saving_gbp']}/month)",
         {"report": report})
    return report


def run_flow(task_id: Optional[str], mode: str, demo_mode: bool, run_id: str) -> dict:
    """Entry point used by the API: wires run events into the DB and executes."""
    def emit(kind: str, message: str, data: Optional[dict] = None):
        db.add_run_event(run_id, kind, message, data or {})

    if config.has_api_key() and config.USE_MISTRAL_AGENT:
        try:
            return _run_tool_loop(mode, demo_mode, run_id, emit)
        except Exception as e:
            emit("warning", f"Agent path failed ({e}); falling back to deterministic pipeline", {})
    report = run_rejection_flow(task_id, mode, demo_mode, emit)
    report["engine"] = "deterministic_pipeline"
    return report


def _run_tool_loop(mode: str, demo_mode: bool,
                   run_id: str, emit: Callable[[str, str, dict], None]) -> dict:
    """Agent run: the MODEL chooses which tools to call and in what order
    (ReAct tool-calling loop on chat/completions with the large model).
    A hosted Mistral Agent (beta.agents) is used instead when the org allows
    agent creation - see _ensure_mistral_agent()."""
    agent_id = _ensure_mistral_agent()
    engine = "mistral_agent" if agent_id else "mistral_tool_agent"
    start = time.time()
    emails, source = fetch_emails(demo_mode)
    emit("info", f"[{engine}] {len(emails)} candidate emails from {source}"
                 + (f" (agent {agent_id})" if agent_id else f" (model {config.MISTRAL_LARGE_MODEL})"),
         {"engine": engine, "agent_id": agent_id, "count": len(emails)})
    write = mode != "dry_run"
    if write:
        bak = excel_client.backup_tracker()
        emit("safety", f"Tracker backed up to {bak.name}", {"backup": str(bak)})

    email_by_id = {e["id"]: e for e in emails}
    # tallies for the impact report (same fields as the deterministic pipeline)
    rejection_ids: set = set()
    stats = {"drafts_created": 0, "rows_updated": 0}
    ghosted: list = []
    unmatched: list = []

    def execute_tool(name: str, args: dict) -> dict:
        emit("tool_call", f"{name}({json.dumps(args, ensure_ascii=False)[:120]})", {"tool": name, "args": args})
        if name == "search_gmail":
            return {"emails": [{"id": e["id"], "from": e["from_addr"], "subject": e["subject"]} for e in emails]}
        if name == "classify_email":
            e = email_by_id.get(args.get("email_id", ""), {})
            cls = classify_email(e)
            if cls.is_rejection and e:
                rejection_ids.add(e["id"])
            return cls.model_dump()
        if name == "read_tracker":
            rows = excel_client.read_rows()
            return {"rows": [{k: str(v) for k, v in r.items()} for r in rows]}
        if name == "update_tracker_row":
            if not write:
                return {"dry_run": True, "would_update": args}
            idx = excel_client.find_match(company=args.get("company", ""), role=args.get("role", ""))
            if idx is None:
                unmatched.append({"company": args.get("company"), "role": args.get("role")})
                return {"matched": False, "company": args.get("company")}
            excel_client.update_row(row_idx=idx, status=args.get("status", "Declined"),
                                    note_append=args.get("note", ""))
            stats["rows_updated"] += 1
            return {"matched": True, "row": idx}
        if name == "create_gmail_draft":
            if not write:
                return {"dry_run": True, "would_draft": {k: (v[:80] + "..." if k == "body" else v) for k, v in args.items()}}
            did = db.save_draft(run_id, args.get("email_id"), args.get("to"),
                                args.get("subject", ""), args.get("body", ""))
            if mode == "auto":
                db.set_draft_status(did, "approved")
            stats["drafts_created"] += 1
            return {"draft_id": did}
        if name == "apply_ghosted_rule":
            if not write:
                return {"dry_run": True}
            g = excel_client.apply_ghosted_rule()
            ghosted.extend(g)
            return {"ghosted": g}
        return {"error": f"unknown tool {name}"}

    messages = [{"role": "system", "content": AGENT_INSTRUCTIONS},
                {"role": "user", "content":
                 f"Handle the job rejection emails now. Mode={mode} "
                 f"({'DRY RUN: do not write anything' if mode == 'dry_run' else 'create drafts and update the tracker'}). "
                 f"Start with search_gmail. There are {len(emails)} candidate emails. "
                 "Finish with a JSON summary of what you did."}]
    final_text = ""
    for step in range(14):
        if agent_id:
            # hosted Mistral Agent (only when the org allows agent creation)
            import httpx
            from .mistral_util import BASE, _headers
            r = httpx.post(f"{BASE}/chat/completions", headers=_headers(),
                           json={"agent_id": agent_id, "messages": messages, "max_tokens": 900},
                           timeout=120)
            r.raise_for_status()
            resp = r.json()
        else:
            resp = chat_complete(messages=messages, model=config.MISTRAL_LARGE_MODEL,
                                 tools=TOOL_SPECS, tool_choice="auto", max_tokens=1500,
                                 reasoning_effort="low")
        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            final_text = content_text(msg.get("content"))
            break
        messages.append({"role": "assistant", "content": content_text(msg.get("content")),
                         "tool_calls": [
                             {"id": tc["id"], "type": "function",
                              "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                             for tc in tool_calls]})
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
            except Exception:
                args = {}
            result = execute_tool(tc["function"]["name"], args)
            emit("tool_result", f"{tc['function']['name']} -> {json.dumps(result, ensure_ascii=False)[:150]}",
                 {"tool": tc["function"]["name"], "result": result})
            messages.append({"role": "tool", "tool_call_id": tc["id"],
                             "name": tc["function"]["name"], "content": json.dumps(result, ensure_ascii=False)[:2000]})
    elapsed = round(time.time() - start, 2)
    n_rejections = len(rejection_ids)
    manual_minutes = round(n_rejections * config.MINUTES_PER_EMAIL, 1)
    weekly_minutes_saved = manual_minutes * config.WORKDAYS_PER_WEEK
    report = {
        "mode": mode, "demo_mode": demo_mode, "engine": engine, "agent_id": agent_id,
        "model": None if agent_id else config.MISTRAL_LARGE_MODEL,
        "emails_found": len(emails), "agent_seconds": elapsed,
        "rejections": n_rejections, "drafts_created": stats["drafts_created"],
        "rows_updated": stats["rows_updated"], "ghosted": ghosted, "unmatched": unmatched,
        "manual_minutes_equivalent": manual_minutes,
        "weekly_minutes_saved": round(weekly_minutes_saved, 1),
        "weekly_hours_saved": round(weekly_minutes_saved / 60, 2),
        "monthly_saving_gbp": round(weekly_minutes_saved / 60 * 4.33 * config.HOURLY_RATE, 2),
        "minutes_per_email_assumed": config.MINUTES_PER_EMAIL,
        "agent_final_message": final_text[:2000],
        "drafts": db.list_drafts(run_id=run_id),
        "n_tool_calls": sum(1 for e in db.get_run_events(run_id) if e["kind"] == "tool_call"),
    }
    emit("done", f"[{engine}] finished in {elapsed}s with {report['n_tool_calls']} tool calls",
         {"report": {"engine": engine}})
    return report
