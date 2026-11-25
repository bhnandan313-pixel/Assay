"""Unit and resiliency tests for Phase 3 HTTP retries and Playwright UI replay."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from aiohttp import web

from assay.browser.replayer import UIActionReplayer
from assay.models.recording import (
    BrowserAction,
    RecordedInteraction,
    RecordedRequest,
    RecordedResponse,
    RecordingSession,
)
from assay.replay.client import ReplayClient


def _sample_interaction() -> RecordedInteraction:
    return RecordedInteraction(
        interaction_id="retry-int-1",
        recorded_at=datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc),
        request=RecordedRequest(
            method="GET",
            url="http://localhost:8000/api/health",
            path="/api/health",
            headers=[],
        ),
        response=RecordedResponse(
            status_code=200,
            headers=[],
            body='{"ok": true}',
            content_type="application/json",
            elapsed_ms=5.0,
        ),
    )


@pytest.mark.asyncio
async def test_replay_client_retries_with_exponential_backoff_and_succeeds() -> None:
    """Verify ReplayClient retries 502 responses with exponential backoff and succeeds."""
    interaction = _sample_interaction()
    sleep_delays: List[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    with respx.mock(assert_all_called=True) as mock_router:
        route = mock_router.get("http://candidate.local/api/health").mock(
            side_effect=[
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(502, text="Bad Gateway"),
                httpx.Response(200, json={"ok": True}),
            ]
        )

        with patch("assay.replay.client.asyncio.sleep", side_effect=fake_sleep):
            async with ReplayClient(
                target_base_url="http://candidate.local",
                max_retries=3,
                backoff_factor=0.25,
            ) as client:
                pair = await client.replay_interaction(interaction)

    assert route.call_count == 3
    assert pair.is_success is True
    assert pair.replayed_response is not None
    assert pair.replayed_response.status_code == 200
    assert pair.error_message is None
    assert len(sleep_delays) == 2
    assert sleep_delays[0] >= 0.25
    assert sleep_delays[1] >= 0.50


@pytest.mark.asyncio
async def test_replay_client_exhausts_retries_on_network_errors() -> None:
    """Verify ReplayClient marks interaction failed once max_retries is exhausted."""
    interaction = _sample_interaction()

    with respx.mock(assert_all_called=True) as mock_router:
        route = mock_router.get("http://candidate.local/api/health").mock(
            side_effect=httpx.ConnectTimeout("timed out")
        )

        with patch("assay.replay.client.asyncio.sleep", new=AsyncMock()):
            async with ReplayClient(
                target_base_url="http://candidate.local",
                max_retries=2,
                backoff_factor=0.1,
            ) as client:
                pair = await client.replay_interaction(interaction)

    assert route.call_count == 3
    assert pair.is_success is False
    assert pair.replayed_response is None
    assert pair.error_message is not None
    assert "ConnectTimeout" in pair.error_message


@pytest.mark.asyncio
async def test_ui_action_replayer_executes_recorded_form_flow() -> None:
    """Verify UIActionReplayer launches headless Chromium and replays form interactions."""
    received_submissions: List[str] = []

    html_page = """
    <!doctype html>
    <html>
      <body>
        <form id="demo-form">
          <input id="name-field" name="name" type="text" />
          <button id="save-btn" type="button">Save</button>
        </form>
        <div id="status">idle</div>
        <script>
          document.getElementById("save-btn").addEventListener("click", async () => {
            const val = document.getElementById("name-field").value;
            const resp = await fetch("/api/save?name=" + encodeURIComponent(val), {
              method: "POST"
            });
            const data = await resp.json();
            document.getElementById("status").textContent = "saved:" + data.name;
          });
        </script>
      </body>
    </html>
    """

    async def handle_index(_: web.Request) -> web.Response:
        return web.Response(text=html_page, content_type="text/html")

    async def handle_save(request: web.Request) -> web.Response:
        name = request.query.get("name", "")
        received_submissions.append(name)
        return web.json_response({"name": name})

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/save", handle_save)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    port = int(site._server.sockets[0].getsockname()[1])

    session = RecordingSession(
        session_id="ui-replay-test",
        target_url=f"http://127.0.0.1:{port}",
        created_at=datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc),
        ui_actions=[
            BrowserAction(
                action_type="navigate",
                selector="window",
                value=f"http://127.0.0.1:{port}/",
                timestamp_offset_ms=0.0,
            ),
            BrowserAction(
                action_type="input",
                selector="input#name-field",
                value="AssayTester",
                timestamp_offset_ms=50.0,
            ),
            BrowserAction(
                action_type="click",
                selector="button#save-btn",
                value=None,
                timestamp_offset_ms=100.0,
            ),
        ],
    )

    try:
        replayer = UIActionReplayer(
            candidate_url=f"http://127.0.0.1:{port}",
            session=session,
            headless=True,
            action_delay_ms=20.0,
        )
        ui_deltas = await replayer.replay_ui_session()
    finally:
        await runner.cleanup()

    assert ui_deltas == []
    assert received_submissions == ["AssayTester"]
