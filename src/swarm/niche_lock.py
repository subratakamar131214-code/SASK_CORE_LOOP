"""Persistent one-niche-per-child lock with a hard child cap."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path

from src.core.errors import InvariantViolation, StateError
from src.core.io import file_lock, read_json, write_json_atomic
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now, parse_iso
from src.core.types import JsonObject, JsonValue
from src.swarm.models import ChildReplica

SCHEMA_VERSION = 1


class NicheLock:
    def __init__(
        self,
        path: Path,
        *,
        max_children: int,
        dry_run: bool = False,
    ) -> None:
        self._path = path
        self._max_children = max(0, max_children)
        self._dry_run = dry_run

    @property
    def path(self) -> Path:
        return self._path

    def children(self) -> list[ChildReplica]:
        return self._children(self._read())

    def active_children(self) -> list[ChildReplica]:
        return [child for child in self.children() if child.status == "active"]

    def active_niche_ids(self) -> list[str]:
        return [child.niche_id for child in self.active_children()]

    def child_ids(self) -> list[str]:
        return [child.child_id for child in self.children()]

    def add(self, child: ChildReplica) -> None:
        if child.status != "active":
            raise StateError("only active children can hold a niche lock")
        with self._guard_lock():
            state = self._read()
            existing = self._children(state)
            self._check_add(existing, child)
            state["children"] = [*self._raw_children(state), child.to_json()]
            state["updated_at"] = iso_now()
            self._write(state)

    def release(self, child_id: str) -> ChildReplica:
        with self._guard_lock():
            state = self._read()
            target = self._find(self._children(state), child_id)
            updated = replace(target, status="retired")
            state["children"] = [
                updated.to_json() if item.child_id == child_id else item.to_json()
                for item in self._children(state)
            ]
            state["updated_at"] = iso_now()
            self._write(state)
            return updated

    def heartbeat(self, child_id: str, timestamp: str) -> None:
        try:
            parse_iso(timestamp)
        except ValueError as exc:
            raise StateError(f"invalid heartbeat timestamp: {timestamp!r}") from exc
        with self._guard_lock():
            state = self._read()
            target = self._find(self._children(state), child_id)
            updated = replace(target, last_heartbeat=timestamp)
            state["children"] = [
                updated.to_json() if item.child_id == child_id else item.to_json()
                for item in self._children(state)
            ]
            state["updated_at"] = iso_now()
            self._write(state)

    def last_spawn_at(self) -> str | None:
        stamps = [child.created_at for child in self.children()]
        return max(stamps) if stamps else None

    def _check_add(self, existing: list[ChildReplica], child: ChildReplica) -> None:
        if any(item.child_id == child.child_id for item in existing):
            raise InvariantViolation(f"child id already registered: {child.child_id}")
        if any(
            item.status == "active" and item.niche_id == child.niche_id for item in existing
        ):
            raise InvariantViolation(f"niche {child.niche_id!r} is already locked by a child")
        active = [item for item in existing if item.status == "active"]
        if len(active) >= self._max_children:
            raise InvariantViolation(
                f"swarm child cap reached ({len(active)}/{self._max_children})"
            )

    def _find(self, children: list[ChildReplica], child_id: str) -> ChildReplica:
        target = next((item for item in children if item.child_id == child_id), None)
        if target is None:
            raise StateError(f"unknown child replica: {child_id}")
        return target

    def _guard_lock(self) -> AbstractContextManager[None]:
        if self._dry_run:
            return nullcontext()
        return file_lock(self._path.with_name(self._path.name + ".lock"))

    def _empty(self) -> JsonObject:
        return {"schema_version": SCHEMA_VERSION, "updated_at": iso_now(), "children": []}

    def _read(self) -> JsonObject:
        raw = read_json(self._path, None)
        if raw is None:
            return self._empty()
        if not isinstance(raw, dict):
            raise StateError("swarm registry is not an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise StateError("unsupported swarm registry schema")
        raw_children = raw.get("children")
        if not isinstance(raw_children, list):
            raise StateError("swarm registry children is malformed")
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": raw.get("updated_at"),
            "children": raw_children,
        }

    @staticmethod
    def _raw_children(state: JsonObject) -> list[JsonValue]:
        raw = state.get("children")
        if not isinstance(raw, list):
            raise StateError("swarm registry children is malformed")
        return raw

    def _children(self, state: JsonObject) -> list[ChildReplica]:
        return [ChildReplica.from_json(item) for item in self._raw_children(state)]

    def _write(self, state: JsonObject) -> None:
        if self._dry_run:
            return
        sanitized, _ = sanitize_public_payload(state)
        if not isinstance(sanitized, dict):
            raise StateError("swarm registry must remain an object")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self._path, sanitized)

