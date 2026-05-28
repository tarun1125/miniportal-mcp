"""One-time interactive login script.

Run this BEFORE starting the MCP server, for each site you want logged-in access to.

Flow:
  1. Opens a non-headless browser window.
  2. Navigates to the URL you provide.
  3. You log in manually (handle 2FA, captcha, RSA token, whatever).
  4. Press ENTER in the terminal when you see you're logged in.
  5. Script saves cookies + localStorage to sessions/<name>.json
  6. From now on, the MCP server can load that file and skip the login dance.

Why a separate script: logins involve human input. Putting them inside the MCP
server would require the LLM to drive 2FA flows, which is fragile and a security
risk. Keep human-in-the-loop steps OUTSIDE the autonomous loop.

Usage:
    python scripts/login_capture.py --url https://github.com/login --name github
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright


async def capture(url: str, name: str, sessions_dir: Path) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_"))
    if not safe:
        print(f"ERROR: invalid session name: {name!r}", file=sys.stderr)
        sys.exit(2)
    out_path = sessions_dir / f"{safe}.json"

    async with async_playwright() as p:
        # Non-headless so the user can interact.
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print(f"[1/3] Opening: {url}")
        await page.goto(url)

        print()
        print("[2/3] Log in manually in the opened window.")
        print("      Handle 2FA / captcha / any consent screens.")
        print("      When you see the post-login dashboard, return here.")
        print()
        try:
            input("      Press ENTER once you are logged in... ")
        except KeyboardInterrupt:
            print("\nAborted.")
            await browser.close()
            sys.exit(130)

        # Defensive: re-check page is still alive.
        if page.is_closed():
            print("ERROR: browser page was closed before save. Try again.", file=sys.stderr)
            await browser.close()
            sys.exit(1)

        await context.storage_state(path=str(out_path))
        print(f"[3/3] Session saved: {out_path}")
        print()
        print("You can close the browser. The MCP server will reuse this session.")

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture a logged-in browser session for reuse by MiniPortal MCP.")
    parser.add_argument("--url", required=True, help="Login page URL (e.g., https://github.com/login)")
    parser.add_argument("--name", required=True, help="Session name (alphanumeric, dash, underscore)")
    parser.add_argument(
        "--sessions-dir",
        default="./sessions",
        help="Directory to store session JSON files (default: ./sessions)",
    )
    args = parser.parse_args()

    asyncio.run(capture(args.url, args.name, Path(args.sessions_dir)))


if __name__ == "__main__":
    main()
