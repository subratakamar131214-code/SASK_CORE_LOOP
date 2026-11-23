"""UTC time helpers with injectable clocks for deterministic tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_now(now: datetime | None = None) -> str:
    return to_iso(now or utc_now())


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def hours_since(timestamp: str, now: datetime | None = None) -> float:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    delta = current.astimezone(UTC) - parse_iso(timestamp)
    return delta.total_seconds() / 3600.0


def add_hours(timestamp: str, hours: float) -> str:
    return to_iso(parse_iso(timestamp) + timedelta(hours=hours))


def utc_day(now: datetime | None = None) -> str:
    return (now or utc_now()).astimezone(UTC).date().isoformat()
