"""Unit and integration tests for the Recorder, Redactor, and SessionStore."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from pytest_mock import MockerFixture

from assay.models.recording import (
    HTTPHeader,
    RecordedInteraction,
    RecordedRequest,
    RecordedResponse,
    RecordingSession,
    decode_body_bytes,
)
from assay.recorder.proxy import RecordingProxyServer
from assay.recorder.redaction import REDACTED_VALUE, Redactor
from assay.storage.session_store import SessionStore


def test_redactor_headers_case_insensitive() -> None:
    """Redactor should scrub matching header names regardless of casing."""
    redactor = Redactor(
        header_names=["Authorization", "Cookie", "Set-Cookie"],
        body_fields=["password"],
    )
    headers = [
        HTTPHeader(name="authorization", value="Bearer super-secret"),
        HTTPHeader(name="COOKIE", value="session=abc123"),
        HTTPHeader(name="Content-Type", value="application/json"),
    ]

    redacted = redactor.redact_headers(headers)

    assert redacted[0].value == REDACTED_VALUE
    assert redacted[1].value == REDACTED_VALUE
    assert redacted[2].value == "application/json"


def test_redactor_nested_json_and_form_bodies() -> None:
    """Redactor should recursively scrub JSON keys and form-urlencoded fields."""
    redactor = Redactor(
        header_names=["Authorization"],
        body_fields=["password", "token", "secret", "cvv"],
    )

    nested_payload = json.dumps(
        {
            "user": {
                "name": "Nandan",
                "password": "my_secure_password",
                "tokens": [{"Token": "tok-1"}, {"label": "public"}],
            },
            "payment": {"cvv": "999", "last4": "1234"},
        }
    )
    redacted_json = json.loads(
        redactor.redact_body(nested_payload, "application/json; charset=utf-8")
    )

    assert redacted_json["user"]["name"] == "Nandan"
    assert redacted_json["user"]["password"] == REDACTED_VALUE
    assert redacted_json["user"]["tokens"][0]["Token"] == REDACTED_VALUE
    assert redacted_json["user"]["tokens"][1]["label"] == "public"
    assert redacted_json["payment"]["cvv"] == REDACTED_VALUE
    assert redacted_json["payment"]["last4"] == "1234"

    form_payload = "username=alice&password=s3cr3t&secret=xyz"
    redacted_form = redactor.redact_body(
        form_payload, "application/x-www-form-urlencoded"
    )
    assert "username=alice" in redacted_form
    assert "password=%5BREDACTED%5D" in redacted_form
    assert "secret=%5BREDACTED%5D" in redacted_form


def test_redactor_handles_malformed_json_gracefully() -> None:
    """Malformed JSON bodies should be returned unchanged without raising."""
    redactor = Redactor()
    malformed = "{not valid json"
    assert redactor.redact_body(malformed, "application/json") == malformed


def test_decode_body_bytes_handles_binary_payload() -> None:
    """Non-UTF-8 binary payloads should be safely hex-encoded."""
    raw = b"\x80\x81\xff\x00\xfe"
    decoded = decode_body_bytes(raw, "application/octet-stream")
    assert decoded == f"<binary:hex:{raw.hex()}>"
    assert decode_body_bytes(b"", "text/plain") is None


def test_session_store_save_load_export_import(tmp_path: Path) -> None:
    """SessionStore should atomically save, load, list, export, and import sessions."""
    store = SessionStore(tmp_path / "sessions")
    session = RecordingSession(
        session_id="abc123",
        target_url="http://127.0.0.1:8000",
        created_at=datetime.now(timezone.utc),
        interactions=[
            RecordedInteraction(
                interaction_id="int-1",
                recorded_at=datetime.now(timezone.utc),
                request=RecordedRequest(
                    method="GET",
                    url="http://127.0.0.1:8000/health",
                    path="/health",
                    headers=[],
                ),
                response=RecordedResponse(
                    status_code=200,
                    headers=[],
                    body='{"status":"ok"}',
                    content_type="application/json",
                    elapsed_ms=1.25,
                ),
            )
        ],
    )

    saved_path = store.save_session(session)
    assert saved_path.name == "session_abc123.json"
    assert saved_path.exists()
    assert store.list_sessions() == [saved_path]

    loaded = store.load_session("abc123")
    assert loaded.session_id == "abc123"
    assert len(loaded.interactions) == 1

    export_dest = tmp_path / "exported" / "backup.json"
    store.export_session("abc123", export_dest)
    assert export_dest.exists()

    second_store = SessionStore(tmp_path / "other_sessions")
    imported = second_store.import_session(export_dest)
    assert imported.session_id == "abc123"
    assert second_store.session_path("abc123").exists()


@pytest.mark.asyncio
async def test_proxy_records_and_redacts_live_http_traffic(tmp_path: Path) -> None:
    """Proxy should forward traffic transparently and persist redacted interactions."""
    received_headers: dict[str, str] = {}

    async def handle_users(request: web.Request) -> web.Response:
        received_headers.update(dict(request.headers))
        payload = await request.json()
        return web.json_response(
            {
                "created": True,
                "name": payload["name"],
                "token": "live-response-token",
            },
            status=201,
            headers={"Set-Cookie": "sid=secret-cookie; HttpOnly"},
        )

    target_app = web.Application()
    target_app.router.add_post("/api/users", handle_users)

    target_runner = web.AppRunner(target_app, access_log=None)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    target_port = int(target_site._server.sockets[0].getsockname()[1])

    store = SessionStore(tmp_path / "sessions")
    redactor = Redactor(
        header_names=["Authorization", "Set-Cookie"],
        body_fields=["password", "token"],
    )
    proxy = RecordingProxyServer(
        target_url=f"http://127.0.0.1:{target_port}",
        host="127.0.0.1",
        port=0,
        session_id="test-session",
        redactor=redactor,
        session_store=store,
    )
    proxy_port = await proxy.start()

    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{proxy_port}/api/users?role=admin",
                headers={"Authorization": "Bearer secret-token"},
                json={"name": "Nandan", "password": "my_secure_password"},
            ) as resp:
                assert resp.status == 201
                assert resp.headers.get("Set-Cookie") == "sid=secret-cookie; HttpOnly"
                client_body = await resp.json()
                assert client_body == {
                    "created": True,
                    "name": "Nandan",
                    "token": "live-response-token",
                }
    finally:
        saved_path = await proxy.stop()
        await target_runner.cleanup()

    assert received_headers.get("Authorization") == "Bearer secret-token"
    assert saved_path is not None and saved_path.exists()

    saved_session = store.load_session(saved_path)
    assert saved_session.session_id == "test-session"
    assert len(saved_session.interactions) == 1

    interaction = saved_session.interactions[0]
    assert interaction.request.method == "POST"
    assert interaction.request.path == "/api/users"
    assert interaction.request.query_params == {"role": "admin"}
    assert interaction.response.status_code == 201
    assert interaction.response.elapsed_ms > 0.0

    req_header_map = {h.name.lower(): h.value for h in interaction.request.headers}
    assert req_header_map["authorization"] == REDACTED_VALUE

    resp_header_map = {h.name.lower(): h.value for h in interaction.response.headers}
    assert resp_header_map["set-cookie"] == REDACTED_VALUE

    recorded_req_body = json.loads(interaction.request.body or "{}")
    assert recorded_req_body["name"] == "Nandan"
    assert recorded_req_body["password"] == REDACTED_VALUE

    recorded_resp_body = json.loads(interaction.response.body or "{}")
    assert recorded_resp_body["created"] is True
    assert recorded_resp_body["token"] == REDACTED_VALUE


@pytest.mark.asyncio
async def test_proxy_returns_response_even_if_recording_fails(
    mocker: MockerFixture,
) -> None:
    """Proxy must still return the upstream response if recording raises."""

    async def handle_ping(_: web.Request) -> web.Response:
        return web.Response(text="pong", status=200)

    target_app = web.Application()
    target_app.router.add_get("/ping", handle_ping)

    target_runner = web.AppRunner(target_app, access_log=None)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    target_port = int(target_site._server.sockets[0].getsockname()[1])

    proxy = RecordingProxyServer(
        target_url=f"http://127.0.0.1:{target_port}",
        host="127.0.0.1",
        port=0,
    )
    mocker.patch.object(
        proxy,
        "_record_interaction",
        side_effect=RuntimeError("Simulated recording failure"),
    )
    proxy_port = await proxy.start()

    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(f"http://127.0.0.1:{proxy_port}/ping") as resp:
                assert resp.status == 200
                assert await resp.text() == "pong"
    finally:
        await proxy.stop()
        await target_runner.cleanup()
