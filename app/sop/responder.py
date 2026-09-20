"""LLM responder: phrases a Directive as the agent's next reply; drafts email summaries."""

from __future__ import annotations

import json
import re
from typing import Any

from app.llm.base import LLMClient, LLMError, Message, timed_call
from app.sop.prompts import (
    AGENT_NAME,
    COMPANY_NAME,
    EMAIL_SUMMARY_SYSTEM,
    EMAIL_SUMMARY_USER_TEMPLATE,
    FALLBACK_EMAIL_BODY,
    FALLBACK_EMAIL_SUBJECT,
    PHASE_GUIDANCE,
    RESPONDER_CORRECTION_NOTE,
    RESPONDER_SYSTEM,
    TONE_GUIDANCE,
)
from app.sop.state import Directive, IdentitySlots, SessionState, TranscriptTurn
from app.tools.data import normalize_phone

HISTORY_WINDOW = 12


def build_responder_system(directive: Directive, corrections: list[str] | None = None) -> str:
    """System prompt for one reply; ``corrections`` are the guard rules the previous draft broke."""
    payload = directive.model_dump()
    payload["phase_guidance"] = PHASE_GUIDANCE[directive.phase]
    system = RESPONDER_SYSTEM.format(
        agent_name=AGENT_NAME,
        company_name=COMPANY_NAME,
        tone=directive.tone or TONE_GUIDANCE["neutral"],
        directive_json=json.dumps(payload, indent=2),
    )
    if not corrections:
        return system
    items = "\n".join(f"- {correction}" for correction in corrections)
    return system + RESPONDER_CORRECTION_NOTE.format(items=items)


def _history_messages(history: list[TranscriptTurn]) -> list[Message]:
    return [
        {"role": "user" if turn.role == "user" else "assistant", "content": turn.text}
        for turn in history[-HISTORY_WINDOW:]
    ]


class Responder:
    def __init__(self, llm: LLMClient):
        self._llm = llm

    def respond(
        self,
        directive: Directive,
        history: list[TranscriptTurn],
        message: str,
        llm_calls: list[dict[str, Any]],
        *,
        corrections: list[str] | None = None,
    ) -> str:
        system = build_responder_system(directive, corrections)
        messages = [*_history_messages(history), {"role": "user", "content": message}]
        text = timed_call(
            "responder", self._llm.model, llm_calls, lambda: self._llm.complete_text(system, messages)
        )
        return text.strip()


EMAIL_NOTE_MODEL_FAILED = "email_summary: model draft failed, templated body used"
EMAIL_NOTE_PII_IN_DRAFT = "email_summary: caller PII in draft, templated body used"


def _pii_patterns(slots: IdentitySlots) -> list[re.Pattern[str]]:
    """Exact caller values only: dob, phone (digits and common formatting), last-4 as a word, email."""
    patterns: list[re.Pattern[str]] = []
    if slots.dob:
        patterns.append(re.compile(re.escape(slots.dob), re.IGNORECASE))
    digits = normalize_phone(slots.phone)
    if digits:
        patterns.append(re.compile(r"(?:\+?1)?" + r"[\s().-]*".join(re.escape(d) for d in digits)))
    if slots.id_last4:
        patterns.append(re.compile(rf"\b{re.escape(slots.id_last4)}\b"))
    if slots.email:
        patterns.append(re.compile(re.escape(slots.email.strip()), re.IGNORECASE))
    return patterns


def contains_caller_pii(text: str, slots: IdentitySlots) -> bool:
    """True when ``text`` echoes one of the caller's exact identity values. Empty slots: always False."""
    return any(pattern.search(text) for pattern in _pii_patterns(slots))


def draft_email_summary(
    llm: LLMClient,
    state: SessionState,
    claim: dict[str, Any],
    llm_calls: list[dict[str, Any]],
) -> tuple[str, str, str | None]:
    """Return (subject, body, note).

    ``note`` is None when the model draft was used, otherwise a decision line explaining why the
    template was used instead: the model failed, or the draft echoed one of the caller's raw
    identity values (dob, phone, last-4, email).
    """
    transcript = "\n".join(f"{turn.role.upper()}: {turn.text}" for turn in state.transcript)
    prompt = EMAIL_SUMMARY_USER_TEMPLATE.format(
        name=state.policyholder_name or "Policyholder",
        claim_json=json.dumps(claim),
        intent_path=state.intent_path or "general_claim_question",
        transcript=transcript or "(none)",
    )
    system = EMAIL_SUMMARY_SYSTEM.format(company_name=COMPANY_NAME, agent_name=AGENT_NAME)
    try:
        data = timed_call(
            "email_summary",
            llm.model,
            llm_calls,
            lambda: llm.complete_json(system, [{"role": "user", "content": prompt}]),
        )
        subject, body = str(data.get("subject", "")).strip(), str(data.get("body", "")).strip()
        if subject and body:
            if contains_caller_pii(f"{subject}\n{body}", state.slots):
                return (*fallback_email(state, claim), EMAIL_NOTE_PII_IN_DRAFT)
            return subject, body, None
    except LLMError:
        pass
    return (*fallback_email(state, claim), EMAIL_NOTE_MODEL_FAILED)


def fallback_email(state: SessionState, claim: dict[str, Any]) -> tuple[str, str]:
    documents = claim.get("documents_needed") or []
    if documents:
        next_steps = (
            f"Next steps: please submit {' and '.join(documents)} through the member portal or claim "
            f"upload link. The appeal deadline on file is {claim.get('appeal_deadline', 'not specified')}."
        )
    else:
        next_steps = "No further action is needed from you at this time."
    subject = FALLBACK_EMAIL_SUBJECT.format(company_name=COMPANY_NAME, case_id=claim["case_id"])
    body = FALLBACK_EMAIL_BODY.format(
        name=state.policyholder_name or "Policyholder",
        company_name=COMPANY_NAME,
        case_type=claim["case_type"],
        case_id=claim["case_id"],
        status=claim["status"],
        next_steps=next_steps,
        agent_name=AGENT_NAME,
    )
    return subject, body
