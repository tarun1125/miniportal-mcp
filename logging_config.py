"""Structured logging with secret redaction.

Why structlog: traditional logging.Logger gives unstructured text; structlog gives
JSON events you can grep, ship to Splunk, or feed into log analytics. For a tool
that touches browsers and forms, the security audit trail matters.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any

import structlog


_REDACTION_PATTERNS: list[re.Pattern[str]] = []


def configure_logging(level: str = "INFO", redaction_patterns: list[str] | None = None) -> None:
    """Configure structlog + stdlib logging.

    Logs go to stderr (stdout is reserved for MCP stdio protocol).
    """
    global _REDACTION_PATTERNS
    _REDACTION_PATTERNS = [re.compile(p) for p in (redaction_patterns or [])]

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.JSONRenderer() if os.getenv("LOG_FORMAT", "json") == "json"
            else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def _redact_processor(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """Walk every string value in the event and redact anything matching configured patterns."""
    for key, value in list(event_dict.items()):
        if isinstance(value, str):
            event_dict[key] = _redact(value)
    return event_dict


def _redact(text: str) -> str:
    redacted = text
    for pattern in _REDACTION_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a logger bound with a component name."""
    return structlog.get_logger(name)


def domain_of(url: str) -> str:
    """Extract just the domain from a URL for logging.

    Avoid logging full URLs because query strings can carry tokens.
    """
    from urllib.parse import urlparse

    try:
        return urlparse(url).netloc or "<empty>"
    except Exception:
        return "<unparseable>"
