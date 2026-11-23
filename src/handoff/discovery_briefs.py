"""Human-reviewed early-user discovery briefs.

Briefs are prepared from sensor-scored high-pain bottlenecks. They are never
sent by the system: ``outreach.auto_send_enabled`` is false by invariant, and
every packet requires a human to review and send one-to-one messages manually.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from src.core.config import NicheConfig, OutreachConfig
from src.core.errors import ConfigError
from src.core.io import ensure_dir, write_json_atomic, write_text_atomic
from src.core.redact import redact_text, sanitize_public_payload
from src.core.timeutil import iso_now
from src.core.types import JsonObject
from src.sensors.niche_scoring import NicheScore

BRIEF_KIND = "discovery_brief"


@dataclass(frozen=True)
class DiscoveryBrief:
    niche_id: str
    title: str
    audience: str
    focus_bottleneck: str
    sensor_evidence: str
    questions: tuple[str, ...]
    human_actions: tuple[str, ...]
    body: str
    auto_send_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.niche_id.strip():
            raise ConfigError("discovery brief requires a niche_id")
        if not self.questions:
            raise ConfigError("discovery brief requires diagnostic questions")
        if not self.human_actions:
            raise ConfigError("discovery brief requires human-owned actions")
        if self.auto_send_enabled:
            raise ConfigError("discovery briefs must never auto-send outreach")

    def to_json(self) -> JsonObject:
        return {
            "kind": BRIEF_KIND,
            "niche_id": self.niche_id,
            "title": self.title,
            "audience": self.audience,
            "focus_bottleneck": self.focus_bottleneck,
            "sensor_evidence": self.sensor_evidence,
            "questions": list(self.questions),
            "human_actions": list(self.human_actions),
            "auto_send_enabled": False,
            "human_review_required": True,
        }

    def to_markdown(self) -> str:
        questions = "\n".join(
            f"{index}. {question}" for index, question in enumerate(self.questions, start=1)
        )
        actions = "\n".join(f"- [ ] {action}" for action in self.human_actions)
        body = redact_text(self.body).text
        return (
            f"# {self.title}\n\n"
            "> **Human review required.** Automated sending is disabled. Send only "
            "individual, relevant messages; never bulk or unsolicited outreach.\n\n"
            f"**Audience:** {self.audience}\n\n"
            f"**Focus bottleneck:** {self.focus_bottleneck}\n\n"
            f"**Sensor evidence:** {self.sensor_evidence}\n\n"
            f"## Diagnostic questions\n\n{questions}\n\n"
            f"## Human-owned actions\n\n{actions}\n\n"
            f"## Notes\n\n{body}\n"
        )


class DiscoveryBriefBuilder:
    def __init__(self, outreach: OutreachConfig, *, max_briefs: int = 3) -> None:
        if outreach.auto_send_enabled:
            raise ConfigError("outreach.auto_send_enabled must remain false")
        self._outreach = outreach
        self._max_briefs = max(1, max_briefs)

    def build(
        self,
        scores: Sequence[NicheScore],
        niches: Sequence[NicheConfig],
        *,
        generated_at: str | None = None,
    ) -> tuple[DiscoveryBrief, ...]:
        stamp = generated_at or iso_now()
        by_id = {niche.id: niche for niche in niches if niche.active}
        ranked = sorted(scores, key=lambda score: (-score.total, score.niche_id))
        briefs: list[DiscoveryBrief] = []
        for score in ranked[: self._max_briefs]:
            niche = by_id.get(score.niche_id)
            if niche is None:
                continue
            briefs.append(self._brief(niche, score, stamp))
        return tuple(briefs)

    def _brief(
        self, niche: NicheConfig, score: NicheScore, stamp: str
    ) -> DiscoveryBrief:
        components = score.components
        evidence = (
            f"score={score.total:.3f}; feedback_pressure={score.feedback_pressure:.2f}; "
            f"urgency={components.get('urgency', 0.0):.2f}; "
            f"failure_cost={components.get('failure_cost', 0.0):.2f}; "
            f"recurrence={components.get('recurrence', 0.0):.2f}; "
            f"recurring_wtp={components.get('recurring_willingness_to_pay', 0.0):.2f}"
        )
        questions = (
            f"Walk me through {niche.workflow_bottleneck}. What breaks today, and what "
            "happens to the release or deploy when it does?",
            "How often did this happen in the last month, and does it cluster around "
            "specific releases, weeks, or customers?",
            "What did the last failure cost in downtime, rework, or engineering hours? "
            "Who absorbs that cost?",
            f"Who owns the budget for this problem ({niche.budget_owner}), and what "
            "approval process applies?",
            "What do you use today, and what would make you switch or renew a paid "
            "hosted or pro workflow?",
            "Would you pay recurringly for a reliable hosted or pro workflow that "
            "prevents this? What monthly price band feels obvious, and what "
            "reliability evidence would you need before paying?",
        )
        human_actions = (
            "Review this brief and confirm the bottleneck is accurate",
            "Send only to people you have a legitimate reason to contact",
            "Never send bulk, scraped, or unsolicited outreach",
            "Record only sanitized answers with the pain-interview importer",
        )
        body = (
            f"Prepared at {stamp}. Automated sending is disabled by invariant "
            f"(auto_send_enabled={str(self._outreach.auto_send_enabled).lower()}).\n\n"
            f"Niche: {niche.id}. Product: {niche.product_name}. "
            f"Bottleneck: {niche.workflow_bottleneck}. Budget owner: {niche.budget_owner}. "
            f"Reliability rationale: {niche.reliability_rationale}.\n\n"
            "These questions are diagnostic, not a sales script. Listen for concrete "
            "failure frequency, cost, budget ownership, and recurring willingness to "
            "pay. Do not promise uptime, revenue, or compliance outcomes."
        )
        return DiscoveryBrief(
            niche_id=niche.id,
            title=f"Discovery brief: {niche.title}",
            audience="Human owner conducting early-user conversations",
            focus_bottleneck=niche.workflow_bottleneck,
            sensor_evidence=evidence,
            questions=questions,
            human_actions=human_actions,
            body=body,
            auto_send_enabled=False,
        )


def save_discovery_briefs(
    briefs: Sequence[DiscoveryBrief],
    output_dir: Path,
    *,
    dry_run: bool = False,
) -> tuple[Path, ...]:
    index: JsonObject = {
        "generated_at": iso_now(),
        "auto_send_enabled": False,
        "human_review_required": True,
        "briefs": [brief.to_json() for brief in briefs],
    }
    sanitized, _ = sanitize_public_payload(index)
    if not isinstance(sanitized, dict):
        raise ConfigError("discovery brief index must remain an object")
    if dry_run:
        return ()
    ensure_dir(output_dir)
    written: list[Path] = []
    for brief in briefs:
        path = output_dir / f"{brief.niche_id}.md"
        write_text_atomic(path, brief.to_markdown())
        written.append(path)
    index_path = output_dir / "index.json"
    write_json_atomic(index_path, sanitized)
    written.append(index_path)
    return tuple(written)
