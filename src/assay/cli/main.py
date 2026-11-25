"""Assay CLI entry point and command definitions."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from assay.attest.signer import KeyStore
from assay.cli.commands_record import run_record_command
from assay.cli.commands_replay import run_replay_command
from assay.cli.commands_verify import run_verify_command, run_verify_evidence_command
from assay.config.settings import (
    DEFAULT_CONFIG_FILENAME,
    AssaySettings,
    load_settings,
    write_default_config,
)
from assay.storage.session_store import SessionStore
from assay.cli.dashboard import start_dashboard

app = typer.Typer(
    name="assay",
    help="Assay — The Verification Runtime for Agent-Written Code.",
    no_args_is_help=True,
)
recordings_app = typer.Typer(
    name="recordings",
    help="Inspect and manage recorded workload sessions.",
    no_args_is_help=True,
)
app.add_typer(recordings_app, name="recordings")

console = Console()


def configure_logging(verbose: bool = False, log_dir: Optional[Path] = None) -> None:
    """Configure file logging under ``.assay/logs/assay.log`` and optional console output."""
    root_logger = logging.getLogger("assay")
    root_logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    root_logger.handlers.clear()
    root_logger.propagate = False

    target_dir = log_dir or Path.cwd() / ".assay" / "logs"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(target_dir / "assay.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
        )
        root_logger.addHandler(file_handler)
    except OSError:
        pass

    if verbose:
        rich_handler = RichHandler(
            console=console,
            show_time=True,
            show_path=False,
            rich_tracebacks=True,
        )
        rich_handler.setLevel(logging.DEBUG)
        root_logger.addHandler(rich_handler)


@app.callback()
def main_callback(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Enable detailed debug traces in the terminal.",
    ),
    debug: bool = typer.Option(
        False,
        "--debug",
        help="Alias for --verbose; emits full debug diagnostics.",
    ),
) -> None:
    """Configure Assay logging before executing a subcommand."""
    configure_logging(verbose=verbose or debug)


def initialize_workspace(
    base_dir: Path | None = None,
    force: bool = False,
) -> tuple[Path, Path, Path]:
    """Initialize the local ``.assay`` directory, YAML config, and signing keys.

    Args:
        base_dir: Project root directory. Defaults to the current working directory.
        force: Overwrite existing configuration and key-pair if present.

    Returns:
        Tuple of ``(config_path, private_key_path, public_key_path)``.
    """
    root = base_dir or Path.cwd()
    assay_dir = root / ".assay"
    assay_dir.mkdir(parents=True, exist_ok=True)

    config_path = root / DEFAULT_CONFIG_FILENAME
    settings = AssaySettings()
    settings.storage.sessions_dir = assay_dir / "sessions"
    settings.storage.keys_dir = assay_dir / "keys"
    settings.storage.sessions_dir.mkdir(parents=True, exist_ok=True)

    if force or not config_path.exists():
        write_default_config(config_path=config_path, settings=settings)

    key_store = KeyStore(settings.storage.keys_dir)
    created_new, private_key_path, public_key_path = key_store.ensure_keys(force=force)
    if created_new:
        console.print(
            f"[bold cyan]🔑 Generated cryptographic keypair at "
            f"{settings.storage.keys_dir}[/bold cyan]"
        )
    return config_path, private_key_path, public_key_path


@app.command()
def init(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing configuration and Ed25519 keys.",
    ),
) -> None:
    """Initialize local .assay folder, configuration file, and Ed25519 key-pair."""
    console.print("[bold cyan]Initializing Assay workspace...[/bold cyan]")
    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task(description="Creating directories, config, and keys...", total=None)
            config_path, private_key_path, public_key_path = initialize_workspace(force=force)
    except OSError as exc:
        console.print(f"[bold red]❌ Initialization failed:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    table = Table(title="Assay Initialization Summary", show_header=True, header_style="bold")
    table.add_column("Artifact", style="cyan")
    table.add_column("Location", style="green")
    table.add_row("Configuration", str(config_path))
    table.add_row("Private Key (Ed25519)", str(private_key_path))
    table.add_row("Public Key (Ed25519)", str(public_key_path))

    console.print(table)
    console.print("[bold green]✔️ Assay workspace initialized successfully.[/bold green]")


@app.command()
def record(
    target: str = typer.Option(
        ...,
        "--target",
        "-t",
        help="Upstream target service URL to proxy and record.",
    ),
    port: Optional[int] = typer.Option(
        None,
        "--port",
        "-p",
        help="Local port for the recording reverse-proxy server.",
    ),
    session: Optional[str] = typer.Option(
        None,
        "--session",
        "-s",
        help="Optional session identifier for the saved recording.",
    ),
    ui: Optional[str] = typer.Option(
        None,
        "--ui",
        help="Optional frontend URL to launch via Playwright for UI capture.",
    ),
    browser: str = typer.Option(
        "chromium",
        "--browser",
        help="Playwright browser engine (chromium, firefox, or webkit).",
    ),
) -> None:
    """Start the recording reverse-proxy in front of a target service."""
    run_record_command(
        target=target,
        port=port,
        session_id=session,
        ui_url=ui,
        browser=browser,
    )


@recordings_app.command("list")
def list_recordings() -> None:
    """List recorded local JSON workload sessions."""
    settings = load_settings()
    sessions_dir = settings.storage.sessions_dir

    if not sessions_dir.exists():
        console.print(
            f"[yellow]No sessions directory found at {sessions_dir}. "
            "Run `assay init` first.[/yellow]"
        )
        return

    store = SessionStore(sessions_dir)
    session_files = store.list_sessions()
    if not session_files:
        console.print(f"[yellow]No recorded sessions found in {sessions_dir}.[/yellow]")
        return

    table = Table(title="Recorded Workload Sessions", show_header=True, header_style="bold")
    table.add_column("Session File", style="cyan")
    table.add_column("Size (bytes)", justify="right", style="green")
    for session_file in session_files:
        table.add_row(session_file.name, str(session_file.stat().st_size))

    console.print(table)
    console.print(f"[bold green]✔️ Found {len(session_files)} recording(s).[/bold green]")


@app.command()
def replay(
    recording: str = typer.Option(
        ...,
        "--recording",
        "-r",
        help="Path or session ID of the recorded JSON workload session.",
    ),
    target: str = typer.Option(
        ...,
        "--target",
        "-t",
        help="Target service URL to replay the recorded workload against.",
    ),
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        "-c",
        min=1,
        help="Maximum concurrent replay requests (1 = sequential).",
    ),
    header: Optional[List[str]] = typer.Option(
        None,
        "--header",
        "-H",
        help="Override request header in 'Name: Value' format (repeatable).",
    ),
    ui_target: Optional[str] = typer.Option(
        None,
        "--ui-target",
        help="Optional candidate frontend URL for headless Playwright UI playback.",
    ),
    max_retries: Optional[int] = typer.Option(
        None,
        "--max-retries",
        min=0,
        help="Override maximum retry attempts for transient HTTP errors.",
    ),
) -> None:
    """Execute a recorded workload session against a target endpoint."""
    run_replay_command(
        recording=recording,
        target=target,
        concurrency=concurrency,
        headers=header,
        ui_target=ui_target,
        max_retries=max_retries,
    )


@app.command()
def verify(
    recording: str = typer.Option(
        ...,
        "--recording",
        "-r",
        help="Path or session ID of the recorded JSON workload session.",
    ),
    baseline: str = typer.Option(
        ...,
        "--baseline",
        "-b",
        help="Baseline service URL for reference behavior.",
    ),
    candidate: str = typer.Option(
        ...,
        "--candidate",
        "-c",
        help="Candidate service URL to verify against the baseline.",
    ),
    ignore_path: Optional[List[str]] = typer.Option(
        None,
        "--ignore-path",
        "-i",
        help="JSONPath field to ignore; repeat for multiple fields.",
    ),
    concurrency: int = typer.Option(
        1,
        "--concurrency",
        min=1,
        help="Maximum concurrent replay requests (1 = sequential).",
    ),
) -> None:
    """Run replay, diff comparison, and attestation between baseline and candidate."""
    run_verify_command(
        recording=recording,
        baseline=baseline,
        candidate=candidate,
        ignore_path=ignore_path,
        concurrency=concurrency,
    )


@app.command("verify-evidence")
def verify_evidence(
    bundle: Path = typer.Option(
        ...,
        "--bundle",
        "-b",
        help="Path to the signed EvidenceBundle JSON file.",
    ),
    public_key: Optional[str] = typer.Option(
        None,
        "--public-key",
        "-k",
        help="Optional hex-encoded Ed25519 public key overriding the bundle key.",
    ),
) -> None:
    """Verify the cryptographic integrity of a signed evidence bundle."""
    run_verify_evidence_command(bundle=bundle, public_key=public_key)


from assay.cli.dashboard import start_dashboard


@app.command()
def dashboard(
    port: int = typer.Option(
        9100,
        "--port",
        "-p",
        help="Local port for the dashboard web UI.",
    ),
) -> None:
    """Launch the interactive Assay local dashboard.

    The dashboard runs a FastAPI server in a background thread and opens the
    default web browser automatically. Press Ctrl+C in this terminal to stop.
    """
    # Render a friendly Rich panel before starting.
    panel_text = (
        "╭────────────────────────── Assay Local Dashboard ───────────────────────────╮\n"
        f"│ ✔️ Dashboard Live:   http://localhost:{port}                                 │\n"
        "│ 🔑 Encryption Mode:  Local Keypair Verified                               │\n"
        "│                                                                            │\n"
        "│ ➜ Real-time traffic, visual diffing, and key signing are now active.       │\n"
        "│   Press Ctrl+C in this terminal window to stop the dashboard server.       │\n"
        "╰────────────────────────────────────────────────────────────────────────────╯"
    )
    console.print(panel_text)
    start_dashboard(port=port)

if __name__ == "__main__":
    app()
