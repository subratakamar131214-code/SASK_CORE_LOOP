"""Groq, Google AI Studio, Ollama, and deterministic providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol

from src.core.config import ProviderConfig
from src.core.errors import SASKError
from src.core.types import JsonObject, JsonValue
from src.inference.transport import HttpResponse, Transport, TransportError
from src.inference.types import Message, Usage

_MODEL_ERROR_MARKERS = (
    "model not found",
    "model_not_found",
    "decommission",
    "not supported",
    "unknown model",
    "no longer supported",
)
_AUTH_ERROR_MARKERS = ("api key", "credential", "unauthorized", "permission")


class ProviderError(SASKError):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        rotate_model: bool = False,
        auth: bool = False,
        outcome: str = "provider_error",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.rotate_model = rotate_model
        self.auth = auth
        self.outcome = outcome
        self.retry_after = retry_after


@dataclass(frozen=True)
class ProviderResult:
    text: str
    usage: Usage | None = None


class ChatProvider(Protocol):
    def complete(
        self,
        messages: tuple[Message, ...],
        *,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> ProviderResult: ...


def _message_payload(messages: tuple[Message, ...]) -> list[JsonValue]:
    payload: list[JsonValue] = []
    for message in messages:
        entry: JsonObject = {"role": message.role, "content": message.content}
        payload.append(entry)
    return payload


def _retry_after(headers: dict[str, str] | object) -> float | None:
    if not isinstance(headers, dict):
        return None
    raw = headers.get("retry-after")
    if not isinstance(raw, str):
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _classify_failure(response: HttpResponse) -> ProviderError:
    body = response.body.lower()[:4000]
    retry_after = _retry_after(dict(response.headers))
    if response.status == 429:
        return ProviderError(
            "rate limited",
            status=429,
            retryable=True,
            rotate_model=True,
            outcome="rate_limited",
            retry_after=retry_after,
        )
    if response.status in {401, 403}:
        return ProviderError(
            "authentication or authorization failure",
            status=response.status,
            auth=True,
            outcome="auth_error",
        )
    if response.status == 404:
        return ProviderError(
            "model or endpoint unavailable",
            status=404,
            retryable=True,
            rotate_model=True,
            outcome="model_unavailable",
        )
    if response.status == 408:
        return ProviderError(
            "request timeout",
            status=408,
            retryable=True,
            outcome="timeout",
            retry_after=retry_after,
        )
    if response.status >= 500:
        return ProviderError(
            "provider server error",
            status=response.status,
            retryable=True,
            outcome="server_error",
            retry_after=retry_after,
        )
    if response.status == 400 and any(marker in body for marker in _MODEL_ERROR_MARKERS):
        return ProviderError(
            "model rejected by provider",
            status=400,
            retryable=True,
            rotate_model=True,
            outcome="model_unavailable",
        )
    if response.status == 400 and any(marker in body for marker in _AUTH_ERROR_MARKERS):
        return ProviderError(
            "credential rejected by provider",
            status=400,
            auth=True,
            outcome="auth_error",
        )
    return ProviderError(
        "non-retryable provider error",
        status=response.status,
        outcome="bad_request",
    )


def _request(
    transport: Transport,
    url: str,
    *,
    headers: dict[str, str],
    payload: JsonObject,
    timeout: float,
) -> HttpResponse:
    try:
        return transport.post_json(url, headers=headers, payload=payload, timeout=timeout)
    except TransportError as exc:
        raise ProviderError("transport failure", retryable=True, outcome="transport_error") from exc


def _parse_json(body: str) -> JsonObject:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderError("invalid JSON response", outcome="invalid_response") from exc
    if not isinstance(parsed, dict):
        raise ProviderError("unexpected response shape", outcome="invalid_response")
    return parsed


def _as_mapping(value: JsonValue) -> JsonObject | None:
    return value if isinstance(value, dict) else None


def _as_list(value: JsonValue) -> list[JsonValue] | None:
    return value if isinstance(value, list) else None


def _as_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class GroqProvider:
    def __init__(self, config: ProviderConfig, transport: Transport, user_agent: str) -> None:
        self._config = config
        self._transport = transport
        self._user_agent = user_agent

    def complete(
        self,
        messages: tuple[Message, ...],
        *,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> ProviderResult:
        if not api_key:
            raise ProviderError("missing Groq API key", auth=True, outcome="auth_error")
        payload: JsonObject = {
            "model": model,
            "messages": _message_payload(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        response = _request(
            self._transport,
            f"{self._config.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
            payload=payload,
            timeout=timeout,
        )
        if response.status != 200:
            raise _classify_failure(response)
        data = _parse_json(response.body)
        choices = _as_list(data.get("choices"))
        if not choices:
            raise ProviderError("empty choices", outcome="empty_response")
        first = _as_mapping(choices[0])
        message = _as_mapping(first.get("message")) if first else None
        content = message.get("content") if message else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("empty content", outcome="empty_response")
        usage_doc = _as_mapping(data.get("usage"))
        usage = Usage(
            prompt_tokens=_as_int(usage_doc.get("prompt_tokens")) if usage_doc else None,
            completion_tokens=_as_int(usage_doc.get("completion_tokens")) if usage_doc else None,
        )
        return ProviderResult(text=content.strip(), usage=usage)


class GeminiProvider:
    def __init__(self, config: ProviderConfig, transport: Transport, user_agent: str) -> None:
        self._config = config
        self._transport = transport
        self._user_agent = user_agent

    def complete(
        self,
        messages: tuple[Message, ...],
        *,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> ProviderResult:
        if not api_key:
            raise ProviderError("missing Google AI Studio API key", auth=True, outcome="auth_error")
        system_parts: list[JsonValue] = []
        contents: list[JsonValue] = []
        for message in messages:
            if message.role == "system":
                system_parts.append({"text": message.content})
                continue
            entry: JsonObject = {
                "role": "model" if message.role == "assistant" else "user",
                "parts": [{"text": message.content}],
            }
            contents.append(entry)
        payload: JsonObject = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}
        model_name = model.removeprefix("models/")
        response = _request(
            self._transport,
            f"{self._config.base_url}/models/{model_name}:generateContent",
            headers={
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
                "User-Agent": self._user_agent,
            },
            payload=payload,
            timeout=timeout,
        )
        if response.status != 200:
            raise _classify_failure(response)
        data = _parse_json(response.body)
        candidates = _as_list(data.get("candidates"))
        if not candidates:
            feedback = _as_mapping(data.get("promptFeedback"))
            reason = feedback.get("blockReason") if feedback else None
            detail = f"blocked ({reason})" if isinstance(reason, str) else "no candidates"
            raise ProviderError(detail, outcome="empty_response")
        first = _as_mapping(candidates[0])
        content = _as_mapping(first.get("content")) if first else None
        parts = _as_list(content.get("parts")) if content else None
        texts = [
            str(part.get("text"))
            for part in (parts or [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        text = "".join(texts).strip()
        if not text:
            raise ProviderError("empty content", outcome="empty_response")
        usage_doc = _as_mapping(data.get("usageMetadata"))
        usage = Usage(
            prompt_tokens=_as_int(usage_doc.get("promptTokenCount")) if usage_doc else None,
            completion_tokens=(
                _as_int(usage_doc.get("candidatesTokenCount")) if usage_doc else None
            ),
        )
        return ProviderResult(text=text, usage=usage)


class OllamaProvider:
    def __init__(self, config: ProviderConfig, transport: Transport, user_agent: str) -> None:
        self._config = config
        self._transport = transport
        self._user_agent = user_agent

    def complete(
        self,
        messages: tuple[Message, ...],
        *,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
    ) -> ProviderResult:
        del api_key
        payload: JsonObject = {
            "model": model,
            "messages": _message_payload(messages),
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }
        response = _request(
            self._transport,
            f"{self._config.base_url}/api/chat",
            headers={"Content-Type": "application/json", "User-Agent": self._user_agent},
            payload=payload,
            timeout=timeout,
        )
        if response.status != 200:
            raise _classify_failure(response)
        data = _parse_json(response.body)
        message = _as_mapping(data.get("message"))
        content = message.get("content") if message else None
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("empty content", outcome="empty_response")
        return ProviderResult(
            text=content.strip(),
            usage=Usage(
                prompt_tokens=_as_int(data.get("prompt_eval_count")),
                completion_tokens=_as_int(data.get("eval_count")),
            ),
        )


_TEMPLATES: dict[str, str] = {
    "triage": (
        "Deterministic triage (offline): prioritize reproducible bugs and customer complaints, "
        "then feature requests. Reproduce the top issue, add a regression test, document the fix, "
        "and reply only with public, sanitized information."
    ),
    "niche_brief": (
        "Deterministic niche brief (offline): keep one high-pain developer niche, "
        "ship the smallest working open-source CLI, and validate demand through "
        "opt-in issues and sponsor links. No revenue is guaranteed and no paid "
        "placement is used."
    ),
    "product_readme": (
        "Deterministic README draft (offline): state the problem, exact CLI usage, limitations, "
        "privacy behavior, MIT license, and a zero-cost support path. No performance or revenue "
        "claims are made without evidence."
    ),
    "upgrade_proposal": (
        "Deterministic upgrade proposal (offline): derive changes from verified "
        "tests and sanitized feedback only. Protected safety files are out of scope; "
        "any code change requires human review."
    ),
    "release_notes": (
        "Deterministic state release (offline): summarize verified maintenance changes, sanitized "
        "telemetry counts, and known limitations. This release makes no financial promises."
    ),
    "support_reply": (
        "Thanks for the report. Please share a minimal reproduction with secrets and personal data "
        "removed. Maintainers will prioritize reproducible customer-impacting issues first."
    ),
    "generic": (
        "Offline deterministic response: cloud inference is disabled or unavailable. No external "
        "data was sent. The pipeline continues using local templates."
    ),
}


class DeterministicProvider:
    def complete(
        self,
        task: str,
        messages: tuple[Message, ...],
        *,
        max_tokens: int,
    ) -> ProviderResult:
        template = _TEMPLATES.get(task, _TEMPLATES["generic"])
        context = "\n".join(message.content for message in messages)
        digest = sha256(context.encode("utf-8")).hexdigest()[:12]
        text = f"{template}\n\nOffline fallback digest: {digest}. No cloud endpoint was contacted."
        return ProviderResult(text=text[: max(0, max_tokens * 4)])
