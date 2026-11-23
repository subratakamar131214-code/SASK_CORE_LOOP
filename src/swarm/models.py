"""Child replica records and strict serialization."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.core.errors import StateError
from src.core.types import JsonObject, JsonValue

CHILD_MODES = ("workspace", "repo")
CHILD_STATUSES = ("planned", "active", "paused", "retired")
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


@dataclass(frozen=True)
class ChildReplica:
    child_id: str
    niche_id: str
    mode: str
    status: str
    parent_digest: str
    workspace: str
    repo: str | None
    approval_by: str
    approval_reference: str
    created_at: str
    last_heartbeat: str | None = None

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.child_id):
            raise StateError(f"unsafe child id: {self.child_id!r}")
        if not _SAFE_ID.fullmatch(self.niche_id):
            raise StateError(f"unsafe child niche: {self.niche_id!r}")
        if self.mode not in CHILD_MODES:
            raise StateError(f"unsupported child mode: {self.mode!r}")
        if self.status not in CHILD_STATUSES:
            raise StateError(f"unsupported child status: {self.status!r}")
        if not re.fullmatch(r"[0-9a-f]{64}", self.parent_digest):
            raise StateError("child must pin a sha256 parent invariant digest")
        if not self.approval_by.strip() or not self.approval_reference.strip():
            raise StateError("child replicas require recorded human approval")

    def to_json(self) -> JsonObject:
        return {
            "child_id": self.child_id,
            "niche_id": self.niche_id,
            "mode": self.mode,
            "status": self.status,
            "parent_digest": self.parent_digest,
            "workspace": self.workspace,
            "repo": self.repo,
            "approval_by": self.approval_by,
            "approval_reference": self.approval_reference,
            "created_at": self.created_at,
            "last_heartbeat": self.last_heartbeat,
        }

    @classmethod
    def from_json(cls, value: JsonValue) -> ChildReplica:
        if not isinstance(value, dict):
            raise StateError("child replica entry is not an object")
        fields = (
            "child_id",
            "niche_id",
            "mode",
            "status",
            "parent_digest",
            "workspace",
            "approval_by",
            "approval_reference",
            "created_at",
        )
        data: dict[str, str] = {}
        for field in fields:
            item = value.get(field)
            if not isinstance(item, str):
                raise StateError(f"child replica field {field} is missing")
            data[field] = item
        repo = value.get("repo")
        if repo is not None and not isinstance(repo, str):
            raise StateError("child replica repo is invalid")
        heartbeat = value.get("last_heartbeat")
        if heartbeat is not None and not isinstance(heartbeat, str):
            raise StateError("child replica last_heartbeat is invalid")
        return cls(**data, repo=repo, last_heartbeat=heartbeat)
