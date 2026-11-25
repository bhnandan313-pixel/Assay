"""Assay CLI entry point and command definitions."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from assay.cli.commands_record import run_record_command
from assay.cli.commands_replay import run_replay_command
from assay.config.settings import (
    DEFAULT_CONFIG_FILENAME,
    AssaySettings,
    load_settings,
    write_default_config,
)
from assay.storage.session_store import SessionStore

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


def generate_ed25519_keypair(keys_dir: Path, force: bool = False) -> tuple[Path, Path]:
    """Generate an Ed25519 signing key-pair in PEM format.

    Args:
        keys_dir: Directory where ``private_key.pem`` and ``public_key.pem`` are saved.
        force: Overwrite existing key files if present.

    Returns:
        Tuple of ``(private_key_path, public_key_path)``.
    """
    keys_dir.mkdir(parents=True, exist_ok=True)
    private_key_path = keys_dir / "private_key.pem"
    public_key_path = keys_dir / "public_key.pem"

    if not force and (private_key_path.exists() or public_key_path.exists()):
        return private_key_path, public_key_path

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    private_key_path.write_bytes(private_pem)
    public_key_path.write_bytes(public_pem)
    return private_key_path, public_key_path


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

    private_key_path, public_key_path = generate_ed25519_keypair(
        keys_dir=settings.storage.keys_dir,
        force=force,
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
) -> None:
    """Start the recording reverse-proxy in front of a target service."""
    run_record_command(target=target, port=port, session_id=session)


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
) -> None:
    """Execute a recorded workload session against a target endpoint."""
    run_replay_command(
        recording=recording,
        target=target,
        concurrency=concurrency,
        headers=header,
    )


@app.command()
def verify(
    recording: Path = typer.Option(
        ...,
        "--recording",
        "-r",
        help="Path to the recorded JSON workload session.",
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
) -> None:
    """Run replay, diff comparison, and attestation between baseline and candidate."""
    console.print("[bold cyan]Assay Verification Pipeline[/bold cyan]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Stage", style="cyan")
    table.add_column("Target / Input", style="white")
    table.add_row("Recording", str(recording))
    table.add_row("Baseline", baseline)
    table.add_row("Candidate", candidate)
    console.print(table)
    console.print(
        "[bold green]✔️ Verification pipeline staged[/bold green] "
        "[yellow](skeleton mode in Milestone 1).[/yellow]"
    )


if __name__ == "__main__":
    app()
