"""End-to-end integration test executing the complete Assay demo workflow."""

from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_DEMO_PATH = REPO_ROOT / "examples" / "regression-demo" / "run_demo.py"


def _allocate_free_port() -> int:
    """Bind an ephemeral loopback port and return its port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_end_to_end_ecommerce_regression_demo(tmp_path: Path) -> None:
    """Run full Init -> Record(v1) -> Verify(v1 vs v2) -> Attest pipeline."""
    spec = importlib.util.spec_from_file_location("assay_run_demo", RUN_DEMO_PATH)
    assert spec is not None and spec.loader is not None
    demo_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo_module)

    baseline_port = _allocate_free_port()
    candidate_port = _allocate_free_port()
    proxy_port = _allocate_free_port()

    bundle = demo_module.run_demo(
        work_dir=tmp_path,
        baseline_port=baseline_port,
        candidate_port=candidate_port,
        proxy_port=proxy_port,
    )

    assert bundle.manifest.summary.total == 5
    assert bundle.manifest.summary.changed == 3
    assert bundle.manifest.summary.unchanged == 2
    assert bundle.manifest.summary.failed == 0
    assert bundle.manifest.summary.inconclusive == 0
