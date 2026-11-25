"""DOM and UI behavioral comparator for detecting frontend regressions."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from assay.models.diff import UIDelta
from assay.models.recording import BrowserAction

logger = logging.getLogger(__name__)


class UIExecutionOutcome(BaseModel):
    """Captured browser state snapshot after executing or observing a UI action."""

    action_type: str = "navigate"
    selector: str = "window"
    value: Optional[str] = None
    trace_id: Optional[str] = None
    current_url: Optional[str] = None
    status_code: Optional[int] = None
    page_title: Optional[str] = None
    visible_elements: Dict[str, bool] = Field(default_factory=dict)
    element_texts: Dict[str, str] = Field(default_factory=dict)
    error_banners: List[str] = Field(default_factory=list)


def _extract_route_path(url_or_path: Optional[str]) -> Optional[str]:
    """Normalize a URL or path string to its route path (plus query if present)."""
    if not url_or_path:
        return None
    parsed = urlsplit(url_or_path.strip())
    if parsed.scheme in {"http", "https"}:
        path = parsed.path or "/"
        return f"{path}?{parsed.query}" if parsed.query else path
    return url_or_path.strip()


def _coerce_outcome(
    item: BrowserAction | UIExecutionOutcome | Dict[str, Any],
) -> UIExecutionOutcome:
    """Normalize a ``BrowserAction``, ``UIExecutionOutcome``, or dict into one model."""
    if isinstance(item, UIExecutionOutcome):
        return item
    if isinstance(item, BrowserAction):
        return UIExecutionOutcome(
            action_type=item.action_type,
            selector=item.selector,
            value=item.value,
            trace_id=item.trace_id,
            current_url=item.value if item.action_type.lower() == "navigate" else None,
        )
    return UIExecutionOutcome.model_validate(item)


class DOMComparator:
    """Compares baseline and candidate frontend UI actions and DOM state snapshots."""

    def __init__(
        self,
        baseline_actions: Optional[
            Sequence[BrowserAction | UIExecutionOutcome | Dict[str, Any]]
        ] = None,
        candidate_outcomes: Optional[
            Sequence[BrowserAction | UIExecutionOutcome | Dict[str, Any]]
        ] = None,
    ) -> None:
        """Initialize the DOM comparator.

        Args:
            baseline_actions: Ordered baseline browser actions or UI snapshots.
            candidate_outcomes: Ordered candidate browser execution outcomes.
        """
        self.baseline_actions = [_coerce_outcome(item) for item in (baseline_actions or [])]
        self.candidate_outcomes = [_coerce_outcome(item) for item in (candidate_outcomes or [])]

    def compare_ui_states(
        self,
        baseline_action: BrowserAction | UIExecutionOutcome | Dict[str, Any],
        candidate_action: BrowserAction | UIExecutionOutcome | Dict[str, Any],
    ) -> List[UIDelta]:
        """Compare a single baseline UI state against a candidate UI state.

        Args:
            baseline_action: Baseline ``BrowserAction`` or ``UIExecutionOutcome``.
            candidate_action: Candidate ``BrowserAction`` or ``UIExecutionOutcome``.

        Returns:
            List of ``UIDelta`` objects describing route, element, or text regressions.
        """
        base = _coerce_outcome(baseline_action)
        cand = _coerce_outcome(candidate_action)
        deltas: List[UIDelta] = []

        # 1. Evaluate route / redirect URLs and HTTP status for navigation.
        base_route = _extract_route_path(base.current_url or (
            base.value if base.action_type.lower() == "navigate" else None
        ))
        cand_route = _extract_route_path(cand.current_url or (
            cand.value if cand.action_type.lower() == "navigate" else None
        ))

        if cand.status_code is not None and cand.status_code >= 400:
            deltas.append(
                UIDelta(
                    selector=base.selector or "window",
                    change_type="url_redirect_mismatch",
                    expected_state=f"{base_route or '/'} (HTTP {base.status_code or 200})",
                    actual_state=f"{cand_route or '/'} (HTTP {cand.status_code})",
                )
            )
        elif base_route is not None and cand_route is not None and base_route != cand_route:
            deltas.append(
                UIDelta(
                    selector=base.selector or "window",
                    change_type="url_redirect_mismatch",
                    expected_state=base_route,
                    actual_state=cand_route,
                )
            )

        # 2. Evaluate page title regressions.
        if (
            base.page_title is not None
            and cand.page_title is not None
            and base.page_title != cand.page_title
        ):
            deltas.append(
                UIDelta(
                    selector="title",
                    change_type="value_mismatch",
                    expected_state=base.page_title,
                    actual_state=cand.page_title,
                )
            )

        # 3. Evaluate DOM element visibility/presence.
        all_selectors = sorted(set(base.visible_elements) | set(cand.visible_elements))
        for selector in all_selectors:
            base_vis = base.visible_elements.get(selector)
            cand_vis = cand.visible_elements.get(selector)
            if base_vis is True and not cand_vis:
                deltas.append(
                    UIDelta(
                        selector=selector,
                        change_type="element_missing",
                        expected_state="VISIBLE",
                        actual_state="MISSING" if cand_vis is None else "HIDDEN",
                    )
                )
            elif base_vis is False and cand_vis is True:
                deltas.append(
                    UIDelta(
                        selector=selector,
                        change_type="visibility_mismatch",
                        expected_state="HIDDEN",
                        actual_state="VISIBLE",
                    )
                )

        # 4. Evaluate form validation text and element text content.
        all_text_selectors = sorted(set(base.element_texts) | set(cand.element_texts))
        for selector in all_text_selectors:
            base_text = base.element_texts.get(selector)
            cand_text = cand.element_texts.get(selector)
            if base_text != cand_text:
                deltas.append(
                    UIDelta(
                        selector=selector,
                        change_type="value_mismatch",
                        expected_state=base_text,
                        actual_state=cand_text,
                    )
                )

        # 5. Evaluate unexpected error toast banners.
        unexpected_banners = [
            banner for banner in cand.error_banners if banner not in base.error_banners
        ]
        for banner_text in unexpected_banners:
            deltas.append(
                UIDelta(
                    selector="div.error-banner",
                    change_type="value_mismatch",
                    expected_state="HIDDEN",
                    actual_state=banner_text,
                )
            )

        if deltas:
            logger.debug(
                "Detected %d UI delta(s) for selector %s",
                len(deltas),
                base.selector,
            )
        return deltas

    def compare_all(self) -> List[tuple[Optional[str], List[UIDelta]]]:
        """Compare all configured baseline actions against candidate outcomes.

        Returns:
            List of ``(trace_id, list[UIDelta])`` pairs aligned by index.
        """
        results: List[tuple[Optional[str], List[UIDelta]]] = []
        for idx, base_item in enumerate(self.baseline_actions):
            if idx >= len(self.candidate_outcomes):
                results.append(
                    (
                        base_item.trace_id,
                        [
                            UIDelta(
                                selector=base_item.selector,
                                change_type="element_missing",
                                expected_state=f"{base_item.action_type}",
                                actual_state="MISSING_CANDIDATE_STEP",
                            )
                        ],
                    )
                )
                continue
            cand_item = self.candidate_outcomes[idx]
            step_deltas = self.compare_ui_states(base_item, cand_item)
            results.append((base_item.trace_id or cand_item.trace_id, step_deltas))
        return results
