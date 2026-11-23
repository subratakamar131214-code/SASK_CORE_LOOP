"""Ruthless pruning decisions, registry persistence, and replica transitions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from src.core.errors import InvariantViolation, StateError
from src.evolution.pruner import (
    ACTION_ARCHIVE,
    ACTION_KEEP,
    ACTION_PAUSE,
    ACTION_SUNSET,
    PruneSignals,
    PruningEngine,
    PruningRegistry,
)
from src.swarm.models import ChildReplica
from src.swarm.niche_lock import NicheLock


def _signals(**overrides: object) -> PruneSignals:
    values: dict[str, object] = {
        "asset_id": "ci-failure-diagnoser",
        "age_days": 60.0,
        "stale_days": 0.0,
    }
    values.update(overrides)
    return PruneSignals(**values)  # type: ignore[arg-type]


def _child(status: str = "active") -> ChildReplica:
    return ChildReplica(
        child_id="child-ci",
        niche_id="ci-failure-diagnoser",
        mode="workspace",
        status=status,
        parent_digest="a" * 64,
        workspace="swarm/children/child-ci",
        repo=None,
        approval_by="maintainer-jordan",
        approval_reference="https://github.com/example/SASK_CORE_LOOP/issues/7",
        created_at="2026-09-01T00:00:00Z",
    )


def test_owner_vault_is_never_pruned(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(asset_id="OWNER_VAULT", stale_days=365.0, consecutive_zero_cycles=99)
    )
    assert decision.action == ACTION_KEEP
    assert "OWNER_VAULT" in decision.reason


def test_disabled_pruning_keeps_everything(config) -> None:
    engine = PruningEngine(replace(config.pruning, enabled=False))
    decision = engine.decide(
        _signals(stale_days=365.0, consecutive_zero_cycles=99)
    )
    assert decision.action == ACTION_KEEP
    assert "disabled" in decision.reason


def test_young_assets_are_not_judged(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(age_days=3.0, stale_days=365.0, consecutive_zero_cycles=99)
    )
    assert decision.action == ACTION_KEEP
    assert "too young" in decision.reason


def test_stale_zero_pull_assets_are_archived(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(
            age_days=120.0,
            stale_days=config.pruning.sunset_after_stale_days + 1.0,
            consecutive_zero_cycles=config.pruning.sunset_after_zero_cycles + 1,
        )
    )
    assert decision.action == ACTION_ARCHIVE
    assert decision.pruned is True


def test_missed_sunset_window_sunsets_without_pull(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(stale_days=config.pruning.sunset_after_stale_days + 1.0)
    )
    assert decision.action == ACTION_SUNSET


def test_no_pull_inside_pause_window_pauses(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(stale_days=config.pruning.pause_after_stale_days + 1.0)
    )
    assert decision.action == ACTION_PAUSE


def test_real_pull_protects_an_asset(config) -> None:
    engine = PruningEngine(config.pruning)
    for pull in (
        {"adoption": 0.3},
        {"repeat_usage": 4},
        {"stars": 12},
        {"conversions": 1},
        {"real_revenue_usd": 25.0},
    ):
        assert engine.decide(_signals(**pull)).action == ACTION_KEEP


def test_stale_asset_with_pull_is_paused_not_killed(config) -> None:
    engine = PruningEngine(config.pruning)
    decision = engine.decide(
        _signals(
            adoption=0.4,
            stale_days=config.pruning.sunset_after_stale_days + 1.0,
        )
    )
    assert decision.action == ACTION_PAUSE


def test_invalid_signals_are_rejected() -> None:
    with pytest.raises(StateError, match="cannot be negative"):
        PruneSignals(asset_id="x", age_days=-1.0)
    with pytest.raises(StateError, match="between 0 and 1"):
        PruneSignals(asset_id="x", adoption=1.5)
    with pytest.raises(StateError, match="asset_id"):
        PruneSignals(asset_id=" ")


def test_registry_persists_and_clears_decisions(config, tmp_path: Path) -> None:
    engine = PruningEngine(config.pruning)
    registry = PruningRegistry(tmp_path / "state" / "pruning.json")
    archived = engine.decide(
        _signals(
            asset_id="openapi-schema-migrator",
            stale_days=config.pruning.sunset_after_stale_days + 1.0,
            consecutive_zero_cycles=config.pruning.sunset_after_zero_cycles + 1,
        )
    )
    snapshot = registry.record([archived])
    assert snapshot.archived == ("openapi-schema-migrator",)
    reloaded = registry.load()
    assert reloaded.archived == ("openapi-schema-migrator",)
    assert registry.path.exists()

    keep = engine.decide(_signals(asset_id="openapi-schema-migrator"))
    cleared = registry.record([keep])
    assert cleared.archived == ()
    assert registry.load().archived == ()


def test_dry_run_registry_writes_nothing(config, tmp_path: Path) -> None:
    engine = PruningEngine(config.pruning)
    path = tmp_path / "pruning.json"
    registry = PruningRegistry(path, dry_run=True)
    decision = engine.decide(
        _signals(stale_days=config.pruning.sunset_after_stale_days + 1.0)
    )
    snapshot = registry.record([decision])
    assert snapshot.sunset == ("ci-failure-diagnoser",)
    assert not path.exists()


def test_replica_transitions_pause_and_retire(tmp_path: Path) -> None:
    lock = NicheLock(tmp_path / "swarm.json", max_children=2)
    lock.add(_child())
    paused = lock.transition("child-ci", "paused")
    assert paused.status == "paused"
    resumed = lock.transition("child-ci", "active")
    assert resumed.status == "active"
    retired = lock.transition("child-ci", "retired")
    assert retired.status == "retired"
    with pytest.raises(StateError, match="illegal child transition"):
        lock.transition("child-ci", "active")


def test_transition_rejects_unknown_replica(tmp_path: Path) -> None:
    lock = NicheLock(tmp_path / "swarm.json", max_children=2)
    with pytest.raises(StateError, match="unknown child"):
        lock.transition("child-missing", "paused")


def test_transition_cannot_double_lock_a_niche(tmp_path: Path) -> None:
    lock = NicheLock(tmp_path / "swarm.json", max_children=3)
    lock.add(_child())
    lock.transition("child-ci", "paused")
    lock.add(
        ChildReplica(
            child_id="child-ci-2",
            niche_id="ci-failure-diagnoser",
            mode="workspace",
            status="active",
            parent_digest="b" * 64,
            workspace="swarm/children/child-ci-2",
            repo=None,
            approval_by="maintainer-jordan",
            approval_reference="https://github.com/example/SASK_CORE_LOOP/issues/8",
            created_at="2026-09-02T00:00:00Z",
        )
    )
    with pytest.raises(InvariantViolation, match="locked"):
        lock.transition("child-ci", "active")
