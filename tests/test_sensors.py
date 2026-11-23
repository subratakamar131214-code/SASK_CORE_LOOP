"""Feedback ingestion, niche scoring, and opportunity signals."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from src.core.config import ScoringWeights
from src.core.errors import ConfigError, StateError
from src.sensors.feedback import FeedbackIngestor, load_feedback_documents
from src.sensors.niche_scoring import NicheScore, NicheScorer, feedback_pressure
from src.sensors.opportunity import OpportunityScanner

from helpers import FIXED_NOW, make_feedback_item


def _documents() -> list[object]:
    return [
        {
            "id": "cust-1",
            "source": "customer",
            "kind": "bug",
            "title": "Pipeline build failure blocks releases",
            "body": "Email ops@example.com; the ci log shows a flaky test.",
            "labels": ["customer", "critical"],
            "reactions": {"totalCount": 12},
            "created_at": "2026-11-19T00:00:00Z",
        },
        {
            "id": 42,
            "title": "OpenAPI schema migration breaks JSON",
            "body": "Swagger migration reports a breaking change.",
            "labels": ["bug"],
            "reactions": {"totalCount": 3},
        },
        {
            "id": "mail-1",
            "source": "inbox",
            "kind": "question",
            "title": "How do I get started?",
            "body": "Looking for a quick start guide.",
            "labels": [],
        },
    ]


def _ingestor(config) -> FeedbackIngestor:
    return FeedbackIngestor(
        config.feedback, config.active_niches(), clock=lambda: FIXED_NOW
    )


def test_feedback_priority_and_sanitization(config) -> None:
    report = _ingestor(config).ingest(_documents())
    assert [item.id for item in report.items] == ["cust-1", "gh-42", "mail-1"]
    customer, github, inbox = report.items

    assert customer.source == "customer"
    assert customer.kind == "bug"
    assert customer.is_customer is True
    assert customer.priority > github.priority > inbox.priority
    assert "ops@example.com" not in customer.body
    assert "[REDACTED:EMAIL]" in customer.body
    assert report.redactions["email"] >= 1

    assert github.source == "github_issue"
    assert github.matched_niches == ("openapi-schema-migrator",)
    assert "ci-failure-diagnoser" in customer.matched_niches
    assert inbox.matched_niches == ()
    assert inbox.source == "inbox"


def test_feedback_label_inference_and_kind_normalization(config) -> None:
    report = _ingestor(config).ingest(
        [
            {
                "id": "labelled-1",
                "title": "Something is wrong",
                "body": "It broke again.",
                "labels": ["support", "complaint"],
                "kind": "feature request",
            }
        ]
    )
    item = report.items[0]
    assert item.source == "support"
    assert item.kind == "feature_request"


def test_feedback_deduplicates_by_id_keeping_highest_priority(config) -> None:
    low = {
        "id": "dup-1",
        "source": "inbox",
        "title": "Duplicate report",
        "body": "ci failure",
    }
    high = {
        "id": "dup-1",
        "source": "customer",
        "title": "Duplicate report",
        "body": "ci failure",
    }
    report = _ingestor(config).ingest([low, high])
    assert len(report.items) == 1
    assert report.items[0].source == "customer"


def test_feedback_drops_invalid_documents_and_respects_limit(config) -> None:
    report = _ingestor(config).ingest(
        [None, {}, {"title": "", "body": ""}, *_documents()], max_items=2
    )
    assert report.raw_count == 6
    assert report.dropped_count == 3
    assert len(report.items) == 2


def test_feedback_ignores_inactive_niches(config) -> None:
    niches = (
        replace(config.niches[0], active=False),
        config.niches[1],
    )
    ingestor = FeedbackIngestor(config.feedback, niches, clock=lambda: FIXED_NOW)
    report = ingestor.ingest([_documents()[0]])
    assert "ci-failure-diagnoser" not in report.items[0].matched_niches


def test_feedback_loaders_support_json_jsonl_and_inbox(tmp_path: Path) -> None:
    list_file = tmp_path / "list.json"
    list_file.write_text(json.dumps([{"id": "a", "title": "A"}]), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"items": [{"id": "b", "title": "B"}]}), encoding="utf-8"
    )
    single = tmp_path / "single.json"
    single.write_text(json.dumps({"id": "c", "title": "C"}), encoding="utf-8")
    jsonl = tmp_path / "feed.jsonl"
    jsonl.write_text(
        "\n".join(json.dumps({"id": f"d{index}"}) for index in range(2)) + "\n",
        encoding="utf-8",
    )
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "note.json").write_text(
        json.dumps([{"id": "e", "title": "E"}]), encoding="utf-8"
    )

    documents = load_feedback_documents(
        paths=[list_file, wrapped, single, jsonl], inbox_dir=inbox
    )
    ids = [item.get("id") for item in documents if isinstance(item, dict)]
    assert ids == ["a", "b", "c", "d0", "d1", "e"]

    with pytest.raises(StateError, match="not found"):
        load_feedback_documents(paths=[tmp_path / "missing.json"])


def test_niche_scoring_ranks_seed_niches(config) -> None:
    scorer = NicheScorer(config.scoring)
    ranked = scorer.rank(config.active_niches())
    assert [score.niche_id for score in ranked] == [
        "ci-failure-diagnoser",
        "openapi-schema-migrator",
    ]
    assert ranked[0].components["pain"] == pytest.approx(0.92)
    assert abs(sum(score.total for score in ranked)) > 0.0


def test_feedback_pressure_changes_the_ranking(config) -> None:
    scorer = NicheScorer(config.scoring)
    feedback = (
        make_feedback_item(
            "op-1",
            priority=30.0,
            matched_niches=("openapi-schema-migrator",),
            title="OpenAPI migration blocked",
        ),
    )
    ranked = scorer.rank(config.active_niches(), feedback)
    assert ranked[0].niche_id == "openapi-schema-migrator"
    assert ranked[0].feedback_count == 1
    assert ranked[0].feedback_pressure == pytest.approx(1.0)
    assert ranked[0].evidence_ids == ("op-1",)
    assert ranked[1].feedback_pressure == 0.0


def test_feedback_must_have_the_highest_weight() -> None:
    weights = ScoringWeights(
        feedback=0.2,
        pain=0.8,
        willingness_to_pay=0.0,
        buildability=0.0,
        competition_inverse=0.0,
        compliance_safety=0.0,
    )
    with pytest.raises(ConfigError, match="highest scoring weight"):
        NicheScorer(weights)


def test_scoring_requires_normalized_weights() -> None:
    weights = ScoringWeights(0.5, 0.5, 0.5, 0.0, 0.0, 0.0)
    with pytest.raises(ConfigError, match="sum to 1.0"):
        NicheScorer(weights)


def test_inactive_niches_are_not_ranked(config) -> None:
    niches = (replace(config.niches[0], active=False), config.niches[1])
    ranked = NicheScorer(config.scoring).rank(niches)
    assert [score.niche_id for score in ranked] == ["openapi-schema-migrator"]


def test_feedback_pressure_is_bounded() -> None:
    assert feedback_pressure([]) == 0.0
    assert feedback_pressure([make_feedback_item(priority=0.0)]) == 0.0
    balanced = feedback_pressure([make_feedback_item(priority=1.0)])
    assert 0.0 < balanced < 1.0
    saturated = feedback_pressure([make_feedback_item(priority=1000.0)])
    assert saturated == 1.0


def test_opportunity_scanner_classifies_signals(config) -> None:
    scanner = OpportunityScanner(config.active_niches(), clock=lambda: FIXED_NOW)
    scores = [
        NicheScore(niche_id="ci-failure-diagnoser", total=0.9),
        NicheScore(niche_id="openapi-schema-migrator", total=0.5),
    ]
    signals = scanner.scan(scores)
    assert signals[0].kind == "focus_validation"
    assert signals[0].niche_id == "ci-failure-diagnoser"
    assert signals[0].strength == pytest.approx(0.675)
    assert signals[0].id.startswith("opp-")
    assert signals[1].kind == "seed_validation"
    assert signals[0].strength >= signals[1].strength


def test_opportunity_scanner_prioritizes_urgent_feedback(config) -> None:
    scanner = OpportunityScanner(config.active_niches(), clock=lambda: FIXED_NOW)
    feedback = (
        make_feedback_item("urgent-1", priority=50.0, matched_niches=("ci-failure-diagnoser",)),
    )
    scores = [
        NicheScore(
            niche_id="ci-failure-diagnoser",
            total=0.9,
            feedback_pressure=1.0,
            feedback_count=1,
            evidence_ids=("urgent-1",),
        )
    ]
    signals = scanner.scan(scores, feedback)
    assert signals[0].kind == "urgent_feedback"
    assert signals[0].strength == pytest.approx(1.0)
    assert signals[0].evidence_ids == ("urgent-1",)


def test_opportunity_ids_are_deterministic(config) -> None:
    scanner = OpportunityScanner(config.active_niches(), clock=lambda: FIXED_NOW)
    scores = [NicheScore(niche_id="ci-failure-diagnoser", total=0.9)]
    first = scanner.scan(scores)
    second = scanner.scan(scores)
    assert [signal.id for signal in first] == [signal.id for signal in second]
