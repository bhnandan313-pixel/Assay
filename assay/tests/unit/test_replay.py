"""Unit and integration tests for the ReplayClient and ReplayRunner."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import respx

from assay.models.recording import (
    HTTPHeader,
    RecordedInteraction,
    RecordedRequest,
    RecordedResponse,
    RecordingSession,
)
from assay.models.replay import ReplaySessionReport
from assay.replay.client import ReplayClient
from assay.replay.runner import ReplayRunner
from assay.storage.session_store import SessionStore


def _make_interaction(
    interaction_id: str = "int-1",
    method: str = "POST",
    path: str = "/api/items",
    query_params: dict[str, str] | None = None,
    headers: list[HTTPHeader] | None = None,
    body: str | None = '{"item":"widget"}',
) -> RecordedInteraction:
    """Create a sample ``RecordedInteraction`` for replay tests."""
    return RecordedInteraction(
        interaction_id=interaction_id,
        recorded_at=datetime.now(timezone.utc),
        request=RecordedRequest(
            method=method,
            url=f"http://original-host:8000{path}",
            path=path,
            query_params=query_params or {},
            headers=headers
            or [
                HTTPHeader(name="Host", value="original-host:8000"),
                HTTPHeader(name="Content-Length", value="17"),
                HTTPHeader(name="Content-Type", value="application/json"),
                HTTPHeader(name="Authorization", value="Bearer old-token"),
                HTTPHeader(name="X-Trace-Id", value="trace-42"),
            ],
            body=body,
            content_type="application/json",
        ),
        response=RecordedResponse(
            status_code=200,
            headers=[HTTPHeader(name="Content-Type", value="application/json")],
            body='{"ok":true}',
            content_type="application/json",
            elapsed_ms=5.0,
        ),
    )


@pytest.mark.asyncio
async def test_replay_client_reconstructs_request_with_fidelity() -> None:
    """ReplayClient should preserve method, path, query, body, and strip hop-by-hop headers."""
    interaction = _make_interaction(
        query_params={"env": "staging", "limit": "10"},
        body='{"name":"assay","count":3}',
    )

    with respx.mock(base_url="http://localhost:8001") as mock_router:
        route = mock_router.post("/api/items").mock(
            return_value=httpx.Response(
                201,
                json={"id": 99, "status": "created"},
                headers={"X-Server": "candidate"},
            )
        )

        async with ReplayClient(
            target_base_url="http://localhost:8001",
            header_overrides={"Authorization": "Bearer fresh-jwt"},
        ) as client:
            pair = await client.replay_interaction(interaction)

    assert route.called
    sent_request = route.calls[0].request
    assert sent_request.method == "POST"
    assert sent_request.url.path == "/api/items"
    assert dict(sent_request.url.params) == {"env": "staging", "limit": "10"}
    assert sent_request.headers["authorization"] == "Bearer fresh-jwt"
    assert sent_request.headers["x-trace-id"] == "trace-42"
    assert sent_request.headers["host"] == "localhost:8001"
    assert json.loads(sent_request.content.decode("utf-8")) == {
        "name": "assay",
        "count": 3,
    }

    assert pair.is_success is True
    assert pair.error_message is None
    assert pair.duration_ms is not None and pair.duration_ms > 0.0
    assert pair.replayed_response is not None
    assert pair.replayed_response.status_code == 201
    assert json.loads(pair.replayed_response.body or "{}") == {
        "id": 99,
        "status": "created",
    }


@pytest.mark.asyncio
async def test_replay_runner_recovers_from_errors_and_continues(tmp_path: Path) -> None:
    """Runner should capture 504s, connect errors, and timeouts without aborting."""
    session = RecordingSession(
        session_id="err-session",
        target_url="http://localhost:8000",
        created_at=datetime.now(timezone.utc),
        interactions=[
            _make_interaction("i-504", method="GET", path="/gateway-timeout", body=None),
            _make_interaction("i-conn", method="GET", path="/conn-refused", body=None),
            _make_interaction("i-hang", method="GET", path="/hang", body=None),
            _make_interaction("i-ok", method="GET", path="/healthy", body=None),
        ],
    )

    store = SessionStore(tmp_path / "sessions")
    session_path = store.save_session(session)

    async def _mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gateway-timeout":
            return httpx.Response(504, text="Gateway Timeout")
        if request.url.path == "/conn-refused":
            raise httpx.ConnectError("Connection refused", request=request)
        if request.url.path == "/hang":
            await asyncio.sleep(0.3)
            raise httpx.ReadTimeout("Simulated read timeout", request=request)
        return httpx.Response(200, json={"healthy": True})

    transport = httpx.MockTransport(_mock_handler)
    runner = ReplayRunner(
        target_url="http://localhost:8001",
        sessions_dir=tmp_path / "sessions",
        replays_dir=tmp_path / "replays",
        concurrency=1,
        timeout_seconds=0.1,
        session_store=store,
        transport=transport,
    )

    report, report_path = await runner.run(session_path)

    assert report_path.exists()
    persisted = ReplaySessionReport.model_validate_json(
        report_path.read_text(encoding="utf-8")
    )
    assert len(persisted.execution_pairs) == 4

    pair_504, pair_conn, pair_hang, pair_ok = persisted.execution_pairs

    assert pair_504.is_success is True
    assert pair_504.replayed_response is not None
    assert pair_504.replayed_response.status_code == 504

    assert pair_conn.is_success is False
    assert pair_conn.replayed_response is None
    assert pair_conn.error_message is not None
    assert "ConnectError" in pair_conn.error_message

    assert pair_hang.is_success is False
    assert pair_hang.replayed_response is None
    assert pair_hang.error_message is not None
    assert "ReadTimeout" in pair_hang.error_message

    assert pair_ok.is_success is True
    assert pair_ok.replayed_response is not None
    assert pair_ok.replayed_response.status_code == 200


@pytest.mark.asyncio
async def test_replay_runner_concurrency_speedup_and_order(tmp_path: Path) -> None:
    """Concurrent replay should run faster than sequential and preserve order."""
    interactions = [
        _make_interaction(f"int-{idx}", method="GET", path=f"/slow/{idx}", body=None)
        for idx in range(6)
    ]
    session = RecordingSession(
        session_id="perf-session",
        target_url="http://localhost:8000",
        created_at=datetime.now(timezone.utc),
        interactions=interactions,
    )

    async def _slow_handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.08)
        return httpx.Response(200, text=request.url.path)

    seq_runner = ReplayRunner(
        target_url="http://localhost:8001",
        sessions_dir=tmp_path / "sessions",
        replays_dir=tmp_path / "replays_seq",
        concurrency=1,
        transport=httpx.MockTransport(_slow_handler),
    )
    seq_start = time.perf_counter()
    seq_report, _ = await seq_runner.run(session)
    seq_elapsed = time.perf_counter() - seq_start

    conc_runner = ReplayRunner(
        target_url="http://localhost:8001",
        sessions_dir=tmp_path / "sessions",
        replays_dir=tmp_path / "replays_conc",
        concurrency=6,
        transport=httpx.MockTransport(_slow_handler),
    )
    conc_start = time.perf_counter()
    conc_report, _ = await conc_runner.run(session)
    conc_elapsed = time.perf_counter() - conc_start

    assert len(seq_report.execution_pairs) == 6
    assert len(conc_report.execution_pairs) == 6
    assert [p.interaction_id for p in conc_report.execution_pairs] == [
        f"int-{idx}" for idx in range(6)
    ]
    assert conc_elapsed < seq_elapsed * 0.6
