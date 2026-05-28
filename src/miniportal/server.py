"""MiniPortal MCP server.

Architecture:
- FastMCP (the high-level decorator-based MCP framework) exposes each @mcp.tool()
  as a callable the LLM client can invoke.
- One BrowserManager instance is shared across all tool calls.
- Every tool: validates inputs → checks security → does work → logs outcome → returns.

Run via stdio: this script's stdout speaks the MCP protocol, so anything that
needs to be human-readable goes to stderr (via the structlog config).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .browser import BrowserManager
from .cache import ExtractionCache
from .logging_config import configure_logging, domain_of, get_logger
from .security import DomainNotAllowedError, check_url_allowed


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

# Anchor to the repo root (src/miniportal/server.py → up two levels) so these
# paths resolve correctly regardless of what cwd the MCP host sets when it
# spawns this process.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", str(_REPO_ROOT / "config.yaml")))
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", str(_REPO_ROOT / "sessions")))
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", "30000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


CONFIG = _load_config()
ALLOWLIST: list[str] = CONFIG.get("url_allowlist", [])
CACHE_CFG = CONFIG.get("cache", {})
REDACTION = CONFIG.get("log_redaction", [])

configure_logging(level=LOG_LEVEL, redaction_patterns=REDACTION)
log = get_logger("server")

log.info(
    "config_loaded",
    config_path=str(CONFIG_PATH),
    allowlist_size=len(ALLOWLIST),
    cache_enabled=CACHE_CFG.get("enabled", True),
    headless=HEADLESS,
)


# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

mcp = FastMCP("miniportal")
browser_mgr = BrowserManager(
    sessions_dir=SESSIONS_DIR,
    headless=HEADLESS,
    default_timeout_ms=BROWSER_TIMEOUT_MS,
)
cache = ExtractionCache(
    ttl_seconds=CACHE_CFG.get("ttl_seconds", 60),
    max_entries=CACHE_CFG.get("max_entries", 128),
    enabled=CACHE_CFG.get("enabled", True),
)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def navigate(url: str, session_name: str = "default") -> dict:
    """Navigate the browser to a URL using a named session.

    Args:
        url: Full URL to navigate to (must match allowlist).
        session_name: Named browser context. Reuses cookies if the session was previously saved.

    Returns:
        Dict with final URL, HTTP status, and page title.
    """
    try:
        check_url_allowed(url, ALLOWLIST)
    except DomainNotAllowedError as e:
        log.warning("navigate_blocked", reason=str(e))
        return {"ok": False, "error": str(e)}

    page = await browser_mgr.goto(session_name, url)
    title = await page.title()
    return {
        "ok": True,
        "url": page.url,
        "title": title,
        "session": session_name,
    }


@mcp.tool()
async def extract_text(
    selector: str,
    session_name: str = "default",
    all_matches: bool = False,
    max_chars: int = 5000,
) -> dict:
    """Extract text content from the current page using a CSS selector.

    Args:
        selector: CSS selector (e.g., 'h1', '.article-title', '#main p').
        session_name: Which session's active page to read.
        all_matches: If True, return text from all matching elements.
        max_chars: Truncate output to this length to avoid context blowup.
    """
    page = await browser_mgr.get_page(session_name)
    cache_key = ExtractionCache.make_key("extract_text", page.url, selector, all_matches)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        if all_matches:
            elements = await page.query_selector_all(selector)
            texts = []
            for el in elements:
                t = await el.inner_text()
                texts.append(t)
            text = "\n---\n".join(texts)
        else:
            element = await page.query_selector(selector)
            text = await element.inner_text() if element else ""

        truncated = len(text) > max_chars
        text = text[:max_chars]
        result = {
            "ok": True,
            "url_domain": domain_of(page.url),
            "selector": selector,
            "match_count": len(text.split("\n---\n")) if all_matches else (1 if text else 0),
            "truncated": truncated,
            "text": text,
        }
        cache.set(cache_key, result)
        log.info("extract_text_ok", selector=selector, chars=len(text), truncated=truncated)
        return result
    except Exception as e:  # noqa: BLE001
        log.error("extract_text_failed", selector=selector, error=str(e))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def extract_table(
    selector: str = "table",
    session_name: str = "default",
    max_rows: int = 200,
) -> dict:
    """Extract a table as structured rows (list of dicts using first row as headers).

    Args:
        selector: CSS selector for the <table>. Defaults to first table on page.
        session_name: Which session's page to read.
        max_rows: Cap on rows returned.
    """
    page = await browser_mgr.get_page(session_name)
    cache_key = ExtractionCache.make_key("extract_table", page.url, selector, max_rows)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        # Run extraction in-browser via evaluate. Faster than serializing every cell over CDP.
        rows = await page.evaluate(
            """(sel) => {
                const t = document.querySelector(sel);
                if (!t) return null;
                const rows = [...t.querySelectorAll('tr')].map(tr =>
                    [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim())
                );
                return rows;
            }""",
            selector,
        )
        if rows is None:
            return {"ok": False, "error": f"No table found for selector '{selector}'"}

        if not rows:
            return {"ok": True, "headers": [], "rows": [], "row_count": 0}

        headers = rows[0]
        data_rows = rows[1 : 1 + max_rows]
        structured = [
            {headers[i] if i < len(headers) else f"col_{i}": cell for i, cell in enumerate(row)}
            for row in data_rows
        ]
        result = {
            "ok": True,
            "url_domain": domain_of(page.url),
            "headers": headers,
            "rows": structured,
            "row_count": len(structured),
            "truncated": len(rows) - 1 > max_rows,
        }
        cache.set(cache_key, result)
        log.info("extract_table_ok", selector=selector, rows=len(structured))
        return result
    except Exception as e:  # noqa: BLE001
        log.error("extract_table_failed", selector=selector, error=str(e))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def click(selector: str, session_name: str = "default") -> dict:
    """Click an element matching the CSS selector."""
    page = await browser_mgr.get_page(session_name)
    try:
        await page.click(selector)
        log.info("click_ok", selector=selector)
        # Invalidate cache for this page since DOM likely changed.
        cache.clear()
        return {"ok": True, "selector": selector, "url": page.url}
    except Exception as e:  # noqa: BLE001
        log.error("click_failed", selector=selector, error=str(e))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def fill(selector: str, value: str, session_name: str = "default") -> dict:
    """Type a value into an input/textarea.

    Note: the actual value is NOT logged. Only its length is recorded, so secrets
    typed into the page don't leak into log files.
    """
    page = await browser_mgr.get_page(session_name)
    try:
        await page.fill(selector, value)
        log.info("fill_ok", selector=selector, value_length=len(value))
        return {"ok": True, "selector": selector}
    except Exception as e:  # noqa: BLE001
        log.error("fill_failed", selector=selector, error=str(e), value_length=len(value))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def screenshot(session_name: str = "default", full_page: bool = False) -> dict:
    """Capture a screenshot of the current page as base64 PNG.

    Args:
        full_page: If True, captures the entire scrollable area (can be large).
    """
    import base64

    page = await browser_mgr.get_page(session_name)
    try:
        img_bytes = await page.screenshot(full_page=full_page)
        b64 = base64.b64encode(img_bytes).decode("ascii")
        log.info("screenshot_ok", full_page=full_page, bytes=len(img_bytes))
        return {
            "ok": True,
            "url_domain": domain_of(page.url),
            "image_base64": b64,
            "format": "png",
            "bytes": len(img_bytes),
        }
    except Exception as e:  # noqa: BLE001
        log.error("screenshot_failed", error=str(e))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def get_page_info(session_name: str = "default") -> dict:
    """Return current URL, title, and basic info about the active page."""
    page = await browser_mgr.get_page(session_name)
    try:
        return {
            "ok": True,
            "url": page.url,
            "title": await page.title(),
            "session": session_name,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def list_sessions() -> dict:
    """List all saved sessions on disk and indicate which are currently active."""
    return {"ok": True, "sessions": browser_mgr.list_sessions()}


@mcp.tool()
async def save_session(session_name: str = "default") -> dict:
    """Persist the current session's cookies/localStorage to disk."""
    try:
        path = await browser_mgr.save_session(session_name)
        return {"ok": True, "session": session_name, "path": str(path)}
    except Exception as e:  # noqa: BLE001
        log.error("save_session_failed", session=session_name, error=str(e))
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def close_session(session_name: str = "default", save: bool = True) -> dict:
    """Close an active browser context. Optionally save state first."""
    try:
        await browser_mgr.close_session(session_name, save=save)
        return {"ok": True, "session": session_name, "saved": save}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@mcp.tool()
async def get_config() -> dict:
    """Return non-sensitive config (allowlist, cache settings) so Claude knows what's allowed."""
    return {
        "ok": True,
        "allowlist": ALLOWLIST,
        "cache_enabled": cache.enabled,
        "headless": HEADLESS,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP server over stdio."""
    log.info("miniportal_starting", version="0.1.0")
    try:
        mcp.run()
    finally:
        # Graceful shutdown: persist any open sessions.
        try:
            asyncio.run(browser_mgr.stop())
        except Exception as exc:  # noqa: BLE001
            log.warning("shutdown_error", error=str(exc))
        log.info("miniportal_stopped")


if __name__ == "__main__":
    main()
