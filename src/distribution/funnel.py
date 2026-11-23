"""Privacy-preserving funnel telemetry from local aggregate events only.

No cookies, fingerprinting, IP collection, or per-user identifiers are allowed.
Events carry only a stage, a niche id, a source label, and an aggregate count.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.errors import StateError
from src.core.io import append_jsonl, file_lock, read_jsonl
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject
from src.sensors.feedback import FeedbackItem

STAGES = ("impression", "artifact_download", "feedback_signal", "paid_intent")
_SAFE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_MAX_EVENT_COUNT = 1_000_000


@dataclass(frozen=True)
class FunnelEvent:
    niche_id: str
    stage: str
    source: str
    count: int
    created_at: str

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "stage": self.stage,
            "source": self.source,
            "count": self.count,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class FunnelSummary:
    counts: dict[str, int]
    conversion: dict[str, float]
    total_events: int
    invalid_events: int
    first_ts: str | None
    last_ts: str | None
    basis: str

    def to_json(self) -> JsonObject:
        return {
            "counts": dict(sorted(self.counts.items())),
            "conversion": {key: round(value, 4) for key, value in self.conversion.items()},
            "total_events": self.total_events,
            "invalid_events": self.invalid_events,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "basis": self.basis,
        }


class FunnelTelemetry:
    def __init__(
        self,
        state_dir: Path,
        *,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._path = state_dir / "funnel.jsonl"
        self._dry_run = dry_run
        self._clock = clock

    @property
    def path(self) -> Path:
        return self._path

    def record(self, niche_id: str, stage: str, *, source: str, count: int = 1) -> FunnelEvent:
        event = FunnelEvent(
            niche_id=self._token(niche_id, "niche_id"),
            stage=self._stage(stage),
            source=self._token(source, "source"),
            count=self._count(count),
            created_at=iso_now(self._clock()),
        )
        if not self._dry_run:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with file_lock(self._path.with_name(self._path.name + ".lock")):
                append_jsonl(self._path, event.to_json())
        return event

    def summarize(self, feedback: Sequence[FeedbackItem] = ()) -> FunnelSummary:
        counts = dict.fromkeys(STAGES, 0)
        invalid = 0
        first_ts: str | None = None
        last_ts: str | None = None
        total = 0
        for raw in read_jsonl(self._path):
            if not isinstance(raw, dict):
                invalid += 1
                continue
            stage = raw.get("stage")
            count = raw.get("count")
            niche = raw.get("niche_id")
            source = raw.get("source")
            timestamp = raw.get("created_at")
            if (
                not isinstance(stage, str)
                or stage not in STAGES
                or not isinstance(count, int)
                or isinstance(count, bool)
                or not 1 <= count <= _MAX_EVENT_COUNT
                or not isinstance(niche, str)
                or not _SAFE_TOKEN.fullmatch(niche)
                or not isinstance(source, str)
                or not _SAFE_TOKEN.fullmatch(source)
                or not isinstance(timestamp, str)
            ):
                invalid += 1
                continue
            counts[stage] += count
            total += 1
            if first_ts is None or timestamp < first_ts:
                first_ts = timestamp
            if last_ts is None or timestamp > last_ts:
                last_ts = timestamp
        for item in feedback:
            counts["feedback_signal"] += 1
            if item.created_at:
                if first_ts is None or item.created_at < first_ts:
                    first_ts = item.created_at
                if last_ts is None or item.created_at > last_ts:
                    last_ts = item.created_at
        conversion = {
            "download_rate": _ratio(counts["artifact_download"], counts["impression"]),
            "feedback_rate": _ratio(counts["feedback_signal"], counts["artifact_download"]),
            "intent_rate": _ratio(counts["paid_intent"], counts["feedback_signal"]),
        }
        return FunnelSummary(
            counts=counts,
            conversion=conversion,
            total_events=total,
            invalid_events=invalid,
            first_ts=first_ts,
            last_ts=last_ts,
            basis=(
                "local aggregate events and sanitized feedback only; "
                "no cookies, device identifiers, or user tracking"
            ),
        )

    @staticmethod
    def _stage(stage: str) -> str:
        if stage not in STAGES:
            raise StateError(f"unsupported funnel stage: {stage!r}")
        return stage

    @staticmethod
    def _token(value: str, field: str) -> str:
        normalized = value.strip().lower().replace(" ", "-")
        if not _SAFE_TOKEN.fullmatch(normalized):
            raise StateError(f"unsafe funnel {field}: {value!r}")
        return normalized

    @staticmethod
    def _count(count: int) -> int:
        if isinstance(count, bool) or not 1 <= count <= _MAX_EVENT_COUNT:
            raise StateError(f"funnel count out of range: {count!r}")
        return count


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return min(1.0, numerator / denominator)
