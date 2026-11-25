"""Pydantic v2 schemas for recorded HTTP interactions and sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


def decode_body_bytes(
    raw_bytes: bytes,
    content_type: Optional[str] = None,
) -> Optional[str]:
    """Decode raw HTTP body bytes into a UTF-8 string or hex-encoded fallback.

    Args:
        raw_bytes: Raw payload bytes from an HTTP request or response.
        content_type: Optional ``Content-Type`` header value for charset hints.

    Returns:
        ``None`` if ``raw_bytes`` is empty; otherwise a decoded string or a
        ``<binary:hex:...>`` representation for non-UTF-8/binary payloads.
    """
    if not raw_bytes:
        return None

    charset = "utf-8"
    if content_type and "charset=" in content_type.lower():
        for part in content_type.split(";")[1:]:
            key, _, val = part.strip().partition("=")
            if key.lower() == "charset" and val:
                charset = val.strip().strip("\"'")
                break

    try:
        return raw_bytes.decode(charset)
    except (UnicodeDecodeError, LookupError):
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary:hex:{raw_bytes.hex()}>"


class HTTPHeader(BaseModel):
    """Single HTTP header name-value pair."""

    name: str
    value: str


class BrowserAction(BaseModel):
    """Captured frontend browser UI interaction event."""

    action_type: str
    selector: str
    value: Optional[str] = None
    timestamp_offset_ms: float = Field(ge=0.0)
    trace_id: Optional[str] = None


class RecordedRequest(BaseModel):
    """Captured inbound HTTP request metadata and payload."""

    method: str
    url: str
    path: str
    query_params: Dict[str, str] = Field(default_factory=dict)
    headers: List[HTTPHeader] = Field(default_factory=list)
    body: Optional[str] = None
    content_type: Optional[str] = None
    trace_id: Optional[str] = None


class RecordedResponse(BaseModel):
    """Captured upstream HTTP response metadata, payload, and timing."""

    status_code: int
    headers: List[HTTPHeader] = Field(default_factory=list)
    body: Optional[str] = None
    content_type: Optional[str] = None
    elapsed_ms: float = Field(ge=0.0)


class RecordedInteraction(BaseModel):
    """Single request/response pair captured by the Assay proxy."""

    interaction_id: str
    recorded_at: datetime
    request: RecordedRequest
    response: RecordedResponse
    trace_id: Optional[str] = None


class RecordingSession(BaseModel):
    """Collection of recorded browser actions and HTTP interactions for a target."""

    version: str = "2.0.0"
    session_id: str
    target_url: str
    created_at: datetime
    ui_actions: List[BrowserAction] = Field(default_factory=list)
    interactions: List[RecordedInteraction] = Field(default_factory=list)
