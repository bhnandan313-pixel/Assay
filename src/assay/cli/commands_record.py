"""CLI handler for the `assay record` command."""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from assay.config.settings import load_settings
from assay.recorder.proxy import RecordingProxyServer
from assay.recorder.redaction import Redactor
from assay.storage.session_store import SessionStore

console = Console()


def validate_target_url(target: str) -> str:
    """Validate that the upstream target URL is a well-formed HTTP(S) URL.

    Args:
        target: Candidate upstream URL string.

    Returns:
        Normalized URL string without trailing slash.

    Raises:
        typer.BadParameter: If the URL scheme or network location is invalid.
    """
    parsed = urlparse(target.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise typer.BadParameter(
            f"Invalid target URL '{target}'. Expected http://<host> or https://<host>."
        )
    return target.strip().rstrip("/")


async def _serve_proxy(proxy: RecordingProxyServer) -> Optional[Path]:
    """Run the recording proxy until cancelled or signalled and flush on shutdown."""
    await proxy.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(*_: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _request_stop)
            except (ValueError, OSError):
                pass

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        return await proxy.stop()


def _render_recording_summary(proxy: RecordingProxyServer, saved_path: Optional[Path]) -> None:
    """Print a Rich summary table of all recorded interactions."""
    interactions = proxy.session.interactions
    table = Table(
        title=f"Recording Session Summary ({proxy.session.session_id})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("#", style="dim", justify="right")
    table.add_column("Method", style="cyan")
    table.add_column("Path", style="white")
    table.add_column("Status", style="green", justify="right")
    table.add_column("Elapsed (ms)", style="magenta", justify="right")

    for index, item in enumerate(interactions, start=1):
        table.add_row(
            str(index),
            item.request.method,
            item.request.path,
            str(item.response.status_code),
            f"{item.response.elapsed_ms:.2f}",
        )

    console.print(table)
    if saved_path is not None:
        console.print(
            f"[bold green]✔️ Saved {len(interactions)} interaction(s) to "
            f"{saved_path}[/bold green]"
        )


def run_record_command(
    target: str,
    port: Optional[int] = None,
    session_id: Optional[str] = None,
) -> None:
    """Bootstrap settings, run the recording proxy, and flush on SIGINT.

    Args:
        target: Upstream target service URL.
        port: Optional local proxy port override.
        session_id: Optional explicit recording session identifier.
    """
    validated_target = validate_target_url(target)
    settings = load_settings()

    listen_host = settings.network.proxy_host
    listen_port = port if port is not None else settings.network.proxy_port
    redactor = Redactor.from_field_list(settings.redaction.fields)
    session_store = SessionStore(settings.storage.sessions_dir)

    proxy = RecordingProxyServer(
        target_url=validated_target,
        host=listen_host,
        port=listen_port,
        session_id=session_id,
        redactor=redactor,
        session_store=session_store,
        timeout_seconds=settings.network.timeout_seconds,
    )

    redacted_list = ", ".join(settings.redaction.fields)
    panel = Panel.fit(
        "\n".join(
            [
                f"[bold green]✔️ Proxy Listening:[/bold green] http://{listen_host}:{listen_port}",
                f"[bold cyan]➜ Forwarding To:[/bold cyan] {validated_target}",
                f"[bold yellow]🔒 Redacting Fields:[/bold yellow] {redacted_list}",
                f"[dim]Session ID: {proxy.session.session_id} | Press Ctrl+C to stop & save[/dim]",
            ]
        ),
        title="[bold]Assay Recording Proxy[/bold]",
        border_style="cyan",
    )
    console.print(panel)

    saved_path: Optional[Path] = None
    try:
        saved_path = asyncio.run(_serve_proxy(proxy))
    except KeyboardInterrupt:
        saved_path = proxy.flush()
    except OSError as exc:
        console.print(f"[bold red]❌ Failed to start proxy server:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if saved_path is None:
        saved_path = proxy.flush()

    _render_recording_summary(proxy, saved_path)
