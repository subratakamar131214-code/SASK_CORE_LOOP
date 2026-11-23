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

from src.compliance.policy import PolicyEngine
from src.core.config import SystemConfig, validate_system_config
from src.core.errors import ApprovalRequired, SASKError, StateError
from src.core.invariants import InvariantGuard
from src.core.paths import RepoPaths
from src.core.redact import redact_text, sanitize_public_payload
from src.core.state import StateStore
from src.core.timeutil import iso_now
from src.core.types import JsonObject, JsonValue
from src.distribution.funnel import FunnelSummary, FunnelTelemetry
from src.distribution.landing import LandingPage, LandingPageBuilder
from src.distribution.packager import PackageArtifact, PackageBuilder
from src.evolution.analyzer import EvolutionAnalyzer
from src.evolution.gate import GateResult, ProposalGate
from src.evolution.proposer import EvolutionProposer
from src.factory.builder import BuildResult, ProductBuilder
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
from src.sensors.feedback import (
    FeedbackIngestor,
    FeedbackItem,
    IngestReport,
    load_feedback_documents,
)
from src.sensors.niche_scoring import NicheScore, NicheScorer
from src.sensors.opportunity import OpportunityScanner, OpportunitySignal
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
        tempdir=tempdir,
    )


def ingest_feedback(ctx: Context, extra_paths: list[str]) -> IngestReport:
    ingestor = FeedbackIngestor(ctx.config.feedback, ctx.config.active_niches())
    documents: list[JsonValue] = []
    if extra_paths:
        documents.extend(load_feedback_documents(paths=[Path(item) for item in extra_paths]))
    documents.extend(load_feedback_documents(inbox_dir=ctx.paths.inbox_dir))
    root_inbox = ctx.root / "inbox"
    if root_inbox.resolve() != ctx.paths.inbox_dir.resolve():
        documents.extend(load_feedback_documents(inbox_dir=root_inbox))
    return ingestor.ingest(documents)


def score_niches(ctx: Context, report: IngestReport) -> list[NicheScore]:
    scorer = NicheScorer(ctx.config.scoring)
    return scorer.rank(ctx.config.active_niches(), report.items)


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
    result = CycleResult(
        report=report,
        scores=scores,
        opportunities=opportunities,
        allocation=decision,
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
        "hard_cap_usd": 0.0,
    }
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
        if args.status or not any(
            (args.cycle, args.evolve, args.replicate, args.heartbeat)
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
