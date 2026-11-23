"""Public API for PII/secret sanitization used by all sensors and state writers."""

from __future__ import annotations

import re

from src.core.redact import (
    RedactionResult,
    assert_no_secrets,
    contains_secret,
    is_placeholder,
    redact_json,
    redact_text,
    sanitize_public_payload,
)
from src.core.types import JsonValue

_WHITESPACE = re.compile(r"[ \t]+")
_MULTI_NEWLINE = re.compile(r"\n{3,}")

__all__ = [
    "RedactionResult",
    "assert_no_secrets",
    "contains_secret",
    "is_placeholder",
    "redact_json",
    "redact_text",
    "sanitize_feedback_text",
    "sanitize_payload",
    "sanitize_public_payload",
    "sanitize_text",
]


def sanitize_text(text: str) -> str:
    return redact_text(text).text


def sanitize_payload(value: JsonValue) -> tuple[JsonValue, dict[str, int]]:
    return redact_json(value)


def sanitize_feedback_text(text: str, *, max_chars: int = 4000) -> str:
    sanitized = redact_text(text).text
    normalized = _WHITESPACE.sub(" ", sanitized)
    normalized = _MULTI_NEWLINE.sub("\n\n", normalized).strip()
    if len(normalized) > max_chars:
        normalized = normalized[: max_chars - 1].rstrip() + "…"
    return normalized
