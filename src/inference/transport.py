"""Minimal injectable HTTP transport (urllib, no paid SDKs)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from src.core.errors import SASKError
from src.core.types import JsonObject

_MAX_RESPONSE_BYTES = 1_000_000


class TransportError(SASKError):
    """Transient or terminal transport failure with no secret material attached."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: str
    headers: Mapping[str, str]


class Transport(Protocol):
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: JsonObject,
        timeout: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def post_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: JsonObject,
        timeout: float,
    ) -> HttpResponse:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(  # noqa: S310
            url, data=data, headers=dict(headers), method="POST"
        )
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310
                body = response.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
                header_map = {key.lower(): value for key, value in response.headers.items()}
                return HttpResponse(status=response.status, body=body, headers=header_map)
        except HTTPError as exc:
            body = exc.read(_MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            header_map = {key.lower(): value for key, value in exc.headers.items()}
            return HttpResponse(status=exc.code, body=body, headers=header_map)
        except (URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"transport failure: {type(exc).__name__}") from exc
