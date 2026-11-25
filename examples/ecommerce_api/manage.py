"""Runner utility to launch the E-Commerce Demo API in v1 or v2 mode."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
import uvicorn

_APP_DIR = Path(__file__).resolve().parent
if str(_APP_DIR) not in sys.path:
    sys.path.insert(0, str(_APP_DIR))

cli = typer.Typer(
    name="manage",
    help="Launch the Assay E-Commerce Demo API in baseline (v1) or regressed (v2) mode.",
    no_args_is_help=True,
)


def _run_server(mode: str, label: str, port: int, host: str = "127.0.0.1") -> None:
    """Set mode environment variable and start Uvicorn."""
    os.environ["ASSAY_DEMO_MODE"] = mode
    print(f"🚀 E-Commerce API started in {label} mode on port {port}", flush=True)
    uvicorn.run(
        "app:create_app",
        host=host,
        port=port,
        factory=True,
        log_level="warning",
    )


@cli.command("run-v1")
def run_v1(
    port: int = typer.Option(8000, "--port", "-p", help="Port to bind the v1 server."),
) -> None:
    """Launch the healthy baseline (v1) E-Commerce API."""
    _run_server(mode="v1", label="v1_baseline", port=port)


@cli.command("run-v2")
def run_v2(
    port: int = typer.Option(8001, "--port", "-p", help="Port to bind the v2 server."),
) -> None:
    """Launch the buggy regressed (v2) E-Commerce API."""
    _run_server(mode="v2", label="v2_regressed", port=port)


if __name__ == "__main__":
    cli()
