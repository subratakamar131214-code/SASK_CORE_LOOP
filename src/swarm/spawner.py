"""Approval-gated child replica spawner with one-niche and digest pinning."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from src.core.config import NicheConfig, SwarmConfig
from src.core.errors import ApprovalRequired, InvariantViolation, StateError
from src.core.invariants import Approval, InvariantGuard
from src.core.io import ensure_dir, write_json_atomic, write_text_atomic
from src.core.paths import RepoPaths
from src.core.redact import sanitize_public_payload
from src.core.timeutil import hours_since, iso_now, to_iso, utc_now
from src.core.types import JsonObject
from src.ledger.ledger import Ledger
from src.ops.gh import GitHubClient
from src.swarm.models import ChildReplica
from src.swarm.niche_lock import NicheLock

_CHILD_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")
_CHILD_README = """\
# {child_id}

Child replica for the `{niche_id}` niche.

- Parent invariants digest: `{digest}`
- Mode: `{mode}`
- One niche only: `{niche_id}`
- Invariant overrides: forbidden
- Replication: disabled for children
- Human approval: {approval_by} ({approval_reference})

## B2B rationale

- Workflow bottleneck: {workflow_bottleneck}
- Budget owner: {budget_owner}
- Why recurring reliability matters: {reliability_rationale}

## Bootstrap

1. Review `config/child_system.yaml`; only `{niche_id}` is active.
2. Copy the parent engine source (`src/`, `pyproject.toml`, workflows) into this
   workspace or repository at a reviewed parent commit.
3. Run `python -m src.orchestrator --cycle --offline` and publish a sanitized state
   commit. Re-run `--heartbeat` from the child workflow.
4. The child must not modify `config/invariants.yaml`, must never store secrets,
   and must not enable paid APIs or automated payouts.
"""


@dataclass(frozen=True)
class SpawnResult:
    child: ChildReplica
    workspace_display: str
    repo_url: str | None
    ledger_seq: int

    def to_json(self) -> JsonObject:
        return {
            "child": self.child.to_json(),
            "workspace": self.workspace_display,
            "repo_url": self.repo_url,
            "ledger_seq": self.ledger_seq,
        }


class SwarmSpawner:
    def __init__(
        self,
        config: SwarmConfig,
        guard: InvariantGuard,
        paths: RepoPaths,
        ledger: Ledger,
        lock: NicheLock,
        *,
        repo_owner: str | None = None,
        github: GitHubClient | None = None,
        dry_run: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._guard = guard
        self._paths = paths
        self._ledger = ledger
        self._lock = lock
        self._repo_owner = repo_owner
        self._github = github
        self._dry_run = dry_run
        self._clock = clock

    def spawn(
        self,
        niche: NicheConfig,
        approval: Approval | None,
        *,
        mode: str | None = None,
    ) -> SpawnResult:
        if not self._config.enabled:
            raise InvariantViolation("swarm replication is disabled in config/system.yaml")
        selected_mode = mode or self._config.default_mode
        if selected_mode not in {"workspace", "repo"}:
            raise StateError(f"unsupported child mode: {selected_mode!r}")
        if selected_mode == "repo" and not self._config.allow_repo_creation:
            raise InvariantViolation("child repository creation is disabled by config")
        if self._config.require_human_approval and approval is None:
            raise ApprovalRequired("human approval is required before spawning a child")
        if not niche.active:
            raise InvariantViolation(f"niche {niche.id!r} is not active")
        children = self._lock.children()
        active = [child for child in children if child.status == "active"]
        if len(active) >= self._config.max_children:
            raise InvariantViolation(
                f"swarm child cap reached ({len(active)}/{self._config.max_children})"
            )
        last_spawn = self._lock.last_spawn_at()
        if last_spawn is not None:
            try:
                elapsed = hours_since(last_spawn, self._clock())
            except ValueError as exc:
                raise StateError("swarm registry has an invalid spawn timestamp") from exc
            if elapsed < self._config.min_spawn_interval_hours:
                raise InvariantViolation(
                    "minimum spawn interval not reached "
                    f"({elapsed:.1f}h < {self._config.min_spawn_interval_hours:g}h)"
                )
        self._guard.assert_replication_allowed(
            approval=approval,
            children_count=len(active),
            requested_niche_id=niche.id,
            active_niche_ids=self._lock.active_niche_ids(),
            child_invariants_digest=self._guard.digest,
            repo_mode=selected_mode == "repo",
        )
        if approval is None:
            raise ApprovalRequired("child replicas require recorded human approval")
        child_id = self._next_child_id(niche.id, self._lock.child_ids())
        repo_name: str | None = None
        repo_url: str | None = None
        if selected_mode == "repo":
            if self._github is None:
                raise StateError("repo mode requires a GitHub client")
            repo_ref = self._github.create_public_repo(
                child_id,
                description=f"SASK child replica: {niche.title}",
                owner=self._repo_owner,
            )
            repo_name = repo_ref.full_name
            repo_url = repo_ref.url
        workspace = self._paths.swarm_dir / child_id
        self._scaffold_workspace(workspace, child_id, niche, approval, selected_mode, repo_name)
        child = ChildReplica(
            child_id=child_id,
            niche_id=niche.id,
            mode=selected_mode,
            status="active",
            parent_digest=self._guard.digest,
            workspace=self._paths.display(workspace),
            repo=repo_name,
            approval_by=approval.approved_by,
            approval_reference=approval.reference,
            created_at=iso_now(self._clock()),
            last_heartbeat=iso_now(self._clock()),
        )
        self._lock.add(child)
        try:
            entry = self._ledger.append(
                "spawn",
                {
                    "child_id": child_id,
                    "niche_id": niche.id,
                    "mode": selected_mode,
                    "repo": repo_name,
                    "workspace": child.workspace,
                    "child_invariants_digest": self._guard.digest,
                    "approval_by": approval.approved_by,
                    "approval_reference": approval.reference,
                    "hard_cap_usd": 0.0,
                },
            )
        except Exception:
            if not self._dry_run:
                self._lock.release(child_id)
            raise
        seq = entry.get("seq")
        return SpawnResult(
            child=child,
            workspace_display=child.workspace,
            repo_url=repo_url,
            ledger_seq=seq if isinstance(seq, int) else 0,
        )

    def _next_child_id(self, niche_id: str, existing: list[str]) -> str:
        base = f"{self._config.repo_prefix}{niche_id}"
        if not _CHILD_ID.fullmatch(base):
            raise StateError(f"unsafe child id derived from prefix and niche: {base!r}")
        if base not in existing:
            return base
        for index in range(2, 100):
            candidate = f"{base}-{index}"
            if candidate not in existing:
                return candidate
        raise StateError("cannot allocate a unique child id")

    def _scaffold_workspace(
        self,
        workspace: Path,
        child_id: str,
        niche: NicheConfig,
        approval: Approval,
        mode: str,
        repo: str | None,
    ) -> None:
        root = self._paths.swarm_dir.resolve()
        target = workspace.resolve()
        if root != target.parent:
            raise StateError(f"refusing to scaffold outside the swarm directory: {target}")
        ensure_dir(workspace / "config")
        ensure_dir(workspace / "state")
        child_config = self._child_system_config(child_id, niche)
        write_text_atomic(workspace / "config" / "child_system.yaml", child_config)
        invariants_source = self._paths.invariants_file
        if not invariants_source.is_file():
            raise StateError("parent invariants file is missing")
        write_text_atomic(
            workspace / "config" / "invariants.yaml",
            invariants_source.read_text(encoding="utf-8"),
        )
        policy: JsonObject = {
            "child_id": child_id,
            "niche_id": niche.id,
            "mode": mode,
            "repo": repo,
            "parent_invariants_digest": self._guard.digest,
            "allow_invariant_override": False,
            "replication_allowed": False,
            "max_children": 0,
            "secrets_location": "platform secret store only",
            "workflow_bottleneck": niche.workflow_bottleneck,
            "budget_owner": niche.budget_owner,
            "reliability_rationale": niche.reliability_rationale,
        }
        write_json_atomic(workspace / "config" / "child_policy.json", policy)
        telemetry: JsonObject = {
            "child_id": child_id,
            "niche_id": niche.id,
            "last_heartbeat": iso_now(self._clock()),
            "real_revenue_usd": 0.0,
            "simulated_revenue_usd": 0.0,
            "cost_usd": 0.0,
            "ledger_entries": 0,
            "chain_valid": True,
            "status": "active",
        }
        write_json_atomic(workspace / "state" / "telemetry.json", telemetry)
        readme = _CHILD_README.format(
            child_id=child_id,
            niche_id=niche.id,
            digest=self._guard.digest,
            mode=mode,
            approval_by=approval.approved_by,
            approval_reference=approval.reference,
            workflow_bottleneck=niche.workflow_bottleneck,
            budget_owner=niche.budget_owner,
            reliability_rationale=niche.reliability_rationale,
        )
        write_text_atomic(workspace / "README.md", readme)
        write_text_atomic(
            workspace / ".gitignore",
            "state/*.lock\n.env\n.env.*\n!.env.example\n*.key\n*.pem\n",
        )

    def _child_system_config(self, child_id: str, niche: NicheConfig) -> str:
        try:
            document = yaml.safe_load(
                self._paths.system_config_file.read_text(encoding="utf-8")
            )
        except (OSError, yaml.YAMLError) as exc:
            raise StateError("cannot read the parent system config") from exc
        if not isinstance(document, dict):
            raise StateError("parent system config is not a mapping")
        repo = document.setdefault("repo", {})
        if not isinstance(repo, dict):
            raise StateError("parent repo config is malformed")
        repo["name"] = child_id
        repo["visibility"] = "public"
        repo["owner"] = self._repo_owner or "OWNER_PLACEHOLDER"
        niches = document.get("niches")
        if not isinstance(niches, list):
            raise StateError("parent niches config is malformed")
        for item in niches:
            if isinstance(item, dict):
                item["active"] = item.get("id") == niche.id
        swarm = document.setdefault("swarm", {})
        if not isinstance(swarm, dict):
            raise StateError("parent swarm config is malformed")
        swarm["enabled"] = False
        swarm["allow_repo_creation"] = False
        swarm["default_mode"] = "workspace"
        monetization = document.setdefault("monetization", {})
        if isinstance(monetization, dict):
            monetization["enabled"] = False
        runtime = document.setdefault("runtime", {})
        if isinstance(runtime, dict):
            runtime["offline_by_default"] = True
        sanitized, _ = sanitize_public_payload(document)
        if not isinstance(sanitized, dict):
            raise StateError("child system config must remain a mapping")
        return yaml.safe_dump(sanitized, sort_keys=False, allow_unicode=True)


def load_approval(path: Path) -> Approval:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise StateError("approval file must contain a mapping")
    action = raw.get("action")
    approved_by = raw.get("approved_by")
    reference = raw.get("reference")
    approved_at = raw.get("approved_at")
    if isinstance(approved_at, datetime):
        approved_at = to_iso(approved_at)
    if not isinstance(action, str) or not action.strip():
        raise StateError("approval file is missing action")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise StateError("approval file is missing approved_by")
    if not isinstance(reference, str) or not reference.strip():
        raise StateError("approval file is missing reference")
    if not isinstance(approved_at, str) or not approved_at.strip():
        raise StateError("approval file is missing approved_at")
    return Approval(
        action=action,
        approved_by=approved_by,
        reference=reference,
        approved_at=approved_at,
    )

