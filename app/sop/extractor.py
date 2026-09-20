"""LLM extractor: message -> strict Extraction JSON, then deterministic normalization."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from app.llm.base import LLMClient, LLMJSONError, Message, timed_call
from app.sop.prompts import EXTRACTOR_RETRY_HINT, EXTRACTOR_SYSTEM, EXTRACTOR_USER_TEMPLATE
from app.sop.state import Extraction, IdentitySlots, TranscriptTurn
from app.tools.data import (
    canonical_case_type,
    canonical_status,
    normalize_date,
    normalize_email,
    normalize_last4,
    normalize_phone,
    normalize_policy_number,
)

logger = logging.getLogger(__name__)

# The extractor only needs enough context to resolve short answers; the responder
# (app/sop/responder.py) keeps a longer window so it can refer back to things the caller said
# several turns ago.
HISTORY_WINDOW = 6


def format_history(history: list[TranscriptTurn]) -> str:
    if not history:
        return "(none)"
    return "\n".join(f"{turn.role.upper()}: {turn.text}" for turn in history)


def normalize_extraction(extraction: Extraction) -> Extraction:
    """Apply deterministic normalizers regardless of what the model emitted."""
    update: dict[str, Any] = {
        "dob": normalize_date(extraction.dob) or extraction.dob,
        "phone": normalize_phone(extraction.phone) or extraction.phone,
        "email": normalize_email(extraction.email) or extraction.email,
        "id_last4": normalize_last4(extraction.id_last4) or extraction.id_last4,
        "policy_number": normalize_policy_number(extraction.policy_number),
    }
    hints = extraction.case_hints.model_copy()
    hints.case_type = canonical_case_type(hints.case_type) or hints.case_type
    hints.status = canonical_status(hints.status) or hints.status
    update["case_hints"] = hints
    # A message carrying identity data is about this conversation, whatever label the model chose.
    if extraction.slots().provided():
        update["scope"] = "in_scope_claim"
    return extraction.model_copy(update=update)


class Extractor:
    def __init__(self, llm: LLMClient):
        self._llm = llm

    def extract(
        self,
        message: str,
        history: list[TranscriptTurn],
        slots: IdentitySlots,
        phase: str,
        llm_calls: list[dict[str, Any]],
    ) -> Extraction:
        known = [name for name in slots.provided() if name != "policy_number"]
        prompt = EXTRACTOR_USER_TEMPLATE.format(
            phase=phase,
            known_slots=", ".join(known) or "none",
            policy_number=slots.policy_number or "unknown",
            history=format_history(history[-HISTORY_WINDOW:]),
            message=message,
        )
        messages: list[Message] = [{"role": "user", "content": prompt}]
        try:
            return normalize_extraction(self._call(messages, llm_calls))
        except (LLMJSONError, ValidationError) as exc:
            hint = EXTRACTOR_RETRY_HINT.format(error=type(exc).__name__)
            retry = [*messages, {"role": "user", "content": hint}]
            try:
                return normalize_extraction(self._call(retry, llm_calls))
            except (LLMJSONError, ValidationError):
                logger.warning("extractor returned invalid output twice; using empty extraction")
                return Extraction()

    def _call(self, messages: list[Message], llm_calls: list[dict[str, Any]]) -> Extraction:
        data = timed_call(
            "extractor", self._llm.model, llm_calls, lambda: self._llm.complete_json(EXTRACTOR_SYSTEM, messages)
        )
        return Extraction.model_validate(data)
