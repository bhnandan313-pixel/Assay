"""Evidence bundle packaging, deterministic hashing, and verification."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from assay.attest.signer import canonicalize_json, sign_manifest, verify_manifest
from assay.models.diff import InteractionDiff, SessionDiffReport, VerificationSummary
from assay.models.evidence import EvidenceBundle, EvidenceManifest


def compute_deltas_hash(deltas: List[InteractionDiff]) -> str:
    """Compute a deterministic SHA-256 hex digest over all interaction deltas.

    Deltas are sorted by ``interaction_id`` and serialized to canonical JSON
    before hashing so ordering or whitespace variations cannot alter the digest.
    """
    sorted_deltas = sorted(deltas, key=lambda item: item.interaction_id)
    serialized = [item.model_dump(mode="json") for item in sorted_deltas]
    canonical_bytes = canonicalize_json(serialized)
    return hashlib.sha256(canonical_bytes).hexdigest()


def verify_evidence_bundle(
    bundle: EvidenceBundle,
    public_key_hex: Optional[str] = None,
) -> Tuple[bool, str]:
    """Verify both delta integrity (SHA-256) and manifest signature (Ed25519).

    Args:
        bundle: The ``EvidenceBundle`` to validate.
        public_key_hex: Optional hex-encoded public key overriding the embedded key.

    Returns:
        Tuple of ``(is_valid, reason_message)``.
    """
    expected_hash = compute_deltas_hash(bundle.deltas)
    if not hmac.compare_digest(expected_hash, bundle.manifest.deltas_hash_sha256):
        return (
            False,
            "Delta payload hash mismatch: interaction deltas have been modified.",
        )

    key_hex = public_key_hex if public_key_hex is not None else bundle.public_key_hex
    if not verify_manifest(key_hex, bundle.signature_ed25519, bundle.manifest):
        return (
            False,
            "Cryptographic signature verification failed!",
        )

    return True, "Evidence verified."


class EvidenceGenerator:
    """Packages diff reports into signed, tamper-evident ``EvidenceBundle`` files."""

    def __init__(self, evidence_dir: Path | str = Path("./.assay/evidence")) -> None:
        """Initialize the generator with the target evidence output directory."""
        self.evidence_dir = Path(evidence_dir)

    def _save_bundle(self, bundle: EvidenceBundle) -> Path:
        """Atomically write ``evidence_<verification_id>.json`` to ``evidence_dir``."""
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        target_path = (
            self.evidence_dir / f"evidence_{bundle.manifest.verification_id}.json"
        )
        payload = bundle.model_dump_json(indent=2)

        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp_evidence_",
            suffix=".json",
            dir=str(self.evidence_dir),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, target_path)
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

        return target_path

    def create_signed_bundle(
        self,
        verification_id: str,
        session_diff_report: SessionDiffReport,
        private_key_pem: bytes,
        public_key_hex: str,
    ) -> EvidenceBundle:
        """Create, sign, and persist an ``EvidenceBundle`` for a diff report.

        Args:
            verification_id: Unique verification run identifier.
            session_diff_report: Completed ``SessionDiffReport``.
            private_key_pem: Ed25519 private key in PEM format.
            public_key_hex: Corresponding hex-encoded Ed25519 public key.

        Returns:
            Signed ``EvidenceBundle`` instance.
        """
        deltas_hash = compute_deltas_hash(session_diff_report.deltas)
        summary = VerificationSummary(
            total=session_diff_report.total_interactions,
            unchanged=session_diff_report.unchanged_count,
            changed=session_diff_report.changed_count,
            failed=session_diff_report.failed_count,
            inconclusive=session_diff_report.inconclusive_count,
        )
        manifest = EvidenceManifest(
            verification_id=verification_id,
            timestamp=datetime.now(timezone.utc),
            baseline_target=session_diff_report.baseline_target,
            candidate_target=session_diff_report.candidate_target,
            session_id=session_diff_report.session_id,
            summary=summary,
            deltas_hash_sha256=deltas_hash,
        )
        signature_hex = sign_manifest(private_key_pem, manifest)
        bundle = EvidenceBundle(
            manifest=manifest,
            deltas=list(session_diff_report.deltas),
            signature_ed25519=signature_hex,
            public_key_hex=public_key_hex,
        )
        self._save_bundle(bundle)
        return bundle
