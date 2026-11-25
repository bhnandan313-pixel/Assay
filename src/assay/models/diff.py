"""Pydantic v2 schemas for behavioral differential analysis reports."""

from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class DiffClassification(str, Enum):
    """Outcome classification for a single baseline-vs-candidate interaction."""

    UNCHANGED = "UNCHANGED"
    CHANGED = "CHANGED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class FieldDelta(BaseModel):
    """Single structural or header difference between baseline and candidate."""

    path: str
    change_type: str
    old_value: Optional[Any] = None
    new_value: Optional[Any] = None


class UIDelta(BaseModel):
    """Single DOM or visual regression detected between baseline and candidate UI."""

    selector: str
    change_type: str
    expected_state: Optional[str] = None
    actual_state: Optional[str] = None


class InteractionDiff(BaseModel):
    """Differential comparison result for one recorded interaction."""

    interaction_id: str
    classification: DiffClassification
    status_code_changed: bool
    old_status: Optional[int] = None
    new_status: Optional[int] = None
    body_diffs: List[FieldDelta] = Field(default_factory=list)
    header_diffs: List[FieldDelta] = Field(default_factory=list)
    ui_diffs: List[UIDelta] = Field(default_factory=list)
    latency_delta_ms: float = 0.0
    error_message: Optional[str] = None


class VerificationSummary(BaseModel):
    """Summary counts of verification outcomes."""

    total: int = 0
    unchanged: int = 0
    changed: int = 0
    failed: int = 0
    inconclusive: int = 0


class SessionDiffReport(BaseModel):
    """Aggregated differential report across an entire verification session."""

    version: str = "1.0.0"
    verification_id: str
    session_id: str
    baseline_target: str
    candidate_target: str
    created_at: str
    total_interactions: int
    unchanged_count: int
    changed_count: int
    failed_count: int
    inconclusive_count: int
    ui_unchanged_count: int = 0
    ui_changed_count: int = 0
    ui_failed_count: int = 0
    deltas: List[InteractionDiff] = Field(default_factory=list)
