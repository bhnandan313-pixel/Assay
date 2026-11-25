"""Replay orchestration runner supporting sequential and concurrent execution."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx

from assay.models.recording import RecordedInteraction, RecordingSession
from assay.models.replay import ReplayExecutionPair, ReplaySessionReport
from assay.replay.client import ReplayClient
from assay.storage.session_store import SessionStore

ProgressCallback = Callable[[ReplayExecutionPair, int, int], None]


class ReplayRunner:
    """Orchestrates replaying a ``RecordingSession`` and saving the report."""

    def __init__(
        self,
        target_url: str,
        sessions_dir: Path | str = Path("./.assay/sessions"),
        replays_dir: Path | str = Path("./.assay/replays"),
        concurrency: int = 1,
        timeout_seconds: float = 10.0,
        header_overrides: Optional[Dict[str, str]] = None,
        session_store: Optional[SessionStore] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        on_progress: Optional[ProgressCallback] = None,
        max_retries: int = 3,
        backoff_factor: float = 0.5,
    ) -> None:
        """Initialize the replay runner.

        Args:
            target_url: Target service base URL.
            sessions_dir: Directory where recorded sessions are stored.
            replays_dir: Directory where ``ReplaySessionReport`` files are saved.
            concurrency: Max concurrent requests (1 = sequential mode).
            timeout_seconds: Per-request HTTP timeout in seconds.
            header_overrides: Optional HTTP header overrides applied to all requests.
            session_store: Optional ``SessionStore`` instance.
            transport: Optional custom httpx transport for testing.
            on_progress: Optional callback ``(pair, completed_count, total_count)``.
            max_retries: Maximum retry attempts on transient errors.
            backoff_factor: Base exponential backoff factor in seconds.
        """
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")

        self.target_url = target_url.rstrip("/")
        self.sessions_dir = Path(sessions_dir)
        self.replays_dir = Path(replays_dir)
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.header_overrides = dict(header_overrides or {})
        self.session_store = session_store or SessionStore(self.sessions_dir)
        self._transport = transport
        self.on_progress = on_progress
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor

    def _load_session(
        self,
        session_or_path: str | Path | RecordingSession,
    ) -> RecordingSession:
        """Resolve a session ID, file path, or model into a ``RecordingSession``."""
        if isinstance(session_or_path, RecordingSession):
            return session_or_path
        return self.session_store.load_session(session_or_path)

    def _save_report(self, report: ReplaySessionReport) -> Path:
        """Atomically persist a ``ReplaySessionReport`` to ``replays_dir``."""
        self.replays_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.replays_dir / f"replay_{report.replay_id}.json"
        payload = report.model_dump_json(indent=2)

        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp_replay_",
            suffix=".json",
            dir=str(self.replays_dir),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, target_path)
        except BaseException:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

        return target_path

    async def _run_sequential(
        self,
        client: ReplayClient,
        interactions: List[RecordedInteraction],
    ) -> List[ReplayExecutionPair]:
        """Replay interactions one by one in recorded order."""
        total = len(interactions)
        pairs: List[ReplayExecutionPair] = []
        for idx, interaction in enumerate(interactions, start=1):
            pair = await client.replay_interaction(interaction)
            pairs.append(pair)
            if self.on_progress is not None:
                self.on_progress(pair, idx, total)
        return pairs

    async def _run_concurrent(
        self,
        client: ReplayClient,
        interactions: List[RecordedInteraction],
    ) -> List[ReplayExecutionPair]:
        """Replay interactions concurrently up to ``self.concurrency``."""
        total = len(interactions)
        semaphore = asyncio.Semaphore(self.concurrency)
        completed = 0
        progress_lock = asyncio.Lock()

        async def _execute(interaction: RecordedInteraction) -> ReplayExecutionPair:
            nonlocal completed
            async with semaphore:
                pair = await client.replay_interaction(interaction)
            async with progress_lock:
                completed += 1
                if self.on_progress is not None:
                    self.on_progress(pair, completed, total)
            return pair

        results = await asyncio.gather(*(_execute(item) for item in interactions))
        return list(results)

    async def run(
        self,
        session_or_path: str | Path | RecordingSession,
    ) -> tuple[ReplaySessionReport, Path]:
        """Execute all interactions in a session and persist the replay report.

        Args:
            session_or_path: Session ID, JSON file path, or ``RecordingSession``.

        Returns:
            Tuple of ``(ReplaySessionReport, saved_report_path)``.
        """
        session = self._load_session(session_or_path)
        replay_id = uuid.uuid4().hex[:12]
        started_at = datetime.now(timezone.utc)

        async with ReplayClient(
            target_base_url=self.target_url,
            timeout_seconds=self.timeout_seconds,
            max_connections=max(self.concurrency * 2, 20),
            header_overrides=self.header_overrides,
            transport=self._transport,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        ) as client:
            if self.concurrency == 1:
                pairs = await self._run_sequential(client, session.interactions)
            else:
                pairs = await self._run_concurrent(client, session.interactions)

        completed_at = datetime.now(timezone.utc)
        report = ReplaySessionReport(
            replay_id=replay_id,
            session_id=session.session_id,
            target_url=self.target_url,
            started_at=started_at,
            completed_at=completed_at,
            execution_pairs=pairs,
        )
        saved_path = self._save_report(report)
        return report, saved_path
