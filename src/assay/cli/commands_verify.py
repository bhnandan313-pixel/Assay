"""CLI handler for the `assay verify` command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from assay.attest.bundle import EvidenceGenerator, verify_evidence_bundle
from assay.attest.signer import KeyStore
from assay.cli.commands_record import validate_target_url
from assay.config.settings import load_settings
from assay.diff.comparator import DifferentialComparator
from assay.diff.session_comparator import SessionComparisonRunner
from assay.models.diff import DiffClassification, FieldDelta, SessionDiffReport
from assay.models.evidence import EvidenceBundle
from assay.models.recording import RecordedInteraction, RecordingSession
from assay.replay.runner import ReplayRunner
from assay.storage.session_store import SessionStore

console = Console()

_VERDICT_STYLES: Dict[DiffClassification, str] = {
    DiffClassification.UNCHANGED: "green",
    DiffClassification.CHANGED: "yellow",
    DiffClassification.FAILED: "red",
    DiffClassification.INCONCLUSIVE: "red",
}


def parse_ignore_paths(ignore_path: Optional[str | List[str]]) -> List[str]:
    """Parse repeated or comma-separated ``--ignore-path`` values."""
    if not ignore_path:
        return []
    values = [ignore_path] if isinstance(ignore_path, str) else ignore_path
    return [item.strip() for value in values for item in value.split(",") if item.strip()]


def _format_delta_line(interaction_id: str, delta: FieldDelta) -> str:
    """Format a ``FieldDelta`` for human-readable terminal display."""
    if delta.change_type == "value_changed":
        return (
            f"⚠️  [{interaction_id}] Field '{delta.path}' changed from "
            f"{delta.old_value!r} to {delta.new_value!r}"
        )
    if delta.change_type == "type_mismatch":
        return (
            f"⚠️  [{interaction_id}] Field '{delta.path}' type mismatch: "
            f"{delta.old_value!r} ({type(delta.old_value).__name__}) -> "
            f"{delta.new_value!r} ({type(delta.new_value).__name__})"
        )
    if delta.change_type == "item_added":
        return f"⚠️  [{interaction_id}] Field '{delta.path}' added: {delta.new_value!r}"
    if delta.change_type == "item_removed":
        return f"⚠️  [{interaction_id}] Field '{delta.path}' removed: {delta.old_value!r}"
    return f"⚠️  [{interaction_id}] Field '{delta.path}' ({delta.change_type})"


def _render_verification_results(
    session: RecordingSession,
    diff_report: SessionDiffReport,
    report_path: Path,
) -> None:
    """Render the Rich verification table and detailed delta sub-panel."""
    by_id: Dict[str, RecordedInteraction] = {
        item.interaction_id: item for item in session.interactions
    }

    table = Table(
        title=f"Assay Verification Results ({diff_report.verification_id})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Interaction ID", style="cyan")
    table.add_column("Method", style="white")
    table.add_column("Path", style="white")
    table.add_column("Status Baseline", justify="right")
    table.add_column("Status Candidate", justify="right")
    table.add_column("Verdict", style="bold")

    detail_lines: List[str] = []

    for delta in diff_report.deltas:
        interaction = by_id.get(delta.interaction_id)
        method = interaction.request.method if interaction else "-"
        path = interaction.request.path if interaction else "-"
        old_status = str(delta.old_status) if delta.old_status is not None else "ERR"
        new_status = str(delta.new_status) if delta.new_status is not None else "ERR"
        style = _VERDICT_STYLES.get(delta.classification, "white")

        table.add_row(
            delta.interaction_id[:12],
            method,
            path,
            old_status,
            new_status,
            f"[{style}]{delta.classification.value}[/{style}]",
            style=style if delta.classification != DiffClassification.UNCHANGED else None,
        )

        if delta.classification == DiffClassification.CHANGED:
            if delta.status_code_changed:
                detail_lines.append(
                    f"⚠️  [{delta.interaction_id}] Status changed from "
                    f"{delta.old_status} to {delta.new_status}"
                )
            for field_delta in delta.header_diffs + delta.body_diffs:
                detail_lines.append(_format_delta_line(delta.interaction_id, field_delta))
        elif delta.error_message:
            detail_lines.append(
                f"❌ [{delta.interaction_id}] {delta.classification.value}: {delta.error_message}"
            )

    console.print(table)

    if detail_lines:
        console.print(
            Panel(
                "\n".join(detail_lines),
                title="[bold yellow]Detected Behavioral Differences & Errors[/bold yellow]",
                border_style="yellow",
            )
        )

    console.print(
        f"[bold]Summary:[/bold] {diff_report.unchanged_count} unchanged, "
        f"{diff_report.changed_count} changed, {diff_report.failed_count} failed, "
        f"{diff_report.inconclusive_count} inconclusive"
    )
    console.print(f"[bold green]✔️ Verification report saved to {report_path}[/bold green]")


async def _execute_dual_replay(
    session: RecordingSession,
    baseline_url: str,
    candidate_url: str,
    sessions_dir: Path,
    replays_dir: Path,
    timeout_seconds: float,
    concurrency: int,
    session_store: SessionStore,
):
    """Replay a session against both baseline and candidate targets."""
    baseline_runner = ReplayRunner(
        target_url=baseline_url,
        sessions_dir=sessions_dir,
        replays_dir=replays_dir,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        session_store=session_store,
    )
    candidate_runner = ReplayRunner(
        target_url=candidate_url,
        sessions_dir=sessions_dir,
        replays_dir=replays_dir,
        concurrency=concurrency,
        timeout_seconds=timeout_seconds,
        session_store=session_store,
    )
    baseline_report, _ = await baseline_runner.run(session)
    candidate_report, _ = await candidate_runner.run(session)
    return baseline_report, candidate_report


def run_verify_command(
    recording: str,
    baseline: str,
    candidate: str,
    ignore_path: Optional[str | List[str]] = None,
    concurrency: int = 1,
) -> None:
    """Replay against baseline and candidate, compare responses, and render report.

    Args:
        recording: Path or session ID of the recorded JSON workload.
        baseline: Baseline service URL.
        candidate: Candidate service URL.
        ignore_path: Comma-separated JSONPath exclusion patterns.
        concurrency: Max concurrent requests during replay.
    """
    baseline_url = validate_target_url(baseline)
    candidate_url = validate_target_url(candidate)
    ignore_patterns = parse_ignore_paths(ignore_path)
    settings = load_settings()

    session_store = SessionStore(settings.storage.sessions_dir)
    try:
        session = session_store.load_session(recording)
    except FileNotFoundError as exc:
        console.print(f"[bold red]❌ Recording not found:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[bold red]❌ Failed to read recording:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    assay_root = settings.storage.sessions_dir.parent
    replays_dir = assay_root / "replays"
    verifications_dir = assay_root / "verifications"

    console.print(
        f"[bold cyan]Verifying session[/bold cyan] [bold]{session.session_id}[/bold] "
        f"({len(session.interactions)} interaction(s)): "
        f"[bold]{baseline_url}[/bold] vs [bold]{candidate_url}[/bold]"
    )

    baseline_report, candidate_report = asyncio.run(
        _execute_dual_replay(
            session=session,
            baseline_url=baseline_url,
            candidate_url=candidate_url,
            sessions_dir=settings.storage.sessions_dir,
            replays_dir=replays_dir,
            timeout_seconds=settings.network.timeout_seconds,
            concurrency=concurrency,
            session_store=session_store,
        )
    )

    comparator = DifferentialComparator(ignore_paths=ignore_patterns)
    runner = SessionComparisonRunner(
        comparator=comparator,
        verifications_dir=verifications_dir,
    )
    diff_report, report_path = runner.compare_reports(baseline_report, candidate_report)
    _render_verification_results(session, diff_report, report_path)

    key_store = KeyStore(settings.storage.keys_dir)
    key_store.ensure_keys()
    private_key_pem = key_store.load_private_key()
    public_key_hex = key_store.load_public_key_hex()

    evidence_dir = assay_root / "evidence"
    evidence_gen = EvidenceGenerator(evidence_dir=evidence_dir)
    evidence_gen.create_signed_bundle(
        verification_id=diff_report.verification_id,
        session_diff_report=diff_report,
        private_key_pem=private_key_pem,
        public_key_hex=public_key_hex,
    )
    evidence_path = evidence_dir / f"evidence_{diff_report.verification_id}.json"
    console.print(
        f"[bold green]📜 Evidence bundle successfully generated and signed: "
        f"{evidence_path}[/bold green]"
    )


def run_verify_evidence_command(
    bundle: Path,
    public_key: Optional[str] = None,
) -> None:
    """Load an ``EvidenceBundle`` and verify its hash and Ed25519 signature."""
    if not bundle.is_file():
        console.print(f"[bold red]❌ Evidence bundle not found:[/bold red] {bundle}")
        raise typer.Exit(code=1)

    try:
        loaded_bundle = EvidenceBundle.model_validate_json(
            bundle.read_text(encoding="utf-8")
        )
    except Exception as exc:
        console.print(f"[bold red]❌ Invalid evidence bundle format:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    is_valid, reason = verify_evidence_bundle(loaded_bundle, public_key_hex=public_key)
    if is_valid:
        ts_str = loaded_bundle.manifest.timestamp.isoformat()
        console.print(
            Panel.fit(
                f"[bold green]✅ EVIDENCE VERIFIED:[/bold green] This run occurred on "
                f"[bold]{ts_str}[/bold] and the behavioral outcomes are untampered.",
                border_style="green",
            )
        )
        return

    console.print(
        f"[bold red]❌ EVIDENCE COMPROMISED:[/bold red] "
        f"Cryptographic signature verification failed! ({reason})"
    )
    raise typer.Exit(code=1)
