"""Capital/effort allocation state machine. Monetary allocation is always zero."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from src.core.types import JsonObject

BOOTSTRAP = "BOOTSTRAP"
VALIDATE = "VALIDATE"
SCALE = "SCALE"
MAINTAIN = "MAINTAIN"


class AllocationState(StrEnum):
    BOOTSTRAP = BOOTSTRAP
    VALIDATE = VALIDATE
    SCALE = SCALE
    MAINTAIN = MAINTAIN


@dataclass(frozen=True)
class AllocationDecision:
    state: AllocationState
    previous_state: AllocationState
    reason: str
    effort_shares: Mapping[str, float]
    money_allocation_usd: float = 0.0

    def to_json(self) -> JsonObject:
        return {
            "state": self.state.value,
            "previous_state": self.previous_state.value,
            "reason": self.reason,
            "effort_shares": dict(sorted(self.effort_shares.items())),
            "money_allocation_usd": 0.0,
        }


class AllocationStateMachine:
    def decide(
        self,
        current: AllocationState,
        *,
        verified_cycles: int,
        cycle_success: bool,
        real_revenue_usd: float,
        conversions: int,
        niche_scores: Mapping[str, float],
    ) -> AllocationDecision:
        next_state = current
        reason = "hold"
        if not cycle_success:
            reason = "last cycle failed; remain conservative"
            if current is AllocationState.SCALE:
                next_state = AllocationState.VALIDATE
        elif current is AllocationState.BOOTSTRAP and verified_cycles >= 1:
            next_state = AllocationState.VALIDATE
            reason = "first verified cycle completed; begin validation"
        elif current is AllocationState.VALIDATE and verified_cycles >= 3 and conversions >= 1:
            next_state = AllocationState.SCALE
            reason = "repeat verification and first recorded conversion proxy"
        elif current is AllocationState.SCALE and real_revenue_usd > 0.0:
            next_state = AllocationState.MAINTAIN
            reason = "human-confirmed real revenue recorded; prioritize maintenance"
        elif current is AllocationState.SCALE:
            reason = "scaling effort within free tiers"

        active_scores = {niche: max(0.0, score) for niche, score in niche_scores.items()}
        total = sum(active_scores.values())
        if total <= 0.0:
            shares = {niche: 1.0 / len(active_scores) for niche in active_scores}
        else:
            shares = {niche: score / total for niche, score in active_scores.items()}
        return AllocationDecision(
            state=next_state,
            previous_state=current,
            reason=reason,
            effort_shares=shares,
            money_allocation_usd=0.0,
        )
