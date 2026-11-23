"""Persistent daily request budgets for free-tier providers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from src.core.errors import StateError
from src.core.io import file_lock, read_json, write_json_atomic
from src.core.timeutil import utc_day, utc_now
from src.core.types import JsonObject, JsonValue


class DailyBudget:
    def __init__(
        self,
        path: Path,
        *,
        global_limit: int,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._path = path
        self._global_limit = max(0, global_limit)
        self._dry_run = dry_run
        self._clock = clock
        self._memory: JsonObject = self._empty(utc_day(clock()))
        if not dry_run:
            self._memory = self._read()

    def _empty(self, day: str) -> JsonObject:
        return {"schema": 1, "date": day, "total": 0, "providers": {}}

    def _read(self) -> JsonObject:
        raw = read_json(self._path, None)
        if raw is None:
            return self._empty(utc_day(self._clock()))
        if not isinstance(raw, dict):
            raise StateError("inference budget file is not an object")
        day = raw.get("date")
        total = raw.get("total")
        providers = raw.get("providers")
        if (
            not isinstance(day, str)
            or not isinstance(total, int)
            or not isinstance(providers, dict)
        ):
            raise StateError("inference budget file is malformed")
        counts: dict[str, JsonValue] = {}
        for key, value in providers.items():
            if not isinstance(key, str) or not isinstance(value, int):
                raise StateError("inference budget provider counters are malformed")
            counts[key] = value
        return {"schema": 1, "date": day, "total": total, "providers": counts}

    def _reset_if_new_day(self, state: JsonObject) -> JsonObject:
        today = utc_day(self._clock())
        if state.get("date") != today:
            return self._empty(today)
        return state

    def try_consume(self, provider: str, provider_limit: int) -> bool:
        if self._dry_run:
            state = self._reset_if_new_day(self._memory)
            allowed = self._consume(state, provider, provider_limit)
            self._memory = state
            return allowed
        with file_lock(self._path.with_name(self._path.name + ".lock")):
            state = self._reset_if_new_day(self._read())
            allowed = self._consume(state, provider, provider_limit)
            if allowed:
                write_json_atomic(self._path, state)
            self._memory = state
            return allowed

    def _consume(self, state: JsonObject, provider: str, provider_limit: int) -> bool:
        total = state.get("total")
        providers = state.get("providers")
        if not isinstance(total, int) or not isinstance(providers, dict):
            raise StateError("inference budget state is malformed")
        current = providers.get(provider, 0)
        if not isinstance(current, int):
            raise StateError("inference budget provider counter is malformed")
        if total >= self._global_limit or current >= max(0, provider_limit):
            return False
        state["total"] = total + 1
        updated = dict(providers)
        updated[provider] = current + 1
        state["providers"] = updated
        return True

    def stats(self) -> JsonObject:
        state = self._reset_if_new_day(self._memory if self._dry_run else self._read())
        providers = state.get("providers")
        return {
            "date": cast(str, state.get("date")),
            "total": cast(int, state.get("total")),
            "global_limit": self._global_limit,
            "providers": cast(JsonObject, providers if isinstance(providers, dict) else {}),
        }
