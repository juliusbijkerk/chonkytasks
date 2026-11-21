from typing import Literal

from pydantic import BaseModel, Field


class Step(BaseModel):
    title: str
    application: str
    description: str
    start_frame_id: str
    end_frame_id: str
    evidence_type: Literal["observed", "inferred"]
    confidence: Literal["high", "medium", "low"]
    uncertainty: str
    evidence_role: Literal["source", "result", "other"] = "other"


class Observations(BaseModel):
    steps: list[Step] = Field(min_length=1, max_length=24)
    limitations: list[str]


class Occurrence(BaseModel):
    start_frame_id: str
    end_frame_id: str
    description: str


class Workflow(BaseModel):
    name: str
    description: str
    occurrences: list[Occurrence]
    repetition_assessment: str


class Integration(BaseModel):
    name: str
    purpose: str
    requirement: str


class Savings(BaseModel):
    estimated_seconds_per_run: int | None = Field(ge=0, le=86400)
    assumptions: list[str]


class EvidencePair(BaseModel):
    source_frame_id: str
    result_frame_id: str


class Recommendation(BaseModel):
    automation_recipe: Literal["meeting_reply_drafts", "unavailable"] = "unavailable"
    title: str
    summary: str
    confidence: Literal["high", "medium", "low"]
    evidence_frame_ids: list[str] = Field(min_length=1)
    evidence_pairs: list[EvidencePair] = Field(default_factory=list, max_length=8)
    automated_steps: list[str] = Field(min_length=1)
    human_steps: list[str]
    integrations: list[Integration]
    implementation_steps: list[str] = Field(min_length=1)
    effort: Literal["small", "medium", "large"]
    savings: Savings
    assumptions: list[str]


class Proposal(BaseModel):
    title: str
    summary: str
    workflow: Workflow
    recommendations: list[Recommendation] = Field(max_length=3)
    limitations: list[str]

