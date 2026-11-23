"""Sanitized JSON/JSONL state persistence with dry-run support."""

from __future__ import annotations

import re
from pathlib import Path

from src.core.errors import StateError
from src.core.io import append_jsonl, file_lock, read_json, read_jsonl, write_json_atomic
from src.core.redact import sanitize_public_payload
from src.core.types import JsonValue

_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class StateStore:
    def __init__(self, state_dir: Path, *, dry_run: bool = False) -> None:
        self._state_dir = state_dir
        self._dry_run = dry_run

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def _name(self, name: str) -> str:
        if not _SAFE_NAME.fullmatch(name):
            raise StateError(f"unsafe state name: {name!r}")
        return name

    def json_path(self, name: str) -> Path:
        return self._state_dir / f"{self._name(name)}.json"

    def jsonl_path(self, name: str) -> Path:
        return self._state_dir / f"{self._name(name)}.jsonl"

    def read_json(self, name: str, default: JsonValue = None) -> JsonValue:
        return read_json(self.json_path(name), default)

    def read_jsonl(self, name: str) -> list[JsonValue]:
        return read_jsonl(self.jsonl_path(name))

    def write_json(self, name: str, value: JsonValue) -> Path | None:
        sanitized, _ = sanitize_public_payload(value)
        if self._dry_run:
            return None
        path = self.json_path(name)
        write_json_atomic(path, sanitized)
        return path

    def append_jsonl(self, name: str, value: JsonValue) -> Path | None:
        sanitized, _ = sanitize_public_payload(value)
        if self._dry_run:
            return None
        path = self.jsonl_path(name)
        with file_lock(path.with_name(path.name + ".lock")):
            append_jsonl(path, sanitized)
        return path
