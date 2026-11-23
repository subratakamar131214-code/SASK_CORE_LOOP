"""Hash-chained ledger, revenue rules, and allocation state machine."""

from __future__ import annotations

import json

import pytest
from src.core.errors import ApprovalRequired, InvariantViolation, StateError
from src.ledger.allocation import (
    BOOTSTRAP,
    MAINTAIN,
    SCALE,
    VALIDATE,
    AllocationState,
    AllocationStateMachine,
)
from src.ledger.ledger import Ledger, init_ledger


def _ledger(runtime_paths, guard, **kwargs: object) -> Ledger:
    ledger = Ledger(runtime_paths.ledger_file, guard, **kwargs)
    ledger.ensure_genesis(guard.digest)
    return ledger


def test_genesis_and_chain(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    assert len(ledger.entries()) == 1
    genesis = ledger.entries()[0]
    assert genesis["kind"] == "init"
    assert genesis["seq"] == 1
    assert genesis["prev_hash"] == "GENESIS"
    ledger.validate_chain()


def test_records_cost_and_revenue_with_summary(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_cost(0.0, "local compute")
    ledger.record_revenue(
        0.0, source="simulated-checkout", simulated=True, evidence="load-test"
    )
    ledger.record_revenue(
        2.5,
        source="github-sponsors",
        simulated=False,
        evidence="payout statement",
        confirmed_by="maintainer-jordan",
    )
    ledger.validate_chain()
    summary = ledger.summary(strict=True)
    assert summary.entries == 4
    assert summary.cost_usd == 0.0
    assert summary.real_revenue_usd == 2.5
    assert summary.simulated_revenue_usd == 0.0
    assert summary.chain_valid is True
    assert summary.counts["revenue"] == 2


def test_real_revenue_requires_human_confirmation(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    with pytest.raises(ApprovalRequired, match="confirmed_by"):
        ledger.record_revenue(
            5.0, source="sponsor", simulated=False, evidence="receipt"
        )
    with pytest.raises(ApprovalRequired, match="bots"):
        ledger.record_revenue(
            5.0,
            source="sponsor",
            simulated=False,
            evidence="receipt",
            confirmed_by="github-actions[bot]",
        )
    ledger.record_revenue(
        0.0,
        source="preview",
        simulated=True,
        evidence="simulation only",
    )


def test_non_zero_cost_is_rejected(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    with pytest.raises(InvariantViolation, match="hard cap"):
        ledger.record_cost(0.01, "should never happen")
    with pytest.raises(InvariantViolation, match="hard cap"):
        ledger.append("cost", {"amount_usd": 1.0})

    with pytest.raises(StateError, match="numeric amount_usd"):
        ledger.append("cost", {"amount_usd": "free"})


def test_payloads_are_sanitized_before_persisting(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_alert("contact ops@example.com about the outage", component="test")
    stored = ledger.entries()[-1]["payload"]
    assert isinstance(stored, dict)
    assert "[REDACTED:EMAIL]" in stored["message"]
    assert "ops@example.com" not in stored["message"]


def test_tampering_breaks_validation(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_cost(0.0, "clean")
    lines = runtime_paths.ledger_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["payload"]["note"] = "edited outside the loop"
    lines[0] = json.dumps(first)
    runtime_paths.ledger_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(StateError, match="tampering"):
        ledger.validate_chain()
    assert ledger.summary().chain_valid is False
    with pytest.raises(StateError):
        ledger.summary(strict=True)


def test_sequence_gaps_are_rejected(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_cost(0.0, "clean")
    lines = runtime_paths.ledger_file.read_text(encoding="utf-8").splitlines()
    runtime_paths.ledger_file.write_text("\n".join(reversed(lines)) + "\n", encoding="utf-8")
    with pytest.raises(StateError, match="sequence gap"):
        ledger.validate_chain()


def test_dry_run_keeps_entries_in_memory(runtime_paths, guard) -> None:
    ledger = Ledger(runtime_paths.ledger_file, guard, dry_run=True)
    ledger.ensure_genesis(guard.digest)
    ledger.record_cost(0.0, "dry")
    assert len(ledger.entries()) == 2
    assert not runtime_paths.ledger_file.exists()
    ledger.validate_chain()


def test_init_ledger_helper(runtime_paths, guard) -> None:
    ledger = init_ledger(runtime_paths.ledger_file, guard)
    assert runtime_paths.ledger_file.exists()
    assert ledger.entries()[0]["payload"]["hard_cap_usd"] == 0.0


def test_allocation_state_transitions() -> None:
    machine = AllocationStateMachine()
    scores = {"a": 2.0, "b": 1.0}

    bootstrap = machine.decide(
        AllocationState.BOOTSTRAP,
        verified_cycles=0,
        cycle_success=True,
        real_revenue_usd=0.0,
        conversions=0,
        niche_scores=scores,
    )
    assert bootstrap.state is AllocationState.BOOTSTRAP
    assert bootstrap.money_allocation_usd == 0.0

    validate = machine.decide(
        AllocationState.BOOTSTRAP,
        verified_cycles=1,
        cycle_success=True,
        real_revenue_usd=0.0,
        conversions=0,
        niche_scores=scores,
    )
    assert validate.state is AllocationState.VALIDATE

    scale = machine.decide(
        AllocationState.VALIDATE,
        verified_cycles=3,
        cycle_success=True,
        real_revenue_usd=0.0,
        conversions=1,
        niche_scores=scores,
    )
    assert scale.state is AllocationState.SCALE

    maintain = machine.decide(
        AllocationState.SCALE,
        verified_cycles=3,
        cycle_success=True,
        real_revenue_usd=1.0,
        conversions=1,
        niche_scores=scores,
    )
    assert maintain.state is AllocationState.MAINTAIN

    rollback = machine.decide(
        AllocationState.SCALE,
        verified_cycles=3,
        cycle_success=False,
        real_revenue_usd=0.0,
        conversions=1,
        niche_scores=scores,
    )
    assert rollback.state is AllocationState.VALIDATE
    assert rollback.reason.startswith("last cycle failed")

    assert AllocationState.BOOTSTRAP.value == BOOTSTRAP
    assert (VALIDATE, SCALE, MAINTAIN) == ("VALIDATE", "SCALE", "MAINTAIN")


def test_effort_shares_are_normalized_and_money_is_zero() -> None:
    decision = AllocationStateMachine().decide(
        AllocationState.VALIDATE,
        verified_cycles=3,
        cycle_success=True,
        real_revenue_usd=0.0,
        conversions=1,
        niche_scores={"a": 2.0, "b": 1.0, "c": 1.0},
    )
    assert sum(decision.effort_shares.values()) == pytest.approx(1.0)
    assert decision.effort_shares["a"] == pytest.approx(0.5)
    assert decision.money_allocation_usd == 0.0
    payload = decision.to_json()
    assert payload["money_allocation_usd"] == 0.0
    assert payload["state"] == "SCALE"


def test_effort_shares_handle_zero_scores() -> None:
    decision = AllocationStateMachine().decide(
        AllocationState.VALIDATE,
        verified_cycles=3,
        cycle_success=True,
        real_revenue_usd=0.0,
        conversions=1,
        niche_scores={"a": 0.0, "b": 0.0},
    )
    assert decision.effort_shares == {"a": 0.5, "b": 0.5}
    assert sum(decision.effort_shares.values()) == pytest.approx(1.0)


def test_capital_entries_enforce_settled_revenue_and_owner_vault(
    runtime_paths, guard
) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_capital(
        {
            "spent_usd": 1.0,
            "settled_revenue_usd": 2.0,
            "source_account": "SYSTEM_TREASURY",
            "owner_vault_touched": False,
        }
    )
    with pytest.raises(InvariantViolation, match="exceed"):
        ledger.record_capital(
            {
                "spent_usd": 3.0,
                "settled_revenue_usd": 2.0,
                "source_account": "SYSTEM_TREASURY",
                "owner_vault_touched": False,
            }
        )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        ledger.record_capital(
            {
                "spent_usd": 1.0,
                "settled_revenue_usd": 2.0,
                "source_account": "SYSTEM_TREASURY",
                "owner_vault_touched": True,
            }
        )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        ledger.record_capital(
            {
                "spent_usd": 1.0,
                "settled_revenue_usd": 2.0,
                "source_account": "OWNER_VAULT",
                "owner_vault_touched": False,
            }
        )
    with pytest.raises(StateError, match="numeric"):
        ledger.record_capital(
            {
                "spent_usd": "free",
                "settled_revenue_usd": 2.0,
                "owner_vault_touched": False,
            }
        )


def test_prune_entries_never_touch_owner_vault(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_pruning(
        {"decisions": [{"asset_id": "openapi-schema-migrator", "action": "pause"}]}
    )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        ledger.record_pruning(
            {"decisions": [{"asset_id": "OWNER_VAULT", "action": "sunset"}]}
        )
    with pytest.raises(StateError, match="decisions list"):
        ledger.record_pruning({"paused": []})


def test_handoff_entries_require_packets(runtime_paths, guard) -> None:
    ledger = _ledger(runtime_paths, guard)
    ledger.record_handoff({"packets": ["reliability_proof"], "count": 1})
    with pytest.raises(StateError, match="packets"):
        ledger.record_handoff({"packets": []})
    ledger.validate_chain()
