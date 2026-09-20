from __future__ import annotations

from app.config import Settings
from app.llm.anthropic import AnthropicClient
from app.llm.base import LLMClient
from app.llm.openai_compat import OpenAICompatClient


def build_llm(settings: Settings) -> LLMClient | None:
    """Return a configured client, or None when no API key is present."""
    if not settings.llm_api_key:
        return None
    if settings.llm_provider == "anthropic":
        return AnthropicClient(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
    return OpenAICompatClient(settings.llm_api_key, settings.llm_model, settings.llm_base_url)
