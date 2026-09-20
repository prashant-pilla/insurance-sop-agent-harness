"""Chat-completions client for OpenAI and OpenAI-compatible hosts."""

from __future__ import annotations

from typing import Any

from app.llm.base import (
    JSON_ONLY_INSTRUCTION,
    HttpLLMClient,
    LLMError,
    LLMRequestError,
    Message,
    parse_json_object,
)


class OpenAICompatClient(HttpLLMClient):
    provider = "openai"

    def __init__(self, api_key: str, model: str, base_url: str):
        super().__init__()
        self.model = model
        self._url = f"{base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._json_mode_supported = True

    def _chat(
        self,
        system: str,
        messages: list[Message],
        *,
        max_tokens: int,
        temperature: float,
        response_format: dict[str, str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format
        data = self._post(self._url, self._headers, payload)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("unexpected chat completion payload") from exc

    def complete_text(
        self, system: str, messages: list[Message], *, max_tokens: int = 400, temperature: float = 0.3
    ) -> str:
        return self._chat(system, messages, max_tokens=max_tokens, temperature=temperature)

    def complete_json(self, system: str, messages: list[Message], *, max_tokens: int = 700) -> dict[str, Any]:
        system_json = f"{system}\n\n{JSON_ONLY_INSTRUCTION}"
        if self._json_mode_supported:
            try:
                text = self._chat(
                    system_json,
                    messages,
                    max_tokens=max_tokens,
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return parse_json_object(text)
            except LLMRequestError as exc:
                if exc.status_code != 400:
                    raise
                self._json_mode_supported = False
        text = self._chat(system_json, messages, max_tokens=max_tokens, temperature=0.0)
        return parse_json_object(text)
