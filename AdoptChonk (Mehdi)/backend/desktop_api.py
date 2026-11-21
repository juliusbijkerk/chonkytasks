"""API for the laptop agent (see backend/desktop/)."""
import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config, db

router = APIRouter(prefix="/api/desktop-agent")
_proc: dict = {}     # run_id -> Popen


class RunBody(BaseModel):
    goal: str
    task_id: Optional[str] = None


class AnswerBody(BaseModel):
    approve: bool


class ExcelBody(BaseModel):
    company: str
    role: str = ""
    status: str = "Declined"


def _running() -> Optional[str]:
    for rid, p in list(_proc.items()):
        if p.poll() is None:
            return rid
        _proc.pop(rid, None)
    return None


@router.post("/run")
def run(body: RunBody):
    goal = body.goal.strip()
    if not goal:
        raise HTTPException(400, "tell the agent what to do")
    if not config.has_api_key():
        raise HTTPException(400, "the laptop agent needs MISTRAL_API_KEY")
    busy = _running()
    if busy:
        raise HTTPException(409, f"the laptop agent is already running (run {busy})")
    run_id = db.create_run(body.task_id, "desktop", False)
    _proc[run_id] = subprocess.Popen(
        [sys.executable, "-m", "backend.desktop.runner", "--run-id", run_id, "--goal", goal],
        cwd=str(config.ROOT))
    return {"run_id": run_id}


@router.post("/{run_id}/stop")
def stop(run_id: str):
    if not db.get_run(run_id):
        raise HTTPException(404, "run not found")
    db.add_run_event(run_id, "stop_request", "Stop requested from the dashboard", {})
    return {"ok": True}


@router.post("/{run_id}/answer")
def answer(run_id: str, body: AnswerBody):
    if not db.get_run(run_id):
        raise HTTPException(404, "run not found")
    db.add_run_event(run_id, "user_answer", "approved" if body.approve else "denied", {"approve": body.approve})
    return {"ok": True}


@router.post("/excel-update")
def excel_update(body: ExcelBody):
    """Visibly update one tracker row in the Excel desktop app."""
    from .desktop import excel_desktop
    try:
        return excel_desktop.update_status(body.company, body.role, body.status)
    except Exception as e:
        raise HTTPException(500, f"Excel automation failed: {e}")
