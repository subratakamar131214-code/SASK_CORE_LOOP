"""Structured early-user pain findings, sanitized before any state write.

The importer only accepts human-validated findings and converts them into
top-priority sensor feedback documents. Text fields are sanitized on
construction so private notes, emails, and secrets never reach repository state.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.errors import StateError
from src.core.io import read_json, sha256_hex, write_json_atomic
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now, parse_iso
from src.core.types import JsonObject, JsonValue
from src.sensors.sanitizer import sanitize_feedback_text

SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
_REQUIRED_TEXT = (
    "workflow_bottleneck",
    "breaks_today",
    "estimated_cost_or_hours_lost",
    "frequency",
)


@dataclass(frozen=True)
class PainFinding:
    finding_id: str
    niche_id: str
    workflow_bottleneck: str
    breaks_today: str
    estimated_cost_or_hours_lost: str
    frequency: str
    recurring_willingness_to_pay_signal: bool
    notes_sanitized: str
    captured_at: str
    verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.finding_id, str) or not _SAFE_ID.fullmatch(self.finding_id):
            raise StateError(f"unsafe pain finding id: {self.finding_id!r}")
        if not isinstance(self.niche_id, str) or not _SAFE_ID.fullmatch(self.niche_id):
            raise StateError(f"unsafe pain niche id: {self.niche_id!r}")
        if not isinstance(self.verified, bool) or not self.verified:
            raise StateError("only human-validated pain findings may be ingested")
        if not isinstance(self.recurring_willingness_to_pay_signal, bool):
            raise StateError("recurring_willingness_to_pay_signal must be a boolean")
        parse_iso(self.captured_at)
        for name in _REQUIRED_TEXT:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise StateError(f"pain finding {name} must be a non-empty string")
            sanitized = sanitize_feedback_text(value, max_chars=600)
            if not sanitized:
                raise StateError(f"pain finding {name} is empty after sanitization")
            object.__setattr__(self, name, sanitized)
        if not isinstance(self.notes_sanitized, str):
            raise StateError("pain finding notes_sanitized must be a string")
        object.__setattr__(
            self,
            "notes_sanitized",
            sanitize_feedback_text(self.notes_sanitized, max_chars=1200),
        )

    def to_feedback_document(self) -> dict[str, object]:
        labels = [
            "customer",
            "critical",
            "pain_interview",
            "verified",
            self.niche_id,
        ]
        if self.recurring_willingness_to_pay_signal:
            labels.append("recurring_wtp")
        body = (
            f"Workflow bottleneck: {self.workflow_bottleneck}. "
            f"Breaks today: {self.breaks_today}. "
            f"Estimated cost or hours lost: {self.estimated_cost_or_hours_lost}. "
            f"Frequency: {self.frequency}. "
            "Recurring willingness to pay for a reliable hosted/pro workflow: "
            f"{'yes' if self.recurring_willingness_to_pay_signal else 'no'}."
        )
        if self.notes_sanitized:
            body = f"{body} Notes: {self.notes_sanitized}"
        return {
            "id": self.finding_id,
            "source": "customer",
            "kind": "complaint",
            "title": f"Pain interview: {self.workflow_bottleneck}",
            "body": body,
            "labels": labels,
            "created_at": self.captured_at,
            "reactions": 0,
        }

    def to_json(self) -> JsonObject:
        return {
            "finding_id": self.finding_id,
            "niche_id": self.niche_id,
            "workflow_bottleneck": self.workflow_bottleneck,
            "breaks_today": self.breaks_today,
            "estimated_cost_or_hours_lost": self.estimated_cost_or_hours_lost,
            "frequency": self.frequency,
            "recurring_willingness_to_pay_signal": (
                self.recurring_willingness_to_pay_signal
            ),
            "notes_sanitized": self.notes_sanitized,
            "captured_at": self.captured_at,
            "verified": True,
        }


def load_pain_findings(path: Path) -> tuple[PainFinding, ...]:
    if not path.exists():
        raise StateError(f"pain interview file not found: {path}")
    raw = read_json(path, None)
    if isinstance(raw, dict):
        nested = raw.get("findings", raw.get("pain_interviews"))
        if not isinstance(nested, list):
            raise StateError("pain interview file must contain a 'findings' list")
        items = nested
    elif isinstance(raw, list):
        items = raw
    else:
        raise StateError("pain interview file must be a list or an object")
    findings: dict[str, PainFinding] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise StateError(f"pain finding {index} is not an object")
        finding = _finding_from_mapping(item, index)
        findings[finding.finding_id] = finding
    return tuple(findings[key] for key in sorted(findings))


def pain_feedback_documents(
    findings: Sequence[PainFinding],
) -> list[dict[str, object]]:
    return [finding.to_feedback_document() for finding in findings]


def save_pain_findings(
    findings: Sequence[PainFinding],
    path: Path,
    *,
    dry_run: bool = False,
) -> None:
    payload: JsonObject = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_now(),
        "findings": [finding.to_json() for finding in findings],
    }
    sanitized, _ = sanitize_public_payload(payload)
    if not isinstance(sanitized, dict):
        raise StateError("pain interview state must remain an object")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, sanitized)


def _finding_from_mapping(document: dict[str, JsonValue], index: int) -> PainFinding:
    niche_id = _text(document, "niche_id", index)
    bottleneck = _text(document, "workflow_bottleneck", index)
    captured_at = _optional_text(document, "captured_at", index) or iso_now()
    finding_id = _optional_text(document, "finding_id", index) or _optional_text(
        document, "id", index
    )
    if finding_id is None:
        finding_id = "pain-" + sha256_hex(
            f"{niche_id}|{bottleneck}|{captured_at}"
        )[:16]
    verified = document.get("verified", True)
    if not isinstance(verified, bool):
        raise StateError(f"pain finding {index}.verified must be a boolean")
    return PainFinding(
        finding_id=finding_id,
        niche_id=niche_id,
        workflow_bottleneck=bottleneck,
        breaks_today=_text(document, "breaks_today", index),
        estimated_cost_or_hours_lost=_measure(document, "estimated_cost_or_hours_lost", index),
        frequency=_measure(document, "frequency", index),
        recurring_willingness_to_pay_signal=_boolean(
            document.get("recurring_willingness_to_pay_signal"),
            f"pain finding {index}.recurring_willingness_to_pay_signal",
        ),
        notes_sanitized=_optional_text(document, "notes_sanitized", index) or "",
        captured_at=captured_at,
        verified=verified,
    )


def _text(document: dict[str, JsonValue], key: str, index: int) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"pain finding {index}.{key} must be a non-empty string")
    return value


def _optional_text(
    document: dict[str, JsonValue], key: str, index: int
) -> str | None:
    value = document.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StateError(f"pain finding {index}.{key} must be a non-empty string")
    return value


def _measure(document: dict[str, JsonValue], key: str, index: int) -> str:
    value = document.get(key)
    if isinstance(value, bool):
        raise StateError(f"pain finding {index}.{key} must be text or a number")
    if isinstance(value, (int, float)):
        return f"{float(value):g}"
    if isinstance(value, str) and value.strip():
        return value
    raise StateError(f"pain finding {index}.{key} must be text or a number")


def _boolean(value: JsonValue, context: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "true", "y", "1"}:
            return True
        if lowered in {"no", "false", "n", "0"}:
            return False
    raise StateError(f"{context} must be a boolean or yes/no")
