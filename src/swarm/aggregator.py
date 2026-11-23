"""Parent telemetry roll-up across child replicas (no per-user data)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from src.core.config import SwarmConfig
from src.core.io import read_json
from src.core.paths import RepoPaths
from src.core.state import StateStore
from src.core.timeutil import hours_since, iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.swarm.models import ChildReplica
from src.swarm.niche_lock import NicheLock


@dataclass(frozen=True)
class ChildTelemetry:
    child_id: str
    niche_id: str
    status: str
    last_heartbeat: str | None
    real_revenue_usd: float
    simulated_revenue_usd: float
    cost_usd: float
    ledger_entries: int
    chain_valid: bool
    stale: bool

    def to_json(self) -> JsonObject:
        return {
            "child_id": self.child_id,
            "niche_id": self.niche_id,
            "status": self.status,
            "last_heartbeat": self.last_heartbeat,
            "real_revenue_usd": round(self.real_revenue_usd, 6),
            "simulated_revenue_usd": round(self.simulated_revenue_usd, 6),
            "cost_usd": round(self.cost_usd, 6),
            "ledger_entries": self.ledger_entries,
            "chain_valid": self.chain_valid,
            "stale": self.stale,
        }


@dataclass(frozen=True)
class SwarmRollup:
    children: tuple[ChildTelemetry, ...]
    active_count: int
    stale_count: int
    invalid_telemetry: int
    total_real_revenue_usd: float
    total_simulated_revenue_usd: float
    total_cost_usd: float
    generated_at: str

    def to_json(self) -> JsonObject:
        return {
            "active_count": self.active_count,
            "stale_count": self.stale_count,
            "invalid_telemetry": self.invalid_telemetry,
            "total_real_revenue_usd": round(self.total_real_revenue_usd, 6),
            "total_simulated_revenue_usd": round(self.total_simulated_revenue_usd, 6),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "generated_at": self.generated_at,
            "children": [child.to_json() for child in self.children],
        }


class SwarmAggregator:
    def __init__(
        self,
        paths: RepoPaths,
        config: SwarmConfig,
        lock: NicheLock,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._paths = paths
        self._config = config
        self._lock = lock
        self._clock = clock

    def rollup(self) -> SwarmRollup:
        now = self._clock()
        telemetry: list[ChildTelemetry] = []
        invalid = 0
        for child in self._lock.children():
            entry, problems = self._child_telemetry(child, now)
            invalid += problems
            telemetry.append(entry)
        active = [item for item in telemetry if item.status == "active"]
        return SwarmRollup(
            children=tuple(telemetry),
            active_count=len(active),
            stale_count=sum(1 for item in active if item.stale),
            invalid_telemetry=invalid,
            total_real_revenue_usd=sum(item.real_revenue_usd for item in telemetry),
            total_simulated_revenue_usd=sum(item.simulated_revenue_usd for item in telemetry),
            total_cost_usd=sum(item.cost_usd for item in telemetry),
            generated_at=iso_now(now),
        )

    def persist(self, store: StateStore, rollup: SwarmRollup | None = None) -> SwarmRollup:
        result = rollup or self.rollup()
        store.write_json("swarm", result.to_json())
        return result

    def _child_telemetry(
        self, child: ChildReplica, now: datetime
    ) -> tuple[ChildTelemetry, int]:
        path = self._paths.swarm_dir / child.child_id / "state" / "telemetry.json"
        raw = read_json(path, {})
        problems = 0
        if not isinstance(raw, dict):
            raw = {}
            problems += 1
        real = _number(raw.get("real_revenue_usd"))
        simulated = _number(raw.get("simulated_revenue_usd"))
        cost = _number(raw.get("cost_usd"))
        entries = _integer(raw.get("ledger_entries"))
        chain_valid = raw.get("chain_valid")
        if not isinstance(chain_valid, bool):
            chain_valid = False
            if raw:
                problems += 1
        last_heartbeat = raw.get("last_heartbeat")
        if last_heartbeat is not None and not isinstance(last_heartbeat, str):
            last_heartbeat = None
            problems += 1
        stamp = child.last_heartbeat or last_heartbeat or child.created_at
        stale = False
        if child.status != "retired":
            try:
                stale = hours_since(stamp, now) > self._config.telemetry_stale_hours
            except ValueError:
                stale = True
                problems += 1
        return (
            ChildTelemetry(
                child_id=child.child_id,
                niche_id=child.niche_id,
                status=child.status,
                last_heartbeat=stamp,
                real_revenue_usd=real,
                simulated_revenue_usd=simulated,
                cost_usd=cost,
                ledger_entries=entries,
                chain_valid=chain_valid,
                stale=stale,
            ),
            problems,
        )


def _number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0.0 else 0.0


def _integer(value: JsonValue) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)

