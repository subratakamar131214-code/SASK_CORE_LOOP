"""End-to-end CLI tests against an isolated copy of the real configuration."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from src import orchestrator
from src.core.invariants import InvariantGuard

from helpers import fake_github_token


@pytest.fixture
def isolated_root(repo_root: Path, tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(repo_root / "config", root / "config")
    shutil.copytree(repo_root / ".github" / "workflows", root / ".github" / "workflows")
    root.mkdir(exist_ok=True)
    return root


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    code = orchestrator.main(list(args))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else {}
    return code, payload


def _write_feedback(root: Path) -> None:
    inbox = root / "inbox"
    inbox.mkdir()
    (inbox / "team.json").write_text(
        json.dumps(
            [
                {
                    "id": "gh-1",
                    "source": "github_issue",
                    "kind": "bug",
                    "title": "CI pipeline failure",
                    "body": "github actions build failure in the log",
                    "labels": ["bug"],
                    "created_at": "2026-11-20T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )


def test_cycle_runs_offline_at_zero_cost(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--cycle"
    )
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["online"] is False
    cycle = payload["cycle"]
    assert cycle["feedback"]["accepted"] == 1
    assert len(cycle["builds"]) == 2
    assert len(cycle["packages"]) == 2
    assert len(cycle["pages"]) == 2
    assert len(cycle["releases"]) == 2
    assert cycle["notes_providers"] == ["deterministic", "deterministic"]
    assert cycle["allocation"]["money_allocation_usd"] == 0.0
    assert cycle["allocation"]["state"] == "BOOTSTRAP"
    assert all(package["sha256"] for package in cycle["packages"])
    for artifact in ("state", "build", "dist", "swarm"):
        assert not (isolated_root / artifact).exists(), artifact


def test_status_reports_invariants(
    isolated_root: Path,
    guard: InvariantGuard,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run", "--status")
    assert code == 0
    status = payload["status"]
    assert status["invariants_digest"] == guard.digest
    assert status["billing"]["hard_cap_usd"] == 0.0
    assert status["billing"]["paid_resources_enabled"] is False
    assert status["ledger"]["chain_valid"] is True
    assert status["ledger"]["cost_usd"] == 0.0
    assert not (isolated_root / "state").exists()


def test_default_invocation_reports_status(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run")
    assert code == 0
    assert "status" in payload
    assert "cycle" not in payload


def test_evolve_proposes_but_never_applies(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(capsys, "--root", str(isolated_root), "--dry-run", "--evolve")
    assert code == 0
    evolve = payload["evolve"]
    assert evolve["verified"] is True
    assert evolve["proposals"]
    assert len(evolve["proposals"]) <= 2
    for proposal in evolve["proposals"]:
        assert proposal["auto_apply"] is False
        assert proposal["status"] == "proposed"
    assert len(evolve["gates"]) == len(evolve["proposals"])
    assert all(gate["passed"] for gate in evolve["gates"])


def test_replicate_skips_when_swarm_disabled(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--replicate"
    )
    assert code == 0
    assert payload["replicate"]["status"] == "skipped"


def test_heartbeat_cli_records_alerts(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--heartbeat"
    )
    assert code == 0
    heartbeat = payload["heartbeat"]
    assert heartbeat["status"] == "stale"
    assert heartbeat["alerts_recorded"] == 2
    assert heartbeat["chain_valid"] is True
    assert heartbeat["rollup"]["active_count"] == 0


def test_offline_flag_beats_online(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys,
        "--root",
        str(isolated_root),
        "--dry-run",
        "--online",
        "--offline",
        "--status",
    )
    assert code == 0
    assert payload["online"] is False


def test_missing_config_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = orchestrator.main(["--root", str(tmp_path), "--dry-run", "--status"])
    captured = capsys.readouterr()
    assert code == 2
    error = json.loads(captured.err)
    assert error["error"] in {"ConfigError", "StateError"}


def test_prune_cli_keeps_young_assets_and_records_decisions(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--prune"
    )
    assert code == 0
    prune = payload["prune"]
    assert {item["asset_id"] for item in prune["decisions"]} == {
        "ci-failure-diagnoser",
        "openapi-schema-migrator",
    }
    assert all(item["action"] == "keep" for item in prune["decisions"])
    assert prune["paused"] == []
    assert prune["archive_manifests"] == []


def test_handoff_cli_generates_human_owned_packets(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--handoff-brief"
    )
    assert code == 0
    handoff = payload["handoff"]
    kinds = [packet["kind"] for packet in handoff["packets"]]
    assert kinds == [
        "reliability_proof",
        "benchmark_proof",
        "design_partner_outreach",
        "interview_script",
        "partnership_pitch",
        "payment_legal_checklist",
    ]
    assert handoff["files"] == []
    assert "payment_and_billing_setup" in handoff["human_owned_tasks"]
    assert "reliability_evidence" in handoff["system_owned_tasks"]
    for packet in handoff["packets"]:
        assert packet["human_actions"]


def test_cycle_includes_zero_cost_capital_and_interview_priority(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    insights = isolated_root / "interview.json"
    insights.write_text(
        json.dumps(
            {
                "insights": [
                    {
                        "insight_id": "int-1",
                        "niche_id": "ci-failure-diagnoser",
                        "role": "staff engineer",
                        "bottleneck": "release triage",
                        "budget_owner": "platform manager",
                        "recurring_need": "every deploy",
                        "quote": "we lose hours each release",
                        "captured_at": "2026-11-20T00:00:00Z",
                        "budget_signal_usd": 120.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    code, payload = _run(
        capsys,
        "--root",
        str(isolated_root),
        "--dry-run",
        "--cycle",
        "--interview-insights",
        "interview.json",
    )
    assert code == 0
    cycle = payload["cycle"]
    assert cycle["capital"]["spent_usd"] == 0.0
    assert cycle["capital"]["owner_vault_touched"] is False
    assert cycle["capital"]["available_usd"] == 0.0
    assert cycle["allocation"]["money_allocation_usd"] == 0.0
    top = cycle["feedback"]["top"][0]
    assert top["id"] == "int-1"
    assert top["priority"] > 20.0


def test_prune_and_handoff_persist_without_registry_collision(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--prune", "--handoff-brief"
    )
    assert code == 0
    assert payload["prune"]["decisions"]
    assert payload["handoff"]["files"]
    state = isolated_root / "state"
    assert (state / "pruning_registry.json").exists()
    assert (state / "pruning.json").exists()
    assert (state / "handoff" / "index.json").exists()

    code, payload = _run(capsys, "--root", str(isolated_root), "--status")
    assert code == 0
    status = payload["status"]
    assert status["ledger"]["chain_valid"] is True
    assert status["pruning"]["decisions"]
    assert len(status["handoff"]["packets"]) == 6


def _write_pain(root: Path, niche_id: str) -> str:
    token = fake_github_token()
    payload = {
        "findings": [
            {
                "finding_id": f"pain-{niche_id}-1",
                "niche_id": niche_id,
                "workflow_bottleneck": "contract-first API changes break consumers",
                "breaks_today": "a schema migration silently breaks client code",
                "estimated_cost_or_hours_lost": "9 engineering hours per release",
                "frequency": "every API release",
                "recurring_willingness_to_pay_signal": True,
                "notes_sanitized": (
                    f"contact owner at ops@example.com; token {token} was rotated"
                ),
                "captured_at": "2026-11-20T00:00:00Z",
            }
        ]
    }
    (root / "pain.json").write_text(json.dumps(payload), encoding="utf-8")
    return token


def test_ingest_pain_cli_sanitizes_and_persists(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _write_pain(isolated_root, "ci-failure-diagnoser")
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--ingest-pain", "pain.json"
    )
    assert code == 0
    assert payload["pain"]["ingested"] == 1
    assert not (isolated_root / "state").exists()

    code, payload = _run(
        capsys, "--root", str(isolated_root), "--ingest-pain", "pain.json"
    )
    assert code == 0
    pain = payload["pain"]
    assert pain["ingested"] == 1
    assert pain["verified_only"] is True
    top = pain["top_priority"]
    assert top["id"] == "pain-ci-failure-diagnoser-1"
    assert top["priority"] > 30.0
    assert "ci-failure-diagnoser" in top["matched_niches"]

    stored = (isolated_root / "state" / "pain_interviews.json").read_text(
        encoding="utf-8"
    )
    assert "ops@example.com" not in stored
    assert token not in stored
    assert "[REDACTED:EMAIL]" in stored
    assert "[REDACTED:GITHUB_TOKEN]" in stored

    code, payload = _run(capsys, "--root", str(isolated_root), "--status")
    assert code == 0
    assert payload["status"]["pain"]["ingested"] == 1


def test_persisted_pain_drives_cycle_focus(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    _write_pain(isolated_root, "openapi-schema-migrator")
    code, _ = _run(capsys, "--root", str(isolated_root), "--ingest-pain", "pain.json")
    assert code == 0

    code, payload = _run(capsys, "--root", str(isolated_root), "--cycle")
    assert code == 0
    cycle = payload["cycle"]
    assert cycle["scores"][0]["niche_id"] == "openapi-schema-migrator"
    assert cycle["scores"][0]["feedback_pressure"] == 1.0
    assert cycle["feedback"]["top"][0]["id"] == "pain-openapi-schema-migrator-1"
    assert cycle["capital"]["spent_usd"] == 0.0


def test_scan_channels_cli_dry_run(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--scan-channels"
    )
    assert code == 0
    channels = payload["channels"]
    assert {item["kind"] for item in channels["opportunities"]} == {
        "marketplace",
        "bounty",
        "paid_template",
        "custom_audit",
        "recurring_saas",
    }
    assert set(channels["top_by_channel"]) == {
        "marketplace",
        "bounty",
        "paid_template",
        "custom_audit",
        "recurring_saas",
    }
    assert channels["live_execution_allowed"] is False
    assert channels["automated_payouts"] is False
    assert channels["owner_vault_touched"] is False
    assert channels["real_funds_at_risk_usd"] == 0.0
    assert channels["hard_cap_usd"] == 0.0
    assert not (isolated_root / "state").exists()


def test_max_outcome_plan_cli_prepares_gated_handoffs(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_feedback(isolated_root)
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--max-outcome-plan"
    )
    assert code == 0
    result = payload["outcome_plan"]
    assert result["research_only"] is True
    assert result["live_execution_allowed"] is False
    assert result["real_funds_at_risk_usd"] == 0.0
    assert result["owner_vault_touched"] is False
    assert result["hard_cap_usd"] == 0.0
    assert result["files"] == []
    assert [packet["kind"] for packet in result["packets"]] == [
        "channel_marketplace",
        "channel_bounty",
        "channel_paid_template",
        "channel_custom_audit",
        "channel_recurring_saas",
        "capital_research_allocation",
    ]
    plan = result["plan"]
    assert plan["policy"]["research_only"] is True
    assert plan["policy"]["live_trading_enabled"] is False
    assert plan["policy"]["live_investing_enabled"] is False
    assert plan["policy"]["real_funds_at_risk_usd"] == 0.0
    assert plan["recommended_next_action"]["live_execution"] is False
    assert not (isolated_root / "state").exists()

    code, payload = _run(capsys, "--root", str(isolated_root), "--max-outcome-plan")
    assert code == 0
    result = payload["outcome_plan"]
    assert result["files"]
    state = isolated_root / "state"
    assert (state / "channels.json").exists()
    assert (state / "outcome_plan.json").exists()
    handoff = state / "handoff" / "channels"
    index = json.loads((handoff / "index.json").read_text(encoding="utf-8"))
    assert index["approval_required"] is True
    assert index["live_execution_allowed"] is False
    assert index["owner_vault_touched"] is False
    for packet in result["packets"]:
        assert (handoff / f"{packet['kind']}.md").exists()

    code, payload = _run(capsys, "--root", str(isolated_root), "--status")
    assert code == 0
    status = payload["status"]
    assert status["channels"]["opportunities"]
    assert status["outcome_plan"]["packets"]
    assert status["capital_research"]["research_only"] is True
    assert status["capital_research"]["live_trading_enabled"] is False
    assert status["capital_research"]["live_investing_enabled"] is False
    assert status["capital_research"]["real_funds_at_risk_usd"] == 0.0
    assert status["capital_research"]["owner_vault_touched"] is False


def test_discovery_briefs_cli_prepares_human_review(
    isolated_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload = _run(
        capsys, "--root", str(isolated_root), "--dry-run", "--discovery-briefs"
    )
    assert code == 0
    discovery = payload["discovery"]
    assert discovery["auto_send_enabled"] is False
    assert discovery["human_review_required"] is True
    assert discovery["briefs"]
    assert discovery["files"] == []
    for brief in discovery["briefs"]:
        assert brief["auto_send_enabled"] is False
        assert brief["questions"]
        assert brief["human_actions"]
    assert not (isolated_root / "state").exists()

    code, payload = _run(capsys, "--root", str(isolated_root), "--discovery-briefs")
    assert code == 0
    discovery = payload["discovery"]
    assert discovery["files"]
    output_dir = isolated_root / "state" / "handoff" / "discovery"
    assert (output_dir / "index.json").exists()
    assert (isolated_root / "state" / "discovery.json").exists()
    for brief in discovery["briefs"]:
        markdown = (output_dir / f"{brief['niche_id']}.md").read_text(encoding="utf-8")
        assert "Human review required" in markdown
        assert "Automated sending is disabled" in markdown
