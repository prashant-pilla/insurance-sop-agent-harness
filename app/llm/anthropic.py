"""Anthropic Messages API client. JSON is prompt-based (no native JSON mode)."""

from __future__ import annotations

from typing import Any

from app.llm.base import JSON_ONLY_INSTRUCTION, HttpLLMClient, LLMError, Message, parse_json_object

ANTHROPIC_VERSION = "2023-06-01"


def _alternating(messages: list[Message]) -> list[Message]:
    """Anthropic requires a leading user turn and strictly alternating roles."""
    result: list[Message] = []
    for msg in messages:
        if not result and msg["role"] != "user":
            continue
        if result and result[-1]["role"] == msg["role"]:
            result[-1] = {"role": msg["role"], "content": f"{result[-1]['content']}\n{msg['content']}"}
        else:
            result.append(dict(msg))
    return result or [{"role": "user", "content": "(no message)"}]


class AnthropicClient(HttpLLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, base_url: str):
        super().__init__()
        self.model = model
        self._url = f"{base_url}/v1/messages"
        self._headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }

    def _chat(self, system: str, messages: list[Message], *, max_tokens: int, temperature: float) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": _alternating(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        data = self._post(self._url, self._headers, payload)
        try:
            return "".join(block.get("text", "") for block in data["content"])
        except (KeyError, TypeError) as exc:
            raise LLMError("unexpected messages payload") from exc

    def complete_text(
        self, system: str, messages: list[Message], *, max_tokens: int = 400, temperature: float = 0.3
    ) -> str:
        return self._chat(system, messages, max_tokens=max_tokens, temperature=temperature)

    def complete_json(self, system: str, messages: list[Message], *, max_tokens: int = 700) -> dict[str, Any]:
        text = self._chat(
            f"{system}\n\n{JSON_ONLY_INSTRUCTION}", messages, max_tokens=max_tokens, temperature=0.0
        )
        return parse_json_object(text)
