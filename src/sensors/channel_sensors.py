"""Channel-aware earning-path detection with a transparent composite score.

Every active niche is evaluated against the five configured monetization kinds:
``marketplace``, ``bounty``, ``paid_template``, ``custom_audit``, and
``recurring_saas``. Scores combine recurring upside, urgency/pain, time to first
dollar, build and maintenance efficiency, compliance fit, and platform fit.
Outputs are sanitized and never contain live-execution or payout automation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.core.config import MonetizationChannelDef, NicheConfig
from src.core.errors import ConfigError
from src.core.redact import sanitize_public_payload
from src.core.types import JsonObject
from src.sensors.feedback import FeedbackItem
from src.sensors.niche_scoring import NicheScore, feedback_pressure

CHANNEL_KINDS = (
    "marketplace",
    "bounty",
    "paid_template",
    "custom_audit",
    "recurring_saas",
)

_KIND_LABEL_HINTS: Mapping[str, frozenset[str]] = {
    "marketplace": frozenset({"marketplace", "extension", "plugin", "integration", "app"}),
    "bounty": frozenset({"bounty", "grant", "reward", "funded", "sponsor"}),
    "paid_template": frozenset(
        {"template", "boilerplate", "starter", "scaffold", "schema", "kit"}
    ),
    "custom_audit": frozenset(
        {"audit", "review", "migration", "performance", "security"}
    ),
    "recurring_saas": frozenset(
        {"hosted", "saas", "subscription", "mrr", "api", "pro"}
    ),
}

_MAX_EVIDENCE = 5
_MAX_TIME_TO_FIRST_DOLLAR_DAYS = 90.0


@dataclass(frozen=True)
class ChannelWeights:
    recurring_upside: float
    pain_and_urgency: float
    time_to_first_dollar: float
    build_efficiency: float
    maintenance_efficiency: float
    compliance_fit: float
    platform_fit: float

    def total(self) -> float:
        return (
            self.recurring_upside
            + self.pain_and_urgency
            + self.time_to_first_dollar
            + self.build_efficiency
            + self.maintenance_efficiency
            + self.compliance_fit
            + self.platform_fit
        )

    def to_json(self) -> JsonObject:
        return {
            "recurring_upside": self.recurring_upside,
            "pain_and_urgency": self.pain_and_urgency,
            "time_to_first_dollar": self.time_to_first_dollar,
            "build_efficiency": self.build_efficiency,
            "maintenance_efficiency": self.maintenance_efficiency,
            "compliance_fit": self.compliance_fit,
            "platform_fit": self.platform_fit,
        }


DEFAULT_CHANNEL_WEIGHTS = ChannelWeights(
    recurring_upside=0.22,
    pain_and_urgency=0.24,
    time_to_first_dollar=0.14,
    build_efficiency=0.12,
    maintenance_efficiency=0.08,
    compliance_fit=0.10,
    platform_fit=0.10,
)


@dataclass(frozen=True)
class ChannelOpportunity:
    opportunity_id: str
    niche_id: str
    kind: str
    channel_id: str
    composite_score: float
    components: dict[str, float]
    evidence_ids: tuple[str, ...]
    matched_hints: tuple[str, ...]
    demand_count: int
    recurring: bool
    time_to_first_dollar_days: float
    rationale: str

    def to_json(self) -> JsonObject:
        payload: JsonObject = {
            "opportunity_id": self.opportunity_id,
            "niche_id": self.niche_id,
            "kind": self.kind,
            "channel_id": self.channel_id,
            "composite_score": round(self.composite_score, 6),
            "components": {
                name: round(value, 6) for name, value in self.components.items()
            },
            "evidence_ids": list(self.evidence_ids),
            "matched_hints": list(self.matched_hints),
            "demand_count": self.demand_count,
            "recurring": self.recurring,
            "time_to_first_dollar_days": round(self.time_to_first_dollar_days, 3),
            "rationale": self.rationale,
            "live_execution": False,
            "automated_payouts": False,
        }
        sanitized, _ = sanitize_public_payload(payload)
        if not isinstance(sanitized, dict):
            return {}
        return sanitized


class ChannelSensor:
    def __init__(
        self,
        channels: Sequence[MonetizationChannelDef],
        *,
        weights: ChannelWeights = DEFAULT_CHANNEL_WEIGHTS,
    ) -> None:
        kinds = {channel.kind for channel in channels}
        if kinds != set(CHANNEL_KINDS) or len(channels) != len(CHANNEL_KINDS):
            raise ConfigError(
                f"channel sensor requires each kind exactly once: {sorted(CHANNEL_KINDS)}"
            )
        if abs(weights.total() - 1.0) > 1e-6:
            raise ConfigError("channel weights must sum to 1.0")
        self._channels = tuple(channels)
        self._weights = weights

    @property
    def weights(self) -> ChannelWeights:
        return self._weights

    def scan(
        self,
        niches: Sequence[NicheConfig],
        scores: Sequence[NicheScore] = (),
        feedback: Sequence[FeedbackItem] = (),
    ) -> list[ChannelOpportunity]:
        score_by_niche = {score.niche_id: score for score in scores}
        opportunities: list[ChannelOpportunity] = []
        for niche in niches:
            if not niche.active:
                continue
            matched = tuple(
                item for item in feedback if niche.id in item.matched_niches
            )
            score = score_by_niche.get(niche.id)
            pressure = (
                score.feedback_pressure
                if score is not None
                else feedback_pressure(matched)
            )
            labels = {
                label.lower()
                for item in matched
                for label in item.labels
            }
            for channel in self._channels:
                opportunities.append(
                    self._score_channel(
                        niche, channel, matched, pressure, labels
                    )
                )
        return sorted(
            opportunities,
            key=lambda opportunity: (
                -opportunity.composite_score,
                opportunity.opportunity_id,
            ),
        )

    def _score_channel(
        self,
        niche: NicheConfig,
        channel: MonetizationChannelDef,
        matched: Sequence[FeedbackItem],
        pressure: float,
        labels: set[str],
    ) -> ChannelOpportunity:
        hints = tuple(sorted(_KIND_LABEL_HINTS[channel.kind] & labels))
        affinity = 1.0 if hints else 0.35
        recurring_signal = "recurring_wtp" in labels
        pain_signal = "pain_interview" in labels

        if channel.recurring:
            recurring = (
                0.55 * niche.recurring_willingness_to_pay
                + 0.30 * pressure
                + 0.15 * (1.0 if recurring_signal else 0.0)
            )
        else:
            recurring = (
                0.25 * niche.recurring_willingness_to_pay
                + 0.10 * (1.0 if recurring_signal else 0.0)
            )

        pain = (
            0.40 * niche.pain
            + 0.25 * niche.urgency
            + 0.15 * niche.failure_cost
            + 0.20 * pressure
        )
        if pain_signal:
            pain = min(1.0, pain + 0.15)

        time_to_first_dollar = max(
            0.0,
            1.0
            - min(
                1.0,
                channel.time_to_first_dollar_days / _MAX_TIME_TO_FIRST_DOLLAR_DAYS,
            ),
        )
        build_efficiency = (1.0 - channel.build_cost) * (
            0.5 + 0.5 * niche.buildability
        )
        maintenance_efficiency = 1.0 - channel.maintenance_cost
        compliance = channel.compliance_fit * (1.0 - niche.compliance_risk)
        platform = channel.platform_fit * (0.5 + 0.5 * affinity)

        components = {
            "recurring_upside": _bounded(recurring),
            "pain_and_urgency": _bounded(pain),
            "time_to_first_dollar": _bounded(time_to_first_dollar),
            "build_efficiency": _bounded(build_efficiency),
            "maintenance_efficiency": _bounded(maintenance_efficiency),
            "compliance_fit": _bounded(compliance),
            "platform_fit": _bounded(platform),
            "signal_affinity": _bounded(affinity),
        }
        composite = (
            self._weights.recurring_upside * components["recurring_upside"]
            + self._weights.pain_and_urgency * components["pain_and_urgency"]
            + self._weights.time_to_first_dollar
            * components["time_to_first_dollar"]
            + self._weights.build_efficiency * components["build_efficiency"]
            + self._weights.maintenance_efficiency
            * components["maintenance_efficiency"]
            + self._weights.compliance_fit * components["compliance_fit"]
            + self._weights.platform_fit * components["platform_fit"]
        )
        ordered_evidence = tuple(
            item.id
            for item in sorted(
                matched, key=lambda item: (-item.priority, item.id)
            )[:_MAX_EVIDENCE]
        )
        rationale = (
            f"{channel.label} fits {niche.id}: affinity={affinity:.2f}, "
            f"demand={len(matched)}, modeled_time_to_first_dollar="
            f"{channel.time_to_first_dollar_days:.0f}d, recurring={channel.recurring}. "
            "Simulated ranking only; no live orders or automatic payouts."
        )
        return ChannelOpportunity(
            opportunity_id=f"ch-{channel.kind}-{niche.id}",
            niche_id=niche.id,
            kind=channel.kind,
            channel_id=channel.id,
            composite_score=composite,
            components=components,
            evidence_ids=ordered_evidence,
            matched_hints=hints,
            demand_count=len(matched),
            recurring=channel.recurring,
            time_to_first_dollar_days=channel.time_to_first_dollar_days,
            rationale=rationale,
        )


def channel_opportunity_documents(
    opportunities: Sequence[ChannelOpportunity],
) -> list[JsonObject]:
    return [opportunity.to_json() for opportunity in opportunities]


def top_by_channel(
    opportunities: Sequence[ChannelOpportunity],
) -> dict[str, ChannelOpportunity]:
    best: dict[str, ChannelOpportunity] = {}
    for opportunity in opportunities:
        current = best.get(opportunity.kind)
        if current is None or opportunity.composite_score > current.composite_score:
            best[opportunity.kind] = opportunity
    return best


def _bounded(value: float) -> float:
    return max(0.0, min(1.0, value))
