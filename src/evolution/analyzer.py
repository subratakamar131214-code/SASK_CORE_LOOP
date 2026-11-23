"""Deterministic analysis of loop health and niche signals."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.core.types import JsonObject
from src.ledger.ledger import LedgerSummary
from src.ops.heartbeat import HeartbeatCheck
from src.sensors.feedback import FeedbackItem
from src.sensors.niche_scoring import NicheScore

_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


@dataclass(frozen=True)
class EvolutionObservation:
    code: str
    severity: str
    summary: str
    evidence: tuple[str, ...] = ()

    def to_json(self) -> JsonObject:
        return {
            "code": self.code,
            "severity": self.severity,
            "summary": self.summary,
            "evidence": list(self.evidence),
        }


class EvolutionAnalyzer:
    def analyze(
        self,
        *,
        ledger: LedgerSummary,
        feedback: Sequence[FeedbackItem],
        scores: Sequence[NicheScore],
        heartbeat: HeartbeatCheck | None = None,
        conversions: int = 0,
    ) -> list[EvolutionObservation]:
        observations: list[EvolutionObservation] = []
        if not ledger.chain_valid:
            observations.append(
                EvolutionObservation(
                    code="chain-invalid",
                    severity="critical",
                    summary="Ledger hash chain failed validation.",
                    evidence=("state/ledger.jsonl",),
                )
            )
        if ledger.cost_usd != 0.0:
            observations.append(
                EvolutionObservation(
                    code="nonzero-cost",
                    severity="critical",
                    summary=(
                        "Ledger recorded a non-zero cost; the zero-billing invariant is at risk."
                    ),
                    evidence=(f"cost_usd={ledger.cost_usd:.6f}",),
                )
            )
        if heartbeat is not None and not heartbeat.healthy:
            observations.append(
                EvolutionObservation(
                    code="stale-heartbeat",
                    severity="critical" if not heartbeat.chain_valid else "warning",
                    summary="One or more required heartbeats or workflows are stale.",
                    evidence=heartbeat.stale[:10],
                )
            )
        if ledger.counts.get("cycle", 0) == 0:
            observations.append(
                EvolutionObservation(
                    code="no-cycle",
                    severity="warning",
                    summary="No revenue cycle has been recorded yet.",
                )
            )
        matched_ids = {
            item_id for item in feedback for item_id in item.matched_niches
        }
        for score in sorted(scores, key=lambda item: (-item.total, item.niche_id)):
            if score.feedback_pressure >= 0.6:
                observations.append(
                    EvolutionObservation(
                        code="feedback-pressure",
                        severity="warning",
                        summary=f"Niche {score.niche_id} has high-priority feedback pressure.",
                        evidence=(
                            f"pressure={score.feedback_pressure:.2f}",
                            *score.evidence_ids[:5],
                        ),
                    )
                )
        unrouted = [item for item in feedback if not item.matched_niches]
        if unrouted:
            observations.append(
                EvolutionObservation(
                    code="unrouted-feedback",
                    severity="warning",
                    summary="Some sanitized feedback does not match any active niche.",
                    evidence=tuple(item.id for item in unrouted[:8]),
                )
            )
        if not feedback:
            observations.append(
                EvolutionObservation(
                    code="no-feedback",
                    severity="info",
                    summary=(
                        "No external feedback is recorded; validate demand with a "
                        "zero-cost release."
                    ),
                )
            )
        if conversions == 0:
            observations.append(
                EvolutionObservation(
                    code="zero-conversion",
                    severity="info",
                    summary=(
                        "No conversion proxy is recorded; distribution needs a clearer "
                        "call to action."
                    ),
                )
            )
        if ledger.real_revenue_usd > 0.0:
            observations.append(
                EvolutionObservation(
                    code="real-revenue",
                    severity="info",
                    summary=(
                        "Human-confirmed revenue exists; prioritize maintenance and reliability."
                    ),
                )
            )
        if not matched_ids and feedback:
            observations.append(
                EvolutionObservation(
                    code="keyword-coverage",
                    severity="warning",
                    summary="Feedback exists but no active niche keywords match it.",
                )
            )
        observations.sort(
            key=lambda item: (
                _SEVERITY_ORDER.get(item.severity, 9),
                item.code,
                item.evidence[:1],
            )
        )
        return observations
