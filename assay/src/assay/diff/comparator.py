"""Differential comparator for evaluating baseline vs. candidate HTTP responses."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from deepdiff import DeepDiff

from assay.diff.normalizer import normalize_deepdiff_path, should_ignore_path
from assay.models.diff import DiffClassification, FieldDelta, InteractionDiff
from assay.models.recording import HTTPHeader, RecordedResponse
from assay.models.replay import ReplayExecutionPair

logger = logging.getLogger(__name__)

DEFAULT_HEADERS_TO_COMPARE: tuple[str, ...] = ("Content-Type", "Cache-Control")


def _try_parse_json(body: Optional[str]) -> Tuple[bool, Any]:
    """Attempt to parse a response body string as JSON."""
    if body is None or not body.strip():
        return False, None
    try:
        return True, json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False, None


def _headers_to_map(headers: List[HTTPHeader]) -> Dict[str, str]:
    """Convert a list of ``HTTPHeader`` objects into a lowercase-keyed dict."""
    mapping: Dict[str, str] = {}
    for header in headers:
        mapping[header.name.lower()] = header.value
    return mapping


class DifferentialComparator:
    """Compares single baseline and candidate HTTP responses structurally."""

    def __init__(
        self,
        ignore_paths: Optional[List[str]] = None,
        headers_to_compare: Optional[List[str]] = None,
    ) -> None:
        """Initialize the comparator with ignore masks and monitored headers.

        Args:
            ignore_paths: JSONPath or bare key patterns to exclude from body diffs.
            headers_to_compare: Header names to compare (case-insensitive).
                Defaults to ``["Content-Type", "Cache-Control"]``.
        """
        self.ignore_paths: List[str] = list(ignore_paths or [])
        self.headers_to_compare: List[str] = (
            list(headers_to_compare)
            if headers_to_compare is not None
            else list(DEFAULT_HEADERS_TO_COMPARE)
        )

    def _compare_headers(
        self,
        baseline: RecordedResponse,
        candidate: RecordedResponse,
    ) -> List[FieldDelta]:
        """Compare configured headers between baseline and candidate responses."""
        baseline_map = _headers_to_map(baseline.headers)
        candidate_map = _headers_to_map(candidate.headers)
        deltas: List[FieldDelta] = []

        for header_name in self.headers_to_compare:
            key = header_name.lower()
            old_val = baseline_map.get(key)
            new_val = candidate_map.get(key)
            if old_val == new_val:
                continue

            if old_val is None:
                change_type = "item_added"
            elif new_val is None:
                change_type = "item_removed"
            else:
                change_type = "value_changed"

            deltas.append(
                FieldDelta(
                    path=f"$.headers.{header_name}",
                    change_type=change_type,
                    old_value=old_val,
                    new_value=new_val,
                )
            )
        return deltas

    def _extract_json_deltas(
        self,
        baseline_json: Any,
        candidate_json: Any,
    ) -> List[FieldDelta]:
        """Run DeepDiff on parsed JSON objects and map differences to ``FieldDelta``."""
        diff = DeepDiff(baseline_json, candidate_json, ignore_order=False, verbose_level=2)
        deltas: List[FieldDelta] = []

        for raw_path, change in diff.get("values_changed", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="value_changed",
                    old_value=change.get("old_value"),
                    new_value=change.get("new_value"),
                )
            )

        for raw_path, change in diff.get("type_changes", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="type_mismatch",
                    old_value=change.get("old_value"),
                    new_value=change.get("new_value"),
                )
            )

        for raw_path, added_val in diff.get("dictionary_item_added", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="item_added",
                    old_value=None,
                    new_value=added_val,
                )
            )

        for raw_path, removed_val in diff.get("dictionary_item_removed", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="item_removed",
                    old_value=removed_val,
                    new_value=None,
                )
            )

        for raw_path, added_val in diff.get("iterable_item_added", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="item_added",
                    old_value=None,
                    new_value=added_val,
                )
            )

        for raw_path, removed_val in diff.get("iterable_item_removed", {}).items():
            norm_path = normalize_deepdiff_path(raw_path)
            if should_ignore_path(norm_path, self.ignore_paths):
                continue
            deltas.append(
                FieldDelta(
                    path=norm_path,
                    change_type="item_removed",
                    old_value=removed_val,
                    new_value=None,
                )
            )

        return deltas

    def _compare_bodies(
        self,
        baseline: RecordedResponse,
        candidate: RecordedResponse,
    ) -> List[FieldDelta]:
        """Compare response bodies structurally (JSON) or as plain text."""
        old_body = baseline.body
        new_body = candidate.body

        if (old_body or "") == (new_body or ""):
            return []

        old_is_json, old_json = _try_parse_json(old_body)
        new_is_json, new_json = _try_parse_json(new_body)

        if old_is_json and new_is_json:
            return self._extract_json_deltas(old_json, new_json)

        if should_ignore_path("$", self.ignore_paths):
            return []

        if old_body is None:
            change_type = "item_added"
        elif new_body is None:
            change_type = "item_removed"
        else:
            change_type = "value_changed"

        return [
            FieldDelta(
                path="$",
                change_type=change_type,
                old_value=old_body,
                new_value=new_body,
            )
        ]

    def compare_responses(
        self,
        baseline: RecordedResponse,
        candidate: RecordedResponse,
        interaction_id: str = "",
    ) -> InteractionDiff:
        """Compare a baseline ``RecordedResponse`` against a candidate response.

        Args:
            baseline: Reference HTTP response.
            candidate: Candidate HTTP response.
            interaction_id: Identifier of the interaction being compared.

        Returns:
            ``InteractionDiff`` summarizing status, header, body, and latency deltas.
        """
        status_code_changed = baseline.status_code != candidate.status_code
        header_diffs = self._compare_headers(baseline, candidate)
        body_diffs = self._compare_bodies(baseline, candidate)
        latency_delta_ms = candidate.elapsed_ms - baseline.elapsed_ms

        has_changes = status_code_changed or bool(header_diffs) or bool(body_diffs)
        classification = (
            DiffClassification.CHANGED if has_changes else DiffClassification.UNCHANGED
        )

        return InteractionDiff(
            interaction_id=interaction_id,
            classification=classification,
            status_code_changed=status_code_changed,
            old_status=baseline.status_code,
            new_status=candidate.status_code,
            body_diffs=body_diffs,
            header_diffs=header_diffs,
            latency_delta_ms=latency_delta_ms,
        )

    def compare_execution_pairs(
        self,
        baseline_pair: ReplayExecutionPair,
        candidate_pair: ReplayExecutionPair,
    ) -> InteractionDiff:
        """Compare two ``ReplayExecutionPair`` objects, handling execution failures."""
        interaction_id = baseline_pair.interaction_id

        if not baseline_pair.is_success or baseline_pair.replayed_response is None:
            return InteractionDiff(
                interaction_id=interaction_id,
                classification=DiffClassification.INCONCLUSIVE,
                status_code_changed=False,
                old_status=None,
                new_status=(
                    candidate_pair.replayed_response.status_code
                    if candidate_pair.replayed_response
                    else None
                ),
                latency_delta_ms=0.0,
                error_message=baseline_pair.error_message
                or "Baseline replay failed without a response.",
            )

        if not candidate_pair.is_success or candidate_pair.replayed_response is None:
            return InteractionDiff(
                interaction_id=interaction_id,
                classification=DiffClassification.FAILED,
                status_code_changed=False,
                old_status=baseline_pair.replayed_response.status_code,
                new_status=None,
                latency_delta_ms=0.0,
                error_message=candidate_pair.error_message
                or "Candidate replay failed without a response.",
            )

        return self.compare_responses(
            baseline=baseline_pair.replayed_response,
            candidate=candidate_pair.replayed_response,
            interaction_id=interaction_id,
        )
