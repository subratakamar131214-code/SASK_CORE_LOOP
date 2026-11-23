"""Human-reviewed discovery briefs and verified pain-interview ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.core.config import OutreachConfig
from src.core.errors import ConfigError, StateError
from src.handoff.discovery_briefs import (
    DiscoveryBriefBuilder,
    save_discovery_briefs,
)
from src.sensors.feedback import FeedbackIngestor
from src.sensors.niche_scoring import NicheScorer
from src.sensors.pain_interviews import (
    PainFinding,
    load_pain_findings,
    pain_feedback_documents,
    save_pain_findings,
)

from helpers import FIXED_NOW, fake_github_token


def _finding(**overrides: object) -> PainFinding:
    values: dict[str, object] = {
        "finding_id": "pain-ci-1",
        "niche_id": "ci-failure-diagnoser",
        "workflow_bottleneck": "release triage across long-lived branches",
        "breaks_today": "a flaky pipeline blocks the deploy and nobody owns triage",
        "estimated_cost_or_hours_lost": "6 engineering hours per release",
        "frequency": "every production release",
        "recurring_willingness_to_pay_signal": True,
        "notes_sanitized": "follow up with the platform team",
        "captured_at": "2026-11-20T00:00:00Z",
    }
    values.update(overrides)
    return PainFinding(**values)  # type: ignore[arg-type]


def _ingestor(config) -> FeedbackIngestor:
    return FeedbackIngestor(
        config.feedback, config.active_niches(), clock=lambda: FIXED_NOW
    )


def test_outreach_defaults_to_human_review_only(config, guard) -> None:
    assert config.outreach.auto_send_enabled is False
    assert guard.invariants.outreach_auto_send_enabled is False
    with pytest.raises(ConfigError, match="auto_send_enabled"):
        DiscoveryBriefBuilder(OutreachConfig(auto_send_enabled=True))


def test_discovery_briefs_are_tailored_and_never_auto_send(config) -> None:
    scores = NicheScorer(config.scoring).rank(config.active_niches())
    briefs = DiscoveryBriefBuilder(config.outreach).build(
        scores,
        config.active_niches(),
        generated_at="2026-11-20T12:00:00Z",
    )
    assert briefs
    top = briefs[0]
    assert top.niche_id == scores[0].niche_id
    assert top.auto_send_enabled is False
    assert len(top.questions) == 6
    joined = " ".join(top.questions).lower()
    assert "what breaks today" in joined
    assert "how often" in joined
    assert "engineering hours" in joined
    assert "budget" in joined
    assert "recurringly" in joined
    assert any("bulk" in action for action in top.human_actions)

    payload = top.to_json()
    assert payload["kind"] == "discovery_brief"
    assert payload["auto_send_enabled"] is False
    assert payload["human_review_required"] is True
    markdown = top.to_markdown()
    assert "Human review required" in markdown
    assert "Automated sending is disabled" in markdown
    assert "- [ ]" in markdown


def test_discovery_briefs_respect_dry_run(config, tmp_path: Path) -> None:
    scores = NicheScorer(config.scoring).rank(config.active_niches())
    briefs = DiscoveryBriefBuilder(config.outreach).build(scores, config.active_niches())
    output = tmp_path / "discovery"
    assert save_discovery_briefs(briefs, output, dry_run=True) == ()
    assert not output.exists()

    written = save_discovery_briefs(briefs, output)
    assert len(written) == len(briefs) + 1
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    assert index["auto_send_enabled"] is False
    assert index["human_review_required"] is True
    assert all(entry["auto_send_enabled"] is False for entry in index["briefs"])
    for brief in briefs:
        assert (output / f"{brief.niche_id}.md").exists()


def test_pain_finding_sanitizes_pii_and_secrets_before_any_write(tmp_path: Path) -> None:
    token = fake_github_token()
    finding = _finding(
        breaks_today="deploy blocked; contact ops@example.com",
        notes_sanitized=f"owner email ops@example.com and token {token}",
    )
    rendered = json.dumps(finding.to_json())
    assert "ops@example.com" not in rendered
    assert token not in rendered
    assert "[REDACTED:EMAIL]" in finding.breaks_today
    assert "[REDACTED:GITHUB_TOKEN]" in finding.notes_sanitized

    state_path = tmp_path / "pain_interviews.json"
    save_pain_findings((finding,), state_path)
    stored = state_path.read_text(encoding="utf-8")
    assert "ops@example.com" not in stored
    assert token not in stored
    loaded = load_pain_findings(state_path)
    assert loaded == (finding,)


def test_pain_findings_require_verification_and_safe_ids() -> None:
    with pytest.raises(StateError, match="human-validated"):
        _finding(verified=False)
    with pytest.raises(StateError, match="unsafe pain niche id"):
        _finding(niche_id="../escape")
    with pytest.raises(StateError, match="unsafe pain finding id"):
        _finding(finding_id="Bad Id")
    with pytest.raises(StateError, match="must be a boolean"):
        _finding(recurring_willingness_to_pay_signal="maybe")


def test_pain_documents_carry_top_priority_labels(config) -> None:
    documents = pain_feedback_documents((_finding(),))
    labels = documents[0]["labels"]
    assert "pain_interview" in labels
    assert "verified" in labels
    assert "recurring_wtp" in labels
    assert "ci-failure-diagnoser" in labels


def test_verified_recurring_pain_outranks_other_feedback(config) -> None:
    pain = _finding()
    interview = {
        "id": "int-1",
        "source": "customer",
        "kind": "complaint",
        "title": "Interview: release triage",
        "body": "we lose hours each release",
        "labels": ["customer", "critical", "interview"],
        "created_at": "2026-11-20T00:00:00Z",
    }
    ordinary = {
        "id": "gh-1",
        "source": "github_issue",
        "kind": "bug",
        "title": "CI pipeline failure",
        "body": "ci failure in the log",
        "labels": ["bug"],
        "created_at": "2026-11-20T00:00:00Z",
    }
    report = _ingestor(config).ingest(
        [*pain_feedback_documents((pain,)), interview, ordinary]
    )
    assert [item.id for item in report.items] == ["pain-ci-1", "int-1", "gh-1"]
    assert report.items[0].priority > report.items[1].priority > report.items[2].priority
    assert report.items[0].priority >= 39.0
    assert report.items[0].matched_niches == ("ci-failure-diagnoser",)

    scores = NicheScorer(config.scoring).rank(config.active_niches(), report.items)
    top = scores[0]
    assert top.niche_id == "ci-failure-diagnoser"
    assert top.feedback_pressure == pytest.approx(1.0)
    assert top.evidence_ids[0] == "pain-ci-1"


def test_unverified_pain_documents_cannot_be_loaded(tmp_path: Path) -> None:
    path = tmp_path / "pain.json"
    path.write_text(
        json.dumps(
            {
                "findings": [
                    {
                        "finding_id": "pain-1",
                        "niche_id": "ci-failure-diagnoser",
                        "workflow_bottleneck": "triage",
                        "breaks_today": "deploy blocked",
                        "estimated_cost_or_hours_lost": "3 hours",
                        "frequency": "weekly",
                        "recurring_willingness_to_pay_signal": True,
                        "captured_at": "2026-11-20T00:00:00Z",
                        "verified": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StateError, match="human-validated"):
        load_pain_findings(path)

    with pytest.raises(StateError, match="not found"):
        load_pain_findings(tmp_path / "missing.json")
