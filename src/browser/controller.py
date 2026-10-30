"""Per-session Playwright browser with policy checks and an approval ledger."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .page_reader import (
    TEXT_EXTRACTION_SCRIPT,
    ElementRef,
    format_page_text,
    lead_text,
    normalize_text,
    parse_snapshot,
)
from .policy import (
    BrowserPolicyError,
    assert_url_allowed,
    describe_consequence,
    is_risky_action,
)

logger = logging.getLogger("browser")

_MAX_TEXT_CHARS = 6000


class BrowserError(Exception):
    """Raised for a browser failure the agent can explain to the user."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class BrowserSettings:
    headless: bool = True
    allowlist: tuple[str, ...] = ()
    allow_private_hosts: bool = False
    max_elements: int = 60
    nav_timeout_ms: int = 20_000
    action_timeout_ms: int = 5_000
    settle_timeout_ms: int = 2_500
    block_media: bool = False
    locale: str = "en-GB"
    timezone_id: str = "Europe/London"
    viewport_width: int = 1280
    viewport_height: int = 800
    confirmation_ttl: float = 60.0
    max_pending: int = 3
    no_sandbox: bool = False

    @classmethod
    def from_env(cls) -> BrowserSettings:
        allowlist = tuple(
            entry.strip()
            for entry in os.getenv("BROWSER_ALLOWLIST", "").split(",")
            if entry.strip()
        )
        return cls(
            headless=_env_bool("BROWSER_HEADLESS", True),
            allowlist=allowlist,
            allow_private_hosts=_env_bool("BROWSER_ALLOW_PRIVATE", False),
            max_elements=_env_int("BROWSER_MAX_ELEMENTS", 60),
            nav_timeout_ms=_env_int("BROWSER_NAV_TIMEOUT_MS", 20_000),
            action_timeout_ms=_env_int("BROWSER_ACTION_TIMEOUT_MS", 5_000),
            block_media=_env_bool("BROWSER_BLOCK_MEDIA", False),
            no_sandbox=_env_bool("BROWSER_NO_SANDBOX", False),
        )


@dataclass
class BrowserUserData:
    """Session state shared with every browser tool through RunContext."""

    browser: BrowserController


@dataclass
class PageView:
    title: str
    url: str
    refs: list[ElementRef] = field(default_factory=list)
    truncated: bool = False
    text: str = ""

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or self.url


@dataclass
class _PendingAction:
    token: str
    description: str
    expires_at: float
    run: Callable[[], Awaitable[str]]


_FRIENDLY_ERRORS = {
    "ERR_NAME_NOT_RESOLVED": "That site could not be found.",
    "ERR_CONNECTION_REFUSED": "That site refused the connection.",
    "ERR_INTERNET_DISCONNECTED": "The browser has no internet connection.",
    "ERR_CERT": "That site has a certificate problem, so I stopped.",
    "ERR_ABORTED": "The page stopped loading before it finished.",
}

_ENTER_SUBMIT_SCRIPT = """
() => {
  const el = document.activeElement;
  if (!el) return null;
  const form = el.form || el.closest('form');
  if (!form) return null;
  const btn = form.querySelector(
    'button[type="submit"], input[type="submit"], button:not([type])'
  );
  const label = btn
    ? (btn.innerText || btn.value || btn.getAttribute('aria-label'))
    : form.getAttribute('action');
  return label ? String(label).trim() : 'submit the form';
}
"""

_SCROLL_PROGRESS_SCRIPT = """
() => {
  const doc = document.documentElement;
  const height = Math.max(doc.scrollHeight, document.body.scrollHeight);
  const y = window.scrollY || doc.scrollTop || 0;
  const viewport = window.innerHeight || 1;
  const max = Math.max(height - viewport, 1);
  return {y: Math.round(y), height, viewport, percent: Math.round((y / max) * 100)};
}
"""


def _friendly_error(exc: PlaywrightError) -> str:
    text = str(exc)
    for code, message in _FRIENDLY_ERRORS.items():
        if code in text:
            return message
    return "That page did not load. The site may be down or blocking the browser."


class BrowserController:
    """Owns exactly one headless Chromium per voice session."""

    def __init__(self, settings: BrowserSettings | None = None) -> None:
        self.settings = settings or BrowserSettings.from_env()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._start_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._refs: dict[str, ElementRef] = {}
        self._refs_url: str | None = None
        self._pending: dict[str, _PendingAction] = {}
        self._last_dialog: str | None = None
        self._closed = False
        self._background: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ setup

    async def prewarm(self, timeout: float = 45.0) -> None:
        """Launch the browser in the background so the first action is fast.

        Bounded so a slow or hung launch can never delay session shutdown, which
        waits on the same lock.
        """
        try:
            async with asyncio.timeout(timeout):
                async with self._lock:
                    await self._ensure_page()
        except TimeoutError:
            logger.warning("browser prewarm timed out after %.0fs", timeout)
        except Exception:
            logger.warning("browser prewarm failed", exc_info=True)

    async def _ensure_page(self) -> Page:
        if self._closed:
            raise BrowserError("The browser is closed for this session.")
        if self._page is not None and not self._page.is_closed():
            return self._page
        async with self._start_lock:
            if self._page is not None and not self._page.is_closed():
                return self._page
            await self._launch()
        assert self._page is not None
        return self._page

    def _launch_args(self) -> list[str]:
        args = ["--disable-dev-shm-usage"]
        if self.settings.no_sandbox:
            args.append("--no-sandbox")
        return args

    async def _launch(self) -> None:
        logger.info("launching chromium (headless=%s)", self.settings.headless)
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(
                headless=self.settings.headless,
                args=self._launch_args(),
            )
            context = await browser.new_context(
                viewport={
                    "width": self.settings.viewport_width,
                    "height": self.settings.viewport_height,
                },
                locale=self.settings.locale,
                timezone_id=self.settings.timezone_id,
                accept_downloads=False,
                permissions=[],
                color_scheme="light",
            )
            if self.settings.block_media:
                await context.route("**/*", self._block_heavy_requests)
            page = await context.new_page()
        except (Exception, asyncio.CancelledError):
            # Also runs when a prewarm times out mid-launch, so no orphan
            # Playwright process is left behind.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await playwright.stop()
            raise

        page.set_default_timeout(self.settings.action_timeout_ms)
        page.set_default_navigation_timeout(self.settings.nav_timeout_ms)
        page.on("dialog", self._on_dialog)
        page.on("framenavigated", self._on_frame_navigated)

        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page

    async def _block_heavy_requests(self, route: Route) -> None:
        resource_type = route.request.resource_type
        try:
            if resource_type in {"image", "media", "font"}:
                await route.abort()
            else:
                await route.continue_()
        except PlaywrightError:
            pass

    def _on_dialog(self, dialog: object) -> None:
        self._last_dialog = str(getattr(dialog, "message", ""))[:300]

        async def _dismiss() -> None:
            with contextlib.suppress(PlaywrightError):
                await dialog.dismiss()  # type: ignore[attr-defined]

        task = asyncio.create_task(_dismiss())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _on_frame_navigated(self, frame: object) -> None:
        if getattr(frame, "parent_frame", None) is None:
            self._refs.clear()
            self._refs_url = None

    async def aclose(self) -> None:
        self._closed = True
        self._pending.clear()
        self._refs.clear()
        async with self._lock:
            for closer in (self._context, self._browser):
                if closer is not None:
                    with contextlib.suppress(Exception):
                        await closer.close()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
            self._context = None
            self._browser = None
            self._page = None
            self._playwright = None

    # ------------------------------------------------------------- navigation

    async def navigate(self, url: str) -> PageView:
        async with self._lock:
            return await self._navigate_locked(url)

    async def _navigate_locked(self, url: str) -> PageView:
        page = await self._ensure_page()
        target = await assert_url_allowed(
            url,
            allowlist=self.settings.allowlist,
            allow_private_hosts=self.settings.allow_private_hosts,
        )

        try:
            await page.goto(target, wait_until="domcontentloaded")
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                "That page took too long to load, so I stopped."
            ) from exc
        except PlaywrightError as exc:
            raise BrowserError(_friendly_error(exc)) from exc

        await self._settle(page)

        try:
            await assert_url_allowed(
                page.url,
                allowlist=self.settings.allowlist,
                allow_private_hosts=self.settings.allow_private_hosts,
            )
        except BrowserPolicyError:
            with contextlib.suppress(PlaywrightError):
                await page.goto("about:blank")
            raise

        return await self._view_locked(page, include_text=True, text_limit=320)

    async def inspect(self) -> PageView:
        async with self._lock:
            page = await self._ensure_page()
            return await self._view_locked(page, include_text=False)

    async def read(self, offset: int = 0, max_chars: int = 3000) -> str:
        async with self._lock:
            page = await self._ensure_page()
            title = await self._safe_title(page)
            text = await self._extract_text(page)
            return format_page_text(
                title=title,
                url=page.url,
                text=text,
                offset=max(0, offset),
                max_chars=max(500, min(max_chars, _MAX_TEXT_CHARS)),
            )

    async def history(
        self, direction: Literal["back", "forward", "reload"]
    ) -> PageView:
        async with self._lock:
            page = await self._ensure_page()
            try:
                if direction == "reload":
                    await page.reload(wait_until="domcontentloaded")
                elif direction == "forward":
                    result = await page.go_forward(wait_until="domcontentloaded")
                    if result is None:
                        raise BrowserError("There is no forward page in this tab.")
                else:
                    result = await page.go_back(wait_until="domcontentloaded")
                    if result is None:
                        raise BrowserError("There is no previous page in this tab.")
            except PlaywrightTimeoutError as exc:
                raise BrowserError(
                    "That page took too long to load, so I stopped."
                ) from exc
            except PlaywrightError as exc:
                raise BrowserError(_friendly_error(exc)) from exc

            await self._settle(page)
            return await self._view_locked(page, include_text=False)

    # --------------------------------------------------------------- actions

    async def click(self, element: str) -> str:
        async with self._lock:
            return await self._click_locked(element)

    async def _click_locked(self, element: str) -> str:
        page = await self._ensure_page()
        entry = self._require_ref(page, element)
        locator = await self._resolve(page, entry)

        try:
            await locator.scroll_into_view_if_needed()
            await locator.click()
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                f'"{entry.name}" did not respond, so I did not click it. '
                "Inspect the page again."
            ) from exc
        except PlaywrightError as exc:
            raise BrowserError(
                f'Could not click "{entry.name}": {_friendly_error(exc)}'
            ) from exc

        await self._settle(page)
        view = await self._view_locked(page, include_text=False)
        return (
            f'Clicked "{entry.name}". Now on "{view.title}" at {view.host}, '
            f"with {len(view.refs)} interactive elements."
        )

    async def type_text(self, element: str, text: str, submit: bool = False) -> str:
        async with self._lock:
            return await self._type_locked(element, text, submit)

    async def _type_locked(self, element: str, text: str, submit: bool) -> str:
        page = await self._ensure_page()
        entry = self._require_ref(page, element)
        locator = await self._resolve(page, entry)

        try:
            await locator.fill(text)
            if submit:
                await locator.press("Enter")
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                f'"{entry.name}" did not accept any text. Inspect the page again.'
            ) from exc
        except PlaywrightError as exc:
            raise BrowserError(
                f'Could not type into "{entry.name}": {_friendly_error(exc)}'
            ) from exc

        if not submit:
            return f'Typed into "{entry.name}".'

        await self._settle(page)
        view = await self._view_locked(page, include_text=False)
        return (
            f'Typed into "{entry.name}" and submitted. Now on "{view.title}" '
            f"at {view.host}, with {len(view.refs)} interactive elements."
        )

    async def press_key(self, key: str) -> str:
        async with self._lock:
            page = await self._ensure_page()
            try:
                await page.keyboard.press(key)
            except PlaywrightError as exc:
                raise BrowserError(
                    f"That key did not work: {_friendly_error(exc)}"
                ) from exc

            if key.lower() == "enter":
                await self._settle(page)
                view = await self._view_locked(page, include_text=False)
                return (
                    f'Pressed Enter. Now on "{view.title}" at {view.host}, '
                    f"with {len(view.refs)} interactive elements."
                )
            return f"Pressed {key}."

    async def scroll(
        self, direction: Literal["up", "down", "top", "bottom"], amount: int = 1
    ) -> str:
        async with self._lock:
            page = await self._ensure_page()
            amount = max(1, min(amount, 10))
            try:
                if direction in {"top", "bottom"}:
                    target = 0 if direction == "top" else 10**9
                    await page.evaluate("(y) => window.scrollTo(0, y)", target)
                else:
                    delta = int(self.settings.viewport_height * 0.8) * amount
                    if direction == "up":
                        delta = -delta
                    await page.mouse.wheel(0, delta)
            except PlaywrightError as exc:
                raise BrowserError("I could not scroll that page.") from exc

            await asyncio.sleep(0.25)
            try:
                progress = await page.evaluate(_SCROLL_PROGRESS_SCRIPT)
            except PlaywrightError:
                progress = None

            if isinstance(progress, dict):
                percent = progress.get("percent", 0)
                return f"Scrolled {direction}. The page is about {percent} percent of the way down."
            return f"Scrolled {direction}."

    def describe_click(self, element: str) -> str | None:
        """Return a description when this click needs explicit user approval."""
        page = self._page
        if page is None:
            raise BrowserError("The browser is not running yet.")
        entry = self._require_ref(page, element)
        if not is_risky_action(entry.name):
            return None
        return describe_consequence("Click", entry.name, self._host(page))

    def describe_submit(self, element: str, text: str) -> str | None:
        page = self._page
        if page is None:
            raise BrowserError("The browser is not running yet.")
        entry = self._require_ref(page, element)
        # Searching is reversible, so it should not need a yes every time.
        if entry.role == "searchbox" or "search" in entry.name.lower():
            return None
        preview = text if len(text) <= 60 else f"{text[:57]}..."
        return (
            f'Type "{preview}" into the "{entry.name}" '
            f"{entry.role} on {self._host(page)} and submit the form"
        )

    async def describe_enter(self) -> str | None:
        """Return a description when Enter would submit a consequential form."""
        page = self._page
        if page is None:
            return None
        try:
            label = await page.evaluate(_ENTER_SUBMIT_SCRIPT)
        except PlaywrightError:
            return None
        if not label:
            return None
        label = str(label)
        if label == "submit the form" or is_risky_action(label):
            return f'Press Enter to submit "{label}" on {self._host(page)}'
        return None

    # ------------------------------------------------------------ approvals

    def stash(self, description: str, run: Callable[[], Awaitable[str]]) -> str:
        """Store a gated action and return the token the model must echo back."""
        now = time.monotonic()
        self._prune_pending(now)
        while len(self._pending) >= self.settings.max_pending:
            oldest = min(self._pending.values(), key=lambda item: item.expires_at)
            self._pending.pop(oldest.token, None)

        token = secrets.token_hex(3)
        self._pending[token] = _PendingAction(
            token=token,
            description=description,
            expires_at=now + self.settings.confirmation_ttl,
            run=run,
        )
        return token

    async def confirm(self, token: str) -> str:
        """Run a stashed action exactly once."""
        key = token.strip().lower()
        pending = self._pending.pop(key, None)
        if pending is None:
            raise BrowserError(
                "That confirmation code is missing or has already been used. "
                "Ask the user again and start the action once more."
            )
        if pending.expires_at < time.monotonic():
            raise BrowserError("That confirmation expired. Ask the user again.")
        return await pending.run()

    def _prune_pending(self, now: float) -> None:
        expired = [
            token for token, item in self._pending.items() if item.expires_at < now
        ]
        for token in expired:
            self._pending.pop(token, None)

    # -------------------------------------------------------------- internals

    async def _view_locked(
        self,
        page: Page,
        *,
        include_text: bool,
        text_limit: int = 320,
    ) -> PageView:
        title = await self._safe_title(page)
        try:
            snapshot = await page.locator("body").aria_snapshot()
        except PlaywrightError:
            snapshot = ""

        parsed = parse_snapshot(
            snapshot,
            page_url=page.url,
            limit=self.settings.max_elements,
        )
        self._refs = {entry.ref: entry for entry in parsed.refs}
        self._refs_url = page.url

        text = ""
        if include_text:
            text = lead_text(await self._extract_text(page), limit=text_limit)

        return PageView(
            title=title,
            url=page.url,
            refs=parsed.refs,
            truncated=parsed.truncated,
            text=text,
        )

    async def _safe_title(self, page: Page) -> str:
        with contextlib.suppress(PlaywrightError):
            title = (await page.title()).strip()
            if title:
                return title[:160]
        return page.url

    async def _extract_text(self, page: Page) -> str:
        try:
            text = await page.evaluate(TEXT_EXTRACTION_SCRIPT)
        except PlaywrightError:
            return ""
        return normalize_text(str(text or ""))

    async def _settle(self, page: Page) -> None:
        with contextlib.suppress(PlaywrightTimeoutError):
            await page.wait_for_load_state(
                "networkidle", timeout=self.settings.settle_timeout_ms
            )

    def _require_ref(self, page: Page, element: str) -> ElementRef:
        cleaned = (element or "").strip().lower()
        digits = cleaned.lstrip("[").rstrip("]")
        if digits.startswith("e"):
            digits = digits[1:]
        if not digits.isdigit():
            raise BrowserError(
                "That is not a valid element reference. Use a ref like e3 from inspect_page."
            )
        entry = self._refs.get(f"e{int(digits)}")
        if entry is None:
            raise BrowserError(
                "That element is not in the current list. Call inspect_page and use a ref it returns."
            )
        if self._refs_url != page.url:
            raise BrowserError(
                "The page has changed since the last inspection. Call inspect_page again."
            )
        return entry

    async def _resolve(self, page: Page, entry: ElementRef) -> Locator:
        locator = page.get_by_role(entry.role, name=entry.name, exact=True)
        if entry.nth:
            locator = locator.nth(entry.nth)
        try:
            await locator.wait_for(state="visible", timeout=2_000)
        except PlaywrightTimeoutError as exc:
            raise BrowserError(
                f'"{entry.name}" is no longer on the page. Call inspect_page again.'
            ) from exc
        return locator

    @staticmethod
    def _host(page: Page) -> str:
        return urlsplit(page.url).hostname or page.url


__all__ = [
    "BrowserController",
    "BrowserError",
    "BrowserSettings",
    "BrowserUserData",
    "PageView",
]
