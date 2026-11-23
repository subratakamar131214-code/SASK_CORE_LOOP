"""Typed inference request/response records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from src.core.types import JsonObject

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def to_json(self) -> JsonObject:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


@dataclass(frozen=True)
class AttemptRecord:
    provider: str
    model: str
    outcome: str
    status: int | None
    sleep_seconds: float
    detail: str

    def to_json(self) -> JsonObject:
        return {
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "status": self.status,
            "sleep_seconds": round(self.sleep_seconds, 3),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Completion:
    text: str
    provider: str
    model: str
    fallback: bool
    cached: bool
    attempts: tuple[AttemptRecord, ...] = field(default_factory=tuple)
    usage: Usage | None = None

    def to_json(self) -> JsonObject:
        return {
            "text": self.text,
            "provider": self.provider,
            "model": self.model,
            "fallback": self.fallback,
            "cached": self.cached,
            "attempts": [attempt.to_json() for attempt in self.attempts],
            "usage": self.usage.to_json() if self.usage else None,
        }


@dataclass(frozen=True)
class InferenceRequest:
    task: str
    messages: tuple[Message, ...]
    max_tokens: int | None = None
    temperature: float = 0.2
