"""Pydantic schemas shared across the API."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    session_id: Optional[str] = None  # the batch's session_id is authoritative
    timestamp_ms: int
    type: str  # navigation | click | input | tab_switch | copy_paste | idle
    app: str = "other"
    detail: dict = Field(default_factory=dict)


class EventBatch(BaseModel):
    session_id: str
    events: list[EventIn]


class SessionStart(BaseModel):
    label: str = "observation"
    duration_min: int = 30


class AutomationProposal(BaseModel):
    title: str = "Automation"
    description: str = ""
    bullets: list[str] = Field(default_factory=list)
    tools_needed: list[str] = Field(default_factory=list)
    difficulty: str = "Easy"
    setup_minutes: int = 10
    estimated_weekly_minutes_saved: float = 0.0
    risk_note: str = ""
    default_level: str = "draft"  # suggest | draft | auto


class Workflow(BaseModel):
    name: str
    apps: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    times_repeated_in_session: int = 1
    avg_minutes_per_repetition: float = 0.0
    total_minutes_in_session: float = 0.0
    estimated_weekly_minutes: float = 0.0
    is_repetitive: bool = True
    confidence: float = 0.5
    evidence: list[str] = Field(default_factory=list)
    automation_proposal: AutomationProposal = Field(default_factory=AutomationProposal)


class AnalysisResult(BaseModel):
    workflows: list[Workflow] = Field(default_factory=list)
    other_ideas: list[dict] = Field(default_factory=list)


class RunRequest(BaseModel):
    mode: str = "dry_run"  # dry_run | draft | auto
    demo_mode: bool = True


class ClassifyResult(BaseModel):
    is_rejection: bool
    company: str = ""
    role: str = ""
    confidence: float = 0.0
    reason: str = ""


class DraftReply(BaseModel):
    to: str
    subject: str
    body: str
    thread_id: Optional[str] = None
    email_id: Optional[str] = None
