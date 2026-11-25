"""Async HTTP replay client built on httpx."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Dict, Optional
from urllib.parse import urlsplit

import httpx

from assay.models.recording import (
    HTTPHeader,
    RecordedInteraction,
    RecordedResponse,
    decode_body_bytes,
)
from assay.models.replay import ReplayExecutionPair

logger = logging.getLogger(__name__)

STRIPPED_REPLAY_HEADERS: frozenset[str] = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "upgrade",
    }
)


def _encode_replay_body(body: Optional[str]) -> Optional[bytes]:
    """Convert a recorded body string back into raw bytes for transmission."""
    if body is None:
        return None
    if body.startswith("<binary:hex:") and body.endswith(">"):
        hex_payload = body[len("<binary:hex:") : -1]
        try:
            return bytes.fromhex(hex_payload)
        except ValueError:
            return body.encode("utf-8")
    return body.encode("utf-8")


class ReplayClient:
    """Resilient async HTTP client for replaying recorded interactions."""

    def __init__(
        self,
        target_base_url: str,
        timeout_seconds: float = 10.0,
        max_keepalive_connections: int = 20,
        max_connections: int = 100,
        header_overrides: Optional[Dict[str, str]] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        """Initialize the replay HTTP client.

        Args:
            target_base_url: Base URL of the target service (e.g. ``http://localhost:8001``).
            timeout_seconds: Request and connection timeout in seconds.
            max_keepalive_connections: Maximum pooled keep-alive connections.
            max_connections: Maximum total concurrent connections.
            header_overrides: Optional mapping of header names to override values.
            transport: Optional custom httpx transport (useful for testing).
        """
        self.target_base_url = target_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_keepalive_connections = max_keepalive_connections
        self.max_connections = max_connections
        self.header_overrides: Dict[str, str] = dict(header_overrides or {})
        self._transport = transport
        self._client: Optional[httpx.AsyncClient] = None
        self._owns_client: bool = False

    def _create_client(self) -> httpx.AsyncClient:
        """Instantiate an ``httpx.AsyncClient`` with configured limits and timeouts."""
        limits = httpx.Limits(
            max_keepalive_connections=self.max_keepalive_connections,
            max_connections=self.max_connections,
        )
        timeout = httpx.Timeout(timeout=self.timeout_seconds)
        return httpx.AsyncClient(
            timeout=timeout,
            limits=limits,
            follow_redirects=False,
            transport=self._transport,
        )

    async def __aenter__(self) -> ReplayClient:
        """Enter async context manager and open the underlying httpx client."""
        if self._client is None:
            self._client = self._create_client()
            self._owns_client = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Exit async context manager and close owned httpx resources."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` if owned by this instance."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    def _build_headers(self, recorded_headers: list[HTTPHeader]) -> Dict[str, str]:
        """Strip hop-by-hop headers and apply case-insensitive header overrides."""
        cleaned: Dict[str, str] = {}
        for header in recorded_headers:
            if header.name.lower() in STRIPPED_REPLAY_HEADERS:
                continue
            cleaned[header.name] = header.value

        if self.header_overrides:
            override_lookup = {k.lower(): (k, v) for k, v in self.header_overrides.items()}
            filtered = {
                k: v for k, v in cleaned.items() if k.lower() not in override_lookup
            }
            for orig_name, override_val in override_lookup.values():
                filtered[orig_name] = override_val
            return filtered

        return cleaned

    def _build_target_url(self, interaction: RecordedInteraction) -> str:
        """Map the recorded request path onto ``target_base_url``."""
        path = interaction.request.path
        if not path:
            parsed = urlsplit(interaction.request.url)
            path = parsed.path or "/"
        if not path.startswith("/"):
            path = f"/{path}"
        return f"{self.target_base_url}{path}"

    async def replay_interaction(
        self,
        interaction: RecordedInteraction,
    ) -> ReplayExecutionPair:
        """Replay a single recorded interaction against ``target_base_url``.

        Args:
            interaction: The ``RecordedInteraction`` to reconstruct and dispatch.

        Returns:
            ``ReplayExecutionPair`` containing either the captured response or
            error diagnostics with ``is_success=False``.
        """
        created_ephemeral = False
        client = self._client
        if client is None:
            client = self._create_client()
            created_ephemeral = True

        target_url = self._build_target_url(interaction)
        headers = self._build_headers(interaction.request.headers)
        body_bytes = _encode_replay_body(interaction.request.body)
        params = interaction.request.query_params or None

        start_time = time.perf_counter()
        try:
            response = await client.request(
                method=interaction.request.method,
                url=target_url,
                params=params,
                headers=headers,
                content=body_bytes,
            )
            elapsed_ms = max((time.perf_counter() - start_time) * 1000.0, 0.0001)
            replayed_at = datetime.now(timezone.utc)

            resp_headers = [
                HTTPHeader(name=k, value=v) for k, v in response.headers.multi_items()
            ]
            resp_content_type = response.headers.get("content-type")
            decoded_body = decode_body_bytes(response.content, resp_content_type)

            replayed_response = RecordedResponse(
                status_code=response.status_code,
                headers=resp_headers,
                body=decoded_body,
                content_type=resp_content_type,
                elapsed_ms=elapsed_ms,
            )
            return ReplayExecutionPair(
                interaction_id=interaction.interaction_id,
                recorded_at=interaction.recorded_at,
                request=interaction.request,
                original_response=interaction.response,
                replayed_response=replayed_response,
                replayed_at=replayed_at,
                duration_ms=elapsed_ms,
                error_message=None,
                is_success=True,
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPError, OSError) as exc:
            elapsed_ms = max((time.perf_counter() - start_time) * 1000.0, 0.0001)
            error_detail = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Replay failed for interaction %s (%s %s): %s",
                interaction.interaction_id,
                interaction.request.method,
                target_url,
                error_detail,
            )
            return ReplayExecutionPair(
                interaction_id=interaction.interaction_id,
                recorded_at=interaction.recorded_at,
                request=interaction.request,
                original_response=interaction.response,
                replayed_response=None,
                replayed_at=datetime.now(timezone.utc),
                duration_ms=elapsed_ms,
                error_message=error_detail,
                is_success=False,
            )
        except Exception as exc:
            elapsed_ms = max((time.perf_counter() - start_time) * 1000.0, 0.0001)
            error_detail = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Unexpected error replaying interaction %s",
                interaction.interaction_id,
            )
            return ReplayExecutionPair(
                interaction_id=interaction.interaction_id,
                recorded_at=interaction.recorded_at,
                request=interaction.request,
                original_response=interaction.response,
                replayed_response=None,
                replayed_at=datetime.now(timezone.utc),
                duration_ms=elapsed_ms,
                error_message=error_detail,
                is_success=False,
            )
        finally:
            if created_ephemeral:
                await client.aclose()
