"""SASK_CORE_LOOP orchestrator: zero-billing revenue cycle, evolution, swarm, heartbeat."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO, cast

from src.capital.allocator import (
    SYSTEM_TREASURY,
    CapitalAllocator,
    CapitalDecision,
    InvestmentOption,
)
from src.compliance.policy import PolicyEngine
from src.core.config import SystemConfig, validate_system_config
from src.core.errors import ApprovalRequired, SASKError, StateError
from src.core.invariants import InvariantGuard
from src.core.io import ensure_dir, write_json_atomic
from src.core.paths import RepoPaths
from src.core.redact import redact_text, sanitize_public_payload
from src.core.state import StateStore
from src.core.timeutil import hours_since, iso_now, utc_now
from src.core.types import JsonObject, JsonValue
from src.distribution.funnel import FunnelSummary, FunnelTelemetry
from src.distribution.landing import LandingPage, LandingPageBuilder
from src.distribution.packager import PackageArtifact, PackageBuilder
from src.evolution.analyzer import EvolutionAnalyzer
from src.evolution.gate import GateResult, ProposalGate
from src.evolution.proposer import EvolutionProposer
from src.evolution.pruner import (
    ACTION_ARCHIVE,
    ACTION_PAUSE,
    ACTION_SUNSET,
    PruneDecision,
    PruneSignals,
    PruningEngine,
    PruningRegistry,
)
from src.factory.builder import BuildResult, ProductBuilder
from src.handoff.channel_handoffs import (
    ChannelHandoffBuilder,
    save_channel_handoffs,
)
from src.handoff.discovery_briefs import (
    DiscoveryBriefBuilder,
    save_discovery_briefs,
)
from src.handoff.packets import (
    HandoffBuilder,
    insight_documents,
    load_interview_insights,
    save_packets,
)
from src.inference.budget import DailyBudget
from src.inference.cache import ResponseCache
from src.inference.router import InferenceRouter
from src.inference.types import Completion, Message
from src.ledger.allocation import (
    BOOTSTRAP,
    AllocationDecision,
    AllocationState,
    AllocationStateMachine,
)
from src.ledger.ledger import Ledger, LedgerSummary
from src.ops.gh import GitHubClient
from src.ops.heartbeat import HeartbeatCheck, HeartbeatMonitor
from src.ops.release import ReleaseManager, ReleaseManifest
from src.sensors.channel_sensors import (
    ChannelOpportunity,
    ChannelSensor,
    top_by_channel,
)
from src.sensors.feedback import (
    FeedbackIngestor,
    FeedbackItem,
    IngestReport,
    load_feedback_documents,
)
from src.sensors.niche_scoring import NicheScore, NicheScorer
from src.sensors.opportunity import OpportunityScanner, OpportunitySignal
from src.sensors.outcome_simulator import OutcomeSimulator
from src.sensors.pain_interviews import (
    PainFinding,
    load_pain_findings,
    pain_feedback_documents,
    save_pain_findings,
)
from src.sensors.sanitizer import sanitize_feedback_text
from src.swarm.aggregator import SwarmAggregator, SwarmRollup
from src.swarm.niche_lock import NicheLock
from src.swarm.spawner import SwarmSpawner, load_approval

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
_PRODUCT_VERSION = "0.1.0"
_MAX_PRODUCTS_PER_CYCLE = 2
_BUILD_VERSION_PATTERN = r"^\d+\.\d+\.\d+$"


@dataclass
class Context:
    root: Path
    runtime_root: Path
    paths: RepoPaths
    guard: InvariantGuard
    config: SystemConfig
    ledger: Ledger
    store: StateStore
    dry_run: bool
    online: bool
    interview_path: Path | None = None
    tempdir: tempfile.TemporaryDirectory[str] | None = None

    def cleanup(self) -> None:
        if self.tempdir is not None:
            self.tempdir.cleanup()


@dataclass
class CycleResult:
    report: IngestReport
    scores: list[NicheScore]
    opportunities: list[OpportunitySignal]
    allocation: AllocationDecision
    capital: JsonObject | None = None
    builds: list[BuildResult] = field(default_factory=list)
    packages: list[PackageArtifact] = field(default_factory=list)
    pages: list[LandingPage] = field(default_factory=list)
    releases: list[ReleaseManifest] = field(default_factory=list)
    funnel: FunnelSummary | None = None
    notes_providers: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    within_budget: bool = True

    def to_json(self) -> JsonObject:
        return {
            "feedback": self.report.to_json(),
            "scores": [score.to_json() for score in self.scores],
            "opportunities": [signal.to_json() for signal in self.opportunities],
            "allocation": self.allocation.to_json(),
            "capital": self.capital,
            "builds": [build.to_json() for build in self.builds],
            "packages": [package.to_json() for package in self.packages],
            "pages": [page.to_json() for page in self.pages],
            "releases": [release.to_json() for release in self.releases],
            "funnel": self.funnel.to_json() if self.funnel else None,
            "notes_providers": list(self.notes_providers),
            "duration_seconds": round(self.duration_seconds, 3),
            "within_cycle_budget": self.within_budget,
        }


def build_context(args: argparse.Namespace) -> Context:
    root = Path(args.root).expanduser().resolve()
    guard = InvariantGuard.load(root / "config" / "invariants.yaml", root=root)
    config = SystemConfig.load(root / "config" / "system.yaml")
    validate_system_config(config, guard)
    tempdir: tempfile.TemporaryDirectory[str] | None = None
    if args.dry_run:
        tempdir = tempfile.TemporaryDirectory(prefix="sask-dry-run-")
        runtime_root = Path(tempdir.name)
    else:
        runtime_root = root
    paths = RepoPaths.load(root, runtime_root=runtime_root)
    paths.ensure_runtime_dirs()
    ledger = Ledger(paths.ledger_file, guard, dry_run=bool(args.dry_run))
    ledger.ensure_genesis(guard.digest)
    store = StateStore(paths.state_dir, dry_run=bool(args.dry_run))
    env_online = os.environ.get("SASK_ALLOW_NETWORK", "").strip() == "1"
    online = (bool(args.online) or env_online) and not bool(args.offline)
    interview_path: Path | None = None
    raw_interview = getattr(args, "interview_insights", None)
    if raw_interview:
        candidate = Path(raw_interview).expanduser()
        interview_path = candidate if candidate.is_absolute() else root / candidate
    return Context(
        root=root,
        runtime_root=runtime_root,
        paths=paths,
        guard=guard,
        config=config,
        ledger=ledger,
        store=store,
        dry_run=bool(args.dry_run),
        online=online,
        interview_path=interview_path,
        tempdir=tempdir,
    )


def _interview_documents(ctx: Context) -> list[JsonValue]:
    candidates: list[Path] = []
    if ctx.interview_path is not None:
        candidates.append(ctx.interview_path)
    candidates.append(ctx.paths.state_dir / "handoff" / "interview_insights.json")
    candidates.append(
        ctx.root / ctx.config.handoff.output_dir / "interview_insights.json"
    )
    seen: set[Path] = set()
    documents: list[JsonValue] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.exists():
            continue
        documents.extend(
            cast(list[JsonValue], insight_documents(load_interview_insights(path)))
        )
    return documents


def _pain_documents(ctx: Context) -> list[JsonValue]:
    path = ctx.paths.state_dir / "pain_interviews.json"
    if not path.exists():
        return []
    findings = load_pain_findings(path)
    return cast(list[JsonValue], pain_feedback_documents(findings))


def ingest_feedback(ctx: Context, extra_paths: list[str]) -> IngestReport:
    ingestor = FeedbackIngestor(ctx.config.feedback, ctx.config.active_niches())
    documents: list[JsonValue] = []
    if extra_paths:
        documents.extend(load_feedback_documents(paths=[Path(item) for item in extra_paths]))
    documents.extend(load_feedback_documents(inbox_dir=ctx.paths.inbox_dir))
    root_inbox = ctx.root / "inbox"
    if root_inbox.resolve() != ctx.paths.inbox_dir.resolve():
        documents.extend(load_feedback_documents(inbox_dir=root_inbox))
    documents.extend(_interview_documents(ctx))
    documents.extend(_pain_documents(ctx))
    return ingestor.ingest(documents)


def _pruning_registry(ctx: Context) -> PruningRegistry:
    return PruningRegistry(
        ctx.paths.state_dir / "pruning_registry.json", dry_run=ctx.dry_run
    )


def _pruned_niche_ids(ctx: Context) -> frozenset[str]:
    return _pruning_registry(ctx).load().pruned_ids()


def score_niches(ctx: Context, report: IngestReport) -> list[NicheScore]:
    pruned = _pruned_niche_ids(ctx)
    active = tuple(
        niche for niche in ctx.config.active_niches() if niche.id not in pruned
    )
    return NicheScorer(ctx.config.scoring).rank(active, report.items)


def _funnel(ctx: Context, feedback: list[FeedbackItem]) -> FunnelSummary:
    telemetry = FunnelTelemetry(ctx.paths.state_dir, dry_run=True)
    return telemetry.summarize(feedback)


def _allocation_state(ctx: Context) -> AllocationState:
    raw = ctx.store.read_json("allocation", {})
    if isinstance(raw, dict):
        value = raw.get("state")
        if isinstance(value, str):
            try:
                return AllocationState(value)
            except ValueError:
                return AllocationState(BOOTSTRAP)
    return AllocationState(BOOTSTRAP)


def _verified_cycles(ledger: Ledger) -> int:
    count = 0
    for entry in ledger.entries():
        if entry.get("kind") != "cycle":
            continue
        payload = entry.get("payload")
        if isinstance(payload, dict) and payload.get("verified") is True:
            count += 1
    return count


def _router(ctx: Context) -> InferenceRouter:
    budget = DailyBudget(
        ctx.paths.state_dir / "inference_budget.json",
        global_limit=ctx.config.network.max_requests_per_day,
        dry_run=ctx.dry_run,
    )
    cache = ResponseCache(
        ctx.paths.cache_dir / "inference.json",
        ttl_hours=ctx.config.inference.cache_ttl_hours,
        enabled=ctx.config.inference.cache_enabled,
        dry_run=ctx.dry_run,
    )
    policy = PolicyEngine(ctx.config.compliance, ctx.guard)
    return InferenceRouter(
        ctx.config.inference,
        ctx.guard,
        allow_network=ctx.online,
        policy=policy,
        budget=budget,
        cache=cache,
    )


def _capital_decision(
    ctx: Context, scores: list[NicheScore], phase: str
) -> CapitalDecision:
    summary = ctx.ledger.summary(strict=False)
    remaining_treasury = _remaining_treasury_usd(ctx, summary.real_revenue_usd)
    proven = summary.real_revenue_usd > 0.0
    requested = max(1.0, remaining_treasury * 0.25)
    options: list[InvestmentOption] = []
    if scores:
        top = scores[0]
        options.append(
            InvestmentOption(
                option_id=f"reliability-{top.niche_id}",
                category="reliability",
                expected_roi=1.0 + top.total,
                proven=proven,
                recurring=top.feedback_pressure > 0.0 or top.components["recurrence"] > 0.5,
                requested_usd=requested,
                description="Reduce recurring failures for the strongest niche",
            )
        )
        options.append(
            InvestmentOption(
                option_id=f"distribution-{top.niche_id}",
                category="distribution",
                expected_roi=0.5 + top.feedback_pressure,
                proven=proven,
                recurring=True,
                requested_usd=requested,
                description="Measurable distribution for proven recurring pain",
            )
        )
        options.append(
            InvestmentOption(
                option_id=f"oci-graduation-{top.niche_id}",
                category="oci_graduation",
                expected_roi=0.75 + top.total,
                proven=proven,
                recurring=top.components["recurrence"] >= 0.5,
                requested_usd=requested,
                description="Harden the product for responsible OCI graduation",
            )
        )
    options.append(
        InvestmentOption(
            option_id="clone-low-value",
            category="clone",
            expected_roi=0.25,
            proven=False,
            recurring=False,
            requested_usd=requested,
            description="Low-value clone expansion that must never compound",
        )
    )
    return CapitalAllocator(ctx.config.capital).decide(
        phase=phase,
        verified=_verified_cycles(ctx.ledger) > 0,
        treasury_balance_usd=remaining_treasury,
        settled_revenue_usd=summary.real_revenue_usd,
        options=options,
        source_account=SYSTEM_TREASURY,
    )


def _remaining_treasury_usd(ctx: Context, settled_revenue_usd: float) -> float:
    spent = 0.0
    for entry in ctx.ledger.entries():
        if entry.get("kind") != "capital":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        amount = payload.get("spent_usd")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            spent += float(amount)
    return max(0.0, settled_revenue_usd - spent)


def _release_notes(
    router: InferenceRouter,
    build: BuildResult,
    feedback_count: int,
) -> Completion:
    messages = (
        Message(
            role="system",
            content=(
                "Write short factual release notes for an open-core developer tool. "
                "No secrets, no personal data, no prices, no revenue promises."
            ),
        ),
        Message(
            role="user",
            content=(
                f"Product: {build.product_name} {build.version}. "
                f"Features: {'; '.join(build.features)}. "
                f"Sanitized feedback signals: {feedback_count}. "
                "Return at most three plain bullet points."
            ),
        ),
    )
    return router.complete("release_notes", messages, max_tokens=300, temperature=0.2)


def run_cycle(ctx: Context, feedback_paths: list[str]) -> CycleResult:
    started = time.monotonic()
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    opportunities = OpportunityScanner(ctx.config.active_niches()).scan(
        scores, report.items
    )
    funnel = _funnel(ctx, list(report.items))
    summary = ctx.ledger.summary()
    previous = _allocation_state(ctx)
    conversions = funnel.counts.get("feedback_signal", 0) + funnel.counts.get("paid_intent", 0)
    decision = AllocationStateMachine().decide(
        previous,
        verified_cycles=_verified_cycles(ctx.ledger),
        cycle_success=summary.chain_valid,
        real_revenue_usd=summary.real_revenue_usd,
        conversions=conversions,
        niche_scores={score.niche_id: score.total for score in scores},
    )
    capital = _capital_decision(ctx, scores, decision.state.value)
    result = CycleResult(
        report=report,
        scores=scores,
        opportunities=opportunities,
        allocation=decision,
        capital=capital.to_json(),
        funnel=funnel,
    )
    router = _router(ctx)
    builder = ProductBuilder(ctx.paths, version=_product_version())
    packager = PackageBuilder(ctx.paths, ctx.config.compliance)
    landing = LandingPageBuilder(ctx.paths)
    releases = ReleaseManager(ctx.paths, ctx.ledger)
    for score in scores[:_MAX_PRODUCTS_PER_CYCLE]:
        niche = ctx.config.niche_by_id(score.niche_id)
        matched = [item for item in report.items if niche.id in item.matched_niches]
        build = builder.build(niche, matched)
        package = packager.package(build)
        page = landing.render(
            build,
            niche,
            ctx.config.monetization.channels,
            archive_name=package.archive.name,
        )
        completion = _release_notes(router, build, len(matched))
        notes = sanitize_feedback_text(completion.text, max_chars=1200)
        release = releases.prepare(build, package, page, notes=notes)
        result.builds.append(build)
        result.packages.append(package)
        result.pages.append(page)
        result.releases.append(release)
        result.notes_providers.append(completion.provider)

    ctx.ledger.record_cost(
        0.0,
        f"free-tier inference ({router.stats.network_calls} network calls) and local compute",
    )
    stats = router.stats.to_json()
    ctx.store.write_json(
        "feedback",
        {
            "generated_at": iso_now(),
            "report": report.to_json(),
            "items": [item.to_json() for item in report.items],
        },
    )
    ctx.store.write_json("scores", [score.to_json() for score in scores])
    ctx.store.write_json(
        "opportunities", [signal.to_json() for signal in opportunities]
    )
    ctx.store.write_json("allocation", decision.to_json())
    ctx.store.write_json("funnel", funnel.to_json())
    ctx.store.write_json("capital", capital.to_json())
    duration = time.monotonic() - started
    result.duration_seconds = duration
    result.within_budget = duration <= ctx.config.runtime.max_cycle_seconds
    cycle_payload: JsonObject = {
        "verified": True,
        "feedback_items": len(report.items),
        "feedback_raw": report.raw_count,
        "top_niche": scores[0].niche_id if scores else None,
        "allocation_state": decision.state.value,
        "effort_shares": dict(decision.effort_shares),
        "money_allocation_usd": 0.0,
        "builds": [build.digest for build in result.builds],
        "releases": [release.release_id for release in result.releases],
        "inference": stats,
        "funnel": cast(JsonValue, funnel.counts),
        "duration_seconds": round(duration, 3),
        "within_cycle_budget": result.within_budget,
        "capital": capital.to_json(),
        "hard_cap_usd": 0.0,
    }
    ctx.ledger.record_capital(
        {
            "phase": capital.phase,
            "source_account": capital.source_account,
            "treasury_balance_usd": capital.treasury_balance_usd,
            "settled_revenue_usd": capital.settled_revenue_usd,
            "spent_usd": capital.spent_usd,
            "owner_vault_touched": capital.owner_vault_touched,
            "allocations": [line.to_json() for line in capital.allocations],
            "hard_cap_usd": 0.0,
        }
    )
    ctx.ledger.record_cycle(cycle_payload)
    ctx.ledger.validate_chain()
    cycle_state = result.to_json()
    cycle_state["cycle"] = len(ctx.ledger.entries())
    ctx.store.write_json("cycle", cycle_state)
    return result


def run_evolve(ctx: Context, feedback_paths: list[str], *, verify_commands: bool) -> JsonObject:
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    funnel = _funnel(ctx, list(report.items))
    heartbeat = HeartbeatMonitor(
        ctx.paths,
        ctx.config.heartbeat,
        ctx.config.swarm,
        ctx.ledger,
        dry_run=ctx.dry_run,
    ).inspect()
    observations = EvolutionAnalyzer().analyze(
        ledger=ctx.ledger.summary(),
        feedback=list(report.items),
        scores=scores,
        heartbeat=heartbeat,
        conversions=funnel.counts.get("feedback_signal", 0)
        + funnel.counts.get("paid_intent", 0),
    )
    proposer = EvolutionProposer(ctx.config.evolution, ctx.config.active_niches())
    proposals = proposer.propose(observations, list(report.items), scores)
    proposal_files = proposer.save(proposals, ctx.paths.proposals_dir)
    gate = ProposalGate(ctx.config.evolution, ctx.guard)
    results: list[GateResult] = []
    for proposal in proposals:
        gate_result = gate.evaluate(
            proposal, root=ctx.root, run_commands=verify_commands
        )
        results.append(gate_result)
        ctx.ledger.append(
            "upgrade_proposal",
            {
                "proposal_id": proposal.proposal_id,
                "title": proposal.title,
                "risk": proposal.risk,
                "target_paths": list(proposal.target_paths),
                "verification_passed": gate_result.passed,
                "auto_apply": False,
                "hard_cap_usd": 0.0,
            },
        )
        if gate_result.passed:
            ctx.ledger.append(
                "upgrade_verified",
                {
                    "proposal_id": proposal.proposal_id,
                    "commands": len(gate_result.commands),
                    "auto_apply": False,
                    "hard_cap_usd": 0.0,
                },
            )
    payload: JsonObject = {
        "observations": [observation.to_json() for observation in observations],
        "proposals": [proposal.to_json() for proposal in proposals],
        "gates": [result.to_json() for result in results],
        "proposal_files": [ctx.paths.display(path) for path in proposal_files],
        "verified": all(result.passed for result in results) if results else True,
    }
    ctx.store.write_json("evolution", payload)
    return payload


def run_replicate(ctx: Context, args: argparse.Namespace) -> JsonObject:
    if not ctx.config.swarm.enabled:
        return {"status": "skipped", "reason": "swarm replication is disabled in config"}
    if not args.approval_file:
        raise ApprovalRequired("--approval-file is required before spawning a child")
    approval = load_approval(Path(args.approval_file))
    if approval.action != "replication":
        raise ApprovalRequired("approval action must be 'replication'")
    mode = args.mode or ctx.config.swarm.default_mode
    if mode == "repo" and not args.apply:
        raise StateError("repo mode requires --apply for the external GitHub action")
    report = ingest_feedback(ctx, [])
    scores = score_niches(ctx, report)
    if args.niche:
        niche = ctx.config.niche_by_id(args.niche)
    elif scores:
        niche = ctx.config.niche_by_id(scores[0].niche_id)
    else:
        niche = ctx.config.active_niches()[0]
    lock = NicheLock(
        ctx.paths.state_dir / "swarm_registry.json",
        max_children=ctx.config.swarm.max_children,
        dry_run=ctx.dry_run,
    )
    owner = ctx.config.repo.owner
    repo_owner = None if "PLACEHOLDER" in owner else owner
    github = GitHubClient(dry_run=(not args.apply) or ctx.dry_run)
    spawner = SwarmSpawner(
        ctx.config.swarm,
        ctx.guard,
        ctx.paths,
        ctx.ledger,
        lock,
        repo_owner=repo_owner,
        github=github,
        dry_run=ctx.dry_run,
    )
    spawn = spawner.spawn(niche, approval, mode=mode)
    aggregator = SwarmAggregator(ctx.paths, ctx.config.swarm, lock)
    rollup = aggregator.persist(ctx.store)
    return {**spawn.to_json(), "rollup": rollup.to_json()}


def run_heartbeat(ctx: Context, workflow_file: str | None) -> JsonObject:
    lock = NicheLock(
        ctx.paths.state_dir / "swarm_registry.json",
        max_children=ctx.config.swarm.max_children,
        dry_run=ctx.dry_run,
    )
    aggregator = SwarmAggregator(ctx.paths, ctx.config.swarm, lock)
    rollup = aggregator.rollup()
    aggregator.persist(ctx.store, rollup)
    if rollup.children:
        ctx.ledger.append(
            "spawn_rollup",
            {
                "children": len(rollup.children),
                "active": rollup.active_count,
                "stale": rollup.stale_count,
                "real_revenue_usd": round(rollup.total_real_revenue_usd, 6),
                "simulated_revenue_usd": round(rollup.total_simulated_revenue_usd, 6),
                "cost_usd": round(rollup.total_cost_usd, 6),
                "hard_cap_usd": 0.0,
            },
        )
    monitor = HeartbeatMonitor(
        ctx.paths,
        ctx.config.heartbeat,
        ctx.config.swarm,
        ctx.ledger,
        dry_run=ctx.dry_run,
    )
    report = monitor.run(workflow_file=workflow_file)
    return {**report.to_json(), "rollup": rollup.to_json()}


def _ledger_age_days(ctx: Context) -> float:
    entries = ctx.ledger.entries()
    if not entries:
        return 0.0
    first_ts = entries[0].get("ts")
    if not isinstance(first_ts, str):
        return 0.0
    try:
        return max(0.0, hours_since(first_ts, utc_now()) / 24.0)
    except ValueError:
        return 0.0


def _active_cycle_niches(ctx: Context) -> set[str]:
    state = ctx.store.read_json("cycle", {})
    if not isinstance(state, dict):
        return set()
    scores = state.get("scores")
    if not isinstance(scores, list):
        return set()
    niches: set[str] = set()
    for item in scores:
        if not isinstance(item, dict):
            continue
        niche_id = item.get("niche_id")
        if isinstance(niche_id, str):
            niches.add(niche_id)
    return niches


def _cycles_since_top(ctx: Context, niche_id: str) -> int:
    count = 0
    for entry in reversed(ctx.ledger.entries()):
        if entry.get("kind") != "cycle":
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("top_niche") == niche_id:
            break
        count += 1
    return count


def run_prune(ctx: Context, feedback_paths: list[str]) -> JsonObject:
    engine = PruningEngine(ctx.config.pruning)
    registry = _pruning_registry(ctx)
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    funnel = _funnel(ctx, list(report.items))
    age_days = _ledger_age_days(ctx)
    active_in_cycle = _active_cycle_niches(ctx)
    top_niche = scores[0].niche_id if scores else None
    decisions: list[PruneDecision] = []
    for niche in ctx.config.active_niches():
        matched = [item for item in report.items if niche.id in item.matched_niches]
        stale = 0.0 if niche.id in active_in_cycle else age_days
        decisions.append(
            engine.decide(
                PruneSignals(
                    asset_id=niche.id,
                    age_days=age_days,
                    stale_days=stale,
                    feedback_items=len(matched),
                    repeat_usage=max(0, len(matched) - 1),
                    adoption=(
                        funnel.conversion.get("download_rate", 0.0)
                        if niche.id == top_niche
                        else 0.0
                    ),
                    conversions=(
                        funnel.counts.get("paid_intent", 0) if niche.id == top_niche else 0
                    ),
                    consecutive_zero_cycles=_cycles_since_top(ctx, niche.id),
                )
            )
        )

    lock = NicheLock(
        ctx.paths.state_dir / "swarm_registry.json",
        max_children=ctx.config.swarm.max_children,
        dry_run=ctx.dry_run,
    )
    rollup = SwarmAggregator(ctx.paths, ctx.config.swarm, lock).rollup()
    for child in lock.children():
        matched = [item for item in report.items if child.niche_id in item.matched_niches]
        try:
            stale_hours = hours_since(child.last_heartbeat or child.created_at, utc_now())
        except ValueError:
            stale_hours = ctx.config.pruning.sunset_after_stale_days * 24.0
        decision = engine.decide(
            PruneSignals(
                asset_id=child.child_id,
                age_days=max(
                    0.0,
                    _timestamp_age_days(child.created_at),
                ),
                stale_days=max(0.0, stale_hours / 24.0),
                feedback_items=len(matched),
                repeat_usage=max(0, len(matched) - 1),
                real_revenue_usd=next(
                    (
                        item.real_revenue_usd
                        for item in rollup.children
                        if item.child_id == child.child_id
                    ),
                    0.0,
                ),
            )
        )
        decisions.append(decision)
        if decision.action == ACTION_PAUSE and child.status == "active":
            lock.transition(child.child_id, "paused")
        elif decision.action in {ACTION_SUNSET, ACTION_ARCHIVE} and child.status in {
            "active",
            "paused",
        }:
            lock.transition(child.child_id, "retired")

    snapshot = registry.record(decisions)
    archived_paths: list[str] = []
    if not ctx.dry_run:
        archive_dir = ctx.runtime_root / ctx.config.pruning.archive_dir
        ensure_dir(archive_dir)
        for decision in decisions:
            if decision.action in {ACTION_SUNSET, ACTION_ARCHIVE}:
                path = archive_dir / f"{decision.asset_id}.json"
                write_json_atomic(path, decision.to_json())
                archived_paths.append(ctx.paths.display(path))
    ctx.ledger.record_pruning(
        {
            "decisions": [decision.to_json() for decision in decisions],
            "paused": cast(list[JsonValue], list(snapshot.paused)),
            "sunset": cast(list[JsonValue], list(snapshot.sunset)),
            "archived": cast(list[JsonValue], list(snapshot.archived)),
            "hard_cap_usd": 0.0,
        }
    )
    payload: JsonObject = {
        "decisions": [decision.to_json() for decision in decisions],
        "paused": cast(list[JsonValue], list(snapshot.paused)),
        "sunset": cast(list[JsonValue], list(snapshot.sunset)),
        "archived": cast(list[JsonValue], list(snapshot.archived)),
        "pruned_niches": cast(list[JsonValue], sorted(snapshot.pruned_ids())),
        "archive_manifests": cast(list[JsonValue], archived_paths),
    }
    ctx.store.write_json("pruning", payload)
    return payload


def _timestamp_age_days(timestamp: str) -> float:
    try:
        return max(0.0, hours_since(timestamp, utc_now()) / 24.0)
    except ValueError:
        return 0.0


def run_handoff_brief(ctx: Context, feedback_paths: list[str]) -> JsonObject:
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    summary = ctx.ledger.summary(strict=False)
    packets = HandoffBuilder(ctx.config.handoff).build(
        repo_name=ctx.config.repo.name,
        scores=scores,
        ledger=summary,
    )
    output_dir = ctx.runtime_root / ctx.config.handoff.output_dir
    files = save_packets(packets, output_dir, dry_run=ctx.dry_run)
    payload: JsonObject = {
        "packets": [packet.to_json() for packet in packets],
        "files": [ctx.paths.display(path) for path in files],
        "human_owned_tasks": list(ctx.config.handoff.human_owned_tasks),
        "system_owned_tasks": list(ctx.config.handoff.system_owned_tasks),
        "interview_insights_path": ctx.paths.display(
            output_dir / "interview_insights.json"
        ),
    }
    ctx.ledger.record_handoff(
        {
            "packets": [packet.kind for packet in packets],
            "count": len(packets),
            "hard_cap_usd": 0.0,
        }
    )
    ctx.store.write_json("handoff", payload)
    return payload


def run_ingest_pain(ctx: Context, source: str) -> JsonObject:
    candidate = Path(source).expanduser()
    path = candidate if candidate.is_absolute() else ctx.root / candidate
    findings = load_pain_findings(path)
    documents = cast(list[JsonValue], pain_feedback_documents(findings))
    report = FeedbackIngestor(
        ctx.config.feedback, ctx.config.active_niches()
    ).ingest(documents)
    state_path = ctx.paths.state_dir / "pain_interviews.json"
    merged: dict[str, PainFinding] = {}
    if state_path.exists():
        for finding in load_pain_findings(state_path):
            merged[finding.finding_id] = finding
    for finding in findings:
        merged[finding.finding_id] = finding
    ordered = tuple(merged[key] for key in sorted(merged))
    save_pain_findings(ordered, state_path, dry_run=ctx.dry_run)
    payload: JsonObject = {
        "findings": [finding.to_json() for finding in ordered],
        "ingested": len(findings),
        "verified_only": True,
        "top_priority": report.items[0].to_json() if report.items else None,
        "redactions": cast(JsonValue, dict(sorted(report.redactions.items()))),
        "state_path": ctx.paths.display(state_path),
        "hard_cap_usd": 0.0,
    }
    ctx.store.write_json("pain", payload)
    return payload


def run_discovery_briefs(ctx: Context, feedback_paths: list[str]) -> JsonObject:
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    briefs = DiscoveryBriefBuilder(ctx.config.outreach).build(
        scores, ctx.config.active_niches()
    )
    output_dir = ctx.runtime_root / ctx.config.handoff.output_dir / "discovery"
    files = save_discovery_briefs(briefs, output_dir, dry_run=ctx.dry_run)
    payload: JsonObject = {
        "briefs": [brief.to_json() for brief in briefs],
        "files": [ctx.paths.display(path) for path in files],
        "output_dir": ctx.paths.display(output_dir),
        "auto_send_enabled": False,
        "human_review_required": True,
        "hard_cap_usd": 0.0,
    }
    ctx.ledger.record_handoff(
        {
            "packets": ["discovery_brief" for _ in briefs],
            "count": len(briefs),
            "auto_send_enabled": False,
            "hard_cap_usd": 0.0,
        }
    )
    ctx.store.write_json("discovery", payload)
    return payload


def _channel_scan(
    ctx: Context, feedback_paths: list[str]
) -> tuple[list[ChannelOpportunity], ChannelSensor]:
    report = ingest_feedback(ctx, feedback_paths)
    scores = score_niches(ctx, report)
    sensor = ChannelSensor(ctx.config.monetization_channels)
    opportunities = sensor.scan(
        ctx.config.active_niches(), scores, report.items
    )
    return opportunities, sensor


def _channel_scan_payload(
    opportunities: list[ChannelOpportunity], sensor: ChannelSensor
) -> JsonObject:
    best = top_by_channel(opportunities)
    return {
        "opportunities": [opportunity.to_json() for opportunity in opportunities],
        "top_by_channel": {
            kind: best[kind].to_json() for kind in sorted(best)
        },
        "weights": sensor.weights.to_json(),
        "live_execution_allowed": False,
        "automated_payouts": False,
        "owner_vault_touched": False,
        "real_funds_at_risk_usd": 0.0,
        "hard_cap_usd": 0.0,
    }


def run_scan_channels(ctx: Context, feedback_paths: list[str]) -> JsonObject:
    opportunities, sensor = _channel_scan(ctx, feedback_paths)
    payload = _channel_scan_payload(opportunities, sensor)
    ctx.store.write_json("channels", payload)
    return payload


def run_max_outcome_plan(ctx: Context, feedback_paths: list[str]) -> JsonObject:
    opportunities, sensor = _channel_scan(ctx, feedback_paths)
    simulator = OutcomeSimulator(
        ctx.config.capital_research,
        ctx.config.monetization_channels,
        weights=sensor.weights,
    )
    plan = simulator.simulate(opportunities)
    packets = ChannelHandoffBuilder(ctx.config.handoff).build(
        repo_name=ctx.config.repo.name,
        plan=plan,
        opportunities=opportunities,
        niches=ctx.config.active_niches(),
    )
    output_dir = ctx.runtime_root / ctx.config.handoff.output_dir / "channels"
    files = save_channel_handoffs(packets, output_dir, dry_run=ctx.dry_run)
    payload: JsonObject = {
        "plan": plan.to_json(),
        "packets": [packet.to_json() for packet in packets],
        "files": [ctx.paths.display(path) for path in files],
        "output_dir": ctx.paths.display(output_dir),
        "research_only": True,
        "live_execution_allowed": False,
        "real_funds_at_risk_usd": 0.0,
        "owner_vault_touched": False,
        "hard_cap_usd": 0.0,
    }
    ctx.store.write_json("channels", _channel_scan_payload(opportunities, sensor))
    ctx.ledger.record_handoff(
        {
            "packets": [packet.kind for packet in packets],
            "count": len(packets),
            "research_only": True,
            "live_execution_allowed": False,
            "owner_vault_touched": False,
            "hard_cap_usd": 0.0,
        }
    )
    ctx.store.write_json("outcome_plan", payload)
    return payload


def status_report(ctx: Context) -> JsonObject:
    summary: LedgerSummary = ctx.ledger.summary(strict=False)
    heartbeat: HeartbeatCheck = HeartbeatMonitor(
        ctx.paths,
        ctx.config.heartbeat,
        ctx.config.swarm,
        ctx.ledger,
        dry_run=ctx.dry_run,
    ).inspect()
    lock = NicheLock(
        ctx.paths.state_dir / "swarm_registry.json",
        max_children=ctx.config.swarm.max_children,
        dry_run=ctx.dry_run,
    )
    rollup: SwarmRollup = SwarmAggregator(ctx.paths, ctx.config.swarm, lock).rollup()
    return {
        "repo": ctx.config.repo.name,
        "owner": ctx.config.repo.owner,
        "invariants_digest": ctx.guard.digest,
        "billing": {
            "hard_cap_usd": ctx.config.billing.hard_cap_usd,
            "paid_resources_enabled": ctx.config.billing.paid_resources_enabled,
            "paid_apis_enabled": ctx.config.billing.paid_apis_enabled,
        },
        "online": ctx.online,
        "ledger": summary.to_json(),
        "heartbeat": heartbeat.to_json(),
        "allocation": ctx.store.read_json("allocation", None),
        "capital": ctx.store.read_json("capital", None),
        "pruning": ctx.store.read_json("pruning", None),
        "handoff": ctx.store.read_json("handoff", None),
        "pain": ctx.store.read_json("pain", None),
        "discovery": ctx.store.read_json("discovery", None),
        "channels": ctx.store.read_json("channels", None),
        "outcome_plan": ctx.store.read_json("outcome_plan", None),
        "capital_research": {
            "research_only": ctx.config.capital_research.research_only,
            "live_trading_enabled": False,
            "live_investing_enabled": False,
            "real_funds_at_risk_usd": 0.0,
            "owner_vault_touched": False,
        },
        "cycle": ctx.store.read_json("cycle", None),
        "funnel": ctx.store.read_json("funnel", None),
        "evolution": ctx.store.read_json("evolution", None),
        "swarm": rollup.to_json(),
        "runtime_root": str(ctx.runtime_root),
    }


def _product_version() -> str:
    value = os.environ.get("SASK_PRODUCT_VERSION", _PRODUCT_VERSION).strip()
    return value if re.fullmatch(_BUILD_VERSION_PATTERN, value) else _PRODUCT_VERSION


def _emit(payload: JsonValue, *, stream: TextIO | None = None) -> None:
    sanitized, _ = sanitize_public_payload(payload)
    print(
        json.dumps(sanitized, indent=2, sort_keys=True),
        file=stream if stream is not None else sys.stdout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sask",
        description="SASK_CORE_LOOP zero-billing product factory orchestrator",
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root")
    parser.add_argument("--dry-run", action="store_true", help="use a temporary runtime")
    parser.add_argument("--cycle", action="store_true", help="run a revenue cycle")
    parser.add_argument("--evolve", action="store_true", help="generate upgrade proposals")
    parser.add_argument("--replicate", action="store_true", help="spawn an approved child")
    parser.add_argument("--heartbeat", action="store_true", help="run heartbeat checks")
    parser.add_argument(
        "--prune", action="store_true", help="prune dead niches and replicas"
    )
    parser.add_argument(
        "--handoff-brief",
        action="store_true",
        help="prepare human-owned reliability, outreach, and legal briefs",
    )
    parser.add_argument(
        "--interview-insights",
        default=None,
        help="sanitized human interview insights JSON for top-priority ingestion",
    )
    parser.add_argument(
        "--ingest-pain",
        default=None,
        metavar="PATH",
        help="ingest structured, verified pain-interview findings (JSON)",
    )
    parser.add_argument(
        "--discovery-briefs",
        action="store_true",
        help="prepare human-reviewed discovery briefs for high-pain niches",
    )
    parser.add_argument(
        "--scan-channels",
        action="store_true",
        help="score marketplace, bounty, template, audit, and recurring channels",
    )
    parser.add_argument(
        "--max-outcome-plan",
        action="store_true",
        help="simulate paper-only outcomes and prepare channel handoff packets",
    )
    parser.add_argument("--status", action="store_true", help="print system status")
    parser.add_argument("--online", action="store_true", help="allow free-tier network APIs")
    parser.add_argument("--offline", action="store_true", help="force deterministic fallback")
    parser.add_argument(
        "--feedback", action="append", default=[], help="feedback JSON/JSONL file"
    )
    parser.add_argument("--niche", default=None, help="niche id for replication")
    parser.add_argument("--mode", choices=["workspace", "repo"], default=None)
    parser.add_argument("--approval-file", default=None)
    parser.add_argument("--apply", action="store_true", help="allow external GitHub actions")
    parser.add_argument("--workflow", default=None, help="record a workflow run timestamp")
    parser.add_argument(
        "--verify-commands",
        action="store_true",
        help="run configured verification commands during --evolve",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx: Context | None = None
    results: JsonObject = {}
    try:
        ctx = build_context(args)
        if args.ingest_pain:
            results["pain"] = run_ingest_pain(ctx, args.ingest_pain)
        if args.cycle:
            results["cycle"] = cast(JsonValue, run_cycle(ctx, list(args.feedback)).to_json())
        if args.evolve:
            results["evolve"] = run_evolve(
                ctx, list(args.feedback), verify_commands=bool(args.verify_commands)
            )
        if args.replicate:
            results["replicate"] = run_replicate(ctx, args)
        if args.heartbeat:
            results["heartbeat"] = run_heartbeat(ctx, args.workflow)
        if args.prune:
            results["prune"] = run_prune(ctx, list(args.feedback))
        if args.handoff_brief:
            results["handoff"] = run_handoff_brief(ctx, list(args.feedback))
        if args.discovery_briefs:
            results["discovery"] = run_discovery_briefs(ctx, list(args.feedback))
        if args.scan_channels:
            results["channels"] = run_scan_channels(ctx, list(args.feedback))
        if args.max_outcome_plan:
            results["outcome_plan"] = run_max_outcome_plan(
                ctx, list(args.feedback)
            )
        if args.status or not any(
            (
                args.cycle,
                args.evolve,
                args.replicate,
                args.heartbeat,
                args.prune,
                args.handoff_brief,
                args.discovery_briefs,
                args.scan_channels,
                args.max_outcome_plan,
                args.ingest_pain,
            )
        ):
            results["status"] = status_report(ctx)
        results["dry_run"] = ctx.dry_run
        results["online"] = ctx.online
        _emit(cast(JsonValue, results))
        return 0
    except SASKError as exc:
        _emit(
            {
                "error": type(exc).__name__,
                "message": redact_text(str(exc)).text,
            },
            stream=sys.stderr,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("SASK_DEBUG") == "1":
            raise
        _emit(
            {
                "error": type(exc).__name__,
                "message": redact_text(f"{type(exc).__name__}: {exc}").text,
            },
            stream=sys.stderr,
        )
        return 1
    finally:
        if ctx is not None:
            ctx.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
