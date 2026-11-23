"""Deterministic secret/PII redaction shared by sensors, logs, state, and inference."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from src.core.errors import SanitizationError
from src.core.types import JsonValue

_PLACEHOLDER_MARKERS: Final = (
    "REDACTED",
    "PLACEHOLDER",
    "CHANGEME",
    "CHANGE_ME",
    "YOUR_",
    "YOUR-",
    "<",
    "EXAMPLE",
    "XXX",
    "TO_BE",
    "${{",
    "SECRETS.",
    "VARS.",
)

_SENSITIVE_KEY_RE: Final = re.compile(
    r"(?i)(?:^|_)(?:password|passwd|secret|token|api_?key|authorization|credential|private_key)(?:_|$)"
)


def is_placeholder(value: str) -> bool:
    upper = value.upper()
    return value.strip() == "" or any(marker in upper for marker in _PLACEHOLDER_MARKERS)


_SAFE_ASSIGNMENT_VALUES = frozenset(
    {
        "true",
        "false",
        "null",
        "none",
        "nil",
        "yes",
        "no",
        "on",
        "off",
        "enabled",
        "disabled",
        "required",
        "optional",
        "default",
    }
)
_UPPER_REFERENCE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLAIN_WORD = re.compile(r"^[a-z][a-z_]*$")
_DOTTED_REFERENCE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$"
)
_URL_VALUE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_ASSIGNMENT_KINDS = frozenset({"secret_assignment", "quoted_secret_assignment"})


def is_suspicious_assignment_value(value: str) -> bool:
    """Distinguish a credential-like value from code, references, and docs.

    Redaction stays deliberately broad; this narrower predicate is used by the
    repository secret scanner so source identifiers and template references do
    not block CI.
    """

    candidate = value.strip().strip("'\"`")
    if not candidate or is_placeholder(candidate):
        return False
    if candidate.lower() in _SAFE_ASSIGNMENT_VALUES:
        return False
    if "${" in candidate or "$(" in candidate or "{{" in candidate or "}}" in candidate:
        return False
    if any(marker in candidate for marker in ("(", ")", "[", "]", ":", "{", "}")):
        return False
    if _URL_VALUE.match(candidate):
        rest = candidate.split("://", 1)[1]
        return "@" in rest
    if "/" in candidate:
        return False
    if _UPPER_REFERENCE.fullmatch(candidate) or _DOTTED_REFERENCE.fullmatch(candidate):
        return False
    if len(candidate) < 6:
        return False
    if _PLAIN_WORD.fullmatch(candidate):
        return False
    return (
        any(char.isdigit() for char in candidate)
        or any(not char.isalnum() and char not in "._-" for char in candidate)
        or len(candidate) >= 16
    )


@dataclass
class RedactionResult:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.counts)


@dataclass(frozen=True)
class _Rule:
    kind: str
    pattern: re.Pattern[str]
    replacer: Callable[[re.Match[str]], str]


def _literal(kind: str) -> Callable[[re.Match[str]], str]:
    token = f"[REDACTED:{kind}]"

    def replace(_: re.Match[str]) -> str:
        return token

    return replace


def _replace_auth(match: re.Match[str]) -> str:
    if is_placeholder(match.group(3)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)} [REDACTED:AUTH]"


def _replace_assignment(match: re.Match[str]) -> str:
    if is_placeholder(match.group(3)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}[REDACTED:SECRET]"


def _replace_quoted_assignment(match: re.Match[str]) -> str:
    if is_placeholder(match.group(4)):
        return match.group(0)
    quote = match.group(3)
    return f"{match.group(1)}{match.group(2)}{quote}[REDACTED:SECRET]{quote}"


def _replace_url_param(match: re.Match[str]) -> str:
    if is_placeholder(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}[REDACTED:URL_SECRET]"


def _replace_userinfo(match: re.Match[str]) -> str:
    return f"{match.group(1)}[REDACTED:USERINFO]@"


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _replace_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return "[REDACTED:CARD]"
    return match.group(0)


_RULES: Final[tuple[_Rule, ...]] = (
    _Rule(
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
            re.DOTALL,
        ),
        _literal("PRIVATE_KEY"),
    ),
    _Rule(
        "auth_header",
        re.compile(r"(?i)\b((?:proxy-)?authorization\s*[:=]\s*)([a-z]+)\s+([^\s\"']+)"),
        _replace_auth,
    ),
    _Rule(
        "secret_assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|"
            r"CREDENTIAL|PRIVATE[_-]?KEY)[A-Z0-9_]*)[ \t]*([:=])[ \t]*([^\s,;\"']+)"
        ),
        _replace_assignment,
    ),
    _Rule(
        "quoted_secret_assignment",
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|"
            r"CREDENTIAL|PRIVATE[_-]?KEY)[A-Z0-9_]*)[ \t]*([:=])[ \t]*"
            r"(['\"])([^'\"\n]{6,300})\3"
        ),
        _replace_quoted_assignment,
    ),
    _Rule(
        "github_token",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
        _literal("GITHUB_TOKEN"),
    ),
    _Rule(
        "groq_key",
        re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
        _literal("GROQ_KEY"),
    ),
    _Rule(
        "openai_key",
        re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
        _literal("API_KEY"),
    ),
    _Rule(
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
        _literal("GOOGLE_API_KEY"),
    ),
    _Rule(
        "google_oauth_token",
        re.compile(r"\bAQ\.[A-Za-z0-9_-]{20,}\b"),
        _literal("GOOGLE_OAUTH"),
    ),
    _Rule(
        "aws_access_key",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        _literal("AWS_KEY"),
    ),
    _Rule(
        "slack_token",
        re.compile(r"\bxox[pbar]-[A-Za-z0-9-]{10,}\b"),
        _literal("SLACK_TOKEN"),
    ),
    _Rule(
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
        _literal("JWT"),
    ),
    _Rule(
        "url_secret",
        re.compile(
            r"(?i)([?&](?:api[_-]?key|key|token|access_token|secret|sig|signature)=)([^&\s#]+)"
        ),
        _replace_url_param,
    ),
    _Rule(
        "url_userinfo",
        re.compile(r"(?i)\b(https?://)[^/\s:@]+:[^/\s@]+@"),
        _replace_userinfo,
    ),
    _Rule(
        "ssn",
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        _literal("SSN"),
    ),
    _Rule(
        "payment_card",
        re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
        _replace_card,
    ),
    _Rule(
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        _literal("EMAIL"),
    ),
    _Rule(
        "phone",
        re.compile(
            r"(?<![\w.])(?:\+\d{1,3}[\s.-]?\(?\d{1,4}\)?|\+\d{6,15}|"
            r"\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]?\d{3,4})(?![\w.])"
        ),
        _literal("PHONE"),
    ),
    _Rule(
        "windows_home",
        re.compile(r"\b([A-Za-z]:\\Users\\)[^\\\s\"']+"),
        lambda match: f"{match.group(1)}[REDACTED:USER]",
    ),
    _Rule(
        "unix_home",
        re.compile(r"(/home/|/Users/)[^/\s\"']+"),
        lambda match: f"{match.group(1)}[REDACTED:USER]",
    ),
    _Rule(
        "private_ip",
        re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
        _literal("PRIVATE_IP"),
    ),
)


SECRET_KINDS: Final[frozenset[str]] = frozenset(
    {
        "private_key",
        "auth_header",
        "secret_assignment",
        "quoted_secret_assignment",
        "github_token",
        "groq_key",
        "openai_key",
        "google_api_key",
        "google_oauth_token",
        "aws_access_key",
        "slack_token",
        "jwt",
        "url_secret",
        "url_userinfo",
    }
)


@dataclass(frozen=True)
class SecretHit:
    kind: str
    start: int
    end: int


def secret_hits(text: str) -> list[SecretHit]:
    """Return locations of secret-like spans without returning the secret text."""

    hits: list[SecretHit] = []
    for rule in _RULES:
        if rule.kind not in SECRET_KINDS:
            continue
        for match in rule.pattern.finditer(text):
            if rule.kind in _ASSIGNMENT_KINDS:
                value = match.group(match.lastindex or 0)
                if not is_suspicious_assignment_value(value):
                    continue
            if rule.replacer(match) != match.group(0):
                hits.append(SecretHit(kind=rule.kind, start=match.start(), end=match.end()))
    hits.sort(key=lambda hit: hit.start)
    return hits


def redact_text(text: str) -> RedactionResult:
    counts: dict[str, int] = {}
    result = text
    for rule in _RULES:

        def replace(match: re.Match[str], *, current_rule: _Rule = rule) -> str:
            replacement = current_rule.replacer(match)
            if replacement != match.group(0):
                counts[current_rule.kind] = counts.get(current_rule.kind, 0) + 1
            return replacement

        result = rule.pattern.sub(replace, result)
    return RedactionResult(text=result, counts=counts)


def contains_secret(text: str) -> bool:
    return bool(redact_text(text).counts)


def assert_no_secrets(text: str, *, context: str = "payload") -> None:
    result = redact_text(text)
    if result.counts:
        kinds = ", ".join(sorted(result.counts))
        raise SanitizationError(
            f"refusing to write {context}: redactable secrets/PII remain ({kinds})"
        )


def redact_json(value: JsonValue) -> tuple[JsonValue, dict[str, int]]:
    counts: dict[str, int] = {}

    def merge(batch: dict[str, int]) -> None:
        for key, amount in batch.items():
            counts[key] = counts.get(key, 0) + amount

    def walk(item: JsonValue) -> JsonValue:
        if isinstance(item, str):
            result = redact_text(item)
            merge(result.counts)
            return result.text
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, dict):
            output: dict[str, JsonValue] = {}
            for key, child in item.items():
                if _SENSITIVE_KEY_RE.search(key) and not isinstance(child, (dict, list)):
                    if (
                        isinstance(child, str)
                        and is_placeholder(child)
                        or child is None
                        or child is False
                    ):
                        output[key] = child
                    else:
                        output[key] = "[REDACTED:SECRET_FIELD]"
                        counts["secret_field"] = counts.get("secret_field", 0) + 1
                    continue
                output[key] = walk(child)
            return output
        return item

    return walk(value), counts


def sanitize_public_payload(value: JsonValue) -> tuple[JsonValue, dict[str, int]]:
    sanitized, counts = redact_json(value)
    for item in _iter_strings(sanitized):
        if contains_secret(item):
            raise SanitizationError("sanitization left a recognizable secret; write refused")
    return sanitized, counts


def _iter_strings(value: JsonValue) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, list):
        for child in value:
            found.extend(_iter_strings(child))
    elif isinstance(value, dict):
        for child in value.values():
            found.extend(_iter_strings(child))
    return found
