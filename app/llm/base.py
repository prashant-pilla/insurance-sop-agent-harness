"""Provider-neutral LLM interface plus shared HTTP/JSON helpers."""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Callable, TypeVar

import httpx

Message = dict[str, str]

REQUEST_TIMEOUT_SECONDS = 30.0
JSON_ONLY_INSTRUCTION = (
    "Respond with a single valid JSON object and nothing else: no prose, no markdown fences."
)

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)

T = TypeVar("T")


class LLMError(Exception):
    """Transport-level or provider-level failure."""


class LLMRequestError(LLMError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"LLM request failed with status {status_code}: {detail[:200]}")
        self.status_code = status_code


class LLMJSONError(LLMError):
    """The model returned text that is not a JSON object."""


class LLMClient(ABC):
    provider: str
    model: str

    @abstractmethod
    def complete_text(
        self,
        system: str,
        messages: list[Message],
        *,
        max_tokens: int = 400,
        temperature: float = 0.3,
    ) -> str: ...

    @abstractmethod
    def complete_json(
        self,
        system: str,
        messages: list[Message],
        *,
        max_tokens: int = 700,
    ) -> dict[str, Any]: ...


def parse_json_object(text: str) -> dict[str, Any]:
    """Strip code fences and surrounding prose, then parse a JSON object."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMJSONError("no JSON object found in model output")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMJSONError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise LLMJSONError("model output is not a JSON object")
    return data


def timed_call(role: str, model: str, sink: list[dict[str, Any]], fn: Callable[[], T]) -> T:
    """Run an LLM call and record its latency in the per-turn debug sink."""
    started = time.perf_counter()
    try:
        return fn()
    finally:
        sink.append(
            {"role": role, "latency_ms": int((time.perf_counter() - started) * 1000), "model": model}
        )


class HttpLLMClient(LLMClient):
    """Shared httpx plumbing: 30s timeout, one retry on timeout or 5xx."""

    def __init__(self) -> None:
        self._http = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)

    def _post(self, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        last_error: LLMError | None = None
        for attempt in range(2):
            try:
                response = self._http.post(url, headers=headers, json=payload)
            except httpx.TimeoutException as exc:
                last_error = LLMError(f"LLM request timed out: {exc}")
                continue
            except httpx.HTTPError as exc:
                raise LLMError(f"LLM request failed: {exc}") from exc
            if response.status_code >= 500:
                last_error = LLMRequestError(response.status_code, response.text)
                continue
            if response.status_code >= 400:
                raise LLMRequestError(response.status_code, response.text)
            try:
                return response.json()
            except ValueError as exc:
                raise LLMError("LLM response was not JSON") from exc
        assert last_error is not None
        raise last_error
