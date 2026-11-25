"""Unit and security tests for Ed25519 signing and evidence bundle verification."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from typer.testing import CliRunner

from assay.attest.bundle import (
    EvidenceGenerator,
    compute_deltas_hash,
    verify_evidence_bundle,
)
from assay.attest.signer import (
    KeyStore,
    canonicalize_json,
    generate_key_pair,
    verify_manifest,
)
from assay.cli.main import app
from assay.models.diff import (
    DiffClassification,
    FieldDelta,
    InteractionDiff,
    SessionDiffReport,
)

runner = CliRunner()


def _sample_diff_report() -> SessionDiffReport:
    """Create a deterministic ``SessionDiffReport`` fixture."""
    return SessionDiffReport(
        verification_id="ver-001",
        session_id="sess-001",
        baseline_target="http://127.0.0.1:8001",
        candidate_target="http://127.0.0.1:8002",
        created_at="2026-11-25T11:40:00+00:00",
        total_interactions=2,
        unchanged_count=1,
        changed_count=1,
        failed_count=0,
        inconclusive_count=0,
        deltas=[
            InteractionDiff(
                interaction_id="int-1",
                classification=DiffClassification.UNCHANGED,
                status_code_changed=False,
                old_status=200,
                new_status=200,
                latency_delta_ms=1.2,
            ),
            InteractionDiff(
                interaction_id="int-2",
                classification=DiffClassification.CHANGED,
                status_code_changed=False,
                old_status=200,
                new_status=200,
                body_diffs=[
                    FieldDelta(
                        path="$.price",
                        change_type="value_changed",
                        old_value=99.99,
                        new_value=89.99,
                    )
                ],
                latency_delta_ms=-2.5,
            ),
        ],
    )


def test_key_generation_and_keystore_roundtrip(tmp_path: Path) -> None:
    """KeyStore should generate, save, and reload Ed25519 keys without corruption."""
    private_pem, public_raw = generate_key_pair()
    assert b"BEGIN PRIVATE KEY" in private_pem
    assert len(public_raw) == 32

    store = KeyStore(tmp_path / "keys")
    priv_path, pub_path = store.save_keys(private_pem, public_raw)
    assert priv_path.exists()
    assert pub_path.exists()

    assert store.load_private_key() == private_pem
    assert store.load_public_key_hex() == public_raw.hex()


def test_canonical_json_invariance() -> None:
    """Canonical JSON bytes must be identical regardless of dict insertion order."""
    obj_a = {"b": 2, "a": 1, "nested": {"z": True, "m": [1, 2, 3]}}
    obj_b = {"nested": {"m": [1, 2, 3], "z": True}, "a": 1, "b": 2}

    assert canonicalize_json(obj_a) == canonicalize_json(obj_b)
    assert canonicalize_json(obj_a) == b'{"a":1,"b":2,"nested":{"m":[1,2,3],"z":true}}'


def test_signed_bundle_happy_path_and_cli_verification(tmp_path: Path) -> None:
    """Signed EvidenceBundle should pass both programmatic and CLI verification."""
    private_pem, public_raw = generate_key_pair()
    generator = EvidenceGenerator(evidence_dir=tmp_path / "evidence")
    diff_report = _sample_diff_report()

    bundle = generator.create_signed_bundle(
        verification_id="ver-001",
        session_diff_report=diff_report,
        private_key_pem=private_pem,
        public_key_hex=public_raw.hex(),
    )

    bundle_path = tmp_path / "evidence" / "evidence_ver-001.json"
    assert bundle_path.exists()

    is_valid, reason = verify_evidence_bundle(bundle)
    assert is_valid is True
    assert "verified" in reason.lower()

    cli_res = runner.invoke(app, ["verify-evidence", "--bundle", str(bundle_path)])
    assert cli_res.exit_code == 0
    assert "EVIDENCE VERIFIED" in cli_res.stdout


def test_tamper_detection_catches_manifest_deltas_and_timestamp_mutations(
    tmp_path: Path,
) -> None:
    """Any mutation to manifest summary, deltas, timestamp, or key must fail."""
    private_pem, public_raw = generate_key_pair()
    generator = EvidenceGenerator(evidence_dir=tmp_path / "evidence")
    bundle = generator.create_signed_bundle(
        verification_id="ver-tamper",
        session_diff_report=_sample_diff_report(),
        private_key_pem=private_pem,
        public_key_hex=public_raw.hex(),
    )

    # 1. Mutate manifest summary count (e.g. unchanged: 1 -> 11)
    tampered_summary = bundle.model_copy(deep=True)
    tampered_summary.manifest.summary.unchanged = 11
    ok_summary, _ = verify_evidence_bundle(tampered_summary)
    assert ok_summary is False
    assert (
        verify_manifest(
            bundle.public_key_hex,
            bundle.signature_ed25519,
            tampered_summary.manifest,
        )
        is False
    )

    # 2. Mutate a FieldDelta inside bundle.deltas -> delta hash check fails
    tampered_deltas = bundle.model_copy(deep=True)
    tampered_deltas.deltas[1].body_diffs[0].new_value = 99.99
    assert (
        compute_deltas_hash(tampered_deltas.deltas)
        != tampered_deltas.manifest.deltas_hash_sha256
    )
    ok_deltas, reason_deltas = verify_evidence_bundle(tampered_deltas)
    assert ok_deltas is False
    assert "hash mismatch" in reason_deltas.lower()

    # 3. Alter timestamp by 1 second -> signature verification fails
    tampered_time = bundle.model_copy(deep=True)
    tampered_time.manifest.timestamp = tampered_time.manifest.timestamp + timedelta(
        seconds=1
    )
    ok_time, _ = verify_evidence_bundle(tampered_time)
    assert ok_time is False

    # 4. Wrong public key -> signature verification fails
    _, other_public_raw = generate_key_pair()
    ok_key, _ = verify_evidence_bundle(bundle, public_key_hex=other_public_raw.hex())
    assert ok_key is False

    # 5. CLI verify-evidence returns exit code 1 on tampered file
    tampered_path = tmp_path / "evidence" / "tampered.json"
    tampered_path.write_text(tampered_summary.model_dump_json(indent=2), encoding="utf-8")
    cli_res = runner.invoke(app, ["verify-evidence", "--bundle", str(tampered_path)])
    assert cli_res.exit_code == 1
    assert "EVIDENCE COMPROMISED" in cli_res.stdout
