"""Session-level differential comparison runner."""

from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from assay.diff.comparator import DifferentialComparator
from assay.diff.dom_comparator import DOMComparator, UIExecutionOutcome
from assay.models.diff import (
    DiffClassification,
    InteractionDiff,
    SessionDiffReport,
    UIDelta,
)
from assay.models.recording import BrowserAction
from assay.models.replay import ReplayExecutionPair, ReplaySessionReport


class SessionComparisonRunner:
    """Compares full replay reports and persists ``SessionDiffReport`` artifacts."""

    def __init__(
        self,
        comparator: Optional[DifferentialComparator] = None,
        verifications_dir: Path | str = Path("./.assay/verifications"),
    ) -> None:
        """Initialize the session comparison runner.

        Args:
            comparator: ``DifferentialComparator`` instance to evaluate pairs.
            verifications_dir: Directory where ``verify_<id>.json`` reports are saved.
        """
        self.comparator = comparator or DifferentialComparator()
        self.verifications_dir = Path(verifications_dir)

    def _save_report(self, report: SessionDiffReport) -> Path:
        """Atomically persist a ``SessionDiffReport`` to ``verifications_dir``."""
        self.verifications_dir.mkdir(parents=True, exist_ok=True)
        target_path = self.verifications_dir / f"verify_{report.verification_id}.json"
        payload = report.model_dump_json(indent=2)

        fd, tmp_name = tempfile.mkstemp(
            prefix=".tmp_verify_",
            suffix=".json",
            dir=str(self.verifications_dir),
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

    def _build_report(
        self,
        session_id: str,
        baseline_target: str,
        candidate_target: str,
        deltas: List[InteractionDiff],
    ) -> SessionDiffReport:
        """Tally classification counts and construct a ``SessionDiffReport``."""
        unchanged = sum(
            1 for d in deltas if d.classification == DiffClassification.UNCHANGED
        )
        changed = sum(
            1 for d in deltas if d.classification == DiffClassification.CHANGED
        )
        failed = sum(
            1 for d in deltas if d.classification == DiffClassification.FAILED
        )
        inconclusive = sum(
            1 for d in deltas if d.classification == DiffClassification.INCONCLUSIVE
        )

        return SessionDiffReport(
            verification_id=uuid.uuid4().hex[:12],
            session_id=session_id,
            baseline_target=baseline_target,
            candidate_target=candidate_target,
            created_at=datetime.now(timezone.utc).isoformat(),
            total_interactions=len(deltas),
            unchanged_count=unchanged,
            changed_count=changed,
            failed_count=failed,
            inconclusive_count=inconclusive,
            deltas=deltas,
        )

    def compare_reports(
        self,
        baseline_report: ReplaySessionReport,
        candidate_report: ReplaySessionReport,
    ) -> tuple[SessionDiffReport, Path]:
        """Compare baseline and candidate ``ReplaySessionReport`` outputs.

        Args:
            baseline_report: Replay report from the baseline target.
            candidate_report: Replay report from the candidate target.

        Returns:
            Tuple of ``(SessionDiffReport, saved_report_path)``.
        """
        candidate_by_id: Dict[str, ReplayExecutionPair] = {
            pair.interaction_id: pair for pair in candidate_report.execution_pairs
        }

        deltas: List[InteractionDiff] = []
        for b_pair in baseline_report.execution_pairs:
            c_pair = candidate_by_id.get(b_pair.interaction_id)
            if c_pair is None:
                deltas.append(
                    InteractionDiff(
                        interaction_id=b_pair.interaction_id,
                        classification=DiffClassification.FAILED,
                        status_code_changed=False,
                        old_status=(
                            b_pair.replayed_response.status_code
                            if b_pair.replayed_response
                            else None
                        ),
                        latency_delta_ms=0.0,
                        error_message="Missing candidate execution pair.",
                    )
                )
                continue
            deltas.append(self.comparator.compare_execution_pairs(b_pair, c_pair))

        report = self._build_report(
            session_id=baseline_report.session_id,
            baseline_target=baseline_report.target_url,
            candidate_target=candidate_report.target_url,
            deltas=deltas,
        )
        saved_path = self._save_report(report)
        return report, saved_path

    def compare_single_report(
        self,
        report: ReplaySessionReport,
        baseline_target: str = "recorded",
    ) -> tuple[SessionDiffReport, Path]:
        """Compare recorded original responses against replayed responses in one report."""
        deltas: List[InteractionDiff] = []
        for pair in report.execution_pairs:
            if not pair.is_success or pair.replayed_response is None:
                deltas.append(
                    InteractionDiff(
                        interaction_id=pair.interaction_id,
                        classification=DiffClassification.FAILED,
                        status_code_changed=False,
                        old_status=pair.original_response.status_code,
                        latency_delta_ms=0.0,
                        error_message=pair.error_message
                        or "Candidate replay failed without a response.",
                    )
                )
            else:
                deltas.append(
                    self.comparator.compare_responses(
                        baseline=pair.original_response,
                        candidate=pair.replayed_response,
                        interaction_id=pair.interaction_id,
                    )
                )

        diff_report = self._build_report(
            session_id=report.session_id,
            baseline_target=baseline_target,
            candidate_target=report.target_url,
            deltas=deltas,
        )
        saved_path = self._save_report(diff_report)
        return diff_report, saved_path


class UnifiedSessionComparator(SessionComparisonRunner):
    """Dual-layer comparator merging backend HTTP diffs with frontend DOM/UI diffs."""

    def __init__(
        self,
        api_comparator: Optional[DifferentialComparator] = None,
        dom_comparator: Optional[DOMComparator] = None,
        verifications_dir: Path | str = Path("./.assay/verifications"),
    ) -> None:
        """Initialize the unified full-stack session comparator.

        Args:
            api_comparator: ``DifferentialComparator`` for HTTP payload/header/status diffs.
            dom_comparator: Optional ``DOMComparator`` for frontend UI diffs.
            verifications_dir: Directory where ``verify_<id>.json`` reports are persisted.
        """
        super().__init__(comparator=api_comparator, verifications_dir=verifications_dir)
        self.dom_comparator = dom_comparator

    def compare_dual_runs(
        self,
        baseline_report: ReplaySessionReport,
        candidate_report: ReplaySessionReport,
        baseline_ui_actions: Optional[
            Sequence[BrowserAction | UIExecutionOutcome | Dict[str, Any]]
        ] = None,
        candidate_ui_outcomes: Optional[
            Sequence[BrowserAction | UIExecutionOutcome | Dict[str, Any]]
        ] = None,
        candidate_ui_deltas: Optional[List[UIDelta]] = None,
    ) -> tuple[SessionDiffReport, Path]:
        """Compare API replay reports and UI execution outcomes into one ``SessionDiffReport``.

        Args:
            baseline_report: Baseline API replay report.
            candidate_report: Candidate API replay report.
            baseline_ui_actions: Optional baseline UI actions/snapshots.
            candidate_ui_outcomes: Optional candidate UI execution outcomes.
            candidate_ui_deltas: Optional pre-collected UI deltas (e.g. from ``UIActionReplayer``).

        Returns:
            Tuple of ``(SessionDiffReport, saved_report_path)``.
        """
        candidate_by_id: Dict[str, ReplayExecutionPair] = {
            pair.interaction_id: pair for pair in candidate_report.execution_pairs
        }

        deltas: List[InteractionDiff] = []
        trace_to_diff_idx: Dict[str, int] = {}

        for b_pair in baseline_report.execution_pairs:
            c_pair = candidate_by_id.get(b_pair.interaction_id)
            if c_pair is None:
                diff = InteractionDiff(
                    interaction_id=b_pair.interaction_id,
                    classification=DiffClassification.FAILED,
                    status_code_changed=False,
                    old_status=(
                        b_pair.replayed_response.status_code
                        if b_pair.replayed_response
                        else None
                    ),
                    latency_delta_ms=0.0,
                    error_message="Missing candidate execution pair.",
                )
            else:
                diff = self.comparator.compare_execution_pairs(b_pair, c_pair)

            idx = len(deltas)
            deltas.append(diff)
            if b_pair.request.trace_id:
                trace_to_diff_idx[b_pair.request.trace_id] = idx

        ui_unchanged = 0
        ui_changed = 0
        ui_failed = 0

        dom_comp = self.dom_comparator
        if dom_comp is None and (baseline_ui_actions or candidate_ui_outcomes):
            dom_comp = DOMComparator(
                baseline_actions=baseline_ui_actions,
                candidate_outcomes=candidate_ui_outcomes,
            )

        uncorrelated_ui_deltas: List[UIDelta] = []
        if dom_comp is not None:
            for trace_id, step_ui_deltas in dom_comp.compare_all():
                if not step_ui_deltas:
                    ui_unchanged += 1
                    continue

                if any(
                    d.change_type in {"element_missing", "playback_error"}
                    for d in step_ui_deltas
                ):
                    ui_failed += 1
                else:
                    ui_changed += 1

                if trace_id and trace_id in trace_to_diff_idx:
                    target_diff = deltas[trace_to_diff_idx[trace_id]]
                    target_diff.ui_diffs.extend(step_ui_deltas)
                    if target_diff.classification == DiffClassification.UNCHANGED:
                        target_diff.classification = DiffClassification.CHANGED
                else:
                    uncorrelated_ui_deltas.extend(step_ui_deltas)

        if candidate_ui_deltas:
            ui_failed += sum(
                1
                for d in candidate_ui_deltas
                if d.change_type in {"element_missing", "playback_error", "page_error"}
            )
            ui_changed += sum(
                1
                for d in candidate_ui_deltas
                if d.change_type not in {"element_missing", "playback_error", "page_error"}
            )
            uncorrelated_ui_deltas.extend(candidate_ui_deltas)

        if uncorrelated_ui_deltas:
            has_failure = any(
                d.change_type in {"element_missing", "playback_error", "page_error"}
                for d in uncorrelated_ui_deltas
            )
            deltas.append(
                InteractionDiff(
                    interaction_id="ui-journey",
                    classification=(
                        DiffClassification.FAILED if has_failure else DiffClassification.CHANGED
                    ),
                    status_code_changed=False,
                    ui_diffs=uncorrelated_ui_deltas,
                )
            )

        report = self._build_report(
            session_id=baseline_report.session_id,
            baseline_target=baseline_report.target_url,
            candidate_target=candidate_report.target_url,
            deltas=deltas,
        )
        report.ui_unchanged_count = ui_unchanged
        report.ui_changed_count = ui_changed
        report.ui_failed_count = ui_failed

        saved_path = self._save_report(report)
        return report, saved_path
