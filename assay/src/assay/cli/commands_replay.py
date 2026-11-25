"""CLI handler for the `assay replay` command."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List, Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table

from assay.cli.commands_record import validate_target_url
from assay.config.settings import load_settings
from assay.models.replay import ReplayExecutionPair, ReplaySessionReport
from assay.replay.runner import ReplayRunner
from assay.storage.session_store import SessionStore

console = Console()


def parse_header_overrides(headers: Optional[List[str]]) -> Dict[str, str]:
    """Parse ``--header "Name: Value"`` CLI options into a dictionary.

    Args:
        headers: List of raw header override strings.

    Returns:
        Mapping of header names to override values.

    Raises:
        typer.BadParameter: If any header string lacks a valid ``Name: Value`` format.
    """
    parsed: Dict[str, str] = {}
    for raw in headers or []:
        name, sep, value = raw.partition(":")
        clean_name = name.strip()
        if not sep or not clean_name:
            raise typer.BadParameter(
                f"Invalid header override '{raw}'. Expected 'Header-Name: Value'."
            )
        parsed[clean_name] = value.strip()
    return parsed


def _render_replay_summary(report: ReplaySessionReport, report_path: Path) -> None:
    """Render a Rich summary table of replay execution results."""
    total = len(report.execution_pairs)
    succeeded = sum(1 for pair in report.execution_pairs if pair.is_success)
    failed = total - succeeded

    table = Table(
        title=f"Replay Summary ({report.replay_id})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Source Session", report.session_id)
    table.add_row("Target URL", report.target_url)
    table.add_row("Total Requests", str(total))
    table.add_row("Succeeded", f"[green]{succeeded}[/green]")
    table.add_row("Connection Failures", f"[red]{failed}[/red]" if failed else "0")
    table.add_row("Saved Report", str(report_path))

    console.print(table)
    if failed == 0:
        console.print("[bold green]✔️ Replay completed with 0 connection errors.[/bold green]")
    else:
        console.print(
            f"[bold yellow]⚠️ Replay completed with {failed} connection failure(s).[/bold yellow]"
        )


def run_replay_command(
    recording: str,
    target: str,
    concurrency: int = 1,
    headers: Optional[List[str]] = None,
) -> None:
    """Validate inputs, execute the replay runner with progress UI, and print summary.

    Args:
        recording: Path to a session JSON file or a session ID.
        target: Target service base URL.
        concurrency: Max concurrent requests (1 = sequential).
        headers: Optional list of ``"Name: Value"`` header overrides.
    """
    validated_target = validate_target_url(target)
    header_overrides = parse_header_overrides(headers)
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

    replays_dir = settings.storage.sessions_dir.parent / "replays"
    total_calls = len(session.interactions)

    console.print(
        f"[bold cyan]Replaying session[/bold cyan] [bold]{session.session_id}[/bold] "
        f"({total_calls} interaction(s)) against [bold]{validated_target}[/bold] "
        f"[dim](concurrency={concurrency})[/dim]"
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("[Replaying...]", total=total_calls)

        def _on_progress(_pair: ReplayExecutionPair, completed: int, _total: int) -> None:
            progress.update(
                task_id,
                completed=completed,
                description=f"[Replaying...] {completed}/{total_calls} calls executed",
            )

        runner = ReplayRunner(
            target_url=validated_target,
            sessions_dir=settings.storage.sessions_dir,
            replays_dir=replays_dir,
            concurrency=concurrency,
            timeout_seconds=settings.network.timeout_seconds,
            header_overrides=header_overrides,
            session_store=session_store,
            on_progress=_on_progress,
        )
        report, saved_path = asyncio.run(runner.run(session))

    _render_replay_summary(report, saved_path)
