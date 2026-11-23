"""Minimal GitHub CLI wrapper that never logs credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.errors import ComplianceError, StateError

_TOKEN_ENV = "FINE_GRAINED_GH_TOKEN"  # noqa: S105 - env var name, not a secret


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


class GitHubClient:
    def __init__(
        self,
        *,
        dry_run: bool = True,
        environ: Mapping[str, str] | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._dry_run = dry_run
        self._environ = environ if environ is not None else os.environ
        self._timeout = timeout_seconds

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def available(self) -> bool:
        return shutil.which("gh") is not None

    def create_public_repo(
        self, name: str, *, description: str, owner: str | None = None
    ) -> RepoRef:
        self._validate_slug(name, "repository name")
        target = f"{owner}/{name}" if owner else name
        if self._dry_run:
            return RepoRef(owner=owner or "dry-run-owner", name=name)
        token = self._token()
        self._run(
            [
                "repo",
                "create",
                target,
                "--public",
                "--description",
                description,
                "--disable-wiki",
            ],
            token=token,
        )
        resolved_owner = owner
        if not resolved_owner:
            resolved_owner = self._run(
                ["api", "user", "--jq", ".login"], token=token
            ).strip()
        self._validate_slug(resolved_owner, "repository owner")
        return RepoRef(owner=resolved_owner, name=name)

    def create_release(
        self,
        repo: RepoRef,
        *,
        tag: str,
        title: str,
        notes: str,
        assets: Sequence[Path] = (),
    ) -> str:
        self._validate_slug(tag, "release tag")
        for asset in assets:
            if not asset.is_file():
                raise StateError(f"release asset is missing: {asset}")
        if self._dry_run:
            return f"https://github.com/{repo.full_name}/releases/tag/{tag}"
        token = self._token()
        command = [
            "release",
            "create",
            tag,
            "--repo",
            repo.full_name,
            "--title",
            title,
            "--notes",
            notes,
        ]
        command.extend(str(asset) for asset in assets)
        self._run(command, token=token)
        return f"https://github.com/{repo.full_name}/releases/tag/{tag}"

    def _token(self) -> str:
        token = self._environ.get(_TOKEN_ENV, "").strip()
        if not token:
            raise ComplianceError(f"{_TOKEN_ENV} is not set; refusing an authenticated call")
        return token

    def _run(self, args: list[str], *, token: str) -> str:
        if not self.available():
            raise StateError("the gh CLI is not installed")
        env = {
            "PATH": self._environ.get("PATH", ""),
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
        }
        try:
            completed = subprocess.run(
                ["gh", *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env=env,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StateError(f"gh command failed: {type(exc).__name__}") from exc
        if completed.returncode != 0:
            raise StateError(f"gh command failed with exit code {completed.returncode}")
        return completed.stdout

    @staticmethod
    def _validate_slug(value: str, label: str) -> None:
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")
        if not value or len(value) > 100 or any(char not in allowed for char in value):
            raise StateError(f"invalid {label}: {value!r}")
