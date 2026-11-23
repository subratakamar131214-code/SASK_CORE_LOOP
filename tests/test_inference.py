"""Provider failover, budgets, caching, and outbound sanitization."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from src.core.io import read_json
from src.inference.budget import DailyBudget
from src.inference.cache import ResponseCache
from src.inference.router import InferenceRouter
from src.inference.transport import HttpResponse
from src.inference.types import Message

from helpers import fake_gemini_key, fake_groq_key, unquoted_assignment


class FakeTransport:
    def __init__(self, responses: list[HttpResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    def post_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> HttpResponse:
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        if self.responses:
            return self.responses.pop(0)
        return HttpResponse(status=500, body="{}", headers={})


def groq_response(text: str = "groq reply") -> HttpResponse:
    body = {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }
    return HttpResponse(status=200, body=json.dumps(body), headers={})


def gemini_response(text: str = "gemini reply") -> HttpResponse:
    body = {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2},
    }
    return HttpResponse(status=200, body=json.dumps(body), headers={})


def error_response(status: int, *, retry_after: str | None = None) -> HttpResponse:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return HttpResponse(status=status, body="{}", headers=headers)


def make_router(
    config,
    guard,
    tmp_path: Path,
    responses: list[HttpResponse] | None = None,
    *,
    online: bool = True,
    environ: dict[str, str] | None = None,
    cache: ResponseCache | None = None,
    budget_limit: int = 100,
    max_calls: int | None = None,
    sleeps: list[float] | None = None,
) -> tuple[InferenceRouter, FakeTransport]:
    inference = config.inference
    if max_calls is not None:
        inference = replace(inference, max_calls_per_cycle=max_calls)
    transport = FakeTransport(responses)
    budget = DailyBudget(
        tmp_path / "budget.json", global_limit=budget_limit, dry_run=True
    )
    environment = environ if environ is not None else {
        "GROQ_API_KEY": fake_groq_key(),
        "GEMINI_API_KEY": fake_gemini_key(),
    }
    delays = sleeps if sleeps is not None else []

    def record_sleep(seconds: float) -> None:
        delays.append(seconds)

    router = InferenceRouter(
        inference,
        guard,
        allow_network=online,
        transport=transport,
        budget=budget,
        cache=cache,
        environ=environment,
        sleep=record_sleep,
    )
    return router, transport


def test_offline_mode_never_touches_the_network(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(config, guard, tmp_path, online=False)
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "deterministic"
    assert completion.fallback is True
    assert "Offline fallback digest" in completion.text
    assert transport.calls == []
    assert router.stats.skipped == 1


def test_missing_keys_skip_to_deterministic(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(config, guard, tmp_path, environ={})
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "deterministic"
    assert transport.calls == []
    details = " ".join(attempt.detail for attempt in completion.attempts)
    assert "missing API key" in details


def test_rate_limit_rotates_models_then_fails_over(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(
        config,
        guard,
        tmp_path,
        [error_response(429), error_response(429), gemini_response()],
    )
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "gemini"
    assert completion.fallback is False
    assert [call["url"].split("/")[2] for call in transport.calls] == [
        "api.groq.com",
        "api.groq.com",
        "generativelanguage.googleapis.com",
    ]
    assert [attempt.status for attempt in completion.attempts] == [429, 429, 200]


def test_auth_failure_moves_on_without_retry(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(
        config, guard, tmp_path, [error_response(401), gemini_response()]
    )
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "gemini"
    assert len([call for call in transport.calls if "groq" in call["url"]]) == 1
    assert completion.attempts[0].outcome == "auth_error"


def test_server_errors_retry_until_the_attempt_cap(config, guard, tmp_path: Path) -> None:
    responses = [error_response(503) for _ in range(4)] + [gemini_response()]
    router, transport = make_router(config, guard, tmp_path, responses)
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "gemini"
    groq_calls = [call for call in transport.calls if "groq" in call["url"]]
    assert len(groq_calls) == config.inference.backoff.max_attempts


def test_model_churn_rotates_to_the_next_model(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(
        config, guard, tmp_path, [error_response(404), groq_response("second model")]
    )
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "groq"
    assert completion.model == config.inference.provider("groq").models[1]
    assert completion.text == "second model"


def test_retry_after_is_capped_by_backoff_max(config, guard, tmp_path: Path) -> None:
    sleeps: list[float] = []
    router, _ = make_router(
        config,
        guard,
        tmp_path,
        [error_response(408, retry_after="9999"), groq_response()],
        sleeps=sleeps,
    )
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert completion.provider == "groq"
    assert sleeps
    assert max(sleeps) <= config.inference.backoff.max_seconds
    assert sleeps[0] == config.inference.backoff.max_seconds


def test_per_cycle_call_cap_forces_deterministic_fallback(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(
        config, guard, tmp_path, [error_response(500)], max_calls=1
    )
    first = router.complete("triage", (Message(role="user", content="one"),))
    second = router.complete("triage", (Message(role="user", content="two"),))
    assert first.provider == "deterministic"
    assert second.provider == "deterministic"
    assert router.stats.network_calls == 1
    assert len(transport.calls) == 1
    details = " ".join(attempt.detail for attempt in second.attempts)
    assert "per-cycle inference call cap reached" in details


def test_daily_budget_stops_calls(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(
        config,
        guard,
        tmp_path,
        [groq_response(), groq_response()],
        budget_limit=1,
    )
    first = router.complete("triage", (Message(role="user", content="one"),))
    second = router.complete("triage", (Message(role="user", content="two"),))
    assert first.provider == "groq"
    assert second.provider == "deterministic"
    assert router.stats.network_calls == 1
    assert len(transport.calls) == 1
    assert "daily budget exhausted" in " ".join(
        attempt.detail for attempt in second.attempts
    )


def test_cache_short_circuits_repeated_requests(config, guard, tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache = ResponseCache(cache_path, ttl_hours=24)
    router, transport = make_router(
        config, guard, tmp_path, [groq_response("cached text")], cache=cache
    )
    first = router.complete("triage", (Message(role="user", content="same"),))
    second = router.complete("triage", (Message(role="user", content="same"),))
    assert first.provider == "groq"
    assert second.cached is True
    assert second.text == "cached text"
    assert len(transport.calls) == 1
    assert router.stats.cache_hits == 1


def test_cache_never_stores_unredacted_values(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache.json"
    cache = ResponseCache(cache_path, ttl_hours=24)
    cache.put("key", text=unquoted_assignment(), provider="groq", model="m")
    stored = read_json(cache_path, {})
    assert isinstance(stored, dict)
    entry = stored["entries"]["key"]
    assert isinstance(entry, dict)
    assert "[REDACTED:SECRET]" in entry["text"]
    assert "abc123def456" not in entry["text"]


def test_outbound_messages_are_sanitized(config, guard, tmp_path: Path) -> None:
    router, transport = make_router(config, guard, tmp_path, [groq_response()])
    router.complete(
        "triage",
        (Message(role="user", content="contact ops@example.com for details"),),
    )
    payload = transport.calls[0]["payload"]
    assert isinstance(payload, dict)
    messages = payload["messages"]
    assert isinstance(messages, list)
    content = messages[0]["content"]
    assert "[REDACTED:EMAIL]" in content
    assert "ops@example.com" not in content


def test_api_keys_never_appear_in_completion_records(config, guard, tmp_path: Path) -> None:
    key = fake_groq_key()
    router, _ = make_router(
        config,
        guard,
        tmp_path,
        [error_response(500), groq_response()],
        environ={"GROQ_API_KEY": key},
    )
    completion = router.complete("triage", (Message(role="user", content="diagnose"),))
    assert key not in json.dumps(completion.to_json())


def test_request_validation(config, guard, tmp_path: Path) -> None:
    router, _ = make_router(config, guard, tmp_path, online=False)
    with pytest.raises(ValueError, match="at least one message"):
        router.complete("triage", ())
    with pytest.raises(ValueError, match="temperature"):
        router.complete(
            "triage", (Message(role="user", content="x"),), temperature=9.0
        )
