"""Pydantic v2 schemas for replayed HTTP interactions and session reports."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

from assay.models.recording import RecordedRequest, RecordedResponse


class ReplayExecutionPair(BaseModel):
    """Comparison record pairing an original interaction with its replay result."""

    interaction_id: str
    recorded_at: datetime
    request: RecordedRequest
    original_response: RecordedResponse
    replayed_response: Optional[RecordedResponse] = None
    replayed_at: Optional[datetime] = None
    duration_ms: Optional[float] = None
    error_message: Optional[str] = None
    is_success: bool = False


class ReplaySessionReport(BaseModel):
    """Complete report of a replay run against a target service."""

    version: str = "1.0.0"
    replay_id: str
    session_id: str
    target_url: str
    started_at: datetime
    completed_at: datetime
    execution_pairs: List[ReplayExecutionPair] = Field(default_factory=list)
