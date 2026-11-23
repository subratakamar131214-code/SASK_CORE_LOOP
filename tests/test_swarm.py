"""Tests for one-niche child replicas, the spawner, and telemetry aggregation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from src.core.config import SystemConfig
from src.core.errors import ApprovalRequired, InvariantViolation, StateError
from src.core.invariants import InvariantGuard
from src.core.paths import RepoPaths
from src.ledger.ledger import Ledger
from src.ops.gh import GitHubClient
from src.swarm.aggregator import SwarmAggregator
from src.swarm.models import ChildReplica
from src.swarm.niche_lock import NicheLock
from src.swarm.spawner import SwarmSpawner, load_approval

from helpers import FrozenClock, make_approval


def _child(
    child_id: str = "sask-child-ci-failure-diagnoser",
    niche_id: str = "ci-failure-diagnoser",
    *,
    guard: InvariantGuard,
    status: str = "active",
    mode: str = "workspace",
    created_at: str = "2026-11-20T12:00:00Z",
    last_heartbeat: str | None = "2026-11-20T12:00:00Z",
) -> ChildReplica:
    return ChildReplica(
        child_id=child_id,
        niche_id=niche_id,
        mode=mode,
        status=status,
        parent_digest=guard.digest,
        workspace=f"swarm/children/{child_id}",
        repo=None,
        approval_by="maintainer-jordan",
        approval_reference="https://github.com/example/SASK_CORE_LOOP/issues/7",
        created_at=created_at,
        last_heartbeat=last_heartbeat,
    )


def test_child_replica_validation(guard: InvariantGuard) -> None:
    valid = _child(guard=guard).to_json()
    assert ChildReplica.from_json(valid).child_id == valid["child_id"]
    for field, value in (
        ("child_id", "Bad Id"),
        ("niche_id", "../escape"),
        ("mode", "cloud"),
        ("status", "running"),
        ("parent_digest", "not-a-digest"),
        ("approval_by", " "),
    ):
        mutated = dict(valid)
        mutated[field] = value
        with pytest.raises(StateError):
            ChildReplica.from_json(mutated)


def test_niche_lock_enforces_cap_and_uniqueness(
    tmp_path: Path, guard: InvariantGuard
) -> None:
    lock = NicheLock(tmp_path / "swarm_registry.json", max_children=2)
    first = _child(guard=guard)
    lock.add(first)
    with pytest.raises(InvariantViolation, match="already locked"):
        lock.add(_child("other-child", guard=guard))
    with pytest.raises(InvariantViolation, match="already registered"):
        lock.add(_child(guard=guard))
    lock.add(
        _child(
            "sask-child-openapi-schema-migrator",
            "openapi-schema-migrator",
            guard=guard,
        )
    )
    with pytest.raises(InvariantViolation, match="cap reached"):
        lock.add(_child("third-child", "third-niche", guard=guard))
    assert lock.active_niche_ids() == ["ci-failure-diagnoser", "openapi-schema-migrator"]
    released = lock.release(first.child_id)
    assert released.status == "retired"
    assert lock.active_niche_ids() == ["openapi-schema-migrator"]


def test_niche_lock_heartbeat_and_dry_run(tmp_path: Path, guard: InvariantGuard) -> None:
    path = tmp_path / "swarm_registry.json"
    lock = NicheLock(path, max_children=1)
    lock.add(_child(guard=guard))
    lock.heartbeat(
        "sask-child-ci-failure-diagnoser", "2026-11-21T00:00:00Z"
    )
    assert lock.children()[0].last_heartbeat == "2026-11-21T00:00:00Z"
    assert lock.last_spawn_at() == "2026-11-20T12:00:00Z"
    with pytest.raises(StateError, match="unknown child"):
        lock.release("missing-child")
    with pytest.raises(StateError):
        lock.heartbeat("sask-child-ci-failure-diagnoser", "yesterday")

    dry_path = tmp_path / "dry" / "swarm_registry.json"
    dry_lock = NicheLock(dry_path, max_children=1, dry_run=True)
    dry_lock.add(_child(guard=guard))
    assert not dry_path.exists()


def _spawner(
    config: SystemConfig,
    guard: InvariantGuard,
    paths: RepoPaths,
    *,
    clock: FrozenClock,
    max_children: int = 3,
    interval_hours: float = 168.0,
    enabled: bool = True,
    allow_repo_creation: bool = False,
    github: GitHubClient | None = None,
) -> tuple[SwarmSpawner, Ledger, NicheLock]:
    swarm = replace(
        config.swarm,
        enabled=enabled,
        max_children=max_children,
        min_spawn_interval_hours=interval_hours,
        allow_repo_creation=allow_repo_creation,
    )
    ledger = Ledger(paths.ledger_file, guard)
    ledger.ensure_genesis(guard.digest)
    lock = NicheLock(paths.state_dir / "swarm_registry.json", max_children=max_children)
    spawner = SwarmSpawner(
        swarm,
        guard,
        paths,
        ledger,
        lock,
        repo_owner="example",
        github=github,
        clock=clock,
    )
    return spawner, ledger, lock


def test_spawner_requires_enabled_swarm(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, _ = _spawner(
        config, guard, runtime_paths, clock=FrozenClock(), enabled=False
    )
    with pytest.raises(InvariantViolation, match="disabled"):
        spawner.spawn(
            config.niche_by_id("ci-failure-diagnoser"), make_approval()
        )


def test_spawner_requires_approval(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, _ = _spawner(config, guard, runtime_paths, clock=FrozenClock())
    with pytest.raises(ApprovalRequired):
        spawner.spawn(config.niche_by_id("ci-failure-diagnoser"), None)


def test_spawner_scaffolds_single_niche_child(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    clock = FrozenClock()
    spawner, ledger, lock = _spawner(
        config, guard, runtime_paths, clock=clock
    )
    niche = config.niche_by_id("ci-failure-diagnoser")
    result = spawner.spawn(niche, make_approval())
    assert result.child.status == "active"
    assert result.child.parent_digest == guard.digest
    assert result.child.niche_id == niche.id
    assert result.ledger_seq >= 2
    assert lock.active_niche_ids() == [niche.id]

    workspace = runtime_paths.swarm_dir / result.child.child_id
    child_config = yaml.safe_load(
        (workspace / "config" / "child_system.yaml").read_text(encoding="utf-8")
    )
    assert [item["id"] for item in child_config["niches"] if item["active"]] == [niche.id]
    assert child_config["swarm"]["enabled"] is False
    assert child_config["swarm"]["allow_repo_creation"] is False
    assert child_config["monetization"]["enabled"] is False
    assert child_config["repo"]["owner"] == "example"

    policy = (workspace / "config" / "child_policy.json").read_text(encoding="utf-8")
    assert '"allow_invariant_override": false' in policy
    assert '"replication_allowed": false' in policy
    readme = (workspace / "README.md").read_text(encoding="utf-8")
    assert guard.digest in readme
    assert niche.id in readme

    entries = [entry for entry in ledger.entries() if entry.get("kind") == "spawn"]
    assert len(entries) == 1
    assert entries[0]["payload"]["niche_id"] == niche.id


def test_spawner_enforces_interval_and_unique_ids(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    clock = FrozenClock()
    spawner, _, lock = _spawner(
        config, guard, runtime_paths, clock=clock, interval_hours=0.0
    )
    niche = config.niche_by_id("ci-failure-diagnoser")
    first = spawner.spawn(niche, make_approval())
    with pytest.raises(InvariantViolation, match="already locked"):
        spawner.spawn(niche, make_approval())
    lock.release(first.child.child_id)
    second = spawner.spawn(niche, make_approval())
    assert second.child.child_id == f"{first.child.child_id}-2"


def test_spawner_enforces_min_interval(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, _ = _spawner(config, guard, runtime_paths, clock=FrozenClock())
    spawner.spawn(config.niche_by_id("ci-failure-diagnoser"), make_approval())
    with pytest.raises(InvariantViolation, match="minimum spawn interval"):
        spawner.spawn(
            config.niche_by_id("openapi-schema-migrator"), make_approval()
        )


def test_spawner_enforces_child_cap(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, lock = _spawner(
        config, guard, runtime_paths, clock=FrozenClock(), max_children=1
    )
    lock.add(_child(guard=guard))
    with pytest.raises(InvariantViolation, match="cap reached"):
        spawner.spawn(
            config.niche_by_id("openapi-schema-migrator"), make_approval()
        )


def test_spawner_repo_mode_requires_allow_flag(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, _ = _spawner(
        config,
        guard,
        runtime_paths,
        clock=FrozenClock(),
        allow_repo_creation=False,
        github=GitHubClient(dry_run=True),
    )
    with pytest.raises(InvariantViolation, match="repository creation"):
        spawner.spawn(
            config.niche_by_id("ci-failure-diagnoser"), make_approval(), mode="repo"
        )


def test_spawner_repo_mode_dry_run_github(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    spawner, _, _ = _spawner(
        config,
        guard,
        runtime_paths,
        clock=FrozenClock(),
        allow_repo_creation=True,
        github=GitHubClient(dry_run=True),
    )
    result = spawner.spawn(
        config.niche_by_id("ci-failure-diagnoser"), make_approval(), mode="repo"
    )
    assert result.child.mode == "repo"
    assert result.child.repo == "example/sask-child-ci-failure-diagnoser"
    assert result.repo_url == (
        "https://github.com/example/sask-child-ci-failure-diagnoser"
    )


def test_aggregator_marks_stale_children(
    config: SystemConfig, guard: InvariantGuard, runtime_paths: RepoPaths
) -> None:
    clock = FrozenClock()
    spawner, _, lock = _spawner(config, guard, runtime_paths, clock=clock)
    result = spawner.spawn(
        config.niche_by_id("ci-failure-diagnoser"), make_approval()
    )
    aggregator = SwarmAggregator(runtime_paths, config.swarm, lock, clock=clock)
    fresh = aggregator.rollup()
    assert fresh.active_count == 1
    assert fresh.stale_count == 0
    clock.advance(config.swarm.telemetry_stale_hours + 28)
    stale = aggregator.rollup()
    assert stale.stale_count == 1
    assert stale.children[0].stale is True

    telemetry_path = (
        runtime_paths.swarm_dir / result.child.child_id / "state" / "telemetry.json"
    )
    telemetry_path.write_text("[]", encoding="utf-8")
    invalid = aggregator.rollup()
    assert invalid.invalid_telemetry >= 1


def test_load_approval_file(tmp_path: Path) -> None:
    path = tmp_path / "approval.yaml"
    path.write_text(
        "action: replication\n"
        "approved_by: maintainer-jordan\n"
        "reference: https://github.com/example/SASK_CORE_LOOP/issues/7\n"
        "approved_at: 2026-11-01T00:00:00Z\n",
        encoding="utf-8",
    )
    approval = load_approval(path)
    assert approval.action == "replication"
    assert approval.approved_by == "maintainer-jordan"

    path.write_text("action: replication\n", encoding="utf-8")
    with pytest.raises(StateError, match="approved_by"):
        load_approval(path)
