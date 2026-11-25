"""Unit tests for Assay v2 full-stack recording and differential schemas."""

from __future__ import annotations

from datetime import datetime, timezone

from assay.models.diff import (
    DiffClassification,
    FieldDelta,
    InteractionDiff,
    SessionDiffReport,
    UIDelta,
)
from assay.models.recording import (
    BrowserAction,
    HTTPHeader,
    RecordedInteraction,
    RecordedRequest,
    RecordedResponse,
    RecordingSession,
)


def test_unified_recording_session_roundtrip_with_trace_correlation() -> None:
    """Validate RecordingSession v2 serialization with linked BrowserAction and HTTP events."""
    trace_id = "trace-ui-001"
    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)

    ui_action = BrowserAction(
        action_type="click",
        selector="button#register-btn",
        value=None,
        timestamp_offset_ms=142.5,
        trace_id=trace_id,
    )
    interaction = RecordedInteraction(
        interaction_id="int-001",
        recorded_at=now,
        trace_id=trace_id,
        request=RecordedRequest(
            method="POST",
            url="http://localhost:8000/api/auth/register",
            path="/api/auth/register",
            headers=[HTTPHeader(name="X-Assay-Trace-ID", value=trace_id)],
            body='{"username": "alice"}',
            content_type="application/json",
            trace_id=trace_id,
        ),
        response=RecordedResponse(
            status_code=201,
            headers=[HTTPHeader(name="Content-Type", value="application/json")],
            body='{"created": true}',
            content_type="application/json",
            elapsed_ms=18.2,
        ),
    )

    session = RecordingSession(
        session_id="sess-fullstack-1",
        target_url="http://localhost:8000",
        created_at=now,
        ui_actions=[ui_action],
        interactions=[interaction],
    )

    serialized = session.model_dump_json()
    restored = RecordingSession.model_validate_json(serialized)

    assert restored.version == "2.0.0"
    assert len(restored.ui_actions) == 1
    assert len(restored.interactions) == 1
    assert restored.ui_actions[0].trace_id == trace_id
    assert restored.interactions[0].trace_id == trace_id
    assert restored.interactions[0].request.trace_id == trace_id
    assert restored.ui_actions[0].selector == "button#register-btn"


def test_session_diff_report_with_mixed_field_and_ui_deltas() -> None:
    """Validate SessionDiffReport containing both API FieldDelta and UI UIDelta items."""
    field_delta = FieldDelta(
        path="$.total_price",
        change_type="value_changed",
        old_value=200.0,
        new_value=230.0,
    )
    ui_delta = UIDelta(
        selector="h1.dashboard-title",
        change_type="element_missing",
        expected_state="Welcome back",
        actual_state=None,
    )
    interaction_diff = InteractionDiff(
        interaction_id="int-002",
        classification=DiffClassification.CHANGED,
        status_code_changed=False,
        old_status=200,
        new_status=200,
        body_diffs=[field_delta],
        ui_diffs=[ui_delta],
        latency_delta_ms=4.5,
    )

    report = SessionDiffReport(
        verification_id="ver-001",
        session_id="sess-fullstack-1",
        baseline_target="http://localhost:8000",
        candidate_target="http://localhost:8001",
        created_at="2026-11-25T10:05:00Z",
        total_interactions=1,
        unchanged_count=0,
        changed_count=1,
        failed_count=0,
        inconclusive_count=0,
        ui_unchanged_count=0,
        ui_changed_count=1,
        ui_failed_count=0,
        deltas=[interaction_diff],
    )

    restored = SessionDiffReport.model_validate_json(report.model_dump_json())
    assert restored.ui_changed_count == 1
    assert restored.deltas[0].body_diffs[0].path == "$.total_price"
    assert restored.deltas[0].ui_diffs[0].selector == "h1.dashboard-title"
    assert restored.deltas[0].ui_diffs[0].change_type == "element_missing"


def test_v2_schema_defaults_and_empty_collections() -> None:
    """Ensure empty lists and default parameters validate cleanly across v2 schemas."""
    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)

    empty_session = RecordingSession(
        session_id="sess-empty",
        target_url="http://localhost:8000",
        created_at=now,
    )
    assert empty_session.version == "2.0.0"
    assert empty_session.ui_actions == []
    assert empty_session.interactions == []

    action = BrowserAction(
        action_type="navigate",
        selector="window",
        timestamp_offset_ms=0.0,
    )
    assert action.value is None
    assert action.trace_id is None

    diff = InteractionDiff(
        interaction_id="int-empty",
        classification=DiffClassification.UNCHANGED,
        status_code_changed=False,
    )
    assert diff.ui_diffs == []
    assert diff.body_diffs == []
    assert diff.header_diffs == []

    report = SessionDiffReport(
        verification_id="ver-empty",
        session_id="sess-empty",
        baseline_target="http://localhost:8000",
        candidate_target="http://localhost:8001",
        created_at="2026-11-25T10:05:00Z",
        total_interactions=0,
        unchanged_count=0,
        changed_count=0,
        failed_count=0,
        inconclusive_count=0,
    )
    assert report.ui_unchanged_count == 0
    assert report.ui_changed_count == 0
    assert report.ui_failed_count == 0
    assert report.deltas == []
