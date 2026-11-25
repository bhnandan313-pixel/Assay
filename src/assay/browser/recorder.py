"""Playwright-based frontend UI interaction recorder with trace correlation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, Optional, Sequence

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from assay.models.recording import BrowserAction, RecordingSession

logger = logging.getLogger(__name__)

TRACE_HEADER_NAME = "X-Assay-Trace-ID"
SUPPORTED_BROWSERS: frozenset[str] = frozenset({"chromium", "firefox", "webkit"})
DEFAULT_SENSITIVE_UI_FIELDS: tuple[str, ...] = (
    "password",
    "token",
    "secret",
    "credit_card",
    "cvv",
    "number",
    "api_key",
)

_DOM_LISTENER_SCRIPT = r"""
(() => {
    if (window.__assayListenersInstalled) {
        return;
    }
    window.__assayListenersInstalled = true;

    function buildSelector(el) {
        if (!el || el.nodeType !== 1) {
            return "unknown";
        }
        const tag = el.tagName.toLowerCase();
        if (el.id) {
            return `${tag}#${el.id}`;
        }
        const nameAttr = el.getAttribute("name");
        if (nameAttr) {
            return `${tag}[name="${nameAttr}"]`;
        }
        if (el.classList && el.classList.length > 0) {
            const classes = Array.from(el.classList).slice(0, 3).join(".");
            return `${tag}.${classes}`;
        }
        const parent = el.parentElement;
        if (parent) {
            const siblings = Array.from(parent.children).filter(
                (child) => child.tagName === el.tagName
            );
            if (siblings.length > 1) {
                const idx = siblings.indexOf(el) + 1;
                return `${tag}:nth-of-type(${idx})`;
            }
        }
        return tag;
    }

    function emit(actionType, target, value) {
        if (typeof window.__assayRecordEvent !== "function") {
            return;
        }
        const el = target && target.nodeType === 1 ? target : null;
        const inputType = el ? (el.getAttribute("type") || "").toLowerCase() : "";
        const fieldName = el ? (el.getAttribute("name") || el.id || "") : "";
        try {
            window.__assayRecordEvent({
                action_type: actionType,
                selector: buildSelector(el),
                value: value !== undefined && value !== null ? String(value) : null,
                input_type: inputType,
                field_name: fieldName,
            });
        } catch (_err) {
            // Never disrupt application UI handlers.
        }
    }

    document.addEventListener(
        "click",
        (event) => {
            const target =
                event.target && event.target.closest
                    ? event.target.closest("button, a, input, select, textarea, [role='button']") ||
                      event.target
                    : event.target;
            emit("click", target, null);
        },
        true
    );

    document.addEventListener(
        "input",
        (event) => {
            const target = event.target;
            const val = target && "value" in target ? target.value : null;
            emit("input", target, val);
        },
        true
    );

    document.addEventListener(
        "change",
        (event) => {
            const target = event.target;
            const val = target && "value" in target ? target.value : null;
            emit("change", target, val);
        },
        true
    );

    document.addEventListener(
        "submit",
        (event) => {
            emit("submit", event.target, null);
        },
        true
    );
})();
"""


class UIActionRecorder:
    """Records frontend DOM interactions via Playwright and correlates API calls."""

    def __init__(
        self,
        frontend_url: str,
        proxy_url: str,
        session: RecordingSession,
        browser_type: str = "chromium",
        headless: bool = False,
        target_url: Optional[str] = None,
        redacted_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """Initialize the Playwright UI action recorder.

        Args:
            frontend_url: Frontend application URL to open in the browser.
            proxy_url: Local Assay recording proxy URL (e.g. ``http://127.0.0.1:9000``).
            session: Active ``RecordingSession`` receiving captured ``BrowserAction``s.
            browser_type: Playwright engine (``chromium``, ``firefox``, or ``webkit``).
            headless: Whether to launch the browser without a visible window.
            target_url: Optional backend upstream URL; requests targeting it are
                automatically routed through ``proxy_url``.
            redacted_fields: Field names whose input values should be masked.
        """
        normalized_browser = browser_type.strip().lower()
        if normalized_browser not in SUPPORTED_BROWSERS:
            raise ValueError(
                f"Unsupported browser '{browser_type}'. "
                f"Expected one of: {', '.join(sorted(SUPPORTED_BROWSERS))}."
            )

        self.frontend_url = frontend_url.rstrip("/")
        self.proxy_url = proxy_url.rstrip("/")
        self.session = session
        self.browser_type = normalized_browser
        self.headless = headless
        self.target_url = target_url.rstrip("/") if target_url else None
        self.redacted_fields = tuple(
            field.lower() for field in (redacted_fields or DEFAULT_SENSITIVE_UI_FIELDS)
        )

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._start_time: float = time.perf_counter()
        self._current_trace_id: Optional[str] = None
        self._action_lock = asyncio.Lock()

    @property
    def page(self) -> Optional[Page]:
        """Return the active Playwright ``Page``, if started."""
        return self._page

    @property
    def current_trace_id(self) -> Optional[str]:
        """Return the most recently generated UI interaction trace ID."""
        return self._current_trace_id

    def _should_redact_value(self, payload: Dict[str, Any]) -> bool:
        """Return True if the DOM input payload represents a sensitive field."""
        input_type = str(payload.get("input_type") or "").lower()
        if input_type == "password":
            return True

        selector = str(payload.get("selector") or "").lower()
        field_name = str(payload.get("field_name") or "").lower()
        for sensitive in self.redacted_fields:
            if sensitive in field_name or sensitive in selector:
                return True
        return False

    async def _on_dom_event(self, payload: Dict[str, Any]) -> None:
        """Handle a DOM event forwarded from the injected browser listener."""
        action_type = str(payload.get("action_type") or "unknown")
        selector = str(payload.get("selector") or "unknown")
        raw_value = payload.get("value")
        value: Optional[str] = str(raw_value) if raw_value is not None else None
        if value is not None and self._should_redact_value(payload):
            value = "[REDACTED]"

        trace_id = f"trace_{uuid.uuid4().hex[:16]}"
        self._current_trace_id = trace_id
        offset_ms = max((time.perf_counter() - self._start_time) * 1000.0, 0.0)

        action = BrowserAction(
            action_type=action_type,
            selector=selector,
            value=value,
            timestamp_offset_ms=offset_ms,
            trace_id=trace_id,
        )
        async with self._action_lock:
            self.session.ui_actions.append(action)

    async def _handle_route(self, route: Route) -> None:
        """Inject ``X-Assay-Trace-ID`` and optionally route backend calls to the proxy."""
        request = route.request
        headers = dict(request.headers)
        if self._current_trace_id is not None:
            headers[TRACE_HEADER_NAME] = self._current_trace_id

        target_override: Optional[str] = None
        if self.target_url and request.url.startswith(self.target_url):
            suffix = request.url[len(self.target_url) :]
            target_override = f"{self.proxy_url}{suffix}"

        try:
            if target_override is not None:
                await route.continue_(url=target_override, headers=headers)
            else:
                await route.continue_(headers=headers)
        except Exception as exc:
            logger.debug("Playwright route continuation skipped for %s: %s", request.url, exc)

    async def start_browser(self) -> Page:
        """Launch Playwright, attach correlation hooks, and navigate to ``frontend_url``."""
        self._start_time = time.perf_counter()
        self._playwright = await async_playwright().start()
        launcher = getattr(self._playwright, self.browser_type)
        self._browser = await launcher.launch(headless=self.headless)
        self._context = await self._browser.new_context()

        await self._context.expose_function("__assayRecordEvent", self._on_dom_event)
        await self._context.add_init_script(_DOM_LISTENER_SCRIPT)
        await self._context.route("**/*", self._handle_route)

        self._page = await self._context.new_page()
        nav_trace_id = f"trace_{uuid.uuid4().hex[:16]}"
        self._current_trace_id = nav_trace_id
        async with self._action_lock:
            self.session.ui_actions.append(
                BrowserAction(
                    action_type="navigate",
                    selector="window",
                    value=self.frontend_url,
                    timestamp_offset_ms=0.0,
                    trace_id=nav_trace_id,
                )
            )
        await self._page.goto(self.frontend_url, wait_until="domcontentloaded")
        return self._page

    async def stop_browser(self) -> None:
        """Close the Playwright page, context, browser, and runtime cleanly."""
        if self._page is not None:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None

        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
