"""Pydantic v2 schemas for cryptographically signed verification evidence."""

from __future__ import annotations

from datetime import datetime
from typing import List

from pydantic import BaseModel

from assay.models.diff import InteractionDiff, VerificationSummary


class EvidenceManifest(BaseModel):
    """Canonicalizable manifest summarizing a verification run and its hash."""

    manifest_version: str = "1.0.0"
    verification_id: str
    timestamp: datetime
    baseline_target: str
    candidate_target: str
    session_id: str
    summary: VerificationSummary
    deltas_hash_sha256: str


class EvidenceBundle(BaseModel):
    """Signed, tamper-evident verification bundle containing manifest and deltas."""

    manifest: EvidenceManifest
    deltas: List[InteractionDiff]
    signature_ed25519: str
    public_key_hex: str
