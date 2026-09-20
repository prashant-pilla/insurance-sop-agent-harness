"""Environment-driven settings. Only LLM_API_KEY is required to talk to a model."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}


@dataclass(frozen=True)
class Settings:
    llm_provider: str
    llm_api_key: str | None
    llm_model: str
    llm_base_url: str
    max_verify_attempts: int = 3
    max_off_topic: int = 3
    max_frustration_turns: int = 3
    trace_enabled: bool = True
    fixtures_dir: Path = REPO_ROOT / "apps" / "insurance_claims" / "fixtures"
    static_dir: Path = REPO_ROOT / "app" / "static"
    traces_dir: Path = REPO_ROOT / "traces"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer, got '{raw}'") from None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    load_dotenv(REPO_ROOT / ".env", override=False)
    provider = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"Unsupported LLM_PROVIDER '{provider}'; use openai or anthropic")
    api_key = (os.getenv("LLM_API_KEY") or "").strip() or None
    model = (os.getenv("LLM_MODEL") or "").strip() or DEFAULT_MODELS[provider]
    base_url = (os.getenv("LLM_BASE_URL") or "").strip() or DEFAULT_BASE_URLS[provider]
    return Settings(
        llm_provider=provider,
        llm_api_key=api_key,
        llm_model=model,
        llm_base_url=base_url.rstrip("/"),
        max_verify_attempts=_env_int("MAX_VERIFY_ATTEMPTS", 3),
        max_off_topic=_env_int("MAX_OFF_TOPIC", 3),
        max_frustration_turns=_env_int("MAX_FRUSTRATION_TURNS", 3),
        trace_enabled=_env_bool("TRACE_ENABLED", True),
    )
