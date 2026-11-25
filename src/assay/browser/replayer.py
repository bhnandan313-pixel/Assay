"""Playwright-based headless UI action replayer."""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    ConsoleMessage,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from assay.browser.recorder import SUPPORTED_BROWSERS
from assay.models.diff import UIDelta
from assay.models.recording import BrowserAction, RecordingSession

logger = logging.getLogger(__name__)


class UIActionReplayer:
    """Replays recorded frontend ``BrowserAction`` sequences via Playwright."""

    def __init__(
        self,
        candidate_url: str,
        session: RecordingSession,
        browser_type: str = "chromium",
        headless: bool = True,
        action_delay_ms: float = 100.0,
        timeout_ms: float = 10000.0,
    ) -> None:
        """Initialize the Playwright UI replayer.

        Args:
            candidate_url: Candidate frontend base URL (e.g. ``http://localhost:5174``).
            session: ``RecordingSession`` containing recorded ``ui_actions``.
            browser_type: Playwright engine (``chromium``, ``firefox``, or ``webkit``).
            headless: Whether to run the browser in headless mode.
            action_delay_ms: Explicit delay in milliseconds between UI actions.
            timeout_ms: Playwright selector/navigation timeout in milliseconds.
        """
        normalized_browser = browser_type.strip().lower()
        if normalized_browser not in SUPPORTED_BROWSERS:
            raise ValueError(
                f"Unsupported browser '{browser_type}'. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_BROWSERS))}."
            )

        self.candidate_url = candidate_url.rstrip("/")
        self.session = session
        self.browser_type = normalized_browser
        self.headless = headless
        self.action_delay_ms = action_delay_ms
        self.timeout_ms = timeout_ms

    def _resolve_navigation_url(self, recorded_value: Optional[str]) -> str:
        """Map a recorded navigation URL onto ``self.candidate_url``."""
        if not recorded_value:
            return self.candidate_url
        parsed = urlsplit(recorded_value)
        if parsed.scheme in {"http", "https"}:
            suffix = parsed.path or ""
            if parsed.query:
                suffix = f"{suffix}?{parsed.query}"
            return f"{self.candidate_url}{suffix}"
        if recorded_value.startswith("/"):
            return f"{self.candidate_url}{recorded_value}"
        return f"{self.candidate_url}/{recorded_value}"

    async def _settle_network(self, page: Page) -> None:
        """Wait briefly for network activity to settle without failing if idle already."""
        try:
            await page.wait_for_load_state("networkidle", timeout=min(self.timeout_ms, 5000.0))
        except PlaywrightTimeoutError:
            logger.debug("Network idle wait timed out; continuing UI playback.")

    async def _execute_action(self, page: Page, action: BrowserAction) -> None:
        """Execute a single recorded ``BrowserAction`` on the Playwright page."""
        action_type = action.action_type.lower()
        if action_type == "navigate":
            target_url = self._resolve_navigation_url(action.value)
            logger.info("UI replay navigating to %s", target_url)
            await page.goto(target_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            await self._settle_network(page)
            return

        locator = page.locator(action.selector).first
        await locator.wait_for(state="attached", timeout=self.timeout_ms)

        if action_type == "click":
            logger.info("UI replay clicking %s", action.selector)
            await locator.click(timeout=self.timeout_ms)
            await self._settle_network(page)
        elif action_type in {"input", "change"}:
            value = action.value if action.value is not None else ""
            tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
            if tag_name == "select":
                await locator.select_option(value, timeout=self.timeout_ms)
            else:
                await locator.fill(value, timeout=self.timeout_ms)
        elif action_type == "submit":
            logger.info("UI replay submitting form %s", action.selector)
            await locator.evaluate(
                "el => el.requestSubmit ? el.requestSubmit() : el.submit()"
            )
            await self._settle_network(page)
        else:
            logger.debug("Skipping unsupported UI action type '%s'", action.action_type)

    async def replay_ui_session(self) -> List[UIDelta]:
        """Replay all ``ui_actions`` in chronological order and return any ``UIDelta`` errors."""
        deltas: List[UIDelta] = []
        ordered_actions = sorted(
            self.session.ui_actions,
            key=lambda item: item.timestamp_offset_ms,
        )
        if not ordered_actions:
            logger.info("No UI actions recorded in session %s.", self.session.session_id)
            return deltas

        playwright: Optional[Playwright] = None
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        page: Optional[Page] = None

        try:
            playwright = await async_playwright().start()
            launcher = getattr(playwright, self.browser_type)
            browser = await launcher.launch(headless=self.headless)
            context = await browser.new_context()
            page = await context.new_page()

            def _on_console(msg: ConsoleMessage) -> None:
                if msg.type == "error":
                    logger.warning("Browser console error: %s", msg.text)
                    deltas.append(
                        UIDelta(
                            selector="console",
                            change_type="console_error",
                            expected_state=None,
                            actual_state=msg.text,
                        )
                    )

            def _on_page_error(exc: Exception) -> None:
                logger.warning("Browser page error: %s", exc)
                deltas.append(
                    UIDelta(
                        selector="window",
                        change_type="page_error",
                        expected_state=None,
                        actual_state=str(exc),
                    )
                )

            page.on("console", _on_console)
            page.on("pageerror", _on_page_error)

            for action in ordered_actions:
                try:
                    await self._execute_action(page, action)
                except (PlaywrightTimeoutError, PlaywrightError) as exc:
                    logger.warning(
                        "UI playback error on %s (%s): %s",
                        action.action_type,
                        action.selector,
                        exc,
                    )
                    deltas.append(
                        UIDelta(
                            selector=action.selector,
                            change_type="element_missing",
                            expected_state=f"{action.action_type}({action.value or ''})",
                            actual_state=str(exc),
                        )
                    )
                except Exception as exc:
                    logger.exception(
                        "Unexpected UI playback failure on %s (%s)",
                        action.action_type,
                        action.selector,
                    )
                    deltas.append(
                        UIDelta(
                            selector=action.selector,
                            change_type="playback_error",
                            expected_state=f"{action.action_type}({action.value or ''})",
                            actual_state=str(exc),
                        )
                    )

                if self.action_delay_ms > 0:
                    await asyncio.sleep(self.action_delay_ms / 1000.0)
        finally:
            for resource, close_method in (
                (page, "close"),
                (context, "close"),
                (browser, "close"),
                (playwright, "stop"),
            ):
                if resource is not None:
                    try:
                        await getattr(resource, close_method)()
                    except Exception:
                        pass

        return deltas
