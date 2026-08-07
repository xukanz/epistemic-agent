"""Domain-agnostic ReviewItem / Decision contract.

Any upstream system (vocabulary grounding, KG ingest, canvas synthesis) can
emit ReviewItem records. The source system then applies logged Decisions in
its own way.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ReviewPriority(BaseModel):
    score: float = Field(ge=0.0, le=1.0, description="Urgency/importance score; 1 = most urgent.")
    strategy: str = Field(description="Ranking strategy used to compute the score.")
    reasons: list[str] = Field(default_factory=list)


class ReviewPrompt(BaseModel):
    title: str
    question: str
    context: list[str] = Field(default_factory=list)


class ReviewOption(BaseModel):
    id: str
    label: str
    description: str = ""


class ReviewAttachment(BaseModel):
    kind: str = Field(description="E.g. 'candidate_table', 'graph_diff', 'raw_json'.")
    path: str = Field(description="Relative path to attachment file.")


class ReviewStatus(str, Enum):
    pending = "pending"
    decided = "decided"
    skipped = "skipped"
    deferred = "deferred"
    needs_context = "needs_context"


class ReviewItem(BaseModel):
    """A single reviewable case emitted by any upstream system."""

    id: str = Field(description="Unique review item ID, e.g. 'review-000123'.")
    source_project: str = Field(description="Which project emitted this item.")
    source_type: str = Field(
        description="Category of review, e.g. 'ontology_grounding', 'kg_merge', 'canvas_diff'."
    )
    source_ref: dict[str, Any] = Field(
        default_factory=dict,
        description="Project-specific reference (ontology query, node IDs, canvas path, etc.).",
    )
    priority: ReviewPriority
    prompt: ReviewPrompt
    options: list[ReviewOption] = Field(default_factory=list)
    suggested_option: str | None = None
    suggested_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    attachments: list[ReviewAttachment] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: ReviewStatus = ReviewStatus.pending
    created_at: datetime = Field(default_factory=datetime.now)

    def dedupe_key(self) -> str:
        """Identity of the *question*, independent of when it was asked.

        `id` is a fresh UUID per emission, so without this a re-ingest asks the
        reviewer the same thing again — the queue doubled from 40 to 80 items
        the first time a payload was re-run.
        """
        import json as _json

        ref = _json.dumps(self.source_ref, sort_keys=True, ensure_ascii=False, default=str)
        return f"{self.source_project}|{self.source_type}|{ref}"


class Decision(BaseModel):
    """A human reviewer's response to a ReviewItem."""

    review_item_id: str
    decision: str = Field(
        description="The chosen option ID, or 'skip' / 'defer' / 'needs_context'."
    )
    reviewer: str = ""
    timestamp: datetime = Field(default_factory=datetime.now)
    comment: str = ""
    confidence: str = Field(
        default="medium",
        description="Reviewer's own confidence: 'high', 'medium', 'low'.",
    )
