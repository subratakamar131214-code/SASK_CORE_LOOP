"""Rate-limit, licensing, robots.txt, anti-abuse, and distribution policy checks."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from src.compliance.secretscan import SecretFinding, assert_clean
from src.core.config import ComplianceConfig
from src.core.errors import ComplianceError
from src.core.invariants import InvariantGuard


@dataclass(frozen=True)
class RobotsRules:
    allow: tuple[str, ...]
    disallow: tuple[str, ...]
    crawl_delay: float | None

    @classmethod
    def parse(cls, text: str, *, user_agent: str = "SASK_CORE_LOOP") -> RobotsRules:
        groups: list[tuple[list[str], list[str], list[str], float | None]] = []
        agents: list[str] = []
        allow: list[str] = []
        disallow: list[str] = []
        delay: float | None = None

        def flush() -> None:
            nonlocal agents, allow, disallow, delay
            if agents:
                groups.append((agents, allow, disallow, delay))
            agents, allow, disallow = [], [], []
            delay = None

        for raw_line in text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if allow or disallow:
                    flush()
                agents.append(value)
            elif key in {"allow", "disallow"}:
                if value:
                    (allow if key == "allow" else disallow).append(value)
            elif key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    delay = None
        flush()

        wanted = user_agent.lower()
        specific = [group for group in groups if any(a.lower() == wanted for a in group[0])]
        wildcard = [group for group in groups if any(a.strip() == "*" for a in group[0])]
        chosen = specific or wildcard
        chosen_allow = tuple(rule for group in chosen for rule in group[1])
        chosen_disallow = tuple(rule for group in chosen for rule in group[2])
        delays = [group[3] for group in chosen if group[3] is not None]
        return cls(
            allow=chosen_allow,
            disallow=chosen_disallow,
            crawl_delay=max(delays) if delays else None,
        )

    def allows(self, path: str) -> bool:
        target = path or "/"
        best_length = -1
        allowed = True
        rules = [(rule, True) for rule in self.allow] + [(rule, False) for rule in self.disallow]
        for rule, verdict in rules:
            if not _robots_match(rule, target):
                continue
            length = len(rule)
            if length > best_length or (length == best_length and verdict):
                best_length = length
                allowed = verdict
        return allowed


def _robots_match(rule: str, path: str) -> bool:
    pattern = re.escape(rule).replace(r"\*", ".*")
    if pattern.endswith(r"\$"):
        pattern = pattern[:-2] + "$"
    else:
        pattern += ".*" if rule.endswith("*") else ""
    return re.match(pattern, path) is not None


class RateLimiter:
    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = max(0.0, min_interval_seconds)
        self._clock = clock
        self._last: dict[str, float] = {}

    def seconds_until_allowed(self, host: str) -> float:
        now = self._clock()
        previous = self._last.get(host)
        wait = 0.0 if previous is None else max(0.0, self._interval - (now - previous))
        self._last[host] = now
        return wait


class LicenseChecker:
    def __init__(self, allowed: tuple[str, ...]) -> None:
        self._allowed = {license_name.lower() for license_name in allowed}

    @property
    def allowed_licenses(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    def is_allowed(self, spdx: str) -> bool:
        return spdx.strip().lower() in self._allowed

    def assert_allowed(self, spdx: str, *, subject: str = "artifact") -> None:
        if not self.is_allowed(spdx):
            raise ComplianceError(
                f"{subject} license {spdx!r} is not on the allowlist {self.allowed_licenses}"
            )


class AntiAbusePolicy:
    def __init__(self, config: ComplianceConfig) -> None:
        self._config = config

    def assert_issue_creation_allowed(self, created_today: int) -> None:
        if created_today < 0:
            raise ComplianceError("issue counters cannot be negative")
        if (
            self._config.forbid_bulk_issue_creation
            and created_today >= self._config.max_github_issues_per_day
        ):
            raise ComplianceError(
                f"daily issue creation cap reached ({created_today}/"
                f"{self._config.max_github_issues_per_day})"
            )

    def assert_batch_issue_creation(self, count: int) -> None:
        if count > 1:
            raise ComplianceError("bulk or batch issue creation is forbidden")
        if count < 0:
            raise ComplianceError("issue count cannot be negative")

    def assert_outreach_allowed(self) -> None:
        if self._config.forbid_unsolicited_outreach:
            raise ComplianceError(
                "unsolicited outreach is disabled; only opt-in channels may be used by a human"
            )


class PolicyEngine:
    def __init__(self, config: ComplianceConfig, guard: InvariantGuard) -> None:
        self._config = config
        self._guard = guard
        self.rate_limiter = RateLimiter(config.min_request_interval_seconds)
        self.licenses = LicenseChecker(config.allowed_licenses)
        self.anti_abuse = AntiAbusePolicy(config)

    @property
    def config(self) -> ComplianceConfig:
        return self._config

    def check_outbound_url(self, url: str) -> None:
        self._guard.assert_host_allowed(url)

    def check_web_fetch(self, url: str, robots_text: str | None) -> None:
        self.check_outbound_url(url)
        if not self._config.respect_robots_txt:
            return
        if robots_text is None:
            raise ComplianceError(
                "robots.txt was not supplied; refusing an unattended web fetch without it"
            )
        rules = RobotsRules.parse(robots_text)
        path = urlsplit(url).path or "/"
        if not rules.allows(path):
            raise ComplianceError(f"robots.txt disallows fetching {path}")
        if rules.crawl_delay and rules.crawl_delay > 0:
            self.rate_limiter = RateLimiter(
                max(self._config.min_request_interval_seconds, rules.crawl_delay)
            )

    def assert_artifacts_clean(self, findings: list[SecretFinding]) -> None:
        assert_clean(findings)
