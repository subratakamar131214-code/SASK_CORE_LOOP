"""Deterministic niche scoring that treats live feedback as the strongest signal."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from src.core.config import NicheConfig, ScoringWeights
from src.core.errors import ConfigError
from src.core.types import JsonObject
from src.sensors.feedback import FeedbackItem

_FEEDBACK_SATURATION = 30.0
_COMPONENT_NAMES = (
    "feedback",
    "pain",
    "willingness_to_pay",
    "buildability",
    "competition_inverse",
    "compliance_safety",
    "urgency",
    "failure_cost",
    "recurrence",
    "recurring_willingness_to_pay",
)


@dataclass(frozen=True)
class NicheScore:
    niche_id: str
    total: float
    components: dict[str, float] = field(default_factory=dict)
    feedback_count: int = 0
    feedback_pressure: float = 0.0
    evidence_ids: tuple[str, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "niche_id": self.niche_id,
            "total": round(self.total, 4),
            "components": {
                name: round(self.components.get(name, 0.0), 4) for name in _COMPONENT_NAMES
            },
            "feedback_count": self.feedback_count,
            "feedback_pressure": round(self.feedback_pressure, 4),
            "evidence_ids": list(self.evidence_ids),
        }


def feedback_pressure(items: Sequence[FeedbackItem]) -> float:
    """Log-compressed sum of priority weights, bounded to [0, 1]."""
    total = sum(max(0.0, item.priority) for item in items)
    if total <= 0.0:
        return 0.0
    return min(1.0, math.log1p(total) / math.log1p(_FEEDBACK_SATURATION))


class NicheScorer:
    def __init__(self, weights: ScoringWeights) -> None:
        if abs(weights.total() - 1.0) > 1e-6:
            raise ConfigError("scoring weights must sum to 1.0")
        highest = max(
            weights.feedback,
            weights.pain,
            weights.willingness_to_pay,
            weights.buildability,
            weights.competition_inverse,
            weights.compliance_safety,
            weights.urgency,
            weights.failure_cost,
            weights.recurrence,
            weights.recurring_willingness_to_pay,
        )
        if weights.feedback < highest:
            raise ConfigError("feedback must carry the highest scoring weight")
        self._weights = weights

    def score(
        self, niche: NicheConfig, feedback: Sequence[FeedbackItem] = ()
    ) -> NicheScore:
        matched = [item for item in feedback if niche.id in item.matched_niches]
        pressure = feedback_pressure(matched)
        components = {
            "feedback": pressure,
            "pain": niche.pain,
            "willingness_to_pay": niche.willingness_to_pay,
            "buildability": niche.buildability,
            "competition_inverse": 1.0 - niche.competition,
            "compliance_safety": 1.0 - niche.compliance_risk,
            "urgency": niche.urgency,
            "failure_cost": niche.failure_cost,
            "recurrence": niche.recurrence,
            "recurring_willingness_to_pay": niche.recurring_willingness_to_pay,
        }
        total = sum(
            getattr(self._weights, name) * components[name] for name in _COMPONENT_NAMES
        )
        ordered_evidence = tuple(
            item.id for item in sorted(matched, key=lambda item: (-item.priority, item.id))
        )
        return NicheScore(
            niche_id=niche.id,
            total=total,
            components={name: round(value, 6) for name, value in components.items()},
            feedback_count=len(matched),
            feedback_pressure=pressure,
            evidence_ids=ordered_evidence,
        )

    def rank(
        self, niches: Sequence[NicheConfig], feedback: Sequence[FeedbackItem] = ()
    ) -> list[NicheScore]:
        scored = [self.score(niche, feedback) for niche in niches if niche.active]
        return sorted(scored, key=lambda score: (-score.total, score.niche_id))


def rank_niches(
    niches: Sequence[NicheConfig],
    feedback: Sequence[FeedbackItem],
    weights: ScoringWeights,
) -> list[NicheScore]:
    return NicheScorer(weights).rank(niches, feedback)
