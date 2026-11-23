"""Typed loading and validation for config/system.yaml."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from src.core.errors import ConfigError, InvariantViolation
from src.core.invariants import InvariantGuard, normalize_repo_path
from src.core.yamlutil import (
    as_mapping,
    load_yaml_mapping,
    mapping_numbers,
    require_bool,
    require_float,
    require_int,
    require_mapping,
    require_sequence,
    require_str,
    require_str_tuple,
)

_PROVIDER_KINDS = {"openai_compatible", "gemini", "ollama", "deterministic"}
_SWARM_MODES = {"workspace", "repo"}
_CHANNEL_STATUSES = {"placeholder", "active", "disabled"}


@dataclass(frozen=True)
class RepoConfig:
    name: str
    owner: str
    visibility: str
    default_branch: str


@dataclass(frozen=True)
class BillingConfig:
    currency: str
    hard_cap_usd: float
    paid_resources_enabled: bool
    paid_apis_enabled: bool


@dataclass(frozen=True)
class RuntimeConfig:
    offline_by_default: bool
    max_cycle_seconds: int
    state_commit_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class FeedbackConfig:
    max_items_per_cycle: int
    max_state_items: int
    source_weights: Mapping[str, float]
    kind_multipliers: Mapping[str, float]
    priority_labels: tuple[str, ...]


@dataclass(frozen=True)
class NicheConfig:
    id: str
    family: str
    title: str
    product_name: str
    product_type: str
    description: str
    keywords: tuple[str, ...]
    pain: float
    willingness_to_pay: float
    buildability: float
    competition: float
    compliance_risk: float
    active: bool


@dataclass(frozen=True)
class ScoringWeights:
    feedback: float
    pain: float
    willingness_to_pay: float
    buildability: float
    competition_inverse: float
    compliance_safety: float

    def total(self) -> float:
        return (
            self.feedback
            + self.pain
            + self.willingness_to_pay
            + self.buildability
            + self.competition_inverse
            + self.compliance_safety
        )


@dataclass(frozen=True)
class BackoffConfig:
    base_seconds: float
    max_seconds: float
    max_attempts: int
    jitter_fraction: float
    seed: int


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    enabled: bool
    kind: str
    base_url: str
    api_key_env: str | None
    models: tuple[str, ...]
    max_requests_per_day: int
    max_tokens_per_request: int
    timeout_seconds: float


@dataclass(frozen=True)
class InferenceConfig:
    max_calls_per_cycle: int
    cache_enabled: bool
    cache_ttl_hours: int
    backoff: BackoffConfig
    providers: tuple[ProviderConfig, ...]

    def provider(self, name: str) -> ProviderConfig:
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise ConfigError(f"unknown inference provider: {name!r}")

    def ordered_enabled(self) -> tuple[ProviderConfig, ...]:
        return tuple(provider for provider in self.providers if provider.enabled)


@dataclass(frozen=True)
class NetworkConfig:
    offline_by_default: bool
    request_timeout_seconds: float
    max_requests_per_day: int
    user_agent: str


@dataclass(frozen=True)
class MonetizationChannel:
    id: str
    label: str
    url: str
    status: str
    requires_human_setup: bool


@dataclass(frozen=True)
class MonetizationConfig:
    enabled: bool
    zero_upfront_only: bool
    channels: tuple[MonetizationChannel, ...]
    payouts_automated: bool
    payment_credentials_in_repo: bool


@dataclass(frozen=True)
class SwarmConfig:
    enabled: bool
    default_mode: str
    max_children: int
    min_spawn_interval_hours: float
    repo_prefix: str
    require_human_approval: bool
    allow_repo_creation: bool
    telemetry_stale_hours: float
    workspace_dir: str


@dataclass(frozen=True)
class EvolutionConfig:
    max_proposals_per_cycle: int
    require_human_review: bool
    auto_apply: bool
    protected_paths: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class HeartbeatWorkflow:
    file: str
    max_age_hours: float


@dataclass(frozen=True)
class HeartbeatConfig:
    stale_after_hours: Mapping[str, float]
    required_workflows: tuple[HeartbeatWorkflow, ...]
    alert_label: str
    max_alerts_per_day: int


@dataclass(frozen=True)
class ComplianceConfig:
    allowed_licenses: tuple[str, ...]
    max_github_issues_per_day: int
    respect_robots_txt: bool
    forbid_bulk_issue_creation: bool
    forbid_unsolicited_outreach: bool
    min_request_interval_seconds: float


@dataclass(frozen=True)
class SystemConfig:
    schema_version: int
    repo: RepoConfig
    billing: BillingConfig
    runtime: RuntimeConfig
    feedback: FeedbackConfig
    niches: tuple[NicheConfig, ...]
    scoring: ScoringWeights
    inference: InferenceConfig
    network: NetworkConfig
    monetization: MonetizationConfig
    swarm: SwarmConfig
    evolution: EvolutionConfig
    heartbeat: HeartbeatConfig
    compliance: ComplianceConfig

    @classmethod
    def load(cls, path: Path) -> SystemConfig:
        document = load_yaml_mapping(path)
        runtime = require_mapping(document, "runtime", "config")
        feedback = require_mapping(document, "feedback", "config")
        scoring = require_mapping(document, "scoring", "config")
        weights_doc = require_mapping(scoring, "weights", "scoring")
        inference = require_mapping(document, "inference", "config")
        backoff = require_mapping(inference, "backoff", "inference")
        network = require_mapping(document, "network", "config")
        monetization = require_mapping(document, "monetization", "config")
        payouts = require_mapping(monetization, "payouts", "monetization")
        swarm = require_mapping(document, "swarm", "config")
        evolution = require_mapping(document, "evolution", "config")
        heartbeat = require_mapping(document, "heartbeat", "config")
        compliance = require_mapping(document, "compliance", "config")

        niches = tuple(
            _parse_niche(item, index)
            for index, item in enumerate(require_sequence(document, "niches", "config"))
        )
        if not niches:
            raise ConfigError("config.niches must contain at least one niche")
        if len({niche.id for niche in niches}) != len(niches):
            raise ConfigError("niche ids must be unique")
        if not any(niche.active for niche in niches):
            raise ConfigError("at least one niche must be active")

        weights = ScoringWeights(
            feedback=require_float(weights_doc, "feedback", "scoring.weights"),
            pain=require_float(weights_doc, "pain", "scoring.weights"),
            willingness_to_pay=require_float(weights_doc, "willingness_to_pay", "scoring.weights"),
            buildability=require_float(weights_doc, "buildability", "scoring.weights"),
            competition_inverse=require_float(
                weights_doc, "competition_inverse", "scoring.weights"
            ),
            compliance_safety=require_float(weights_doc, "compliance_safety", "scoring.weights"),
        )
        if abs(weights.total() - 1.0) > 1e-6:
            raise ConfigError("scoring.weights must sum to 1.0")

        providers = tuple(
            _parse_provider(as_mapping(item, f"inference.providers[{index}]"))
            for index, item in enumerate(require_sequence(inference, "providers", "inference"))
        )
        if not providers:
            raise ConfigError("inference.providers cannot be empty")
        if len({provider.name for provider in providers}) != len(providers):
            raise ConfigError("inference provider names must be unique")
        if not any(provider.kind == "deterministic" for provider in providers):
            raise ConfigError("a deterministic fallback provider is mandatory")

        channels = tuple(
            _parse_channel(as_mapping(item, f"monetization.channels[{index}]"))
            for index, item in enumerate(require_sequence(monetization, "channels", "monetization"))
        )

        source_weights = mapping_numbers(
            require_mapping(feedback, "source_weights", "feedback"), "feedback.source_weights"
        )
        kind_multipliers = mapping_numbers(
            require_mapping(feedback, "kind_multipliers", "feedback"),
            "feedback.kind_multipliers",
        )
        for key in ("customer", "support", "github_issue", "inbox"):
            if key not in source_weights:
                raise ConfigError(f"feedback.source_weights.{key} is required")
        for key in ("bug", "complaint", "feature_request", "question"):
            if key not in kind_multipliers:
                raise ConfigError(f"feedback.kind_multipliers.{key} is required")

        verification_commands = _parse_commands(evolution)
        protected_paths = require_str_tuple(evolution, "protected_paths", "evolution")

        heartbeat_workflows = tuple(
            _parse_heartbeat_workflow(as_mapping(item, f"heartbeat.required_workflows[{index}]"))
            for index, item in enumerate(
                require_sequence(heartbeat, "required_workflows", "heartbeat")
            )
        )

        config = cls(
            schema_version=require_int(document, "schema_version", "config"),
            repo=_parse_repo(require_mapping(document, "repo", "config")),
            billing=_parse_billing(require_mapping(document, "billing", "config")),
            runtime=RuntimeConfig(
                offline_by_default=require_bool(runtime, "offline_by_default", "runtime"),
                max_cycle_seconds=require_int(runtime, "max_cycle_seconds", "runtime"),
                state_commit_allowlist=require_str_tuple(
                    runtime, "state_commit_allowlist", "runtime"
                ),
            ),
            feedback=FeedbackConfig(
                max_items_per_cycle=require_int(feedback, "max_items_per_cycle", "feedback"),
                max_state_items=require_int(feedback, "max_state_items", "feedback"),
                source_weights=source_weights,
                kind_multipliers=kind_multipliers,
                priority_labels=require_str_tuple(feedback, "priority_labels", "feedback"),
            ),
            niches=niches,
            scoring=weights,
            inference=InferenceConfig(
                max_calls_per_cycle=require_int(inference, "max_calls_per_cycle", "inference"),
                cache_enabled=require_bool(inference, "cache_enabled", "inference"),
                cache_ttl_hours=require_int(inference, "cache_ttl_hours", "inference"),
                backoff=BackoffConfig(
                    base_seconds=require_float(backoff, "base_seconds", "inference.backoff"),
                    max_seconds=require_float(backoff, "max_seconds", "inference.backoff"),
                    max_attempts=require_int(backoff, "max_attempts", "inference.backoff"),
                    jitter_fraction=require_float(backoff, "jitter_fraction", "inference.backoff"),
                    seed=require_int(backoff, "seed", "inference.backoff"),
                ),
                providers=providers,
            ),
            network=NetworkConfig(
                offline_by_default=require_bool(network, "offline_by_default", "network"),
                request_timeout_seconds=require_float(
                    network, "request_timeout_seconds", "network"
                ),
                max_requests_per_day=require_int(network, "max_requests_per_day", "network"),
                user_agent=require_str(network, "user_agent", "network"),
            ),
            monetization=MonetizationConfig(
                enabled=require_bool(monetization, "enabled", "monetization"),
                zero_upfront_only=require_bool(monetization, "zero_upfront_only", "monetization"),
                channels=channels,
                payouts_automated=require_bool(payouts, "automated", "monetization.payouts"),
                payment_credentials_in_repo=require_bool(
                    payouts, "payment_credentials_in_repo", "monetization.payouts"
                ),
            ),
            swarm=SwarmConfig(
                enabled=require_bool(swarm, "enabled", "swarm"),
                default_mode=require_str(swarm, "default_mode", "swarm"),
                max_children=require_int(swarm, "max_children", "swarm"),
                min_spawn_interval_hours=require_float(swarm, "min_spawn_interval_hours", "swarm"),
                repo_prefix=require_str(swarm, "repo_prefix", "swarm"),
                require_human_approval=require_bool(swarm, "require_human_approval", "swarm"),
                allow_repo_creation=require_bool(swarm, "allow_repo_creation", "swarm"),
                telemetry_stale_hours=require_float(swarm, "telemetry_stale_hours", "swarm"),
                workspace_dir=require_str(swarm, "workspace_dir", "swarm"),
            ),
            evolution=EvolutionConfig(
                max_proposals_per_cycle=require_int(
                    evolution, "max_proposals_per_cycle", "evolution"
                ),
                require_human_review=require_bool(evolution, "require_human_review", "evolution"),
                auto_apply=require_bool(evolution, "auto_apply", "evolution"),
                protected_paths=protected_paths,
                verification_commands=verification_commands,
            ),
            heartbeat=HeartbeatConfig(
                stale_after_hours=mapping_numbers(
                    require_mapping(heartbeat, "stale_after_hours", "heartbeat"),
                    "heartbeat.stale_after_hours",
                ),
                required_workflows=heartbeat_workflows,
                alert_label=require_str(heartbeat, "alert_label", "heartbeat"),
                max_alerts_per_day=require_int(heartbeat, "max_alerts_per_day", "heartbeat"),
            ),
            compliance=ComplianceConfig(
                allowed_licenses=require_str_tuple(compliance, "allowed_licenses", "compliance"),
                max_github_issues_per_day=require_int(
                    compliance, "max_github_issues_per_day", "compliance"
                ),
                respect_robots_txt=require_bool(compliance, "respect_robots_txt", "compliance"),
                forbid_bulk_issue_creation=require_bool(
                    compliance, "forbid_bulk_issue_creation", "compliance"
                ),
                forbid_unsolicited_outreach=require_bool(
                    compliance, "forbid_unsolicited_outreach", "compliance"
                ),
                min_request_interval_seconds=require_float(
                    compliance, "min_request_interval_seconds", "compliance"
                ),
            ),
        )
        config._validate_basics()
        return config

    def _validate_basics(self) -> None:
        if self.schema_version != 1:
            raise ConfigError("unsupported config schema_version")
        if self.runtime.max_cycle_seconds <= 0:
            raise ConfigError("runtime.max_cycle_seconds must be positive")
        if self.inference.max_calls_per_cycle < 0:
            raise ConfigError("inference.max_calls_per_cycle cannot be negative")
        if self.network.max_requests_per_day <= 0:
            raise ConfigError("network.max_requests_per_day must be positive")
        if self.swarm.default_mode not in _SWARM_MODES:
            raise ConfigError(f"swarm.default_mode must be one of {sorted(_SWARM_MODES)}")
        normalized_workspace = Path(self.swarm.workspace_dir)
        if normalized_workspace.is_absolute() or ".." in normalized_workspace.parts:
            raise ConfigError("swarm.workspace_dir must be a relative path without '..'")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38})", self.swarm.repo_prefix):
            raise ConfigError("swarm.repo_prefix must be a short lowercase slug")
        for entry in self.runtime.state_commit_allowlist:
            if not entry.endswith("/") or ".." in Path(entry).parts:
                raise ConfigError("runtime.state_commit_allowlist entries must be safe prefixes")
        if self.evolution.auto_apply:
            raise InvariantViolation("evolution.auto_apply must remain false")
        if not self.evolution.require_human_review:
            raise InvariantViolation("evolution.require_human_review must remain true")
        if not self.evolution.verification_commands:
            raise ConfigError("evolution.verification_commands cannot be empty")
        if not self.compliance.allowed_licenses:
            raise ConfigError("compliance.allowed_licenses cannot be empty")
        for channel in self.monetization.channels:
            if channel.status not in _CHANNEL_STATUSES:
                raise ConfigError(f"invalid monetization status for {channel.id}")
            if channel.status == "active" and "OWNER_PLACEHOLDER" in channel.url:
                raise ConfigError(f"active channel {channel.id} still uses a placeholder URL")

    def active_niches(self) -> tuple[NicheConfig, ...]:
        return tuple(niche for niche in self.niches if niche.active)

    def niche_by_id(self, niche_id: str) -> NicheConfig:
        for niche in self.niches:
            if niche.id == niche_id:
                return niche
        raise ConfigError(f"unknown niche: {niche_id!r}")


def validate_system_config(config: SystemConfig, guard: InvariantGuard) -> None:
    invariants = guard.invariants
    guard.assert_visibility(config.repo.visibility)
    if config.billing.hard_cap_usd != invariants.hard_cap_usd:
        raise InvariantViolation("config billing cap must match the invariants cap")
    guard.assert_paid_resources_disabled(
        config.billing.paid_resources_enabled, context="config.billing.paid_resources_enabled"
    )
    guard.assert_paid_resources_disabled(
        config.billing.paid_apis_enabled, context="config.billing.paid_apis_enabled"
    )
    if not config.monetization.zero_upfront_only:
        raise InvariantViolation("monetization must remain zero-upfront-only")
    if config.monetization.payouts_automated:
        raise InvariantViolation("automated payouts are forbidden")
    if config.monetization.payment_credentials_in_repo:
        raise InvariantViolation("payment credentials must never live in the repository")
    if config.swarm.max_children > invariants.swarm_max_children:
        raise InvariantViolation("config swarm cap exceeds the invariants cap")
    if config.compliance.max_github_issues_per_day > invariants.max_github_issues_per_day:
        raise InvariantViolation("config issue cap exceeds the invariants cap")
    if config.network.max_requests_per_day > invariants.max_network_requests_per_day:
        raise InvariantViolation("config network cap exceeds the invariants cap")
    required_protected = set(invariants.protected_paths)
    if not required_protected.issubset(set(config.evolution.protected_paths)):
        raise InvariantViolation(
            "config protected_paths must include every invariant-protected path"
        )
    for prefix in config.runtime.state_commit_allowlist:
        normalized = normalize_repo_path(prefix)
        if not normalized:
            raise InvariantViolation(
                f"state commit allowlist entry {prefix!r} is not a safe path prefix"
            )
        base = normalized.rstrip("/")
        overlaps_protected = guard.is_protected_path(base) or any(
            protected.rstrip("/").startswith(base + "/")
            for protected in invariants.protected_paths
        )
        if overlaps_protected:
            raise InvariantViolation(
                f"state commit allowlist may not include protected path {prefix}"
            )
    for provider in config.inference.providers:
        if provider.kind != "deterministic":
            guard.assert_host_allowed(provider.base_url)
    for channel in config.monetization.channels:
        if channel.url:
            parsed = urlsplit(channel.url)
            if parsed.scheme != "https":
                raise ConfigError(f"monetization channel {channel.id} must use HTTPS")


def _parse_repo(document: dict[str, object]) -> RepoConfig:
    return RepoConfig(
        name=require_str(document, "name", "repo"),
        owner=require_str(document, "owner", "repo"),
        visibility=require_str(document, "visibility", "repo"),
        default_branch=require_str(document, "default_branch", "repo"),
    )


def _parse_billing(document: dict[str, object]) -> BillingConfig:
    return BillingConfig(
        currency=require_str(document, "currency", "billing"),
        hard_cap_usd=require_float(document, "hard_cap_usd", "billing"),
        paid_resources_enabled=require_bool(document, "paid_resources_enabled", "billing"),
        paid_apis_enabled=require_bool(document, "paid_apis_enabled", "billing"),
    )


def _parse_niche(item: object, index: int) -> NicheConfig:
    document = as_mapping(item, f"niches[{index}]")
    niche = NicheConfig(
        id=require_str(document, "id", f"niches[{index}]"),
        family=require_str(document, "family", f"niches[{index}]"),
        title=require_str(document, "title", f"niches[{index}]"),
        product_name=require_str(document, "product_name", f"niches[{index}]"),
        product_type=require_str(document, "product_type", f"niches[{index}]"),
        description=require_str(document, "description", f"niches[{index}]"),
        keywords=require_str_tuple(document, "keywords", f"niches[{index}]"),
        pain=require_float(document, "pain", f"niches[{index}]"),
        willingness_to_pay=require_float(document, "willingness_to_pay", f"niches[{index}]"),
        buildability=require_float(document, "buildability", f"niches[{index}]"),
        competition=require_float(document, "competition", f"niches[{index}]"),
        compliance_risk=require_float(document, "compliance_risk", f"niches[{index}]"),
        active=require_bool(document, "active", f"niches[{index}]"),
    )
    for field_name in (
        "pain",
        "willingness_to_pay",
        "buildability",
        "competition",
        "compliance_risk",
    ):
        value = getattr(niche, field_name)
        if not 0.0 <= value <= 1.0:
            raise ConfigError(f"niches[{index}].{field_name} must be between 0 and 1")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", niche.id):
        raise ConfigError(f"invalid niche id {niche.id!r}")
    return niche


def _parse_provider(document: dict[str, object]) -> ProviderConfig:
    name = require_str(document, "name", "provider")
    kind = require_str(document, "kind", f"provider {name}")
    if kind not in _PROVIDER_KINDS:
        raise ConfigError(f"provider {name} has unsupported kind {kind!r}")
    api_key_env = document.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ConfigError(f"provider {name}.api_key_env must be a string or null")
    provider = ProviderConfig(
        name=name,
        enabled=require_bool(document, "enabled", f"provider {name}"),
        kind=kind,
        base_url=require_str(document, "base_url", f"provider {name}").rstrip("/"),
        api_key_env=(api_key_env.strip() if api_key_env else None),
        models=require_str_tuple(document, "models", f"provider {name}"),
        max_requests_per_day=require_int(document, "max_requests_per_day", f"provider {name}"),
        max_tokens_per_request=require_int(document, "max_tokens_per_request", f"provider {name}"),
        timeout_seconds=require_float(document, "timeout_seconds", f"provider {name}"),
    )
    if not provider.models:
        raise ConfigError(f"provider {name} must list at least one model")
    if provider.kind in {"openai_compatible", "gemini"} and not provider.api_key_env:
        raise ConfigError(f"provider {name} must reference an api_key_env")
    return provider


def _parse_channel(document: dict[str, object]) -> MonetizationChannel:
    return MonetizationChannel(
        id=require_str(document, "id", "monetization channel"),
        label=require_str(document, "label", "monetization channel"),
        url=require_str(document, "url", "monetization channel"),
        status=require_str(document, "status", "monetization channel"),
        requires_human_setup=require_bool(document, "requires_human_setup", "monetization channel"),
    )


def _parse_commands(document: dict[str, object]) -> tuple[tuple[str, ...], ...]:
    raw = require_sequence(document, "verification_commands", "evolution")
    commands: list[tuple[str, ...]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, list):
            raise ConfigError(f"evolution.verification_commands[{index}] must be a list")
        command: list[str] = []
        for token in item:
            if not isinstance(token, str) or not token:
                raise ConfigError(f"evolution.verification_commands[{index}] must contain strings")
            command.append(token)
        if not command:
            raise ConfigError(f"evolution.verification_commands[{index}] cannot be empty")
        commands.append(tuple(command))
    return tuple(commands)


def _parse_heartbeat_workflow(document: dict[str, object]) -> HeartbeatWorkflow:
    return HeartbeatWorkflow(
        file=require_str(document, "file", "heartbeat.required_workflows"),
        max_age_hours=require_float(document, "max_age_hours", "heartbeat.required_workflows"),
    )
