"""Repository and runtime path layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.core.io import ensure_dir


@dataclass(frozen=True)
class RepoPaths:
    root: Path
    config_dir: Path
    state_dir: Path
    build_dir: Path
    dist_dir: Path
    swarm_dir: Path

    @classmethod
    def load(cls, root: str | Path, *, runtime_root: str | Path | None = None) -> RepoPaths:
        base = Path(root).resolve()
        runtime = Path(runtime_root).resolve() if runtime_root is not None else base
        return cls(
            root=base,
            config_dir=base / "config",
            state_dir=runtime / "state",
            build_dir=runtime / "build",
            dist_dir=runtime / "dist",
            swarm_dir=runtime / "swarm" / "children",
        )

    @property
    def system_config_file(self) -> Path:
        return self.config_dir / "system.yaml"

    @property
    def invariants_file(self) -> Path:
        return self.config_dir / "invariants.yaml"

    @property
    def ledger_file(self) -> Path:
        return self.state_dir / "ledger.jsonl"

    @property
    def inbox_dir(self) -> Path:
        return self.state_dir / "inbox"

    @property
    def proposals_dir(self) -> Path:
        return self.state_dir / "proposals"

    @property
    def releases_dir(self) -> Path:
        return self.state_dir / "releases"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    def state_file(self, name: str) -> Path:
        return self.state_dir / f"{name}.json"

    def state_jsonl(self, name: str) -> Path:
        return self.state_dir / f"{name}.jsonl"

    def ensure_runtime_dirs(self) -> None:
        for directory in (
            self.state_dir,
            self.inbox_dir,
            self.proposals_dir,
            self.releases_dir,
            self.cache_dir,
            self.build_dir,
            self.dist_dir,
            self.swarm_dir,
        ):
            ensure_dir(directory)

    def display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(path)
