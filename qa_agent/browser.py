from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

# How long to give a click that might have opened a new tab/window to be
# noticed via the "page" event before finalizing this step's observation.
# The event's own dispatch latency (Chromium -> CDP -> Playwright driver ->
# here) is jittery and occasionally exceeds even a generous fixed wait, so
# this is a best-effort same-step catch, not a guarantee — a popup missed
# here still gets picked up on the *next* action's observation, since
# `_observe()` always finalizes any pending popup first. The executor's
# system prompt tells the LLM not to conclude "fail" off an empty `new_tabs`
# after a click it expected to open one; that's the actual backstop, this is
# just latency-vs-reliability tuning. Fixed cost on every click either way.
NEW_TAB_SETTLE_MS = 1200
NEW_TAB_LOAD_TIMEOUT_MS = 5_000


@dataclass
class ActionObservation:
    ok: bool
    result_text: str
    url: str
    title: str
    snapshot: str
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[str] = field(default_factory=list)
    blocked_navigations: list[str] = field(default_factory=list)
    new_tabs: list[dict] = field(default_factory=list)


def _origin(url: str) -> tuple[str, str]:
    p = urlparse(url)
    return (p.scheme, p.netloc)


class BrowserSession:
    """Wraps a single Playwright page, restricted to one origin, with
    accumulating console/page-error/request-failure buffers drained on each
    ``observe()`` call so every tool result carries only *new* events.
    """

    def __init__(
        self,
        headless: bool = True,
        nav_timeout_ms: int = 30_000,
        action_timeout_ms: int = 10_000,
        channel: str = "chrome",
    ):
        self.headless = headless
        self.channel = channel
        self.nav_timeout_ms = nav_timeout_ms
        self.action_timeout_ms = action_timeout_ms

        self._playwright = None
        self._browser = None
        self._context = None
        self.page: Page | None = None
        self.allowed_origin: tuple[str, str] | None = None
        self._initial_url: str | None = None

        self._console_buf: list[str] = []
        self._page_error_buf: list[str] = []
        self._request_failed_buf: list[str] = []
        self._blocked_nav_buf: list[str] = []
        self._new_tab_buf: list[dict] = []
        self._pending_popups: list[Page] = []

    def __enter__(self) -> "BrowserSession":
        self._playwright = sync_playwright().start()
        launch_kwargs = {"headless": self.headless}
        if self.channel != "chromium":
            launch_kwargs["channel"] = self.channel
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(self.action_timeout_ms)
        self.page = self._context.new_page()
        self._context.on("page", self._on_new_page)
        self.page.on("console", lambda msg: self._console_buf.append(f"[{msg.type}] {msg.text}") if msg.type == "error" else None)
        self.page.on("pageerror", lambda exc: self._page_error_buf.append(str(exc)))
        self.page.on("requestfailed", self._on_request_failed)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _on_request_failed(self, request) -> None:
        failure = request.failure
        self._request_failed_buf.append(f"{request.method} {request.url} -> {failure}")

    def _on_new_page(self, popup_page) -> None:
        """Fires when a new tab/window is created (target="_blank" links,
        window.open, ctrl+click, ...). Playwright only attaches to and tracks
        such pages while a "page" listener is registered — without one,
        context.pages never grows and the popup is invisible to us entirely.

        Deliberately does no blocking work here: this runs on Playwright's
        internal dispatch thread, and calling slow sync methods (wait_for_load_state,
        aria_snapshot) from inside it was observed to stall *other*, unrelated
        Playwright calls made from the main thread for several seconds —
        likely contention on the single driver connection. So this just
        records the page; `_finalize_pending_popups` does the real work,
        called from the main thread.
        """
        if popup_page is self.page:
            return
        self._pending_popups.append(popup_page)

    def _finalize_pending_popups(self) -> None:
        """Main-thread counterpart to `_on_new_page`: waits for each pending
        popup to load, captures its url/title/snapshot, then closes it.
        """
        while self._pending_popups:
            popup_page = self._pending_popups.pop(0)
            try:
                popup_page.wait_for_load_state("load", timeout=NEW_TAB_LOAD_TIMEOUT_MS)
            except PlaywrightError:
                pass
            info = {"url": "about:blank", "title": "", "snapshot": ""}
            try:
                info["url"] = popup_page.url
                info["title"] = popup_page.title()
                info["snapshot"] = popup_page.aria_snapshot(mode="ai")
            except PlaywrightError:
                pass
            try:
                popup_page.close()
            except PlaywrightError:
                pass
            self._new_tab_buf.append(info)

    def _wait_for_possible_new_tab(self) -> None:
        """After a click, give the "page" event time to arrive and be noticed
        by `_on_new_page`, then finalize it on the main thread.

        This can't be made adaptive (e.g. poll and exit early once nothing
        shows up quickly): the event's own dispatch latency — Chromium to
        CDP to the Playwright driver to this process — was measured at
        roughly NEW_TAB_SETTLE_MS even for a trivial handler, so "nothing
        happened yet" and "nothing is coming" aren't distinguishable any
        earlier than that. It's a fixed cost on every click either way.
        """
        self.page.wait_for_timeout(NEW_TAB_SETTLE_MS)
        self._finalize_pending_popups()

    def _enforce_origin(self) -> None:
        """If the page has drifted off the allowed origin (e.g. a link click
        navigated away), revert by going back. Aborting the navigation at the
        network level instead leaves the browser on a chrome-error:// page, so
        we let it happen and then recover.
        """
        if not self.allowed_origin or self.page is None:
            return
        url = self.page.url
        if not url.startswith(("http://", "https://")):
            return
        if _origin(url) == self.allowed_origin:
            return
        self._blocked_nav_buf.append(url)
        try:
            self.page.go_back(timeout=self.nav_timeout_ms)
        except PlaywrightError:
            pass
        if self.page.url == url or _origin(self.page.url) != self.allowed_origin:
            try:
                self.page.goto(self._initial_url, timeout=self.nav_timeout_ms)
            except PlaywrightError:
                pass

    def capture_storage_state(self) -> dict:
        """Snapshots cookies + localStorage for every origin visited so far.
        Used to pin down what "reset to the initial state" should actually
        mean: not just the same URL, but the same cookies/localStorage too —
        otherwise anything a page stores client-side (a language switch, a
        dismissed-banner flag, ...) silently survives a plain re-navigate,
        since it's the same browser context/storage. Login cookies captured
        here deliberately *do* survive resets — the precondition runs once,
        this is the baseline every case resets back to.
        """
        try:
            return self._context.storage_state()
        except PlaywrightError:
            return {"cookies": [], "origins": []}

    def restore_storage_state(self, state: dict) -> None:
        """Clears and restores cookies to exactly `state`. Call before
        navigating — cookies must be in place for the page's first request,
        not patched in after it already loaded and read them.
        """
        try:
            self._context.clear_cookies()
        except PlaywrightError:
            pass
        cookies = state.get("cookies") or []
        if cookies:
            try:
                self._context.add_cookies(cookies)
            except PlaywrightError:
                pass

    def reset_local_storage(self, state: dict) -> None:
        """Clears localStorage/sessionStorage on the current page and
        restores whatever `state` (from capture_storage_state) had for this
        origin. Must be called after navigating to the target page — and the
        caller should navigate again afterward, since scripts that read
        localStorage at load time won't notice a change made after the fact.
        """
        items: list[dict] = []
        current_origin = _origin(self.page.url)
        for entry in state.get("origins") or []:
            if _origin(entry.get("origin", "")) == current_origin:
                items = entry.get("localStorage") or []
                break
        try:
            self.page.evaluate(
                "(items) => { "
                "try { localStorage.clear(); } catch (e) {} "
                "try { sessionStorage.clear(); } catch (e) {} "
                "items.forEach(({name, value}) => { try { localStorage.setItem(name, value); } catch (e) {} }); "
                "}",
                items,
            )
        except PlaywrightError:
            pass

    def navigate(self, url: str) -> ActionObservation:
        self.allowed_origin = _origin(url)
        self._initial_url = url
        try:
            self.page.goto(url, timeout=self.nav_timeout_ms, wait_until="load")
            return self._observe(ok=True, result_text=f"Navigated to {url}")
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return self._observe(ok=False, result_text=f"Navigation error: {exc}")

    def snapshot_text(self) -> str:
        try:
            return self.page.aria_snapshot(mode="ai")
        except PlaywrightError as exc:
            return f"<snapshot unavailable: {exc}>"

    def _drain(self, buf: list[str]) -> list[str]:
        items = list(buf)
        buf.clear()
        return items

    def _observe(self, ok: bool, result_text: str) -> ActionObservation:
        self._finalize_pending_popups()
        self._enforce_origin()
        return ActionObservation(
            ok=ok,
            result_text=result_text,
            url=self.page.url,
            title=self.page.title(),
            snapshot=self.snapshot_text(),
            console_errors=self._drain(self._console_buf),
            page_errors=self._drain(self._page_error_buf),
            request_failures=self._drain(self._request_failed_buf),
            blocked_navigations=self._drain(self._blocked_nav_buf),
            new_tabs=self._drain(self._new_tab_buf),
        )

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path, full_page=True, timeout=self.action_timeout_ms)
        except PlaywrightError:
            pass

    def act(self, tool_name: str, args: dict) -> ActionObservation:
        try:
            ref = args.get("ref")
            locator = self.page.locator(f"aria-ref={ref}") if ref else None

            if tool_name == "browser_click":
                locator.click(timeout=self.action_timeout_ms)
                self._wait_for_possible_new_tab()
                text = f"Clicked '{args.get('description', ref)}'"
            elif tool_name == "browser_fill":
                locator.fill(args.get("text", ""), timeout=self.action_timeout_ms)
                text = f"Filled '{args.get('description', ref)}' with given text"
            elif tool_name == "browser_select_option":
                values = args.get("values") or []
                try:
                    locator.select_option(label=values, timeout=self.action_timeout_ms)
                except PlaywrightError:
                    locator.select_option(value=values, timeout=self.action_timeout_ms)
                text = f"Selected {values} in '{args.get('description', ref)}'"
            elif tool_name == "browser_check":
                locator.set_checked(bool(args.get("checked", True)), timeout=self.action_timeout_ms)
                text = f"Set checked={args.get('checked')} on '{args.get('description', ref)}'"
            elif tool_name == "browser_hover":
                locator.hover(timeout=self.action_timeout_ms)
                text = f"Hovered '{args.get('description', ref)}'"
            elif tool_name == "browser_press_key":
                self.page.keyboard.press(args.get("key", "Enter"))
                text = f"Pressed key {args.get('key')}"
            elif tool_name == "browser_wait":
                seconds = min(float(args.get("seconds", 1)), 5.0)
                self.page.wait_for_timeout(seconds * 1000)
                text = f"Waited {seconds}s"
            elif tool_name == "browser_go_back":
                self.page.go_back(timeout=self.nav_timeout_ms)
                text = "Navigated back"
            else:
                return self._observe(ok=False, result_text=f"Unknown tool: {tool_name}")

            obs = self._observe(ok=True, result_text=text)
            if obs.blocked_navigations:
                obs.result_text += " (note: this navigated off the allowed origin; reverted to stay in scope)"
            for tab in obs.new_tabs:
                obs.result_text += (
                    f" (note: this opened a new tab/window at {tab['url']!r} (title: {tab['title']!r}); "
                    "it was inspected and closed — testing continues on the original tab. "
                    "See 'new_tabs' in this observation for its content.)"
                )
            return obs
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            return self._observe(ok=False, result_text=f"Action failed: {exc}")
