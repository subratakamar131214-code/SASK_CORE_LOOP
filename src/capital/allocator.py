"""Disciplined capital allocation from settled revenue, never from OWNER_VAULT.

The allocator only spends human-confirmed ``SYSTEM_TREASURY`` revenue, only in
verified paid phases, and only on proven-ROI reliability, distribution, or
OCI-graduation work. Bootstrap and unverified phases spend exactly ``$0.00``.
Low-value clone proliferation is rejected by default.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.core.config import CapitalConfig
from src.core.errors import ConfigError, InvariantViolation
from src.core.types import JsonObject

SYSTEM_TREASURY = "SYSTEM_TREASURY"
OWNER_VAULT = "OWNER_VAULT"
PHASES = frozenset({"BOOTSTRAP", "VALIDATE", "SCALE", "MAINTAIN"})
_ZERO_SPEND_PHASES = frozenset({"BOOTSTRAP", "VALIDATE"})


@dataclass(frozen=True)
class InvestmentOption:
    option_id: str
    category: str
    expected_roi: float
    proven: bool
    recurring: bool
    requested_usd: float
    description: str

    def __post_init__(self) -> None:
        if not self.option_id.strip():
            raise ConfigError("investment option requires an option_id")
        if not self.category.strip():
            raise ConfigError(f"investment option {self.option_id} requires a category")
        if not self.description.strip():
            raise ConfigError(f"investment option {self.option_id} requires a description")
        if self.expected_roi < 0.0:
            raise ConfigError(f"investment option {self.option_id} ROI cannot be negative")
        if self.requested_usd < 0.0:
            raise ConfigError(f"investment option {self.option_id} request cannot be negative")


@dataclass(frozen=True)
class CapitalAllocationLine:
    option_id: str
    category: str
    amount_usd: float
    expected_roi: float
    proven: bool
    recurring: bool

    def to_json(self) -> JsonObject:
        return {
            "option_id": self.option_id,
            "category": self.category,
            "amount_usd": round(self.amount_usd, 6),
            "expected_roi": round(self.expected_roi, 4),
            "proven": self.proven,
            "recurring": self.recurring,
        }


@dataclass(frozen=True)
class Rejection:
    option_id: str
    reason: str

    def to_json(self) -> JsonObject:
        return {"option_id": self.option_id, "reason": self.reason}


@dataclass(frozen=True)
class CapitalDecision:
    phase: str
    source_account: str
    treasury_balance_usd: float
    settled_revenue_usd: float
    available_usd: float
    spent_usd: float
    allocations: tuple[CapitalAllocationLine, ...]
    rejected: tuple[Rejection, ...]
    reason: str
    owner_vault_touched: bool = False

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise ConfigError(f"unsupported allocation phase: {self.phase!r}")
        if self.owner_vault_touched:
            raise InvariantViolation("OWNER_VAULT must never be spent or reallocated")
        if self.spent_usd > self.available_usd + 1e-9:
            raise InvariantViolation("capital spend cannot exceed settled treasury revenue")

    def to_json(self) -> JsonObject:
        return {
            "phase": self.phase,
            "source_account": self.source_account,
            "treasury_balance_usd": round(self.treasury_balance_usd, 6),
            "settled_revenue_usd": round(self.settled_revenue_usd, 6),
            "available_usd": round(self.available_usd, 6),
            "spent_usd": round(self.spent_usd, 6),
            "allocations": [line.to_json() for line in self.allocations],
            "rejected": [item.to_json() for item in self.rejected],
            "reason": self.reason,
            "owner_vault_touched": self.owner_vault_touched,
        }


class CapitalAllocator:
    def __init__(self, config: CapitalConfig) -> None:
        self._config = config
        if config.owner_vault == config.system_treasury:
            raise ConfigError("OWNER_VAULT and SYSTEM_TREASURY must be distinct")
        if not 0.0 <= config.reinvestment_fraction <= 1.0:
            raise ConfigError("reinvestment_fraction must be between 0 and 1")

    def decide(
        self,
        *,
        phase: str,
        verified: bool,
        treasury_balance_usd: float,
        settled_revenue_usd: float,
        options: Sequence[InvestmentOption],
        source_account: str = SYSTEM_TREASURY,
    ) -> CapitalDecision:
        if source_account != self._config.system_treasury:
            raise InvariantViolation(
                f"only {self._config.system_treasury} can fund reinvestment"
            )
        treasury = _non_negative(treasury_balance_usd, "treasury_balance_usd")
        settled = _non_negative(settled_revenue_usd, "settled_revenue_usd")
        available = min(treasury, settled)
        if phase in _ZERO_SPEND_PHASES or not verified or available <= 0.0:
            reason = (
                "bootstrap/validate phases spend nothing"
                if phase in _ZERO_SPEND_PHASES
                else "no verified settled treasury revenue is available"
            )
            return CapitalDecision(
                phase=phase,
                source_account=source_account,
                treasury_balance_usd=treasury,
                settled_revenue_usd=settled,
                available_usd=available,
                spent_usd=0.0,
                allocations=(),
                rejected=tuple(
                    Rejection(option.option_id, "no settled revenue available")
                    for option in options
                ),
                reason=reason,
            )

        budget = available * self._config.reinvestment_fraction
        clone_budget = budget * self._config.max_clone_share
        rejected: list[Rejection] = []
        eligible: list[InvestmentOption] = []
        for option in options:
            rejection_reason = self._rejection_reason(option)
            if rejection_reason is not None:
                rejected.append(Rejection(option.option_id, rejection_reason))
            else:
                eligible.append(option)

        eligible.sort(key=lambda option: (-option.expected_roi, option.option_id))
        remaining = budget
        clone_spent = 0.0
        lines: list[CapitalAllocationLine] = []
        for option in eligible:
            share_limit = remaining
            if option.category == "clone":
                share_limit = min(share_limit, clone_budget - clone_spent)
            amount = min(option.requested_usd, share_limit)
            if amount <= 1e-9:
                rejected.append(
                    Rejection(option.option_id, "budget exhausted or clone share is zero")
                )
                continue
            remaining -= amount
            if option.category == "clone":
                clone_spent += amount
            lines.append(
                CapitalAllocationLine(
                    option_id=option.option_id,
                    category=option.category,
                    amount_usd=round(amount, 6),
                    expected_roi=option.expected_roi,
                    proven=option.proven,
                    recurring=option.recurring,
                )
            )
        spent = round(sum(line.amount_usd for line in lines), 6)
        reason = (
            "ROI-ranked reinvestment of settled treasury revenue"
            if lines
            else "no proven-ROI options met the allocation bar"
        )
        return CapitalDecision(
            phase=phase,
            source_account=source_account,
            treasury_balance_usd=treasury,
            settled_revenue_usd=settled,
            available_usd=available,
            spent_usd=spent,
            allocations=tuple(lines),
            rejected=tuple(rejected),
            reason=reason,
        )

    def _rejection_reason(self, option: InvestmentOption) -> str | None:
        if not option.proven:
            return "ROI is not proven by real pull signals"
        if option.category not in self._config.categories:
            if option.category != "clone":
                return "category is outside the compounding mandate"
            if self._config.max_clone_share <= 0.0:
                return "clone multiplication is disabled"
            if option.expected_roi < self._config.clone_roi_threshold:
                return "clone ROI is below the clone threshold"
        if option.expected_roi < self._config.min_roi_threshold:
            return "ROI is below the reinvestment threshold"
        return None


def _non_negative(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0.0:
        raise ConfigError(f"{name} must be a non-negative number")
    return float(value)
