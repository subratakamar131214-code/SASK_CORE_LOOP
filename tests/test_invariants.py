"""Invariant pinning, intrinsic safety checks, approvals, and config validation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from src.core.config import SystemConfig, validate_system_config
from src.core.errors import ApprovalRequired, InvariantViolation
from src.core.invariants import (
    APPROVAL_INVARIANT_CHANGE,
    APPROVAL_PAYOUT,
    APPROVAL_REPLICATION,
    PINNED_INVARIANTS_SHA256,
    Approval,
    InvariantGuard,
    Invariants,
    compute_digest,
)
from src.core.invariants import (
    main as invariants_main,
)

from helpers import make_approval, url_userinfo


def _variant(repo_root: Path, tmp_path: Path, old: str, new: str) -> Path:
    source = (repo_root / "config" / "invariants.yaml").read_text(encoding="utf-8")
    assert old in source
    target = tmp_path / "invariants.yaml"
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    return target


def test_pinned_digest_matches_repository_file(repo_root: Path, guard: InvariantGuard) -> None:
    pinned = compute_digest(repo_root / "config" / "invariants.yaml")
    assert pinned == PINNED_INVARIANTS_SHA256
    assert guard.digest == PINNED_INVARIANTS_SHA256


def test_invariants_encode_the_safety_contract(guard: InvariantGuard) -> None:
    rules = guard.invariants
    assert rules.hard_cap_usd == 0.0
    assert rules.allow_paid_resources is False
    assert rules.allow_paid_apis is False
    assert all(
        (
            rules.approvals.payouts,
            rules.approvals.payment_credentials,
            rules.approvals.replication,
            rules.approvals.invariant_change,
            rules.approvals.visibility_change,
        )
    )
    assert rules.sanitize_before_outbound is True
    assert rules.allow_pii_to_cloud is False
    assert rules.visibility == "public"
    assert rules.swarm_require_approval is True
    assert rules.one_niche_per_child is True


def test_modified_file_without_pin_update_is_rejected(repo_root: Path, tmp_path: Path) -> None:
    variant = _variant(repo_root, tmp_path, "visibility: public", "visibility: private")
    with pytest.raises(InvariantViolation, match="digest does not match"):
        Invariants.load(variant)


def test_weakened_invariants_rejected_even_with_updated_pin(
    repo_root: Path, tmp_path: Path
) -> None:
    variant = _variant(repo_root, tmp_path, "hard_cap_usd: 0.00", "hard_cap_usd: 1.00")
    digest = compute_digest(variant)
    with pytest.raises(InvariantViolation, match="refusing weakened invariants"):
        Invariants.load(variant, expected_digest=digest)


def test_zero_spend_is_the_only_valid_spend(guard: InvariantGuard) -> None:
    guard.assert_zero_spend(0)
    guard.assert_zero_spend(0.0)
    with pytest.raises(InvariantViolation, match="hard cap"):
        guard.assert_zero_spend(0.01)
    with pytest.raises(InvariantViolation, match="numeric"):
        guard.assert_zero_spend(True)  # type: ignore[arg-type]


def test_paid_resources_are_refused(guard: InvariantGuard) -> None:
    guard.assert_paid_resources_disabled(False)
    with pytest.raises(InvariantViolation, match="paid resources"):
        guard.assert_paid_resources_disabled(True)


def test_host_allowlist_and_https_requirements(guard: InvariantGuard) -> None:
    guard.assert_host_allowed("https://api.groq.com/openai/v1")
    guard.assert_host_allowed("https://generativelanguage.googleapis.com/v1beta")
    guard.assert_host_allowed("http://localhost:11434/api/chat")
    guard.assert_host_allowed("http://127.0.0.1:11434/api/chat")
    with pytest.raises(InvariantViolation, match="HTTPS"):
        guard.assert_host_allowed("http://api.groq.com/openai/v1")
    with pytest.raises(InvariantViolation, match="allowlist"):
        guard.assert_host_allowed("https://example.com/data")
    with pytest.raises(InvariantViolation, match="credentials"):
        guard.assert_host_allowed(url_userinfo())
    with pytest.raises(InvariantViolation, match="scheme"):
        guard.assert_host_allowed("ftp://api.groq.com/x")


def test_human_approvals_reject_bots_and_scope_mismatches(guard: InvariantGuard) -> None:
    approval = make_approval()
    guard.assert_human_approval(APPROVAL_REPLICATION, approval)
    with pytest.raises(ApprovalRequired, match="required"):
        guard.assert_human_approval(APPROVAL_REPLICATION, None)
    with pytest.raises(ApprovalRequired, match="scope mismatch"):
        guard.assert_human_approval(APPROVAL_PAYOUT, approval)
    with pytest.raises(InvariantViolation, match="unknown approval"):
        guard.assert_human_approval("made_up_action", approval)

    with pytest.raises(ApprovalRequired, match="bot identities"):
        make_approval(approved_by="github-actions[bot]")
    with pytest.raises(ApprovalRequired, match="bot identities"):
        make_approval(approved_by="dependabot")
    with pytest.raises(ApprovalRequired, match="approved_by"):
        make_approval(approved_by="   ")
    with pytest.raises(ValueError):
        make_approval(approved_at="not-a-timestamp")


def test_protected_paths_cannot_be_written_by_runtime(
    guard: InvariantGuard, repo_root: Path
) -> None:
    assert guard.is_protected_path("config/invariants.yaml")
    assert guard.is_protected_path("src/core/redact.py")
    assert guard.is_protected_path("src/compliance/secretscan.py")
    assert guard.is_protected_path(".github/workflows/revenue-cycle.yml")
    assert guard.is_protected_path("../outside-the-repo")
    assert not guard.is_protected_path("README.md")
    assert not guard.is_protected_path("docs/SECURITY.md")

    guard.assert_path_writable("docs/ARCHITECTURE.md")
    change_approval = make_approval(APPROVAL_INVARIANT_CHANGE)
    with pytest.raises(InvariantViolation, match="protected safety path"):
        guard.assert_path_writable("src/core/invariants.py", approval=change_approval)
    with pytest.raises(ApprovalRequired):
        guard.assert_path_writable("config/invariants.yaml")


def test_replication_gates(guard: InvariantGuard, approval: Approval) -> None:
    kwargs = {
        "children_count": 0,
        "requested_niche_id": "ci-failure-diagnoser",
        "active_niche_ids": [],
        "child_invariants_digest": guard.digest,
        "repo_mode": False,
    }
    guard.assert_replication_allowed(approval=approval, **kwargs)
    with pytest.raises(InvariantViolation, match="cap reached"):
        guard.assert_replication_allowed(
            approval=approval, **{**kwargs, "children_count": 3}
        )
    with pytest.raises(InvariantViolation, match="already locked"):
        guard.assert_replication_allowed(
            approval=approval,
            **{**kwargs, "active_niche_ids": ["ci-failure-diagnoser"]},
        )
    with pytest.raises(InvariantViolation, match="digest"):
        guard.assert_replication_allowed(
            approval=approval, **{**kwargs, "child_invariants_digest": "0" * 64}
        )
    with pytest.raises(ApprovalRequired):
        guard.assert_replication_allowed(approval=None, **kwargs)
    with pytest.raises(ApprovalRequired):
        guard.assert_replication_allowed(
            approval=make_approval(APPROVAL_PAYOUT), **kwargs
        )


def test_visibility_must_stay_public(guard: InvariantGuard) -> None:
    guard.assert_visibility("public")
    with pytest.raises(InvariantViolation, match="visibility"):
        guard.assert_visibility("private")


def test_shipped_config_passes_validation(config: SystemConfig, guard: InvariantGuard) -> None:
    validate_system_config(config, guard)


def test_config_cannot_raise_the_billing_cap(config: SystemConfig, guard: InvariantGuard) -> None:
    altered = replace(config, billing=replace(config.billing, hard_cap_usd=1.0))
    with pytest.raises(InvariantViolation, match="cap"):
        validate_system_config(altered, guard)


def test_config_cannot_enable_paid_services(config: SystemConfig, guard: InvariantGuard) -> None:
    paid_api = replace(config, billing=replace(config.billing, paid_apis_enabled=True))
    with pytest.raises(InvariantViolation, match="paid"):
        validate_system_config(paid_api, guard)

    paid_resource = replace(
        config, billing=replace(config.billing, paid_resources_enabled=True)
    )
    with pytest.raises(InvariantViolation, match="paid"):
        validate_system_config(paid_resource, guard)


def test_config_cannot_automate_money(config: SystemConfig, guard: InvariantGuard) -> None:
    payouts = replace(
        config, monetization=replace(config.monetization, payouts_automated=True)
    )
    with pytest.raises(InvariantViolation, match="payouts"):
        validate_system_config(payouts, guard)

    credentials = replace(
        config,
        monetization=replace(config.monetization, payment_credentials_in_repo=True),
    )
    with pytest.raises(InvariantViolation, match="credentials"):
        validate_system_config(credentials, guard)


def test_config_cannot_go_private(config: SystemConfig, guard: InvariantGuard) -> None:
    altered = replace(config, repo=replace(config.repo, visibility="private"))
    with pytest.raises(InvariantViolation, match="visibility"):
        validate_system_config(altered, guard)


def test_config_caps_cannot_exceed_invariants(config: SystemConfig, guard: InvariantGuard) -> None:
    swarm = replace(config, swarm=replace(config.swarm, max_children=99))
    with pytest.raises(InvariantViolation, match="swarm cap"):
        validate_system_config(swarm, guard)

    issues = replace(
        config,
        compliance=replace(config.compliance, max_github_issues_per_day=99),
    )
    with pytest.raises(InvariantViolation, match="issue cap"):
        validate_system_config(issues, guard)

    network = replace(
        config, network=replace(config.network, max_requests_per_day=10_000)
    )
    with pytest.raises(InvariantViolation, match="network cap"):
        validate_system_config(network, guard)


def test_config_must_keep_protected_paths_and_safe_allowlist(
    config: SystemConfig, guard: InvariantGuard
) -> None:
    narrowed = replace(
        config,
        evolution=replace(
            config.evolution, protected_paths=("config/invariants.yaml",)
        ),
    )
    with pytest.raises(InvariantViolation, match="protected_paths"):
        validate_system_config(narrowed, guard)

    unsafe = replace(
        config, runtime=replace(config.runtime, state_commit_allowlist=("config/",))
    )
    with pytest.raises(InvariantViolation, match="protected path"):
        validate_system_config(unsafe, guard)


def test_config_providers_must_use_allowed_hosts(
    config: SystemConfig, guard: InvariantGuard
) -> None:
    providers = tuple(
        replace(provider, base_url="https://api.openai.com/v1")
        if provider.name == "groq"
        else provider
        for provider in config.inference.providers
    )
    altered = replace(
        config, inference=replace(config.inference, providers=providers)
    )
    with pytest.raises(InvariantViolation):
        validate_system_config(altered, guard)


def test_invariants_cli_checks_the_live_file(repo_root: Path) -> None:
    path = repo_root / "config" / "invariants.yaml"
    assert invariants_main(["--path", str(path), "--check"]) == 0
