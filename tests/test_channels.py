"""Multi-channel earning paths, paper-only research, and channel handoffs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from src.core.config import SystemConfig, validate_system_config
from src.core.errors import ConfigError, InvariantViolation
from src.core.invariants import InvariantGuard
from src.handoff.channel_handoffs import (
    CHANNEL_PACKET_KINDS,
    PACKET_AUDIT,
    PACKET_BOUNTY,
    PACKET_MARKETPLACE,
    PACKET_RESEARCH,
    PACKET_SAAS,
    PACKET_TEMPLATE,
    ChannelHandoffBuilder,
    ChannelHandoffPacket,
    save_channel_handoffs,
)
from src.sensors.channel_sensors import (
    CHANNEL_KINDS,
    DEFAULT_CHANNEL_WEIGHTS,
    ChannelOpportunity,
    ChannelSensor,
    top_by_channel,
)
from src.sensors.outcome_simulator import OutcomePlan, OutcomeSimulator

from helpers import fake_github_token, make_feedback_item

_GENERATED_AT = "2026-11-20T00:00:00Z"


def _plan_and_packets(
    config: SystemConfig,
) -> tuple[list[ChannelOpportunity], OutcomePlan, tuple[ChannelHandoffPacket, ...]]:
    sensor = ChannelSensor(config.monetization_channels)
    niches = config.active_niches()
    opportunities = sensor.scan(niches)
    simulator = OutcomeSimulator(config.capital_research, config.monetization_channels)
    plan = simulator.simulate(opportunities, generated_at=_GENERATED_AT)
    packets = ChannelHandoffBuilder(config.handoff).build(
        repo_name=config.repo.name,
        plan=plan,
        opportunities=opportunities,
        niches=niches,
        generated_at=_GENERATED_AT,
    )
    return opportunities, plan, packets


def test_config_defines_exactly_five_channels(config: SystemConfig) -> None:
    kinds = [channel.kind for channel in config.monetization_channels]
    assert kinds == [
        "marketplace",
        "bounty",
        "paid_template",
        "custom_audit",
        "recurring_saas",
    ]
    assert len(set(kinds)) == len(kinds) == len(CHANNEL_KINDS)
    for channel in config.monetization_channels:
        assert channel.url.startswith("https://")
        assert channel.requires_human_setup is True

    by_kind = {channel.kind: channel for channel in config.monetization_channels}
    assert by_kind["recurring_saas"].recurring is True
    assert by_kind["bounty"].recurring is False
    assert by_kind["marketplace"].platform_billing is True
    assert by_kind["paid_template"].build_cost < by_kind["custom_audit"].build_cost
    assert (
        by_kind["recurring_saas"].time_to_first_dollar_days
        > by_kind["paid_template"].time_to_first_dollar_days
    )


def test_capital_research_defaults_are_paper_only(config: SystemConfig) -> None:
    research = config.capital_research
    assert research.research_only is True
    assert research.live_trading_enabled is False
    assert research.live_investing_enabled is False
    assert research.require_human_compliance_verification is True
    assert research.require_broker_verification is True
    assert research.max_system_treasury_loss_fraction == 0.0
    assert research.forbid_owner_vault is True
    assert research.paper_capital_units > 0.0
    assert {strategy.kind for strategy in research.strategies} == {
        "backtest",
        "paper_simulation",
        "capital_allocation_model",
    }
    assert all(strategy.live is False for strategy in research.strategies)
    assert all(strategy.uses_owner_vault is False for strategy in research.strategies)


def test_invariants_lock_live_capital_and_owner_vault(guard: InvariantGuard) -> None:
    invariants = guard.invariants
    assert invariants.automated_payouts_allowed is False
    assert invariants.capital_research_only is True
    assert invariants.live_trading_enabled is False
    assert invariants.live_investing_enabled is False
    assert invariants.require_research_compliance_verification is True
    assert invariants.require_broker_verification is True
    assert invariants.max_live_loss_usd == 0.0
    assert invariants.owner_vault_excluded_from_research is True
    assert set(invariants.allowed_channel_kinds) == set(CHANNEL_KINDS)
    assert {"broker_and_exchange_compliance", "live_trading_activation"} <= set(
        invariants.human_only_tasks
    )


def test_config_validation_rejects_live_capital(
    config: SystemConfig, guard: InvariantGuard
) -> None:
    research = config.capital_research
    live_trading = replace(
        config, capital_research=replace(research, live_trading_enabled=True)
    )
    with pytest.raises(InvariantViolation, match="live trading"):
        validate_system_config(live_trading, guard)

    live_investing = replace(
        config, capital_research=replace(research, live_investing_enabled=True)
    )
    with pytest.raises(InvariantViolation, match="live investing"):
        validate_system_config(live_investing, guard)

    not_research = replace(
        config, capital_research=replace(research, research_only=False)
    )
    with pytest.raises(InvariantViolation, match="research-only"):
        validate_system_config(not_research, guard)

    owner_vault = replace(
        config, capital_research=replace(research, forbid_owner_vault=False)
    )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        validate_system_config(owner_vault, guard)

    no_verification = replace(
        config, capital_research=replace(research, require_broker_verification=False)
    )
    with pytest.raises(InvariantViolation, match="compliance and broker"):
        validate_system_config(no_verification, guard)


def test_sensor_scores_all_five_kinds(config: SystemConfig) -> None:
    sensor = ChannelSensor(config.monetization_channels)
    niches = config.active_niches()
    opportunities = sensor.scan(niches)
    assert len(opportunities) == len(niches) * len(CHANNEL_KINDS)
    assert {opportunity.kind for opportunity in opportunities} == set(CHANNEL_KINDS)
    assert len({opportunity.opportunity_id for opportunity in opportunities}) == len(
        opportunities
    )

    scores = [opportunity.composite_score for opportunity in opportunities]
    assert scores == sorted(scores, reverse=True)

    weights = DEFAULT_CHANNEL_WEIGHTS
    for opportunity in opportunities:
        components = opportunity.components
        expected = (
            weights.recurring_upside * components["recurring_upside"]
            + weights.pain_and_urgency * components["pain_and_urgency"]
            + weights.time_to_first_dollar * components["time_to_first_dollar"]
            + weights.build_efficiency * components["build_efficiency"]
            + weights.maintenance_efficiency
            * components["maintenance_efficiency"]
            + weights.compliance_fit * components["compliance_fit"]
            + weights.platform_fit * components["platform_fit"]
        )
        assert opportunity.composite_score == pytest.approx(expected)
        payload = opportunity.to_json()
        assert payload["live_execution"] is False
        assert payload["automated_payouts"] is False

    assert set(top_by_channel(opportunities)) == set(CHANNEL_KINDS)


def test_signal_hints_route_to_channels(config: SystemConfig) -> None:
    sensor = ChannelSensor(config.monetization_channels)
    niche = config.active_niches()[0]
    baseline = {opportunity.kind: opportunity for opportunity in sensor.scan([niche])}
    feedback = [
        make_feedback_item(
            "sig-bounty",
            labels=("bounty",),
            matched_niches=(niche.id,),
            priority=30.0,
        ),
        make_feedback_item(
            "sig-recurring",
            labels=("recurring_wtp", "hosted"),
            matched_niches=(niche.id,),
            priority=30.0,
        ),
    ]
    routed = {
        opportunity.kind: opportunity
        for opportunity in sensor.scan([niche], feedback=feedback)
    }
    assert "bounty" in routed["bounty"].matched_hints
    assert (
        routed["bounty"].components["platform_fit"]
        > baseline["bounty"].components["platform_fit"]
    )
    assert routed["bounty"].demand_count == 2
    assert "hosted" in routed["recurring_saas"].matched_hints
    assert (
        routed["recurring_saas"].components["recurring_upside"]
        > baseline["recurring_saas"].components["recurring_upside"]
    )


def test_outcome_simulator_ranks_and_recommends(config: SystemConfig) -> None:
    opportunities, plan, _ = _plan_and_packets(config)
    assert len(plan.options) == len(opportunities) + len(
        config.capital_research.strategies
    )
    nets = [option.expected_net_units for option in plan.options]
    assert nets == sorted(nets, reverse=True)
    assert plan.recommended_next_action == plan.options[0]
    assert {
        option.channel_kind for option in plan.options if option.channel_kind is not None
    } == set(CHANNEL_KINDS)
    assert {
        option.option_id for option in plan.options if option.kind == "paper_research"
    } == {
        f"research-{strategy.id}" for strategy in config.capital_research.strategies
    }
    for option in plan.options:
        assert option.live_execution is False
        payload = option.to_json()
        assert payload["live_execution"] is False
        assert payload["owner_vault_touched"] is False

    policy = plan.policy
    assert policy["research_only"] is True
    assert policy["live_trading_enabled"] is False
    assert policy["live_investing_enabled"] is False
    assert policy["real_funds_at_risk_usd"] == 0.0
    assert policy["owner_vault_touched"] is False
    assert policy["automated_payouts"] is False
    assert policy["currency"] == "SIMULATED_UNITS"

    simulator = OutcomeSimulator(config.capital_research, config.monetization_channels)
    again = simulator.simulate(opportunities, generated_at=_GENERATED_AT)
    assert again.to_json() == plan.to_json()


def test_simulator_rejects_live_or_owner_vault_research(config: SystemConfig) -> None:
    research = config.capital_research
    channels = config.monetization_channels
    with pytest.raises(InvariantViolation, match="live trading and investing"):
        OutcomeSimulator(
            replace(research, live_trading_enabled=True), channels
        )
    with pytest.raises(InvariantViolation, match="live trading and investing"):
        OutcomeSimulator(
            replace(research, live_investing_enabled=True), channels
        )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        OutcomeSimulator(replace(research, forbid_owner_vault=False), channels)

    live_strategy = replace(research.strategies[0], live=True)
    with pytest.raises(InvariantViolation, match="live orders"):
        OutcomeSimulator(replace(research, strategies=(live_strategy,)), channels)

    vault_strategy = replace(research.strategies[0], uses_owner_vault=True)
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        OutcomeSimulator(replace(research, strategies=(vault_strategy,)), channels)


def test_channel_handoffs_cover_every_path(config: SystemConfig) -> None:
    _, _, packets = _plan_and_packets(config)
    assert tuple(packet.kind for packet in packets) == CHANNEL_PACKET_KINDS
    by_kind = {packet.kind: packet for packet in packets}
    for packet in packets:
        assert packet.approval_required is True
        assert packet.live_execution_allowed is False
        assert packet.owner_vault_touched is False
        assert packet.human_actions
        assert packet.checklist
        assert packet.body.strip()
        markdown = packet.to_markdown()
        assert "Human approval required" in markdown
        assert "OWNER_VAULT access are disabled" in markdown

    marketplace = by_kind[PACKET_MARKETPLACE]
    assert "listing" in marketplace.objective.lower()
    assert any("pricing" in action.lower() for action in marketplace.checklist)

    bounty = by_kind[PACKET_BOUNTY]
    assert any("acceptance criteria" in item for item in bounty.checklist)
    assert any("one-off" in item for item in bounty.checklist)

    template = by_kind[PACKET_TEMPLATE]
    assert any("<HUMAN_CHECKOUT_URL>" in item for item in template.human_actions)
    assert any("open-core" in item.lower() for item in template.checklist)

    audit = by_kind[PACKET_AUDIT]
    assert any("fixed fee" in item.lower() for item in audit.human_actions)
    assert any("discovery" in item.lower() for item in audit.checklist)

    saas = by_kind[PACKET_SAAS]
    assert any("OWNER_VAULT" in item for item in saas.checklist)
    assert any("ROI summary" in item for item in saas.checklist)

    research = by_kind[PACKET_RESEARCH]
    assert "research only" in research.body.lower()
    assert "live trading" in research.body.lower()
    assert any("OWNER_VAULT" in item for item in research.checklist)
    assert any("SYSTEM_TREASURY" in item for item in research.checklist)


def test_channel_packet_rejects_live_execution_or_owner_vault(
    config: SystemConfig,
) -> None:
    _, _, packets = _plan_and_packets(config)
    packet = packets[0]
    with pytest.raises(ConfigError, match="live execution"):
        replace(packet, live_execution_allowed=True)
    with pytest.raises(ConfigError, match="OWNER_VAULT"):
        replace(packet, owner_vault_touched=True)
    with pytest.raises(ConfigError, match="approval"):
        replace(packet, approval_required=False)


def test_save_channel_handoffs_dry_run_then_write(
    config: SystemConfig, tmp_path: Path
) -> None:
    _, _, packets = _plan_and_packets(config)
    target = tmp_path / "channels"
    assert save_channel_handoffs(packets, target, dry_run=True) == ()
    assert not target.exists()

    written = save_channel_handoffs(packets, target)
    assert len(written) == len(packets) + 1
    for packet in packets:
        assert (target / f"{packet.kind}.md").exists()
    index = json.loads((target / "index.json").read_text(encoding="utf-8"))
    assert index["approval_required"] is True
    assert index["live_execution_allowed"] is False
    assert index["owner_vault_touched"] is False
    assert len(index["packets"]) == len(packets)


def test_channel_outputs_redact_pii_and_secrets(config: SystemConfig) -> None:
    token = fake_github_token()
    opportunities, _, packets = _plan_and_packets(config)
    leaked_opportunity = replace(
        opportunities[0],
        rationale=f"contact ops@example.com; rotated token {token}",
    )
    serialized = json.dumps(leaked_opportunity.to_json())
    assert token not in serialized
    assert "ops@example.com" not in serialized
    assert "[REDACTED" in serialized

    leaked_packet = replace(
        packets[0],
        body=f"contact ops@example.com or {token}",
    )
    markdown = leaked_packet.to_markdown()
    assert token not in markdown
    assert "ops@example.com" not in markdown
    assert "[REDACTED" in markdown
