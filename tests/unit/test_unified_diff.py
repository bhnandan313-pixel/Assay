"""Unit tests for Phase 4 unified DOM + API differential comparison."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from assay.diff.comparator import DifferentialComparator
from assay.diff.dom_comparator import DOMComparator, UIExecutionOutcome
from assay.diff.session_comparator import UnifiedSessionComparator
from assay.models.diff import DiffClassification
from assay.models.recording import (
    HTTPHeader,
    RecordedRequest,
    RecordedResponse,
)
from assay.models.replay import ReplayExecutionPair, ReplaySessionReport


def _make_pair(
    interaction_id: str,
    status_code: int,
    body_json: str,
    trace_id: str | None = None,
) -> ReplayExecutionPair:
    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)
    req = RecordedRequest(
        method="POST",
        url="http://localhost:8000/api/checkout",
        path="/api/checkout",
        headers=[HTTPHeader(name="Content-Type", value="application/json")],
        trace_id=trace_id,
    )
    resp = RecordedResponse(
        status_code=status_code,
        headers=[HTTPHeader(name="Content-Type", value="application/json")],
        body=body_json,
        content_type="application/json",
        elapsed_ms=15.0,
    )
    return ReplayExecutionPair(
        interaction_id=interaction_id,
        recorded_at=now,
        request=req,
        original_response=resp,
        replayed_response=resp,
        replayed_at=now,
        duration_ms=15.0,
        is_success=status_code < 500,
    )


def test_dom_comparator_catches_route_redirect_and_element_regressions() -> None:
    """Assert DOMComparator detects route redirect failures (404) and missing elements."""
    baseline = UIExecutionOutcome(
        action_type="navigate",
        selector="button#login-btn",
        current_url="http://localhost:5173/dashboard/hotel",
        status_code=200,
        page_title="Hotel Dashboard",
        visible_elements={"h1.dashboard-title": True, "div.error-banner": False},
        trace_id="trace-1",
    )
    candidate = UIExecutionOutcome(
        action_type="navigate",
        selector="button#login-btn",
        current_url="http://localhost:5173/404",
        status_code=404,
        page_title="Not Found",
        visible_elements={"h1.dashboard-title": False, "div.error-banner": True},
        trace_id="trace-1",
    )

    comparator = DOMComparator()
    ui_deltas = comparator.compare_ui_states(baseline, candidate)
    change_types = {d.change_type for d in ui_deltas}

    assert "url_redirect_mismatch" in change_types
    assert "element_missing" in change_types
    assert "visibility_mismatch" in change_types
    assert "value_mismatch" in change_types


def test_api_comparator_filters_ignored_jsonpaths_and_flags_real_changes() -> None:
    """Assert session_id and timestamps are ignored while genuine price changes surface."""
    comparator = DifferentialComparator(
        ignore_paths=["$.session_id", "$.created_at"],
    )
    baseline = _make_pair(
        "int-price",
        200,
        '{"session_id": "sess-aaa", "created_at": "2026-01-01T00:00:00Z", "price": 250.0}',
    )
    candidate = _make_pair(
        "int-price",
        200,
        '{"session_id": "sess-bbb", "created_at": "2026-11-25T15:00:00Z", "price": 300.0}',
    )

    diff = comparator.compare_execution_pairs(baseline, candidate)
    assert diff.classification == DiffClassification.CHANGED
    assert [d.path for d in diff.body_diffs] == ["$.price"]
    assert diff.body_diffs[0].old_value == 250.0
    assert diff.body_diffs[0].new_value == 300.0


def test_unified_session_comparator_merges_ui_and_api_counts(tmp_path: Path) -> None:
    """Verify UnifiedSessionComparator merges UI and API diffs into SessionDiffReport."""
    now = datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc)
    trace_id = "trace-checkout-1"

    base_pair = _make_pair(
        "int-1",
        200,
        '{"total": 250.0, "created_at": "2026-01-01"}',
        trace_id=trace_id,
    )
    cand_pair = _make_pair(
        "int-1",
        200,
        '{"total": 300.0, "created_at": "2026-11-25"}',
        trace_id=trace_id,
    )

    base_report = ReplaySessionReport(
        replay_id="rep-base",
        session_id="sess-unified",
        target_url="http://localhost:8000",
        started_at=now,
        completed_at=now,
        execution_pairs=[base_pair],
    )
    cand_report = ReplaySessionReport(
        replay_id="rep-cand",
        session_id="sess-unified",
        target_url="http://localhost:8001",
        started_at=now,
        completed_at=now,
        execution_pairs=[cand_pair],
    )

    base_ui = [
        UIExecutionOutcome(
            action_type="navigate",
            selector="window",
            current_url="/checkout",
            status_code=200,
            trace_id="trace-nav",
        ),
        UIExecutionOutcome(
            action_type="click",
            selector="button#pay",
            visible_elements={"div.receipt": True},
            trace_id=trace_id,
        ),
    ]
    cand_ui = [
        UIExecutionOutcome(
            action_type="navigate",
            selector="window",
            current_url="/checkout",
            status_code=200,
            trace_id="trace-nav",
        ),
        UIExecutionOutcome(
            action_type="click",
            selector="button#pay",
            visible_elements={"div.receipt": False},
            trace_id=trace_id,
        ),
    ]

    unified = UnifiedSessionComparator(
        api_comparator=DifferentialComparator(ignore_paths=["$.created_at"]),
        verifications_dir=tmp_path / "verifications",
    )
    report, saved_path = unified.compare_dual_runs(
        baseline_report=base_report,
        candidate_report=cand_report,
        baseline_ui_actions=base_ui,
        candidate_ui_outcomes=cand_ui,
    )

    assert saved_path.exists()
    assert report.total_interactions == 1
    assert report.changed_count == 1
    assert report.ui_unchanged_count == 1
    assert report.ui_failed_count == 1
    assert len(report.deltas[0].body_diffs) == 1
    assert len(report.deltas[0].ui_diffs) == 1
    assert report.deltas[0].ui_diffs[0].selector == "div.receipt"
    assert report.deltas[0].ui_diffs[0].change_type == "element_missing"
