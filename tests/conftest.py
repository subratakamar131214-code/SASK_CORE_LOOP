"""Session-wide fixtures bound to the real configuration and invariant pin."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.core.config import SystemConfig, validate_system_config
from src.core.invariants import Approval, InvariantGuard
from src.core.paths import RepoPaths


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def guard(repo_root: Path) -> InvariantGuard:
    return InvariantGuard.load(repo_root / "config" / "invariants.yaml", root=repo_root)


@pytest.fixture(scope="session")
def config(repo_root: Path, guard: InvariantGuard) -> SystemConfig:
    loaded = SystemConfig.load(repo_root / "config" / "system.yaml")
    validate_system_config(loaded, guard)
    return loaded


@pytest.fixture
def runtime_paths(repo_root: Path, tmp_path: Path) -> RepoPaths:
    paths = RepoPaths.load(repo_root, runtime_root=tmp_path)
    paths.ensure_runtime_dirs()
    return paths


@pytest.fixture
def approval() -> Approval:
    return Approval(
        action="replication",
        approved_by="maintainer-jordan",
        reference="https://github.com/example/SASK_CORE_LOOP/issues/7",
        approved_at="2026-11-01T00:00:00Z",
    )
