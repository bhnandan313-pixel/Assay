"""Async transparent HTTP reverse proxy with recording and redaction."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

import aiohttp
from aiohttp import ClientTimeout, web

from assay.models.recording import (
    HTTPHeader,
    RecordedInteraction,
    RecordedRequest,
    RecordedResponse,
    RecordingSession,
    decode_body_bytes,
)
from assay.recorder.redaction import Redactor
from assay.storage.session_store import SessionStore

logger = logging.getLogger(__name__)

HOP_BY_HOP_HEADERS: frozenset[str] = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
    }
)


class RecordingProxyServer:
    """Async HTTP reverse proxy that captures, redacts, and stores interactions."""

    def __init__(
        self,
        target_url: str,
        host: str = "127.0.0.1",
        port: int = 9000,
        session_id: Optional[str] = None,
        redactor: Optional[Redactor] = None,
        session_store: Optional[SessionStore] = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Initialize the recording reverse proxy.

        Args:
            target_url: Base URL of the upstream target service.
            host: Local interface to bind.
            port: Local TCP port to listen on (0 for ephemeral port).
            session_id: Optional explicit session identifier.
            redactor: ``Redactor`` instance for scrubbing sensitive data.
            session_store: Optional ``SessionStore`` for persisting recordings.
            timeout_seconds: Upstream HTTP client timeout in seconds.
        """
        self.target_url = target_url.rstrip("/")
        self.host = host
        self.port = port
        self.redactor = redactor or Redactor()
        self.session_store = session_store
        self.timeout_seconds = timeout_seconds

        resolved_session_id = session_id or uuid.uuid4().hex[:12]
        self.session = RecordingSession(
            session_id=resolved_session_id,
            target_url=self.target_url,
            created_at=datetime.now(timezone.utc),
        )

        self._session_lock = asyncio.Lock()
        self._client: Optional[aiohttp.ClientSession] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._bound_port: int = port

    @property
    def bound_port(self) -> int:
        """Return the actual TCP port the proxy is listening on."""
        return self._bound_port

    def _build_web_app(self) -> web.Application:
        """Create the aiohttp web application with a catch-all proxy route."""
        web_app = web.Application()
        web_app.router.add_route("*", "/{tail:.*}", self._handle_request)
        return web_app

    async def start(self) -> int:
        """Start the HTTP reverse proxy and upstream client session.

        Returns:
            The bound local TCP port.
        """
        if self._client is None:
            self._client = aiohttp.ClientSession(
                timeout=ClientTimeout(total=self.timeout_seconds),
                auto_decompress=False,
            )

        web_app = self._build_web_app()
        self._runner = web.AppRunner(web_app, access_log=None)
        await self._runner.setup()

        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()

        if self._site._server and self._site._server.sockets:
            self._bound_port = int(self._site._server.sockets[0].getsockname()[1])

        return self._bound_port

    async def stop(self) -> Optional[Path]:
        """Shut down the proxy, close client connections, and flush the session.

        Returns:
            Path to the persisted session JSON file if ``session_store`` is set.
        """
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

        if self._client is not None:
            await self._client.close()
            self._client = None

        return self.flush()

    def flush(self) -> Optional[Path]:
        """Persist the current in-memory ``RecordingSession`` to disk."""
        if self.session_store is None:
            return None
        return self.session_store.save_session(self.session)

    async def _record_interaction(
        self,
        method: str,
        upstream_url: str,
        path: str,
        query_params: dict[str, str],
        raw_req_headers: list[HTTPHeader],
        req_bytes: bytes,
        req_content_type: Optional[str],
        status_code: int,
        raw_resp_headers: list[HTTPHeader],
        resp_bytes: bytes,
        resp_content_type: Optional[str],
        elapsed_ms: float,
    ) -> None:
        """Redact and append a captured interaction to the active session."""
        redacted_req_headers = self.redactor.redact_headers(raw_req_headers)
        decoded_req_body = decode_body_bytes(req_bytes, req_content_type)
        redacted_req_body = (
            self.redactor.redact_body(decoded_req_body, req_content_type)
            if decoded_req_body is not None
            else None
        )

        redacted_resp_headers = self.redactor.redact_headers(raw_resp_headers)
        decoded_resp_body = decode_body_bytes(resp_bytes, resp_content_type)
        redacted_resp_body = (
            self.redactor.redact_body(decoded_resp_body, resp_content_type)
            if decoded_resp_body is not None
            else None
        )

        interaction = RecordedInteraction(
            interaction_id=str(uuid.uuid4()),
            recorded_at=datetime.now(timezone.utc),
            request=RecordedRequest(
                method=method,
                url=upstream_url,
                path=path,
                query_params=query_params,
                headers=redacted_req_headers,
                body=redacted_req_body,
                content_type=req_content_type,
            ),
            response=RecordedResponse(
                status_code=status_code,
                headers=redacted_resp_headers,
                body=redacted_resp_body,
                content_type=resp_content_type,
                elapsed_ms=elapsed_ms,
            ),
        )

        async with self._session_lock:
            self.session.interactions.append(interaction)

    async def _handle_request(self, request: web.Request) -> web.Response:
        """Forward an incoming HTTP request to upstream and record it safely."""
        if self._client is None:
            return web.Response(status=503, text="Proxy client session not initialized.")

        req_bytes = await request.read()
        upstream_url = f"{self.target_url}{request.raw_path}"

        raw_req_headers = [
            HTTPHeader(name=k, value=v) for k, v in request.headers.items()
        ]
        forward_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
        }
        req_content_type = request.headers.get("Content-Type")
        query_params = {k: v for k, v in parse_qsl(request.query_string)}

        start_time = time.perf_counter()
        try:
            async with self._client.request(
                method=request.method,
                url=upstream_url,
                headers=forward_headers,
                data=req_bytes if req_bytes else None,
                allow_redirects=False,
            ) as upstream_resp:
                resp_bytes = await upstream_resp.read()
                elapsed_ms = max((time.perf_counter() - start_time) * 1000.0, 0.0001)
                resp_status = upstream_resp.status
                raw_resp_headers = [
                    HTTPHeader(name=k, value=v)
                    for k, v in upstream_resp.headers.items()
                ]
                resp_content_type = upstream_resp.headers.get("Content-Type")
        except aiohttp.ClientError as exc:
            logger.warning("Upstream request failed for %s: %s", upstream_url, exc)
            return web.Response(
                status=502,
                text=f"Bad Gateway: upstream request failed ({exc})",
            )

        try:
            await self._record_interaction(
                method=request.method,
                upstream_url=upstream_url,
                path=request.path,
                query_params=query_params,
                raw_req_headers=raw_req_headers,
                req_bytes=req_bytes,
                req_content_type=req_content_type,
                status_code=resp_status,
                raw_resp_headers=raw_resp_headers,
                resp_bytes=resp_bytes,
                resp_content_type=resp_content_type,
                elapsed_ms=elapsed_ms,
            )
        except Exception:
            logger.exception(
                "Unexpected error while recording interaction for %s %s",
                request.method,
                request.path,
            )

        client_headers = {
            h.name: h.value
            for h in raw_resp_headers
            if h.name.lower() not in HOP_BY_HOP_HEADERS
        }
        return web.Response(
            status=resp_status,
            body=resp_bytes,
            headers=client_headers,
        )
