"""Deterministic, human-reviewed upgrade proposals (never auto-applied)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.config import EvolutionConfig, NicheConfig
from src.core.errors import StateError
from src.core.io import ensure_dir, sha256_hex, write_json_atomic
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.evolution.analyzer import EvolutionObservation
from src.sensors.feedback import FeedbackItem
from src.sensors.niche_scoring import NicheScore

_PROPOSAL_ID = re.compile(r"^prop-[0-9a-f]{12}$")
_RISK = {"low", "medium", "high"}


@dataclass(frozen=True)
class ProposalChange:
    path: str
    description: str
    diff: str

    def to_json(self) -> JsonObject:
        return {
            "path": self.path,
            "description": self.description,
            "diff": self.diff,
        }

    @classmethod
    def from_json(cls, value: JsonValue) -> ProposalChange:
        if not isinstance(value, dict):
            raise StateError("proposal change is not an object")
        path = value.get("path")
        description = value.get("description")
        diff = value.get("diff")
        if (
            not isinstance(path, str)
            or not isinstance(description, str)
            or not isinstance(diff, str)
        ):
            raise StateError("proposal change fields are incomplete")
        return cls(path=path, description=description, diff=diff)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    title: str
    rationale: str
    risk: str
    target_paths: tuple[str, ...]
    changes: tuple[ProposalChange, ...]
    observation_codes: tuple[str, ...]
    suggested_verification: tuple[str, ...]
    status: str
    created_at: str

    def to_json(self) -> JsonObject:
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "rationale": self.rationale,
            "risk": self.risk,
            "target_paths": list(self.target_paths),
            "changes": [change.to_json() for change in self.changes],
            "observation_codes": list(self.observation_codes),
            "suggested_verification": list(self.suggested_verification),
            "status": self.status,
            "created_at": self.created_at,
            "auto_apply": False,
        }

    @classmethod
    def from_json(cls, value: JsonValue) -> Proposal:
        if not isinstance(value, dict):
            raise StateError("proposal is not an object")
        proposal_id = value.get("proposal_id")
        title = value.get("title")
        rationale = value.get("rationale")
        risk = value.get("risk")
        status = value.get("status")
        created_at = value.get("created_at")
        target_paths = value.get("target_paths")
        changes = value.get("changes")
        codes = value.get("observation_codes")
        verification = value.get("suggested_verification")
        if (
            not isinstance(proposal_id, str)
            or not isinstance(title, str)
            or not isinstance(rationale, str)
            or not isinstance(risk, str)
            or not isinstance(status, str)
            or not isinstance(created_at, str)
            or not isinstance(target_paths, list)
            or not isinstance(changes, list)
            or not isinstance(codes, list)
            or not isinstance(verification, list)
        ):
            raise StateError("proposal fields are incomplete")
        if not _PROPOSAL_ID.fullmatch(proposal_id):
            raise StateError("proposal id is invalid")
        if risk not in _RISK:
            raise StateError("proposal risk is invalid")
        return cls(
            proposal_id=proposal_id,
            title=title,
            rationale=rationale,
            risk=risk,
            target_paths=tuple(str(item) for item in target_paths),
            changes=tuple(ProposalChange.from_json(item) for item in changes),
            observation_codes=tuple(str(item) for item in codes),
            suggested_verification=tuple(str(item) for item in verification),
            status=status,
            created_at=created_at,
        )


class EvolutionProposer:
    def __init__(
        self,
        config: EvolutionConfig,
        niches: Sequence[NicheConfig],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._config = config
        self._niches = {niche.id: niche for niche in niches}
        self._clock = clock

    def propose(
        self,
        observations: Sequence[EvolutionObservation],
        feedback: Sequence[FeedbackItem] = (),
        scores: Sequence[NicheScore] = (),
    ) -> list[Proposal]:
        proposals: list[Proposal] = []
        seen: set[str] = set()
        for observation in observations:
            if observation.code in seen:
                continue
            builder = self._TEMPLATES.get(observation.code)
            if builder is None:
                continue
            proposal = builder(self, observation, feedback, scores)
            seen.add(observation.code)
            proposals.append(proposal)
            if len(proposals) >= self._config.max_proposals_per_cycle:
                break
        proposals.sort(key=lambda item: item.proposal_id)
        return proposals

    def save(self, proposals: Sequence[Proposal], proposals_dir: Path) -> list[Path]:
        ensure_dir(proposals_dir)
        written: list[Path] = []
        for proposal in proposals:
            path = proposals_dir / f"{proposal.proposal_id}.json"
            sanitized, _ = sanitize_public_payload(proposal.to_json())
            if not isinstance(sanitized, dict):
                raise StateError("proposal must remain an object after sanitization")
            write_json_atomic(path, sanitized)
            written.append(path)
        return written

    def _build(
        self,
        observation: EvolutionObservation,
        *,
        title: str,
        rationale: str,
        risk: str,
        changes: tuple[ProposalChange, ...],
        codes: tuple[str, ...],
    ) -> Proposal:
        digest = sha256_hex(f"{title}|{observation.code}|{observation.summary}")[:12]
        return Proposal(
            proposal_id=f"prop-{digest}",
            title=title,
            rationale=rationale,
            risk=risk,
            target_paths=tuple(change.path for change in changes),
            changes=changes,
            observation_codes=codes,
            suggested_verification=tuple(
                " ".join(command) for command in self._config.verification_commands
            ),
            status="proposed",
            created_at=iso_now(self._clock()),
        )

    def _feedback_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        top = scores[0] if scores else None
        niche_id = top.niche_id if top else "unmatched"
        evidence_ids = ", ".join(observation.evidence[:5]) or "none"
        change = ProposalChange(
            path="tests/test_feedback_regressions.py",
            description=(
                f"Add a regression test for niche {niche_id} covering sanitized feedback "
                f"signals {evidence_ids}."
            ),
            diff=(
                "New test module: build a synthetic log or document containing the reported "
                "failure signature, assert the deterministic classifier/report returns the "
                "expected category, and assert no secret or PII markers survive sanitization."
            ),
        )
        return self._build(
            observation,
            title=f"Add feedback regression coverage for {niche_id}",
            rationale=(
                f"{len(feedback)} sanitized feedback items are available and pressure is high. "
                "A regression test converts feedback into a non-regressing fix."
            ),
            risk="low",
            changes=(change,),
            codes=("feedback-pressure",),
        )

    def _keyword_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del scores
        change = ProposalChange(
            path="config/system.yaml",
            description="Review niche keywords against sanitized, unmatched feedback.",
            diff=(
                "Add only generic product keywords (no personal data) to the active niche "
                "keyword lists, then run the scoring tests to confirm the top niche is stable."
            ),
        )
        return self._build(
            observation,
            title="Broaden niche keyword coverage",
            rationale=(
                f"{len(feedback)} feedback items exist without a niche match; routing them "
                "improves the feedback signal that drives allocation."
            ),
            risk="medium",
            changes=(change,),
            codes=("unrouted-feedback", "keyword-coverage"),
        )

    def _validation_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del feedback, scores
        change = ProposalChange(
            path="docs/validation-plan.md",
            description="Document a zero-cost demand validation checklist.",
            diff=(
                "Define: target user, pain hypothesis, free artifact, install command, one "
                "feedback question, and a stop rule if no signal arrives after two releases."
            ),
        )
        return self._build(
            observation,
            title="Publish a zero-cost validation plan",
            rationale=(
                "Without live feedback, a written validation plan prevents speculative builds."
            ),
            risk="low",
            changes=(change,),
            codes=("no-feedback",),
        )

    def _conversion_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del feedback, scores
        change = ProposalChange(
            path="src/distribution/landing.py",
            description="Clarify the landing-page call to action without adding trackers.",
            diff=(
                "Add a single, explicit next step (download the archive, then send sanitized "
                "feedback). Keep the page static, CSP-locked, and free of analytics."
            ),
        )
        return self._build(
            observation,
            title="Clarify the landing-page call to action",
            rationale="A conversion proxy of zero suggests visitors lack a clear next step.",
            risk="low",
            changes=(change,),
            codes=("zero-conversion",),
        )

    def _heartbeat_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del feedback, scores
        change = ProposalChange(
            path="docs/runbooks/heartbeat.md",
            description="Document the human repair path for stale workflows.",
            diff=(
                "List each required workflow, its secret requirements, and the manual "
                "dispatch command. Workflow files are protected and require human review."
            ),
        )
        return self._build(
            observation,
            title="Document heartbeat recovery steps",
            rationale="Stale heartbeats need a human runbook; workflow files stay protected.",
            risk="low",
            changes=(change,),
            codes=("stale-heartbeat",),
        )

    def _ledger_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del feedback, scores
        change = ProposalChange(
            path="docs/incidents/ledger-recovery.md",
            description="Document forensic steps for an invalid ledger chain.",
            diff=(
                "Do not rewrite the ledger automatically. Preserve a copy, identify the first "
                "broken entry, restore from a reviewed commit, and require human approval."
            ),
        )
        return self._build(
            observation,
            title="Document ledger recovery procedure",
            rationale="Chain-invalid or non-zero-cost incidents require human forensics.",
            risk="medium",
            changes=(change,),
            codes=("chain-invalid", "nonzero-cost"),
        )

    def _maintenance_proposal(
        self,
        observation: EvolutionObservation,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
    ) -> Proposal:
        del feedback, scores
        change = ProposalChange(
            path="docs/maintenance.md",
            description="Document maintenance cadence for confirmed revenue.",
            diff=(
                "Track incident response, dependency updates, release verification, and "
                "human-confirmed revenue entries; never automate payouts."
            ),
        )
        return self._build(
            observation,
            title="Add a maintenance runbook",
            rationale="Human-confirmed revenue shifts effort toward reliability.",
            risk="low",
            changes=(change,),
            codes=("real-revenue",),
        )

    _TEMPLATES = {
        "feedback-pressure": _feedback_proposal,
        "unrouted-feedback": _keyword_proposal,
        "keyword-coverage": _keyword_proposal,
        "no-feedback": _validation_proposal,
        "zero-conversion": _conversion_proposal,
        "stale-heartbeat": _heartbeat_proposal,
        "chain-invalid": _ledger_proposal,
        "nonzero-cost": _ledger_proposal,
        "real-revenue": _maintenance_proposal,
    }
