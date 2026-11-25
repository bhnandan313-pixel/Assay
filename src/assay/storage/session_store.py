"""Thread-safe local JSON storage manager for RecordingSession artifacts."""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import List

from assay.models.recording import RecordingSession


class SessionStore:
    """Manages persistence, locking, import, and export of recording sessions."""

    def __init__(self, sessions_dir: Path | str = Path("./.assay/sessions")) -> None:
        """Initialize the session store and ensure its directory exists.

        Args:
            sessions_dir: Directory where ``session_<id>.json`` files are stored.
        """
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def session_path(self, session_id: str) -> Path:
        """Compute the canonical storage path for a session ID.

        Args:
            session_id: Unique session identifier.

        Returns:
            Path to ``session_<safe_id>.json`` inside ``sessions_dir``.
        """
        safe_id = Path(session_id).name
        if not safe_id or safe_id in {".", ".."}:
            raise ValueError(f"Invalid session_id: {session_id!r}")
        if safe_id.startswith("session_") and safe_id.endswith(".json"):
            return self.sessions_dir / safe_id
        return self.sessions_dir / f"session_{safe_id}.json"

    def save_session(self, session: RecordingSession) -> Path:
        """Atomically serialize a ``RecordingSession`` to indented JSON.

        Uses a temporary file and ``os.replace`` under a lock to prevent
        file corruption during concurrent proxy writes.

        Args:
            session: The ``RecordingSession`` to persist.

        Returns:
            Path to the written JSON file.
        """
        target_path = self.session_path(session.session_id)
        payload = session.model_dump_json(indent=2)

        with self._lock:
            self.sessions_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".tmp_session_",
                suffix=".json",
                dir=str(self.sessions_dir),
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

    def load_session(self, session_id_or_path: str | Path) -> RecordingSession:
        """Load and validate a ``RecordingSession`` from disk.

        Args:
            session_id_or_path: Either a session ID or an explicit JSON file path.

        Returns:
            Validated ``RecordingSession`` object.
        """
        candidate = Path(session_id_or_path)
        if candidate.is_file():
            target_path = candidate
        else:
            target_path = self.session_path(str(session_id_or_path))

        if not target_path.exists():
            raise FileNotFoundError(f"Recording session not found: {target_path}")

        raw_json = target_path.read_text(encoding="utf-8")
        return RecordingSession.model_validate_json(raw_json)

    def list_sessions(self) -> List[Path]:
        """List all recorded JSON session files in ``sessions_dir``."""
        if not self.sessions_dir.exists():
            return []
        return sorted(self.sessions_dir.glob("*.json"))

    def export_session(self, session_id: str, destination: Path | str) -> Path:
        """Export a stored session to an external JSON file path.

        Args:
            session_id: Identifier of the stored session.
            destination: Target file path for the exported JSON.

        Returns:
            Resolved destination ``Path``.
        """
        session = self.load_session(session_id)
        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text(session.model_dump_json(indent=2), encoding="utf-8")
        return dest_path

    def import_session(self, source: Path | str) -> RecordingSession:
        """Import an external session JSON file into ``sessions_dir``.

        Args:
            source: Path to an existing ``RecordingSession`` JSON file.

        Returns:
            Validated and stored ``RecordingSession``.
        """
        source_path = Path(source)
        if not source_path.is_file():
            raise FileNotFoundError(f"Source session file not found: {source_path}")
        session = RecordingSession.model_validate_json(
            source_path.read_text(encoding="utf-8")
        )
        self.save_session(session)
        return session
