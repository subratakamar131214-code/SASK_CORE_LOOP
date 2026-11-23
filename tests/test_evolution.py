"""Tests for evolution observations, proposal generation, and the review gate."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from src.core.config import SystemConfig
from src.core.invariants import InvariantGuard
from src.evolution.analyzer import EvolutionAnalyzer, EvolutionObservation
from src.evolution.gate import CommandOutcome, ProposalGate
from src.evolution.proposer import EvolutionProposer, Proposal, ProposalChange
from src.ledger.ledger import LedgerSummary
from src.ops.heartbeat import HeartbeatCheck
from src.sensors.niche_scoring import NicheScore

from helpers import make_feedback_item, unquoted_assignment


def _summary(
    *,
    chain_valid: bool = True,
    cost: float = 0.0,
    real: float = 0.0,
    cycles: int = 1,
) -> LedgerSummary:
    return LedgerSummary(
        entries=1 + cycles,
        real_revenue_usd=real,
        simulated_revenue_usd=0.0,
        cost_usd=cost,
        counts={"cycle": cycles},
        last_ts="2026-11-20T00:00:00Z",
        chain_valid=chain_valid,
    )


def _proposal(
    *,
    proposal_id: str = "prop-0123456789ab",
    target_paths: tuple[str, ...] = ("tests/test_feedback_regressions.py",),
    changes: tuple[ProposalChange, ...] | None = None,
    status: str = "proposed",
    diff: str = "Add a regression test for the reported failure.",
) -> Proposal:
    if changes is None:
        changes = tuple(
            ProposalChange(path=path, description="test change", diff=diff)
            for path in target_paths
        )
    return Proposal(
        proposal_id=proposal_id,
        title="Add feedback regression coverage",
        rationale="Convert sanitized feedback into a non-regressing test.",
        risk="low",
        target_paths=target_paths,
        changes=changes,
        observation_codes=("no-feedback",),
        suggested_verification=("python -m pytest -q",),
        status=status,
        created_at="2026-11-20T00:00:00Z",
    )


def _observation(
    code: str, severity: str = "warning", evidence: tuple[str, ...] = ()
) -> EvolutionObservation:
    return EvolutionObservation(code=code, severity=severity, summary=code, evidence=evidence)


def test_analyzer_orders_by_severity() -> None:
    observations = EvolutionAnalyzer().analyze(
        ledger=_summary(chain_valid=False, cycles=0),
        feedback=[],
        scores=[],
        heartbeat=HeartbeatCheck(
            stale=("cycle heartbeat is stale",), chain_valid=False, last={}
        ),
    )
    codes = [item.code for item in observations]
    assert codes[0] == "chain-invalid"
    assert "stale-heartbeat" in codes
    assert "no-cycle" in codes
    assert "no-feedback" in codes
    assert "zero-conversion" in codes
    severities = [item.severity for item in observations]
    order = {"critical": 0, "warning": 1, "info": 2}
    assert [order[item] for item in severities] == sorted(order[item] for item in severities)


def test_analyzer_detects_cost_and_revenue() -> None:
    observations = EvolutionAnalyzer().analyze(
        ledger=_summary(cost=0.25, real=12.0),
        feedback=[],
        scores=[],
        conversions=3,
    )
    codes = {item.code for item in observations}
    assert {"nonzero-cost", "real-revenue"} <= codes


def test_analyzer_detects_feedback_pressure_and_unrouted_items() -> None:
    score = NicheScore(
        niche_id="ci-failure-diagnoser",
        total=0.8,
        feedback_pressure=0.9,
        evidence_ids=("gh-1",),
    )
    routed = make_feedback_item()
    unrouted = make_feedback_item("gh-2", matched_niches=())
    observations = EvolutionAnalyzer().analyze(
        ledger=_summary(),
        feedback=[routed, unrouted],
        scores=[score],
        conversions=1,
    )
    codes = {item.code for item in observations}
    assert "feedback-pressure" in codes
    assert "unrouted-feedback" in codes
    pressure = next(item for item in observations if item.code == "feedback-pressure")
    assert "gh-1" in pressure.evidence


def test_analyzer_emits_keyword_coverage_when_nothing_matches() -> None:
    observations = EvolutionAnalyzer().analyze(
        ledger=_summary(),
        feedback=[make_feedback_item(matched_niches=())],
        scores=[],
        conversions=1,
    )
    codes = {item.code for item in observations}
    assert "keyword-coverage" in codes
    assert "no-feedback" not in codes


def test_proposer_respects_proposal_cap_and_marks_review_only(
    config: SystemConfig,
) -> None:
    proposer = EvolutionProposer(config.evolution, config.niches)
    observations = [
        _observation("no-feedback", "info"),
        _observation("zero-conversion", "info"),
        _observation("feedback-pressure", "warning"),
    ]
    proposals = proposer.propose(observations, [make_feedback_item()])
    assert len(proposals) == config.evolution.max_proposals_per_cycle
    assert len({proposal.proposal_id for proposal in proposals}) == len(proposals)
    for proposal in proposals:
        payload = proposal.to_json()
        assert payload["auto_apply"] is False
        assert proposal.status == "proposed"
        assert proposal.target_paths
        assert proposal.suggested_verification == tuple(
            " ".join(command) for command in config.evolution.verification_commands
        )
        for path in proposal.target_paths:
            assert not path.startswith(("state/", "build/", "dist/", "swarm/"))
            assert path != "config/invariants.yaml"


def test_proposer_saves_sanitized_files(
    config: SystemConfig, tmp_path: Path
) -> None:
    proposer = EvolutionProposer(config.evolution, config.niches)
    proposals = proposer.propose(
        [_observation("no-feedback", "info"), _observation("zero-conversion", "info")]
    )
    written = proposer.save(proposals, tmp_path / "proposals")
    assert len(written) == len(proposals)
    for path, proposal in zip(written, proposals, strict=True):
        assert path.name == f"{proposal.proposal_id}.json"
        assert path.is_file()
    assert any("call to action" in proposal.title for proposal in proposals)


@pytest.fixture
def gate(config: SystemConfig, guard: InvariantGuard) -> ProposalGate:
    return ProposalGate(config.evolution, guard)


def test_gate_accepts_clean_reviewed_proposal(gate: ProposalGate, tmp_path: Path) -> None:
    result = gate.evaluate(_proposal(), root=tmp_path)
    assert result.passed is True
    assert result.reasons == ()
    assert result.commands == ()


def test_gate_rejects_protected_and_forbidden_paths(
    gate: ProposalGate, tmp_path: Path
) -> None:
    protected = gate.evaluate(
        _proposal(target_paths=("config/invariants.yaml",)), root=tmp_path
    )
    assert protected.passed is False
    assert any("protected" in reason for reason in protected.reasons)

    forbidden = gate.evaluate(
        _proposal(target_paths=("state/ledger.jsonl",)), root=tmp_path
    )
    assert forbidden.passed is False
    assert any("not reviewable" in reason for reason in forbidden.reasons)


def test_gate_rejects_secret_like_content(gate: ProposalGate, tmp_path: Path) -> None:
    proposal = _proposal(diff=f"Document the unsafe sample: {unquoted_assignment()}")
    result = gate.evaluate(proposal, root=tmp_path)
    assert result.passed is False
    assert any("secret-like" in reason for reason in result.reasons)


def test_gate_rejects_non_reviewable_proposals(
    gate: ProposalGate, tmp_path: Path
) -> None:
    applied = gate.evaluate(_proposal(status="applied"), root=tmp_path)
    assert applied.passed is False
    assert any("not reviewable" in reason for reason in applied.reasons)

    duplicate = gate.evaluate(
        _proposal(target_paths=("tests/a.py", "tests/a.py")), root=tmp_path
    )
    assert any("unique" in reason for reason in duplicate.reasons)

    empty = gate.evaluate(_proposal(target_paths=(), changes=()), root=tmp_path)
    assert any("at least one change" in reason for reason in empty.reasons)


def test_gate_rejects_auto_apply_configuration(
    config: SystemConfig, guard: InvariantGuard, tmp_path: Path
) -> None:
    gate = ProposalGate(replace(config.evolution, auto_apply=True), guard)
    result = gate.evaluate(_proposal(), root=tmp_path)
    assert result.passed is False
    assert "auto_apply must remain false" in result.reasons


def test_gate_runs_verification_commands(
    config: SystemConfig, guard: InvariantGuard, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def passing(command: tuple[str, ...], root: Path) -> CommandOutcome:
        calls.append(command)
        return CommandOutcome(command=command, returncode=0, output="ok")

    gate = ProposalGate(config.evolution, guard, runner=passing)
    result = gate.evaluate(_proposal(), root=tmp_path, run_commands=True)
    assert result.passed is True
    assert len(calls) == len(config.evolution.verification_commands)
    assert len(result.commands) == len(calls)


def test_gate_fails_closed_on_command_failure(
    config: SystemConfig, guard: InvariantGuard, tmp_path: Path
) -> None:
    calls: list[tuple[str, ...]] = []

    def failing(command: tuple[str, ...], root: Path) -> CommandOutcome:
        calls.append(command)
        return CommandOutcome(command=command, returncode=2, output="boom")

    gate = ProposalGate(config.evolution, guard, runner=failing)
    result = gate.evaluate(_proposal(), root=tmp_path, run_commands=True)
    assert result.passed is False
    assert len(calls) == 1
    assert any("verification command failed" in reason for reason in result.reasons)
