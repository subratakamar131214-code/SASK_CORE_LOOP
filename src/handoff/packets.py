"""Prepare human-owned work while keeping automated execution auditable.

The system may prepare evidence, scripts, questionnaires, pitch briefs, and
readiness checklists. Humans execute payment/legal/tax setup, one-to-one design
partner calls, trust building, partnerships, and word-of-mouth. No packet asks a
human to paste secrets into the repository or to make bulk/unsolicited outreach.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.config import HandoffConfig
from src.core.errors import ConfigError, StateError
from src.core.io import ensure_dir, read_json, write_json_atomic, write_text_atomic
from src.core.redact import redact_text, sanitize_public_payload
from src.core.timeutil import iso_now
from src.core.types import JsonObject, JsonValue
from src.ledger.ledger import LedgerSummary
from src.sensors.niche_scoring import NicheScore

PACKET_RELIABILITY = "reliability_proof"
PACKET_BENCHMARK = "benchmark_proof"
PACKET_OUTREACH = "design_partner_outreach"
PACKET_INTERVIEW = "interview_script"
PACKET_PARTNERSHIP = "partnership_pitch"
PACKET_PAYMENT_LEGAL = "payment_legal_checklist"
KNOWN_PACKETS = frozenset(
    {
        PACKET_RELIABILITY,
        PACKET_BENCHMARK,
        PACKET_OUTREACH,
        PACKET_INTERVIEW,
        PACKET_PARTNERSHIP,
        PACKET_PAYMENT_LEGAL,
    }
)
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")


@dataclass(frozen=True)
class HandoffPacket:
    kind: str
    title: str
    audience: str
    objective: str
    system_prepared: tuple[str, ...]
    human_actions: tuple[str, ...]
    checklist: tuple[str, ...]
    body: str

    def __post_init__(self) -> None:
        if self.kind not in KNOWN_PACKETS:
            raise ConfigError(f"unknown handoff packet kind: {self.kind!r}")
        if not self.human_actions:
            raise ConfigError(f"handoff packet {self.kind} requires human actions")
        if not self.body.strip():
            raise ConfigError(f"handoff packet {self.kind} requires a body")

    def to_json(self) -> JsonObject:
        return {
            "kind": self.kind,
            "title": self.title,
            "audience": self.audience,
            "objective": self.objective,
            "system_prepared": list(self.system_prepared),
            "human_actions": list(self.human_actions),
            "checklist": list(self.checklist),
        }

    def to_markdown(self) -> str:
        prepared = "\n".join(f"- {item}" for item in self.system_prepared)
        human = "\n".join(f"- [ ] {item}" for item in self.human_actions)
        checklist = "\n".join(f"- [ ] {item}" for item in self.checklist)
        body = redact_text(self.body).text
        return (
            f"# {self.title}\n\n"
            f"**Audience:** {self.audience}\n\n"
            f"**Objective:** {self.objective}\n\n"
            f"## Prepared by the system\n\n{prepared}\n\n"
            f"## Human-owned actions\n\n{human}\n\n"
            f"## Checklist\n\n{checklist}\n\n"
            f"## Brief\n\n{body}\n"
        )


class HandoffBuilder:
    def __init__(self, config: HandoffConfig) -> None:
        unknown = set(config.required_packets) - KNOWN_PACKETS
        if unknown:
            raise ConfigError(f"unknown required handoff packets: {sorted(unknown)}")
        self._config = config

    def build(
        self,
        *,
        repo_name: str,
        scores: Sequence[NicheScore] = (),
        ledger: LedgerSummary | None = None,
        generated_at: str | None = None,
    ) -> tuple[HandoffPacket, ...]:
        stamp = generated_at or iso_now()
        top = scores[0] if scores else None
        builders = {
            PACKET_RELIABILITY: lambda: _reliability_packet(
                repo_name, top, ledger, stamp
            ),
            PACKET_BENCHMARK: lambda: _benchmark_packet(repo_name, scores, stamp),
            PACKET_OUTREACH: lambda: _outreach_packet(repo_name, top, stamp),
            PACKET_INTERVIEW: lambda: _interview_packet(repo_name, top, stamp),
            PACKET_PARTNERSHIP: lambda: _partnership_packet(repo_name, top, ledger, stamp),
            PACKET_PAYMENT_LEGAL: lambda: _payment_legal_packet(stamp),
        }
        return tuple(builders[kind]() for kind in self._config.required_packets)


@dataclass(frozen=True)
class InterviewInsight:
    insight_id: str
    niche_id: str
    role: str
    bottleneck: str
    budget_owner: str
    recurring_need: str
    quote: str
    captured_at: str
    budget_signal_usd: float = 0.0

    def __post_init__(self) -> None:
        if not _SAFE_ID.fullmatch(self.insight_id):
            raise StateError(f"unsafe interview insight id: {self.insight_id!r}")
        if not _SAFE_ID.fullmatch(self.niche_id):
            raise StateError(f"unsafe interview niche id: {self.niche_id!r}")
        if self.budget_signal_usd < 0.0:
            raise StateError("interview budget signal cannot be negative")
        text_fields = (
            "role",
            "bottleneck",
            "budget_owner",
            "recurring_need",
            "quote",
            "captured_at",
        )
        for name in text_fields:
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise StateError(f"interview insight {name} must be a non-empty string")

    def to_document(self) -> dict[str, object]:
        body = (
            f"Role: {self.role}. Budget owner: {self.budget_owner}. "
            f"Recurring need: {self.recurring_need}. Quote: {self.quote}. "
            f"Indicated monthly budget: ${self.budget_signal_usd:.2f}."
        )
        return {
            "id": self.insight_id,
            "source": "customer",
            "kind": "complaint",
            "title": f"Interview: {self.bottleneck}",
            "body": body,
            "labels": ["customer", "critical", "interview", self.niche_id],
            "created_at": self.captured_at,
        }

    def to_json(self) -> JsonObject:
        return {
            "insight_id": self.insight_id,
            "niche_id": self.niche_id,
            "role": self.role,
            "bottleneck": self.bottleneck,
            "budget_owner": self.budget_owner,
            "recurring_need": self.recurring_need,
            "quote": self.quote,
            "captured_at": self.captured_at,
            "budget_signal_usd": round(self.budget_signal_usd, 2),
        }


def load_interview_insights(path: Path) -> tuple[InterviewInsight, ...]:
    raw = read_json(path, None)
    if raw is None:
        return ()
    if isinstance(raw, dict):
        nested = raw.get("insights")
        if not isinstance(nested, list):
            raise StateError("interview insights file must contain an 'insights' list")
        items = nested
    elif isinstance(raw, list):
        items = raw
    else:
        raise StateError("interview insights file must be a list or object")
    insights: list[InterviewInsight] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise StateError(f"interview insight {index} is not an object")
        insights.append(_insight_from_mapping(item, index))
    return tuple(insights)


def insight_documents(insights: Sequence[InterviewInsight]) -> list[dict[str, object]]:
    return [insight.to_document() for insight in insights]


def save_packets(
    packets: Sequence[HandoffPacket],
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> tuple[Path, ...]:
    index: JsonObject = {
        "generated_at": iso_now(),
        "packets": [packet.to_json() for packet in packets],
        "human_owned_only": True,
    }
    sanitized, _ = sanitize_public_payload(index)
    if not isinstance(sanitized, dict):
        raise StateError("handoff index must remain an object")
    if dry_run:
        return ()
    ensure_dir(output_dir)
    written: list[Path] = []
    for packet in packets:
        path = output_dir / f"{packet.kind}.md"
        write_text_atomic(path, packet.to_markdown())
        written.append(path)
    index_path = output_dir / "index.json"
    write_json_atomic(index_path, sanitized)
    written.append(index_path)
    return tuple(written)


def _insight_from_mapping(document: dict[str, JsonValue], index: int) -> InterviewInsight:
    def text(key: str) -> str:
        value = document.get(key)
        if not isinstance(value, str) or not value.strip():
            raise StateError(f"interview insight {index}.{key} must be a non-empty string")
        return value.strip()

    budget = document.get("budget_signal_usd", 0.0)
    if isinstance(budget, bool) or not isinstance(budget, (int, float)):
        raise StateError(f"interview insight {index}.budget_signal_usd must be numeric")
    return InterviewInsight(
        insight_id=text("insight_id"),
        niche_id=text("niche_id"),
        role=text("role"),
        bottleneck=text("bottleneck"),
        budget_owner=text("budget_owner"),
        recurring_need=text("recurring_need"),
        quote=text("quote"),
        captured_at=text("captured_at"),
        budget_signal_usd=float(budget),
    )


def _top_context(top: NicheScore | None) -> str:
    if top is None:
        return "No niche currently clears the scoring bar."
    components = ", ".join(
        f"{name}={value:.2f}" for name, value in sorted(top.components.items())
    )
    return (
        f"Top niche `{top.niche_id}` scores {top.total:.3f} "
        f"({components}) with feedback pressure {top.feedback_pressure:.2f}."
    )


def _reliability_packet(
    repo_name: str,
    top: NicheScore | None,
    ledger: LedgerSummary | None,
    stamp: str,
) -> HandoffPacket:
    if ledger is None:
        ledger_line = "Ledger summary is not available in this run."
    else:
        ledger_line = (
            f"{ledger.entries} ledger entries, chain valid: {ledger.chain_valid}, "
            f"real revenue ${ledger.real_revenue_usd:.2f}, simulated revenue "
            f"${ledger.simulated_revenue_usd:.2f}, infrastructure cost "
            f"${ledger.cost_usd:.2f}."
        )
    return HandoffPacket(
        kind=PACKET_RELIABILITY,
        title="Reliability proof for design partners",
        audience="Human owner speaking with engineering leaders",
        objective="Prove the product behaves deterministically and within budget.",
        system_prepared=(
            "Hash-chained ledger summary and invariant digest",
            "Offline deterministic fallback evidence",
            "Zero-cost inference and packaging receipts",
        ),
        human_actions=(
            "Review the proof for accuracy before sharing it",
            "Share only sanitized summaries with prospective design partners",
            "Never promise uptime, revenue, or compliance outcomes",
        ),
        checklist=(
            "Invariant digest matches the pinned value",
            "Ledger chain validates with zero infrastructure cost",
            "No customer names, secrets, or private data appear in the proof",
        ),
        body=f"Repository: {repo_name}. {_top_context(top)} {ledger_line} "
        f"Prepared at {stamp}. The system runs on free tiers with a deterministic "
        "fallback and fails closed when an invariant is violated.",
    )


def _benchmark_packet(
    repo_name: str, scores: Sequence[NicheScore], stamp: str
) -> HandoffPacket:
    lines = "\n".join(
        f"- `{score.niche_id}`: total {score.total:.3f}, evidence "
        f"{', '.join(score.evidence_ids) or 'none'}"
        for score in scores
    )
    return HandoffPacket(
        kind=PACKET_BENCHMARK,
        title="Benchmark and reliability proof plan",
        audience="Human owner and design partner engineers",
        objective="Run a reproducible offline benchmark before asking for money.",
        system_prepared=(
            "Deterministic product builds and generated tests",
            "Sanitized scoring evidence and component breakdown",
            "Offline cycle command that writes nothing to the repository",
        ),
        human_actions=(
            "Ask the design partner for permission before running on their logs",
            "Run the benchmark with the partner present and record the outcome",
            "Record only sanitized results; never copy raw customer logs",
        ),
        checklist=(
            "Benchmark runs offline with zero network calls",
            "Generated tests pass in the partner environment",
            "Results are reproducible across two runs",
        ),
        body=f"Repository: {repo_name}. Prepared at {stamp}.\n\n{lines}",
    )


def _outreach_packet(
    repo_name: str, top: NicheScore | None, stamp: str
) -> HandoffPacket:
    niche = top.niche_id if top else "the strongest niche"
    template = (
        "Hello — I maintain a small open-core tool for teams that hit recurring "
        f"{niche} failures. I am not selling anything in this message. Would you be "
        "open to a 20-minute conversation about how often this blocks releases and "
        "who owns the budget for fixing it?"
    )
    return HandoffPacket(
        kind=PACKET_OUTREACH,
        title="One-to-one design partner outreach",
        audience="Human owner only",
        objective="Start genuine conversations with high-pain recurring buyers.",
        system_prepared=(
            "Qualification criteria focused on urgency, failure cost, recurrence, and budget",
            f"One-to-one message draft for {niche}",
            "Privacy-safe note-taking template",
        ),
        human_actions=(
            "Send messages individually to people you already have a reason to contact",
            "Do not buy lists or send bulk or unsolicited outreach",
            "Respect opt-outs immediately and never pressure for payment",
        ),
        checklist=(
            "Recipient is a real practitioner or budget owner with a plausible recurring pain",
            "Message is personal, short, and does not promise revenue or uptime",
            "Conversation notes are sanitized before they enter the inbox",
        ),
        body=f"Prepared at {stamp}.\n\nDraft:\n\n> {template}",
    )


def _interview_packet(
    repo_name: str, top: NicheScore | None, stamp: str
) -> HandoffPacket:
    niche = top.niche_id if top else "the strongest niche"
    questions = (
        "1. What breaks in the workflow, and how often does it recur?\n"
        "2. What does one failure cost in downtime, rework, or compliance exposure?\n"
        "3. Who owns the budget for fixing this, and what approval process applies?\n"
        "4. What do you use today, and what makes you switch or renew?\n"
        "5. What would make you trust a small tool enough to run it on every release?\n"
        "6. What reliability evidence would you need before paying monthly?"
    )
    schema = (
        '{"insights": [{"insight_id": "int-1", "niche_id": "' + niche + '", '
        '"role": "staff engineer", "bottleneck": "release triage", '
        '"budget_owner": "platform manager", "recurring_need": "every deploy", '
        '"quote": "sanitized quote", "captured_at": "2026-01-01T00:00:00Z", '
        '"budget_signal_usd": 0.0}]}'
    )
    return HandoffPacket(
        kind=PACKET_INTERVIEW,
        title="Design partner interview script",
        audience="Human owner conducting the call",
        objective="Capture recurring pain, failure cost, budget ownership, and trust needs.",
        system_prepared=(
            "Questionnaire focused on recurring expensive B2B pain",
            "Sanitized insight schema that the sensor layer treats as top priority",
            "Follow-up checklist for reliability proof",
        ),
        human_actions=(
            "Run the call yourself and build genuine trust",
            "Record only sanitized notes; remove names, emails, and private data",
            "Save sanitized answers to the interview insights file for ingestion",
        ),
        checklist=(
            "At least one concrete recurring bottleneck is documented",
            "Budget owner is named by role, not by personal data",
            "Recurring reliability need is explicit",
        ),
        body=(
            f"Prepared at {stamp}.\n\n## Questions\n\n{questions}\n\n"
            f"## Sanitized insight file\n\nWrite `{schema}` to "
            "`state/handoff/interview_insights.json` (never commit raw notes)."
        ),
    )


def _partnership_packet(
    repo_name: str,
    top: NicheScore | None,
    ledger: LedgerSummary | None,
    stamp: str,
) -> HandoffPacket:
    chain = "unknown"
    if ledger is not None:
        chain = "valid" if ledger.chain_valid else "invalid"
    return HandoffPacket(
        kind=PACKET_PARTNERSHIP,
        title="Partnership pitch brief",
        audience="Human owner meeting platform or agency partners",
        objective="Propose a reliability integration that compounds distribution.",
        system_prepared=(
            "Reliability proof summary and ledger chain status",
            "Niche and workflow bottleneck context",
            "Integration scope that stays free and open-core",
        ),
        human_actions=(
            "Choose partners whose users already hit this recurring pain",
            "Negotiate scope, attribution, and support boundaries yourself",
            "Sign agreements and handle money only through approved human processes",
        ),
        checklist=(
            "Partner has a measurable distribution channel",
            "Integration requires no paid API or paid infrastructure",
            "No revenue share is promised by automated code",
        ),
        body=(
            f"Repository: {repo_name}. Prepared at {stamp}. "
            f"Ledger chain: {chain}. {_top_context(top)} "
            "Propose a small, verifiable integration that reduces recurring failures "
            "for the partner's users before discussing economics."
        ),
    )


def _payment_legal_packet(stamp: str) -> HandoffPacket:
    return HandoffPacket(
        kind=PACKET_PAYMENT_LEGAL,
        title="Payment, legal, tax, and banking readiness checklist",
        audience="Human owner and their professional advisers",
        objective="Make paid operations lawful and durable without automated money handling.",
        system_prepared=(
            "Zero-cost release evidence and open-core license inventory",
            "Sanitized security and data-handling summary",
            "Checklist of human-only setup steps",
        ),
        human_actions=(
            "Set up the legal entity, contracts, and terms of service",
            "Register for tax and configure invoices and refunds",
            "Open the payout account and store credentials only in the platform vault",
            "Configure hosted checkout; never commit payment credentials",
        ),
        checklist=(
            "Legal entity and contract terms are reviewed by a professional",
            "Tax registration and invoicing are configured",
            "Payout account is verified and isolated from the repository",
            "Refund, cancellation, and data-processing terms are published",
            "Automated payouts remain disabled",
        ),
        body=f"Prepared at {stamp}. These steps are human-owned by invariant. "
        "The system never creates payment credentials, automates payouts, or handles "
        "bank/tax data.",
    )
