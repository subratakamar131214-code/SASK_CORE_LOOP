"""ROI-weighted compounding from settled treasury revenue only."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from src.capital.allocator import (
    OWNER_VAULT,
    SYSTEM_TREASURY,
    CapitalAllocationLine,
    CapitalAllocator,
    CapitalDecision,
    InvestmentOption,
)
from src.core.errors import ConfigError, InvariantViolation
from src.ledger.ledger import Ledger
from src.orchestrator import _remaining_treasury_usd


def _option(
    option_id: str = "reliability-proof",
    *,
    category: str = "reliability",
    roi: float = 2.0,
    proven: bool = True,
    recurring: bool = True,
    requested: float = 10.0,
) -> InvestmentOption:
    return InvestmentOption(
        option_id=option_id,
        category=category,
        expected_roi=roi,
        proven=proven,
        recurring=recurring,
        requested_usd=requested,
        description=f"{option_id} allocation candidate",
    )


def test_bootstrap_and_validate_spend_nothing(config) -> None:
    allocator = CapitalAllocator(config.capital)
    for phase in ("BOOTSTRAP", "VALIDATE"):
        decision = allocator.decide(
            phase=phase,
            verified=True,
            treasury_balance_usd=100.0,
            settled_revenue_usd=50.0,
            options=[_option()],
        )
        assert decision.spent_usd == 0.0
        assert decision.allocations == ()
        assert len(decision.rejected) == 1
        assert decision.owner_vault_touched is False


def test_unverified_or_unsettled_revenue_spends_nothing(config) -> None:
    allocator = CapitalAllocator(config.capital)
    unverified = allocator.decide(
        phase="SCALE",
        verified=False,
        treasury_balance_usd=100.0,
        settled_revenue_usd=50.0,
        options=[_option()],
    )
    assert unverified.spent_usd == 0.0
    unsettled = allocator.decide(
        phase="SCALE",
        verified=True,
        treasury_balance_usd=100.0,
        settled_revenue_usd=0.0,
        options=[_option()],
    )
    assert unsettled.spent_usd == 0.0
    assert unsettled.available_usd == 0.0


def test_only_owner_vault_cannot_fund_reinvestment(config) -> None:
    allocator = CapitalAllocator(config.capital)
    with pytest.raises(InvariantViolation, match="only SYSTEM_TREASURY"):
        allocator.decide(
            phase="SCALE",
            verified=True,
            treasury_balance_usd=100.0,
            settled_revenue_usd=50.0,
            options=[_option()],
            source_account=OWNER_VAULT,
        )


def test_spend_is_bounded_by_settled_revenue_and_fraction(config) -> None:
    allocator = CapitalAllocator(config.capital)
    decision = allocator.decide(
        phase="SCALE",
        verified=True,
        treasury_balance_usd=1000.0,
        settled_revenue_usd=40.0,
        options=[_option(requested=500.0)],
    )
    assert decision.available_usd == 40.0
    assert decision.spent_usd == pytest.approx(
        40.0 * config.capital.reinvestment_fraction
    )
    assert decision.spent_usd <= decision.settled_revenue_usd
    assert decision.allocations[0].amount_usd == pytest.approx(decision.spent_usd)


def test_options_are_ranked_by_proven_roi(config) -> None:
    allocator = CapitalAllocator(config.capital)
    decision = allocator.decide(
        phase="SCALE",
        verified=True,
        treasury_balance_usd=100.0,
        settled_revenue_usd=40.0,
        options=[
            _option("reliability", roi=2.0, requested=10.0),
            _option("distribution", category="distribution", roi=3.0, requested=10.0),
        ],
    )
    assert [line.option_id for line in decision.allocations] == [
        "distribution",
        "reliability",
    ]
    assert decision.spent_usd <= decision.settled_revenue_usd


def test_unproven_low_roi_and_off_mandate_work_is_rejected(config) -> None:
    allocator = CapitalAllocator(config.capital)
    decision = allocator.decide(
        phase="SCALE",
        verified=True,
        treasury_balance_usd=100.0,
        settled_revenue_usd=100.0,
        options=[
            _option("unproven", proven=False),
            _option("low-roi", roi=config.capital.min_roi_threshold - 0.1),
            _option("marketing", category="marketing"),
        ],
    )
    assert decision.spent_usd == 0.0
    reasons = {item.option_id: item.reason for item in decision.rejected}
    assert "not proven" in reasons["unproven"]
    assert "below" in reasons["low-roi"]
    assert "mandate" in reasons["marketing"]


def test_low_value_clones_are_rejected_by_default(config) -> None:
    allocator = CapitalAllocator(config.capital)
    decision = allocator.decide(
        phase="SCALE",
        verified=True,
        treasury_balance_usd=100.0,
        settled_revenue_usd=100.0,
        options=[
            _option(
                "clone-cheap",
                category="clone",
                roi=config.capital.clone_roi_threshold + 5.0,
            )
        ],
    )
    assert decision.spent_usd == 0.0
    assert decision.rejected[0].reason == "clone multiplication is disabled"


def test_negative_or_unsupported_inputs_are_rejected(config) -> None:
    allocator = CapitalAllocator(config.capital)
    with pytest.raises(ConfigError, match="treasury_balance_usd"):
        allocator.decide(
            phase="SCALE",
            verified=True,
            treasury_balance_usd=-1.0,
            settled_revenue_usd=0.0,
            options=(),
        )
    with pytest.raises(ConfigError, match="unsupported allocation phase"):
        allocator.decide(
            phase="EXPAND",
            verified=True,
            treasury_balance_usd=0.0,
            settled_revenue_usd=0.0,
            options=(),
        )


def test_decision_cannot_exceed_available_or_touch_owner_vault() -> None:
    line = CapitalAllocationLine(
        option_id="reliability",
        category="reliability",
        amount_usd=5.0,
        expected_roi=2.0,
        proven=True,
        recurring=True,
    )
    with pytest.raises(InvariantViolation, match="exceed"):
        CapitalDecision(
            phase="SCALE",
            source_account=SYSTEM_TREASURY,
            treasury_balance_usd=10.0,
            settled_revenue_usd=4.0,
            available_usd=4.0,
            spent_usd=5.0,
            allocations=(line,),
            rejected=(),
            reason="test",
        )
    with pytest.raises(InvariantViolation, match="OWNER_VAULT"):
        CapitalDecision(
            phase="SCALE",
            source_account=SYSTEM_TREASURY,
            treasury_balance_usd=10.0,
            settled_revenue_usd=10.0,
            available_usd=10.0,
            spent_usd=0.0,
            allocations=(),
            rejected=(),
            reason="test",
            owner_vault_touched=True,
        )


def test_remaining_treasury_subtracts_prior_capital_spend(
    config, runtime_paths, guard
) -> None:
    ledger = Ledger(runtime_paths.ledger_file, guard)
    ledger.ensure_genesis(guard.digest)
    ledger.record_revenue(
        40.0,
        source="sponsor",
        simulated=False,
        evidence="payout statement",
        confirmed_by="maintainer-jordan",
    )
    ledger.record_capital(
        {
            "spent_usd": 15.0,
            "settled_revenue_usd": 40.0,
            "source_account": SYSTEM_TREASURY,
            "owner_vault_touched": False,
        }
    )
    ctx = SimpleNamespace(ledger=ledger, config=config)
    assert _remaining_treasury_usd(ctx, 40.0) == pytest.approx(25.0)
