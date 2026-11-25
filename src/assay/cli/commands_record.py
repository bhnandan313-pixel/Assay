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

from assay.browser.recorder import SUPPORTED_BROWSERS, UIActionRecorder
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


async def _serve_proxy(
    proxy: RecordingProxyServer,
    ui_recorder: Optional[UIActionRecorder] = None,
) -> Optional[Path]:
    """Run the recording proxy (and optional browser recorder) until stopped."""
    bound_port = await proxy.start()
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

    browser_task: Optional[asyncio.Task[None]] = None
    if ui_recorder is not None:
        ui_recorder.proxy_url = f"http://{proxy.host}:{bound_port}"

        async def _run_browser() -> None:
            page = await ui_recorder.start_browser()
            page.on("close", lambda *_: stop_event.set())
            await stop_event.wait()

        browser_task = asyncio.create_task(_run_browser())

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        pass
    finally:
        if ui_recorder is not None:
            await ui_recorder.stop_browser()
        if browser_task is not None and not browser_task.done():
            browser_task.cancel()
            try:
                await browser_task
            except asyncio.CancelledError:
                pass
        return await proxy.stop()


def _render_recording_summary(proxy: RecordingProxyServer, saved_path: Optional[Path]) -> None:
    """Print a Rich summary table of all recorded UI actions and API interactions."""
    ui_actions = proxy.session.ui_actions
    if ui_actions:
        ui_table = Table(
            title=f"Browser UI Actions ({proxy.session.session_id})",
            show_header=True,
            header_style="bold",
        )
        ui_table.add_column("#", style="dim", justify="right")
        ui_table.add_column("Action", style="cyan")
        ui_table.add_column("Selector", style="white")
        ui_table.add_column("Value", style="yellow")
        ui_table.add_column("Trace ID", style="magenta")

        for index, action in enumerate(ui_actions, start=1):
            ui_table.add_row(
                str(index),
                action.action_type,
                action.selector,
                action.value or "-",
                action.trace_id or "-",
            )
        console.print(ui_table)

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
    if ui_actions:
        table.add_column("Trace ID", style="blue")

    for index, item in enumerate(interactions, start=1):
        row = [
            str(index),
            item.request.method,
            item.request.path,
            str(item.response.status_code),
            f"{item.response.elapsed_ms:.2f}",
        ]
        if ui_actions:
            row.append(item.trace_id or "-")
        table.add_row(*row)

    console.print(table)
    if saved_path is not None:
        console.print(
            f"[bold green]✔️ Saved {len(ui_actions)} UI action(s) and "
            f"{len(interactions)} interaction(s) to {saved_path}[/bold green]"
        )


def run_record_command(
    target: str,
    port: Optional[int] = None,
    session_id: Optional[str] = None,
    ui_url: Optional[str] = None,
    browser: str = "chromium",
) -> None:
    """Bootstrap settings, run the recording proxy (and optional browser), and flush on SIGINT.

    Args:
        target: Upstream target service URL.
        port: Optional local proxy port override.
        session_id: Optional explicit recording session identifier.
        ui_url: Optional frontend URL to launch in Playwright for UI capture.
        browser: Playwright browser engine (``chromium``, ``firefox``, or ``webkit``).
    """
    validated_target = validate_target_url(target)
    validated_ui = validate_target_url(ui_url) if ui_url else None
    normalized_browser = browser.strip().lower()
    if normalized_browser not in SUPPORTED_BROWSERS:
        raise typer.BadParameter(
            f"Unsupported browser '{browser}'. "
            f"Choose from: {', '.join(sorted(SUPPORTED_BROWSERS))}."
        )

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

    ui_recorder: Optional[UIActionRecorder] = None
    if validated_ui is not None:
        ui_recorder = UIActionRecorder(
            frontend_url=validated_ui,
            proxy_url=f"http://{listen_host}:{listen_port}",
            session=proxy.session,
            browser_type=normalized_browser,
            headless=False,
            target_url=validated_target,
            redacted_fields=settings.redaction.fields,
        )

    redacted_list = ", ".join(settings.redaction.fields)
    panel_lines = [
        f"[bold green]✔️ Proxy Listening:[/bold green] http://{listen_host}:{listen_port}",
        f"[bold cyan]➜ Forwarding To:[/bold cyan] {validated_target}",
        f"[bold yellow]🔒 Redacting Fields:[/bold yellow] {redacted_list}",
    ]
    if validated_ui is not None:
        panel_lines.append(
            f"[bold magenta]🌐 Browser UI ({normalized_browser}):[/bold magenta] {validated_ui}"
        )
    panel_lines.append(
        f"[dim]Session ID: {proxy.session.session_id} | Press Ctrl+C to stop & save[/dim]"
    )

    panel = Panel.fit(
        "\n".join(panel_lines),
        title="[bold]Assay Recording Proxy[/bold]",
        border_style="cyan",
    )
    console.print(panel)

    saved_path: Optional[Path] = None
    try:
        saved_path = asyncio.run(_serve_proxy(proxy, ui_recorder=ui_recorder))
    except KeyboardInterrupt:
        saved_path = proxy.flush()
    except OSError as exc:
        console.print(f"[bold red]❌ Failed to start proxy server:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    if saved_path is None:
        saved_path = proxy.flush()

    _render_recording_summary(proxy, saved_path)
