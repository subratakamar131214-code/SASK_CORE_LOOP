"""Immutable, code-pinned safety invariants and their enforcement guard.

The digest below is computed over the canonical JSON encoding of the parsed
`config/invariants.yaml` document. Any semantic change to the invariants must be a
human-reviewed commit that updates this pin. In addition, intrinsic safety checks
reject any invariants file that attempts to weaken the zero-spend, approval,
redaction, or replication rules, even if a new digest were pinned incorrectly.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from src.core.errors import ApprovalRequired, ConfigError, InvariantViolation
from src.core.io import canonical_json, sha256_hex
from src.core.timeutil import parse_iso
from src.core.types import JsonValue
from src.core.yamlutil import (
    load_yaml_mapping,
    require_bool,
    require_float,
    require_int,
    require_mapping,
    require_str,
    require_str_tuple,
)

PINNED_INVARIANTS_SHA256 = "408ff94e97806e13d8faecac8ac4f1f25d126c15b16f8347ad5079ba5ebd8705"

_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PAID_AI_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "api.mistral.ai",
        "api.cohere.ai",
        "api.together.xyz",
        "openrouter.ai",
        "api.replicate.com",
    }
)

APPROVAL_PAYOUT = "payout"
APPROVAL_PAYMENT_CREDENTIALS = "payment_credentials"
APPROVAL_REPLICATION = "replication"
APPROVAL_INVARIANT_CHANGE = "invariant_change"
APPROVAL_VISIBILITY_CHANGE = "visibility_change"


@dataclass(frozen=True)
class ApprovalRules:
    payouts: bool
    payment_credentials: bool
    replication: bool
    invariant_change: bool
    visibility_change: bool
    reject_bots: bool


@dataclass(frozen=True)
class Approval:
    action: str
    approved_by: str
    reference: str
    approved_at: str

    def __post_init__(self) -> None:
        for name in ("action", "approved_by", "reference", "approved_at"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ApprovalRequired(f"approval.{name} must be a non-empty human-provided value")
        parse_iso(self.approved_at)
        lowered = self.approved_by.strip().lower()
        bot_markers = ("[bot]", "github-actions", "dependabot", "renovate-bot", "actions-bot")
        if any(marker in lowered for marker in bot_markers):
            raise ApprovalRequired("bot identities cannot satisfy a human approval gate")


@dataclass(frozen=True)
class Invariants:
    schema_version: int
    hard_cap_usd: float
    allow_paid_resources: bool
    allow_paid_apis: bool
    approvals: ApprovalRules
    redact_before_write: bool
    sanitize_pii_public: bool
    sanitize_before_outbound: bool
    allow_pii_to_cloud: bool
    forbid_private_customer_data_public: bool
    visibility: str
    forbid_secrets_in_git: bool
    protected_paths: tuple[str, ...]
    allowed_hosts: frozenset[str]
    require_https_except_localhost: bool
    max_network_requests_per_day: int
    forbid_paid_ai_hosts: bool
    replication_allowed: bool
    swarm_max_children: int
    one_niche_per_child: bool
    swarm_require_approval: bool
    allow_child_invariant_override: bool
    allow_repo_creation_by_default: bool
    child_must_pin_parent_invariants: bool
    forbid_bulk_issues: bool
    max_github_issues_per_day: int
    require_license_allowlist: bool
    respect_robots_txt: bool
    forbid_unsolicited_outreach: bool
    owner_vault: str
    system_treasury: str
    owner_vault_untouchable: bool
    human_only_tasks: tuple[str, ...]
    system_may_prepare_handoffs: bool
    interview_insights_highest_priority: bool
    bootstrap_spend_usd: float
    outreach_auto_send_enabled: bool
    allowed_channel_kinds: tuple[str, ...]
    automated_payouts_allowed: bool
    capital_research_only: bool
    live_trading_enabled: bool
    live_investing_enabled: bool
    require_research_compliance_verification: bool
    require_broker_verification: bool
    max_live_loss_usd: float
    owner_vault_excluded_from_research: bool
    digest: str

    @classmethod
    def load(cls, path: Path, *, expected_digest: str | None = None) -> Invariants:
        document = load_yaml_mapping(path)
        digest = sha256_hex(canonical_json(cast(JsonValue, document)))
        expected = expected_digest or PINNED_INVARIANTS_SHA256
        if not _HEX_DIGEST.match(expected) or expected == "0" * 64:
            raise ConfigError("invariants digest pin is not configured in src/core/invariants.py")
        if digest != expected:
            raise InvariantViolation(
                "config/invariants.yaml digest does not match the pinned digest; "
                "refusing to run. A human must review and update the pin."
            )

        billing = require_mapping(document, "billing", "invariants")
        approvals_doc = require_mapping(document, "approvals", "invariants")
        data = require_mapping(document, "data", "invariants")
        repository = require_mapping(document, "repository", "invariants")
        network = require_mapping(document, "network", "invariants")
        swarm = require_mapping(document, "swarm", "invariants")
        compliance = require_mapping(document, "compliance", "invariants")
        ownership = require_mapping(document, "ownership", "invariants")
        outreach = require_mapping(document, "outreach", "invariants")
        monetization = require_mapping(document, "monetization", "invariants")
        capital_research = require_mapping(document, "capital_research", "invariants")

        allowed_hosts = frozenset(
            host.lower() for host in require_str_tuple(network, "allowed_hosts", "network")
        )
        invariants = cls(
            schema_version=require_int(document, "schema_version", "invariants"),
            hard_cap_usd=require_float(billing, "hard_cap_usd", "billing"),
            allow_paid_resources=require_bool(billing, "allow_paid_resources", "billing"),
            allow_paid_apis=require_bool(billing, "allow_paid_apis", "billing"),
            approvals=ApprovalRules(
                payouts=require_bool(
                    approvals_doc, "require_human_for_real_money_payouts", "approvals"
                ),
                payment_credentials=require_bool(
                    approvals_doc, "require_human_for_payment_credentials", "approvals"
                ),
                replication=require_bool(
                    approvals_doc, "require_human_for_replication", "approvals"
                ),
                invariant_change=require_bool(
                    approvals_doc, "require_human_for_invariant_change", "approvals"
                ),
                visibility_change=require_bool(
                    approvals_doc, "require_human_for_visibility_change", "approvals"
                ),
                reject_bots=require_bool(approvals_doc, "reject_bot_approvers", "approvals"),
            ),
            redact_before_write=require_bool(data, "redact_secrets_before_write", "data"),
            sanitize_pii_public=require_bool(data, "sanitize_pii_before_public_write", "data"),
            sanitize_before_outbound=require_bool(
                data, "sanitize_before_outbound_inference", "data"
            ),
            allow_pii_to_cloud=require_bool(data, "allow_pii_to_cloud", "data"),
            forbid_private_customer_data_public=require_bool(
                data, "forbid_private_customer_data_in_public_commits", "data"
            ),
            visibility=require_str(repository, "visibility", "repository"),
            forbid_secrets_in_git=require_bool(repository, "forbid_secrets_in_git", "repository"),
            protected_paths=require_str_tuple(repository, "protected_paths", "repository"),
            allowed_hosts=allowed_hosts,
            require_https_except_localhost=require_bool(
                network, "require_https_except_localhost", "network"
            ),
            max_network_requests_per_day=require_int(network, "max_requests_per_day", "network"),
            forbid_paid_ai_hosts=require_bool(network, "forbid_paid_ai_hosts", "network"),
            replication_allowed=require_bool(swarm, "replication_allowed", "swarm"),
            swarm_max_children=require_int(swarm, "max_children", "swarm"),
            one_niche_per_child=require_bool(swarm, "one_niche_per_child", "swarm"),
            swarm_require_approval=require_bool(swarm, "require_human_approval", "swarm"),
            allow_child_invariant_override=require_bool(
                swarm, "allow_child_invariant_override", "swarm"
            ),
            allow_repo_creation_by_default=require_bool(
                swarm, "allow_repo_creation_by_default", "swarm"
            ),
            child_must_pin_parent_invariants=require_bool(
                swarm, "child_must_pin_parent_invariants", "swarm"
            ),
            forbid_bulk_issues=require_bool(compliance, "forbid_bulk_issue_creation", "compliance"),
            max_github_issues_per_day=require_int(
                compliance, "max_github_issues_per_day", "compliance"
            ),
            require_license_allowlist=require_bool(
                compliance, "require_license_allowlist", "compliance"
            ),
            respect_robots_txt=require_bool(compliance, "respect_robots_txt", "compliance"),
            forbid_unsolicited_outreach=require_bool(
                compliance, "forbid_unsolicited_outreach", "compliance"
            ),
            owner_vault=require_str(ownership, "owner_vault", "ownership"),
            system_treasury=require_str(ownership, "system_treasury", "ownership"),
            owner_vault_untouchable=require_bool(
                ownership, "owner_vault_untouchable", "ownership"
            ),
            human_only_tasks=require_str_tuple(ownership, "human_only_tasks", "ownership"),
            system_may_prepare_handoffs=require_bool(
                ownership, "system_may_prepare_handoffs", "ownership"
            ),
            interview_insights_highest_priority=require_bool(
                ownership, "interview_insights_highest_priority", "ownership"
            ),
            bootstrap_spend_usd=require_float(
                ownership, "bootstrap_spend_usd", "ownership"
            ),
            outreach_auto_send_enabled=require_bool(
                outreach, "auto_send_enabled", "outreach"
            ),
            allowed_channel_kinds=require_str_tuple(
                monetization, "allowed_channel_kinds", "monetization"
            ),
            automated_payouts_allowed=require_bool(
                monetization, "automated_payouts_allowed", "monetization"
            ),
            capital_research_only=require_bool(
                capital_research, "research_only", "capital_research"
            ),
            live_trading_enabled=require_bool(
                capital_research, "live_trading_enabled", "capital_research"
            ),
            live_investing_enabled=require_bool(
                capital_research, "live_investing_enabled", "capital_research"
            ),
            require_research_compliance_verification=require_bool(
                capital_research,
                "require_human_compliance_verification",
                "capital_research",
            ),
            require_broker_verification=require_bool(
                capital_research, "require_broker_verification", "capital_research"
            ),
            max_live_loss_usd=require_float(
                capital_research, "max_live_loss_usd", "capital_research"
            ),
            owner_vault_excluded_from_research=require_bool(
                capital_research, "owner_vault_excluded", "capital_research"
            ),
            digest=digest,
        )
        invariants._enforce_intrinsic_safety()
        return invariants

    def _enforce_intrinsic_safety(self) -> None:
        checks: tuple[tuple[bool, str], ...] = (
            (self.schema_version == 1, "unsupported invariants schema_version"),
            (self.hard_cap_usd == 0.0, "hard_cap_usd must be exactly 0.00"),
            (not self.allow_paid_resources, "paid resources must remain disabled"),
            (not self.allow_paid_apis, "paid APIs must remain disabled"),
            (
                self.approvals.payouts
                and self.approvals.payment_credentials
                and self.approvals.replication
                and self.approvals.invariant_change
                and self.approvals.visibility_change,
                "all human approval gates must remain enabled",
            ),
            (self.approvals.reject_bots, "bot approvals must remain rejected"),
            (self.redact_before_write, "secret redaction before writes must remain enabled"),
            (self.sanitize_pii_public, "PII sanitization for public writes must remain enabled"),
            (
                self.sanitize_before_outbound,
                "sanitization before outbound inference must remain enabled",
            ),
            (not self.allow_pii_to_cloud, "PII must never be allowed to cloud endpoints"),
            (
                self.forbid_private_customer_data_public,
                "private customer data must remain forbidden from public commits",
            ),
            (
                self.visibility == "public",
                "repository must remain public for free-runner operation",
            ),
            (self.forbid_secrets_in_git, "secrets must remain forbidden in git"),
            (bool(self.allowed_hosts), "network allowlist cannot be empty"),
            (self.require_https_except_localhost, "HTTPS must remain required for remote hosts"),
            (self.forbid_paid_ai_hosts, "paid AI hosts must remain forbidden"),
            (self.replication_allowed, "replication governance must remain available"),
            (self.swarm_max_children > 0, "swarm child cap must be positive"),
            (self.swarm_max_children <= 10, "swarm child cap must remain small"),
            (self.one_niche_per_child, "one-niche-per-child must remain enforced"),
            (self.swarm_require_approval, "replication must require human approval"),
            (
                not self.allow_child_invariant_override,
                "children must never override parent invariants",
            ),
            (
                self.child_must_pin_parent_invariants,
                "children must pin parent invariants",
            ),
            (self.forbid_bulk_issues, "bulk issue creation must remain forbidden"),
            (0 <= self.max_github_issues_per_day <= 10, "issue creation cap must be small"),
            (self.require_license_allowlist, "license allowlist must remain enforced"),
            (self.respect_robots_txt, "robots.txt must remain respected"),
            (self.forbid_unsolicited_outreach, "unsolicited outreach must remain forbidden"),
            (self.owner_vault != self.system_treasury, "OWNER_VAULT and treasury must differ"),
            (self.owner_vault_untouchable, "OWNER_VAULT must remain untouchable"),
            (self.bootstrap_spend_usd == 0.0, "bootstrap spend must remain 0.00"),
            (
                self.system_may_prepare_handoffs,
                "the system must prepare human handoff packets",
            ),
            (
                self.interview_insights_highest_priority,
                "human interview insights must remain top priority",
            ),
            (
                not self.outreach_auto_send_enabled,
                "outreach must require explicit human review and manual sending",
            ),
            (
                set(self.allowed_channel_kinds)
                == {
                    "marketplace",
                    "bounty",
                    "paid_template",
                    "custom_audit",
                    "recurring_saas",
                },
                "all five monetization channel kinds must remain allowed",
            ),
            (
                not self.automated_payouts_allowed,
                "automated payouts must remain forbidden",
            ),
            (self.capital_research_only, "capital research must remain research-only"),
            (
                not self.live_trading_enabled,
                "live trading must remain disabled without a human-reviewed invariant change",
            ),
            (
                not self.live_investing_enabled,
                "live investing must remain disabled without a human-reviewed invariant change",
            ),
            (
                self.require_research_compliance_verification
                and self.require_broker_verification,
                "research activation must require human compliance and broker verification",
            ),
            (self.max_live_loss_usd == 0.0, "live research loss cap must remain 0.00"),
            (
                self.owner_vault_excluded_from_research,
                "OWNER_VAULT must remain excluded from research capital",
            ),
            (
                {
                    "payment_and_billing_setup",
                    "legal_entity_and_contracts",
                    "tax_registration_and_filings",
                    "design_partner_calls",
                    "word_of_mouth",
                    "broker_and_exchange_compliance",
                    "live_trading_activation",
                }.issubset(set(self.human_only_tasks)),
                "human-only ownership must cover payment, legal, tax, discovery, "
                "trust, and trading activation",
            ),
        )
        for ok, message in checks:
            if not ok:
                raise InvariantViolation(f"refusing weakened invariants: {message}")


def compute_digest(path: Path) -> str:
    document = load_yaml_mapping(path)
    return sha256_hex(canonical_json(cast(JsonValue, document)))


def normalize_repo_path(path: str | Path, root: Path | None = None) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute():
        if root is None:
            return None
        try:
            candidate = candidate.resolve().relative_to(root.resolve())
        except ValueError:
            return None
    parts = [part for part in candidate.as_posix().split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return None
    return "/".join(parts)


class InvariantGuard:
    """Single enforcement point for all non-negotiable rules."""

    def __init__(self, invariants: Invariants, *, root: Path | None = None) -> None:
        self._invariants = invariants
        self._root = root

    @classmethod
    def load(cls, path: Path, *, root: Path | None = None) -> InvariantGuard:
        return cls(Invariants.load(path), root=root)

    @property
    def invariants(self) -> Invariants:
        return self._invariants

    @property
    def digest(self) -> str:
        return self._invariants.digest

    def assert_zero_spend(self, amount_usd: float, *, context: str = "entry") -> None:
        if not isinstance(amount_usd, (int, float)) or isinstance(amount_usd, bool):
            raise InvariantViolation(f"{context}: spend amount must be numeric")
        if float(amount_usd) != 0.0:
            raise InvariantViolation(
                f"{context}: spend of {amount_usd} USD violates the $0.00 hard cap"
            )

    def assert_paid_resources_disabled(self, paid: bool, *, context: str = "resource") -> None:
        if paid:
            raise InvariantViolation(f"{context}: paid resources are disabled by invariants")

    def assert_host_allowed(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            raise InvariantViolation(f"unsupported URL scheme: {parsed.scheme!r}")
        if parsed.username or parsed.password:
            raise InvariantViolation("URLs containing credentials are forbidden")
        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise InvariantViolation("URL has no hostname")
        is_local = hostname in {"localhost", "127.0.0.1"}
        if (
            parsed.scheme == "http"
            and not is_local
            and self._invariants.require_https_except_localhost
        ):
            raise InvariantViolation("remote hosts must use HTTPS")
        if hostname not in self._invariants.allowed_hosts:
            raise InvariantViolation(f"host {hostname!r} is not on the invariants allowlist")
        if self._invariants.forbid_paid_ai_hosts and (
            hostname in _PAID_AI_HOSTS or hostname.endswith(".openai.azure.com")
        ):
            raise InvariantViolation(f"paid AI endpoint {hostname!r} is forbidden")

    def is_protected_path(self, path: str | Path) -> bool:
        normalized = normalize_repo_path(path, self._root)
        if normalized is None:
            return True
        for protected in self._invariants.protected_paths:
            prefix = protected.rstrip("/")
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
        return False

    def assert_path_writable(self, path: str | Path, *, approval: Approval | None = None) -> None:
        if not self.is_protected_path(path):
            return
        self.assert_human_approval(APPROVAL_INVARIANT_CHANGE, approval)
        normalized = normalize_repo_path(path, self._root) or str(path)
        raise InvariantViolation(
            f"{normalized} is a protected safety path; even with approval it can only be "
            "changed through a human-reviewed commit, never by runtime code"
        )

    def assert_human_approval(self, action: str, approval: Approval | None) -> None:
        rule = {
            APPROVAL_PAYOUT: self._invariants.approvals.payouts,
            APPROVAL_PAYMENT_CREDENTIALS: self._invariants.approvals.payment_credentials,
            APPROVAL_REPLICATION: self._invariants.approvals.replication,
            APPROVAL_INVARIANT_CHANGE: self._invariants.approvals.invariant_change,
            APPROVAL_VISIBILITY_CHANGE: self._invariants.approvals.visibility_change,
        }
        if action not in rule:
            raise InvariantViolation(f"unknown approval action: {action!r}")
        if not rule[action]:
            raise InvariantViolation(f"approval gate {action!r} is disabled; refusing to proceed")
        if approval is None:
            raise ApprovalRequired(f"human approval required for action {action!r}")
        if approval.action != action:
            raise ApprovalRequired(
                f"approval scope mismatch: got {approval.action!r}, need {action!r}"
            )

    def assert_replication_allowed(
        self,
        *,
        approval: Approval | None,
        children_count: int,
        requested_niche_id: str,
        active_niche_ids: Iterable[str],
        child_invariants_digest: str,
        repo_mode: bool,
    ) -> None:
        if not self._invariants.replication_allowed:
            raise InvariantViolation("replication is disabled by invariants")
        if self._invariants.swarm_require_approval:
            self.assert_human_approval(APPROVAL_REPLICATION, approval)
        if children_count >= self._invariants.swarm_max_children:
            raise InvariantViolation(
                f"swarm child cap reached ({children_count}/{self._invariants.swarm_max_children})"
            )
        if not requested_niche_id.strip():
            raise InvariantViolation("every child must lock exactly one niche")
        if self._invariants.one_niche_per_child and requested_niche_id in set(active_niche_ids):
            raise InvariantViolation(
                f"niche {requested_niche_id!r} is already locked by an active child"
            )
        if child_invariants_digest != self.digest:
            raise InvariantViolation("child invariants digest must match the parent digest")
        if repo_mode and not approval:
            raise ApprovalRequired("child repository creation requires explicit human approval")

    def assert_child_invariants(self, child_digest: str) -> None:
        if child_digest != self.digest:
            raise InvariantViolation("child invariants do not match the parent pin")

    def assert_visibility(self, visibility: str) -> None:
        if self._invariants.visibility == "public" and visibility != "public":
            raise InvariantViolation("repository visibility must remain public")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify SASK_CORE_LOOP invariants")
    parser.add_argument("--path", default="config/invariants.yaml")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--print-digest", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.path)
    digest = compute_digest(path)
    if args.print_digest:
        print(digest)
    if args.check:
        Invariants.load(path)
        print(f"invariants OK: {digest}")
    if not args.check and not args.print_digest:
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
