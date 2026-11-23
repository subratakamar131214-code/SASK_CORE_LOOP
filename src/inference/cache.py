"""Bounded, sanitized response cache to conserve free-tier quotas within a day."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from src.core.errors import StateError
from src.core.io import file_lock, read_json, write_json_atomic
from src.core.redact import assert_no_secrets, redact_text
from src.core.timeutil import hours_since, iso_now, utc_now
from src.core.types import JsonObject, JsonValue


@dataclass(frozen=True)
class CachedResponse:
    text: str
    provider: str
    model: str


class ResponseCache:
    def __init__(
        self,
        path: Path,
        *,
        ttl_hours: int,
        enabled: bool = True,
        dry_run: bool = False,
        max_entries: int = 200,
    ) -> None:
        self._path = path
        self._ttl_hours = max(1, ttl_hours)
        self._enabled = enabled and not dry_run
        self._max_entries = max(1, max_entries)

    def _read(self) -> dict[str, JsonObject]:
        raw = read_json(self._path, None)
        if raw is None:
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != 1:
            raise StateError("inference cache is malformed")
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            raise StateError("inference cache entries are malformed")
        result: dict[str, JsonObject] = {}
        for key, value in entries.items():
            if isinstance(key, str) and isinstance(value, dict):
                result[key] = value
        return result

    def get(self, key: str) -> CachedResponse | None:
        if not self._enabled:
            return None
        entries = self._read()
        entry = entries.get(key)
        if entry is None:
            return None
        timestamp = entry.get("ts")
        text = entry.get("text")
        provider = entry.get("provider")
        model = entry.get("model")
        if not all(isinstance(value, str) for value in (timestamp, text, provider, model)):
            return None
        try:
            age = hours_since(cast(str, timestamp))
        except ValueError:
            return None
        if age > self._ttl_hours:
            return None
        return CachedResponse(
            text=cast(str, text),
            provider=cast(str, provider),
            model=cast(str, model),
        )

    def put(self, key: str, *, text: str, provider: str, model: str) -> None:
        if not self._enabled:
            return
        sanitized = redact_text(text).text
        assert_no_secrets(sanitized, context="inference cache")
        with file_lock(self._path.with_name(self._path.name + ".lock")):
            entries = self._read()
            entries[key] = {
                "ts": iso_now(utc_now()),
                "text": sanitized,
                "provider": provider,
                "model": model,
            }
            if len(entries) > self._max_entries:
                ordered = sorted(
                    entries.items(),
                    key=lambda item: str(item[1].get("ts") or ""),
                    reverse=True,
                )
                entries = dict(ordered[: self._max_entries])
            write_json_atomic(
                self._path,
                {"schema": 1, "entries": cast(JsonValue, entries)},
            )
