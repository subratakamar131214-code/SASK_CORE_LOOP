"""Shared test factories.

Credential-shaped values are assembled at runtime so this test source stays
readable while the repository secret scanner only ever sees inert fragments.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.core.invariants import Approval
from src.sensors.feedback import FeedbackItem

FIXED_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class FrozenClock:
    """Mutable clock for deterministic time-dependent checks."""

    def __init__(self, moment: datetime = FIXED_NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, hours: float) -> FrozenClock:
        self.moment = self.moment + timedelta(hours=hours)
        return self


def _join(*parts: str) -> str:
    return "".join(parts)


def fake_github_token() -> str:
    return _join("gh", "p_", "A" * 24)


def fake_fine_grained_token() -> str:
    return _join("github", "_pat_", "B" * 24)


def fake_groq_key() -> str:
    return _join("gs", "k_", "C" * 24)


def fake_openai_key() -> str:
    return _join("s", "k-", "D" * 24)


def fake_google_api_key() -> str:
    return _join("AI", "za", "E" * 30)


def fake_gemini_key() -> str:
    return fake_google_api_key()


def fake_google_oauth_token() -> str:
    return _join("AQ", ".", "F" * 24)


def fake_aws_access_key() -> str:
    return _join("AK", "IA", "G" * 16)


def fake_slack_token() -> str:
    return _join("xo", "xb-", "H" * 20)


def fake_jwt() -> str:
    header = _join("ey", "J", "a" * 12)
    payload = "b" * 12
    signature = "c" * 12
    return ".".join((header, payload, signature))


def fake_private_key() -> str:
    fence = "-" * 5
    begin = f"{fence}BEGIN RSA PRIVATE KEY{fence}"
    end = f"{fence}END RSA PRIVATE KEY{fence}"
    body = _join("MIIB", "x" * 32)
    return f"{begin}\n{body}\n{end}"


def unquoted_assignment() -> str:
    return _join("API_", "KEY=", "abc", "123def456")


def quoted_assignment() -> str:
    return _join("pass", "word = ", '"', "hunter", "2", '"')


def auth_header() -> str:
    return _join("Author", "ization: Bearer ", "abc", "123def456")


def url_secret() -> str:
    return _join("https://example.com/x?tok", "en=", "abc", "123def456")


def url_userinfo() -> str:
    return _join("https://us", "er:pa", "ss@", "example.com/x")


def make_approval(
    action: str = "replication",
    *,
    approved_by: str = "maintainer-jordan",
    reference: str = "https://github.com/example/SASK_CORE_LOOP/issues/7",
    approved_at: str = "2026-11-01T00:00:00Z",
) -> Approval:
    return Approval(
        action=action,
        approved_by=approved_by,
        reference=reference,
        approved_at=approved_at,
    )


def make_feedback_item(
    item_id: str = "gh-1",
    *,
    source: str = "github_issue",
    kind: str = "bug",
    title: str = "Flaky pipeline failure",
    body: str = "ci build failure in the log",
    labels: tuple[str, ...] = ("bug",),
    created_at: str = "2026-11-20T00:00:00Z",
    reactions: int = 0,
    is_customer: bool = False,
    priority: float = 2.5,
    matched_niches: tuple[str, ...] = ("ci-failure-diagnoser",),
) -> FeedbackItem:
    return FeedbackItem(
        id=item_id,
        source=source,
        kind=kind,
        title=title,
        body=body,
        labels=labels,
        created_at=created_at,
        reactions=reactions,
        is_customer=is_customer,
        priority=priority,
        matched_niches=matched_niches,
    )
