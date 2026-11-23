"""High-priority ingestion of customer feedback and support/GitHub issues."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from src.core.config import FeedbackConfig, NicheConfig
from src.core.errors import StateError
from src.core.io import read_json, read_jsonl, sha256_hex
from src.core.redact import redact_text
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.sensors.sanitizer import sanitize_feedback_text

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
_EXPLICIT_SOURCES = {"customer", "support", "github_issue", "inbox"}
_FEATURE_LABELS = {"enhancement", "feature", "feature-request", "improvement"}
_BUG_LABELS = {"bug", "defect", "regression"}
_COMPLAINT_LABELS = {"complaint", "churn", "dissatisfied"}
_QUESTION_LABELS = {"question", "help", "support"}


@dataclass(frozen=True)
class FeedbackItem:
    id: str
    source: str
    kind: str
    title: str
    body: str
    labels: tuple[str, ...]
    created_at: str
    reactions: int
    is_customer: bool
    priority: float
    matched_niches: tuple[str, ...]

    def to_json(self) -> JsonObject:
        return {
            "id": self.id,
            "source": self.source,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "created_at": self.created_at,
            "reactions": self.reactions,
            "is_customer": self.is_customer,
            "priority": round(self.priority, 4),
            "matched_niches": list(self.matched_niches),
        }


@dataclass(frozen=True)
class IngestReport:
    items: tuple[FeedbackItem, ...]
    raw_count: int
    dropped_count: int
    redactions: dict[str, int]

    def to_json(self) -> JsonObject:
        return {
            "raw_count": self.raw_count,
            "accepted": len(self.items),
            "dropped": self.dropped_count,
            "redactions": dict(sorted(self.redactions.items())),
            "top": [item.to_json() for item in self.items[:10]],
        }


def load_feedback_documents(
    paths: Sequence[Path] = (), inbox_dir: Path | None = None
) -> list[JsonValue]:
    documents: list[JsonValue] = []
    candidates = list(paths)
    if inbox_dir is not None and inbox_dir.exists():
        candidates.extend(sorted(inbox_dir.glob("*.json")))
        candidates.extend(sorted(inbox_dir.glob("*.jsonl")))
    seen_files: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen_files:
            continue
        seen_files.add(resolved)
        if not path.exists():
            raise StateError(f"feedback file not found: {path}")
        if path.suffix == ".jsonl":
            documents.extend(read_jsonl(path))
            continue
        raw = read_json(path, [])
        if isinstance(raw, list):
            documents.extend(raw)
        elif isinstance(raw, dict):
            for key in ("items", "issues", "feedback"):
                nested = raw.get(key)
                if isinstance(nested, list):
                    documents.extend(nested)
                    break
            else:
                documents.append(raw)
        else:
            documents.append(raw)
    return documents


class FeedbackIngestor:
    def __init__(
        self,
        config: FeedbackConfig,
        niches: Sequence[NicheConfig],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._niches = tuple(niches)
        self._clock = clock

    def ingest(
        self,
        documents: Iterable[JsonValue],
        *,
        max_items: int | None = None,
    ) -> IngestReport:
        items: list[FeedbackItem] = []
        redactions: dict[str, int] = {}
        raw_count = 0
        dropped = 0
        for document in documents:
            raw_count += 1
            if not isinstance(document, dict):
                dropped += 1
                continue
            try:
                item = self._parse_item(document, redactions)
            except (StateError, ValueError):
                dropped += 1
                continue
            if item is None:
                dropped += 1
                continue
            items.append(item)
        unique: dict[str, FeedbackItem] = {}
        for item in items:
            existing = unique.get(item.id)
            if existing is None or item.priority > existing.priority:
                unique[item.id] = item
        ordered = sorted(unique.values(), key=lambda item: (-item.priority, item.id))
        limit = max_items if max_items is not None else self._config.max_items_per_cycle
        return IngestReport(
            items=tuple(ordered[: max(0, limit)]),
            raw_count=raw_count,
            dropped_count=dropped,
            redactions=redactions,
        )

    def _parse_item(
        self, document: Mapping[str, object], redactions: dict[str, int]
    ) -> FeedbackItem | None:
        title = self._clean(_first_str(document, "title", "subject", "name"), redactions, 220)
        body = self._clean(
            _first_str(document, "body", "description", "text", "content"), redactions, 4000
        )
        if not title and not body:
            return None
        labels = tuple(
            label
            for label in (
                self._clean(label, redactions, 80)
                for label in _extract_labels(document.get("labels"))
            )
            if label
        )
        lowered_labels = {label.lower() for label in labels}
        source = self._detect_source(document, lowered_labels)
        kind = self._detect_kind(document, lowered_labels)
        is_customer = self._is_customer(document, lowered_labels, source)
        reactions = _extract_reactions(document.get("reactions"))
        priority = self._priority(source, kind, reactions, is_customer, lowered_labels)
        matched = self._match_niches(f"{title}\n{body}", lowered_labels)
        item_id = self._item_id(document, title, body)
        created_at = (
            _first_str(document, "created_at", "createdAt", "timestamp")
            or iso_now(self._clock())
        )
        return FeedbackItem(
            id=item_id,
            source=source,
            kind=kind,
            title=title,
            body=body,
            labels=labels,
            created_at=created_at,
            reactions=reactions,
            is_customer=is_customer,
            priority=priority,
            matched_niches=matched,
        )

    def _clean(
        self, text: str, redactions: dict[str, int], max_chars: int
    ) -> str:
        result = redact_text(text)
        for key, amount in result.counts.items():
            redactions[key] = redactions.get(key, 0) + amount
        return sanitize_feedback_text(result.text, max_chars=max_chars)

    def _detect_source(self, document: Mapping[str, object], labels: set[str]) -> str:
        explicit = document.get("source")
        if isinstance(explicit, str) and explicit in _EXPLICIT_SOURCES:
            return explicit
        if labels & {"customer", "paid", "sponsor"}:
            return "customer"
        if labels & {"support", "complaint"}:
            return "support"
        return "github_issue"

    def _detect_kind(self, document: Mapping[str, object], labels: set[str]) -> str:
        explicit = document.get("kind") or document.get("type")
        if isinstance(explicit, str):
            normalized = explicit.strip().lower().replace(" ", "_").replace("-", "_")
            if normalized in self._config.kind_multipliers:
                return normalized
        if labels & _COMPLAINT_LABELS:
            return "complaint"
        if labels & _BUG_LABELS:
            return "bug"
        if labels & _FEATURE_LABELS:
            return "feature_request"
        if labels & _QUESTION_LABELS:
            return "question"
        return "question"

    def _is_customer(
        self, document: Mapping[str, object], labels: set[str], source: str
    ) -> bool:
        explicit = document.get("is_customer")
        if isinstance(explicit, bool):
            return explicit
        return source == "customer" or bool(
            labels & {label.lower() for label in self._config.priority_labels}
        )

    def _priority(
        self,
        source: str,
        kind: str,
        reactions: int,
        is_customer: bool,
        labels: set[str],
    ) -> float:
        source_weight = self._config.source_weights.get(source, 1.0)
        kind_multiplier = self._config.kind_multipliers.get(kind, 1.0)
        engagement = 1.0 + min(1.0, math.log1p(max(0, reactions)) / 5.0)
        customer_multiplier = 1.75 if is_customer else 1.0
        urgency = 1.5 if labels & {"critical", "p1", "urgent", "regression"} else 1.0
        if "pain_interview" in labels:
            recurring = "recurring_wtp" in labels and "verified" in labels
            urgency = 3.0 if recurring else 2.5
        elif "interview" in labels:
            urgency = max(urgency, 2.0)
        return round(
            source_weight * kind_multiplier * engagement * customer_multiplier * urgency,
            4,
        )

    def _match_niches(self, text: str, labels: set[str]) -> tuple[str, ...]:
        haystack = text.lower()
        matched: list[str] = []
        for niche in self._niches:
            if not niche.active:
                continue
            if niche.id.lower() in labels or niche.family.lower() in labels:
                matched.append(niche.id)
                continue
            if any(keyword.lower() in haystack for keyword in niche.keywords):
                matched.append(niche.id)
        return tuple(sorted(matched))

    def _item_id(self, document: Mapping[str, object], title: str, body: str) -> str:
        for key in ("id", "number", "key"):
            value = document.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                candidate = f"gh-{value}" if isinstance(value, int) else str(value)
                if _SAFE_ID.fullmatch(candidate):
                    return candidate
        digest = sha256_hex(f"{title}\n{body}")[:16]
        return f"local-{digest}"


def _first_str(document: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_labels(value: object) -> list[str]:
    if not isinstance(value, list):
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return []
    labels: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            labels.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                labels.append(name.strip())
    return labels


def _extract_reactions(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return max(0, value)
    if isinstance(value, dict):
        for key in ("totalCount", "total_count", "total"):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool):
                return max(0, count)
    return 0


def feedback_to_documents(items: Sequence[FeedbackItem]) -> list[JsonValue]:
    return [cast(JsonValue, item.to_json()) for item in items]
