# MiniPortal MCP

A local MCP server that gives Claude (or any MCP client) the ability to drive a real Chromium browser via Playwright — with **persistent logged-in sessions**, **URL allowlisting**, **TTL caching**, and **secret-aware logging**.

Think of it as a small-scale, locally-controlled version of the official Claude Chrome Connector — built so you can later adapt the same patterns inside a locked-down environment where you can't install browser extensions.

## What it does

- Opens a real browser (headless or visible), navigates to URLs, extracts text/tables, clicks, fills forms, screenshots.
- Saves your logged-in state per "session" so you don't re-authenticate every call.
- Refuses to navigate outside an allowlist you control.
- Redacts tokens and passwords from logs.
- Caches extraction results briefly to reduce duplicate scraping.

## Architecture (mental model)

```
+-------------------+         stdio          +----------------------+
|  Claude Desktop / |  <------------------>  |   MiniPortal MCP     |
|  VS Code Copilot  |   JSON-RPC over pipe   |   (FastMCP + Python) |
+-------------------+                        +----------+-----------+
                                                        |
                                                        v
                                              +---------+----------+
                                              |  BrowserManager    |
                                              |  (one Chromium)    |
                                              +---------+----------+
                                                        |
                                          +-------------+--------------+
                                          |             |              |
                                     Context A    Context B      Context C
                                     "github"     "att-portal"   "default"
                                     (cookies)    (cookies)      (cookies)
```

- One **Browser** process per MCP server (cheap to keep alive).
- One **BrowserContext** per named session (isolated cookies).
- One **storage_state.json** per session on disk (survives restarts).

## Quick start (Windows)

### 1. Prerequisites

- Python 3.10+
- Windows 10/11 with PowerShell
- ~500 MB disk for Chromium

### 2. Install

```powershell
cd C:\path\to\miniportal-mcp

# Create and activate venv
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install
pip install -e .

# Install Chromium for Playwright (one-time, ~150 MB download)
python -m playwright install chromium
```

### 3. Configure

```powershell
copy .env.example .env
```

Edit `config.yaml` to set your URL allowlist. The defaults already include `github.com`.

### 4. Capture a logged-in session (one time per site)

```powershell
python scripts\login_capture.py --url https://github.com/login --name github
```

A browser window opens. Log in to GitHub. Press ENTER in the terminal. Your session is saved to `sessions\github.json`.

### 5. Test the server runs

```powershell
python -m miniportal.server
```

You should see structured JSON logs on stderr. Press Ctrl+C to stop. (Direct invocation doesn't do anything useful — MCP clients drive it.)

### 6. Wire up Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` (create if missing):

```json
{
  "mcpServers": {
    "miniportal": {
      "command": "C:\\path\\to\\miniportal-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "miniportal.server"],
      "cwd": "C:\\path\\to\\miniportal-mcp"
    }
  }
}
```

Restart Claude Desktop. In a new chat, you should see `miniportal` listed under the connected tools (look for the hammer/plug icon).

### 7. Wire up VS Code (GitHub Copilot with MCP)

VS Code uses `.vscode/mcp.json` in your workspace, OR a global `mcp.json` in user settings. Create `.vscode/mcp.json`:

```json
{
  "servers": {
    "miniportal": {
      "type": "stdio",
      "command": "C:\\path\\to\\miniportal-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "miniportal.server"],
      "cwd": "C:\\path\\to\\miniportal-mcp"
    }
  }
}
```

Reload VS Code. Open the Copilot chat in **Agent mode** — the miniportal tools should appear in the tools picker.

> Copilot's MCP support is in preview; if it's behind a setting, search for "MCP" in settings and enable it.

## Tools exposed

| Tool | Purpose |
|---|---|
| `navigate(url, session_name)` | Go to a URL on a named session |
| `extract_text(selector, all_matches, max_chars)` | Read text from element(s) |
| `extract_table(selector, max_rows)` | Read a table as structured rows |
| `click(selector)` | Click an element |
| `fill(selector, value)` | Type into an input (value never logged) |
| `screenshot(full_page)` | PNG screenshot as base64 |
| `get_page_info()` | Current URL + title |
| `list_sessions()` | Show saved sessions |
| `save_session(session_name)` | Persist current cookies |
| `close_session(session_name, save)` | Close active context |
| `get_config()` | Show non-sensitive config (allowlist, etc.) |

## Try it out

In Claude Desktop:

> "Use miniportal to navigate to https://github.com/notifications using the `github` session, then list the first 5 notification titles."

Claude should:
1. Call `navigate(url='https://github.com/notifications', session_name='github')`
2. Call `extract_text(selector='.notifications-list .notification-list-item', all_matches=True)`
3. Summarize back to you.

## Security model

| Layer | What it stops |
|---|---|
| URL allowlist (`config.yaml`) | Server refuses to navigate to non-allowlisted domains. Stops accidental or prompt-injected exfiltration. |
| Session isolation (one context per name) | GitHub cookies can't leak into your AT&T portal session. |
| Log redaction (`config.yaml` regex list) | Tokens, passwords in query strings, Authorization headers are masked in logs. |
| Form-fill value never logged | Only the length of typed values is logged, not the value itself. |
| Sessions git-ignored | `sessions/*.json` never accidentally committed. |
| No credential storage in server | Only session cookies. No passwords or tokens stored by the server itself. |

## Design notes (where it's right-fit / under / over engineered)

**Right-fit:**
- FastMCP for the protocol layer. The full MCP SDK is overkill for ~10 tools.
- One persistent browser, many contexts. Balances cold-start cost against isolation.
- Structlog with JSON output. Easy to grep, ready to ship to a log pipeline later.

**Under-engineered (deliberately, for POC):**
- No automatic session-expiry detection. When a session goes stale, you re-run `login_capture.py`. Production would detect 401/redirect-to-login and prompt for re-auth.
- No retry logic on navigation failures. A flaky portal would benefit from exponential backoff.
- No rate limiting on tool calls. If Claude loops, it loops fast. Production would add a token bucket.
- No metrics. Logs only. Production: add Prometheus or OpenTelemetry.

**Over-engineered for a POC, but kept because it pays off later:**
- Allowlist enforcement at the security layer. For local play you could skip it; for the eventual AT&T migration you absolutely need it.
- Per-session contexts (vs one global context). One context would be simpler, but mixing GitHub + portal cookies makes session-export to production unsafe.
- Caching layer. At POC scale you'd never notice; once you start scraping the same dashboard repeatedly during agent loops, the saved requests matter.

## Migration path to VD / AT&T

Things that will need to change when you take this inside AT&T:

1. **Playwright Chromium binaries** — need to be downloadable, or pre-packaged. Many VDs block first-run downloads.
2. **Allowlist** — replace test entries with your real portal domains.
3. **SSO/RSA tokens** — `login_capture.py` already handles this (you log in manually). Only re-runs needed when sessions expire.
4. **Headless detection** — some enterprise portals detect headless Chromium. Set `HEADLESS=false` first to verify the portal works, then test headless.
5. **Network egress rules** — verify VD firewall lets Chromium reach the portal hostnames (it should — same hostnames your real browser uses).
6. **Audit logging** — pipe stderr JSON logs to whatever IBM/AT&T logging stack expects. Already structured, no rewriting needed.

## File map

```
miniportal-mcp/
├── pyproject.toml           # deps + entry point
├── .env.example             # env vars template
├── config.yaml              # allowlist, cache, redaction
├── .gitignore               # protects sessions/, .env
├── README.md                # this file
├── sessions/                # storage_state.json files (gitignored)
├── scripts/
│   └── login_capture.py     # one-time interactive login
└── src/
    └── miniportal/
        ├── __init__.py
        ├── server.py        # FastMCP entry + all tools
        ├── browser.py       # BrowserManager (sessions, contexts, pages)
        ├── cache.py         # TTL cache for extraction results
        ├── security.py      # URL allowlist enforcement
        └── logging_config.py # structlog + redaction
```

## Troubleshooting

**"Server starts but tools don't show in Claude Desktop"**
- Check `%APPDATA%\Claude\claude_desktop_config.json` paths use double backslashes or forward slashes.
- Logs from MCP servers go to `%APPDATA%\Claude\logs\mcp-server-miniportal.log`.

**"navigate returns 'Domain not in allowlist'"**
- Add the domain (suffix-matched) to `url_allowlist` in `config.yaml`. Restart the MCP client.

**"Playwright fails to launch chromium"**
- Re-run `python -m playwright install chromium` inside the venv.

**"Session file exists but I'm not logged in"**
- Cookies expired. Re-run `python scripts\login_capture.py --url <login_url> --name <session>`.
