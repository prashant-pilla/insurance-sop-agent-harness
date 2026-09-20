"""Shared types for phase handlers. Each handler: (TurnContext) -> PhaseResult."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.llm.base import LLMClient
from app.sop.state import Directive, Extraction, Phase, SessionState
from app.tools.data import FixtureStore


@dataclass
class TurnContext:
    state: SessionState
    extraction: Extraction
    message: str
    store: FixtureStore
    settings: Settings
    llm: LLMClient
    llm_calls: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    entered_this_turn: bool = False
    just_verified: bool = False
    progress: bool = False
    handoff_reason: str = ""


@dataclass
class PhaseResult:
    decision: str
    directive: Directive | None = None
    next_phase: Phase | None = None


def verification_exhausted(state: SessionState, settings: Settings) -> bool:
    """Soft stop: ``max_verify_attempts`` mismatching attempts offer a human; one more transfers.

    The attempt at ``== max`` is the offer turn (the door stays open); the attempt after it is the
    grace attempt, and when that one also fails the caller is handed off automatically.
    """
    return state.verify_attempts > settings.max_verify_attempts
