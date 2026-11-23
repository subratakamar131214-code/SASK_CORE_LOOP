"""Deterministic, paper-only outcome simulation for channel and research options.

The simulator ranks monetization channels against paper/backtest strategies using
modeled value units, never USD revenue. It refuses any live strategy, forbids
``OWNER_VAULT`` in every path, and reports ``real_funds_at_risk_usd`` as zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.core.config import (
    CapitalResearchConfig,
    MonetizationChannelDef,
    ResearchStrategyConfig,
)
from src.core.errors import ConfigError, InvariantViolation
from src.core.redact import sanitize_public_payload
from src.core.timeutil import iso_now
from src.core.types import JsonObject, JsonValue
from src.sensors.channel_sensors import (
    DEFAULT_CHANNEL_WEIGHTS,
    ChannelOpportunity,
    ChannelWeights,
)

_HORIZON_CYCLES = 4.0
_RESEARCH_RISK_FACTOR = 0.25
_DISCLAIMER = (
    "Modeled units are deterministic planning scores, not revenue forecasts or "
    "financial advice. No live order execution, automated payout, or OWNER_VAULT "
    "access is permitted."
)


@dataclass(frozen=True)
class SimulatedOption:
    option_id: str
    label: str
    kind: str
    channel_kind: str | None
    niche_id: str | None
    expected_net_units: float
    probability_adjusted_units: float
    time_to_value_days: float
    risk_penalty_units: float
    recurring: bool
    live_execution: bool
    rationale: str

    def to_json(self) -> JsonObject:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "kind": self.kind,
            "channel_kind": self.channel_kind,
            "niche_id": self.niche_id,
            "expected_net_units": round(self.expected_net_units, 6),
            "probability_adjusted_units": round(
                self.probability_adjusted_units, 6
            ),
            "time_to_value_days": round(self.time_to_value_days, 3),
            "risk_penalty_units": round(self.risk_penalty_units, 6),
            "recurring": self.recurring,
            "live_execution": False,
            "owner_vault_touched": False,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class OutcomePlan:
    generated_at: str
    options: tuple[SimulatedOption, ...]
    recommended_next_action: SimulatedOption | None
    weights: ChannelWeights
    policy: dict[str, JsonValue]

    def to_json(self) -> JsonObject:
        payload: JsonObject = {
            "generated_at": self.generated_at,
            "recommended_next_action": (
                self.recommended_next_action.to_json()
                if self.recommended_next_action is not None
                else None
            ),
            "options": [option.to_json() for option in self.options],
            "weights": self.weights.to_json(),
            "policy": dict(self.policy),
        }
        sanitized, _ = sanitize_public_payload(payload)
        if not isinstance(sanitized, dict):
            return {}
        return sanitized


class OutcomeSimulator:
    def __init__(
        self,
        research: CapitalResearchConfig,
        channels: Sequence[MonetizationChannelDef],
        *,
        weights: ChannelWeights = DEFAULT_CHANNEL_WEIGHTS,
        horizon_cycles: float = _HORIZON_CYCLES,
    ) -> None:
        if research.live_trading_enabled or research.live_investing_enabled:
            raise InvariantViolation("live trading and investing must remain disabled")
        if not research.forbid_owner_vault:
            raise InvariantViolation("OWNER_VAULT must remain excluded from research")
        for strategy in research.strategies:
            if strategy.live:
                raise InvariantViolation(
                    f"research strategy {strategy.id} cannot execute live orders"
                )
            if strategy.uses_owner_vault:
                raise InvariantViolation(
                    f"research strategy {strategy.id} cannot use OWNER_VAULT"
                )
        if abs(weights.total() - 1.0) > 1e-6:
            raise ConfigError("outcome weights must sum to 1.0")
        if horizon_cycles < 1.0:
            raise ConfigError("horizon_cycles must be at least 1")
        self._research = research
        self._channels: Mapping[str, MonetizationChannelDef] = {
            channel.kind: channel for channel in channels
        }
        self._weights = weights
        self._horizon_cycles = horizon_cycles

    def simulate(
        self,
        opportunities: Sequence[ChannelOpportunity],
        *,
        generated_at: str | None = None,
    ) -> OutcomePlan:
        options: list[SimulatedOption] = []
        for opportunity in opportunities:
            options.append(self._channel_option(opportunity))
        for strategy in self._research.strategies:
            options.append(self._research_option(strategy))
        ranked = tuple(
            sorted(options, key=lambda option: (-option.expected_net_units, option.option_id))
        )
        policy: dict[str, JsonValue] = {
            "research_only": True,
            "live_trading_enabled": False,
            "live_investing_enabled": False,
            "real_funds_at_risk_usd": 0.0,
            "owner_vault_touched": False,
            "automated_payouts": False,
            "currency": "SIMULATED_UNITS",
            "horizon_cycles": self._horizon_cycles,
            "paper_capital_units": self._research.paper_capital_units,
            "disclaimer": _DISCLAIMER,
        }
        return OutcomePlan(
            generated_at=generated_at or iso_now(),
            options=ranked,
            recommended_next_action=ranked[0] if ranked else None,
            weights=self._weights,
            policy=policy,
        )

    def _channel_option(
        self, opportunity: ChannelOpportunity
    ) -> SimulatedOption:
        channel = self._channels.get(opportunity.kind)
        if channel is None:
            raise ConfigError(
                f"opportunity {opportunity.opportunity_id} has no configured channel"
            )
        probability_units = (
            channel.modeled_value_units * channel.conversion_probability
        )
        probability_adjusted = probability_units * (
            0.5 + 0.5 * opportunity.composite_score
        )
        maintenance = channel.maintenance_units_per_cycle * self._horizon_cycles
        risk = channel.modeled_value_units * (1.0 - channel.compliance_fit) * 0.10
        net = (
            probability_adjusted
            - channel.build_cost_units
            - maintenance
            - risk
        )
        rationale = (
            f"{channel.label}: probability-adjusted {probability_adjusted:.3f} - "
            f"build {channel.build_cost_units:.3f} - maintenance {maintenance:.3f} - "
            f"compliance risk {risk:.3f}; composite {opportunity.composite_score:.3f}."
        )
        return SimulatedOption(
            option_id=f"opt-{opportunity.opportunity_id}",
            label=f"{channel.label} for {opportunity.niche_id}",
            kind="monetization_channel",
            channel_kind=opportunity.kind,
            niche_id=opportunity.niche_id,
            expected_net_units=net,
            probability_adjusted_units=probability_adjusted,
            time_to_value_days=channel.time_to_first_dollar_days,
            risk_penalty_units=risk,
            recurring=channel.recurring,
            live_execution=False,
            rationale=rationale,
        )

    def _research_option(self, strategy: ResearchStrategyConfig) -> SimulatedOption:
        capital = self._research.paper_capital_units
        gross = capital * strategy.expected_return_fraction
        fees = capital * strategy.fee_fraction
        risk = capital * strategy.max_drawdown_fraction * _RESEARCH_RISK_FACTOR
        net = gross - fees - risk
        rationale = (
            f"{strategy.kind} paper model on {capital:.2f} simulated units: "
            f"return {gross:.3f} - fees {fees:.3f} - drawdown risk {risk:.3f}. "
            "No live orders."
        )
        return SimulatedOption(
            option_id=f"research-{strategy.id}",
            label=f"Paper strategy: {strategy.id}",
            kind="paper_research",
            channel_kind=None,
            niche_id=None,
            expected_net_units=net,
            probability_adjusted_units=gross - fees,
            time_to_value_days=0.0,
            risk_penalty_units=risk,
            recurring=strategy.kind in {"paper_simulation", "capital_allocation_model"},
            live_execution=False,
            rationale=rationale,
        )
