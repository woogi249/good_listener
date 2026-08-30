"""Typed OpenAI outputs and public API request contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class MeetingCreate(BaseModel):
    topic: str = Field(default="", max_length=300)
    goal: str = Field(default="", max_length=500)
    terms: list[str] = Field(default_factory=list, max_length=100)
    consent_external_processing: bool = False


class TranscriptFinal(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    previous_item_id: str | None = Field(default=None, max_length=200)
    text: str = Field(min_length=1, max_length=20_000)
    speaker: str = Field(default="unknown", max_length=100)
    started_at: str | None = None
    ended_at: str | None = None


class MinutesUpdate(BaseModel):
    structured: dict
    markdown: str | None = Field(default=None, max_length=2_000_000)


class SourceRef(BaseModel):
    title: str = ""
    url: str
    publisher: str = ""


class ProgressItem(BaseModel):
    id: str = Field(description="Stable short identifier from the supplied existing state or a new slug")
    label: str
    status: Literal["pending", "discussing", "decided", "blocked"]
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class DecisionItem(BaseModel):
    id: str
    text: str
    status: Literal["candidate", "confirmed", "rejected"] = "candidate"
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class ActionItem(BaseModel):
    id: str
    text: str
    assignee: str | None = None
    due_at: str | None = None
    status: Literal["candidate", "confirmed", "completed"] = "candidate"
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class OpenQuestion(BaseModel):
    id: str
    text: str
    status: Literal["open", "resolved"] = "open"
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class ProgressAnalysis(BaseModel):
    current_topic: str = ""
    current_topic_evidence_ids: list[str] = Field(default_factory=list)
    progress: list[ProgressItem] = Field(default_factory=list)
    decisions: list[DecisionItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)


class FactVerification(BaseModel):
    status: Literal["supported", "contradicted", "insufficient"]
    verdict: str
    sources: list[SourceRef] = Field(default_factory=list)


class MinutesAgendaItem(BaseModel):
    topic: str
    outcome: str = ""
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class MinutesFact(BaseModel):
    claim_id: str
    claim: str
    status: Literal[
        "supported",
        "contradicted",
        "insufficient",
        "internal_source_required",
        "failed",
    ]
    verdict: str = ""
    sources: list[SourceRef] = Field(default_factory=list)
    evidence_utterance_ids: list[str] = Field(default_factory=list)


class MinutesDraft(BaseModel):
    title: str
    summary: str
    agenda: list[MinutesAgendaItem] = Field(default_factory=list)
    decisions: list[DecisionItem] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    facts: list[MinutesFact] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
