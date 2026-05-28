"""Browser lifecycle and session management.

Design choice: ONE persistent Playwright browser per process, but a NEW
BrowserContext per session_name. Each session_name maps to a storage_state JSON
file on disk that holds cookies + localStorage.

Mental model:
- Browser = the chrome.exe process
- Context = a fresh incognito profile inside that process
- Page = a single tab inside a context

Why this layering: contexts are cheap to create (~50ms), browsers are expensive
(~1-2s). Reusing the browser keeps tool calls fast; isolating contexts per
session keeps GitHub cookies separate from your AT&T portal cookies.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .logging_config import domain_of, get_logger

log = get_logger("browser")


class BrowserManager:
    """Singleton-ish: one Playwright + one Browser, many named contexts."""

    def __init__(self, sessions_dir: Path, headless: bool = True, default_timeout_ms: int = 30000):
        self.sessions_dir = sessions_dir
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms

        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._pages: dict[str, Page] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._playwright is not None:
            return
        log.info("playwright_starting", headless=self.headless)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        log.info("playwright_started", browser_version=self._browser.version)

    async def stop(self) -> None:
        log.info("playwright_stopping", active_contexts=len(self._contexts))
        for name, ctx in list(self._contexts.items()):
            try:
                await self._save_state(name, ctx)
                await ctx.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("context_close_failed", session=name, error=str(exc))
        self._contexts.clear()
        self._pages.clear()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._playwright = None
        self._browser = None
        log.info("playwright_stopped")

    # ------------------------------------------------------------------
    # Session / context handling
    # ------------------------------------------------------------------

    def _state_path(self, session_name: str) -> Path:
        # Sanitize: only allow alnum + dash + underscore. Prevents path traversal.
        safe = "".join(c for c in session_name if c.isalnum() or c in ("-", "_"))
        if not safe:
            raise ValueError(f"Invalid session name: {session_name!r}")
        return self.sessions_dir / f"{safe}.json"

    async def get_context(self, session_name: str) -> BrowserContext:
        """Return (creating if needed) a BrowserContext for the named session."""
        await self.start()
        async with self._lock:
            if session_name in self._contexts:
                return self._contexts[session_name]

            state_path = self._state_path(session_name)
            kwargs: dict[str, Any] = {"viewport": {"width": 1366, "height": 768}}
            if state_path.exists():
                log.info("session_loading", session=session_name, path=str(state_path))
                kwargs["storage_state"] = str(state_path)
            else:
                log.info("session_new", session=session_name)

            assert self._browser is not None
            ctx = await self._browser.new_context(**kwargs)
            ctx.set_default_timeout(self.default_timeout_ms)
            self._contexts[session_name] = ctx
            return ctx

    async def get_page(self, session_name: str) -> Page:
        """Return (creating if needed) the active page for a session."""
        if session_name in self._pages and not self._pages[session_name].is_closed():
            return self._pages[session_name]
        ctx = await self.get_context(session_name)
        page = await ctx.new_page()
        self._pages[session_name] = page
        log.info("page_created", session=session_name)
        return page

    async def save_session(self, session_name: str) -> Path:
        """Persist cookies/localStorage for a session to disk."""
        if session_name not in self._contexts:
            raise ValueError(f"No active context for session '{session_name}'")
        return await self._save_state(session_name, self._contexts[session_name])

    async def _save_state(self, session_name: str, ctx: BrowserContext) -> Path:
        path = self._state_path(session_name)
        await ctx.storage_state(path=str(path))
        log.info("session_saved", session=session_name, path=str(path))
        return path

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return metadata for every saved session on disk."""
        out = []
        for f in sorted(self.sessions_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text())
                cookie_count = len(data.get("cookies", []))
                origin_count = len(data.get("origins", []))
            except Exception:
                cookie_count = -1
                origin_count = -1
            out.append({
                "name": f.stem,
                "path": str(f),
                "cookies": cookie_count,
                "origins": origin_count,
                "active": f.stem in self._contexts,
            })
        return out

    async def close_session(self, session_name: str, save: bool = True) -> None:
        """Close an active session, optionally saving state first."""
        if session_name not in self._contexts:
            return
        ctx = self._contexts.pop(session_name)
        self._pages.pop(session_name, None)
        if save:
            await self._save_state(session_name, ctx)
        await ctx.close()
        log.info("session_closed", session=session_name)

    async def goto(self, session_name: str, url: str, wait_until: str = "domcontentloaded") -> Page:
        """Navigate the session's page to a URL and return the page."""
        page = await self.get_page(session_name)
        log.info("navigate_start", session=session_name, url_domain=domain_of(url))
        response = await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        status = response.status if response else None
        log.info("navigate_done", session=session_name, url_domain=domain_of(url), status=status)
        return page
