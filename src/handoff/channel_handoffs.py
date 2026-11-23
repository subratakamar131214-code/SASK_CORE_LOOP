"""Channel-specific, human-reviewed earning-path handoff packets.

Each packet prepares listing, bounty, template, audit, hosted, or capital-research
readiness material for a human. Packets never publish, send, charge, trade, or
touch ``OWNER_VAULT``; every action remains human-owned and approval-gated.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.config import HandoffConfig, NicheConfig
from src.core.errors import ConfigError
from src.core.io import ensure_dir, write_json_atomic, write_text_atomic
from src.core.redact import redact_text, sanitize_public_payload
from src.core.timeutil import iso_now
from src.core.types import JsonObject
from src.sensors.channel_sensors import ChannelOpportunity, top_by_channel
from src.sensors.outcome_simulator import OutcomePlan, SimulatedOption

PACKET_MARKETPLACE = "channel_marketplace"
PACKET_BOUNTY = "channel_bounty"
PACKET_TEMPLATE = "channel_paid_template"
PACKET_AUDIT = "channel_custom_audit"
PACKET_SAAS = "channel_recurring_saas"
PACKET_RESEARCH = "capital_research_allocation"
CHANNEL_PACKET_KINDS = (
    PACKET_MARKETPLACE,
    PACKET_BOUNTY,
    PACKET_TEMPLATE,
    PACKET_AUDIT,
    PACKET_SAAS,
    PACKET_RESEARCH,
)
_SAFE_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


@dataclass(frozen=True)
class ChannelHandoffPacket:
    kind: str
    channel_kind: str | None
    niche_id: str | None
    title: str
    audience: str
    objective: str
    system_prepared: tuple[str, ...]
    human_actions: tuple[str, ...]
    checklist: tuple[str, ...]
    body: str
    approval_required: bool = True
    live_execution_allowed: bool = False
    owner_vault_touched: bool = False

    def __post_init__(self) -> None:
        if self.kind not in CHANNEL_PACKET_KINDS:
            raise ConfigError(f"unknown channel handoff kind: {self.kind!r}")
        if not self.human_actions:
            raise ConfigError(f"channel handoff {self.kind} requires human actions")
        if not self.checklist:
            raise ConfigError(f"channel handoff {self.kind} requires a checklist")
        if not self.body.strip():
            raise ConfigError(f"channel handoff {self.kind} requires a body")
        if self.live_execution_allowed:
            raise ConfigError("channel handoffs must never allow live execution")
        if self.owner_vault_touched:
            raise ConfigError("channel handoffs must never touch OWNER_VAULT")
        if not self.approval_required:
            raise ConfigError("channel handoffs require explicit human approval")

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "channel_kind": self.channel_kind,
            "niche_id": self.niche_id,
            "title": self.title,
            "audience": self.audience,
            "objective": self.objective,
            "system_prepared": list(self.system_prepared),
            "human_actions": list(self.human_actions),
            "checklist": list(self.checklist),
            "approval_required": True,
            "live_execution_allowed": False,
            "owner_vault_touched": False,
        }

    def to_markdown(self) -> str:
        prepared = "\n".join(f"- {item}" for item in self.system_prepared)
        human = "\n".join(f"- [ ] {item}" for item in self.human_actions)
        checklist = "\n".join(f"- [ ] {item}" for item in self.checklist)
        body = redact_text(self.body).text
        return (
            f"# {self.title}\n\n"
            "> **Human approval required.** Live execution, automated payouts, and "
            "OWNER_VAULT access are disabled by invariant.\n\n"
            f"**Audience:** {self.audience}\n\n"
            f"**Objective:** {self.objective}\n\n"
            f"## Prepared by the system\n\n{prepared}\n\n"
            f"## Human-owned actions\n\n{human}\n\n"
            f"## Checklist\n\n{checklist}\n\n"
            f"## Brief\n\n{body}\n"
        )


class ChannelHandoffBuilder:
    def __init__(self, config: HandoffConfig) -> None:
        self._config = config

    def build(
        self,
        *,
        repo_name: str,
        plan: OutcomePlan,
        opportunities: Sequence[ChannelOpportunity],
        niches: Sequence[NicheConfig],
        generated_at: str | None = None,
    ) -> tuple[ChannelHandoffPacket, ...]:
        stamp = generated_at or iso_now()
        niche_by_id = {niche.id: niche for niche in niches if niche.active}
        best = top_by_channel(opportunities)
        option_by_kind: dict[str, SimulatedOption] = {}
        for option in plan.options:
            if option.channel_kind is None:
                continue
            current = option_by_kind.get(option.channel_kind)
            if current is None or option.expected_net_units > current.expected_net_units:
                option_by_kind[option.channel_kind] = option

        builders = (
            ("marketplace", _marketplace_packet),
            ("bounty", _bounty_packet),
            ("paid_template", _template_packet),
            ("custom_audit", _audit_packet),
            ("recurring_saas", _saas_packet),
        )
        packets: list[ChannelHandoffPacket] = []
        for channel_kind, builder in builders:
            opportunity = best.get(channel_kind)
            if opportunity is None:
                continue
            niche = niche_by_id.get(opportunity.niche_id)
            if niche is None:
                continue
            packets.append(
                builder(
                    repo_name=repo_name,
                    niche=niche,
                    opportunity=opportunity,
                    option=option_by_kind.get(channel_kind),
                    stamp=stamp,
                )
            )
        packets.append(_research_packet(repo_name=repo_name, plan=plan, stamp=stamp))
        return tuple(packets)


def _marketplace_packet(
    *,
    repo_name: str,
    niche: NicheConfig,
    opportunity: ChannelOpportunity,
    option: SimulatedOption | None,
    stamp: str,
) -> ChannelHandoffPacket:
    return ChannelHandoffPacket(
        kind=PACKET_MARKETPLACE,
        channel_kind="marketplace",
        niche_id=niche.id,
        title=f"Marketplace listing: {niche.product_name}",
        audience="Human owner handling the marketplace account",
        objective="Publish a compliant listing for a low-friction, platform-billed product.",
        system_prepared=(
            "Sanitized product build, tests, and MIT license",
            "Deterministic package archive with SHA-256 manifest",
            "Channel composite score and modeled value units",
        ),
        human_actions=(
            "Create the listing as a draft in your own marketplace account",
            "Set the price, trial, tax, and payout settings",
            "Review and publish only after the checklist is complete",
            "Handle platform reviewer questions and support requests",
        ),
        checklist=(
            "Confirm marketplace policy allows this product and category",
            "Use a sanitized demo, screenshots, and changelog with no secrets or PII",
            "Choose a pricing model: free core plus paid extension, or one-time price",
            "Verify platform billing, fees, refund policy, and payout ownership",
            "Attach tests, quickstart docs, license, and package checksums",
            "Scan listing text and assets for secrets, PII, and unsupported claims",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. Marketplace channels have low "
            "build-to-publish friction and platform billing, but a human must own "
            "the account, pricing, and listing.\n\n"
            f"Focus: {niche.workflow_bottleneck}. Budget owner: {niche.budget_owner}. "
            f"Modeled channel units: {_units(option)}. {_channel_evidence(opportunity)}"
        ),
    )


def _bounty_packet(
    *,
    repo_name: str,
    niche: NicheConfig,
    opportunity: ChannelOpportunity,
    option: SimulatedOption | None,
    stamp: str,
) -> ChannelHandoffPacket:
    effort_hours = round(4.0 + 16.0 * (1.0 - niche.buildability), 1)
    return ChannelHandoffPacket(
        kind=PACKET_BOUNTY,
        channel_kind="bounty",
        niche_id=niche.id,
        title=f"Bounty scope: {niche.title}",
        audience="Human owner reviewing issue-backed rewards",
        objective="Convert explicit bounty demand into a scoped, one-off paid delivery.",
        system_prepared=(
            "Issue and label inventory from sanitized feedback",
            "Reproduction notes and affected workflow summary",
            "Effort estimate derived from buildability and scope",
        ),
        human_actions=(
            "Confirm the reward, scope, deadline, and payout terms with the funder",
            "Accept the bounty only under your own identity and account",
            "Submit the fix and request payout review",
            "Decline duplicate, unclear, or non-compliant bounty terms",
        ),
        checklist=(
            "Scope summary matches the funder's acceptance criteria",
            "Reproduce the issue and capture a failing test",
            "Implement the smallest correct fix with regression coverage",
            "Run ruff, mypy, pytest, and package checks before submitting",
            "Confirm one-off payout terms; never assume recurring revenue",
            "Record the payout as human-owned revenue, not simulated income",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. Bounties provide an explicit demand "
            "and reward signal with one-off payout.\n\n"
            f"Scope: {niche.workflow_bottleneck}. Estimated effort: "
            f"{effort_hours} engineering hours (planning estimate, not a commitment). "
            f"Modeled channel units: {_units(option)}. {_channel_evidence(opportunity)}"
        ),
    )


def _template_packet(
    *,
    repo_name: str,
    niche: NicheConfig,
    opportunity: ChannelOpportunity,
    option: SimulatedOption | None,
    stamp: str,
) -> ChannelHandoffPacket:
    return ChannelHandoffPacket(
        kind=PACKET_TEMPLATE,
        channel_kind="paid_template",
        niche_id=niche.id,
        title=f"Paid template launch: {niche.product_name} starter kit",
        audience="Human owner running checkout and support",
        objective="Ship a high-margin starter kit while keeping the core open.",
        system_prepared=(
            "Open-core starter kit outline and sample schema",
            "Quickstart documentation skeleton and test suite",
            "Open-core boundary notes for the human owner",
        ),
        human_actions=(
            "Choose the tier structure and set prices",
            "Replace <HUMAN_CHECKOUT_URL> with your own checkout page",
            "Publish the free core and the paid template under the MIT core license",
            "Own refunds, support, and customer communication",
        ),
        checklist=(
            "Define tiers: free core, pro starter kit, and optional team bundle",
            "Keep the open-core boundary explicit: no private customer data in paid files",
            "Write a quickstart, a demo, and a migration/schema example",
            "Include tests and a license; scan every template for secrets and PII",
            "Insert your own checkout link; the system never creates payment links",
            "Verify margin assumptions exclude paid infrastructure",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. Paid templates have high margin and "
            "low ongoing infrastructure cost.\n\n"
            f"Focus: {niche.workflow_bottleneck}. Modeled channel units: "
            f"{_units(option)}. {_channel_evidence(opportunity)}"
        ),
    )


def _audit_packet(
    *,
    repo_name: str,
    niche: NicheConfig,
    opportunity: ChannelOpportunity,
    option: SimulatedOption | None,
    stamp: str,
) -> ChannelHandoffPacket:
    return ChannelHandoffPacket(
        kind=PACKET_AUDIT,
        channel_kind="custom_audit",
        niche_id=niche.id,
        title=f"Custom audit offer: {niche.title}",
        audience="Human owner conducting discovery and delivery",
        objective="Offer a fixed-fee, human-delivered reliability or schema audit.",
        system_prepared=(
            "Audit discovery questions and pass/fail criteria template",
            "Evidence pack summarizing sanitized failure patterns",
            "Deliverable outline and boundary notes",
        ),
        human_actions=(
            "Run the discovery call and confirm the client's actual bottleneck",
            "Agree a fixed fee or retainer in writing before work starts",
            "Deliver findings yourself and keep client data out of the repository",
            "Decline engagements that require unsupported compliance or uptime claims",
        ),
        checklist=(
            "Discovery: who is affected, how often, and what the failure costs",
            "Define pass/fail criteria and the exact deliverable scope",
            "Frame a fixed fee or retainer with clear exclusions",
            "Prepare a 30-minute call agenda and boundary script",
            "Use sanitized artifacts only; never commit client data or secrets",
            "Avoid guaranteeing uptime, revenue, or compliance outcomes",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. Custom audits are higher ticket and "
            "human-delivered.\n\n"
            f"Focus: {niche.workflow_bottleneck}. Budget owner: {niche.budget_owner}. "
            f"Modeled channel units: {_units(option)}. {_channel_evidence(opportunity)}"
        ),
    )


def _saas_packet(
    *,
    repo_name: str,
    niche: NicheConfig,
    opportunity: ChannelOpportunity,
    option: SimulatedOption | None,
    stamp: str,
) -> ChannelHandoffPacket:
    return ChannelHandoffPacket(
        kind=PACKET_SAAS,
        channel_kind="recurring_saas",
        niche_id=niche.id,
        title=f"Hosted/pro tier plan: {niche.product_name}",
        audience="Human owner approving hosted infrastructure and billing",
        objective="Prepare a compounding recurring tier without violating zero-spend.",
        system_prepared=(
            "Recurring-value evidence from verified pain signals",
            "Treasury allocation summary and modeled net units",
            "Maintenance and hosting risk notes",
        ),
        human_actions=(
            "Verify the payment provider, refunds, privacy, and data handling",
            "Approve any paid hosting only after settled SYSTEM_TREASURY revenue exists",
            "Keep OWNER_VAULT separate and untouched",
            "Set the recurring price, support boundary, and cancellation policy",
        ),
        checklist=(
            "ROI summary: modeled units and recurring upside are documented",
            "Risk limits: zero spend in bootstrap, no paid infra before settled revenue",
            "Human approval before any live payment or hosting commitment",
            "Privacy and data-retention plan for hosted customer data",
            "Support and uptime boundaries stated honestly",
            "OWNER_VAULT is excluded from every allocation path",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. Recurring SaaS compounds MRR but adds "
            "maintenance and hosting needs; it remains blocked until proven revenue.\n\n"
            f"Focus: {niche.workflow_bottleneck}. Recurring signal: "
            f"{niche.recurring_willingness_to_pay:.2f}. Modeled channel units: "
            f"{_units(option)}. {_channel_evidence(opportunity)}"
        ),
    )


def _research_packet(
    *, repo_name: str, plan: OutcomePlan, stamp: str
) -> ChannelHandoffPacket:
    research_options = tuple(
        option for option in plan.options if option.kind == "paper_research"
    )
    strategy_summary = "; ".join(
        f"{option.label} net={option.expected_net_units:.3f}" for option in research_options
    ) or "no paper strategies configured"
    paper_capital = plan.policy.get("paper_capital_units", 0.0)
    return ChannelHandoffPacket(
        kind=PACKET_RESEARCH,
        channel_kind=None,
        niche_id=None,
        title="Capital and trading research allocation",
        audience="Human owner approving any research or capital action",
        objective="Evaluate capital outcomes on paper only; never execute live orders.",
        system_prepared=(
            "Backtest, paper-simulation, and capital-allocation models",
            "Modeled net units for every paper strategy",
            "Risk-limit and approval checklist",
        ),
        human_actions=(
            "Verify broker/exchange compliance, licensing, and tax before any live action",
            "Update invariants through a human-reviewed commit to enable live research",
            "Confirm SYSTEM_TREASURY loss caps and keep OWNER_VAULT untouched",
            "Run paper simulations first; never place live orders from this loop",
        ),
        checklist=(
            "Research-only status: live trading and live investing are disabled",
            f"Paper capital: {paper_capital} simulated units; no real funds at risk",
            "Modeled net units are planning scores, not financial forecasts",
            f"Paper strategies: {strategy_summary}",
            "SYSTEM_TREASURY loss cap remains 0.00 while live research is disabled",
            "OWNER_VAULT is excluded from every research and allocation path",
        ),
        body=(
            f"Prepared {stamp} for {repo_name}. This packet is research only. "
            "No live trading, live investing, automated payout, or OWNER_VAULT access "
            "is permitted. Enabling live research requires a human-reviewed invariant "
            "change, verified broker/exchange compliance, and a capped treasury policy."
        ),
    )


def _units(option: SimulatedOption | None) -> str:
    if option is None:
        return "unavailable"
    return f"{option.expected_net_units:.3f}"


def _channel_evidence(opportunity: ChannelOpportunity) -> str:
    hints = ", ".join(opportunity.matched_hints) if opportunity.matched_hints else "none"
    return (
        f"Composite {opportunity.composite_score:.3f}; demand "
        f"{opportunity.demand_count}; matched hints: {hints}. Simulated ranking only."
    )


def save_channel_handoffs(
    packets: Sequence[ChannelHandoffPacket],
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> tuple[Path, ...]:
    index: JsonObject = {
        "generated_at": iso_now(),
        "approval_required": True,
        "live_execution_allowed": False,
        "owner_vault_touched": False,
        "packets": [packet.to_json() for packet in packets],
    }
    sanitized, _ = sanitize_public_payload(index)
    if not isinstance(sanitized, dict):
        raise ConfigError("channel handoff index must remain an object")
    if dry_run:
        return ()
    ensure_dir(output_dir)
    written: list[Path] = []
    for packet in packets:
        slug = _SAFE_SLUG.fullmatch(packet.kind)
        if slug is None:
            raise ConfigError(f"unsafe channel handoff kind: {packet.kind!r}")
        path = output_dir / f"{packet.kind}.md"
        write_text_atomic(path, packet.to_markdown())
        written.append(path)
    index_path = output_dir / "index.json"
    write_json_atomic(index_path, sanitized)
    written.append(index_path)
    return tuple(written)
