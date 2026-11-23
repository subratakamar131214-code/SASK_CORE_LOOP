"""Provider failover router: Groq -> Gemini -> Ollama -> deterministic."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

from src.compliance.policy import PolicyEngine
from src.core.config import InferenceConfig, ProviderConfig
from src.core.errors import ConfigError
from src.core.invariants import InvariantGuard
from src.core.io import canonical_json, sha256_hex
from src.core.redact import assert_no_secrets, redact_text
from src.core.types import JsonObject, JsonValue
from src.inference.backoff import BackoffPolicy
from src.inference.budget import DailyBudget
from src.inference.cache import ResponseCache
from src.inference.providers import (
    ChatProvider,
    DeterministicProvider,
    GeminiProvider,
    GroqProvider,
    OllamaProvider,
    ProviderError,
)
from src.inference.transport import Transport, UrllibTransport
from src.inference.types import AttemptRecord, Completion, InferenceRequest, Message


@dataclass
class RouterStats:
    network_calls: int = 0
    cache_hits: int = 0
    fallbacks: int = 0
    skipped: int = 0
    per_provider: dict[str, int] = field(default_factory=dict)

    def record_attempt(self, provider: str) -> None:
        self.per_provider[provider] = self.per_provider.get(provider, 0) + 1

    def to_json(self) -> JsonObject:
        return {
            "network_calls": self.network_calls,
            "cache_hits": self.cache_hits,
            "fallbacks": self.fallbacks,
            "skipped": self.skipped,
            "per_provider": dict(sorted(self.per_provider.items())),
        }


def _message_objects(messages: tuple[Message, ...]) -> list[JsonValue]:
    objects: list[JsonValue] = []
    for message in messages:
        item: JsonObject = {"role": message.role, "content": message.content}
        objects.append(item)
    return objects


class InferenceRouter:
    def __init__(
        self,
        config: InferenceConfig,
        guard: InvariantGuard,
        *,
        allow_network: bool = False,
        policy: PolicyEngine | None = None,
        transport: Transport | None = None,
        budget: DailyBudget | None = None,
        cache: ResponseCache | None = None,
        environ: Mapping[str, str] | None = None,
        sleep: Callable[[float], None] | None = None,
        user_agent: str = "SASK_CORE_LOOP/0.1.0",
    ) -> None:
        if allow_network and budget is None:
            raise ConfigError("network inference requires a persistent daily budget")
        self._config = config
        self._guard = guard
        self._allow_network = allow_network
        self._policy = policy
        self._budget = budget
        self._cache = cache
        self._environ = environ if environ is not None else os.environ
        self._sleep = sleep if sleep is not None else time.sleep
        self._backoff = BackoffPolicy(config.backoff)
        self._cycle_limit = max(0, config.max_calls_per_cycle)
        self._cycle_calls = 0
        self._user_agent = user_agent
        self._stats = RouterStats()
        self._deterministic = DeterministicProvider()
        self._providers: dict[str, ChatProvider] = {}
        if allow_network:
            effective_transport = transport if transport is not None else UrllibTransport()
            for provider_config in config.ordered_enabled():
                provider = self._make_provider(provider_config, effective_transport)
                if provider is not None:
                    self._providers[provider_config.name] = provider

    @property
    def stats(self) -> RouterStats:
        return self._stats

    def _make_provider(self, config: ProviderConfig, transport: Transport) -> ChatProvider | None:
        if config.kind == "openai_compatible":
            return GroqProvider(config, transport, self._user_agent)
        if config.kind == "gemini":
            return GeminiProvider(config, transport, self._user_agent)
        if config.kind == "ollama":
            return OllamaProvider(config, transport, self._user_agent)
        return None

    def complete(
        self,
        task: str,
        messages: Sequence[Message],
        *,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> Completion:
        if not messages:
            raise ValueError("at least one message is required")
        if not 0.0 <= temperature <= 2.0:
            raise ValueError("temperature must be between 0 and 2")
        sanitized = self._sanitize_messages(messages)
        cache_key = self._cache_key(task, sanitized, max_tokens, temperature)
        attempts: list[AttemptRecord] = []

        cached = self._cache.get(cache_key) if self._cache else None
        if cached is not None:
            self._stats.cache_hits += 1
            return Completion(
                text=cached.text,
                provider=cached.provider,
                model=cached.model,
                fallback=cached.provider == "deterministic",
                cached=True,
            )

        cycle_budget_hit = False
        if not self._allow_network:
            attempts.append(
                AttemptRecord(
                    provider="network",
                    model="none",
                    outcome="skipped",
                    status=None,
                    sleep_seconds=0.0,
                    detail="offline mode; deterministic fallback only",
                )
            )
            self._stats.skipped += 1
        else:
            for provider_config in self._config.ordered_enabled():
                if provider_config.kind == "deterministic":
                    continue
                if self._cycle_calls >= self._cycle_limit:
                    cycle_budget_hit = True
                    break
                api_key = self._api_key(provider_config)
                if provider_config.kind in {"openai_compatible", "gemini"} and not api_key:
                    attempts.append(
                        AttemptRecord(
                            provider=provider_config.name,
                            model=provider_config.models[0],
                            outcome="skipped",
                            status=None,
                            sleep_seconds=0.0,
                            detail="missing API key",
                        )
                    )
                    self._stats.skipped += 1
                    continue
                provider = self._providers.get(provider_config.name)
                if provider is None:
                    continue
                completion, exhausted_by_cycle = self._attempt_provider(
                    provider_config,
                    provider,
                    sanitized,
                    api_key=api_key,
                    requested_max_tokens=max_tokens,
                    temperature=temperature,
                    attempts=attempts,
                )
                if completion is not None:
                    if self._cache is not None:
                        self._cache.put(
                            cache_key,
                            text=completion.text,
                            provider=completion.provider,
                            model=completion.model,
                        )
                    return completion
                if exhausted_by_cycle:
                    cycle_budget_hit = True
                    break

        if cycle_budget_hit:
            attempts.append(
                AttemptRecord(
                    provider="budget",
                    model="none",
                    outcome="skipped",
                    status=None,
                    sleep_seconds=0.0,
                    detail="per-cycle inference call cap reached",
                )
            )
            self._stats.skipped += 1

        deterministic_config = self._config.provider("deterministic")
        requested = (
            min(max_tokens, deterministic_config.max_tokens_per_request)
            if max_tokens is not None
            else deterministic_config.max_tokens_per_request
        )
        result = self._deterministic.complete(task, sanitized, max_tokens=requested)
        attempts.append(
            AttemptRecord(
                provider="deterministic",
                model=deterministic_config.models[0],
                outcome="success",
                status=None,
                sleep_seconds=0.0,
                detail="local deterministic fallback",
            )
        )
        self._stats.fallbacks += 1
        return Completion(
            text=result.text,
            provider="deterministic",
            model=deterministic_config.models[0],
            fallback=True,
            cached=False,
            attempts=tuple(attempts),
            usage=result.usage,
        )

    def _attempt_provider(
        self,
        config: ProviderConfig,
        provider: ChatProvider,
        messages: tuple[Message, ...],
        *,
        api_key: str | None,
        requested_max_tokens: int | None,
        temperature: float,
        attempts: list[AttemptRecord],
    ) -> tuple[Completion | None, bool]:
        model_index = 0
        provider_attempts = 0
        max_attempts = max(1, self._config.backoff.max_attempts)
        requested = (
            min(requested_max_tokens, config.max_tokens_per_request)
            if requested_max_tokens is not None
            else config.max_tokens_per_request
        )
        while provider_attempts < max_attempts and model_index < len(config.models):
            if self._cycle_calls >= self._cycle_limit:
                attempts.append(
                    AttemptRecord(
                        provider=config.name,
                        model=config.models[model_index],
                        outcome="skipped",
                        status=None,
                        sleep_seconds=0.0,
                        detail="per-cycle inference call cap reached",
                    )
                )
                return None, True
            model = config.models[model_index]
            self._guard.assert_host_allowed(config.base_url)
            if self._policy is not None:
                self._policy.check_outbound_url(config.base_url)
                host = urlsplit(config.base_url).hostname or config.name
                wait = self._policy.rate_limiter.seconds_until_allowed(host)
                if wait > 0:
                    self._sleep(wait)
            if self._budget is not None and not self._budget.try_consume(
                config.name, config.max_requests_per_day
            ):
                attempts.append(
                    AttemptRecord(
                        provider=config.name,
                        model=model,
                        outcome="skipped",
                        status=None,
                        sleep_seconds=0.0,
                        detail="daily budget exhausted",
                    )
                )
                self._stats.skipped += 1
                return None, False

            self._cycle_calls += 1
            self._stats.network_calls += 1
            self._stats.record_attempt(config.name)
            provider_attempts += 1
            try:
                result = provider.complete(
                    messages,
                    model=model,
                    api_key=api_key,
                    max_tokens=requested,
                    temperature=temperature,
                    timeout=config.timeout_seconds,
                )
            except ProviderError as exc:
                delay = 0.0
                if exc.retryable:
                    delay = self._backoff.delay(provider_attempts - 1)
                    if exc.retry_after is not None:
                        delay = max(delay, min(exc.retry_after, self._config.backoff.max_seconds))
                    if delay > 0:
                        self._sleep(delay)
                attempts.append(
                    AttemptRecord(
                        provider=config.name,
                        model=model,
                        outcome=exc.outcome,
                        status=exc.status,
                        sleep_seconds=delay,
                        detail=str(exc),
                    )
                )
                if exc.auth:
                    return None, False
                if exc.rotate_model:
                    model_index += 1
                if not exc.retryable:
                    return None, False
                continue

            attempts.append(
                AttemptRecord(
                    provider=config.name,
                    model=model,
                    outcome="success",
                    status=200,
                    sleep_seconds=0.0,
                    detail="completion received",
                )
            )
            return (
                Completion(
                    text=result.text,
                    provider=config.name,
                    model=model,
                    fallback=False,
                    cached=False,
                    attempts=tuple(attempts),
                    usage=result.usage,
                ),
                False,
            )
        return None, False

    def complete_many(self, requests: Sequence[InferenceRequest]) -> list[Completion]:
        return [
            self.complete(
                request.task,
                request.messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
            for request in requests
        ]

    def _sanitize_messages(self, messages: Sequence[Message]) -> tuple[Message, ...]:
        sanitized: list[Message] = []
        for message in messages:
            redacted = redact_text(message.content).text
            assert_no_secrets(redacted, context="outbound inference payload")
            sanitized.append(Message(role=message.role, content=redacted))
        return tuple(sanitized)

    def _cache_key(
        self,
        task: str,
        messages: tuple[Message, ...],
        max_tokens: int | None,
        temperature: float,
    ) -> str:
        payload: JsonObject = {
            "schema": 1,
            "task": task,
            "messages": _message_objects(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        return sha256_hex(canonical_json(cast(JsonValue, payload)))

    def _api_key(self, config: ProviderConfig) -> str | None:
        if not config.api_key_env:
            return None
        value = self._environ.get(config.api_key_env, "")
        return value or None
