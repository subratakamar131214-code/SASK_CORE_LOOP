"""Typed exception hierarchy used across the core loop."""

from __future__ import annotations


class SASKError(Exception):
    """Base class for all expected SASK_CORE_LOOP failures."""


class ConfigError(SASKError):
    """Configuration is missing, malformed, or inconsistent with invariants."""


class InvariantViolation(SASKError):
    """A non-negotiable safety invariant would be violated."""


class ApprovalRequired(InvariantViolation):
    """A human approval gate was not satisfied."""


class ComplianceError(SASKError):
    """A platform, licensing, rate-limit, or anti-abuse rule blocked an action."""


class BudgetExceeded(SASKError):
    """A free-tier or per-cycle request budget was exhausted."""


class StateError(SASKError):
    """Persistent state is corrupt, locked, or unreadable."""


class SanitizationError(SASKError):
    """Sensitive data remained after sanitization; the write is refused."""


class VerificationError(SASKError):
    """A deterministic verification gate failed."""
