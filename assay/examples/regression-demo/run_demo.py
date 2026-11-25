"""End-to-end Assay verification pipeline demonstration script."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional

import httpx
from rich.console import Console
from rich.panel import Panel

from assay.attest.bundle import verify_evidence_bundle
from assay.models.diff import DiffClassification
from assay.models.evidence import EvidenceBundle

console = Console()

REPO_ROOT = Path(__file__).resolve().parents[2]
MANAGE_SCRIPT = REPO_ROOT / "examples" / "ecommerce_api" / "manage.py"


def _assay_cmd() -> List[str]:
    """Resolve the Assay CLI invocation using the active Python environment."""
    scripts_dir = Path(sys.executable).parent
    exe_name = "assay.exe" if os.name == "nt" else "assay"
    candidate = scripts_dir / exe_name
    if candidate.exists():
        return [str(candidate)]
    return [sys.executable, "-m", "assay.cli.main"]


def _wait_for_http(url: str, timeout: float = 15.0) -> None:
    """Poll an HTTP health endpoint until it responds 200 or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.15)
    raise TimeoutError(f"Server at {url} did not become ready within {timeout}s.")


def _wait_for_port(host: str, port: int, timeout: float = 15.0) -> None:
    """Poll a TCP port until a connection succeeds or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.15)
    raise TimeoutError(f"Port {host}:{port} did not open within {timeout}s.")


def _stop_process(proc: Optional[subprocess.Popen[str]], graceful: bool = False) -> None:
    """Terminate a background subprocess cleanly without leaving orphans."""
    if proc is None or proc.poll() is not None:
        return

    if graceful:
        if os.name == "nt":
            sig = getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM)
            proc.send_signal(sig)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _start_demo_server(version_cmd: str, port: int) -> subprocess.Popen[str]:
    """Start the v1 or v2 E-Commerce demo server as a background subprocess."""
    proc = subprocess.Popen(
        [sys.executable, str(MANAGE_SCRIPT), version_cmd, "--port", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _wait_for_http(f"http://127.0.0.1:{port}/health")
    return proc


def _simulate_ecommerce_traffic(proxy_url: str) -> int:
    """Send a realistic 5-step e-commerce workflow through the recording proxy."""
    with httpx.Client(base_url=proxy_url, timeout=10.0) as client:
        reg_resp = client.post(
            "/api/auth/register",
            json={"username": "nandan", "password": "super_secret_password"},
        )
        assert reg_resp.status_code == 201

        login_resp = client.post(
            "/api/auth/login",
            json={"username": "nandan", "password": "super_secret_password"},
        )
        assert login_resp.status_code == 200
        token = login_resp.json()["token"]
        auth_headers = {"Authorization": f"Bearer {token}"}

        products_resp = client.get("/api/products", headers=auth_headers)
        assert products_resp.status_code == 200
        assert "inventory" in products_resp.json()[0]

        cart_resp = client.post(
            "/api/cart/items",
            headers=auth_headers,
            json={"product_id": "prod-1", "quantity": 2},
        )
        assert cart_resp.status_code == 200

        checkout_resp = client.post("/api/cart/checkout", headers=auth_headers)
        assert checkout_resp.status_code == 200
        assert checkout_resp.json()["total_price"] == 200.0

    return 5


def run_demo(
    work_dir: Path | None = None,
    baseline_port: int = 8000,
    candidate_port: int = 8001,
    proxy_port: int = 9000,
) -> EvidenceBundle:
    """Execute and assert the full Assay end-to-end demonstration pipeline."""
    workspace = (work_dir or Path.cwd()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    assay_bin = _assay_cmd()

    v1_proc: Optional[subprocess.Popen[str]] = None
    v2_proc: Optional[subprocess.Popen[str]] = None
    proxy_proc: Optional[subprocess.Popen[str]] = None

    try:
        # 1. Initialize workspace
        init_res = subprocess.run(
            [*assay_bin, "init"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=True,
        )
        assert init_res.returncode == 0

        # 2. Start v1 baseline server for recording
        v1_proc = _start_demo_server("run-v1", baseline_port)

        # 3. Start recording proxy in background
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        )
        proxy_proc = subprocess.Popen(
            [
                *assay_bin,
                "record",
                "--target",
                f"http://127.0.0.1:{baseline_port}",
                "--port",
                str(proxy_port),
                "--session",
                "demo",
            ],
            cwd=str(workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )
        _wait_for_port("127.0.0.1", proxy_port)

        # 4. Simulate traffic through proxy
        total_sent = _simulate_ecommerce_traffic(f"http://127.0.0.1:{proxy_port}")

        # 5. Gracefully stop proxy so session_demo.json is flushed
        _stop_process(proxy_proc, graceful=True)
        proxy_proc = None

        session_file = workspace / ".assay" / "sessions" / "session_demo.json"
        assert session_file.exists(), f"Expected recording at {session_file}"

        # 6. Restart v1 cleanly and start v2 candidate server
        _stop_process(v1_proc)
        v1_proc = _start_demo_server("run-v1", baseline_port)
        v2_proc = _start_demo_server("run-v2", candidate_port)

        # 7. Run `assay verify` with ignore path for $.transaction_id
        verify_res = subprocess.run(
            [
                *assay_bin,
                "verify",
                "--recording",
                str(session_file),
                "--baseline",
                f"http://127.0.0.1:{baseline_port}",
                "--candidate",
                f"http://127.0.0.1:{candidate_port}",
                "--ignore-path",
                "$.transaction_id",
            ],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=True,
        )
        assert verify_res.returncode == 0

        # 8. Locate and assert EvidenceBundle contents & cryptographic validity
        evidence_files = sorted((workspace / ".assay" / "evidence").glob("evidence_*.json"))
        assert len(evidence_files) == 1
        evidence_path = evidence_files[0]

        bundle = EvidenceBundle.model_validate_json(
            evidence_path.read_text(encoding="utf-8")
        )
        assert bundle.manifest.summary.total == total_sent
        assert bundle.manifest.summary.unchanged == 2
        assert bundle.manifest.summary.changed == 3
        assert bundle.manifest.summary.failed == 0
        assert bundle.manifest.summary.inconclusive == 0

        changed_deltas = [
            d for d in bundle.deltas if d.classification == DiffClassification.CHANGED
        ]
        assert len(changed_deltas) == 3
        assert any(
            d.status_code_changed and d.old_status == 201 and d.new_status == 200
            for d in changed_deltas
        )

        all_delta_paths = [
            fd.path for d in bundle.deltas for fd in d.body_diffs
        ]
        assert any("inventory" in path for path in all_delta_paths)
        assert any("total_price" in path for path in all_delta_paths)
        assert not any("transaction_id" in path for path in all_delta_paths)

        is_valid, reason = verify_evidence_bundle(bundle)
        assert is_valid, reason

        verify_ev_res = subprocess.run(
            [*assay_bin, "verify-evidence", "--bundle", str(evidence_path)],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            check=True,
        )
        assert verify_ev_res.returncode == 0

        console.print(
            Panel.fit(
                "\n".join(
                    [
                        "[bold green]✅ End-to-End Assay Demo Passed![/bold green]",
                        f"Total Replayed: {bundle.manifest.summary.total}",
                        f"Unchanged: {bundle.manifest.summary.unchanged} | "
                        f"Changed (Regressions Caught): {bundle.manifest.summary.changed}",
                        f"Evidence Bundle: {evidence_path}",
                    ]
                ),
                title="[bold cyan]Assay Regression Demo Report[/bold cyan]",
                border_style="green",
            )
        )
        return bundle
    finally:
        _stop_process(proxy_proc)
        _stop_process(v2_proc)
        _stop_process(v1_proc)


if __name__ == "__main__":
    run_demo()
