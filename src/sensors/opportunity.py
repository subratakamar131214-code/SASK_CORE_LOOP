"""Opportunity discovery from seed niches and sanitized feedback.

No scraping or unsolicited outreach happens here: signals are derived only from
declared seed niches and feedback that was already ingested and redacted.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from src.core.config import NicheConfig
from src.core.io import sha256_hex
from src.core.timeutil import iso_now, utc_now
from src.core.types import JsonObject
from src.sensors.feedback import FeedbackItem
from src.sensors.niche_scoring import NicheScore

_URGENT_PRESSURE = 0.6
_FOCUS_TOTAL = 0.6


@dataclass(frozen=True)
class OpportunitySignal:
    id: str
    niche_id: str
    kind: str
    title: str
    rationale: str
    strength: float
    evidence_ids: tuple[str, ...]
    created_at: str
    workflow_bottleneck: str = ""
    budget_owner: str = ""
    reliability_rationale: str = ""

    def to_json(self) -> JsonObject:
        return {
            "id": self.id,
            "niche_id": self.niche_id,
            "kind": self.kind,
            "title": self.title,
            "rationale": self.rationale,
            "strength": round(self.strength, 4),
            "evidence_ids": list(self.evidence_ids),
            "created_at": self.created_at,
            "workflow_bottleneck": self.workflow_bottleneck,
            "budget_owner": self.budget_owner,
            "reliability_rationale": self.reliability_rationale,
        }


class OpportunityScanner:
    def __init__(
        self,
        niches: Sequence[NicheConfig],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._niches = {niche.id: niche for niche in niches}
        self._clock = clock

    def scan(
        self,
        scores: Sequence[NicheScore],
        feedback: Sequence[FeedbackItem] = (),
    ) -> list[OpportunitySignal]:
        ranked = sorted(scores, key=lambda score: (-score.total, score.niche_id))
        ranked_ids = [score.niche_id for score in ranked]
        signals: list[OpportunitySignal] = []
        for score in ranked:
            niche = self._niches.get(score.niche_id)
            if niche is None or not niche.active:
                continue
            matched = [item for item in feedback if niche.id in item.matched_niches]
            kind, strength, rationale = self._classify(
                niche, score, matched, ranked_ids
            )
            evidence = tuple(item.id for item in matched[:10])
            signal_id = sha256_hex(f"{niche.id}|{kind}|{'|'.join(evidence)}")[:12]
            signals.append(
                OpportunitySignal(
                    id=f"opp-{signal_id}",
                    niche_id=niche.id,
                    kind=kind,
                    title=self._title(niche, kind, len(matched)),
                    rationale=rationale,
                    strength=strength,
                    evidence_ids=evidence,
                    created_at=iso_now(self._clock()),
                    workflow_bottleneck=niche.workflow_bottleneck,
                    budget_owner=niche.budget_owner,
                    reliability_rationale=niche.reliability_rationale,
                )
            )
        return sorted(signals, key=lambda signal: (-signal.strength, signal.niche_id))

    def _classify(
        self,
        niche: NicheConfig,
        score: NicheScore,
        feedback: Sequence[FeedbackItem],
        ranked_ids: Sequence[str],
    ) -> tuple[str, float, str]:
        context = (
            f"Bottleneck: {niche.workflow_bottleneck} "
            f"Budget owner: {niche.budget_owner}. "
            f"Recurring reliability matters: {niche.reliability_rationale}"
        )
        if score.feedback_pressure >= _URGENT_PRESSURE:
            return (
                "urgent_feedback",
                min(1.0, 0.6 + score.feedback_pressure * 0.4),
                f"{len(feedback)} high-priority feedback items demand a fast fix for "
                f"{niche.title}. {context}",
            )
        if feedback:
            return (
                "feedback_intake",
                min(1.0, 0.3 + score.feedback_pressure),
                f"{len(feedback)} sanitized feedback items inform the next {niche.title} "
                f"increment. {context}",
            )
        if ranked_ids and niche.id == ranked_ids[0] and score.total >= _FOCUS_TOTAL:
            return (
                "focus_validation",
                score.total * 0.75,
                f"{niche.title} is the strongest seed niche; validate willingness to pay "
                f"with a zero-cost release. {context}",
            )
        return (
            "seed_validation",
            score.total * 0.5,
            f"Publish a deterministic {niche.title} artifact and watch for real feedback. "
            f"{context}",
        )

    def _title(self, niche: NicheConfig, kind: str, feedback_count: int) -> str:
        if kind == "urgent_feedback":
            return f"Urgent: fix {niche.product_name} ({feedback_count} feedback signals)"
        if kind == "feedback_intake":
            return f"Prioritize {niche.product_name} feedback ({feedback_count} signals)"
        if kind == "focus_validation":
            return f"Validate demand for {niche.product_name}"
        return f"Probe {niche.product_name} with a zero-cost artifact"
