"""AutoPilot MVP - FastAPI backend.

Run:  uvicorn backend.main:app --reload
Demo Mode (no Gmail, no extension needed): POST /api/sessions/load-sample then
POST /api/agents/{task_id}/run with {"mode": "draft", "demo_mode": true}.
"""
import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import agent, analyzer, config, db, voice
from .models import EventBatch, RunRequest, SessionStart

app = FastAPI(title="AutoPilot", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

db.init()

from .browser_agent import router as browser_agent_router  # noqa: E402
app.include_router(browser_agent_router)
from .desktop_api import router as desktop_agent_router  # noqa: E402
app.include_router(desktop_agent_router)


@app.on_event("startup")
def _start_file_watcher():
    # A server (re)start never resumes recording: close sessions left open earlier, so the
    # whole-computer recorder can't silently start capturing. (The extension, if it still
    # thinks it's recording, gets 409 on its next batch and stops itself.)
    n = db._exec("UPDATE sessions SET ended_at=? WHERE ended_at IS NULL", (time.time(),)).rowcount
    if n:
        print(f"[startup] closed {n} recording session(s) left open")
    try:   # whole-computer recording (Windows): apps, windows, clicks, typing bursts
        from .desktop import observer
        observer.start()
    except Exception as e:
        print(f"[desktop observer] not started ({e}); falling back to the Excel file watcher")
        from . import file_watcher
        file_watcher.start()


# ---------------------------------------------------------------------------
# Sessions & events (observer input)
# ---------------------------------------------------------------------------

@app.post("/api/sessions/start")
def start_session(body: SessionStart):
    s = db.start_session(body.label, source="extension")
    return {**s, "expires_at": time.time() + body.duration_min * 60,
            "note": "The Chrome extension (or the UI) posts events to /api/events."}


@app.post("/api/sessions/{session_id}/stop")
def stop_session(session_id: str):
    prev = db.get_session(session_id)
    if not prev:
        raise HTTPException(404, "session not found")
    if prev.get("ended_at"):
        return {**prev, "already_stopped": True}
    return {**db.stop_session(session_id), "already_stopped": False}


@app.post("/api/events")
def post_events(batch: EventBatch):
    s = db.get_session(batch.session_id)
    if not s:
        raise HTTPException(404, "unknown session_id - call /api/sessions/start first")
    if s.get("ended_at"):
        # stopped from the dashboard: tells the extension to stop recording
        raise HTTPException(409, "session already stopped")
    n = db.add_events(batch.session_id, [e.model_dump() for e in batch.events])
    return {"received": n}


@app.get("/api/events/{session_id}")
def get_events(session_id: str):
    return {"session_id": session_id, "events": db.list_events(session_id)}


@app.post("/api/sessions/load-sample")
def load_sample(analyze: bool = True):
    """Demo Mode: load the bundled sample session (3 repetitive workflows)."""
    path = config.DEMO_DATA / "sample_events.json"
    events = json.loads(path.read_text(encoding="utf-8"))
    sid = db.start_session("sample session (demo)", source="demo")["id"]
    # re-stamp events with the new session id
    for e in events:
        e["session_id"] = sid
    db.add_events(sid, events)
    db.stop_session(sid)
    result = None
    if analyze:
        result = analyzer.analyze_session(sid).model_dump()
    return {"session_id": sid, "events_loaded": len(events), "analysis": result}


@app.get("/api/sessions/active")
def active_session():
    """The most recent open (not stopped) recording session, if any - lets the
    web UI mirror the Chrome extension's recording state."""
    s = db.latest_session(open_only=True)
    if s:
        s["event_count"] = db.count_events(s["id"])
    return {"session": s}


@app.post("/api/sessions/analyze-latest")
def analyze_latest():
    """Stop + analyse the most recent real (non-demo) session that has events.
    The web UI's 'Stop & analyse' uses this, because recording happens in the
    Chrome extension, which creates its own session."""
    s = db.latest_session(with_events=True)
    if not s:
        raise HTTPException(404, "No recorded events yet - start observation from the "
                                 "AutoPilot Chrome extension and browse for a bit first.")
    if not s.get("ended_at"):
        db.stop_session(s["id"])
    return {"session_id": s["id"], "event_count": db.count_events(s["id"]),
            "analysis": analyzer.analyze_session(s["id"]).model_dump()}


@app.post("/api/sessions/{session_id}/analyze")
def analyze(session_id: str):
    if not db.get_session(session_id):
        raise HTTPException(404, "session not found")
    return analyzer.analyze_session(session_id).model_dump()


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _dashboard_payload() -> dict:
    analysis = db.get_latest_analysis()
    # only the latest analysed session's tasks: re-analysing / reloading the sample
    # must not stack duplicate tasks (and double the hours)
    latest_sid = (analysis or {}).get("session_id")
    tasks = [t for t in db.list_tasks() if t["session_id"] == latest_sid] if latest_sid else []
    weekly_minutes_lost = sum(t["weekly_minutes"] for t in tasks)
    hours_lost = weekly_minutes_lost / 60
    potential_saved = sum(t.get("proposal", {}).get("estimated_weekly_minutes_saved", 0) for t in tasks)
    hours_saved = potential_saved / 60
    monthly_gbp = hours_saved * 4.33 * config.HOURLY_RATE
    # realised savings from finished runs
    realised_minutes = 0.0
    for r in db.list_runs(50):
        rep = r.get("report") or {}
        rep_minutes = rep.get("manual_minutes_equivalent")
        if rep_minutes and r["mode"] != "dry_run":
            realised_minutes += rep_minutes
    return {
        "has_api_key": config.has_api_key(),
        "models": {"large": config.MISTRAL_LARGE_MODEL, "small": config.MISTRAL_SMALL_MODEL,
                   "voice": config.MISTRAL_VOICE_MODEL},
        "summary": {
            "tasks_found": len(tasks),
            "hours_lost_week": round(hours_lost, 2),
            "hours_saved_week": round(hours_saved, 2),
            "monthly_saving_gbp": round(monthly_gbp, 2),
            "hours_saved_realised": round(realised_minutes / 60, 2),
            "hourly_rate_gbp": config.HOURLY_RATE,
        },
        "tasks": tasks,
        "other_ideas": (analysis or {}).get("other_ideas", []),
        "recent_runs": db.list_runs(10),
    }


@app.get("/api/dashboard")
def dashboard():
    return _dashboard_payload()


@app.post("/api/tasks/{task_id}/dismiss")
def dismiss_task(task_id: str):
    if not db.get_task(task_id):
        raise HTTPException(404, "task not found")
    db.set_task_status(task_id, "dismissed")
    return {"ok": True, "task_id": task_id}


# ---------------------------------------------------------------------------
# Agent runs
# ---------------------------------------------------------------------------

@app.post("/api/agents/{task_id}/run")
def run_agent(task_id: str, body: RunRequest):
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    run_id = db.create_run(task_id, body.mode, body.demo_mode)
    db.set_task_status(task_id, "running")

    def _work():
        try:
            report = agent.run_flow(task_id, body.mode, body.demo_mode, run_id)
            db.finish_run(run_id, report)
            db.set_task_status(task_id, "automated" if body.mode != "dry_run" else "new")
        except Exception as e:
            db.add_run_event(run_id, "error", f"run failed: {e}", {})
            db.finish_run(run_id, {"error": str(e)})

    threading.Thread(target=_work, daemon=True).start()
    return {"run_id": run_id, "task_id": task_id, "mode": body.mode, "demo_mode": body.demo_mode}


@app.get("/api/agents/runs/{run_id}")
def get_run(run_id: str):
    r = db.get_run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.get("/api/agents/runs/{run_id}/events")
async def run_events_sse(run_id: str):
    """SSE stream of run events; replays history then follows live until the run finishes."""
    if not db.get_run(run_id):
        raise HTTPException(404, "run not found")

    async def gen():
        last_id = 0
        while True:
            events = db.get_run_events(run_id, after_id=last_id)
            for e in events:
                last_id = e["id"]
                yield f"data: {json.dumps(e)}\n\n"
            run = db.get_run(run_id)
            if run and run["status"] == "finished" and not events:
                yield f"data: {json.dumps({'id': last_id, 'kind': 'stream_end', 'message': 'done', 'data': {}})}\n\n"
                break
            await asyncio.sleep(0.25)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Drafts (approve / edit / skip; sending is explicit and off by default)
# ---------------------------------------------------------------------------

@app.get("/api/drafts")
def list_drafts(run_id: Optional[str] = None, status: Optional[str] = None):
    return {"drafts": db.list_drafts(run_id=run_id, status=status)}


@app.post("/api/drafts/{draft_id}/approve")
def approve_draft(draft_id: str):
    db.set_draft_status(draft_id, "approved")
    return {"ok": True}


@app.post("/api/drafts/{draft_id}/skip")
def skip_draft(draft_id: str):
    db.set_draft_status(draft_id, "skipped")
    return {"ok": True}


class DraftEdit(BaseModel):
    body: str


@app.post("/api/drafts/{draft_id}/edit")
def edit_draft(draft_id: str, body: DraftEdit):
    if not db.get_draft(draft_id):
        raise HTTPException(404, "draft not found")
    db.set_draft_status(draft_id, "edited", body=body.body)
    return {"ok": True}


@app.post("/api/drafts/send-approved")
def send_approved():
    """Explicit, separate action: sends approved drafts via Gmail.
    Demo mode: just marks them 'sent' locally. Off the happy path by design."""
    approved = db.list_drafts(status="approved")
    if not approved:
        return {"sent": 0, "note": "no approved drafts"}
    # Find the run's demo flag
    sent = 0
    for d in approved:
        run = db.get_run(d["run_id"]) if d.get("run_id") else None
        if run and run["demo"]:
            db.set_draft_status(d["id"], "sent")
            sent += 1
            continue
        try:
            from . import gmail_client
            gmail_client.create_draft(d["to_addr"], d["subject"], d["body"], d.get("thread_id"))
            # NOTE: drafts.create only creates a draft; actual sending would use
            # users().messages().send - intentionally left out of the demo path.
            db.set_draft_status(d["id"], "sent")
            sent += 1
        except Exception as e:
            db.add_run_event(d["run_id"] or "manual", "error", f"send failed for {d['id']}: {e}", {})
    return {"sent": sent}


# ---------------------------------------------------------------------------
# Export as Vibe skill / scheduled task (hackathon addendum 11.4)
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "automation"


@app.post("/api/agents/{task_id}/export-vibe-skill")
def export_vibe_skill(task_id: str):
    """Write the approved automation as a reusable artifact under exported_skills/:
    a SKILL.md (Vibe skill format), a runnable run.py, and a schedule.json (weekly cron)."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    slug = _slugify(task["name"])
    folder = config.EXPORTED_SKILLS / slug
    folder.mkdir(parents=True, exist_ok=True)

    skill_md = f"""---
name: {slug}
description: >
  {task['name']} - auto-exported by AutoPilot. {task.get('proposal', {}).get('description', '')}
  Repeated {task['repetitions']}x in a recorded session (~{task['weekly_minutes']} min/week).
---

# {task['name']}

Detected by AutoPilot from a recorded work session. Steps observed:

{chr(10).join(f"{i+1}. {s}" for i, s in enumerate(task.get('steps', [])))}

## What this skill does
{task.get('proposal', {}).get('description', '')}

{chr(10).join('- ' + b for b in task.get('proposal', {}).get('bullets', []))}

## Run it
```bash
python run.py            # dry run
python run.py --apply    # create drafts + update tracker (drafts only, never sends)
```

Safety: creates Gmail drafts only; backs up applications.xlsx before writing.
"""
    (folder / "SKILL.md").write_text(skill_md, encoding="utf-8")

    run_py = f'''"""AutoPilot exported automation: {task['name']}.
Dry run by default; --apply creates Gmail drafts and updates the tracker."""
import json, sys, urllib.request

API = "{config.API_BASE}"
APPLY = "--apply" in sys.argv

def post(path, payload):
    req = urllib.request.Request(API + path, data=json.dumps(payload).encode(),
                                 headers={{"Content-Type": "application/json"}})
    return json.load(urllib.request.urlopen(req))

# 1. make sure a session is analysed (demo sample)
post("/api/sessions/load-sample", None) if False else None
# 2. run the agent on the rejection task
tasks = json.load(urllib.request.urlopen(API + "/api/dashboard"))["tasks"]
task = next((t for t in tasks if t["id"] == "{task_id}"), tasks[0])
run = post(f"/api/agents/{{task['id']}}/run", {{"mode": "draft" if APPLY else "dry_run", "demo_mode": True}})
print(json.dumps(run, indent=2))
'''
    (folder / "run.py").write_text(run_py, encoding="utf-8")

    schedule = {
        "name": slug,
        "cron": "0 9 * * MON",  # every Monday 09:00
        "command": f"cd {config.ROOT} && python exported_skills/{slug}/run.py --apply",
        "description": f"Weekly run of '{task['name']}' exported by AutoPilot",
    }
    (folder / "schedule.json").write_text(json.dumps(schedule, indent=2), encoding="utf-8")

    db.set_task_status(task_id, "automated")
    return {"ok": True, "folder": str(folder), "files": ["SKILL.md", "run.py", "schedule.json"],
            "schedule": schedule}


@app.post("/api/agents/{task_id}/schedule")
def schedule_task(task_id: str):
    """Lightweight: returns the weekly schedule config for a task (cron Mondays 09:00)."""
    task = db.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return {"task_id": task_id, "cron": "0 9 * * MON", "note": "Pair with export-vibe-skill for a runnable artifact."}


# ---------------------------------------------------------------------------
# Voice (Voxtral) - optional
# ---------------------------------------------------------------------------

@app.post("/api/voice")
async def voice_endpoint(file: UploadFile = File(...)):
    data = await file.read()
    transcript = voice.transcribe(data, file.filename or "audio.webm")
    if transcript is None:
        return JSONResponse({"ok": False, "error": "no MISTRAL_API_KEY - voice disabled (demo flow unaffected)",
                             "transcript": ""}, status_code=200)
    intent = voice.route_command(transcript)
    result = {"ok": True, "transcript": transcript, "intent": intent}
    # execute simple intents server-side
    if intent.get("intent") == "load_sample":
        s = load_sample(analyze=True)
        result["session_id"] = s["session_id"]
    elif intent.get("intent") in ("run_rejections_dry", "run_rejections_draft"):
        tasks = db.list_tasks()
        if tasks:
            mode = "dry_run" if intent["intent"].endswith("dry") else "draft"
            r = run_agent(tasks[0]["id"], RunRequest(mode=mode, demo_mode=True))
            result["run_id"] = r["run_id"]
    return result


# ---------------------------------------------------------------------------
# Health + static frontend
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"ok": True, "has_api_key": config.has_api_key(),
            "tracker": str(config.TRACKER_PATH), "tracker_exists": config.TRACKER_PATH.exists()}


@app.get("/")
def index():
    return FileResponse(config.FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=config.FRONTEND), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

