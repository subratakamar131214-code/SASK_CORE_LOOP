"""Human-in-the-loop handoff packets and interview insight ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.core.errors import StateError
from src.handoff.packets import (
    KNOWN_PACKETS,
    HandoffBuilder,
    InterviewInsight,
    load_interview_insights,
    save_packets,
)
from src.ledger.ledger import LedgerSummary
from src.sensors.feedback import FeedbackIngestor
from src.sensors.niche_scoring import NicheScore

from helpers import FIXED_NOW


def _scores() -> list[NicheScore]:
    return [
        NicheScore(
            niche_id="ci-failure-diagnoser",
            total=0.72,
            components={"urgency": 0.96, "failure_cost": 0.95, "recurrence": 0.9},
            feedback_pressure=0.4,
        )
    ]


def _ledger() -> LedgerSummary:
    return LedgerSummary(
        entries=9,
        real_revenue_usd=0.0,
        simulated_revenue_usd=0.0,
        cost_usd=0.0,
        counts={"cycle": 3},
        last_ts="2026-11-20T00:00:00Z",
        chain_valid=True,
    )


def _insight() -> InterviewInsight:
    return InterviewInsight(
        insight_id="int-1",
        niche_id="ci-failure-diagnoser",
        role="staff engineer",
        bottleneck="release triage",
        budget_owner="platform manager",
        recurring_need="every deploy",
        quote="we lose hours each release",
        captured_at="2026-11-20T00:00:00Z",
        budget_signal_usd=120.0,
    )


def test_builder_prepares_all_configured_packets(config) -> None:
    builder = HandoffBuilder(config.handoff)
    packets = builder.build(
        repo_name=config.repo.name,
        scores=_scores(),
        ledger=_ledger(),
        generated_at="2026-11-20T12:00:00Z",
    )
    assert tuple(packet.kind for packet in packets) == config.handoff.required_packets
    assert {packet.kind for packet in packets} <= KNOWN_PACKETS
    for packet in packets:
        assert packet.human_actions
        assert packet.system_prepared
        assert packet.checklist
        markdown = packet.to_markdown()
        assert "## Human-owned actions" in markdown
        assert "- [ ]" in markdown

    outreach = next(packet for packet in packets if packet.kind == "design_partner_outreach")
    assert any("bulk" in action for action in outreach.human_actions)
    payment = next(packet for packet in packets if packet.kind == "payment_legal_checklist")
    assert any("hosted checkout" in action for action in payment.human_actions)


def test_human_and_system_ownership_are_separated(config) -> None:
    assert "payment_and_billing_setup" in config.handoff.human_owned_tasks
    assert "legal_entity_and_contracts" in config.handoff.human_owned_tasks
    assert "design_partner_calls" in config.handoff.human_owned_tasks
    assert "word_of_mouth" in config.handoff.human_owned_tasks
    assert set(config.handoff.human_owned_tasks).isdisjoint(config.handoff.system_owned_tasks)
    assert "reliability_evidence" in config.handoff.system_owned_tasks


def test_save_packets_respects_dry_run(config, tmp_path: Path) -> None:
    packets = HandoffBuilder(config.handoff).build(
        repo_name=config.repo.name, scores=_scores()
    )
    assert save_packets(packets, tmp_path / "handoff", dry_run=True) == ()
    assert not (tmp_path / "handoff").exists()

    written = save_packets(packets, tmp_path / "handoff")
    assert len(written) == len(packets) + 1
    assert (tmp_path / "handoff" / "index.json").exists()
    for packet in packets:
        assert (tmp_path / "handoff" / f"{packet.kind}.md").exists()


def test_interview_insight_becomes_top_priority_feedback(config) -> None:
    document = _insight().to_document()
    assert document["labels"] == [
        "customer",
        "critical",
        "interview",
        "ci-failure-diagnoser",
    ]
    assert document["kind"] == "complaint"

    ingestor = FeedbackIngestor(config.feedback, config.active_niches(), clock=lambda: FIXED_NOW)
    report = ingestor.ingest([document])
    item = report.items[0]
    assert item.priority > 20.0
    assert item.matched_niches == ("ci-failure-diagnoser",)
    assert item.is_customer is True


def test_interview_insights_load_from_list_and_wrapper(tmp_path: Path) -> None:
    insight = _insight().to_json()
    list_path = tmp_path / "list.json"
    list_path.write_text(json.dumps([insight]), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"insights": [insight]}), encoding="utf-8")

    assert load_interview_insights(list_path)[0].bottleneck == "release triage"
    assert load_interview_insights(wrapped)[0].budget_signal_usd == 120.0


def test_interview_insights_reject_unsafe_or_incomplete_documents(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"insights": [{"insight_id": "x"}]}), encoding="utf-8")
    with pytest.raises(StateError, match="must be a non-empty string"):
        load_interview_insights(bad)

    with pytest.raises(StateError, match="unsafe interview"):
        InterviewInsight(
            insight_id="../escape",
            niche_id="ci-failure-diagnoser",
            role="engineer",
            bottleneck="triage",
            budget_owner="manager",
            recurring_need="weekly",
            quote="sanitized",
            captured_at="2026-11-20T00:00:00Z",
        )
    assert load_interview_insights(tmp_path / "missing.json") == ()
