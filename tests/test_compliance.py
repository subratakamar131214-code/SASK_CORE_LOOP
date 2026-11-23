"""Licensing, robots.txt, rate limiting, anti-abuse, and artifact scanning."""

from __future__ import annotations

import pytest
from src.compliance.policy import (
    AntiAbusePolicy,
    LicenseChecker,
    PolicyEngine,
    RateLimiter,
    RobotsRules,
)
from src.compliance.secretscan import SecretFinding
from src.core.errors import ComplianceError, InvariantViolation


def test_license_allowlist_is_case_insensitive() -> None:
    checker = LicenseChecker(("MIT", "Apache-2.0"))
    assert checker.is_allowed("mit")
    assert checker.is_allowed("Apache-2.0")
    assert not checker.is_allowed("GPL-3.0")
    checker.assert_allowed("MIT")
    with pytest.raises(ComplianceError, match="allowlist"):
        checker.assert_allowed("GPL-3.0", subject="demo")


def test_rate_limiter_is_anchored_to_the_last_request() -> None:
    now = [0.0]
    limiter = RateLimiter(10.0, clock=lambda: now[0])
    assert limiter.seconds_until_allowed("api.groq.com") == 0.0
    assert limiter.seconds_until_allowed("api.groq.com") == 10.0
    now[0] = 4.0
    assert limiter.seconds_until_allowed("api.groq.com") == 6.0
    assert limiter.seconds_until_allowed("api.github.com") == 0.0


def test_robots_wildcard_group() -> None:
    rules = RobotsRules.parse(
        "# comment\nUser-agent: *\nDisallow: /private\nAllow: /private/public\n"
        "Crawl-delay: 3\n"
    )
    assert rules.crawl_delay == 3.0
    assert rules.allows("/")
    assert not rules.allows("/private/data")
    assert rules.allows("/private/public/page")


def test_robots_specific_agent_beats_wildcard() -> None:
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: SASK_CORE_LOOP\nDisallow: /admin\n"
    )
    rules = RobotsRules.parse(text)
    assert rules.allows("/public")
    assert not rules.allows("/admin/panel")
    assert not rules.disallow or "/admin" in rules.disallow

    other = RobotsRules.parse(text, user_agent="AnotherBot")
    assert not other.allows("/")


def test_policy_engine_requires_robots_for_unattended_fetch(
    config, guard
) -> None:
    engine = PolicyEngine(config.compliance, guard)
    url = "https://api.github.com/repos/example/repo"
    engine.check_web_fetch(url, "User-agent: *\nAllow: /\n")
    with pytest.raises(ComplianceError, match="robots.txt"):
        engine.check_web_fetch(url, None)
    with pytest.raises(ComplianceError, match="disallows"):
        engine.check_web_fetch(url, "User-agent: *\nDisallow: /\n")
    with pytest.raises(InvariantViolation):
        engine.check_web_fetch("https://example.com/", "User-agent: *\nAllow: /\n")


def test_policy_engine_honors_crawl_delay(config, guard) -> None:
    engine = PolicyEngine(config.compliance, guard)
    engine.check_web_fetch(
        "https://api.github.com/repos/example/repo",
        "User-agent: *\nAllow: /\nCrawl-delay: 5\n",
    )
    assert engine.rate_limiter.seconds_until_allowed("api.github.com") == 0.0
    assert engine.rate_limiter.seconds_until_allowed("api.github.com") >= 4.9


def test_anti_abuse_caps_and_outreach(config) -> None:
    policy = AntiAbusePolicy(config.compliance)
    policy.assert_issue_creation_allowed(0)
    with pytest.raises(ComplianceError, match="cap"):
        policy.assert_issue_creation_allowed(config.compliance.max_github_issues_per_day)
    with pytest.raises(ComplianceError, match="negative"):
        policy.assert_issue_creation_allowed(-1)
    policy.assert_batch_issue_creation(1)
    with pytest.raises(ComplianceError, match="bulk"):
        policy.assert_batch_issue_creation(2)
    with pytest.raises(ComplianceError, match="outreach"):
        policy.assert_outreach_allowed()


def test_artifact_findings_fail_closed(config, guard) -> None:
    engine = PolicyEngine(config.compliance, guard)
    engine.assert_artifacts_clean([])
    findings = [SecretFinding(path="dist/x.py", line=1, kind="github_token")]
    with pytest.raises(ComplianceError, match="secret scan failed"):
        engine.assert_artifacts_clean(findings)
