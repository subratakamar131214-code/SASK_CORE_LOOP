"""Ruthless pruning of dead niches and replicas using real pull signals.

Nothing here deletes safety artifacts. Decisions are recorded in sanitized
runtime state, replicas are moved to ``paused`` or ``retired``, and archived
builds are represented by manifests. Protected invariants and ``OWNER_VAULT``
are never candidates for pruning.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from src.core.config import PruningConfig
from src.core.errors import ConfigError, StateError
from src.core.io import file_lock, read_json, write_json_atomic
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now
from src.core.types import JsonObject, JsonValue

OWNER_VAULT = "OWNER_VAULT"
SCHEMA_VERSION = 1

ACTION_KEEP = "keep"
ACTION_PAUSE = "pause"
ACTION_SUNSET = "sunset"
ACTION_ARCHIVE = "archive"
ACTIONS = frozenset({ACTION_KEEP, ACTION_PAUSE, ACTION_SUNSET, ACTION_ARCHIVE})
INACTIVE_ACTIONS = frozenset({ACTION_PAUSE, ACTION_SUNSET, ACTION_ARCHIVE})


@dataclass(frozen=True)
class PruneSignals:
    asset_id: str
    age_days: float = 0.0
    stale_days: float = 0.0
    feedback_items: int = 0
    repeat_usage: int = 0
    adoption: float = 0.0
    stars: int = 0
    clones: int = 0
    issues: int = 0
    conversions: int = 0
    real_revenue_usd: float = 0.0
    consecutive_zero_cycles: int = 0

    def __post_init__(self) -> None:
        if not self.asset_id.strip():
            raise StateError("prune signals require an asset_id")
        for name in (
            "age_days",
            "stale_days",
            "adoption",
            "real_revenue_usd",
        ):
            value = getattr(self, name)
            if value < 0.0:
                raise StateError(f"prune signal {name} cannot be negative")
        if self.adoption > 1.0:
            raise StateError("prune signal adoption must be between 0 and 1")
        for name in (
            "feedback_items",
            "repeat_usage",
            "stars",
            "clones",
            "issues",
            "conversions",
            "consecutive_zero_cycles",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise StateError(f"prune signal {name} must be a non-negative integer")

    def has_pull(self) -> bool:
        return (
            self.real_revenue_usd > 0.0
            or self.conversions > 0
            or self.adoption > 0.0
            or self.repeat_usage > 0
            or self.stars + self.clones + self.issues > 0
        )


@dataclass(frozen=True)
class PruneDecision:
    asset_id: str
    action: str
    reason: str
    signals: PruneSignals

    def __post_init__(self) -> None:
        if self.action not in ACTIONS:
            raise StateError(f"unsupported prune action: {self.action!r}")

    @property
    def pruned(self) -> bool:
        return self.action in INACTIVE_ACTIONS

    def to_json(self) -> JsonObject:
        return {
            "asset_id": self.asset_id,
            "action": self.action,
            "reason": self.reason,
            "signals": {
                "age_days": round(self.signals.age_days, 3),
                "stale_days": round(self.signals.stale_days, 3),
                "feedback_items": self.signals.feedback_items,
                "repeat_usage": self.signals.repeat_usage,
                "adoption": round(self.signals.adoption, 6),
                "stars": self.signals.stars,
                "clones": self.signals.clones,
                "issues": self.signals.issues,
                "conversions": self.signals.conversions,
                "real_revenue_usd": round(self.signals.real_revenue_usd, 6),
                "consecutive_zero_cycles": self.signals.consecutive_zero_cycles,
            },
        }


class PruningEngine:
    def __init__(self, config: PruningConfig) -> None:
        self._config = config
        if config.pause_after_stale_days <= 0:
            raise ConfigError("pruning pause threshold must be positive")
        if config.sunset_after_stale_days <= config.pause_after_stale_days:
            raise ConfigError("pruning sunset threshold must exceed the pause threshold")

    def decide(self, signals: PruneSignals) -> PruneDecision:
        if not self._config.enabled:
            return self._decision(signals, ACTION_KEEP, "pruning is disabled")
        if signals.asset_id.strip().upper() == OWNER_VAULT:
            return self._decision(
                signals, ACTION_KEEP, "OWNER_VAULT is never pruned or reallocated"
            )
        if signals.real_revenue_usd > 0.0:
            return self._decision(
                signals, ACTION_KEEP, "human-confirmed revenue proves real pull"
            )
        if signals.age_days < self._config.min_age_days:
            return self._decision(signals, ACTION_KEEP, "asset is too young to judge")
        pull = signals.has_pull()
        if not pull and (
            signals.stale_days >= self._config.sunset_after_stale_days
            and signals.consecutive_zero_cycles >= self._config.sunset_after_zero_cycles
        ):
            return self._decision(
                signals,
                ACTION_ARCHIVE,
                "no adoption, repeat usage, or revenue; stale beyond the sunset window",
            )
        if not pull and (
            signals.stale_days >= self._config.sunset_after_stale_days
            or signals.consecutive_zero_cycles >= self._config.sunset_after_zero_cycles
        ):
            return self._decision(
                signals, ACTION_SUNSET, "no real pull and the asset missed its sunset window"
            )
        if signals.stale_days >= self._config.sunset_after_stale_days:
            return self._decision(
                signals, ACTION_PAUSE, "proven pull is stale; pause instead of killing it"
            )
        if not pull and signals.stale_days >= self._config.pause_after_stale_days:
            return self._decision(
                signals, ACTION_PAUSE, "no real pull inside the pause window"
            )
        if not pull:
            return self._decision(
                signals, ACTION_KEEP, "within the grace window; collecting pull signals"
            )
        return self._decision(signals, ACTION_KEEP, "adoption or revenue proves real pull")

    @staticmethod
    def _decision(signals: PruneSignals, action: str, reason: str) -> PruneDecision:
        return PruneDecision(
            asset_id=signals.asset_id, action=action, reason=reason, signals=signals
        )


@dataclass(frozen=True)
class PruningSnapshot:
    decisions: tuple[PruneDecision, ...] = ()
    paused: tuple[str, ...] = ()
    sunset: tuple[str, ...] = ()
    archived: tuple[str, ...] = ()

    def pruned_ids(self) -> frozenset[str]:
        return frozenset((*self.paused, *self.sunset, *self.archived))

    def to_json(self) -> JsonObject:
        return {
            "paused": list(self.paused),
            "sunset": list(self.sunset),
            "archived": list(self.archived),
            "decisions": [decision.to_json() for decision in self.decisions],
        }


class PruningRegistry:
    def __init__(self, path: Path, *, dry_run: bool = False) -> None:
        self._path = path
        self._dry_run = dry_run

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> PruningSnapshot:
        raw = read_json(self._path, None)
        if raw is None:
            return PruningSnapshot()
        if not isinstance(raw, dict):
            raise StateError("pruning registry is not an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise StateError("unsupported pruning registry schema")
        return PruningSnapshot(
            decisions=self._decisions(raw.get("decisions")),
            paused=_safe_ids(raw.get("paused")),
            sunset=_safe_ids(raw.get("sunset")),
            archived=_safe_ids(raw.get("archived")),
        )

    def record(self, decisions: Sequence[PruneDecision]) -> PruningSnapshot:
        with self._guard():
            previous = self.load()
            paused = set(previous.paused)
            sunset = set(previous.sunset)
            archived = set(previous.archived)
            for decision in decisions:
                target = decision.asset_id
                if not decision.pruned:
                    paused.discard(target)
                    sunset.discard(target)
                    archived.discard(target)
                    continue
                paused.discard(target)
                sunset.discard(target)
                archived.discard(target)
                if decision.action == ACTION_PAUSE:
                    paused.add(target)
                elif decision.action == ACTION_SUNSET:
                    sunset.add(target)
                elif decision.action == ACTION_ARCHIVE:
                    archived.add(target)
            snapshot = PruningSnapshot(
                decisions=tuple(decisions),
                paused=tuple(sorted(paused)),
                sunset=tuple(sorted(sunset)),
                archived=tuple(sorted(archived)),
            )
            self._write(snapshot)
            return snapshot

    def _write(self, snapshot: PruningSnapshot) -> None:
        if self._dry_run:
            return
        payload: JsonObject = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": iso_now(),
            **snapshot.to_json(),
        }
        sanitized, _ = sanitize_public_payload(payload)
        if not isinstance(sanitized, dict):
            raise StateError("pruning registry must remain an object")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._path, sanitized)

    @staticmethod
    def _decisions(value: JsonValue) -> tuple[PruneDecision, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise StateError("pruning registry decisions is malformed")
        decisions: list[PruneDecision] = []
        for item in value:
            if not isinstance(item, dict):
                raise StateError("pruning decision is not an object")
            asset_id = item.get("asset_id")
            action = item.get("action")
            reason = item.get("reason")
            signals = item.get("signals")
            if (
                not isinstance(asset_id, str)
                or not isinstance(action, str)
                or not isinstance(reason, str)
                or not isinstance(signals, dict)
            ):
                raise StateError("pruning decision is missing required fields")
            decisions.append(
                PruneDecision(
                    asset_id=asset_id,
                    action=action,
                    reason=reason,
                    signals=_signals_from_json(asset_id, signals),
                )
            )
        return tuple(decisions)

    def _guard(self) -> AbstractContextManager[None]:
        if self._dry_run:
            return nullcontext()
        return file_lock(self._path.with_name(self._path.name + ".lock"))


def _safe_ids(value: JsonValue) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise StateError("pruning registry id list is malformed")
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise StateError("pruning registry id list is malformed")
        ids.append(item)
    return tuple(sorted(set(ids)))


def _signals_from_json(asset_id: str, document: dict[str, JsonValue]) -> PruneSignals:
    def number(key: str) -> float:
        value = document.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise StateError(f"prune signal {key} must be numeric")
        return float(value)

    def integer(key: str) -> int:
        value = document.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise StateError(f"prune signal {key} must be an integer")
        return value

    return PruneSignals(
        asset_id=asset_id,
        age_days=number("age_days"),
        stale_days=number("stale_days"),
        feedback_items=integer("feedback_items"),
        repeat_usage=integer("repeat_usage"),
        adoption=number("adoption"),
        stars=integer("stars"),
        clones=integer("clones"),
        issues=integer("issues"),
        conversions=integer("conversions"),
        real_revenue_usd=number("real_revenue_usd"),
        consecutive_zero_cycles=integer("consecutive_zero_cycles"),
    )
