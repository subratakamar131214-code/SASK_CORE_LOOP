"""Fail-closed verification gate for evolution proposals."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.compliance.secretscan import scan_text
from src.core.config import EvolutionConfig
from src.core.invariants import InvariantGuard, normalize_repo_path
from src.core.redact import redact_text
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject
from src.evolution.proposer import Proposal

_FORBIDDEN_PREFIXES = ("state/", "build/", "dist/", "swarm/", ".git/", "secrets/")


@dataclass(frozen=True)
class CommandOutcome:
    command: tuple[str, ...]
    returncode: int
    output: str
    timed_out: bool = False

    def to_json(self) -> JsonObject:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "output_tail": self.output[-2000:],
        }


@dataclass(frozen=True)
class GateResult:
    proposal_id: str
    passed: bool
    reasons: tuple[str, ...]
    commands: tuple[CommandOutcome, ...]
    checked_at: str

    def to_json(self) -> JsonObject:
        return {
            "proposal_id": self.proposal_id,
            "passed": self.passed,
            "reasons": list(self.reasons),
            "commands": [command.to_json() for command in self.commands],
            "checked_at": self.checked_at,
        }


class ProposalGate:
    def __init__(
        self,
        config: EvolutionConfig,
        guard: InvariantGuard,
        *,
        timeout_seconds: float = 300.0,
        runner: Callable[[tuple[str, ...], Path], CommandOutcome] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._guard = guard
        self._timeout = timeout_seconds
        self._runner = runner
        self._clock = clock

    def evaluate(
        self,
        proposal: Proposal,
        *,
        root: Path,
        run_commands: bool = False,
    ) -> GateResult:
        reasons: list[str] = []
        if self._config.auto_apply:
            reasons.append("auto_apply must remain false")
        if not self._config.require_human_review:
            reasons.append("human review must remain required")
        if proposal.status not in {"proposed", "verified"}:
            reasons.append(f"proposal status {proposal.status!r} is not reviewable")
        if not proposal.title.strip() or not proposal.rationale.strip():
            reasons.append("proposal title and rationale are required")
        if not proposal.target_paths or not proposal.changes:
            reasons.append("proposal must include at least one change")
        if len(set(proposal.target_paths)) != len(proposal.target_paths):
            reasons.append("proposal target paths must be unique")
        change_paths = {change.path for change in proposal.changes}
        missing = change_paths - set(proposal.target_paths)
        if missing:
            reasons.append(f"target_paths is missing changes: {sorted(missing)}")
        for path in proposal.target_paths:
            normalized = normalize_repo_path(path, root)
            if normalized is None:
                reasons.append(f"unsafe target path: {path!r}")
                continue
            if self._guard.is_protected_path(normalized):
                reasons.append(f"target path is protected: {normalized}")
            if normalized.startswith(_FORBIDDEN_PREFIXES):
                reasons.append(f"target path is not reviewable: {normalized}")
        scanned = "\n".join(
            [proposal.title, proposal.rationale, *(change.diff for change in proposal.changes)]
        )
        findings = scan_text(scanned, path=f"proposal/{proposal.proposal_id}")
        if findings:
            kinds = ", ".join(sorted({finding.kind for finding in findings}))
            reasons.append(f"proposal contains secret-like content: {kinds}")

        commands: list[CommandOutcome] = []
        if run_commands and not reasons:
            runner = self._runner or self._run_command
            for command in self._config.verification_commands:
                outcome = runner(command, root)
                commands.append(outcome)
                if outcome.returncode != 0:
                    label = " ".join(command)
                    reasons.append(f"verification command failed ({outcome.returncode}): {label}")
                    break
        return GateResult(
            proposal_id=proposal.proposal_id,
            passed=not reasons,
            reasons=tuple(reasons),
            commands=tuple(commands),
            checked_at=iso_now(self._clock()),
        )

    def _run_command(self, command: tuple[str, ...], root: Path) -> CommandOutcome:
        resolved = list(command)
        if not resolved:
            return CommandOutcome(command=command, returncode=127, output="empty command")
        executable = resolved[0]
        if executable == "python":
            resolved[0] = sys.executable
        else:
            located = shutil.which(executable)
            if located is None:
                return CommandOutcome(
                    command=command, returncode=127, output=f"executable not found: {executable}"
                )
            resolved[0] = located
        try:
            completed = subprocess.run(  # noqa: S603
                resolved,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandOutcome(
                command=command,
                returncode=124,
                output=f"timed out after {self._timeout:g}s",
                timed_out=True,
            )
        except OSError as exc:
            return CommandOutcome(
                command=command,
                returncode=127,
                output=f"cannot execute command: {type(exc).__name__}",
            )
        output = (completed.stdout or "") + (completed.stderr or "")
        return CommandOutcome(
            command=command,
            returncode=completed.returncode,
            output=redact_text(output[-4000:]).text,
        )
