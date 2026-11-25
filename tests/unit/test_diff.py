"""Unit tests for the Differential Engine (normalizer, comparator, runner)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from assay.diff.comparator import DifferentialComparator
from assay.diff.normalizer import normalize_deepdiff_path, should_ignore_path
from assay.diff.session_comparator import SessionComparisonRunner
from assay.models.diff import DiffClassification, SessionDiffReport
from assay.models.recording import HTTPHeader, RecordedRequest, RecordedResponse
from assay.models.replay import ReplayExecutionPair, ReplaySessionReport


def _resp(
    status_code: int = 200,
    body: str | None = None,
    headers: list[HTTPHeader] | None = None,
    elapsed_ms: float = 10.0,
) -> RecordedResponse:
    """Build a ``RecordedResponse`` fixture."""
    return RecordedResponse(
        status_code=status_code,
        headers=headers or [HTTPHeader(name="Content-Type", value="application/json")],
        body=body,
        content_type="application/json",
        elapsed_ms=elapsed_ms,
    )


def test_path_normalizer_and_ignore_patterns() -> None:
    """Normalizer should convert DeepDiff paths and match exact/wildcard/key masks."""
    assert normalize_deepdiff_path("root['user']['age']") == "$.user.age"
    assert normalize_deepdiff_path("root['items'][0]['id']") == "$.items[0].id"
    assert normalize_deepdiff_path("root") == "$"

    patterns = ["$.meta.created_at", "$.*.updated_at", "$.items[*].id", "timestamp"]
    assert should_ignore_path("root['meta']['created_at']", patterns) is True
    assert should_ignore_path("$.user.updated_at", patterns) is True
    assert should_ignore_path("root['items'][3]['id']", patterns) is True
    assert should_ignore_path("$.nested.timestamp", patterns) is True
    assert should_ignore_path("$.user.age", patterns) is False


def test_comparator_status_and_header_mismatches() -> None:
    """Status code and monitored header mismatches should be flagged as CHANGED."""
    comparator = DifferentialComparator(headers_to_compare=["Content-Type", "Cache-Control"])

    baseline = _resp(
        status_code=200,
        body='{"ok": true}',
        headers=[
            HTTPHeader(name="Content-Type", value="application/json"),
            HTTPHeader(name="Date", value="Mon, 01 Jan 2026 00:00:00 GMT"),
        ],
        elapsed_ms=12.0,
    )
    candidate = _resp(
        status_code=201,
        body='{"ok": true}',
        headers=[
            HTTPHeader(name="Content-Type", value="application/problem+json"),
            HTTPHeader(name="Date", value="Tue, 02 Jan 2026 00:00:00 GMT"),
        ],
        elapsed_ms=17.5,
    )

    diff = comparator.compare_responses(baseline, candidate, interaction_id="i-1")

    assert diff.classification == DiffClassification.CHANGED
    assert diff.status_code_changed is True
    assert diff.old_status == 200
    assert diff.new_status == 201
    assert diff.latency_delta_ms == 5.5
    assert len(diff.header_diffs) == 1
    assert diff.header_diffs[0].path == "$.headers.Content-Type"
    assert diff.header_diffs[0].change_type == "value_changed"
    assert diff.body_diffs == []


def test_comparator_nested_json_structural_deltas() -> None:
    """Comparator should map nested JSON changes, additions, and removals."""
    comparator = DifferentialComparator()

    baseline = _resp(
        body=json.dumps(
            {
                "price": 99.99,
                "user": {"name": "Alice", "role": "viewer"},
                "items": [{"sku": "A1"}, {"sku": "B2"}],
            }
        )
    )
    candidate = _resp(
        body=json.dumps(
            {
                "price": 89.99,
                "user": {"name": "Alice", "active": True},
                "items": [{"sku": "A1"}, {"sku": "B2"}, {"sku": "C3"}],
            }
        )
    )

    diff = comparator.compare_responses(baseline, candidate)

    assert diff.classification == DiffClassification.CHANGED
    by_path = {(d.path, d.change_type): d for d in diff.body_diffs}
    assert ("$.price", "value_changed") in by_path
    assert by_path[("$.price", "value_changed")].old_value == 99.99
    assert by_path[("$.price", "value_changed")].new_value == 89.99
    assert ("$.user.active", "item_added") in by_path
    assert ("$.user.role", "item_removed") in by_path
    assert ("$.items[2]", "item_added") in by_path


def test_comparator_exclusion_mask_hides_ignored_fields() -> None:
    """Ignoring dynamic fields like $.meta.timestamp should yield UNCHANGED."""
    comparator = DifferentialComparator(
        ignore_paths=["$.meta.timestamp", "$.items[*].id"]
    )

    baseline = _resp(
        body=json.dumps(
            {
                "meta": {"timestamp": "2026-01-01T00:00:00Z"},
                "items": [{"id": "uuid-1", "name": "widget"}],
            }
        )
    )
    candidate = _resp(
        body=json.dumps(
            {
                "meta": {"timestamp": "2026-11-25T11:30:00Z"},
                "items": [{"id": "uuid-999", "name": "widget"}],
            }
        )
    )

    diff = comparator.compare_responses(baseline, candidate)

    assert diff.classification == DiffClassification.UNCHANGED
    assert diff.body_diffs == []


def test_comparator_detects_type_mismatch() -> None:
    """Comparator should emit type_mismatch when a field's type changes."""
    comparator = DifferentialComparator()

    baseline = _resp(body=json.dumps({"age": "21", "verified": True}))
    candidate = _resp(body=json.dumps({"age": 21, "verified": "true"}))

    diff = comparator.compare_responses(baseline, candidate)

    assert diff.classification == DiffClassification.CHANGED
    types_by_path = {d.path: d for d in diff.body_diffs}
    assert types_by_path["$.age"].change_type == "type_mismatch"
    assert types_by_path["$.age"].old_value == "21"
    assert types_by_path["$.age"].new_value == 21
    assert types_by_path["$.verified"].change_type == "type_mismatch"


def test_session_comparison_runner_tallies_and_saves_report(tmp_path: Path) -> None:
    """SessionComparisonRunner should classify UNCHANGED, CHANGED, FAILED, INCONCLUSIVE."""
    now = datetime.now(timezone.utc)
    req = RecordedRequest(method="GET", url="http://localhost/x", path="/x", headers=[])

    def _pair(
        interaction_id: str,
        resp: RecordedResponse | None,
        is_success: bool = True,
        err: str | None = None,
    ) -> ReplayExecutionPair:
        return ReplayExecutionPair(
            interaction_id=interaction_id,
            recorded_at=now,
            request=req,
            original_response=_resp(body="{}"),
            replayed_response=resp,
            replayed_at=now,
            duration_ms=10.0,
            error_message=err,
            is_success=is_success,
        )

    baseline_report = ReplaySessionReport(
        replay_id="base-1",
        session_id="sess-1",
        target_url="http://localhost:8001",
        started_at=now,
        completed_at=now,
        execution_pairs=[
            _pair("i-same", _resp(body='{"v": 1}')),
            _pair("i-diff", _resp(body='{"v": 1}')),
            _pair("i-fail", _resp(body='{"v": 1}')),
            _pair("i-inc", None, is_success=False, err="Baseline connect timeout"),
        ],
    )
    candidate_report = ReplaySessionReport(
        replay_id="cand-1",
        session_id="sess-1",
        target_url="http://localhost:8002",
        started_at=now,
        completed_at=now,
        execution_pairs=[
            _pair("i-same", _resp(body='{"v": 1}')),
            _pair("i-diff", _resp(body='{"v": 2}')),
            _pair("i-fail", None, is_success=False, err="Candidate TCP reset"),
            _pair("i-inc", _resp(body='{"v": 1}')),
        ],
    )

    runner = SessionComparisonRunner(verifications_dir=tmp_path / "verifications")
    report, saved_path = runner.compare_reports(baseline_report, candidate_report)

    assert saved_path.exists()
    persisted = SessionDiffReport.model_validate_json(
        saved_path.read_text(encoding="utf-8")
    )
    assert persisted.total_interactions == 4
    assert persisted.unchanged_count == 1
    assert persisted.changed_count == 1
    assert persisted.failed_count == 1
    assert persisted.inconclusive_count == 1
    assert report.deltas[2].error_message == "Candidate TCP reset"
    assert report.deltas[3].error_message == "Baseline connect timeout"
