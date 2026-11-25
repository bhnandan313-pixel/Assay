"""Unit and browser-integration tests for Phase 2 trace correlation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict, List

import httpx
import pytest
from aiohttp import web

from assay.browser.recorder import TRACE_HEADER_NAME, UIActionRecorder
from assay.recorder.proxy import RecordingProxyServer
from assay.storage.session_store import SessionStore


async def _start_aiohttp_app(app: web.Application) -> tuple[web.AppRunner, int]:
    """Start an aiohttp web application on an ephemeral localhost port."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None and site._server.sockets
    port = int(site._server.sockets[0].getsockname()[1])
    return runner, port


@pytest.mark.asyncio
async def test_proxy_strips_trace_header_and_records_trace_id(tmp_path: Path) -> None:
    """Verify the proxy strips X-Assay-Trace-ID upstream while storing it in the session."""
    received_headers: List[Dict[str, str]] = []

    async def handle_items(request: web.Request) -> web.Response:
        received_headers.append({k.lower(): v for k, v in request.headers.items()})
        return web.json_response({"ok": True}, status=200)

    backend_app = web.Application()
    backend_app.router.add_get("/api/items", handle_items)
    backend_runner, backend_port = await _start_aiohttp_app(backend_app)

    store = SessionStore(tmp_path / "sessions")
    proxy = RecordingProxyServer(
        target_url=f"http://127.0.0.1:{backend_port}",
        host="127.0.0.1",
        port=0,
        session_id="corr-strip",
        session_store=store,
    )

    try:
        proxy_port = await proxy.start()
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"http://127.0.0.1:{proxy_port}/api/items",
                headers={TRACE_HEADER_NAME: "trace_test_12345", "X-Custom": "keep-me"},
            )
        assert resp.status_code == 200
        saved_path = await proxy.stop()
    finally:
        await backend_runner.cleanup()

    assert saved_path is not None
    assert len(received_headers) == 1
    assert TRACE_HEADER_NAME.lower() not in received_headers[0]
    assert received_headers[0]["x-custom"] == "keep-me"

    loaded = store.load_session(saved_path)
    assert len(loaded.interactions) == 1
    interaction = loaded.interactions[0]
    assert interaction.trace_id == "trace_test_12345"
    assert interaction.request.trace_id == "trace_test_12345"


@pytest.mark.asyncio
async def test_browser_click_correlates_trace_id_with_backend_interaction(
    tmp_path: Path,
) -> None:
    """Verify Playwright UI actions and proxied API calls share an identical trace_id."""
    backend_received_trace_headers: List[str] = []

    cors_headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Assay-Trace-ID",
    }

    async def handle_options(_: web.Request) -> web.Response:
        return web.Response(status=204, headers=cors_headers)

    async def handle_submit(request: web.Request) -> web.Response:
        if TRACE_HEADER_NAME in request.headers:
            backend_received_trace_headers.append(request.headers[TRACE_HEADER_NAME])
        payload = await request.json()
        return web.json_response(
            {"status": "submitted", "user": payload.get("username")},
            headers=cors_headers,
        )

    backend_app = web.Application()
    backend_app.router.add_options("/api/submit", handle_options)
    backend_app.router.add_post("/api/submit", handle_submit)
    backend_runner, backend_port = await _start_aiohttp_app(backend_app)

    store = SessionStore(tmp_path / "sessions")
    proxy = RecordingProxyServer(
        target_url=f"http://127.0.0.1:{backend_port}",
        host="127.0.0.1",
        port=0,
        session_id="corr-e2e",
        session_store=store,
    )
    proxy_port = await proxy.start()

    html_page = f"""
    <!doctype html>
    <html>
      <body>
        <input id="username-input" name="username" type="text" />
        <input id="password-input" name="password" type="password" />
        <button id="submit-btn" type="button">Submit</button>
        <div id="result"></div>
        <script>
          document.getElementById("submit-btn").addEventListener("click", async () => {{
            const username = document.getElementById("username-input").value;
            const resp = await fetch("http://127.0.0.1:{proxy_port}/api/submit", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ username }})
            }});
            const data = await resp.json();
            document.getElementById("result").textContent = data.status;
          }});
        </script>
      </body>
    </html>
    """

    async def handle_index(_: web.Request) -> web.Response:
        return web.Response(text=html_page, content_type="text/html")

    frontend_app = web.Application()
    frontend_app.router.add_get("/", handle_index)
    frontend_runner, frontend_port = await _start_aiohttp_app(frontend_app)

    ui_recorder = UIActionRecorder(
        frontend_url=f"http://127.0.0.1:{frontend_port}",
        proxy_url=f"http://127.0.0.1:{proxy_port}",
        session=proxy.session,
        browser_type="chromium",
        headless=True,
    )

    saved_path: Path | None = None
    try:
        page = await ui_recorder.start_browser()
        await page.fill("#username-input", "alice")
        await page.fill("#password-input", "super_secret_pw")
        await page.click("#submit-btn")
        await page.wait_for_function(
            "document.getElementById('result').textContent === 'submitted'"
        )
        await asyncio.sleep(0.05)
    finally:
        await ui_recorder.stop_browser()
        saved_path = await proxy.stop()
        await frontend_runner.cleanup()
        await backend_runner.cleanup()

    assert saved_path is not None
    assert backend_received_trace_headers == []

    loaded = store.load_session(saved_path)
    assert loaded.version == "2.0.0"
    post_interactions = [i for i in loaded.interactions if i.request.method == "POST"]
    assert len(post_interactions) == 1

    click_actions = [
        action
        for action in loaded.ui_actions
        if action.action_type == "click" and action.selector == "button#submit-btn"
    ]
    assert len(click_actions) == 1
    click_trace_id = click_actions[0].trace_id
    assert click_trace_id is not None and click_trace_id.startswith("trace_")

    interaction = post_interactions[0]
    assert interaction.trace_id == click_trace_id
    assert interaction.request.trace_id == click_trace_id

    pw_actions = [
        action
        for action in loaded.ui_actions
        if action.selector == "input#password-input" and action.value is not None
    ]
    assert pw_actions and all(action.value == "[REDACTED]" for action in pw_actions)
