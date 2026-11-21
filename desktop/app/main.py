import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Literal
import re
import shutil
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

from .analyze import analyze, get_model
from .video import extract_frames, make_preview, probe
from . import automations, automation_store, mac_automation, draft_review, privacy_review, workflow_evidence

DATA = ROOT / "data" / "jobs"
DATA.mkdir(parents=True, exist_ok=True)
INBOX_NAME = re.compile(r"[0-9a-f]{32}\.(?:mp4|mov|webm|mkv|m4v)")


def inbox_dir() -> Path:
    # The macOS app copies files dropped on its window/icon here before calling the API.
    folder = DATA / "inbox"
    folder.mkdir(parents=True, exist_ok=True)
    return folder
MAX_BYTES = 500 * 1024 * 1024
EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
JOBS: dict[str, dict] = {}
TASKS: set = set()
SUBSCRIBERS: dict[str, set[asyncio.Queue]] = {}
SLOTS = asyncio.Semaphore(2)


def persist(job):
    folder = DATA / job["id"]
    folder.mkdir(exist_ok=True)
    temp = folder / "job.tmp"
    temp.write_text(json.dumps(job, ensure_ascii=False, indent=2))
    temp.replace(folder / "job.json")
    JOBS[job["id"]] = job
    for queue in SUBSCRIBERS.get(job["id"], ()):
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(json.dumps(job, ensure_ascii=False))


@asynccontextmanager
async def lifespan(app):
    if automation_store.DATABASE.exists():
        automation_store.recover_pending_sends()
        state = automation_store.get()
        if state['status'] in ('activating', 'pausing'):
            automation_store.update(status='error', enabled=False,
                message='Setup was interrupted. Activate again to finish it.')
    for file in DATA.glob("*/job.json"):
        try:
            job = json.loads(file.read_text())
            if job["status"] in ("queued", "processing"):
                job.update(status="failed", stage="failed", message="The app restarted before analysis completed. Retry this recording.")
                persist(job)
            JOBS[job["id"]] = job
        except (ValueError, KeyError):
            continue
    yield
    for task in TASKS:
        task.cancel()
    if TASKS:
        await asyncio.gather(*TASKS, return_exceptions=True)


app = FastAPI(title="chonkytasks", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])


@app.middleware("http")
async def local_requests(request: Request, call_next):
    # Local private app: prevent other websites from starting paid analysis through a browser.
    origin = request.headers.get("origin")
    if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
        return JSONResponse({"detail": "Cross-origin requests are not allowed."}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def ensure_provider():
    if not os.getenv("OPENROUTER_API_KEY", "").strip():
        raise HTTPException(503, "Add OPENROUTER_API_KEY to .env and restart the app.")
    try:
        get_model()
    except ValueError as error:
        raise HTTPException(503, str(error))


def ensure_ready():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise HTTPException(503, "Install FFmpeg and ffprobe to analyze recordings.")
    if sum(j["status"] in ("queued", "processing") for j in JOBS.values()) >= 4:
        raise HTTPException(429, "Four recordings are already in progress. Wait for one to finish.")


def get_job(job_id):
    if job_id not in JOBS:
        raise HTTPException(404, "Recording not found.")
    return JOBS[job_id]


def new_job(name, context):
    return {"id": uuid.uuid4().hex, "filename": name, "display_name": name, "notes": "", "context": context[:2000],
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "queued", "stage": "queued",
            "progress": 0, "message": "Recording queued"}


def launch(job, *, for_analysis=False):
    persist(job)
    task = asyncio.create_task(process(job, for_analysis=for_analysis))
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


async def process(job, *, for_analysis=False):
    folder = DATA / job["id"]
    def update(**values):
        job.update(values)
        persist(job)
    async with SLOTS:
        try:
            if for_analysis:
                update(status="processing", stage="observing", progress=25, message="Analyzing reviewed evidence",
                       started_at=datetime.now(timezone.utc).isoformat(), model_request=None, analysis_progress=None)
                report = await analyze(folder, job, update)
                update(report=workflow_evidence.attach(report, job), status="completed", stage="completed",
                       progress=100, message="Done", local_only=False)
                return
            update(status="processing", stage="preparing", progress=5, message="Preparing the video",
                   started_at=datetime.now(timezone.utc).isoformat(), model_request=None,
                   analysis_progress=None)
            source = folder / job["source_name"]
            metadata = await asyncio.to_thread(probe, source)
            update(metadata=metadata, message="Preparing the video preview")
            await asyncio.to_thread(make_preview, source, folder)
            update(video_url=f"/api/jobs/{job['id']}/video", stage="extracting", progress=15,
                   message="Choosing screenshots")
            frames, interval = await asyncio.to_thread(extract_frames, source, folder, metadata)
            update(frames=frames, sample_interval=interval, progress=21, stage="extracting",
                   message="Checking for repeatable work on this Mac")
            try:
                findings = await asyncio.to_thread(workflow_evidence.scan, source, folder / "preview.mp4", metadata['duration'])
                update(local_findings=findings, local_evidence_status="checked")
            except ValueError:
                update(local_findings=[], local_evidence_status="unavailable")
            update(progress=23, message="Redacting screenshots on this Mac")
            try:
                await asyncio.to_thread(privacy_review.prepare, folder, frames)
            except ValueError as error:
                # A missing redactor does not block local findings or authorize a cloud request.
                update(redaction_ready=False, redaction_message=str(error))
            else:
                update(redaction_ready=True, redaction_message="")
            update(status="review", stage="review", progress=25, message="Review recording evidence")
        except asyncio.CancelledError:
            update(status="failed", stage="failed", message="Analysis stopped when the server shut down. Retry this recording.")
            raise
        except Exception as error:
            # Never expose upstream payloads, headers, or credentials to the browser/logs.
            message = str(error) if isinstance(error, ValueError) else "Analysis could not finish. Check your connection and retry."
            update(status="failed", stage="failed", message=message)


@app.get("/api/health")
async def health():
    try:
        model = get_model()
        error = None
    except ValueError as exc:
        model, error = "Invalid model", str(exc)
    return {"configured": bool(os.getenv("OPENROUTER_API_KEY", "").strip()), "model": model,
            "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")), "error": error}


class MistralConnection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: SecretStr


class AutomationActivation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    recommendation_index: int = Field(ge=0, le=2)


@app.get('/api/connections')
async def connections():
    return await automations.settings()


@app.post('/api/connections/mistral')
async def connect_mistral(connection: MistralConnection):
    key = connection.api_key.get_secret_value().strip()
    if not key or len(key) > 4096:
        raise HTTPException(400, 'Enter a valid Mistral Studio API key.')
    try:
        return await automations.connect(key)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/connections/mac')
async def connect_mac(service: str = Query(default='both', pattern=r'^(both|mail|calendar)$')):
    try:
        return await mac_automation.call('permissions', service=service)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.get('/api/automations')
async def automation_status():
    return await automations.status()


@app.get('/api/activity')
async def activity(before: datetime | None = None, limit: int = Query(default=50, ge=1, le=100), results_only: bool = False):
    return automation_store.activity(before.isoformat() if before else None, limit, results_only)


@app.post('/api/activity/read')
async def read_activity():
    automation_store.mark_activity_read()
    return {'read': True}


@app.post('/api/activity/clear')
async def clear_activity():
    return automation_store.clear_activity()


class DraftSend(BaseModel):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]
    calendar_id: str | None = Field(default=None, max_length=512)
    event_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class CalendarApproval(BaseModel):
    event_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]


@app.get('/api/drafts/{draft_id}')
async def preview_draft(draft_id: str):
    try:
        return await draft_review.preview(draft_id)
    except draft_review.ReviewError as error:
        raise HTTPException(error.status, str(error)) from None


@app.post('/api/drafts/{draft_id}/send')
async def send_draft(draft_id: str, request: DraftSend):
    try:
        return await draft_review.send(draft_id, request.revision, request.calendar_id, request.event_revision)
    except draft_review.ReviewError as error:
        raise HTTPException(error.status, str(error)) from None


@app.post('/api/drafts/{draft_id}/calendar')
async def complete_calendar_event(draft_id: str, request: CalendarApproval):
    try:
        return await draft_review.finish_calendar(draft_id, request.event_revision)
    except draft_review.ReviewError as error:
        raise HTTPException(error.status, str(error)) from None


@app.post('/api/connections/mac/settings/{pane}')
async def open_privacy_settings(pane: str):
    if pane not in ('Calendars', 'Automation'):
        raise HTTPException(404, 'Unknown privacy setting.')
    try:
        return await mac_automation.call('open_settings', pane=pane)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/automations/activate', status_code=202)
async def activate_automation(request: AutomationActivation):
    job = get_job(request.job_id)
    recommendations = job.get('report', {}).get('recommendations', [])
    if job['status'] != 'completed' or request.recommendation_index >= len(recommendations):
        raise HTTPException(409, 'Choose a completed analysis to activate an automation.')
    if not automations.supported(recommendations[request.recommendation_index]):
        raise HTTPException(422, 'This suggestion does not yet have an executable automation.')
    return await queue_automation(job, request.recommendation_index)


async def queue_automation(job, index):
    recommendation = job['report']['recommendations'][index]
    state = automation_store.get()
    if state.get('enabled') or state['status'] == 'activating':
        return await automations.status()
    readiness = await automations.settings()
    if not all(readiness.get(key) for key in ('connected', 'mail', 'calendar', 'runtime_ready')):
        raise HTTPException(409, 'Connect Mistral, Mail and Calendar before activating.')
    automation_store.update(status='activating', enabled=False, source_job_id=job['id'],
        source_recommendation={'title': recommendation['title'], 'automation_recipe': 'meeting_reply_drafts'},
        title=recommendation['title'], message='Starting the background service')
    async def setup():
        try:
            await automations.activate(job, index)
        except ValueError as error:
            automation_store.update(status='error', enabled=False, message=str(error))
    task = asyncio.create_task(setup())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)
    return automation_store.get()


@app.post('/api/automations/resume', status_code=202)
async def resume_automation():
    state = automation_store.get()
    if not state.get('source_recommendation'):
        raise HTTPException(409, 'Activate an automation from a recording first.')
    return await queue_automation({'id': state['source_job_id'],
        'report': {'recommendations': [state['source_recommendation']]}}, 0)


@app.post('/api/automations/pause')
async def pause_automation():
    return await automations.pause()


@app.get("/api/jobs")
async def list_jobs():
    return [{**{k: j[k] for k in ("id", "filename", "created_at", "status", "progress", "message")},
             "display_name": j.get("display_name", j["filename"]), "notes": j.get("notes", "")}
            for j in sorted(JOBS.values(), key=lambda j: j["created_at"], reverse=True)]


@app.post("/api/jobs", status_code=202)
async def upload(file: UploadFile = File(...), context: str = Form("")):
    ensure_ready()
    name = Path(file.filename or "recording.mp4").name
    extension = Path(name).suffix.lower()
    if extension not in EXTENSIONS:
        raise HTTPException(415, "Choose an MP4, MOV, WebM, MKV, or M4V video.")
    job = new_job(name, context)
    folder = DATA / job["id"]
    folder.mkdir()
    job["source_name"] = "source" + extension
    total = 0
    try:
        with (folder / job["source_name"]).open("wb") as target:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise HTTPException(413, "Please upload a recording smaller than 500 MB.")
                target.write(chunk)
        if total == 0:
            raise HTTPException(400, "The uploaded file is empty.")
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    finally:
        await file.close()
    launch(job)
    return job


def desktop_recordings():
    recordings = []
    for folder in (Path.home() / "Desktop", Path.home() / "Bureau"):
        if folder.is_dir():
            recordings.extend(p for p in folder.iterdir() if p.suffix.lower() in EXTENSIONS and p.is_file())
    return sorted(recordings, key=lambda p: p.stat().st_mtime, reverse=True)[:6]


def sample_id(path):
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


@app.get("/api/samples")
async def samples():
    return [{"id": sample_id(p), "name": p.name, "size": p.stat().st_size,
             "modified_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
            for p in desktop_recordings()]


@app.post("/api/samples/{recording_id}/analyze", status_code=202)
async def analyze_sample(recording_id: str, context: str = Form("")):
    ensure_ready()
    source = next((p for p in desktop_recordings() if sample_id(p) == recording_id), None)
    if source is None:
        raise HTTPException(404, "This Desktop recording is no longer available.")
    if source.stat().st_size > MAX_BYTES:
        raise HTTPException(413, "This recording exceeds the 500 MB upload limit.")
    job = new_job(source.name, context)
    folder = DATA / job["id"]
    folder.mkdir()
    job["source_name"] = "source" + source.suffix.lower()
    await asyncio.to_thread(shutil.copyfile, source, folder / job["source_name"])
    launch(job)
    return job


@app.post("/api/jobs/from-inbox", status_code=202)
async def analyze_inbox(name: str = Form(...), filename: str = Form(""), context: str = Form("")):
    # Used by the macOS app: it copies a dropped recording into data/jobs/inbox as
    # "<32 hex chars>.<ext>" and posts only that name. Never accept arbitrary paths.
    ensure_ready()
    if not INBOX_NAME.fullmatch(name.lower()):
        raise HTTPException(400, "Invalid inbox file name.")
    source = (inbox_dir() / name).resolve()
    if source.parent != inbox_dir().resolve() or not source.is_file():
        raise HTTPException(404, "This file is not in the inbox.")
    if source.stat().st_size == 0:
        raise HTTPException(400, "The dropped file is empty.")
    if source.stat().st_size > MAX_BYTES:
        raise HTTPException(413, "Please use a recording smaller than 500 MB.")
    display = Path(filename).name if filename else "recording" + source.suffix.lower()
    job = new_job(display, context)
    folder = DATA / job["id"]
    folder.mkdir()
    job["source_name"] = "source" + source.suffix.lower()
    try:
        shutil.copyfile(source, folder / job["source_name"])
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    source.unlink(missing_ok=True)
    launch(job)
    return job


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    return get_job(job_id)


class EvidenceSelection(BaseModel):
    frame_ids: list[str] = Field(min_length=1, max_length=72)
    context: str = Field(default='', max_length=2000)


class EvidenceApproval(EvidenceSelection):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    confirmed: Literal[True]


def check_evidence_selection(job: dict, selection: EvidenceSelection):
    reviewable = job['status'] == 'review' or (job['status'] == 'completed' and job.get('local_only'))
    if not reviewable or not job.get('redaction_ready'):
        raise HTTPException(409, 'Prepare the local privacy review first.')
    try:
        return privacy_review.review(DATA / job['id'], job['frames'], selection.frame_ids, selection.context, get_model())
    except (ValueError, OSError) as error:
        message = str(error) if isinstance(error, ValueError) else 'A redacted screenshot is unavailable.'
        raise HTTPException(422, message) from None


@app.post('/api/jobs/{job_id}/privacy/preview')
async def preview_evidence(job_id: str, selection: EvidenceSelection, exact: bool = False):
    packet = check_evidence_selection(get_job(job_id), selection)
    return packet.manifest() if exact else packet.selection()


@app.post('/api/jobs/{job_id}/privacy/approve', status_code=202)
async def approve_evidence(job_id: str, approval: EvidenceApproval):
    job = get_job(job_id)
    packet = check_evidence_selection(job, approval)
    if packet.revision != approval.revision:
        raise HTTPException(409, 'The evidence changed. Review the current selection again.')
    ensure_provider()
    ensure_ready()
    job.update(privacy_approval=packet.selection(), status='queued', stage='queued', progress=25,
               message='Reviewed evidence queued for analysis')
    launch(job, for_analysis=True)
    return job


@app.post('/api/jobs/{job_id}/privacy/local')
async def finish_locally(job_id: str):
    job = get_job(job_id)
    if job['status'] != 'review':
        raise HTTPException(409, 'Wait for the local checks to finish.')
    job.update(status='completed', stage='completed', progress=100, message='Local findings ready',
               report=workflow_evidence.local_report(job), local_only=True)
    persist(job)
    return job


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    get_job(job_id)
    async def events():
        queue = asyncio.Queue(maxsize=1)
        subscribers = SUBSCRIBERS.setdefault(job_id, set())
        subscribers.add(queue)
        queue.put_nowait(json.dumps(get_job(job_id), ensure_ascii=False))
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {payload}\n\n"
                if json.loads(payload)["status"] in ("completed", "failed", "review"):
                    break
        finally:
            subscribers.discard(queue)
            if not subscribers:
                SUBSCRIBERS.pop(job_id, None)
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


class RecordingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        if value is not None:
            value = value.strip()
            if not value or any(ord(char) < 32 for char in value):
                raise ValueError("Choose a name without line breaks or control characters.")
        return value


@app.patch("/api/jobs/{job_id}")
async def edit_job(job_id: str, changes: RecordingEdit):
    job = get_job(job_id)
    job.update(changes.model_dump(exclude_none=True))
    persist(job)
    return job


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str):
    job = get_job(job_id)
    if job["status"] in ("queued", "processing"):
        raise HTTPException(409, "Wait for this recording to finish before deleting it.")
    # Only remove the app's stored copy. Original uploads and Desktop files are
    # never referenced here. Keep this operation synchronous to prevent a retry
    # from starting between checking the status and deleting the folder.
    folder = DATA / job_id
    if folder.exists():
        shutil.rmtree(folder)
    del JOBS[job_id]
    return Response(status_code=204)


@app.post("/api/jobs/{job_id}/retry", status_code=202)
async def retry(job_id: str):
    ensure_ready()
    job = get_job(job_id)
    if job["status"] != "failed" and not (job["status"] == "review" and not job.get('redaction_ready')):
        raise HTTPException(409, "This recording is not waiting for a retry.")
    if job.get("privacy_approval"):
        ensure_provider()
        try:
            privacy_review.stored(DATA / job_id, job, get_model())
        except ValueError:
            job.update(status='review', stage='review', progress=25,
                       message='Review the recording again before sending', privacy_approval=None)
            persist(job)
            return job
        job.update(status='queued', stage='queued', progress=25, message='Reviewed evidence queued for retry')
        launch(job, for_analysis=True)
        return job
    job.update(status="queued", stage="queued", progress=0, message="Recording queued for retry",
               started_at=None, analysis_progress=None, model_request=None)
    launch(job)
    return job


@app.get("/api/jobs/{job_id}/video")
async def video(job_id: str):
    get_job(job_id)
    file = DATA / job_id / "preview.mp4"
    if not file.is_file():
        raise HTTPException(404, "Video preview is not ready yet.")
    return FileResponse(file, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/frames/{filename}")
async def frame(job_id: str, filename: str):
    job = get_job(job_id)
    if filename not in {f"{f['id']}.jpg" for f in job.get("frames", [])}:
        raise HTTPException(404, "Screenshot not found.")
    return FileResponse(DATA / job_id / "frames" / filename, media_type="image/jpeg")


@app.get('/api/jobs/{job_id}/redacted/{filename}')
async def redacted_frame(job_id: str, filename: str):
    job = get_job(job_id)
    if not job.get('redaction_ready') or filename not in {f"{frame['id']}.jpg" for frame in job.get('frames', [])}:
        raise HTTPException(404, 'Redacted screenshot not found.')
    try:
        jpeg = privacy_review.read_image(DATA / job_id / 'redacted', filename.removesuffix('.jpg'))
    except (ValueError, OSError):
        raise HTTPException(404, 'Redacted screenshot not found.') from None
    return Response(jpeg, media_type='image/jpeg')


def markdown_report(job):
    r = job["report"]
    lines = [f"# {r['title']}", "", r["summary"], "", f"Recording: {job.get('display_name', job['filename'])}",
             f"Duration: {job['metadata']['duration']} seconds. Model: {r['model']}", "",
             "## Observed workflow", r["workflow"]["description"], r["workflow"]["repetition_assessment"], ""]
    if job.get("notes"):
        lines += ["## Your notes", job["notes"], ""]
    for step in r["timeline"]:
        lines += [f"- {step['start_seconds']:.1f}–{step['end_seconds']:.1f}s: **{step['title']}** ({step['evidence_type']}). {step['description']}"]
    lines += ["", f"Observed workflow instances: {r['workflow']['observed_occurrence_count']}"]
    for occurrence in r["workflow"]["occurrences"]:
        lines += [f"- {occurrence['start_seconds']:.1f}–{occurrence['end_seconds']:.1f}s ({occurrence['observed_span_seconds']:.1f}s elapsed screen span): {occurrence['description']}"]
    if r.get('local_findings'):
        lines += ['', '## Matched on this Mac']
        for finding in r['local_findings']:
            lines += ['', f"### {finding['title']}"]
            lines += [f"- {pair['source']:.1f}s → {pair['result']:.1f}s" for pair in finding['examples']]
    for rec in r["recommendations"]:
        lines += ["", f"## {rec['title']}", rec["summary"], "", "### Proposed automation"]
        lines += [f"- {s}" for s in rec["automated_steps"]]
        lines += ["", "### Human decisions"] + [f"- {s}" for s in rec["human_steps"]]
        lines += ["", "### Integrations"] + [f"- **{i['name']}**: {i['purpose']} Requires: {i['requirement']}" for i in rec["integrations"]]
        lines += ["", "### Implementation"] + [f"{i + 1}. {s}" for i, s in enumerate(rec["implementation_steps"])]
        estimate = rec["savings"]["estimated_seconds_per_run"]
        lines += ["", "### Hypothetical savings", f"{estimate} seconds per run (unverified estimate)." if estimate is not None else "Not enough evidence to estimate savings."]
        lines += [f"- {s}" for s in rec["savings"]["assumptions"] + rec["assumptions"]]
        lines += ["", "Evidence: " + ", ".join(rec["evidence_frame_ids"])]
    lines += ["", "## Limitations"] + [f"- {s}" for s in r["limitations"]]
    return "\n".join(lines)


@app.get("/api/jobs/{job_id}/export/{format}")
async def export(job_id: str, format: str):
    job = get_job(job_id)
    if job["status"] != "completed":
        raise HTTPException(409, "The report is not ready.")
    if format == "json":
        return Response(json.dumps(job, ensure_ascii=False, indent=2), media_type="application/json",
                        headers={"Content-Disposition": 'attachment; filename="chonkytasks-report.json"'})
    if format == "markdown":
        return Response(markdown_report(job), media_type="text/markdown",
                        headers={"Content-Disposition": 'attachment; filename="chonkytasks-report.md"'})
    raise HTTPException(404, "Choose json or markdown.")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")

